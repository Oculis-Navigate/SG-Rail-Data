from datetime import date
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from sg_rail_station_data.network_geometry import (
    NetworkGeometryError,
    build_coverage,
    build_gtfs_network,
    collect_osm_geometry,
    compare_geometry,
    validate_geometry_baseline,
)
from sg_rail_station_data.sources.lta_gtfs import active_inventory


SERVICE_DATE = date.fromisoformat("2026-10-04")


def network_gtfs_zip() -> bytes:
    files = {
        "feed_info.txt": (
            "feed_publisher_name,feed_publisher_url,feed_lang,feed_start_date,feed_end_date,feed_version\n"
            "Land Transport Authority,https://www.lta.gov.sg/,en,20260101,20261231,test-1\n"
        ),
        "agency.txt": "agency_name,agency_url,agency_timezone\nLTA,https://example.test,Asia/Singapore\n",
        "routes.txt": "route_id,route_short_name,route_type\nNSL,NS,1\n",
        "trips.txt": "route_id,service_id,trip_id,trip_headsign,direction_id\nNSL,DAILY,T1,North,0\n",
        "stops.txt": (
            "stop_id,stop_code,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
            "A,NS1,Alpha,1.30,103.80,1,\n"
            "A_1,,Alpha Platform,1.30,103.80,0,A\n"
            "B,NS2,Beta,1.31,103.81,1,\n"
            "B_1,,Beta Platform,1.31,103.81,0,B\n"
        ),
        "stop_times.txt": "trip_id,stop_id,stop_sequence\nT1,A_1,1\nT1,B_1,2\n",
        "calendar.txt": (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "DAILY,1,1,1,1,1,1,1,20260101,20261231\n"
        ),
        "calendar_dates.txt": "service_id,date,exception_type\n",
    }
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def discovery() -> dict[str, object]:
    return {
        "version": 0.6,
        "osm3s": {"timestamp_osm_base": "2026-10-25T00:00:00Z"},
        "elements": [{
            "type": "relation", "id": 10,
            "tags": {"type": "route", "route": "subway", "ref": "NSL", "name": "NSL north"},
        }],
    }


def approved_baseline(feature: dict[str, object]) -> dict[str, object]:
    return {
        "type": "FeatureCollection",
        "metadata": {
            "schema_version": 1,
            "kind": "stage3_approved_rail_geometry_baseline",
            "status": "approved",
            "publication_allowed": False,
            "approval_scope": "comparison_baseline_not_production_release",
            "decision_id": "D-028",
            "service_date": "2026-10-04",
            "gtfs_zip_sha256": "0" * 64,
            "osm_snapshot_timestamp": "2026-10-25T00:00:00Z",
            "license": "ODbL-1.0",
            "attribution": "© OpenStreetMap contributors",
            "feature_count": 1,
            "line_ids": ["NSL", "EWL", "NEL", "CCL", "DTL", "TEL", "BPLRT", "SKLRT", "PGLRT"],
            "relation_count": 1,
            "source_relations": [],
            "reconstruction": {},
            "limitations": ["Test fixture."],
        },
        "features": [feature],
    }


def full_relation(*, railway: str = "subway") -> dict[str, object]:
    return {
        "version": 0.6,
        "elements": [
            {"type": "node", "id": 1, "lat": 1.30, "lon": 103.80},
            {"type": "node", "id": 2, "lat": 1.31, "lon": 103.81},
            {
                "type": "way", "id": 20, "version": 2, "timestamp": "2026-10-24T00:00:00Z",
                "nodes": [1, 2], "tags": {"railway": railway, "name": "Test line"},
            },
            {
                "type": "relation", "id": 10, "version": 3,
                "timestamp": "2026-10-25T00:00:00Z",
                "members": [{"type": "way", "ref": 20, "role": ""}],
                "tags": {"type": "route", "route": "subway", "ref": "NSL", "name": "NSL north"},
            },
        ],
    }


