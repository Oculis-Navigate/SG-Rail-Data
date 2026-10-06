"""Normalize SgWiki Layout tables without inventing indoor connectivity."""

from __future__ import annotations

import re

from .contracts import fail, require_fields


LINE_KEYS = (
    "NSEWL", "BPLRT", "SKLRT", "PGLRT", "NSL", "EWL", "NEL", "CCL", "DTL", "TEL"
)
KNOWN_LAYOUT_REVIEWS = {
    "combined_platform_label",
    "layout_floor_rowspan_overruns_table",
}


def _line_keys(text: str) -> list[str]:
    return [
        key for key in LINE_KEYS
        if re.search(rf"(?<![A-Z]){re.escape(key)}(?![A-Z])", text)
    ]


def _platform_line_keys(platform: dict[str, object], codes: list[str]) -> list[str]:
    keys = _line_keys(str(platform["line_or_area"]))
    if keys:
        return keys
    line_or_area = str(platform["line_or_area"])
    if line_or_area.startswith("LRT "):
        prefixes = {re.match(r"[A-Z]+", code).group() for code in codes}
        if prefixes <= {"PE", "PW"}:
            return ["PGLRT"]
        if prefixes <= {"SE", "SW"}:
            return ["SKLRT"]
        if prefixes <= {"BP"}:
            return ["BPLRT"]
    return []


def _area_kinds(area: str) -> list[str]:
    if not area:
        return []
    kinds: list[str] = []
    if re.match(
        r"^(?:(?:[A-Z]+(?:/[A-Z]+)?) (?:Upper |Lower )?)?Concourse(?:\b|,)",
        area,
        re.IGNORECASE,
    ):
        kinds.append("concourse")
    if re.search(r"\bStreet Level\b", area, re.IGNORECASE):
        kinds.append("street_level")
    lowered = area.lower()
    if lowered.startswith("service level"):
        kinds.append("restricted_service_level" if "restricted" in lowered else "service_level")
    if lowered.startswith(("transfer link", "transfer hall", "transfer mezzanine")):
        kinds.append("transfer_connection")
    if lowered.startswith("unpaid link"):
        kinds.append("unpaid_link")
    if lowered.startswith("underpass"):
        kinds.append("underpass")
    if lowered.startswith("linkbridge"):
        kinds.append("linkbridge")
    return kinds or ["other"]


def _concourse_candidates(
    platform: dict[str, object], concourses: list[dict[str, object]], codes: list[str]
) -> tuple[str, list[dict[str, object]]]:
    platform_keys = set(_platform_line_keys(platform, codes))
    candidates = [
        area for area in concourses
        if platform_keys & set(_line_keys(str(area["area_verbatim"])))
    ]
    basis = "line_token"
    line_or_area = str(platform["line_or_area"])
    qualifier = (
        "Upper" if " Upper " in f" {line_or_area} "
        else "Lower" if " Lower " in f" {line_or_area} "
        else None
    )
    qualified = [
        area for area in candidates
        if qualifier and qualifier.lower() in str(area["area_verbatim"]).lower()
    ]
    if qualified:
        candidates = qualified
        basis = "line_and_vertical_qualifier"
    if not candidates and len(concourses) == 1:
        candidates = list(concourses)
        basis = "sole_concourse_record"
    return (basis if candidates else "unresolved"), candidates


