from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from sg_rail_station_data.contracts import ContractError
from sg_rail_station_data.stage2 import (
    build_stage2,
    compare_stage2,
    prepare_kartographer_exits,
    review_evidence_urls,
    reviewed_ledger_entry_hash,
)


PAGE_ID = 10
REVISION_ID = 20
ZERO_HASH = "0" * 64
POLICY = [
    "An exact human-reviewed override replaces Kartographer.",
    "Otherwise an exact Kartographer coordinate is selected.",
    "A human-reviewed gap fill is selected only when Kartographer has no exact coordinate.",
    "Unapproved external candidates never resolve an exit.",
]


def stage1(*, revision_id=REVISION_ID, exit_ids=("MRT:A", "MRT:B", "MRT:C", "MRT:D")):
    exits = []
    for identity in exit_ids:
        prefix, label = identity.split(":")
        scopes = ["MRT", "LRT"] if prefix == "SHARED" else [prefix]
        exits.append({
            "id": identity,
            "label": label,
            "scopes": scopes,
            "inventory_evidence": "complete",
            "destinations": [f"Landmark {label}"],
            "source_rows": [],
            "alternative_exits": [],
            "accessibility": {
                "status": "not_classified",
                "uninterpreted_source_icon": False,
            },
            "operating_status": "not_stated",
            "operating_hours": [],
            "status_notes": [],
            "bus_stop_codes": [],
        })
    return {
        "schema_version": 1,
        "kind": "sg_rail_station_data_stage_1",
        "status": "complete",
        "publication_allowed": True,
        "gtfs": {},
        "reviewed_decisions": {},
        "summary": {},
        "normalizations": [],
        "stations": [{
            "id": "example",
            "name": "Example",
            "codes": ["NS99"],
            "catalog_status": "v1_baseline",
            "operational_status": "active",
            "gtfs_matches": [],
            "source": {
                "kind": "sgwiki_station_page",
                "title": "Example station",
                "url": "https://sgwiki.example/wiki/Example_station",
                "api_url": "https://sgwiki.example/api.php",
                "revision_url": f"https://sgwiki.example/index.php?oldid={revision_id}",
                "page_id": PAGE_ID,
                "revision_id": revision_id,
                "redirects": [],
                "contracts_applied": [],
            },
            "exit_inventory_status": "complete",
            "exits": exits,
            "bus_links": [],
            "excluded_bus_links": [],
            "station_connections": [],
            "areas": [],
            "platforms": [],
            "layout_supplements": [],
            "layout_coverage": {},
            "reviews": [],
        }],
    }


def marker(exit_id, coordinates, feature_index):
    return {
        "exit_id": exit_id,
        "label": exit_id.split(":")[1],
        "coordinates": coordinates,
        "source_feature_index": feature_index,
    }


def kartographer(*markers, revision_id=REVISION_ID, unmatched_markers=()):
    return {
        "schema_version": 1,
        "kind": "sgwiki_kartographer_exit_overlays",
        "overlays": [{
            "station_id": "example",
            "page_id": PAGE_ID,
            "revision_id": revision_id,
            "source_url": "https://commons.example/mapdata/10",
            "markers": list(markers),
        }],
        "unmatched_markers": list(unmatched_markers),
    }


