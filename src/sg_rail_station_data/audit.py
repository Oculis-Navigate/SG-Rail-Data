"""Validate and update the intervention decision ledger.

The JSONL ledger is canonical. Markdown sign-off pages are generated views.
An audit approval does not approve a data release.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
from pathlib import Path
import re
import tempfile
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from .contracts import exact_int, exact_str, fail, require_fields, safe_repo_path, string_list
from .storage import atomic_write_bytes, atomic_write_json, atomic_write_text


PACKAGE = "audit/initial-package.json"
LEDGER = "audit/decision-ledger.jsonl"
DECISIONS = "audit/decisions"
SIGNOFFS = "audit/signoffs"
BOOTSTRAP = "audit/bootstrap"
STAGE1 = "config/stage1-reviewed-decisions.json"
COORDINATES = (
    "config/stage2-reviewed-coordinates.geojson",
    "config/stage2-trusted-onemap-baseline.geojson",
    "config/stage2-trusted-supplemental-baseline.geojson",
)

_EVENT_V1_FIELDS = {
    "schema_version", "sequence", "event_id", "recorded_at", "operation",
    "disposition", "decision_id", "decision_sha256", "package_sha256",
    "recorded_by", "approved_by", "authorization_basis", "authorization_refs",
    "historical_approval_at", "note", "previous_event_hash", "event_hash",
}
_EVENT_V2_FIELDS = {
    "schema_version", "sequence", "event_id", "recorded_at", "operation",
    "disposition", "decision_id", "decision_path", "decision_sha256",
    "package_path", "package_sha256", "recorded_by", "approved_by",
    "authorization_basis", "authorization_refs", "note", "related_decision_id",
    "previous_event_hash", "event_hash",
}
_DECISION_FIELDS = {
    "schema_version", "kind", "id", "title", "change_type", "reason",
    "targets", "evidence", "source_revisions", "limitations",
}
_TARGET_FIELDS = {
    "identity", "path", "pointer", "before", "after", "before_sha256",
    "after_sha256",
}
_OPERATIONS = {
    "propose": (None, "pending_review"),
    "approve": ("pending_review", "approved"),
    "reject": ("pending_review", "rejected"),
    "revalidate": ("approved", "approved"),
    "replace": ("approved", "replaced"),
    "retire": ("approved", "retired"),
    "apply": ("approved", "approved"),
}
_AUTHORIZATION_BASES = {
    "direct_human_signoff",
    "documented_delegation",
    "pull_request_review",
}


def content_hash(value: object) -> str:
    """Return the stable SHA-256 for a JSON value."""

    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return sha256(encoded).hexdigest()


def _hash(value: object, context: str) -> str:
    text = exact_str(value, context)
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        fail(context, "expected SHA-256")
    return text


def _list(value: object, context: str, *, nonempty: bool = True) -> list:
    if type(value) is not list or (nonempty and not value):
        fail(context, "expected nonempty array" if nonempty else "expected array")
    return value


def _decode(text: str, context: str) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail(context, f"duplicate JSON key {key}")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=pairs)
    except json.JSONDecodeError as error:
        fail(context, f"invalid JSON: {error}")


def _load(path: Path) -> object:
    return _decode(path.read_text(encoding="utf-8"), str(path))


def _pointer(document: object, pointer: str) -> object:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        fail("audit pointer", "must be an absolute JSON pointer")
    current = document
    try:
        for raw in pointer[1:].split("/"):
            token = raw.replace("~1", "/").replace("~0", "~")
            if type(current) is list:
                if re.fullmatch(r"0|[1-9][0-9]*", token) is None:
                    fail("audit pointer", "noncanonical array index")
                current = current[int(token)]
            else:
                current = current[token]
    except (KeyError, IndexError, TypeError):
        fail("audit pointer", f"unresolved pointer {pointer}")
    return current


def _validate_decision(document: object, root: Path, context: str) -> dict:
    row = dict(require_fields(document, _DECISION_FIELDS, context))
    if exact_int(row["schema_version"], f"{context} schema") != 2:
        fail(context, "unsupported schema")
    if row["kind"] != "audit_decision":
        fail(context, "unsupported kind")
    for field in ("id", "title", "change_type", "reason"):
        exact_str(row[field], f"{context} {field}")
    if re.fullmatch(r"D-[0-9]{3,}", row["id"]) is None:
        fail(context, "decision ID must use D-NNN format")
    targets = _list(row["targets"], f"{context} targets")
    identities = set()
    locations = set()
    for index, value in enumerate(targets):
        target = require_fields(value, _TARGET_FIELDS, f"{context} target {index}")
        identity = exact_str(target["identity"], f"{context} target identity")
        if identity in identities:
            fail(context, f"duplicate target identity {identity}")
        identities.add(identity)
        target_path = exact_str(target["path"], f"{context} target path")
        if not target_path.startswith("config/"):
            fail(context, "target path must be under config/")
        safe_repo_path(target_path, root, f"{context} target path")
        pointer = exact_str(
            target["pointer"], f"{context} target pointer", nonempty=False
        )
        if pointer != "" and not pointer.startswith("/"):
            fail(context, "target pointer must be a JSON pointer")
        location = (target_path, pointer)
        if location in locations:
            fail(context, f"duplicate target location {target_path}#{pointer}")
        locations.add(location)
        before_hash = content_hash(target["before"])
        after_hash = content_hash(target["after"])
        if before_hash != _hash(target["before_sha256"], f"{context} before hash"):
            fail(context, f"target {identity} before hash mismatch")
        if after_hash != _hash(target["after_sha256"], f"{context} after hash"):
            fail(context, f"target {identity} after hash mismatch")
        if target["before"] == target["after"] and row["change_type"] != "revision_revalidation":
            fail(context, f"target {identity} has no change")
    evidence = _list(row["evidence"], f"{context} evidence")
    for index, value in enumerate(evidence):
        item = require_fields(value, {"description", "path", "sha256"}, f"{context} evidence {index}")
        exact_str(item["description"], f"{context} evidence description")
        path = safe_repo_path(item["path"], root, f"{context} evidence path")
        if not path.is_file():
            fail(context, f"evidence file does not exist: {item['path']}")
        if sha256(path.read_bytes()).hexdigest() != _hash(item["sha256"], f"{context} evidence hash"):
            fail(context, f"evidence content changed: {item['path']}")
    string_list(row["source_revisions"], f"{context} source revisions")
    string_list(row["limitations"], f"{context} limitations")
    return row


def _bootstrap_path(root: Path, expected_hash: str) -> Path:
    """Return the immutable bootstrap copy for one hash, when it exists."""

    return root / BOOTSTRAP / expected_hash


def _historical_file(root: Path, name: str, expected_hash: str) -> Path:
    """Resolve a bootstrap file without tying validation to mutable live config."""

    archived = _bootstrap_path(root, expected_hash)
    path = archived if archived.exists() else safe_repo_path(name, root, "audit path")
    if not path.is_file() or path.is_symlink():
        fail("audit file", f"historical file does not exist: {name}")
    if sha256(path.read_bytes()).hexdigest() != expected_hash:
        location = archived.relative_to(root) if archived.exists() else name
        fail("audit file", f"historical content changed: {location}")
    return path


def _load_initial(root: Path) -> dict[str, object]:
    package_path = root / PACKAGE
    package = require_fields(_load(package_path), {
        "schema_version", "kind", "scope", "release_approval", "limitations", "files", "decisions",
    }, "audit package")
    if exact_int(package["schema_version"], "audit schema") != 1 or package["kind"] != "initial_intervention_inventory":
        fail("audit package", "unsupported schema/kind")
    if package["scope"] != "record_existing_configuration" or package["release_approval"] != "not_granted":
        fail("audit package", "initial ledger cannot approve a release")
    string_list(package["limitations"], "audit limitations", nonempty=True)
    files = {}
    for value in _list(package["files"], "audit files"):
        item = require_fields(value, {"path", "sha256"}, "audit file")
        name = exact_str(item["path"], "audit path")
        if name in files:
            fail("audit files", "duplicate path")
        expected_hash = _hash(item["sha256"], "audit file hash")
        path = _historical_file(root, name, expected_hash)
        files[name] = path
    required = {STAGE1, *COORDINATES, "audit/evidence/session-authorization.json"}
    if not required.issubset(files):
        fail("audit files", "missing required intervention/authorization file")
    documents = {name: _load(files[name]) for name in required}
    authorization = require_fields(documents["audit/evidence/session-authorization.json"], {
        "schema_version", "kind", "limitations", "statements",
    }, "authorization evidence")
    if exact_int(authorization["schema_version"], "authorization schema") != 1 or authorization["kind"] != "conversation_excerpts":
        fail("authorization evidence", "unsupported schema/kind")
    string_list(authorization["limitations"], "authorization limitations", nonempty=True)
    statements = {}
    for value in _list(authorization["statements"], "authorization statements"):
        row = require_fields(value, {"id", "speaker", "quote", "scope"}, "authorization statement")
        for field in row:
            exact_str(row[field], f"authorization {field}")
        if row["speaker"] != "project_owner" or row["id"] in statements:
            fail("authorization statement", "invalid speaker or duplicate ID")
        statements[row["id"]] = row
    expected = set()
    for group, values in documents[STAGE1].items():
        if group == "schema_version":
            continue
        for index in range(len(_list(values, f"Stage 1 {group}"))):
            expected.add((STAGE1, f"/{group}/{index}"))
    for name in COORDINATES:
        for index in range(len(_list(documents[name]["features"], name))):
            expected.add((name, f"/features/{index}"))
    covered = set()
    decisions = {}
    for value in _list(package["decisions"], "audit decisions"):
        row = dict(require_fields(value, {
            "id", "title", "rationale", "trust_class", "targets", "evidence_files", "limitations",
        }, "audit decision"))
        for field in ("id", "title", "rationale", "trust_class"):
            exact_str(row[field], f"decision {field}")
        string_list(row["limitations"], "decision limitations")
        evidence = string_list(row["evidence_files"], "decision evidence", nonempty=True)
        if not set(evidence).issubset(files):
            fail("audit decision", "evidence file not hash-pinned")
        if row["id"] in decisions:
            fail("audit decision", "duplicate ID")
        decisions[row["id"]] = row
        for target_value in _list(row["targets"], "decision targets"):
            target = require_fields(target_value, {"path", "pointer", "identity", "sha256"}, "decision target")
            for field in ("path", "pointer", "identity"):
                exact_str(target[field], f"target {field}")
            key = (target["path"], target["pointer"])
            if key in covered or key not in expected:
                fail("decision targets", "duplicate or unexpected target")
            actual = _pointer(documents[target["path"]], target["pointer"])
            identity = actual["station"] if target["path"] == STAGE1 else actual["id"]
            if target["identity"] != identity or content_hash(actual) != _hash(target["sha256"], "target hash"):
                fail("decision target", "identity or content mismatch")
            covered.add(key)
    if covered != expected:
        fail("audit coverage", f"{len(expected - covered)} interventions lack decision records")
    return {
        "package": package,
        "package_hash": sha256(package_path.read_bytes()).hexdigest(),
        "decisions": decisions,
        "statements": statements,
        "intervention_count": len(covered),
    }


def _timestamp(value: object, context: str) -> str:
    text = exact_str(value, context)
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        fail(context, "expected ISO 8601 date and time")
    if stamp.tzinfo is None:
        fail(context, "timezone is required")
    return text


def _validate_event_v1(row: dict, sequence: int, initial: dict, state: dict) -> tuple[dict, str]:
    require_fields(row, _EVENT_V1_FIELDS, "ledger event")
    if row["event_id"] != f"S-{sequence:03d}" or row["operation"] != "record_existing":
        fail("ledger", "unsupported initial event identity/operation")
    _timestamp(row["recorded_at"], "ledger recorded_at")
    if row["historical_approval_at"] is not None or row["recorded_by"] != "codex_agent":
        fail("ledger", "initial event has invalid historical time or actor")
    decision_id = exact_str(row["decision_id"], "ledger decision_id")
    if decision_id not in initial["decisions"] or decision_id in state:
        fail("ledger", "unknown or duplicate initial decision")
    if row["decision_sha256"] != content_hash(initial["decisions"][decision_id]) or row["package_sha256"] != initial["package_hash"]:
        fail("ledger", "decision/package hash mismatch")
    refs = string_list(row["authorization_refs"], "ledger authorization refs")
    if not set(refs).issubset(initial["statements"]):
        fail("ledger", "unknown authorization reference")
    if row["disposition"] == "approved":
        if not refs or row["approved_by"] != "project_owner" or row["authorization_basis"] not in {"prior_explicit_instruction", "prior_delegation"}:
            fail("ledger", "approval needs attributed authorization")
    elif row["disposition"] == "pending_review":
        if row["approved_by"] is not None or row["authorization_basis"] != "not_established":
            fail("ledger", "pending decision cannot assert approval")
    else:
        fail("ledger", "unsupported initial disposition")
    exact_str(row["note"], "ledger note")
    return initial["decisions"][decision_id], row["disposition"]


def _event_decision(row: dict, root: Path, initial: dict) -> tuple[dict, str]:
    decision_path = exact_str(row["decision_path"], "ledger decision_path")
    if decision_path == PACKAGE:
        decision_id = row["decision_id"]
        if decision_id not in initial["decisions"]:
            fail("ledger", "initial package has no such decision")
        decision = initial["decisions"][decision_id]
    else:
        expected_path = f"{DECISIONS}/{row['decision_id']}.json"
        if decision_path != expected_path:
            fail("ledger", f"decision path must be {expected_path}")
        decision = _validate_decision(_load(safe_repo_path(decision_path, root, "ledger decision path")), root, "audit decision")
        if decision["id"] != row["decision_id"]:
            fail("ledger", "decision path and ID differ")
    return decision, decision_path


def _target_map(decision: dict) -> dict[tuple[str, str], dict]:
    """Index the strict targets of a standalone decision."""

    if decision.get("schema_version") != 2:
        return {}
    return {
        (target["path"], target["pointer"]): target
        for target in decision["targets"]
    }


def _validate_replacement(old: dict, replacement: dict) -> None:
    """Require an atomic replacement to continue every old target exactly."""

    old_targets = _target_map(old)
    replacement_targets = _target_map(replacement)
    if not old_targets or set(old_targets) != set(replacement_targets):
        fail("ledger", "replacement must cover the same strict targets as the old decision")
    for key, old_target in old_targets.items():
        new_target = replacement_targets[key]
        if (
            old_target["identity"] != new_target["identity"]
            or
            old_target["after"] != new_target["before"]
            or old_target["after_sha256"] != new_target["before_sha256"]
        ):
            fail(
                "ledger",
                "replacement before value must equal the old decision after value for "
                f"{key[0]}#{key[1]}",
            )


def _approved_target_owners(state: dict, decision_id: str) -> set[str]:
    current = state[decision_id]["decision"]
    target_keys = set(_target_map(current))
    if not target_keys:
        return set()
    return {
        other_id
        for other_id, other in state.items()
        if other_id != decision_id
        and other["status"] == "approved"
        and target_keys & set(_target_map(other["decision"]))
    }


def _validate_event_v2(row: dict, sequence: int, root: Path, initial: dict, state: dict) -> tuple[dict, str]:
    require_fields(row, _EVENT_V2_FIELDS, "ledger event")
    if row["event_id"] != f"S-{sequence:03d}":
        fail("ledger", "event ID does not match sequence")
    _timestamp(row["recorded_at"], "ledger recorded_at")
    operation = exact_str(row["operation"], "ledger operation")
    if operation not in _OPERATIONS:
        fail("ledger", f"unsupported operation {operation}")
    decision_id = exact_str(row["decision_id"], "ledger decision_id")
    decision, decision_path = _event_decision(row, root, initial)
    if row["decision_sha256"] != content_hash(decision):
        fail("ledger", "decision hash mismatch")
    package_path = exact_str(row["package_path"], "ledger package_path")
    package_file = safe_repo_path(package_path, root, "ledger package path")
    if not package_file.is_file() or row["package_sha256"] != sha256(package_file.read_bytes()).hexdigest():
        fail("ledger", "package hash mismatch")
    if decision_path == PACKAGE and package_path != PACKAGE:
        fail("ledger", "initial decision must use the initial package")
    if decision_path != PACKAGE and package_path != decision_path:
        fail("ledger", "standalone decision must be its own package")
    old_required, disposition = _OPERATIONS[operation]
    previous = state.get(decision_id)
    if old_required is None:
        if previous is not None:
            fail("ledger", "propose requires a new decision ID")
    elif (
        previous is None
        or previous["status"] != old_required
    ):
        actual = None if previous is None else previous["status"]
        fail("ledger", f"invalid {operation} transition from {actual}")
    if row["disposition"] != disposition:
        fail("ledger", f"{operation} must use disposition {disposition}")
    exact_str(row["recorded_by"], "ledger recorded_by")
    exact_str(row["note"], "ledger note")
    refs = string_list(row["authorization_refs"], "ledger authorization refs")
    related = row["related_decision_id"]
    if operation == "propose":
        if row["approved_by"] is not None or row["authorization_basis"] != "not_applicable" or refs or related is not None:
            fail("ledger", "a proposal cannot assert approval or a related decision")
    else:
        exact_str(row["approved_by"], "ledger approved_by")
        basis = exact_str(row["authorization_basis"], "ledger authorization_basis")
        if basis not in _AUTHORIZATION_BASES:
            fail("ledger", f"unsupported authorization basis {basis}")
        if not refs:
            fail("ledger", "a human decision needs an authorization reference")
        if operation == "replace":
            related_id = exact_str(related, "ledger related_decision_id")
            related_state = state.get(related_id)
            if related_id == decision_id or related_state is None or related_state["status"] != "pending_review":
                fail("ledger", "replacement must identify another pending decision")
            if decision_path == PACKAGE or related_state["decision_path"] == PACKAGE:
                fail("ledger", "standalone strict decisions are required for replacement")
            _validate_replacement(decision, related_state["decision"])
        elif operation == "approve":
            owners = _approved_target_owners(state, decision_id)
            if owners:
                fail(
                    "ledger",
                    "approve would overlap approved decision(s) "
                    f"{', '.join(sorted(owners))}; use replace",
                )
        elif related is not None:
            fail("ledger", f"{operation} cannot identify a related decision")
    return decision, disposition


def _read_audit(root: Path) -> dict[str, object]:
    root = root.resolve()
    initial = _load_initial(root)
    ledger_path = root / LEDGER
    raw = ledger_path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        fail("ledger", "file must end with a newline")
    lines = raw.decode("utf-8").splitlines()
    state: dict[str, dict] = {}
    events = []
    previous_hash = None
    initial_seen = set()
    for sequence, line in enumerate(lines, 1):
        decoded = _decode(line, f"ledger line {sequence}")
        if type(decoded) is not dict:
            fail("ledger event", "expected table")
        row = decoded
        schema = exact_int(row.get("schema_version"), "ledger schema")
        if exact_int(row.get("sequence"), "ledger sequence") != sequence:
            fail("ledger", "invalid sequence")
        if schema == 1:
            decision, disposition = _validate_event_v1(row, sequence, initial, state)
            initial_seen.add(row["decision_id"])
            decision_path = PACKAGE
        elif schema == 2:
            decision, disposition = _validate_event_v2(row, sequence, root, initial, state)
            decision_path = row["decision_path"]
        else:
            fail("ledger", "unsupported event schema")
        body = {key: value for key, value in row.items() if key != "event_hash"}
        if row["previous_event_hash"] != previous_hash or _hash(row["event_hash"], "event hash") != content_hash(body):
            fail("ledger", "broken event hash chain")
        previous_hash = row["event_hash"]
        state[row["decision_id"]] = {
            "status": disposition,
            "decision": decision,
            "decision_path": decision_path,
            "event": row,
        }
        if schema == 2 and row["operation"] == "replace":
            replacement_id = row["related_decision_id"]
            replacement = state[replacement_id]
            state[replacement_id] = {
                **replacement,
                "status": "approved",
                "event": row,
            }
        events.append(row)
    if initial_seen != set(initial["decisions"]):
        fail("ledger", "missing initial decision sign-off records")
    return {"root": root, "initial": initial, "events": events, "state": state, "ledger_head": previous_hash}


def audit_status(root: Path) -> dict[str, object]:
    """Validate the ledger and return each decision's effective state."""

    audit = _read_audit(root)
    by_status: dict[str, list[str]] = defaultdict(list)
    for decision_id, value in audit["state"].items():
        by_status[value["status"]].append(decision_id)
    for values in by_status.values():
        values.sort()
    pending = by_status.get("pending_review", [])
    return {
        "status": "review_required" if pending else "valid",
        "package_sha256": audit["initial"]["package_hash"],
        "decision_count": len(audit["state"]),
        "intervention_count": audit["initial"]["intervention_count"],
        "approved_count": len(by_status.get("approved", [])),
        "pending_decision_ids": pending,
        "effective_states": dict(sorted(by_status.items())),
        "event_count": len(audit["events"]),
        "release_approval": "not_granted",
        "ledger_head": audit["ledger_head"],
    }


