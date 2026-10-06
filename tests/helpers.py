from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from mrt_wayfinding.config import DEFAULT_CONFIG_PATH, load_config


SERVICE_DATE = "2026-10-04"


def source_record(*, revision_id: int = 2, unknown_entry_field: bool = False):
    entry = {
        "labels": ["A"],
        "scopes": ["MRT"],
        "label_cell_verbatim": "A Test Landmark",
        "label_note_verbatim": "",
        "row_verbatim": ["A Test Landmark"],
        "table": 1,
        "row": 2,
        "destination_verbatim": "Test Landmark",
        "label_markup": "sgwiki_exit_badges",
        "wheelchair_access": "not_classified",
        "has_uninterpreted_icon": False,
        "destination_missing": False,
    }
    if unknown_entry_field:
        entry["new_upstream_field"] = True
    return {
        "name": "Example",
        "codes": ["NS99"],
        "catalog_status": "v1_baseline",
        "sgwiki_title": "Example station",
        "source": {
            "status": "complete",
            "labels": ["A"],
            "entries": [entry],
            "sections": ["Entrances/Exits"],
            "prose_verbatim": [],
            "reviews": [],
            "exit_statuses": [],
            "exit_operating_hours": [],
            "requires_human_review": True,
            "bus_connections": {
                "status": "partial_positive_evidence",
                "labels": ["A"],
                "rows": [{
                    "row": 2,
                    "bus_stop_code": "99999",
                    "stop_verbatim": "99999 Test Road",
                    "labels": ["A"],
                    "service_verbatim": "1 2 3",
                }],
                "unlinked_rows": [],
                "reviews": [],
            },
            "layout": {
                "floors": [
                    {"row": 1, "floor": "L1", "area_verbatim": "Street Level", "platforms": []},
                    {"row": 2, "floor": "B1", "area_verbatim": "NSL Concourse", "platforms": []},
                    {
                        "row": 3,
                        "floor": "B2",
                        "area_verbatim": "Platform level",
                        "platforms": [{
                            "line_or_area": "NSL Platform",
                            "line_or_area_source": "caption",
                            "label": "A",
                            "heading_verbatim": "Towards Test Terminus",
                        }],
                    },
                ],
                "supplementary_tables": [],
                "reviews": [],
            },
            "title": "Example station",
            "page_id": 1,
            "revision_id": revision_id,
            "redirects": [],
            "url": "https://sgwiki.miraheze.org/wiki/Example_station",
            "api_url": "https://sgwiki.miraheze.org/w/api.php?page=Example+station",
            "revision_url": f"https://sgwiki.miraheze.org/index.php?oldid={revision_id}",
        },
    }


def sgwiki_snapshot(*, revision_id: int = 2, unknown_entry_field: bool = False):
    return {
        "schema_version": 1,
        "kind": "sgwiki_station_snapshot",
        "stations": [source_record(
            revision_id=revision_id,
            unknown_entry_field=unknown_entry_field,
        )],
    }


def gtfs_inventory():
    return {
        "schema_version": 1,
        "status": "complete",
        "kind": "lta_gtfs_active_train_stations",
        "service_date": SERVICE_DATE,
        "source_zip_sha256": "0" * 64,
        "feed_info": {
            "publisher_name": "Land Transport Authority",
            "publisher_url": "https://www.lta.gov.sg/",
            "language": "en",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "version": "test-1",
        },
        "active_service_count": 1,
        "active_trip_count": 1,
        "active_station_count": 1,
        "stations": [{
            "stop_id": "EXAMPLE",
            "stop_code": "NS99",
            "name": "NS99 Example MRT Station",
            "codes": ["NS99"],
            "child_stop_ids": ["EXAMPLE_A"],
            "route_ids": ["NSL"],
            "route_names": ["North South Line"],
        }],
    }


def bus_snapshot():
    return {
        "schema_version": 1,
        "kind": "lta_bus_stops_snapshot",
        "bus_stops": [{
            "BusStopCode": "99999",
            "RoadName": "Test Road",
            "Description": "Example Station Exit A",
            "Latitude": 1.3,
            "Longitude": 103.8,
        }],
    }


def mapdata_snapshot():
    mapdata_url = (
        "https://sgwiki.miraheze.org/w/api.php?action=query&prop=mapdata&"
        "revids=2&mpdgroups=_0000000000000000000000000000000000000000&"
        "mpdparser=legacy&format=json&formatversion=2"
    )
    return {
        "schema_version": 1,
        "kind": "sgwiki_kartographer_snapshot",
        "stations": [{
            "station": "Example",
            "codes": ["NS99"],
            "title": "Example station",
            "page_id": 1,
            "revision_id": 2,
            "rendered_file": "rendered/000-example.html",
            "rendered_sha256": "0" * 64,
            "sgwiki_snapshot_sha256": "1" * 64,
            "status": "downloaded",
            "overlay_id": "_0000000000000000000000000000000000000000",
            "mapdata_url": mapdata_url,
            "response_file": "responses/000-example.json",
            "response_sha256": "2" * 64,
            "normalized_file": "overlays/000-example.json",
            "normalized_sha256": "3" * 64,
            "exit_marker_count": 1,
        }],
        "overlays": [{
            "schema_version": 1,
            "kind": "sgwiki_kartographer_overlay",
            "station": "Example",
            "codes": ["NS99"],
            "page": {
                "title": "Example station",
                "page_id": 1,
                "revision_id": 2,
                "overlay_id": "_0000000000000000000000000000000000000000",
            },
            "map": {
                "rendered": True,
                "kind": "kartographer_osm_basemap_with_sgwiki_overlay",
                "zoom": 18,
                "center": [103.8, 1.3],
                "width": 1024,
                "height": 500,
                "overlay_id": "_0000000000000000000000000000000000000000",
                "map_href": "/wiki/Special:Map/18/1.3/103.8/en",
                "snapshot_url": "https://tile.openstreetmap.org/img/example.png",
                "mapdata_url": mapdata_url,
            },
            "feature_collection": {"type": "FeatureCollection", "features": []},
            "exit_markers": [{
                "label": "A",
                "title": "Exit A",
                "destination_verbatim": "Test Landmark",
                "coordinates": [103.8, 1.3],
                "feature_index": 0,
            }],
        }],
    }