def normalize_layout(
    layout: object, *, station: str, codes: list[str]
) -> dict[str, object]:
    """Validate one parsed layout and derive levels, platforms and review coverage."""

    root = require_fields(
        layout,
        {"floors", "supplementary_tables", "reviews"},
        f"{station}.layout",
    )
    if type(root["floors"]) is not list or not root["floors"]:
        fail(f"{station}.layout.floors", "expected nonempty array")
    if type(root["reviews"]) is not list:
        fail(f"{station}.layout.reviews", "expected array")
    if type(root["supplementary_tables"]) is not list:
        fail(f"{station}.layout.supplementary_tables", "expected array")
    blocking: list[dict[str, object]] = []
    for index, review in enumerate(root["reviews"]):
        if type(review) is not dict or type(review.get("reason")) is not str:
            fail(f"{station}.layout.reviews[{index}]", "invalid review")
        reason = review["reason"]
        if reason not in KNOWN_LAYOUT_REVIEWS:
            fail(f"{station}.layout.reviews[{index}]", f"unknown reason {reason!r}")
        blocking.append(review)

    supplementary_tables: list[dict[str, object]] = []
    seen_table_numbers: set[int] = set()
    for index, value in enumerate(root["supplementary_tables"]):
        context = f"{station}.layout.supplementary_tables[{index}]"
        item = require_fields(value, {"table", "kind"}, context)
        if (
            type(item["table"]) is not int
            or item["table"] < 2
            or item["table"] in seen_table_numbers
        ):
            fail(f"{context}.table", "expected unique integer at least 2")
        seen_table_numbers.add(item["table"])
        if item["kind"] not in {"rail_service_diagram", "empty_formatting_table"}:
            fail(f"{context}.kind", f"unsupported value {item['kind']!r}")
        supplementary_tables.append(dict(item))

    areas: list[dict[str, object]] = []
    platforms: list[dict[str, object]] = []
    seen_rows: set[int] = set()
    seen_platforms: set[tuple[str, str]] = set()
    for index, value in enumerate(root["floors"]):
        context = f"{station}.layout.floors[{index}]"
        row = require_fields(value, {"row", "floor", "area_verbatim", "platforms"}, context)
        if type(row["row"]) is not int or row["row"] < 1 or row["row"] in seen_rows:
            fail(f"{context}.row", "expected unique positive integer")
        seen_rows.add(row["row"])
        if type(row["floor"]) is not str or re.fullmatch(
            r"(?:L[0-9]+|B[0-9]+|M|P|U[0-9]+)", row["floor"]
        ) is None:
            fail(f"{context}.floor", "unsupported floor label")
        if type(row["area_verbatim"]) is not str:
            fail(f"{context}.area_verbatim", "expected string")
        if type(row["platforms"]) is not list:
            fail(f"{context}.platforms", "expected array")
        areas.append({
            "source_row": row["row"],
            "level": row["floor"],
            "area_verbatim": row["area_verbatim"],
            "kinds": _area_kinds(row["area_verbatim"]),
        })
        for platform_index, value in enumerate(row["platforms"]):
            platform_context = f"{context}.platforms[{platform_index}]"
            item = require_fields(
                value,
                {"line_or_area", "line_or_area_source", "label", "heading_verbatim"},
                platform_context,
            )
            if item["line_or_area_source"] not in {"caption", "station_code"}:
                fail(f"{platform_context}.line_or_area_source", "unsupported value")
            if not all(type(item[key]) is str and item[key] for key in item):
                fail(platform_context, "platform fields must be nonempty strings")
            source_group_label = item["label"] if "/" in item["label"] else None
            labels = item["label"].split("/")
            if len(labels) != len(set(labels)) or not all(re.fullmatch(r"[A-Z0-9]+", x) for x in labels):
                fail(f"{platform_context}.label", "invalid grouped platform label")
            for label in labels:
                identity = (item["line_or_area"], label)
                if identity in seen_platforms:
                    fail(platform_context, f"duplicate platform identity {identity!r}")
                seen_platforms.add(identity)
                platforms.append({
                    "source_row": row["row"],
                    "level": row["floor"],
                    **item,
                    "label": label,
                    "source_group_label": source_group_label,
                })

    concourses = [area for area in areas if "concourse" in area["kinds"]]
    normalized_platforms: list[dict[str, object]] = []
    for platform in platforms:
        basis, candidates = _concourse_candidates(platform, concourses, codes)
        levels = sorted({str(candidate["level"]) for candidate in candidates})
        normalized_platforms.append({
            **platform,
            "line_keys": _platform_line_keys(platform, codes),
            "concourse_match_basis": basis,
            "concourse_candidates": candidates,
            "concourse_levels": levels,
            "concourse_level_is_unique": len(levels) == 1,
        })

    coverage_reviews: list[str] = []
    if not platforms:
        coverage_reviews.append("no_platform_records")
    if not concourses:
        coverage_reviews.append("no_concourse_record")
    unresolved = sum(item["concourse_match_basis"] == "unresolved" for item in normalized_platforms)
    ambiguous = sum(
        not item["concourse_level_is_unique"] and item["concourse_match_basis"] != "unresolved"
        for item in normalized_platforms
    )
    if unresolved:
        coverage_reviews.append("platform_without_concourse_candidate")
    if ambiguous:
        coverage_reviews.append("platform_with_multiple_concourse_levels")
    return {
        "areas": areas,
        "platforms": normalized_platforms,
        "supplementary_tables": supplementary_tables,
        "blocking_reviews": blocking,
        "coverage_reviews": coverage_reviews,
        "coverage": {
            "level_row_count": len(areas),
            "platform_count": len(platforms),
            "source_platform_record_count": sum(
                len(row["platforms"]) for row in root["floors"]
            ),
            "grouped_source_platform_record_count": sum(
                "/" in item["label"]
                for row in root["floors"]
                for item in row["platforms"]
            ),
            "all_platforms_have_levels": bool(platforms),
            "concourse_count": len(concourses),
            "has_concourse": bool(concourses),
            "has_street_level": any("street_level" in area["kinds"] for area in areas),
            "platforms_with_unique_concourse_level": sum(
                item["concourse_level_is_unique"] for item in normalized_platforms
            ),
            "platforms_with_ambiguous_concourse_level": ambiguous,
            "platforms_without_concourse_candidate": unresolved,
            "explicit_exit_to_level_relationships": 0,
        },
    }


__all__ = ["normalize_layout"]
