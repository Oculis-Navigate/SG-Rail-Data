import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qs, urlsplit

from mrt_wayfinding.sources.lta_bus_stops import (
    BusStop,
    BusStopsSourceError,
    HttpResponse,
    collect_lta_bus_stops,
    select_required_bus_stops,
)
from mrt_wayfinding.storage import OutputExistsError


METADATA = "https://datamall2.mytransport.sg/ltaodataservice/$metadata#BusStops"


def record(code: str, *, latitude: float = 1.3, longitude: float = 103.8):
    return {
        "BusStopCode": code,
        "RoadName": "Example Road",
        "Description": "Example stop",
        "Latitude": latitude,
        "Longitude": longitude,
    }


def response(records, *, status=200, extra_root=None, headers=None):
    document = {"odata.metadata": METADATA, "value": records}
    if extra_root:
        document.update(extra_root)
    return HttpResponse(
        status=status,
        headers=headers or {"Content-Type": "application/json; charset=utf-8"},
        body=json.dumps(document).encode("utf-8"),
    )


def collect(output: Path, http_get, **overrides):
    arguments = {
        "endpoint": "https://datamall2.mytransport.sg/ltaodataservice/BusStops",
        "account_key": "secret-key-that-must-not-be-persisted",
        "output_directory": output,
        "timeout_seconds": 30.0,
        "max_attempts": 3,
        "initial_backoff_seconds": 0.0,
        "maximum_backoff_seconds": 0.0,
        "minimum_interval_seconds": 0.0,
        "http_get": http_get,
        "sleep": lambda _seconds: None,
    }
    arguments.update(overrides)
    return collect_lta_bus_stops(**arguments)