def validate_initial_ledger(root: Path) -> dict[str, object]:
    """Backward-compatible name for complete audit validation."""

    return audit_status(root)


def _append_event(root: Path, values: dict[str, object]) -> dict[str, object]:
    root = root.resolve()
    ledger_path = root / LEDGER
    root_hash = sha256(str(root).encode()).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"sg-rail-station-data-audit-{root_hash}.lock"
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        audit = _read_audit(root)
        sequence = len(audit["events"]) + 1
        event = {
            "schema_version": 2,
            "sequence": sequence,
            "event_id": f"S-{sequence:03d}",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "previous_event_hash": audit["ledger_head"],
            **values,
        }
        event["event_hash"] = content_hash(event)
        line = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        current = ledger_path.read_text(encoding="utf-8")
        atomic_write_text(ledger_path, current + line + "\n", overwrite=True)
        try:
            result = audit_status(root)
        except BaseException:
            atomic_write_text(ledger_path, current, overwrite=True)
            raise
        return {"event": event, "audit": result}


def _assert_targets_match_live(root: Path, decision: dict) -> None:
    """Check proposal before-values against the current configuration once."""

    documents: dict[str, object] = {}
    for target in decision["targets"]:
        path = target["path"]
        if path not in documents:
            source = safe_repo_path(path, root, "decision target path")
            if not source.is_file() or source.is_symlink():
                fail("audit propose", f"target file does not exist: {path}")
            documents[path] = _load(source)
        actual = _pointer(documents[path], target["pointer"])
        if actual != target["before"] or content_hash(actual) != target["before_sha256"]:
            fail(
                "audit propose",
                f"stale before value at {path}#{target['pointer']}",
            )


