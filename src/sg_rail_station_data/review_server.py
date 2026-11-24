"""Local review server that writes draft decisions only.

API contract
============

``GET /api/review-state`` returns the validated review bundle, existing draft
decisions, its immutable content hash, and ``bundle_sha256``.  The latter is a
state hash over the bundle and current draft list.

``POST /api/draft-decisions`` accepts exactly::

    {
      "schema_version": 1,
      "bundle_sha256": "<state hash from GET>",
      "station_id": "expo",
      "exit_id": "MRT:A",
      "proposed_coordinates": [103.0, 1.0],
      "change_category": "source_correction",
      "rationale": "...",
      "evidence": [{"url": "https://...", "title": "...", "note": "..."}],
      "alternatives_rejected": "..."
    }

The state hash prevents a stale browser tab from writing after another tab has
added a draft.  The server never edits approved configuration or the sign-off
ledger.  It creates only immutable ``audit/drafts/*.json`` files.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from threading import Lock
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from .contracts import ContractError, exact_int, exact_str, require_fields
from .review_bundle import canonical_sha256, validate_review_bundle
from .storage import atomic_write_json, read_json


HASH = re.compile(r"[0-9a-f]{64}")
DRAFT_NAME = re.compile(r"DRAFT-[0-9a-f]{32}\.json")
CHANGE_CATEGORIES = frozenset({
    "source_correction", "physical_relocation", "new_exit", "manual_gap_fill",
    "source_conflict", "revision_revalidation",
})


class ReviewServerError(ValueError):
    """Raised when a review request or saved draft is invalid."""


class StaleReviewState(ReviewServerError):
    """Raised when a browser submits an earlier review-state hash."""


def _coordinates(value: object, context: str) -> list[float]:
    if type(value) is not list or len(value) != 2:
        raise ReviewServerError(f"{context}: expected [longitude, latitude]")
    longitude, latitude = value
    if type(longitude) is not float or type(latitude) is not float:
        raise ReviewServerError(f"{context}: coordinates must be exact floats")
    if not 103.58 <= longitude <= 104.12 or not 1.12 <= latitude <= 1.50:
        raise ReviewServerError(f"{context}: coordinate is outside Singapore")
    return [longitude, latitude]


def _https_url(value: object, context: str) -> str:
    url = exact_str(value, context)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ReviewServerError(f"{context}: expected credential-free HTTPS URL")
    return url


def _validate_draft(value: object, context: str = "draft") -> dict[str, object]:
    row = require_fields(
        value,
        {
            "schema_version", "kind", "status", "decision_id", "created_at",
            "run_id", "bundle_content_sha256", "review_state_sha256", "station_id",
            "station_name", "exit_id", "before", "proposed_coordinates",
            "change_category", "rationale", "evidence", "alternatives_rejected",
        },
        context,
    )
    if exact_int(row["schema_version"], f"{context}.schema_version") != 1:
        raise ReviewServerError(f"{context}.schema_version: unsupported version")
    if exact_str(row["kind"], f"{context}.kind") != "coordinate_decision_draft":
        raise ReviewServerError(f"{context}.kind: unsupported kind")
    if exact_str(row["status"], f"{context}.status") != "draft":
        raise ReviewServerError(f"{context}.status: must be draft")
    for field in ("decision_id", "created_at", "run_id", "station_id", "station_name", "exit_id", "rationale", "alternatives_rejected"):
        exact_str(row[field], f"{context}.{field}")
    try:
        created_at = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
    except ValueError as error:
        raise ReviewServerError(f"{context}.created_at: expected ISO 8601 date and time") from error
    if created_at.tzinfo is None:
        raise ReviewServerError(f"{context}.created_at: timezone is required")
    for field in ("bundle_content_sha256", "review_state_sha256"):
        if HASH.fullmatch(exact_str(row[field], f"{context}.{field}")) is None:
            raise ReviewServerError(f"{context}.{field}: expected SHA-256")
    if row["before"] is not None:
        before = require_fields(row["before"], {"coordinates", "source"}, f"{context}.before")
        _coordinates(before["coordinates"], f"{context}.before.coordinates")
        exact_str(before["source"], f"{context}.before.source")
    _coordinates(row["proposed_coordinates"], f"{context}.proposed_coordinates")
    category = exact_str(row["change_category"], f"{context}.change_category")
    if category not in CHANGE_CATEGORIES:
        raise ReviewServerError(f"{context}.change_category: unsupported category")
    if type(row["evidence"]) is not list or not row["evidence"]:
        raise ReviewServerError(f"{context}.evidence: expected nonempty array")
    for index, value in enumerate(row["evidence"]):
        evidence = require_fields(value, {"url", "title", "note"}, f"{context}.evidence[{index}]")
        _https_url(evidence["url"], f"{context}.evidence[{index}].url")
        exact_str(evidence["title"], f"{context}.evidence[{index}].title")
        exact_str(evidence["note"], f"{context}.evidence[{index}].note")
    return dict(row)


class ReviewStore:
    """Validated bundle state and immutable draft-decision storage."""

    def __init__(
        self, bundle_path: Path, drafts_directory: Path,
        basemaps: list[dict[str, object]] | None = None,
    ):
        self.bundle_path = bundle_path.resolve()
        self.drafts_directory = drafts_directory.resolve()
        self.bundle = validate_review_bundle(read_json(self.bundle_path))
        self.bundle_content_sha256 = canonical_sha256(self.bundle)
        self.identities = {
            (row["station_id"], row["exit_id"]): row for row in self.bundle["exits"]
        }
        self.basemaps = list(default_basemaps() if basemaps is None else basemaps)
        if not self.drafts_directory.is_dir():
            raise ReviewServerError(
                f"draft directory does not exist: {self.drafts_directory}"
            )
        self._lock = Lock()
        self._load_drafts()

    def _load_drafts(self) -> list[dict[str, object]]:
        drafts: list[dict[str, object]] = []
        for path in sorted(self.drafts_directory.glob("*.json")):
            if DRAFT_NAME.fullmatch(path.name) is None or path.is_symlink() or not path.is_file():
                raise ReviewServerError(f"draft directory contains unexpected entry: {path.name}")
            draft = _validate_draft(read_json(path), f"saved draft {path.name}")
            if path.name != f"{draft['decision_id']}.json":
                raise ReviewServerError(
                    f"saved draft {path.name}: filename does not match decision_id"
                )
            if draft["bundle_content_sha256"] != self.bundle_content_sha256:
                raise ReviewServerError(
                    f"saved draft {path.name}: belongs to a different review bundle"
                )
            identity = (draft["station_id"], draft["exit_id"])
            exit_row = self.identities.get(identity)
            if exit_row is None:
                raise ReviewServerError(
                    f"saved draft {path.name}: unknown exit identity {identity}"
                )
            if draft["run_id"] != self.bundle["run"]["id"]:
                raise ReviewServerError(
                    f"saved draft {path.name}: run_id does not match the review bundle"
                )
            if draft["station_name"] != exit_row["station_name"]:
                raise ReviewServerError(
                    f"saved draft {path.name}: station_name does not match the review bundle"
                )
            if draft["before"] != exit_row["approved_coordinate"]:
                raise ReviewServerError(
                    f"saved draft {path.name}: before does not match the approved baseline"
                )
            drafts.append(draft)
        identifiers = [draft["decision_id"] for draft in drafts]
        if len(identifiers) != len(set(identifiers)):
            raise ReviewServerError("draft directory contains duplicate decision IDs")
        return drafts

    def _state_hash(self, drafts: list[dict[str, object]]) -> str:
        return canonical_sha256({
            "bundle_content_sha256": self.bundle_content_sha256,
            "draft_sha256": [canonical_sha256(draft) for draft in drafts],
        })

    def state(self) -> dict[str, object]:
        with self._lock:
            drafts = self._load_drafts()
            return {
                "schema_version": 1,
                "kind": "sg_rail_station_data_review_state",
                "status": "editable",
                "bundle": self.bundle,
                "bundle_content_sha256": self.bundle_content_sha256,
                "bundle_sha256": self._state_hash(drafts),
                "basemaps": self.basemaps,
                "drafts": drafts,
            }

    def save(self, request: object) -> dict[str, object]:
        row = require_fields(
            request,
            {
                "schema_version", "bundle_sha256", "station_id", "exit_id",
                "proposed_coordinates", "change_category", "rationale", "evidence",
                "alternatives_rejected",
            },
            "draft request",
        )
        if exact_int(row["schema_version"], "draft request.schema_version") != 1:
            raise ReviewServerError("draft request.schema_version: unsupported version")
        expected_state = exact_str(row["bundle_sha256"], "draft request.bundle_sha256")
        station_id = exact_str(row["station_id"], "draft request.station_id")
        exit_id = exact_str(row["exit_id"], "draft request.exit_id")
        identity = (station_id, exit_id)
        if identity not in self.identities:
            raise ReviewServerError(f"draft request: unknown exit identity {identity}")
        proposed = _coordinates(row["proposed_coordinates"], "draft request.proposed_coordinates")
        category = exact_str(row["change_category"], "draft request.change_category")
        if category not in CHANGE_CATEGORIES:
            raise ReviewServerError("draft request.change_category: unsupported category")
        rationale = exact_str(row["rationale"], "draft request.rationale")
        alternatives = exact_str(row["alternatives_rejected"], "draft request.alternatives_rejected")
        if type(row["evidence"]) is not list or not row["evidence"]:
            raise ReviewServerError("draft request.evidence: expected nonempty array")
        evidence: list[dict[str, str]] = []
        for index, value in enumerate(row["evidence"]):
            item = require_fields(value, {"url", "title", "note"}, f"draft request.evidence[{index}]")
            evidence.append({
                "url": _https_url(item["url"], f"draft request.evidence[{index}].url"),
                "title": exact_str(item["title"], f"draft request.evidence[{index}].title"),
                "note": exact_str(item["note"], f"draft request.evidence[{index}].note"),
            })

        with self._lock:
            drafts = self._load_drafts()
            current_state = self._state_hash(drafts)
            if expected_state != current_state:
                raise StaleReviewState(
                    "Review state changed. Reload the page before saving this draft."
                )
            exit_row = self.identities[identity]
            decision_id = f"DRAFT-{uuid4().hex}"
            draft = {
                "schema_version": 1,
                "kind": "coordinate_decision_draft",
                "status": "draft",
                "decision_id": decision_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "run_id": self.bundle["run"]["id"],
                "bundle_content_sha256": self.bundle_content_sha256,
                "review_state_sha256": current_state,
                "station_id": station_id,
                "station_name": exit_row["station_name"],
                "exit_id": exit_id,
                "before": exit_row["approved_coordinate"],
                "proposed_coordinates": proposed,
                "change_category": category,
                "rationale": rationale,
                "evidence": evidence,
                "alternatives_rejected": alternatives,
            }
            _validate_draft(draft)
            atomic_write_json(self.drafts_directory / f"{decision_id}.json", draft)
            updated = drafts + [draft]
            return {
                "status": "created",
                "draft": draft,
                "bundle_sha256": self._state_hash(updated),
            }


# Only expose the built preview UI and its inspection reports, not the
# containing run directory, source archives or directory listings.
NETWORK_FILES = frozenset({
    "index.html", "app.js", "styles.css", "preview-data.js", "manifest.json",
    "coverage.json", "changes.json", "vendor/leaflet/leaflet.js", "vendor/leaflet/leaflet.css",
})


def validate_network_preview(directory: Path) -> Path:
    root = directory.resolve()
    for name in NETWORK_FILES:
        path = root / name
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise ReviewServerError(f"Invalid or incomplete network preview: {name}")
    manifest = read_json(root / "manifest.json")
    if (not isinstance(manifest, dict) or manifest.get("kind") != "rail_network_geometry_preview"
            or manifest.get("publication_allowed") is not False):
        raise ReviewServerError("Expected an unapproved network geometry preview")
    return root


class ReviewRequestHandler(SimpleHTTPRequestHandler):
    """Serve static UI assets and the loopback review API."""

    store: ReviewStore
    allowed_origins: frozenset[str]

    network_preview: Path | None = None

    def send_head(self):
        path = urlsplit(self.path).path
        if path == '/stage3' or path.startswith('/stage3/'):
            name = path.removeprefix('/stage3').lstrip('/') or 'index.html'
            if self.network_preview is None or name not in NETWORK_FILES | {"stage-switch.css", "stage-switch.js", "continuity.json"}:
                self.send_error(404)
                return None
            if path == '/stage3':
                self.send_response(301)
                self.send_header('Location', '/stage3/')
                self.end_headers()
                return None
            asset = self.network_preview / name
            if not asset.resolve().is_relative_to(self.network_preview) or not asset.is_file():
                self.send_error(404)
                return None
            self.directory = str(self.network_preview)
            self.path = '/' + name
        return super().send_head()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self' https:; frame-ancestors 'none'")
        super().end_headers()

    def _json(self, status: int, value: object) -> None:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/api/review-state":
            self._json(200, {**self.store.state(), "network_preview": self.network_preview is not None})
            return
        super().do_GET()

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/api/draft-decisions":
            self._json(404, {"error": "Unknown API endpoint"})
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.allowed_origins:
            self._json(403, {"error": "Unexpected Origin header"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "Invalid Content-Length"})
            return
        if not 0 < length <= 64_000:
            self._json(413, {"error": "Request body must be between 1 and 64000 bytes"})
            return
        try:
            request = json.loads(self.rfile.read(length))
            result = self.store.save(request)
        except StaleReviewState as error:
            self._json(409, {"error": str(error)})
            return
        except (ReviewServerError, ContractError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})
            return
        except OSError:
            self._json(500, {"error": "Could not save the draft decision"})
            return
        self._json(201, result)

    def log_message(self, format: str, *args: Any) -> None:
        super().log_message(format, *args)


def serve_review_app(
    *, bundle_path: Path, drafts_directory: Path, static_directory: Path,
    bind: str = "127.0.0.1", port: int = 8000, network_preview: Path | None = None,
) -> None:
    """Serve the local application until interrupted."""

    if not 1 <= port <= 65_535:
        raise ReviewServerError("port must be between 1 and 65535")
    static = static_directory.resolve()
    if not static.is_dir():
        raise ReviewServerError(f"static directory does not exist: {static}")
    store = ReviewStore(bundle_path, drafts_directory)
    class Handler(ReviewRequestHandler):
        pass

    Handler.store = store
    Handler.network_preview = validate_network_preview(network_preview) if network_preview else None
    Handler.allowed_origins = frozenset({f"http://{bind}:{port}", f"http://localhost:{port}"})
    handler = partial(Handler, directory=str(static))
    server = ThreadingHTTPServer((bind, port), handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def default_basemaps() -> list[dict[str, object]]:
    """Return an out-of-the-box neutral view and an unfiltered OSM view."""

    osm = {
        "url_template": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>',
        "max_zoom": 19,
    }
    return [{
        **osm,
        "id": "neutral-osm",
        "label": "Neutral OSM",
        "is_default": True,
        "neutral_style": True,
    }, {
        **osm,
        "id": "osm",
        "label": "OpenStreetMap (full colour)",
        "is_default": False,
        "neutral_style": False,
    }]