def review_inputs(*decisions):
    entries = []
    features = []
    previous = None
    for index, decision in enumerate(decisions, 1):
        exit_id, mode, coordinates = decision[:3]
        revision_id = decision[3] if len(decision) > 3 else REVISION_ID
        label = exit_id.split(":")[1]
        entry = {
            "sequence": index,
            "edit_id": f"edit-{index}",
            "saved_at": "2026-10-05T12:00:00+08:00",
            "station_id": "example",
            "station_name": "Example",
            "exit_id": exit_id,
            "label": label,
            "from": {
                "source": "operator_map",
                "coordinates": [coordinates[0] - 0.0001, coordinates[1] - 0.0001],
                "candidate_index": index - 1,
            },
            "to_coordinates": coordinates,
            "note": "reviewed against the operator map",
            "previous_entry_hash": previous,
        }
        entry_hash = reviewed_ledger_entry_hash(entry)
        entry["entry_hash"] = entry_hash
        entries.append(entry)
        features.append({
            "type": "Feature",
            "id": f"example:{exit_id}",
            "geometry": {"type": "Point", "coordinates": coordinates},
            "properties": {
                "station_id": "example",
                "station_name": "Example",
                "station_codes": ["NS99"],
                "exit_id": exit_id,
                "label": label,
                "scope": exit_id.split(":")[0],
                "mode": mode,
                "approved_at": entry["saved_at"],
                "edit_id": entry["edit_id"],
                "ledger_entry_hash": entry_hash,
                "based_on_source": entry["from"]["source"],
                "based_on_coordinates": entry["from"]["coordinates"],
                "review_note": entry["note"],
                "source_guard": {
                    "sgwiki_page_id": PAGE_ID,
                    "sgwiki_revision_id": revision_id,
                },
            },
        })
        previous = entry_hash
    ledger = {
        "schema_version": 1,
        "kind": "exit_coordinate_edit_ledger",
        "revision": len(entries),
        "candidate_sha256": ZERO_HASH,
        "entries": entries,
    }
    reviewed = {
        "type": "FeatureCollection",
        "metadata": {
            "schema_version": 1,
            "kind": "stage2_reviewed_exit_coordinates",
            "status": "approved",
            "revision": len(entries),
            "feature_count": len(features),
            "ledger_file": "config/stage2-coordinate-ledger.json",
            "ledger_sha256": ZERO_HASH,
            "ledger_head_entry_hash": previous,
            "migrated_from": {
                "repository": "mrt-data-v2",
                "kind": "human_reviewed_exit_coordinate_ssot",
                "release_revision": len(entries),
                "ssot_sha256": ZERO_HASH,
                "candidate_sha256": ZERO_HASH,
            },
            "selection_policy": POLICY,
        },
        "features": features,
    }
    return reviewed, ledger


def native_snapshot(*exit_markers):
    overlay_id = "_" + "a" * 40
    mapdata_url = "https://sgwiki.example/api.php?action=query"
    row = {
        "station": "Example",
        "codes": ["NS99"],
        "title": "Example station",
        "page_id": PAGE_ID,
        "revision_id": REVISION_ID,
        "rendered_file": "rendered/example.html",
        "rendered_sha256": ZERO_HASH,
        "sgwiki_snapshot_sha256": ZERO_HASH,
        "status": "downloaded",
        "overlay_id": overlay_id,
        "mapdata_url": mapdata_url,
        "response_file": "responses/example.json",
        "response_sha256": ZERO_HASH,
        "normalized_file": "overlays/example.json",
        "normalized_sha256": ZERO_HASH,
        "exit_marker_count": len(exit_markers),
    }
    return {
        "schema_version": 1,
        "kind": "sgwiki_kartographer_snapshot",
        "stations": [row],
        "overlays": [{
            "schema_version": 1,
            "kind": "sgwiki_kartographer_overlay",
            "station": "Example",
            "codes": ["NS99"],
            "page": {
                "title": "Example station",
                "page_id": PAGE_ID,
                "revision_id": REVISION_ID,
                "overlay_id": overlay_id,
            },
            "map": {
                "rendered": True,
                "kind": "kartographer_osm_basemap_with_sgwiki_overlay",
                "zoom": 15,
                "center": [103.8, 1.3],
                "width": 1024,
                "height": 500,
                "overlay_id": overlay_id,
                "map_href": "/wiki/Special:Map/example",
                "snapshot_url": "https://tile.openstreetmap.org/example.png",
                "mapdata_url": mapdata_url,
            },
            "feature_collection": {"type": "FeatureCollection", "features": []},
            "exit_markers": list(exit_markers),
        }],
    }


def native_marker(label, coordinates, feature_index):
    return {
        "label": label,
        "title": f"Exit {label}",
        "destination_verbatim": f"Landmark {label}",
        "coordinates": coordinates,
        "feature_index": feature_index,
    }