def _store_proposal(
    root: Path,
    decision: dict,
    *,
    recorded_by: str,
    note: str,
) -> dict[str, object]:
    """Store one validated decision and append its pending ledger event."""

    decision_id = decision["id"]
    if decision_id in _read_audit(root)["state"]:
        fail("audit propose", f"decision {decision_id} already exists")
    _assert_targets_match_live(root, decision)
    directory = root / DECISIONS
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{decision_id}.json"
    if destination.exists():
        fail("audit propose", f"decision file already exists: {destination.relative_to(root)}")
    atomic_write_json(destination, decision)
    path = f"{DECISIONS}/{decision_id}.json"
    try:
        return _append_event(root, {
            "operation": "propose", "disposition": "pending_review",
            "decision_id": decision_id, "decision_path": path,
            "decision_sha256": content_hash(decision), "package_path": path,
            "package_sha256": sha256(destination.read_bytes()).hexdigest(),
            "recorded_by": recorded_by, "approved_by": None,
            "authorization_basis": "not_applicable", "authorization_refs": [],
            "note": note, "related_decision_id": None,
        })
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def propose_decision(root: Path, decision_file: Path, *, recorded_by: str, note: str) -> dict[str, object]:
    """Copy one decision into the audit store and append a pending event."""

    root = root.resolve()
    recorded_by = exact_str(recorded_by, "recorded_by")
    note = exact_str(note, "note")
    source = decision_file.resolve()
    decision = _validate_decision(_load(source), root, "proposed decision")
    return _store_proposal(
        root,
        decision,
        recorded_by=recorded_by,
        note=note,
    )


