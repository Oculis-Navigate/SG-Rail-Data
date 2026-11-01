from datetime import date
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from sg_rail_station_data.config import load_config
from sg_rail_station_data.sources.lta_gtfs import (
    GtfsSourceError,
    HttpResponse,
    _codes,
    active_inventory,
    collect_lta_gtfs,
    match_catalog_stations,
)
from sg_rail_station_data.station_catalog import load_station_catalog
from tests.helpers import SERVICE_DATE, write_test_project


def gtfs_zip(*, extra_member: bool = False) -> bytes:
    files = {
        "feed_info.txt": (
            "feed_publisher_name,feed_publisher_url,feed_lang,feed_start_date,feed_end_date,feed_version\n"
            "Land Transport Authority,https://www.lta.gov.sg/,en,20260101,20261231,test-1\n"
        ),
        "agency.txt": "agency_name,agency_url,agency_timezone\nLTA,https://example.test,Asia/Singapore\n",
        "routes.txt": "route_id,route_short_name,route_type\nNSL,NSL,1\n",
        "trips.txt": "route_id,service_id,trip_id\nNSL,DAILY,T1\n",
        "stops.txt": (
            "stop_id,stop_code,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
            "EXAMPLE,NS99,NS99 Example MRT Station,1.3,103.8,1,\n"
            "EXAMPLE_A,,Example Platform A,1.3,103.8,0,EXAMPLE\n"
        ),
        "stop_times.txt": "trip_id,stop_id,stop_sequence\nT1,EXAMPLE_A,1\n",
        "calendar.txt": (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "DAILY,1,1,1,1,1,1,1,20260101,20261231\n"
        ),
        "calendar_dates.txt": "service_id,date,exception_type\n",
    }
    if extra_member:
        files["shapes.txt"] = "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\nS,1.3,103.8,1\n"
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