def write_test_project(root: Path):
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    source_toml = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    source_toml = source_toml.replace("expected_station_count = 184", "expected_station_count = 1")
    (config_dir / "pipeline.toml").write_text(source_toml, encoding="utf-8")
    (config_dir / "stations.json").write_text(json.dumps({
        "schema_version": 1,
        "source": {
            "kind": "reviewed_station_page_registry",
            "derived_from": "test fixture",
            "reviewed_on": SERVICE_DATE,
        },
        "stations": [{
            "name": "Example",
            "codes": ["NS99"],
            "catalog_status": "v1_baseline",
            "sgwiki_title": "Example station",
        }],
    }), encoding="utf-8")
    (config_dir / "stage1-reviewed-decisions.json").write_text(json.dumps({
        "schema_version": 1,
        "exit_scope_contracts": [],
        "exit_inventory_overrides": [],
        "bus_exit_overrides": [],
        "station_connection_contracts": [],
        "review_acceptances": [],
    }), encoding="utf-8")
    ledger = {
        "schema_version": 1,
        "kind": "exit_coordinate_edit_ledger",
        "revision": 0,
        "candidate_sha256": "0" * 64,
        "entries": [],
    }
    ledger_path = config_dir / "stage2-coordinate-ledger.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    (config_dir / "stage2-reviewed-coordinates.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "metadata": {
            "schema_version": 1,
            "kind": "stage2_reviewed_exit_coordinates",
            "status": "approved",
            "revision": 0,
            "feature_count": 0,
            "ledger_file": "config/stage2-coordinate-ledger.json",
            "ledger_sha256": sha256(ledger_path.read_bytes()).hexdigest(),
            "ledger_head_entry_hash": None,
            "migrated_from": {
                "repository": "mrt-data-v2",
                "kind": "human_reviewed_exit_coordinate_ssot",
                "release_revision": 0,
                "ssot_sha256": "0" * 64,
                "candidate_sha256": "0" * 64,
            },
            "selection_policy": [
                "An exact human-reviewed override replaces Kartographer.",
                "Otherwise an exact Kartographer coordinate is selected.",
                "A human-reviewed gap fill is selected only when Kartographer has no exact coordinate.",
                "Unapproved external candidates never resolve an exit.",
            ],
        },
        "features": [],
    }), encoding="utf-8")
    (config_dir / "stage2-trusted-onemap-baseline.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "metadata": {
            "schema_version": 1,
            "kind": "stage2_trusted_onemap_baseline",
            "status": "trusted",
            "feature_count": 0,
            "imported_at": "2026-10-05T00:00:00+08:00",
            "source": {
                "repository": "mrt-data-v2",
                "stage2_path": "outputs/mvp-data/mvp-full/stage2.json",
                "stage2_sha256": "0" * 64,
                "candidate_bundle_path": "review_app/data/candidates.geojson",
                "candidate_bundle_sha256": "0" * 64,
            },
            "trust_policy": [
                "Imported OneMap positions are trusted deterministic gap fills authorized by the project owner.",
                "Human-reviewed coordinates and exact SgWiki Kartographer markers take precedence.",
                "A changed SgWiki page identity or revision invalidates an imported coordinate until it is vetted again.",
                "Original OneMap response provenance is retained for audit.",
            ],
        },
        "features": [],
    }), encoding="utf-8")
    (config_dir / "stage2-trusted-supplemental-baseline.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "metadata": {"schema_version": 1, "kind": "stage2_trusted_supplemental_baseline", "status": "trusted", "feature_count": 0},
        "features": [],
    }), encoding="utf-8")
    return load_config(config_dir / "pipeline.toml")


def write_replay_bundle(root: Path, *, unknown_entry_field: bool = False):
    sources = root / "sources"
    for directory in ("lta-gtfs", "lta-bus-stops", "sgwiki", "sgwiki-mapdata"):
        (sources / directory).mkdir(parents=True, exist_ok=True)
    (sources / "lta-gtfs/active-stations.json").write_text(
        json.dumps(gtfs_inventory()), encoding="utf-8"
    )
    (sources / "lta-bus-stops/snapshot.json").write_text(
        json.dumps(bus_snapshot()), encoding="utf-8"
    )
    (sources / "sgwiki/snapshot.json").write_text(
        json.dumps(sgwiki_snapshot(unknown_entry_field=unknown_entry_field)), encoding="utf-8"
    )
    (sources / "sgwiki-mapdata/snapshot.json").write_text(
        json.dumps(mapdata_snapshot()), encoding="utf-8"
    )
    return root
