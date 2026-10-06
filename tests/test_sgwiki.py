import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mrt_wayfinding.sources.sgwiki import HttpResponse, collect_sgwiki
from mrt_wayfinding.sources.sgwiki_parser import SgWikiSourceError, parse_response
from mrt_wayfinding.station_catalog import load_station_catalog
from tests.helpers import write_test_project


def station_html(*, include_exit_table: bool = True, crossed_wheelchair: bool = False) -> str:
    accessibility = (
        '<div class="pos-rel d-inline-block">'
        '<div><img src="//static.wikitide.net/sgwikiwiki/9/97/Icon-WC.svg"></div>'
        '<div class="pos-abs" style="top:0;left:0;width:100%;height:100%;'
        'background:linear-gradient(to top right,#0000 calc(50% - 1px),'
        '#f00 calc(50% - 1px),#f00 calc(50% + 1px),#0000 calc(50% + 1px))"></div>'
        '</div>'
        if crossed_wheelchair else ""
    )
    exit_table = (
        '<table><tr><th>Exit</th></tr><tr><td>'
        '<div class="d-flex align-items-center tx-center"><div class="fw-bold">A</div></div>'
        f'{accessibility}Test Landmark</td></tr></table>'
        if include_exit_table
        else "<p>Exit inventory is pending.</p>"
    )
    return (
        "<h2>Layout</h2>"
        "<table><tr><th>L1</th><td>Street Level</td></tr>"
        "<tr><th>B1</th><td>NSL Concourse</td></tr></table>"
        f"<h2>Entrances/Exits</h2>{exit_table}"
        "<h2>Bus connections</h2>"
        "<table><tr><th>Bus Stop</th><th>Exit</th><th>Bus Service</th></tr>"
        "<tr><td>99999 Test Road</td><td>A</td><td>1 2 3</td></tr></table>"
    )


def api_response(
    *,
    include_exit_table: bool = True,
    crossed_wheelchair: bool = False,
    extra_parse_field: bool = False,
) -> bytes:
    parsed = {
        "title": "Example station",
        "pageid": 1,
        "revid": 2,
        "redirects": [],
        "text": station_html(
            include_exit_table=include_exit_table,
            crossed_wheelchair=crossed_wheelchair,
        ),
    }
    if extra_parse_field:
        parsed["new_field"] = "unexpected"
    return json.dumps({"parse": parsed}).encode()


