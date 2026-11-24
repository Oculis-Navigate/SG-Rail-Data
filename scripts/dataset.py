"""Prepare, review and verify dataset candidates. Run from the repository root."""

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from datetime import date
from hashlib import sha256
from pathlib import Path

from sg_rail_station_data.audit import audit_status

BASELINES = [f"config/stage{i}-approved-baseline.json" for i in (1, 2)]
PRODUCTS = ["stage1/stations.json", "stage2/stations.json", "stage2/exits.geojson"]
SOURCES = [
    "lta-gtfs/active-stations.json",
    "lta-bus-stops/snapshot.json",
    "sgwiki/snapshot.json",
    "sgwiki-mapdata/snapshot.json",
]
GEOMETRY_BASE = "config/stage3-approved-geometry-baseline.geojson"
GEOMETRY_RELEASE = "config/stage3-release.json"
GEOMETRY_PRODUCT = "stage3/rail-lines.geojson"
RULES = [
    GEOMETRY_BASE, GEOMETRY_RELEASE,
    *BASELINES,
    "config/stations.json",
    "config/stage1-reviewed-decisions.json",
    "config/stage2-coordinate-ledger.json",
    "config/stage2-reviewed-coordinates.geojson",
    "config/stage2-trusted-onemap-baseline.geojson",
    "config/stage2-trusted-supplemental-baseline.geojson",
]
LEDGER = "audit/decision-ledger.jsonl"


def require(ok, message):
    if not ok:
        raise ValueError(message)


def git(*args):
    return subprocess.check_output(["git", *args])