class LtaBusStopsTests(unittest.TestCase):
    def test_collects_all_pages_until_empty_and_writes_auditable_snapshot(self):
        first = [record(f"{index:05d}") for index in range(500)]
        second = [record("00500"), record("00501")]
        calls = []

        def fake_get(url, *, headers, timeout):
            calls.append((url, dict(headers), timeout))
            skip = int(parse_qs(urlsplit(url).query)["$skip"][0])
            return {0: response(first), 500: response(second), 502: response([])}[skip]

        with TemporaryDirectory() as temporary:
            result = collect(Path(temporary) / "bus-stops", fake_get)
            self.assertEqual(result.record_count, 502)
            self.assertEqual(result.page_count, 3)
            self.assertEqual(
                [int(parse_qs(urlsplit(url).query)["$skip"][0]) for url, _, _ in calls],
                [0, 500, 502],
            )
            self.assertTrue(result.snapshot_path.is_file())
            self.assertTrue(result.manifest_path.is_file())
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["record_count"], 502)
            self.assertEqual(
                [page["record_count"] for page in manifest["pages"]], [500, 2, 0]
            )
            self.assertRegex(manifest["snapshot_sha256"], r"^[0-9a-f]{64}$")
            all_output = "".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in result.output_directory.rglob("*")
                if path.is_file()
            )
            self.assertNotIn("secret-key-that-must-not-be-persisted", all_output)
            self.assertTrue(
                all(
                    headers["AccountKey"] == "secret-key-that-must-not-be-persisted"
                    for _url, headers, _timeout in calls
                )
            )

    def test_collection_reports_page_and_record_progress(self):
        calls = 0
        updates = []

        def fake_get(url, *, headers, timeout):
            nonlocal calls
            calls += 1
            return response([record("01012")]) if calls == 1 else response([])

        with TemporaryDirectory() as temporary:
            collect(
                Path(temporary) / "bus-stops",
                fake_get,
                progress=lambda task, completed, total, message: updates.append(
                    (task, completed, total, message)
                ),
            )
        self.assertEqual(updates[-1], ("LTA BusStops", 2, 2, "1 bus stops"))
        self.assertIn(
            ("LTA BusStops", 1, None, "1 bus stops"),
            updates,
        )

    def test_pagination_uses_actual_page_length_instead_of_assuming_500(self):
        calls = []

        def fake_get(url, *, headers, timeout):
            skip = int(parse_qs(urlsplit(url).query)["$skip"][0])
            calls.append(skip)
            if skip == 0:
                return response([record("01012"), record("01013")])
            return response([])

        with TemporaryDirectory() as temporary:
            result = collect(Path(temporary) / "bus-stops", fake_get)
        self.assertEqual(calls, [0, 2])
        self.assertEqual(result.record_count, 2)

    def test_retryable_response_is_retained_then_retried(self):
        calls = 0
        sleeps = []

        def fake_get(url, *, headers, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                return HttpResponse(
                    status=429,
                    headers={"Content-Type": "application/json", "Retry-After": "3"},
                    body=b'{"error":"slow down"}',
                )
            return response([record("01012")]) if calls == 2 else response([])

        with TemporaryDirectory() as temporary:
            result = collect(
                Path(temporary) / "bus-stops",
                fake_get,
                initial_backoff_seconds=1.0,
                maximum_backoff_seconds=5.0,
                sleep=sleeps.append,
            )
            failures = list((result.output_directory / "failures").iterdir())
            self.assertEqual(len(failures), 1)
            self.assertIn("status-429", failures[0].name)
            self.assertEqual(failures[0].read_bytes(), b'{"error":"slow down"}')
        self.assertIn(3.0, sleeps)

    def test_unknown_field_fails_after_preserving_raw_response(self):
        def fake_get(url, *, headers, timeout):
            return response([record("01012")], extra_root={"newField": "unexpected"})

        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "bus-stops"
            with self.assertRaisesRegex(BusStopsSourceError, "extra=\\['newField'\\]"):
                collect(output, fake_get)
            self.assertTrue((output / "pages/skip-000000.json").is_file())
            self.assertFalse((output / "snapshot.json").exists())

    def test_duplicate_source_code_fails_loudly(self):
        calls = 0

        def fake_get(url, *, headers, timeout):
            nonlocal calls
            calls += 1
            if calls <= 2:
                return response([record("01012")])
            raise AssertionError("collector should have stopped on duplicate")

        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(BusStopsSourceError, "duplicate BusStopCode 01012"):
                collect(Path(temporary) / "bus-stops", fake_get)

    def test_non_json_content_type_is_retained_and_rejected(self):
        def fake_get(url, *, headers, timeout):
            return HttpResponse(200, {"Content-Type": "text/html"}, b"<h1>blocked</h1>")

        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "bus-stops"
            with self.assertRaisesRegex(BusStopsSourceError, "expected application/json"):
                collect(output, fake_get)
            retained = output / "failures/skip-000000-unexpected-content-type.body"
            self.assertEqual(retained.read_bytes(), b"<h1>blocked</h1>")

    def test_exact_code_selection_deduplicates_sgwiki_references(self):
        records = (
            BusStop("66389", "Petir Road", "Opp Hillion Mall", 1.377, 103.763),
            BusStop("66381", "Petir Road", "Hillion Mall", 1.378, 103.764),
        )
        selected = select_required_bus_stops(records, ["66389", "66389", "66381"])
        self.assertEqual([item.code for item in selected], ["66381", "66389"])

    def test_missing_exact_code_fails_without_geographic_fallback(self):
        geographically_close = BusStop(
            "66388", "Petir Road", "Nearby but wrong code", 1.377, 103.763
        )
        with self.assertRaisesRegex(BusStopsSourceError, "66389"):
            select_required_bus_stops((geographically_close,), ["66389"])

    def test_existing_output_directory_is_never_reused(self):
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "bus-stops"
            output.mkdir()
            with self.assertRaisesRegex(
                OutputExistsError, "Refusing to reuse existing BusStops collection"
            ):
                collect(output, lambda *_args, **_kwargs: response([]))

if __name__ == "__main__":
    unittest.main()