def _retain_local_evidence(root: Path, source: Path) -> dict[str, str]:
    source = source.resolve()
    if not source.is_file() or source.is_symlink():
        fail("audit evidence", f"not a regular file: {source}")
    raw = source.read_bytes()
    digest = sha256(raw).hexdigest()
    suffix = source.suffix.lower() if re.fullmatch(r"\.[a-z0-9]{1,10}", source.suffix.lower()) else ""
    directory = root / "audit/evidence/retained"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{digest}{suffix}"
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != raw:
            fail("audit evidence", f"retained evidence collision: {destination.relative_to(root)}")
    else:
        atomic_write_bytes(destination, raw)
    return {
        "description": f"Retained local evidence: {source.name}",
        "path": destination.relative_to(root).as_posix(),
        "sha256": digest,
    }


def _json_file_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True
        )
        + "\n"
    ).encode("utf-8")


def _whole_document_target(identity: str, path: str, before: object, after: object) -> dict:
    return {
        "identity": identity,
        "path": path,
        "pointer": "",
        "before": before,
        "after": after,
        "before_sha256": content_hash(before),
        "after_sha256": content_hash(after),
    }


def _reviewed_override_documents(
    root: Path,
    draft: dict,
    bundle: dict,
    stage1: dict,
    decision_id: str,
    draft_file_sha256: str,
) -> tuple[dict, dict, dict, dict]:
    """Build a ledger-backed reviewed override without mutating frozen evidence."""

    from .stage2 import (
        _reviewed_coordinates,
        _stage1_identity_index,
        reviewed_ledger_entry_hash,
    )

    identity = (draft["station_id"], draft["exit_id"])
    _, stage1_identities = _stage1_identity_index(stage1)
    stage1_identity = stage1_identities.get(identity)
    if stage1_identity is None:
        fail("coordinate draft", f"identity is absent from the retained Stage 1 data: {identity}")
    station = stage1_identity["station"]
    exit_row = stage1_identity["exit"]
    if station["name"] != draft["station_name"]:
        fail("coordinate draft", "station name differs from retained Stage 1 data")
    bundle_exit = next(
        (
            row for row in bundle["exits"]
            if (row["station_id"], row["exit_id"]) == identity
        ),
        None,
    )
    if bundle_exit is None:
        fail("coordinate draft", "identity is absent from the retained review bundle")
    if bundle_exit["approved_coordinate"] != draft["before"]:
        fail("coordinate draft", "draft before-coordinate differs from the review bundle")
    if draft["before"] is None:
        fail("coordinate draft", "an approved before-coordinate is required")
    if draft["before"]["coordinates"] == draft["proposed_coordinates"]:
        fail("coordinate draft", "proposed coordinate does not change the approved coordinate")

    ledger_path = root / "config/stage2-coordinate-ledger.json"
    reviewed_path = root / "config/stage2-reviewed-coordinates.geojson"
    before_ledger = _load(ledger_path)
    before_reviewed = _load(reviewed_path)
    if sha256(ledger_path.read_bytes()).hexdigest() != bundle["inputs"]["coordinate_ledger_sha256"]:
        fail("coordinate draft", "coordinate ledger changed after the review bundle was built")
    if sha256(reviewed_path.read_bytes()).hexdigest() != bundle["inputs"]["reviewed_coordinates_sha256"]:
        fail("coordinate draft", "reviewed coordinates changed after the review bundle was built")
    _reviewed_coordinates(before_reviewed, before_ledger)

    after_ledger = deepcopy(before_ledger)
    entries = after_ledger["entries"]
    existing_feature = next(
        (feature for feature in before_reviewed["features"] if feature["id"] == f"{identity[0]}:{identity[1]}"),
        None,
    )
    if existing_feature is not None:
        prior_from = {
            "source": "human_reviewed",
            "coordinates": list(existing_feature["geometry"]["coordinates"]),
            "edit_id": existing_feature["properties"]["edit_id"],
        }
        based_on_source = existing_feature["properties"]["based_on_source"]
        based_on_coordinates = list(existing_feature["properties"]["based_on_coordinates"])
    else:
        prior_from = {
            "source": "audit_review_draft",
            "coordinates": list(draft["before"]["coordinates"]),
            "decision_id": decision_id,
            "draft_sha256": draft_file_sha256,
            "bundle_sha256": draft["bundle_content_sha256"],
            "basis_source": draft["before"]["source"],
        }
        based_on_source = prior_from["source"]
        based_on_coordinates = list(prior_from["coordinates"])
    draft_digest = content_hash(draft)
    edit_id = str(uuid5(NAMESPACE_URL, f"sg-rail-station-data:{decision_id}:{draft_digest}"))
    review_note = (
        f"{draft['rationale']} Alternatives rejected: {draft['alternatives_rejected']}"
    )
    entry_without_hash = {
        "sequence": len(entries) + 1,
        "edit_id": edit_id,
        "saved_at": draft["created_at"],
        "station_id": identity[0],
        "station_name": station["name"],
        "exit_id": identity[1],
        "label": exit_row["label"],
        "from": prior_from,
        "to_coordinates": list(draft["proposed_coordinates"]),
        "note": review_note,
        "previous_entry_hash": entries[-1]["entry_hash"] if entries else None,
    }
    entry_hash = reviewed_ledger_entry_hash(entry_without_hash)
    entries.append({**entry_without_hash, "entry_hash": entry_hash})
    after_ledger["revision"] = len(entries)

    source = station["source"]
    new_feature = {
        "type": "Feature",
        "id": f"{identity[0]}:{identity[1]}",
        "geometry": {"type": "Point", "coordinates": list(draft["proposed_coordinates"])},
        "properties": {
            "station_id": identity[0],
            "station_name": station["name"],
            "station_codes": list(station["codes"]),
            "exit_id": identity[1],
            "label": exit_row["label"],
            "scope": identity[1].split(":", 1)[0],
            "mode": "override",
            "approved_at": draft["created_at"],
            "edit_id": edit_id,
            "ledger_entry_hash": entry_hash,
            "based_on_source": based_on_source,
            "based_on_coordinates": based_on_coordinates,
            "review_note": review_note,
            "source_guard": {
                "sgwiki_page_id": source["page_id"],
                "sgwiki_revision_id": source["revision_id"],
            },
        },
    }
    after_reviewed = deepcopy(before_reviewed)
    features = after_reviewed["features"]
    features = [feature for feature in features if feature["id"] != new_feature["id"]]
    features.append(new_feature)
    features.sort(key=lambda feature: feature["id"])
    after_reviewed["features"] = features
    metadata = after_reviewed["metadata"]
    metadata["revision"] = after_ledger["revision"]
    metadata["feature_count"] = len(features)
    metadata["ledger_sha256"] = sha256(_json_file_bytes(after_ledger)).hexdigest()
    metadata["ledger_head_entry_hash"] = entry_hash
    _reviewed_coordinates(after_reviewed, after_ledger)
    return before_ledger, after_ledger, before_reviewed, after_reviewed


