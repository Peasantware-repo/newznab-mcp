#!/usr/bin/env python3
"""newznab-mcp — search multiple Newznab usenet indexers in parallel through MCP.

A single-file MCP stdio server (stdlib + the `mcp` SDK) that fans a search out to
every configured Newznab/Torznab indexer concurrently, merges and de-duplicates the
results, and returns them tagged with their source indexer(s).

It exists so an agent (or the peasant-gateway, which brokers it as a capability) can
ask "find me this film/TV episode/book" and get one answer across all indexers, without
knowing which indexer holds what or leaking any indexer API key.

Configuration
-------------
Indexers come from one of (in order):

1. ``NEWZNAB_INDEXERS_FILE`` — path to a JSON file::

     [{"name": "nzbfinder", "base_url": "https://nzbfinder.ws/api", "api_key_env": "NZBFINDER_API_KEY"},
      {"name": "nzbgeek",  "base_url": "https://api.nzbgeek.info/api", "api_key_env": "NZBGEEK_API_KEY"}]

2. ``NEWZNAB_INDEXERS`` — the same JSON inline.

``api_key_env`` names an environment variable that holds that indexer's API key; keys
never live in the config file. An indexer may instead set ``api_key`` directly, but that
is discouraged (never commit it).

Run
---
    python3 newznab_mcp.py --mcp-stdio                 # MCP stdio server
    python3 newznab_mcp.py                             # one-shot: print per-indexer caps

API keys are read only from the environment, never from tool arguments, and are never
echoed. Indexers answer in XML (RSS + newznab attributes); this module parses that into
JSON dicts.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

_NZ = "{http://www.newznab.com/DTD/2010/feeds/attributes/}"
_UA = "newznab-mcp/0.1"
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Friendly operation name -> caps "searching" key, and the API `t=` value.
_CAPS_KEY = {
    "search": "search",
    "tvsearch": "tv-search",
    "movie": "movie-search",
    "book": "book-search",
    "audio": "audio-search",
}
_T_VALUE = {
    "search": "search",
    "tvsearch": "tvsearch",
    "movie": "movie",
    "book": "book",
    "audio": "audio",
}


class IndexerError(RuntimeError):
    """A single indexer failed; the caller decides whether that fails the whole search."""


# --------------------------------------------------------------------------- config

def load_indexers(env: dict | None = None) -> list["Indexer"]:
    env = env if env is not None else os.environ
    path = (env.get("NEWZNAB_INDEXERS_FILE") or "").strip()
    raw = (env.get("NEWZNAB_INDEXERS") or "").strip()
    if path and os.path.exists(path):
        data = json.load(open(path, encoding="utf-8"))
    elif raw:
        data = json.loads(raw)
    else:
        raise RuntimeError(
            "no indexers configured: set NEWZNAB_INDEXERS_FILE or NEWZNAB_INDEXERS"
        )
    indexers: list[Indexer] = []
    for entry in data:
        name = entry["name"]
        base_url = entry["base_url"]
        key_env = entry.get("api_key_env")
        api_key = env.get(key_env, "") if key_env else entry.get("api_key", "")
        indexers.append(Indexer(name, base_url, api_key))
    return indexers


# --------------------------------------------------------------------------- client

def _error_text(body: str) -> str:
    try:
        root = ET.fromstring(body)
        e = root.find("error")
        if e is not None:
            return e.get("description", body[:160])
    except ET.ParseError:
        pass
    return body[:160]


def _parse_caps(text: str) -> dict:
    root = ET.fromstring(text)
    out: dict = {}
    server = root.find("server")
    out["server"] = dict(server.attrib) if server is not None else {}
    limits = root.find("limits")
    out["limits"] = dict(limits.attrib) if limits is not None else {}
    reg = root.find("registration")
    out["registration"] = dict(reg.attrib) if reg is not None else {}
    searching: dict = {}
    snode = root.find("searching")
    if snode is not None:
        for m in list(snode):
            searching[m.tag] = dict(m.attrib)
    out["searching"] = searching
    cats: list = []
    cnode = root.find("categories")
    if cnode is not None:
        for c in cnode.findall("category"):
            cats.append(
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "subcats": [dict(s.attrib) for s in c.findall("subcat")],
                }
            )
    out["categories"] = cats
    return out


def _parse_rss(text: str) -> dict:
    root = ET.fromstring(text)
    channel = root.find("channel")
    if channel is None:
        raise IndexerError(_error_text(text))
    out: dict = {"total": None, "offset": None, "releases": []}
    resp = channel.find(f"{_NZ}response")
    if resp is not None:
        try:
            out["total"] = int(resp.get("total", 0))
            out["offset"] = int(resp.get("offset", 0))
        except (TypeError, ValueError):
            pass
    for item in channel.findall("item"):
        r: dict = {
            "title": (item.findtext("title") or "").strip(),
            "guid": (item.findtext("guid") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "pubDate": (item.findtext("pubDate") or "").strip(),
            "category": [c.text.strip() for c in item.findall("category") if c.text],
            "size": None,
            "attrs": {},
        }
        enc = item.find("enclosure")
        if enc is not None:
            length = enc.get("length")
            if length is not None and length.isdigit():
                r["size"] = int(length)
        for a in item.findall(f"{_NZ}attr"):
            r["attrs"][a.get("name")] = a.get("value")
        out["releases"].append(r)
    return out


class Indexer:
    def __init__(self, name: str, base_url: str, api_key: str) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._caps: dict | None = None

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.base_url).hostname or ""

    def _get(self, params: dict) -> str:
        if not self.api_key:
            raise IndexerError(f"{self.name}: no API key configured")
        q = dict(params)
        q["apikey"] = self.api_key
        url = self.base_url + "?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as he:
            body = he.read().decode("utf-8", "replace")
            raise IndexerError(
                f"{self.name}: HTTP {he.code} {_error_text(body)}"
            ) from None
        except Exception as ex:
            raise IndexerError(f"{self.name}: {ex}") from None

    def caps(self) -> dict:
        if self._caps is None:
            self._caps = _parse_caps(self._get({"t": "caps"}))
        return self._caps

    def search(self, t: str, params: dict) -> dict:
        return _parse_rss(self._get({"t": t, **params}))

    def details(self, release_id: str) -> dict:
        return _parse_rss(self._get({"t": "details", "id": release_id}))


# --------------------------------------------------------------------------- search

def _release_id(value: str) -> str:
    """A release's UUID, whether handed a bare id or the details guid URL."""
    value = (value or "").strip()
    m = _UUID_RE.search(value)
    return m.group(0) if m else value


