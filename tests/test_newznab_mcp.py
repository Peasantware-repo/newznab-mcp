"""Unit tests for newznab_mcp — pure functions only, no network, no MCP SDK."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import newznab_mcp as nz

CAPS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
 <server title="TestIndexer"/>
 <limits max="100" default="100"/>
 <registration available="no" open="no"/>
 <searching>
  <search available="yes" supportedParams="q,group"/>
  <tv-search available="yes" supportedParams="q,rid,tvdbid,tvmazeid,season,ep"/>
  <movie-search available="yes" supportedParams="q,imdbid,genre"/>
  <book-search available="yes" supportedParams="title,author,isbn"/>
 </searching>
 <categories>
  <category id="2000" name="Movies"><subcat id="2040" name="HD"/></category>
  <category id="5000" name="TV"><subcat id="5040" name="HD"/></category>
 </categories>
</caps>"""

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
 <channel>
  <title>TestIndexer</title>
  <newznab:response offset="0" total="2"/>
  <item>
   <title>Foo.2020.1080p</title>
   <guid>https://x.test/details/11111111-2222-3333-4444-555555555555</guid>
   <link>https://x.test/getnzb</link>
   <pubDate>Wed, 25 Nov 2026 00:00:00 +0000</pubDate>
   <category>Movies &gt; HD</category>
   <enclosure url="x" length="12345" type="application/x-nzb"/>
   <newznab:attr name="size" value="12345"/>
   <newznab:attr name="imdbid" value="1234567"/>
  </item>
  <item>
   <title>Bar.2021.720p</title>
   <guid>https://x.test/details/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee</guid>
   <enclosure url="x" length="999"/>
  </item>
 </channel>
</rss>"""


def test_parse_caps():
    caps = nz._parse_caps(CAPS_XML)
    assert caps["server"]["title"] == "TestIndexer"
    assert "tv-search" in caps["searching"]
    assert caps["searching"]["movie-search"]["supportedParams"] == "q,imdbid,genre"
    cats = {c["name"]: c for c in caps["categories"]}
    assert cats["Movies"]["subcats"][0]["name"] == "HD"


def test_parse_rss():
    rss = nz._parse_rss(RSS_XML)
    assert rss["total"] == 2
    assert rss["offset"] == 0
    assert len(rss["releases"]) == 2
    first = rss["releases"][0]
    assert first["title"] == "Foo.2020.1080p"
    assert first["size"] == 12345
    assert first["category"] == ["Movies > HD"]
    assert first["attrs"]["imdbid"] == "1234567"
    assert rss["releases"][1]["size"] == 999


def test_release_id():
    guid = "https://x.test/details/11111111-2222-3333-4444-555555555555"
    assert nz._release_id(guid) == "11111111-2222-3333-4444-555555555555"
    assert nz._release_id("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_build_params_drops_unsupported_ids():
    mode_caps = {"available": "yes", "supportedParams": "q,imdbid,genre"}
    provided = {"q": "matrix", "imdbid": "0133093", "tmdbid": "603", "cat": "2000", "limit": 5}
    params = nz._build_params(mode_caps, provided, "movie")
    assert params["q"] == "matrix"
    assert params["imdbid"] == "0133093"
    assert params["cat"] == "2000"
    assert params["limit"] == "5"
    assert "tmdbid" not in params


def test_build_params_skips_empty():
    mode_caps = {"available": "yes", "supportedParams": "q"}
    params = nz._build_params(mode_caps, {"q": "x", "group": "", "extended": False}, "search")
    assert params == {"q": "x"}


def test_dedupe_merges_found_in():
    a = {"title": "Foo.2020.1080p", "size": 12345, "indexer": "one"}
    b = {"title": "Foo.2020.1080p", "size": 12345, "indexer": "two"}
    c = {"title": "Foo.2020.1080p", "size": 54321, "indexer": "one"}
    merged = nz._dedupe([a, b, c])
    assert len(merged) == 2
    assert sorted(merged[0]["found_in"]) == ["one", "two"]
    assert merged[1]["found_in"] == ["one"]


def test_load_indexers_from_env():
    env = {
        "NEWZNAB_INDEXERS": json.dumps(
            [
                {"name": "a", "base_url": "https://a.test/api", "api_key_env": "A_KEY"},
                {"name": "b", "base_url": "https://b.test/api", "api_key_env": "B_KEY"},
            ]
        ),
        "A_KEY": "key-a",
        "B_KEY": "key-b",
    }
    idxs = nz.load_indexers(env)
    assert [ix.name for ix in idxs] == ["a", "b"]
    assert idxs[0].api_key == "key-a"
    assert idxs[0].host == "a.test"