def promote_coordinate_draft(
    root: Path,
    draft_file: Path,
    decision_id: str,
    *,
    bundle_file: Path,
    stage1_file: Path,
    recorded_by: str,
    note: str,
    evidence_files: list[Path] | None = None,
) -> dict[str, object]:
    """Promote one saved map draft to a strict, pending audit decision."""

    from .review_bundle import canonical_sha256, validate_review_bundle
    from .review_server import ReviewServerError, _validate_draft

    root = root.resolve()
    recorded_by = exact_str(recorded_by, "recorded_by")
    note = exact_str(note, "note")
    if re.fullmatch(r"D-[0-9]{3,}", exact_str(decision_id, "decision_id")) is None:
        fail("decision_id", "must use D-NNN format")
    source = draft_file.resolve()
    drafts_root = (root / "audit/drafts").resolve()
    if source.parent != drafts_root or not source.is_file() or source.is_symlink():
        fail("coordinate draft", "must be a regular file directly under audit/drafts")
    try:
        draft = _validate_draft(_load(source), "coordinate draft")
    except ReviewServerError as error:
        fail("coordinate draft", str(error))
    if source.name != f"{draft['decision_id']}.json":
        fail("coordinate draft", "filename does not match the draft decision_id")
    bundle_source = bundle_file.resolve()
    stage1_source = stage1_file.resolve()
    bundle = validate_review_bundle(_load(bundle_source))
    if canonical_sha256(bundle) != draft["bundle_content_sha256"]:
        fail("coordinate draft", "review bundle hash does not match the saved draft")
    if bundle["run"]["id"] != draft["run_id"]:
        fail("coordinate draft", "review bundle run does not match the saved draft")
    stage1 = _load(stage1_source)
    if sha256(stage1_source.read_bytes()).hexdigest() != bundle["inputs"]["stage1_sha256"]:
        fail("coordinate draft", "Stage 1 file hash does not match the review bundle")
    before_ledger, after_ledger, before_reviewed, after_reviewed = _reviewed_override_documents(
        root,
        draft,
        bundle,
        stage1,
        decision_id,
        sha256(source.read_bytes()).hexdigest(),
    )
    targets = [
        _whole_document_target(
            "stage2-reviewed-coordinate-ledger",
            "config/stage2-coordinate-ledger.json",
            before_ledger,
            after_ledger,
        ),
        _whole_document_target(
            "stage2-reviewed-coordinates",
            "config/stage2-reviewed-coordinates.geojson",
            before_reviewed,
            after_reviewed,
        ),
    ]
    evidence = [
        _retain_local_evidence(root, source),
        _retain_local_evidence(root, bundle_source),
        _retain_local_evidence(root, stage1_source),
    ]
    for evidence_file in evidence_files or []:
        item = _retain_local_evidence(root, evidence_file)
        if item["sha256"] not in {existing["sha256"] for existing in evidence}:
            evidence.append(item)
    source_revisions = [
        f"{item['title']}: {item['url']} — {item['note']}"
        for item in draft["evidence"]
    ]
    decision = {
        "schema_version": 2,
        "kind": "audit_decision",
        "id": decision_id,
        "title": f"{draft['station_name']}: {draft['exit_id']} coordinate",
        "change_type": draft["change_category"],
        "reason": (
            f"{draft['rationale']} Alternatives rejected: "
            f"{draft['alternatives_rejected']}"
        ),
        "targets": targets,
        "evidence": evidence,
        "source_revisions": source_revisions,
        "limitations": [
            "The pinned draft retains source URLs and review notes. Add retained source files "
            "with --evidence-file when the cited source is not durable.",
            "The reviewed-coordinate approved_at value records when the map draft was saved; "
            "the audit ledger records the later human approval time.",
        ],
    }
    decision = _validate_decision(decision, root, "promoted decision")
    return _store_proposal(root, decision, recorded_by=recorded_by, note=note)