def trusted_onemap(*decisions):
    features = []
    for index, decision in enumerate(decisions):
        exit_id, coordinates = decision[:2]
        revision_id = decision[2] if len(decision) > 2 else REVISION_ID
        label = exit_id.split(":", 1)[1]
        scopes = ["MRT", "LRT"] if exit_id.startswith("SHARED:") else [
            exit_id.split(":", 1)[0]
        ]
        features.append({
            "type": "Feature",
            "id": f"example:{exit_id}",
            "geometry": {"type": "Point", "coordinates": coordinates},
            "properties": {
                "station_id": "example",
                "station_name": "Example",
                "station_codes": ["NS99"],
                "exit_id": exit_id,
                "label": label,
                "scopes": scopes,
                "trust_class": "trusted_onemap_import",
                "source_guard": {
                    "sgwiki_page_id": PAGE_ID,
                    "sgwiki_revision_id": revision_id,
                },
                "provenance": {
                    "source": "onemap",
                    "original_exit_id": exit_id,
                    "original_selection_status": "experimental_selected",
                    "candidate_id": f"MRT:{index}",
                    "candidate_scope": "MRT",
                    "candidate_label": label,
                    "building": f"EXAMPLE MRT STATION EXIT {label}",
                    "query_scope": "MRT",
                    "response_files": [{
                        "file": "sources/example/onemap-mrt/page-1.json",
                        "sha256": ZERO_HASH,
                        "fetched_at": "2026-10-01T00:00:00+00:00",
                    }],
                },
            },
        })
    return {
        "type": "FeatureCollection",
        "metadata": {
            "schema_version": 1,
            "kind": "stage2_trusted_onemap_baseline",
            "status": "trusted",
            "feature_count": len(features),
            "imported_at": "2026-10-05T00:00:00+08:00",
            "source": {
                "repository": "mrt-data-v2",
                "stage2_path": "outputs/mvp-data/mvp-full/stage2.json",
                "stage2_sha256": ZERO_HASH,
                "candidate_bundle_path": "review_app/data/candidates.geojson",
                "candidate_bundle_sha256": ZERO_HASH,
            },
            "trust_policy": [
                "Imported OneMap positions are trusted deterministic gap fills authorized by the project owner.",
                "Human-reviewed coordinates and exact SgWiki Kartographer markers take precedence.",
                "A changed SgWiki page identity or revision invalidates an imported coordinate until it is vetted again.",
                "Original OneMap response provenance is retained for audit.",
            ],
        },
        "features": features,
    }


def supplemental(*, revision_id=REVISION_ID):
    root = Path(__file__).resolve().parents[1]
    document = json.loads((root / "config/stage2-trusted-supplemental-baseline.geojson").read_text())
    feature = document["features"][0]
    feature["id"] = "example:MRT:A"
    props = feature["properties"]
    props.update(station_id="example", station_name="Example", station_codes=["NS99"], exit_id="MRT:A")
    props["coordinate_source"].update(page_id=PAGE_ID, revision_id=revision_id)
    document["features"] = [feature]
    document["metadata"]["feature_count"] = 1
    return document