def _supported_params(mode_caps: dict) -> set[str]:
    raw = mode_caps.get("supportedParams", "") or ""
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


_UNIVERSAL = {"cat", "limit", "group", "attrs", "extended", "maxage", "minage"}


def _build_params(mode_caps: dict, provided: dict, mode: str) -> dict:
    supported = _supported_params(mode_caps)
    params: dict = {}
    for k, v in provided.items():
        if v in (None, "", 0, False):
            continue
        kk = k.lower()
        if kk in _UNIVERSAL:
            params[k] = "1" if v is True else str(v)
        elif kk in supported:
            params[k] = str(v)
    return params


def _dedupe_key(rel: dict) -> str | None:
    title = re.sub(r"\s+", " ", (rel.get("title") or "").strip().lower())
    if not title:
        return None
    size = rel.get("size")
    return f"{title}|{size}" if size is not None else title


def _dedupe(releases: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    out: list[dict] = []
    for rel in releases:
        key = _dedupe_key(rel)
        if key and key in seen:
            prev = seen[key]
            prev["found_in"] = sorted(
                set(prev.get("found_in", [prev["indexer"]]) + [rel["indexer"]])
            )
            continue
        rel["found_in"] = [rel["indexer"]]
        if key:
            seen[key] = rel
        out.append(rel)
    return out


class SearchEngine:
    def __init__(self, indexers: list[Indexer]) -> None:
        self.indexers = indexers
        self.by_name = {ix.name: ix for ix in indexers}

    def _select(self, indexers_filter: str) -> list[Indexer]:
        names = [n.strip() for n in (indexers_filter or "").split(",") if n.strip()]
        if not names:
            return list(self.indexers)
        missing = [n for n in names if n not in self.by_name]
        if missing:
            raise IndexerError(f"unknown indexer(s): {', '.join(missing)}")
        return [self.by_name[n] for n in names]

    def caps(self, indexer: str = "") -> dict:
        out: dict = {}
        for ix in self._select(indexer):
            try:
                out[ix.name] = ix.caps()
            except Exception as ex:
                out[ix.name] = {"error": str(ex)[:200]}
        return out

    def search(self, mode: str, provided: dict, indexers_filter: str = "") -> dict:
        selected = self._select(indexers_filter)
        caps_key = _CAPS_KEY[mode]
        t_value = _T_VALUE[mode]

        # Pre-fetch caps (single-threaded) so unsupported modes are skipped up front.
        plan: list[tuple[Indexer, dict]] = []
        indexers_meta: dict = {}
        for ix in selected:
            try:
                mode_caps = ix.caps()["searching"].get(caps_key)
            except Exception as ex:
                indexers_meta[ix.name] = {"status": "error", "error": str(ex)[:200]}
                continue
            if not mode_caps or mode_caps.get("available") != "yes":
                indexers_meta[ix.name] = {"status": "unsupported"}
                continue
            plan.append((ix, _build_params(mode_caps, provided, mode)))

        releases: list[dict] = []
        errors: dict = {}
        if plan:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(plan)) as ex:
                futures = {
                    ex.submit(ix.search, t_value, params): ix for ix, params in plan
                }
                for fut in concurrent.futures.as_completed(futures):
                    ix = futures[fut]
                    try:
                        result = fut.result()
                    except Exception as exc:
                        errors[ix.name] = str(exc)[:200]
                        indexers_meta[ix.name] = {"status": "error", "error": str(exc)[:200]}
                        continue
                    indexers_meta[ix.name] = {"status": "ok", "total": result.get("total")}
                    for rel in result.get("releases", []):
                        rel["indexer"] = ix.name
                        releases.append(rel)

        merged = _dedupe(releases)
        out: dict = {
            "releases": merged,
            "total": len(merged),
            "indexers": indexers_meta,
        }
        if errors:
            out["errors"] = errors
        return out

    def details(self, release_id: str, indexer: str = "") -> dict:
        ix = self._pick_indexer(release_id, indexer)
        result = ix.details(_release_id(release_id))
        rel = result["releases"][0] if result["releases"] else None
        if rel is not None:
            rel["indexer"] = ix.name
        return {"release": rel, "indexer": ix.name}

    def _pick_indexer(self, release_id: str, indexer: str) -> Indexer:
        if indexer:
            ix = self.by_name.get(indexer.strip())
            if ix is None:
                raise IndexerError(f"unknown indexer: {indexer}")
            return ix
        # Infer from a guid URL whose host matches a configured indexer.
        host = urllib.parse.urlsplit(release_id).hostname or ""
        for ix in self.indexers:
            if host and host == ix.host:
                return ix
        if len(self.indexers) == 1:
            return self.indexers[0]
        raise IndexerError("multiple indexers; pass the indexer name")