def record_decision_action(
    root: Path,
    operation: str,
    decision_id: str,
    *,
    recorded_by: str,
    approved_by: str,
    authorization_basis: str,
    authorization_refs: list[str],
    note: str,
    related_decision_id: str | None = None,
) -> dict[str, object]:
    """Append one explicit human decision to the ledger."""

    if operation not in {"approve", "reject", "revalidate", "replace", "retire"}:
        fail("audit action", f"unsupported operation {operation}")
    audit = _read_audit(root)
    current = audit["state"].get(decision_id)
    if current is None:
        fail("audit action", f"unknown decision {decision_id}")
    for value, name in (
        (recorded_by, "recorded_by"), (approved_by, "approved_by"),
        (authorization_basis, "authorization_basis"), (note, "note"),
    ):
        exact_str(value, name)
    refs = list(string_list(authorization_refs, "authorization_refs", nonempty=True))
    if authorization_basis not in _AUTHORIZATION_BASES:
        fail("authorization_basis", f"unsupported value {authorization_basis}")
    decision_path = current["decision_path"]
    package_file = safe_repo_path(decision_path, root.resolve(), "package path")
    return _append_event(root, {
        "operation": operation, "disposition": _OPERATIONS[operation][1],
        "decision_id": decision_id, "decision_path": decision_path,
        "decision_sha256": content_hash(current["decision"]),
        "package_path": decision_path, "package_sha256": sha256(package_file.read_bytes()).hexdigest(),
        "recorded_by": recorded_by, "approved_by": approved_by,
        "authorization_basis": authorization_basis, "authorization_refs": refs,
        "note": note, "related_decision_id": related_decision_id,
    })


