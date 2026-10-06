from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qs, urlsplit

from sg_rail_station_data.sources.sgwiki import HttpResponse
from sg_rail_station_data.sources.sgwiki_mapdata import (
    SgWikiMapDataError,
    collect_sgwiki_mapdata,
    discover_embedded_map,
    parse_mapdata_response,
)


ENDPOINT = "https://sgwiki.miraheze.org/w/api.php"
OVERLAY_ID = "_8d9347cdcd828d58e0eb8761d36f423a0ead446b"
MAP_HTML = f'''<h2>Entrances/Exits</h2>
<a class="mw-kartographer-map notheme" style="width: 100%; height: 500px;"
 data-mw-kartographer="mapframe" data-style="." data-width="full" data-height="500"
 data-zoom="15" data-lat="1.27296" data-lon="103.83701"
 data-overlays='["{OVERLAY_ID}"]'
 href="/wiki/Special:Map/15/1.27296/103.83701/en"><img
 src="https://tile.openstreetmap.org/img/x.png?lang=en&amp;domain=sgwiki.miraheze.org&amp;title=Example_station&amp;revid=2&amp;groups={OVERLAY_ID}&amp;parser=legacy"
 width="1024" height="500" decoding="async" alt="Map" /></a>'''


def feature_collection(*, coordinates=None, symbol="exit-A", title="Exit A"):
    return [{
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": coordinates or [103.8, 1.3],
            },
            "properties": {
                "marker-symbol": symbol,
                "description": "Test landmark",
                "title": title,
                "marker-color": "#FFD000",
            },
        }],
    }]


def mapdata_response(*, overlay=None) -> bytes:
    payload = {OVERLAY_ID: feature_collection() if overlay is None else overlay}
    return json.dumps({
        "batchcomplete": True,
        "query": {"pages": [{
            "pageid": 1,
            "ns": 0,
            "title": "Example station",
            "revid": 2,
            "mapdata": [json.dumps(payload)],
        }]},
    }).encode()


def parse(raw: bytes):
    map_metadata = discover_embedded_map(
        MAP_HTML, title="Example station", revision_id=2, endpoint=ENDPOINT
    )
    return parse_mapdata_response(
        raw,
        station_name="Example",
        codes=["NS99"],
        title="Example station",
        page_id=1,
        revision_id=2,
        overlay_id=OVERLAY_ID,
        map_metadata=map_metadata,
    )


def write_source(root: Path, *, html: str = MAP_HTML) -> Path:
    root.mkdir()
    (root / "rendered").mkdir()
    rendered = html.encode()
    (root / "rendered/000-example.html").write_bytes(rendered)
    snapshot = {
        "schema_version": 1,
        "kind": "sgwiki_station_snapshot",
        "stations": [{
            "name": "Example",
            "codes": ["NS99"],
            "catalog_status": "v1_baseline",
            "sgwiki_title": "Example station",
            "source": {"title": "Example station", "page_id": 1, "revision_id": 2},
        }],
    }
    snapshot_raw = (json.dumps(snapshot, indent=2, sort_keys=True) + "\n").encode()
    (root / "snapshot.json").write_bytes(snapshot_raw)
    collection = {
        "schema_version": 1,
        "kind": "sgwiki_station_collection",
        "endpoint": ENDPOINT,
        "station_count": 1,
        "complete_station_count": 1,
        "partial_station_count": 0,
        "partial_station_names": [],
        "snapshot_sha256": sha256(snapshot_raw).hexdigest(),
        "pages": [{
            "station": "Example",
            "response_file": "responses/000-example.json",
            "response_sha256": "0" * 64,
            "rendered_file": "rendered/000-example.html",
            "rendered_sha256": sha256(rendered).hexdigest(),
            "page_id": 1,
            "revision_id": 2,
            "status": "complete",
        }],
    }
    (root / "collection.json").write_text(json.dumps(collection), encoding="utf-8")
    return root