def version(value):
    require(re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.[1-9]\d*", value), "Use YYYY.MM.DD.N")
    date.fromisoformat(value[:10].replace(".", "-"))
    return tuple(map(int, value.split(".")))


def write(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def load(path):
    return json.loads(Path(path).read_bytes())


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def geometry_bytes(baseline, release):
    """Package physical ways, never a review UI, GTFS timetable or preview flags."""
    from sg_rail_station_data.network_geometry import validate_geometry_baseline
    validate_geometry_baseline(baseline)
    data = {"type": "FeatureCollection", "metadata": {
        "kind": "released_rail_track_geometry", "publication_allowed": True,
        "scope": "Physical track geometry only; not a directional service or routing dataset.",
        "service_date": release["service_date"], "decision_id": release["decision_id"],
        "osm_snapshot_timestamp": baseline["metadata"]["osm_snapshot_timestamp"],
        "attribution": "© OpenStreetMap contributors, https://www.openstreetmap.org/copyright",
        "license": "Open Database License 1.0, https://opendatacommons.org/licenses/odbl/1-0/",
        "baseline_sha256": release["baseline_sha256"],
    }, "features": baseline["features"]}
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def approved_geometry(source, service_date, status):
    release = load(GEOMETRY_RELEASE)
    did = release.get("decision_id")
    require(release.get("publication_allowed") is True and did in status["effective_states"]["approved"],
            "Stage 3 publication requires an effective exact-artifact approval")
    targets = load(f"audit/decisions/{did}.json")["targets"]
    require(any(t["path"] == GEOMETRY_RELEASE and t["pointer"] == "" and t["after"] == release for t in targets),
            "Stage 3 release approval differs from manifest")
    require(service_date == release["service_date"] and digest(source / SOURCES[0]) == release["inventory_sha256"],
            "Stage 3 approval does not cover this GTFS inventory/service date")
    require(digest(GEOMETRY_BASE) == release["baseline_sha256"], "Stage 3 geometry baseline changed")
    raw = geometry_bytes(load(GEOMETRY_BASE), release)
    require(sha256(raw).hexdigest() == release["asset_sha256"], "Stage 3 asset hash mismatch")
    return raw


def prior(base):
    names = (
        git("ls-tree", "--name-only", base + ":changesets").decode().splitlines()
        if "changesets" in git("ls-tree", "--name-only", base).decode().splitlines()
        else []
    )
    return max(names, key=version, default=None)


def changes(base):
    """Cover all station fields (including provenance), keyed by stable identity."""
    result = []
    for stage, path in enumerate(BASELINES, 1):
        before = json.loads(git("show", f"{base}:{path}"))
        after = load(path)

        def add(label, a, b, stage=stage):
            fields = sorted(
                k
                for k in a.keys() | b.keys()
                if a.get(k) != b.get(k) or (k in a) != (k in b)
            )
            if fields:
                result.append(f"Stage {stage} {label}: " + ", ".join(fields))

        def rows(label, old, new, stage=stage):
            for key in sorted(old.keys() | new.keys()):
                name = f"{label}/{key}"
                if key not in old or key not in new:
                    result.append(
                        f"Stage {stage} {name}: "
                        + ("added" if key in new else "removed")
                    )
                else:
                    a, b = dict(old[key]), dict(new[key])
                    if "exits" in a:
                        rows(name, indexed(a.pop("exits")), indexed(b.pop("exits")))
                    add(name, a, b)

        def indexed(items):
            return {item["id"]: item for item in items}

        rows(
            "stations", indexed(before.pop("stations")), indexed(after.pop("stations"))
        )
        add("dataset metadata", before, after)
    if Path(GEOMETRY_RELEASE).exists():
        previous = (json.loads(git('show', f'{base}:{GEOMETRY_RELEASE}'))
                    if git('ls-tree', base, '--', GEOMETRY_RELEASE).strip() else None)
        if load(GEOMETRY_RELEASE) != previous:
            result.append('Stage 3 physical geometry publication approval and asset hash')
    return result


def findings(run):
    rows = []
    for stage in ("stage1", "stage2"):
        for row in load(run / stage / "changes.json")["items"]:
            rows.append({"stage": stage, "finding": row})
        for group in load(run / stage / "reviews.json")["groups"]:
            for row in group["findings" if stage == "stage1" else "items"]:
                rows.append({"stage": stage, "reason": group["reason"], "finding": row})
    return rows


def review_keys(base, rows):
    return changes(base) + [
        f"Finding {i}: {row['stage']} {row.get('reason') or row['finding']['reason']}"
        for i, row in enumerate(rows, 1)
    ]


def prepare(v, run, base, collection):
    root = Path("changesets") / v
    manifest = load(run / "manifest.json")
    require(
        manifest["status"] == "complete" and manifest["data_release_created"] is True,
        "Need a completed final replay after review",
    )
    for baseline, product in zip(BASELINES, PRODUCTS):
        require(
            digest(baseline) == digest(run / product),
            f"{product} differs from reviewed baseline",
        )
    source = run / "sources"
    audit = audit_status(Path.cwd())
    require(audit["status"] == "valid", "Pending audit decision")
    base = git("rev-parse", "--verify", base + "^{commit}").decode().strip()
    previous = prior(base)
    require(not previous or version(v) > version(previous), "Version must increase")
    live = load(collection / "manifest.json")
    require(
        live["configuration"]["source_mode"] == "live"
        and live["stages"]["collection"]["status"] == "complete",
        "Need a complete live collection",
    )
    require(
        live["configuration"]["service_date"]
        == manifest["configuration"]["service_date"]
        and all(
            digest(collection / "sources" / n) == digest(source / n) for n in SOURCES
        ),
        "Final run must replay the reviewed live inputs",
    )
    geometry = approved_geometry(source, manifest["configuration"]["service_date"], audit)
    rows = findings(collection)
    root.mkdir(parents=True, exist_ok=False)
    write(root / "findings.json", rows)
    for name in SOURCES:
        dest = root / "sources" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, dest)
    write(
        root / "candidate.json",
        {
            "version": v,
            "previous": previous,
            "base": base,
            "run_id": manifest["run_id"],
            "service_date": manifest["configuration"]["service_date"],
            "ledger_head": audit["ledger_head"],
            "collection_run": live["run_id"],
            "findings_sha256": digest(root / "findings.json"),
            "sources": {n: digest(source / n) for n in SOURCES},
            "products": {**{n: digest(run / n) for n in PRODUCTS}, GEOMETRY_PRODUCT: sha256(geometry).hexdigest()},
        },
    )
    write(
        root / "review.json",
        {
            "reviewer": "",
            "summary": "",
            "evidence": "",
            "signoffs": [],
            "explanations": {key: "" for key in review_keys(base, rows)},
        },
    )
    print(f"Complete {root}/review.json, then run notes {v}.")


def notes(root, m):
    r = load(root / "review.json")

    def prose(text):
        require(
            isinstance(text, str)
            and text.strip()
            and not re.search(r"\b(TODO|TBD)\b", text, re.IGNORECASE),
            "Missing review text or placeholder",
        )
        return text.strip()

    require(
        digest(root / "findings.json") == m["findings_sha256"],
        "Original findings changed",
    )
    keys = review_keys(m["base"], load(root / "findings.json"))
    require(
        set(r["explanations"]) == set(keys),
        "Review identities differ from baseline diff",
    )
    old = git("show", m["base"] + ":" + LEDGER)
    ledger = Path(LEDGER).read_bytes()
    require(ledger.startswith(old), "Ledger history changed")
    events = [json.loads(line) for line in ledger[len(old) :].splitlines()]
    signoffs = [
        e["event_id"]
        for e in events
        if e["disposition"] in ("approved", "retired", "replaced")
    ]
    require(r["signoffs"] == signoffs, f"Expected new sign-offs: {signoffs}")
    human_events = {
        e["event_id"]
        for e in events
        if e.get("operation") in ("approve", "revalidate", "replace", "retire")
    }
    if keys or any(
        Path(p).read_bytes() != git("show", m["base"] + ":" + p) for p in RULES
    ):
        require(
            human_events,
            "Changed inputs require a new audit sign-off, not just application of an old decision",
        )
    for key in keys:
        reason = prose(r["explanations"][key])
        require(
            set(re.findall(r"\bS-\d+\b", reason)) & human_events,
            f"{key}: cite the applicable new sign-off in the explanation",
        )
    evidence = prose(r["evidence"])
    decisions = [
        f"{e['event_id']} / {e['decision_id']}: {e['note']}"
        for e in events
        if e["event_id"] in signoffs
    ]
    return "\n".join(
        [
            f"# Dataset {m['version']}",
            "",
            prose(r["summary"]),
            "",
            "## Changes",
            *(
                [f"- {key} — {prose(r['explanations'][key])}" for key in keys]
                or ["No baseline data changed."]
            ),
            "",
            "## Evidence",
            evidence,
            "",
            "## Human decisions",
            f"Reviewed by: {prose(r['reviewer'])}",
            *(decisions or ["No new intervention sign-offs."]),
            "",
            "## Validation",
            f"Run: {m['run_id']}; service date: {m['service_date']}; prior version: {m['previous'] or 'initial baseline'}.",
            "",
        ]
    )


def verify(v, out=None):
    root = Path("changesets") / v
    m = load(root / "candidate.json")
    require(
        m["version"] == v and re.fullmatch("[0-9a-f]{40}", m["base"]),
        "Invalid candidate identity",
    )
    require(
        prior(m["base"]) == m["previous"]
        and (not m["previous"] or version(v) > version(m["previous"])),
        "Invalid predecessor",
    )
    require(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", m["run_id"]), "Invalid run ID"
    )
    status = audit_status(Path.cwd())
    require(
        status["status"] == "valid" and status["ledger_head"] == m["ledger_head"],
        "Audit head mismatch or pending decisions",
    )
    expected = {
        "candidate.json",
        "review.json",
        "findings.json",
        "changes.md",
        *("sources/" + s for s in SOURCES),
    }
    require(
        not root.is_symlink()
        and not any(p.is_symlink() for p in root.rglob("*"))
        and {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        == expected,
        "Unexpected candidate files; include only the four normalized snapshots and review records",
    )
    require(
        (root / "changes.md").read_text() == notes(root, m),
        "Run notes again; published notes must match review",
    )
    require(
        {n: digest(root / "sources" / n) for n in SOURCES} == m["sources"],
        "Source digest mismatch",
    )
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [
                "sg-rail-station-data",
                "pipeline",
                "run",
                "--replay-sources",
                str(root / "sources"),
                "--service-date",
                m["service_date"],
                "--runs-dir",
                tmp,
                "--run-id",
                "verify",
            ],
            check=True,
        )
        run = Path(tmp) / "verify"
        geometry = approved_geometry(root / 'sources', m['service_date'], status)
        (run / 'stage3').mkdir(exist_ok=True)
        (run / GEOMETRY_PRODUCT).write_bytes(geometry)
        products = [*PRODUCTS, GEOMETRY_PRODUCT]
        require(
            {n: digest(run / n) for n in products} == m["products"],
            "Replay product mismatch",
        )
        for p, n in zip(BASELINES, PRODUCTS):
            require(digest(p) == digest(run / n), "Baseline differs from replay")
        if out:
            out.mkdir(exist_ok=False)
            for n in products:
                shutil.copyfile(run / n, out / n.replace("/", "-"))
            write(
                out / "manifest.json",
                {
                    **m,
                    "release_commit": git("rev-parse", "HEAD").decode().strip(),
                    "review_sha256": digest(root / "review.json"),
                    "notes_sha256": digest(root / "changes.md"),
                    "asset_sha256": {
                        n.replace("/", "-"): digest(run / n) for n in products
                    },
                },
            )
            shutil.copyfile(root / "changes.md", out / "changes.md")


def gate(base, staged):
    args = [
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        *(["--cached"] if staged else []),
        base,
    ]
    paths = set(git(*args).decode().strip("\0").split("\0"))
    if LEDGER in paths:
        current = git("show", ":" + LEDGER) if staged else Path(LEDGER).read_bytes()
        require(
            current.startswith(git("show", base + ":" + LEDGER)),
            "Audit history is append-only",
        )
    candidates = {p.split("/")[1] for p in paths if p.startswith("changesets/")}
    if not candidates and not paths.intersection(RULES):
        return
    if staged:
        # Never validate unstaged bytes instead of the snapshot about to be committed.
        require(
            not git(
                "diff",
                "--name-only",
                "--",
                "changesets",
                "config",
                "audit",
                "src",
                "scripts",
            )
            and not git(
                "ls-files",
                "--others",
                "--exclude-standard",
                "--",
                "changesets",
                "config",
                "audit",
                "src",
                "scripts",
            ),
            "Stage review inputs and code before validating",
        )
    require(
        len(candidates) == 1, "Data changes need exactly one new reviewed changeset"
    )
    v = candidates.pop()
    version(v)
    m = load(Path("changesets") / v / "candidate.json")
    require(
        git("ls-tree", base, "--", f"changesets/{v}") == b"",
        "Historical changesets are immutable",
    )
    ancestor = m["base"]
    require(re.fullmatch("[0-9a-f]{40}", ancestor), "Invalid review base")
    subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, base], check=True)
    require(
        not git(
            "diff", "--name-only", ancestor, base, "--", "changesets", "config", "audit"
        ),
        "Review base is stale",
    )
    verify(v)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for cmd in ("prepare", "notes", "verify"):
        p = sub.add_parser(cmd)
        p.add_argument("version")
        if cmd == "prepare":
            p.add_argument("--collection", type=Path, required=True)
            p.add_argument("--run-dir", type=Path, required=True)
            p.add_argument("--base", default="HEAD")
        if cmd == "verify":
            p.add_argument("--out", type=Path)
    p = sub.add_parser("gate")
    p.add_argument("base")
    p.add_argument("--staged", action="store_true")
    args = parser.parse_args()
    if args.cmd == "gate":
        return gate(args.base, args.staged)
    v = args.version
    version(v)
    if args.cmd == "prepare":
        return prepare(v, args.run_dir, args.base, args.collection)
    root = Path("changesets") / v
    if args.cmd == "notes":
        (root / "changes.md").write_text(notes(root, load(root / "candidate.json")))
    else:
        verify(v, args.out)


if __name__ == "__main__":
    main()