def _reviewed_after_documents(decision: dict) -> tuple[dict, dict]:
    targets = _target_map(decision)
    expected = {
        ("config/stage2-coordinate-ledger.json", ""),
        ("config/stage2-reviewed-coordinates.geojson", ""),
    }
    if set(targets) != expected:
        fail(
            "audit apply",
            "only complete reviewed-coordinate ledger and GeoJSON document targets are supported",
        )
    ledger = targets[("config/stage2-coordinate-ledger.json", "")]["after"]
    reviewed = targets[("config/stage2-reviewed-coordinates.geojson", "")]["after"]
    if type(ledger) is not dict or type(reviewed) is not dict:
        fail("audit apply", "reviewed-coordinate after targets must be JSON documents")
    declared = reviewed.get("metadata", {}).get("ledger_sha256")
    actual = sha256(_json_file_bytes(ledger)).hexdigest()
    if declared != actual:
        fail("audit apply", "reviewed-coordinate ledger file hash does not match metadata")
    from .stage2 import _reviewed_coordinates

    _reviewed_coordinates(reviewed, ledger)
    return ledger, reviewed


def apply_decision(
    root: Path,
    decision_id: str,
    *,
    recorded_by: str,
    note: str,
) -> dict[str, object]:
    """Apply exact approved reviewed-coordinate documents with rollback on failure."""

    root = root.resolve()
    decision_id = exact_str(decision_id, "decision_id")
    recorded_by = exact_str(recorded_by, "recorded_by")
    note = exact_str(note, "note")
    lock_path = Path(tempfile.gettempdir()) / (
        "sg-rail-station-data-config-" + sha256(str(root).encode()).hexdigest()[:16] + ".lock"
    )
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        audit = _read_audit(root)
        current = audit["state"].get(decision_id)
        if current is None:
            fail("audit apply", f"unknown decision {decision_id}")
        if current["status"] != "approved":
            fail("audit apply", f"decision must be approved, got {current['status']}")
        ledger_after, reviewed_after = _reviewed_after_documents(current["decision"])
        after_by_path = {
            "config/stage2-coordinate-ledger.json": ledger_after,
            "config/stage2-reviewed-coordinates.geojson": reviewed_after,
        }
        before_by_path = {
            target["path"]: target["before"] for target in current["decision"]["targets"]
        }
        backups: dict[str, bytes] = {}
        for path, before in before_by_path.items():
            destination = safe_repo_path(path, root, "audit apply target")
            if not destination.is_file() or destination.is_symlink():
                fail("audit apply", f"target is not a regular file: {path}")
            backups[path] = destination.read_bytes()
            if _load(destination) != before:
                fail("audit apply", f"stale before document: {path}")

        written: list[str] = []
        try:
            for path in (
                "config/stage2-coordinate-ledger.json",
                "config/stage2-reviewed-coordinates.geojson",
            ):
                atomic_write_json(root / path, after_by_path[path], overwrite=True)
                written.append(path)
            if sha256((root / "config/stage2-coordinate-ledger.json").read_bytes()).hexdigest() != reviewed_after["metadata"]["ledger_sha256"]:
                fail("audit apply", "written ledger does not match reviewed-coordinate metadata")
            from .stage2 import _reviewed_coordinates

            _reviewed_coordinates(
                _load(root / "config/stage2-reviewed-coordinates.geojson"),
                _load(root / "config/stage2-coordinate-ledger.json"),
            )
            approval = current["event"]
            decision_path = current["decision_path"]
            package_file = safe_repo_path(decision_path, root, "package path")
            return _append_event(root, {
                "operation": "apply", "disposition": "approved",
                "decision_id": decision_id, "decision_path": decision_path,
                "decision_sha256": content_hash(current["decision"]),
                "package_path": decision_path,
                "package_sha256": sha256(package_file.read_bytes()).hexdigest(),
                "recorded_by": recorded_by,
                "approved_by": approval["approved_by"],
                "authorization_basis": approval["authorization_basis"],
                "authorization_refs": approval["authorization_refs"],
                "note": note,
                "related_decision_id": None,
            })
        except BaseException:
            rollback_errors = []
            for path in reversed(written):
                try:
                    atomic_write_bytes(root / path, backups[path], overwrite=True)
                except BaseException as error:
                    rollback_errors.append(f"{path}: {error}")
            if rollback_errors:
                fail("audit apply rollback", "; ".join(rollback_errors))
            raise