class NetworkGeometryTests(unittest.TestCase):
    def test_continuity_requires_shared_nodes_and_no_intervening_stop(self):
        from sg_rail_station_data.network_continuity import _connected
        graph = {1: {2}, 2: {1, 3}, 3: {2}, 4: {5}, 5: {4}}
        self.assertTrue(_connected(graph, 1, 3, {1, 3}))
        self.assertFalse(_connected(graph, 1, 3, {1, 2, 3}))
        self.assertFalse(_connected(graph, 1, 5, {1, 5}))

    def test_endpoint_qualified_routes(self):
        from sg_rail_station_data.network_geometry import _line_id
        for route, line in [('NEL_NE16_NE18', 'NEL'), ('NSL_NS1_NS28', 'NSL'),
                            ('EWL_EW29_EW1', 'EWL'), ('EWL_CGL_CG_CG2', 'EWL'),
                            ('DTL_DT1_DT14', 'DTL'), ('TEL_TE1_TE9', 'TEL')]:
            with self.subTest(route=route):
                self.assertEqual(_line_id(route), line)
        for route in ('NEL_depot', 'NEL_NE1_TE2', 'UNKNOWN_NE1_NE2'):
            with self.assertRaises(NetworkGeometryError):
                _line_id(route)

    def test_gtfs_network_retains_station_points_and_service_pattern(self):
        raw = network_gtfs_zip()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            zip_path = root / "GTFSScheduleTrain.zip"
            inventory_path = root / "active-stations.json"
            zip_path.write_bytes(raw)
            inventory_path.write_text(
                json.dumps(active_inventory(raw, SERVICE_DATE)), encoding="utf-8"
            )
            network, stations = build_gtfs_network(zip_path, inventory_path)
        self.assertFalse(network["publication_allowed"])
        self.assertEqual([line["id"] for line in network["lines"]], ["NSL"])
        self.assertEqual(network["patterns"][0]["station_stop_ids"], ["A", "B"])
        self.assertEqual(network["stations"][0]["codes"], ["NS1"])
        self.assertEqual(network["stations"][0]["display_codes"], ["NS1"])
        self.assertEqual(stations["features"][1]["geometry"]["coordinates"], [103.81, 1.31])

    def test_osm_collection_keeps_raw_evidence_and_normalizes_member_way(self):
        calls = []

        def fake_get(url, *, timeout, max_bytes):
            calls.append(url)
            document = discovery() if "interpreter?" in url else full_relation()
            return json.dumps(document).encode()

        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "osm"
            geometry, manifest = collect_osm_geometry(
                active_line_ids={"NSL"},
                overpass_endpoint="https://overpass-api.de/api/interpreter",
                api_endpoint="https://api.openstreetmap.org/api/0.6",
                source_directory=source,
                timeout_seconds=1,
                max_attempts=1,
                initial_backoff_seconds=0,
                maximum_backoff_seconds=0,
                minimum_interval_seconds=0,
                http_get=fake_get,
            )
            self.assertTrue((source / "discovery.json").is_file())
            self.assertTrue((source / "relations/10.json").is_file())
        self.assertEqual(len(calls), 2)
        self.assertEqual(geometry["features"][0]["properties"]["line_ids"], ["NSL"])
        self.assertEqual(manifest["relations"][0]["way_ids"], [20])
        self.assertFalse(geometry["metadata"]["publication_allowed"])

    def test_non_operational_osm_way_is_rejected(self):
        def fake_get(url, *, timeout, max_bytes):
            document = discovery() if "interpreter?" in url else full_relation(railway="construction")
            return json.dumps(document).encode()

        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(NetworkGeometryError, "non-operational way"):
                collect_osm_geometry(
                    active_line_ids={"NSL"},
                    overpass_endpoint="https://overpass-api.de/api/interpreter",
                    api_endpoint="https://api.openstreetmap.org/api/0.6",
                    source_directory=Path(temporary) / "osm",
                    timeout_seconds=1,
                    max_attempts=1,
                    initial_backoff_seconds=0,
                    maximum_backoff_seconds=0,
                    minimum_interval_seconds=0,
                    http_get=fake_get,
                )

    def test_approved_baseline_detects_geometry_change(self):
        feature = {
            "type": "Feature",
            "id": "osm-way-20",
            "geometry": {"type": "LineString", "coordinates": [[103.8, 1.3], [103.81, 1.31]]},
            "properties": {
                "source_kind": "openstreetmap_route_relation_member",
                "line_ids": ["NSL"], "relation_ids": [10], "source_version": 2,
                "source_timestamp": "2026-10-24T00:00:00Z", "railway": "subway", "way_id": 20,
            },
        }
        baseline = approved_baseline(feature)
        self.assertIs(validate_geometry_baseline(baseline), baseline)
        candidate = {"features": [json.loads(json.dumps(feature))]}
        self.assertEqual(compare_geometry(candidate, baseline)["count"], 0)
        candidate["features"][0]["geometry"]["coordinates"][1][0] += 0.0001
        changes = compare_geometry(candidate, baseline)
        self.assertEqual(changes["count"], 1)
        self.assertEqual(changes["items"][0]["fields"], ["geometry"])

    def test_coverage_reports_station_to_matching_line_distance(self):
        network = {
            "stations": [{
                "stop_id": "A", "name": "Alpha", "line_ids": ["NSL"],
                "coordinates": [103.8, 1.3],
            }]
        }
        geometry = {
            "features": [{
                "geometry": {"coordinates": [[103.8, 1.3], [103.81, 1.31]]},
                "properties": {"line_ids": ["NSL"]},
            }]
        }
        coverage = build_coverage(network, geometry)
        self.assertEqual(coverage["review_count"], 0)
        self.assertEqual(coverage["checks"][0]["distance_metres"], 0.0)
        self.assertFalse(coverage["publication_allowed"])


if __name__ == "__main__":
    unittest.main()