# --------------------------------------------------------------------------- MCP server

def _engine() -> SearchEngine:
    return SearchEngine(load_indexers())


def serve_mcp() -> None:
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    server = MCPServer("newznab-mcp")
    read = ToolAnnotations(read_only_hint=True, idempotent_hint=True)
    eng = _engine()

    @server.tool(name="caps", annotations=read)
    def caps_tool(indexer: str = "") -> dict:
        """Per-indexer capabilities: search modes, supported parameters, categories and limits."""
        return eng.caps(indexer)

    @server.tool(name="search", annotations=read)
    def search_tool(
        q: str,
        group: str = "",
        attrs: str = "",
        extended: bool = False,
        cat: str = "",
        maxage: int = 0,
        minage: int = 0,
        limit: int = 100,
        indexers: str = "",
    ) -> dict:
        """Keyword search across every indexer, optionally filtered by newsgroup, category, attributes, age and indexer list."""
        return eng.search(
            "search",
            {"q": q, "group": group, "attrs": attrs, "extended": extended,
             "cat": cat, "maxage": maxage, "minage": minage, "limit": limit},
            indexers,
        )

    @server.tool(name="tvsearch", annotations=read)
    def tvsearch_tool(
        q: str = "",
        tvdbid: str = "",
        imdbid: str = "",
        tmdbid: str = "",
        traktid: str = "",
        rid: str = "",
        tvmazeid: str = "",
        season: str = "",
        ep: str = "",
        cat: str = "",
        limit: int = 100,
        indexers: str = "",
    ) -> dict:
        """Search TV by title or id, narrowed to a season and episode where given."""
        if not (q or tvdbid or imdbid or tmdbid or traktid or rid or tvmazeid):
            raise RuntimeError("tvsearch needs a title or one of tvdbid/imdbid/tmdbid/traktid/rid/tvmazeid")
        return eng.search(
            "tvsearch",
            {"q": q, "tvdbid": tvdbid, "imdbid": imdbid, "tmdbid": tmdbid,
             "traktid": traktid, "rid": rid, "tvmazeid": tvmazeid,
             "season": season, "ep": ep, "cat": cat, "limit": limit},
            indexers,
        )

    @server.tool(name="movie", annotations=read)
    def movie_tool(
        q: str = "",
        imdbid: str = "",
        tmdbid: str = "",
        traktid: str = "",
        genre: str = "",
        cat: str = "",
        limit: int = 100,
        indexers: str = "",
    ) -> dict:
        """Search films by title or imdbid/tmdbid/traktid, with an optional genre."""
        if not (q or imdbid or tmdbid or traktid):
            raise RuntimeError("movie needs a title or one of imdbid/tmdbid/traktid")
        return eng.search(
            "movie",
            {"q": q, "imdbid": imdbid, "tmdbid": tmdbid, "traktid": traktid,
             "genre": genre, "cat": cat, "limit": limit},
            indexers,
        )

    @server.tool(name="book", annotations=read)
    def book_tool(
        title: str = "",
        author: str = "",
        isbn: str = "",
        cat: str = "",
        limit: int = 100,
        indexers: str = "",
    ) -> dict:
        """Search books by title, author or ISBN."""
        if not (title or author or isbn):
            raise RuntimeError("book needs a title, author or isbn")
        return eng.search(
            "book",
            {"title": title, "author": author, "isbn": isbn, "cat": cat, "limit": limit},
            indexers,
        )

    @server.tool(name="audio", annotations=read)
    def audio_tool(
        q: str,
        cat: str = "",
        limit: int = 100,
        indexers: str = "",
    ) -> dict:
        """Search music/audio by keyword."""
        return eng.search(
            "audio",
            {"q": q, "cat": cat, "limit": limit},
            indexers,
        )

    @server.tool(name="details", annotations=read)
    def details_tool(id: str, indexer: str = "") -> dict:
        """Full metadata for one release by its guid (from a search result) or id."""
        return eng.details(id, indexer)

    server.run(transport="stdio")


def main() -> int:
    try:
        eng = _engine()
        for ix in eng.indexers:
            caps = ix.caps()
            modes = list(caps.get("searching", {}).keys())
            print(
                json.dumps(
                    {"indexer": ix.name, "ok": True, "modes": modes,
                     "categories": [c["name"] for c in caps.get("categories", [])]},
                    separators=(",", ":"),
                )
            )
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:200]}, separators=(",", ":")), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--mcp-stdio"]:
        serve_mcp()
    elif len(sys.argv) == 1:
        sys.exit(main())
    else:
        print("Usage: newznab_mcp.py [--mcp-stdio]", file=sys.stderr)
        sys.exit(2)