class SgWikiMapDataTests(unittest.TestCase):
    def test_discovers_strict_revision_pinned_map(self):
        result = discover_embedded_map(
            MAP_HTML, title="Example station", revision_id=2, endpoint=ENDPOINT
        )
        self.assertTrue(result["rendered"])
        self.assertEqual(result["center"], [103.83701, 1.27296])
        self.assertEqual(
            result["kind"], "kartographer_osm_basemap_with_sgwiki_overlay"
        )
        query = parse_qs(urlsplit(result["mapdata_url"]).query)
        self.assertEqual(query["revids"], ["2"])
        self.assertEqual(query["mpdgroups"], [OVERLAY_ID])

    def test_absent_map_is_explicit_successful_evidence(self):
        result = discover_embedded_map(
            "<h2>Entrances/Exits</h2><table></table>",
            title="Example station",
            revision_id=2,
            endpoint=ENDPOINT,
        )
        self.assertEqual(result, {"rendered": False})

    def test_changed_map_markup_fails_loudly(self):
        with self.assertRaisesRegex(SgWikiMapDataError, "attributes changed"):
            discover_embedded_map(
                MAP_HTML.replace(' data-zoom="15"', ' data-zoom="15" data-new="x"'),
                title="Example station",
                revision_id=2,
                endpoint=ENDPOINT,
            )

    def test_normalizes_exit_marker_with_page_provenance(self):
        result = parse(mapdata_response())
        self.assertEqual(result["station"], "Example")
        self.assertEqual(result["page"]["revision_id"], 2)
        self.assertEqual(result["exit_markers"], [{
            "label": "A",
            "title": "Exit A",
            "destination_verbatim": "Test landmark",
            "coordinates": [103.8, 1.3],
            "feature_index": 0,
        }])

    def test_unknown_response_field_fails_loudly(self):
        payload = json.loads(mapdata_response())
        payload["warnings"] = {}
        with self.assertRaisesRegex(SgWikiMapDataError, "unexpected fields"):
            parse(json.dumps(payload).encode())

    def test_duplicate_json_key_fails_loudly(self):
        raw = mapdata_response().decode().replace(
            '"batchcomplete": true,', '"batchcomplete": true, "batchcomplete": true,'
        )
        with self.assertRaisesRegex(SgWikiMapDataError, "duplicate key"):
            parse(raw.encode())

    def test_coordinate_outside_singapore_fails_loudly(self):
        overlay = feature_collection(coordinates=[0.0, 0.0])
        with self.assertRaisesRegex(SgWikiMapDataError, "outside Singapore"):
            parse(mapdata_response(overlay=overlay))

    def test_conflicting_exit_symbol_and_title_is_ambiguous(self):
        overlay = feature_collection(symbol="exit-A", title="Exit B")
        with self.assertRaisesRegex(SgWikiMapDataError, "ambiguous exit label"):
            parse(mapdata_response(overlay=overlay))

    def test_duplicate_exit_label_fails_loudly(self):
        overlay = feature_collection()
        overlay[0]["features"].append(overlay[0]["features"][0].copy())
        with self.assertRaisesRegex(SgWikiMapDataError, "duplicate exit label"):
            parse(mapdata_response(overlay=overlay))

    def test_collection_retains_raw_normalized_hashes_and_progress(self):
        calls = []

        def fake_get(url, *, headers, timeout):
            calls.append((url, headers, timeout))
            return HttpResponse(200, {"Content-Type": "application/json"}, mapdata_response())

        progress = []
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = write_source(root / "sgwiki")
            result = collect_sgwiki_mapdata(
                endpoint=ENDPOINT,
                sgwiki_directory=source,
                output_directory=root / "mapdata",
                timeout_seconds=30.0,
                max_attempts=2,
                initial_backoff_seconds=0.0,
                maximum_backoff_seconds=0.0,
                minimum_interval_seconds=0.0,
                http_get=fake_get,
                sleep=lambda _seconds: None,
                progress=lambda *args: progress.append(args),
            )
            manifest = json.loads(result.manifest_path.read_text())
            row = manifest["pages"][0]
            self.assertEqual(row["status"], "downloaded")
            self.assertEqual(row["exit_marker_count"], 1)
            self.assertEqual(
                sha256((root / "mapdata" / row["response_file"]).read_bytes()).hexdigest(),
                row["response_sha256"],
            )
            self.assertEqual(progress, [
                ("SgWiki maps", 0, 1, "Example"),
                ("SgWiki maps", 1, 1, "Example"),
            ])
            self.assertEqual(len(calls), 1)

    def test_collection_does_not_call_api_for_no_map_page(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = write_source(
                root / "sgwiki", html="<h2>Entrances/Exits</h2><table></table>"
            )

            def unexpected_get(url, *, headers, timeout):
                self.fail("no-map station must not make a MapData request")

            result = collect_sgwiki_mapdata(
                endpoint=ENDPOINT,
                sgwiki_directory=source,
                output_directory=root / "mapdata",
                timeout_seconds=30.0,
                max_attempts=1,
                initial_backoff_seconds=0.0,
                maximum_backoff_seconds=0.0,
                minimum_interval_seconds=0.0,
                http_get=unexpected_get,
                sleep=lambda _seconds: None,
            )
            self.assertEqual(result.no_map_station_names, ("Example",))
            self.assertEqual(result.stations[0]["status"], "no_embedded_map")


if __name__ == "__main__":
    unittest.main()