class SgWikiTests(unittest.TestCase):
    def test_parser_extracts_landmark_bus_and_layout_without_llm(self):
        result, _html = parse_response(
            api_response(), {"name": "Example", "codes": ["NS99"]}, source="sgwiki"
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["labels"], ["A"])
        self.assertEqual(result["entries"][0]["destination_verbatim"], "Test Landmark")
        self.assertEqual(result["bus_connections"]["rows"][0]["bus_stop_code"], "99999")
        self.assertEqual([row["floor"] for row in result["layout"]["floors"]], ["L1", "B1"])

    def test_exact_daily_exit_hours_become_structured_data(self):
        payload = json.loads(api_response())
        payload["parse"]["text"] = payload["parse"]["text"].replace(
            "<h2>Bus connections</h2>",
            "<p>Notes: Exit A is open from 10:00 - 22:00 daily.</p>"
            "<h2>Bus connections</h2>",
        )
        result, _html = parse_response(
            json.dumps(payload).encode(),
            {"name": "Example", "codes": ["NS99"]},
            source="sgwiki",
        )
        self.assertEqual(result["prose_verbatim"], [])
        self.assertEqual(result["exit_operating_hours"], [{
            "labels": ["A"],
            "scopes": ["MRT"],
            "days": [
                "monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday",
            ],
            "opens": "10:00",
            "closes": "22:00",
            "timezone": "Asia/Singapore",
            "source_verbatim": "Notes: Exit A is open from 10:00 - 22:00 daily.",
            "grammar": "daily_open_interval",
        }])
        self.assertNotIn(
            "exit_section_prose_not_interpreted",
            {review["reason"] for review in result["reviews"]},
        )

    def test_bus_row_without_exit_is_retained_for_station_connection_review(self):
        payload = json.loads(api_response())
        payload["parse"]["text"] = payload["parse"]["text"].replace(
            "<td>99999 Test Road</td><td>A</td><td>1 2 3</td>",
            "<td>99999 Test Road</td><td></td><td>1 2 3</td>",
        )
        result, _html = parse_response(
            json.dumps(payload).encode(),
            {"name": "Example", "codes": ["NS99"]},
            source="sgwiki",
        )
        self.assertEqual(result["bus_connections"]["rows"], [])
        self.assertEqual(result["bus_connections"]["unlinked_rows"], [{
            "row": 2,
            "bus_stop_code": "99999",
            "stop_verbatim": "99999 Test Road",
            "service_verbatim": "1 2 3",
        }])
        self.assertIn(
            "bus_stop_without_exit_link",
            {review["reason"] for review in result["reviews"]},
        )

    def test_layout_service_diagram_is_classified_without_review(self):
        payload = json.loads(api_response())
        diagram = (
            '<table class="tx-center"><tr><td>'
            '<span class="stncode left">NS98</span>'
            '<a href="/wiki/Previous_station">Previous</a>'
            '<span class="stncode both">NS99</span>'
            '<a class="mw-selflink selflink">Example</a>'
            '</td></tr></table>'
        )
        payload["parse"]["text"] = payload["parse"]["text"].replace(
            "<h2>Entrances/Exits</h2>", diagram + "<h2>Entrances/Exits</h2>"
        )
        result, _html = parse_response(
            json.dumps(payload).encode(),
            {"name": "Example", "codes": ["NS99"]},
            source="sgwiki",
        )
        self.assertEqual(result["layout"]["supplementary_tables"], [
            {"table": 2, "kind": "rail_service_diagram"}
        ])
        self.assertFalse(result["layout"]["reviews"])

    def test_unknown_layout_supplement_fails_loudly(self):
        payload = json.loads(api_response())
        payload["parse"]["text"] = payload["parse"]["text"].replace(
            "<h2>Entrances/Exits</h2>",
            "<table><tr><td>Unknown supplement</td></tr></table>"
            "<h2>Entrances/Exits</h2>",
        )
        with self.assertRaisesRegex(SgWikiSourceError, "unknown structure"):
            parse_response(
                json.dumps(payload).encode(),
                {"name": "Example", "codes": ["NS99"]},
                source="sgwiki",
            )

    def test_bus_only_fallback_is_partial_positive_evidence(self):
        result, _html = parse_response(
            api_response(include_exit_table=False),
            {"name": "Example", "codes": ["NS99"]},
            source="sgwiki",
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["evidence_kind"], "bus_connections_only")
        self.assertEqual(result["labels"], ["A"])

    def test_crossed_wheelchair_icon_is_not_accessible_without_review_noise(self):
        result, _html = parse_response(
            api_response(crossed_wheelchair=True),
            {"name": "Example", "codes": ["NS99"]},
            source="sgwiki",
        )
        entry = result["entries"][0]
        self.assertEqual(entry["wheelchair_access"], "not_accessible")
        self.assertFalse(entry["has_uninterpreted_icon"])
        self.assertNotIn(
            "exit_cell_icon_not_interpreted",
            {review["reason"] for review in result["reviews"]},
        )

    def test_changed_wheelchair_markup_remains_uninterpreted_and_reviewable(self):
        payload = json.loads(api_response(crossed_wheelchair=True))
        payload["parse"]["text"] = payload["parse"]["text"].replace(
            'class="pos-abs"', 'class="changed-overlay"'
        )
        result, _html = parse_response(
            json.dumps(payload).encode(),
            {"name": "Example", "codes": ["NS99"]},
            source="sgwiki",
        )
        entry = result["entries"][0]
        self.assertEqual(entry["wheelchair_access"], "not_classified")
        self.assertTrue(entry["has_uninterpreted_icon"])
        self.assertIn(
            "exit_cell_icon_not_interpreted",
            {review["reason"] for review in result["reviews"]},
        )

    def test_new_api_field_fails_loudly(self):
        with self.assertRaisesRegex(SgWikiSourceError, "Unexpected Wiki parse fields"):
            parse_response(
                api_response(extra_parse_field=True),
                {"name": "Example", "codes": ["NS99"]},
                source="sgwiki",
            )

    def test_collector_retains_raw_json_and_rendered_html(self):
        def fake_get(url, *, headers, timeout):
            return HttpResponse(200, {"Content-Type": "application/json"}, api_response())

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = write_test_project(root / "project")
            catalog = load_station_catalog(config.paths.station_catalog, expected_count=1)
            result = collect_sgwiki(
                endpoint="https://sgwiki.miraheze.org/w/api.php",
                catalog=catalog,
                output_directory=root / "collected",
                timeout_seconds=1.0,
                max_attempts=1,
                initial_backoff_seconds=0.0,
                maximum_backoff_seconds=0.0,
                minimum_interval_seconds=0.0,
                http_get=fake_get,
            )
            self.assertEqual(len(result.stations), 1)
            self.assertTrue((result.output_directory / "responses/000-example.json").is_file())
            self.assertTrue((result.output_directory / "rendered/000-example.html").is_file())
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["partial_station_count"], 0)

    def test_collector_reports_station_progress(self):
        updates = []

        def fake_get(url, *, headers, timeout):
            return HttpResponse(200, {"Content-Type": "application/json"}, api_response())

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = write_test_project(root / "project")
            catalog = load_station_catalog(config.paths.station_catalog, expected_count=1)
            collect_sgwiki(
                endpoint="https://sgwiki.miraheze.org/w/api.php",
                catalog=catalog,
                output_directory=root / "collected",
                timeout_seconds=1.0,
                max_attempts=1,
                initial_backoff_seconds=0.0,
                maximum_backoff_seconds=0.0,
                minimum_interval_seconds=0.0,
                http_get=fake_get,
                progress=lambda task, completed, total, message: updates.append(
                    (task, completed, total, message)
                ),
            )
        self.assertEqual(updates, [
            ("SgWiki stations", 0, 1, "Example"),
            ("SgWiki stations", 1, 1, "Example"),
        ])


if __name__ == "__main__":
    unittest.main()