class LtaGtfsTests(unittest.TestCase):
    def test_station_codes_have_canonical_order(self):
        self.assertEqual(_codes("EW16", "TE17", "NE3", "EW16"), ["EW16", "NE3", "TE17"])

    def test_active_inventory_uses_service_date_and_parent_station(self):
        result = active_inventory(gtfs_zip(), date.fromisoformat(SERVICE_DATE))
        self.assertEqual(result["active_station_count"], 1)
        self.assertEqual(result["stations"][0]["stop_id"], "EXAMPLE")
        self.assertEqual(result["stations"][0]["codes"], ["NS99"])
        self.assertEqual(result["stations"][0]["child_stop_ids"], ["EXAMPLE_A"])
        self.assertEqual(result["feed_info"]["version"], "test-1")

    def test_new_gtfs_file_fails_loudly(self):
        with self.assertRaisesRegex(GtfsSourceError, r"extra=\['shapes.txt'\]"):
            active_inventory(gtfs_zip(extra_member=True), date.fromisoformat(SERVICE_DATE))

    def test_collector_accepts_current_schema_and_never_persists_signed_url(self):
        signed = "https://bucket.amazonaws.com/train.zip?X-Amz-Signature=secret-signature"
        archive = gtfs_zip()
        calls = []

        def fake_get(url, *, headers, timeout, max_bytes):
            calls.append((url, dict(headers)))
            if "GTFSScheduleTrain" in url:
                return HttpResponse(
                    200,
                    {"Content-Type": "application/json"},
                    json.dumps({
                        "value": [{
                            "link": signed,
                            "timestamp": "2026-10-04T12:34:56+08:00",
                        }]
                    }).encode(),
                )
            return HttpResponse(200, {"Content-Type": "application/zip"}, archive)

        with TemporaryDirectory() as temporary:
            result = collect_lta_gtfs(
                endpoint="https://datamall2.mytransport.sg/ltaodataservice/GTFSScheduleTrain",
                account_key="account-key-secret",
                service_date=date.fromisoformat(SERVICE_DATE),
                output_directory=Path(temporary) / "gtfs",
                timeout_seconds=1.0,
                max_attempts=1,
                initial_backoff_seconds=0.0,
                maximum_backoff_seconds=0.0,
                http_get=fake_get,
            )
            combined = b"".join(
                path.read_bytes() for path in result.output_directory.rglob("*") if path.is_file()
            )
            self.assertNotIn(b"secret-signature", combined)
            self.assertNotIn(b"account-key-secret", combined)
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["download_url"], "https://bucket.amazonaws.com/train.zip")
            self.assertEqual(manifest["source_schema"], "lowercase_link_with_timestamp")
            self.assertEqual(manifest["source_timestamp"], "2026-10-04T12:34:56+08:00")
            self.assertRegex(manifest["signed_url_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(calls[0][1]["AccountKey"], "account-key-secret")

    def test_legacy_uppercase_link_schema_remains_explicitly_supported(self):
        signed = "https://bucket.amazonaws.com/train.zip?signature=legacy-secret"
        archive = gtfs_zip()

        def fake_get(url, *, headers, timeout, max_bytes):
            if "GTFSScheduleTrain" in url:
                return HttpResponse(
                    200,
                    {"Content-Type": "application/json"},
                    json.dumps({"value": [{"Link": signed}]}).encode(),
                )
            return HttpResponse(200, {"Content-Type": "application/zip"}, archive)

        with TemporaryDirectory() as temporary:
            result = collect_lta_gtfs(
                endpoint="https://datamall2.mytransport.sg/ltaodataservice/GTFSScheduleTrain",
                account_key="account-key-secret",
                service_date=date.fromisoformat(SERVICE_DATE),
                output_directory=Path(temporary) / "gtfs",
                timeout_seconds=1.0,
                max_attempts=1,
                initial_backoff_seconds=0.0,
                maximum_backoff_seconds=0.0,
                http_get=fake_get,
            )
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["source_schema"], "legacy_uppercase_link")
            self.assertIsNone(manifest["source_timestamp"])

    def test_current_schema_rejects_unrecognised_timestamp_format(self):
        def fake_get(url, *, headers, timeout, max_bytes):
            return HttpResponse(
                200,
                {"Content-Type": "application/json"},
                json.dumps({
                    "value": [{
                        "link": "https://bucket.amazonaws.com/train.zip?secret=value",
                        "timestamp": "sometime today",
                    }]
                }).encode(),
            )

        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(GtfsSourceError, "ISO-8601"):
                collect_lta_gtfs(
                    endpoint="https://datamall2.mytransport.sg/ltaodataservice/GTFSScheduleTrain",
                    account_key="account-key-secret",
                    service_date=date.fromisoformat(SERVICE_DATE),
                    output_directory=Path(temporary) / "gtfs",
                    timeout_seconds=1.0,
                    max_attempts=1,
                    initial_backoff_seconds=0.0,
                    maximum_backoff_seconds=0.0,
                    http_get=fake_get,
                )

    def test_catalog_match_rejects_unknown_station_fields(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = write_test_project(root)
            catalog = load_station_catalog(config.paths.station_catalog, expected_count=1)
            inventory = active_inventory(gtfs_zip(), date.fromisoformat(SERVICE_DATE))
            inventory["stations"][0]["new_field"] = True
            with self.assertRaisesRegex(GtfsSourceError, r"extra=\['new_field'\]"):
                match_catalog_stations(catalog, inventory)

    def test_catalog_match_rejects_noncanonical_station_code_order(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = write_test_project(root)
            catalog = load_station_catalog(config.paths.station_catalog, expected_count=1)
            inventory = active_inventory(gtfs_zip(), date.fromisoformat(SERVICE_DATE))
            inventory["stations"][0]["codes"] = ["NS99", "EW1"]
            with self.assertRaisesRegex(GtfsSourceError, "expected canonical order"):
                match_catalog_stations(catalog, inventory)

    def test_catalog_match_revalidates_feed_info_snapshot(self):
        with TemporaryDirectory() as temporary:
            catalog_path = Path(temporary) / "stations.json"
            catalog_path.write_text(json.dumps({
                "schema_version": 1,
                "source": {
                    "kind": "reviewed_station_page_registry",
                    "derived_from": "test",
                    "reviewed_on": SERVICE_DATE,
                },
                "stations": [{
                    "name": "Alpha",
                    "codes": ["NS99"],
                    "catalog_status": "v1_baseline",
                    "sgwiki_title": "Alpha station",
                }],
            }))
            catalog = load_station_catalog(catalog_path, expected_count=1)
            inventory = active_inventory(gtfs_zip(), date.fromisoformat(SERVICE_DATE))
            inventory["feed_info"]["unreviewed"] = True
            with self.assertRaisesRegex(GtfsSourceError, "feed_info: unexpected fields"):
                match_catalog_stations(catalog, inventory)


if __name__ == "__main__":
    unittest.main()