class Stage2Tests(unittest.TestCase):
    def test_supplemental_import_resolves_gap_and_preserves_provenance(self):
        reviewed, ledger = review_inputs()
        document = supplemental()
        baseline = build_stage2(stage1(exit_ids=("MRT:A",)), kartographer(), reviewed, ledger).dataset
        del baseline["summary"]["trusted_supplemental_baseline_exit_count"]
        result = build_stage2(stage1(exit_ids=("MRT:A",)), kartographer(), reviewed, ledger, None, document)
        selected = result.dataset["stations"][0]["exits"][0]
        self.assertEqual(result.dataset["status"], "complete")
        self.assertEqual(selected["coordinates"], document["features"][0]["geometry"]["coordinates"])
        self.assertEqual(selected["coordinate_source"], document["features"][0]["properties"]["coordinate_source"])
        self.assertEqual(result.dataset["summary"]["trusted_supplemental_baseline_exit_count"], 1)
        self.assertEqual(compare_stage2(result.dataset, result.dataset)["count"], 0)
        self.assertEqual(compare_stage2(result.dataset, baseline)["items"][0]["reason"], "coordinate_became_resolved")

    def test_supplemental_source_guard_and_missing_identity_block_publication(self):
        reviewed, ledger = review_inputs()
        for field, value in (("revision_id", 19), ("page_id", 11), ("station_name", "Changed"), ("station_codes", ["NS98"])):
            with self.subTest(field=field):
                document = supplemental()
                props = document["features"][0]["properties"]
                target = props["coordinate_source"] if field in {"revision_id", "page_id"} else props
                target[field] = value
                result = build_stage2(stage1(exit_ids=("MRT:A",)), kartographer(), reviewed, ledger, None, document)
                self.assertEqual(result.dataset["status"], "failed")
                self.assertIsNone(result.dataset["stations"][0]["exits"][0]["coordinates"])
                self.assertIn("supplemental_source_guard_changed", {g["reason"] for g in result.reviews["groups"]})
        missing = build_stage2(stage1(exit_ids=("MRT:B",)), kartographer(), reviewed, ledger, None, supplemental())
        self.assertEqual(missing.reviews["highest_severity"], "ERROR")
        self.assertIn("supplemental_identity_missing_from_stage1", {g["reason"] for g in missing.reviews["groups"]})

    def test_human_and_kartographer_coordinates_supersede_supplemental_imports(self):
        reviewed, ledger = review_inputs(("MRT:A", "gap_fill", [103.82, 1.32]))
        result = build_stage2(stage1(exit_ids=("MRT:A",)), kartographer(), reviewed, ledger, None, supplemental(revision_id=19))
        selected = result.dataset["stations"][0]["exits"][0]
        self.assertEqual(result.dataset["status"], "complete")
        self.assertEqual(selected["coordinate_source"]["kind"], "human_reviewed")
        self.assertEqual(selected["coordinates"], [103.82, 1.32])
        reviewed, ledger = review_inputs()
        result = build_stage2(stage1(exit_ids=("MRT:A",)), kartographer(marker("MRT:A", [103.83, 1.33], 0)), reviewed, ledger, None, supplemental())
        self.assertEqual(result.dataset["stations"][0]["exits"][0]["coordinate_source"]["kind"], "sgwiki_kartographer")
        self.assertEqual(result.reviews["groups"][0]["reason"], "kartographer_supersedes_supplemental_baseline")

    def test_supplemental_contract_rejects_bad_types_duplicates_and_overlapping_imports(self):
        reviewed, ledger = review_inputs()
        bad_cases = []
        for field, value in (("unknown", True), ("revision_id", "20"), ("decision_method", []), ("candidate_feature_index", True), ("candidate_bundle_sha256", "bad"), ("source", "unknown")):
            document = supplemental()
            document["features"][0]["properties"]["coordinate_source"][field] = value
            bad_cases.append(document)
        document = supplemental()
        document["features"][0]["geometry"]["coordinates"][0] = "103.81"
        bad_cases.append(document)
        document = supplemental()
        document["features"][0]["properties"]["coordinate_source"]["source_attributes"]["source_crc"] = 42
        bad_cases.append(document)
        document = supplemental()
        document["features"].append(deepcopy(document["features"][0]))
        document["metadata"]["feature_count"] = 2
        bad_cases.append(document)
        for document in bad_cases:
            with self.subTest(document=document), self.assertRaises(ContractError):
                build_stage2(stage1(), kartographer(), reviewed, ledger, None, document)
        with self.assertRaisesRegex(ContractError, "overlaps trusted OneMap"):
            build_stage2(stage1(), kartographer(), reviewed, ledger, trusted_onemap(("MRT:A", [103.81, 1.31])), supplemental())

    def test_checked_in_supplemental_import_matches_retained_decision_evidence(self):
        root = Path(__file__).resolve().parents[1]
        load = lambda name: json.loads((root / name).read_text())
        document = load("config/stage2-trusted-supplemental-baseline.geojson")
        audit = load("reports/stage2-supplemental-import.json")
        decisions = {(d["station_id"], d["exit_id"]): d for d in audit["decisions"]}
        providers = {}
        for feature in document["features"]:
            props = feature["properties"]
            source = props["coordinate_source"]
            decision = decisions[(props["station_id"], props["exit_id"])]
            chosen = next(c["feature"] for c in decision["candidates"] if c["candidate_feature_index"] == source["candidate_feature_index"])
            self.assertEqual(feature["geometry"], chosen["geometry"])
            self.assertEqual(source["source"], chosen["properties"]["source"])
            self.assertEqual(source["candidate_bundle_sha256"], audit["candidate_bundle_sha256"])
            providers[source["source"]] = providers.get(source["source"], 0) + 1
            expected_method = "assistant_evidence_selection" if props["station_id"] in {"expo", "farrer-park"} else "owner_accepted_candidate"
            self.assertEqual(source["decision_method"], expected_method)
        self.assertEqual(providers, {"lta_geojson": 31, "osm": 7, "onemap": 2})
        result = build_stage2(
            load("config/stage1-approved-baseline.json"),
            {"schema_version": 1, "kind": "sgwiki_kartographer_exit_overlays", "overlays": [], "unmatched_markers": []},
            load("config/stage2-reviewed-coordinates.geojson"), load("config/stage2-coordinate-ledger.json"),
            load("config/stage2-trusted-onemap-baseline.geojson"), document,
        )
        self.assertEqual(result.dataset["summary"]["trusted_supplemental_baseline_exit_count"], 40)
        self.assertEqual(result.dataset["summary"]["resolved_exit_count"], 321)

    def test_resolution_precedence_unresolved_reviews_and_provenance(self):
        maps = kartographer(
            marker("MRT:A", [103.8001, 1.3001], 0),
            marker("MRT:B", [103.8002, 1.3002], 1),
            marker("MRT:X", [103.8009, 1.3009], 2),
        )
        reviewed, ledger = review_inputs(
            ("MRT:A", "override", [103.8101, 1.3101]),
            ("MRT:B", "gap_fill", [103.8102, 1.3102]),
            ("MRT:C", "gap_fill", [103.8103, 1.3103]),
        )
        result = build_stage2(stage1(), maps, reviewed, ledger)
        exits = {item["id"]: item for item in result.dataset["stations"][0]["exits"]}
        self.assertEqual(exits["MRT:A"]["coordinates"], [103.8101, 1.3101])
        self.assertEqual(exits["MRT:A"]["coordinate_source"]["mode"], "override")
        self.assertEqual(exits["MRT:B"]["coordinates"], [103.8002, 1.3002])
        self.assertEqual(exits["MRT:B"]["coordinate_source"]["kind"], "sgwiki_kartographer")
        self.assertEqual(exits["MRT:C"]["coordinates"], [103.8103, 1.3103])
        self.assertEqual(exits["MRT:C"]["coordinate_source"]["mode"], "gap_fill")
        self.assertEqual(exits["MRT:D"]["coordinate_status"], "unresolved")
        self.assertEqual(len(result.geojson["features"]), 3)
        self.assertEqual(result.coverage["summary"]["unresolved_exit_count"], 1)
        reasons = {
            item["reason"] for group in result.reviews["groups"] for item in group["items"]
        }
        self.assertEqual(reasons, {
            "exit_coordinate_unresolved",
            "kartographer_supersedes_reviewed_gap_fill",
            "unmatched_kartographer_marker",
        })
        self.assertEqual(result.dataset["status"], "review_required")
        self.assertFalse(result.dataset["publication_allowed"])

    def test_complete_exact_kartographer_resolution_is_publishable(self):
        markers = [
            marker(identity, [103.8 + i / 10_000, 1.3 + i / 10_000], i)
            for i, identity in enumerate(("MRT:A", "MRT:B", "MRT:C", "MRT:D"))
        ]
        reviewed, ledger = review_inputs()
        result = build_stage2(stage1(), kartographer(*markers), reviewed, ledger)
        self.assertEqual(result.dataset["status"], "complete")
        self.assertTrue(result.dataset["publication_allowed"])
        self.assertEqual(result.reviews["finding_count"], 0)
        self.assertEqual(result.coverage["status"], "complete")

    def test_trusted_onemap_is_separate_and_lower_precedence(self):
        reviewed, ledger = review_inputs(
            ("MRT:C", "gap_fill", [103.803, 1.303]),
        )
        trusted = trusted_onemap(
            ("MRT:A", [103.811, 1.311]),
            ("MRT:B", [103.812, 1.312]),
        )
        result = build_stage2(
            stage1(exit_ids=("MRT:A", "MRT:B", "MRT:C")),
            kartographer(marker("MRT:A", [103.801, 1.301], 0)),
            reviewed,
            ledger,
            trusted,
        )
        exits = {item["id"]: item for item in result.dataset["stations"][0]["exits"]}
        self.assertEqual(exits["MRT:A"]["coordinate_source"]["kind"], "sgwiki_kartographer")
        self.assertEqual(exits["MRT:B"]["coordinates"], [103.812, 1.312])
        self.assertEqual(
            exits["MRT:B"]["coordinate_source"]["kind"],
            "trusted_onemap_baseline",
        )
        self.assertEqual(exits["MRT:C"]["coordinate_source"]["kind"], "human_reviewed")
        self.assertEqual(
            result.dataset["summary"]["trusted_onemap_baseline_exit_count"], 1
        )
        reasons = {
            item["reason"] for group in result.reviews["groups"] for item in group["items"]
        }
        self.assertEqual(reasons, {"kartographer_supersedes_trusted_onemap_baseline"})

    def test_stale_trusted_onemap_guard_is_a_blocking_error(self):
        reviewed, ledger = review_inputs()
        result = build_stage2(
            stage1(exit_ids=("MRT:A",)),
            kartographer(),
            reviewed,
            ledger,
            trusted_onemap(("MRT:A", [103.81, 1.31], REVISION_ID - 1)),
        )
        exit_row = result.dataset["stations"][0]["exits"][0]
        self.assertEqual(result.dataset["status"], "failed")
        self.assertEqual(exit_row["coordinate_status"], "unresolved")
        reasons = [
            item["reason"] for group in result.reviews["groups"] for item in group["items"]
        ]
        self.assertEqual(
            reasons,
            ["trusted_onemap_source_guard_changed", "exit_coordinate_unresolved"],
        )

    def test_human_review_replaces_trusted_onemap_even_when_import_guard_is_stale(self):
        reviewed, ledger = review_inputs(("MRT:A", "gap_fill", [103.82, 1.32]))
        result = build_stage2(
            stage1(exit_ids=("MRT:A",)), kartographer(), reviewed, ledger,
            trusted_onemap(("MRT:A", [103.81, 1.31], REVISION_ID - 1)),
        )
        self.assertEqual(result.dataset["status"], "complete")
        selected = result.dataset["stations"][0]["exits"][0]
        self.assertEqual(selected["coordinates"], [103.82, 1.32])
        self.assertEqual(selected["coordinate_source"]["kind"], "human_reviewed")

    def test_trusted_onemap_contract_rejects_ambiguous_or_malformed_evidence(self):
        reviewed, ledger = review_inputs()
        original = trusted_onemap(("MRT:A", [103.81, 1.31]))
        bad_cases = []
        changed = deepcopy(original)
        changed["features"][0]["properties"]["surprise"] = True
        bad_cases.append(changed)
        changed = deepcopy(original)
        changed["features"][0]["geometry"]["coordinates"][0] = "103.81"
        bad_cases.append(changed)
        changed = deepcopy(original)
        changed["features"][0]["properties"]["provenance"]["response_files"] = []
        bad_cases.append(changed)
        changed = deepcopy(original)
        changed["features"].append(deepcopy(changed["features"][0]))
        changed["metadata"]["feature_count"] = 2
        bad_cases.append(changed)
        for document in bad_cases:
            with self.subTest(document=document):
                with self.assertRaises(ContractError):
                    build_stage2(stage1(), kartographer(), reviewed, ledger, document)

    def test_comparison_reads_legacy_baseline_and_reports_imported_resolution(self):
        reviewed, ledger = review_inputs()
        baseline = build_stage2(
            stage1(exit_ids=("MRT:A",)), kartographer(), reviewed, ledger
        ).dataset
        del baseline["summary"]["trusted_onemap_baseline_exit_count"]
        del baseline["summary"]["trusted_supplemental_baseline_exit_count"]
        current = build_stage2(
            stage1(exit_ids=("MRT:A",)), kartographer(), reviewed, ledger,
            trusted_onemap(("MRT:A", [103.81, 1.31])),
        ).dataset
        changes = compare_stage2(current, baseline)
        self.assertEqual([item["reason"] for item in changes["items"]], ["coordinate_became_resolved"])
        self.assertEqual(compare_stage2(current, current)["count"], 0)

    def test_trusted_onemap_identity_drift_is_blocking(self):
        reviewed, ledger = review_inputs()
        trusted = trusted_onemap(("MRT:A", [103.81, 1.31]))
        missing = build_stage2(
            stage1(exit_ids=("MRT:B",)), kartographer(), reviewed, ledger, trusted
        )
        self.assertEqual(missing.reviews["highest_severity"], "ERROR")
        self.assertEqual(
            missing.reviews["groups"][0]["reason"],
            "trusted_onemap_identity_missing_from_stage1",
        )
        current = stage1(exit_ids=("MRT:A",))
        current["stations"][0]["codes"] = ["NS98"]
        drift = build_stage2(current, kartographer(), reviewed, ledger, trusted)
        self.assertEqual(drift.reviews["highest_severity"], "ERROR")
        self.assertIsNone(drift.dataset["stations"][0]["exits"][0]["coordinates"])

    def test_native_snapshot_maps_only_a_unique_stage1_label(self):
        snapshot = native_snapshot(
            native_marker("A", [103.8, 1.3], 0),
            native_marker("X", [103.81, 1.31], 1),
        )
        prepared = prepare_kartographer_exits(stage1(), snapshot)
        self.assertEqual(prepared["overlays"][0]["markers"][0]["exit_id"], "MRT:A")
        self.assertEqual(
            prepared["unmatched_markers"][0]["reason"],
            "kartographer_exit_label_missing_from_stage1",
        )
        reviewed, ledger = review_inputs()
        result = build_stage2(stage1(), snapshot, reviewed, ledger)
        reasons = {
            item["reason"] for group in result.reviews["groups"] for item in group["items"]
        }
        self.assertIn("kartographer_exit_label_missing_from_stage1", reasons)

    def test_native_snapshot_accepts_only_matching_prefixed_title(self):
        marker = native_marker("A", [103.8, 1.3], 0)
        marker["title"] = "[Exit-A]Exit A"
        prepared = prepare_kartographer_exits(stage1(), native_snapshot(marker))
        self.assertEqual(prepared["overlays"][0]["markers"][0]["exit_id"], "MRT:A")
        marker["title"] = "[Exit-B]Exit A"
        with self.assertRaisesRegex(ContractError, "marker title does not agree"):
            prepare_kartographer_exits(stage1(), native_snapshot(marker))

    def test_native_snapshot_retains_ambiguous_label_without_inventing_identity(self):
        current = stage1(exit_ids=("MRT:A", "LRT:A"))
        prepared = prepare_kartographer_exits(
            current, native_snapshot(native_marker("A", [103.8, 1.3], 0))
        )
        self.assertEqual(prepared["overlays"][0]["markers"], [])
        self.assertEqual(
            prepared["unmatched_markers"][0]["matching_exit_ids"], ["LRT:A", "MRT:A"]
        )

    def test_unresolved_evidence_links_are_manual_review_links_only(self):
        reviewed, ledger = review_inputs()
        result = build_stage2(
            stage1(exit_ids=("MRT:A",)), kartographer(), reviewed, ledger
        )
        item = result.reviews["groups"][0]["items"][0]
        self.assertEqual([link["source"] for link in item["evidence_urls"]], [
            "sgwiki", "openstreetmap", "onemap_search", "lta_train_station_exit_dataset",
        ])
        self.assertIn("Example+station+exit+A", item["evidence_urls"][1]["url"])
        self.assertEqual(
            review_evidence_urls(
                "Example", "A", "https://sgwiki.example/wiki/Example_station"
            ),
            item["evidence_urls"],
        )
        exact_osm = review_evidence_urls(
            "Bugis", "E", "https://sgwiki.miraheze.org/wiki/Bugis_station",
            station_id="bugis", exit_id="MRT:E",
        )
        self.assertEqual(
            exact_osm[1]["url"], "https://www.openstreetmap.org/node/6898222161"
        )

    def test_stale_review_guard_is_an_error_and_is_not_applied(self):
        reviewed, ledger = review_inputs(
            ("MRT:A", "gap_fill", [103.81, 1.31], REVISION_ID - 1),
        )
        result = build_stage2(
            stage1(exit_ids=("MRT:A",)), kartographer(), reviewed, ledger
        )
        self.assertEqual(result.dataset["status"], "failed")
        self.assertIsNone(result.dataset["stations"][0]["exits"][0]["coordinates"])
        reasons = [
            item["reason"] for group in result.reviews["groups"] for item in group["items"]
        ]
        self.assertEqual(reasons, [
            "reviewed_coordinate_source_guard_changed", "exit_coordinate_unresolved",
        ])

    def test_city_hall_marker_exclusion_is_exact(self):
        from sg_rail_station_data.stage2 import _reviewed_city_hall_non_exit
        root = Path(__file__).resolve().parents[1]
        evidence = json.loads((root / 'audit/evidence/2026-11-23-stage2-guards.json').read_bytes())
        for item in evidence['unmatched_city_hall_features']:
            marker = dict(item, label=item['exit_label'])
            self.assertTrue(_reviewed_city_hall_non_exit(marker))
            for change in ({'station_id': 'esplanade'}, {'label': 'A'}, {'source_feature_index': 0},
                           {'source_url': item['source_url'].replace('41804', '41805')},
                           {'coordinates': [103.85, 1.29]}):
                self.assertFalse(_reviewed_city_hall_non_exit(dict(marker, **change)))

    def test_kartographer_contract_fails_loudly(self):
        cases = []
        changed = kartographer(marker("MRT:A", [103.8, 1.3], 0))
        changed["overlays"][0]["markers"][0]["new"] = True
        cases.append((changed, "unexpected fields"))
        changed = kartographer(marker("MRT:A", [0.0, 0.0], 0))
        cases.append((changed, "outside Singapore bounds"))
        changed = kartographer(marker("MRT:A", [103.8, 1.3], 0))
        changed["overlays"][0]["markers"].append(
            deepcopy(changed["overlays"][0]["markers"][0])
        )
        cases.append((changed, "duplicate/conflicting marker identity"))
        reviewed, ledger = review_inputs()
        for document, pattern in cases:
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(ContractError, pattern):
                    build_stage2(stage1(), document, reviewed, ledger)

    def test_review_ledger_tampering_fails_loudly(self):
        reviewed, ledger = review_inputs(("MRT:A", "gap_fill", [103.81, 1.31]))
        ledger["entries"][0]["to_coordinates"] = [103.82, 1.32]
        with self.assertRaisesRegex(ContractError, "entry_hash does not match"):
            build_stage2(stage1(), kartographer(), reviewed, ledger)

    def test_checked_in_reviewed_evidence_passes_strict_contract(self):
        root = Path(__file__).resolve().parents[1]
        reviewed = json.loads((root / "config/stage2-reviewed-coordinates.geojson").read_text())
        ledger = json.loads((root / "config/stage2-coordinate-ledger.json").read_text())
        result = build_stage2(stage1(), kartographer(), reviewed, ledger)
        reasons = {
            item["reason"] for group in result.reviews["groups"] for item in group["items"]
        }
        self.assertIn("reviewed_coordinate_identity_missing_from_stage1", reasons)

    def test_checked_in_trusted_onemap_baseline_passes_strict_contract(self):
        root = Path(__file__).resolve().parents[1]
        current = json.loads((root / "config/stage1-approved-baseline.json").read_text())
        reviewed = json.loads((root / "config/stage2-reviewed-coordinates.geojson").read_text())
        ledger = json.loads((root / "config/stage2-coordinate-ledger.json").read_text())
        trusted = json.loads(
            (root / "config/stage2-trusted-onemap-baseline.geojson").read_text()
        )
        result = build_stage2(
            current,
            {
                "schema_version": 1,
                "kind": "sgwiki_kartographer_exit_overlays",
                "overlays": [],
                "unmatched_markers": [],
            },
            reviewed,
            ledger,
            trusted,
        )
        self.assertEqual(
            result.dataset["summary"]["trusted_onemap_baseline_exit_count"], 253
        )
        self.assertEqual(result.dataset["summary"]["reviewed_gap_fill_exit_count"], 27)
        self.assertEqual(result.dataset["summary"]["reviewed_override_exit_count"], 1)
        self.assertEqual(result.dataset["summary"]["resolved_exit_count"], 281)

    def test_reviewed_identity_absent_from_stage1_is_retained_as_error(self):
        reviewed, ledger = review_inputs(("MRT:B", "gap_fill", [103.81, 1.31]))
        result = build_stage2(
            stage1(exit_ids=("MRT:A",)), kartographer(), reviewed, ledger
        )
        errors = result.reviews["groups"][0]["items"]
        self.assertEqual(errors[0]["reason"], "reviewed_coordinate_identity_missing_from_stage1")
        self.assertEqual(errors[0]["exit_id"], "MRT:B")
        self.assertEqual(len(result.dataset["stations"][0]["exits"]), 1)

    def test_stage1_unknown_field_and_non_float_coordinate_are_rejected(self):
        current = stage1()
        current["stations"][0]["new_upstream_field"] = True
        reviewed, ledger = review_inputs()
        with self.assertRaisesRegex(ContractError, "unexpected fields"):
            build_stage2(current, kartographer(), reviewed, ledger)
        with self.assertRaisesRegex(ContractError, "exact finite floats"):
            build_stage2(
                stage1(), kartographer(marker("MRT:A", [103, 1.3], 0)), reviewed, ledger
            )

    def test_compare_stage2_flags_identity_and_material_coordinate_changes(self):
        reviewed, ledger = review_inputs()
        baseline = build_stage2(
            stage1(exit_ids=("MRT:A", "MRT:B")),
            kartographer(
                marker("MRT:A", [103.8, 1.3], 0),
                marker("MRT:B", [103.81, 1.31], 1),
            ),
            reviewed,
            ledger,
        ).dataset
        current = deepcopy(baseline)
        current["stations"][0]["exits"][0]["coordinates"] = [103.801, 1.301]
        current["stations"][0]["exits"].pop()
        changes = compare_stage2(current, baseline, movement_threshold_metres=25.0)
        self.assertEqual(changes["status"], "failed")
        self.assertEqual(
            [(item["severity"], item["reason"]) for item in changes["items"]],
            [("ERROR", "exit_identity_removed"), ("WARNING", "coordinate_moved")],
        )
        self.assertGreater(changes["items"][1]["distance_metres"], 25)

    def test_compare_stage2_requires_review_without_a_baseline(self):
        reviewed, ledger = review_inputs()
        current = build_stage2(
            stage1(exit_ids=("MRT:A",)),
            kartographer(marker("MRT:A", [103.8, 1.3], 0)),
            reviewed,
            ledger,
        ).dataset
        changes = compare_stage2(current, None)
        self.assertEqual(changes["status"], "baseline_unavailable")
        self.assertEqual(changes["highest_severity"], "WARNING")


if __name__ == "__main__":
    unittest.main()