def _markdown_text(value: object) -> str:
    return str(value).replace("\n", " ").strip()


def render_signoffs(root: Path) -> dict[str, object]:
    """Generate simple dated Markdown pages from the canonical ledger."""

    audit = _read_audit(root)
    root = root.resolve()
    timezone_sg = ZoneInfo("Asia/Singapore")
    by_date: dict[str, list[dict]] = defaultdict(list)
    for event in audit["events"]:
        stamp = datetime.fromisoformat(event["recorded_at"].replace("Z", "+00:00"))
        by_date[stamp.astimezone(timezone_sg).date().isoformat()].append(event)
    output_dir = root / SIGNOFFS
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    initial_file_hashes = {
        item["path"]: item["sha256"] for item in audit["initial"]["package"]["files"]
    }
    for day, events in sorted(by_date.items()):
        lines = [f"# Sign-offs — {day}", "", "Times use Asia/Singapore.", "", "## Changes", ""]
        for event in events:
            decision = audit["initial"]["decisions"].get(event["decision_id"]) or audit["state"][event["decision_id"]]["decision"]
            reason = decision.get("rationale", decision.get("reason"))
            replacement = (
                f"; replacement `{event['related_decision_id']}` approved atomically"
                if event.get("operation") == "replace"
                else ""
            )
            lines.append(
                f"- {event['event_id']}: {event['operation']} `{event['decision_id']}` — "
                f"{_markdown_text(decision['title'])}{replacement}. {_markdown_text(reason)}"
            )
        lines.extend(["", "## Evidence", ""])
        for event in events:
            decision = audit["initial"]["decisions"].get(event["decision_id"]) or audit["state"][event["decision_id"]]["decision"]
            if "evidence_files" in decision:
                for path in decision["evidence_files"]:
                    lines.append(
                        f"- `{event['decision_id']}`: `{path}` at `{initial_file_hashes[path]}`."
                    )
            else:
                for item in decision["evidence"]:
                    lines.append(
                        f"- `{event['decision_id']}`: {_markdown_text(item['description'])} "
                        f"`{item['path']}` at `{item['sha256']}`."
                    )
                for revision in decision["source_revisions"]:
                    lines.append(f"- `{event['decision_id']}` source revision: {_markdown_text(revision)}.")
        lines.extend(["", "## Human decisions", ""])
        for event in events:
            actor = event["approved_by"] or "No approver"
            refs = ", ".join(event["authorization_refs"]) or "None"
            lines.append(
                f"- {event['event_id']}: `{event['disposition']}`. Actor: {_markdown_text(actor)}. "
                f"Authorization: {_markdown_text(refs)}. {_markdown_text(event['note'])}"
            )
        lines.extend(["", "## Validation", ""])
        for event in events:
            lines.append(
                f"- {event['event_id']}: decision `{event['decision_sha256']}`; "
                f"event `{event['event_hash']}`."
            )
        lines.extend([f"- Ledger head: `{audit['ledger_head']}`.", ""])
        atomic_write_text(output_dir / f"{day}.md", "\n".join(lines), overwrite=True)
        rows.append((day, len(events), sum(event["operation"] == "propose" for event in events)))
    index = [
        "# Sign-offs by date", "", "These pages are generated from `audit/decision-ledger.jsonl`.", "",
        "| Date | Events | Proposal events |", "|---|---:|---:|",
    ]
    for day, count, pending in rows:
        index.append(f"| [{day}]({day}.md) | {count} | {pending} |")
    index.append("")
    atomic_write_text(output_dir / "README.md", "\n".join(index), overwrite=True)
    return {
        "status": "complete", "date_count": len(rows), "event_count": len(audit["events"]),
        "output_directory": str(output_dir),
    }
