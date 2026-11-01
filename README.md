# sg-rail-station-data

`sg-rail-station-data` builds reviewed station data for Singapore's MRT and LRT
network. It combines operational stations, layouts, exit details, bus
connections, and exit coordinates in one deterministic pipeline.

The pipeline validates every source against
an explicit contract and stops when it finds an unexpected field, invalid value,
or unsupported layout. Incomplete or ambiguous data is kept visible for human
review instead of being accepted silently.

The `main` branch represents the latest approved state. Scheduled runs collect
and compare data, but they do not modify or approve it.

## How the data is built

Stage 1 builds the station and exit records:

- LTA GTFS identifies stations that are active on the selected service date.
- SgWiki supplies exit labels, nearby places, bus links, levels, and platforms.
- LTA BusStops supplies the official name and coordinates for bus stops that
  SgWiki references.

Stage 2 assigns coordinates to the Stage 1 exits. It uses this order:

1. An approved manual override.
2. An exact SgWiki Kartographer marker.
3. An approved manual gap fill.
4. A coordinate from the frozen trusted OneMap baseline.
5. A coordinate from the frozen trusted supplemental baseline.
6. An unresolved record that requires review.

Each coordinate keeps its original provenance. The pipeline does not refresh a
frozen source or replace approved data with a new search result automatically.

The repository also contains an approved network-geometry comparison baseline
and an unapproved preview builder. They combine LTA GTFS service topology with
OSM physical rail ways, but they are not part of the Stage 1/2 release pipeline
or the scheduled workflow. Baseline approval does not authorize a production
Stage 3 release.

## Station-code ordering contract

`config/stations.json` stores station codes in system-map presentation order,
not lexical order. Repository-owned, consumer-facing Stage 1 and Stage 2 station
records preserve that order, so applications may render their `codes` arrays
directly. The current order is NSL, EWL, NEL, CCL, DTL, TEL, future MRT lines,
then LRT systems. Branch codes such as CG and CE inherit their parent line's
position.

Source evidence may retain a source-specific deterministic order. In particular,
the normalized LTA GTFS active inventory keeps lexically canonical codes and is
not a presentation-ready station catalogue. The network preview therefore emits
both source-ordered `codes` and presentation-ready `display_codes`.

An unsupported station-code prefix is a contract failure. Add its explicit line
mapping and reviewed display position before using it in the catalogue or a
consumer-facing artifact.

## Set up the project

The project requires Python 3.12 and [`uv`](https://docs.astral.sh/uv/).

```console
uv sync --frozen
cp .env.example .env
```

Add the LTA DataMall key to `.env` as `LTA_API_KEY`, then run the initial checks:

```console
uv run sg-rail-station-data config validate
uv run sg-rail-station-data credentials validate
uv run python -m unittest discover -s tests -v
```

The credential loader does not print or store the API key in run artifacts.

## Run the pipeline

Run a live collection with today's Singapore service date:

```console
uv run sg-rail-station-data pipeline run
```

Use a fixed date when you need a reproducible run:

```console
uv run sg-rail-station-data pipeline run --service-date 2026-10-05
```

You can also replay the source snapshots from an earlier run without making
network requests:

```console
uv run sg-rail-station-data pipeline run \
  --service-date 2026-10-05 \
  --replay-sources runs/<run-id>
```

The repository contains approved Stage 1 and Stage 2 baselines. To compare with
a different approved Stage 2 dataset, provide its `stations.json` file:

```console
uv run sg-rail-station-data pipeline run \
  --stage2-baseline runs/<approved-run>/stage2/stations.json
```

Without an override, the pipeline uses the checked-in Stage 1 and Stage 2
approved baselines. The separate geometry preview uses its checked-in comparison
baseline.

## Understand a run

Every invocation creates a new `runs/<run-id>/` directory. Existing runs are
never resumed or overwritten.

Start with these files:

1. `manifest.json` gives the overall status and artifact hashes.
2. `stage1/changes.json` and `stage2/changes.json` show what changed.
3. The two `reviews.json` files contain warnings and errors.
4. `stage2/coverage.json` lists the coordinate coverage and unresolved exits.
5. `sources/` contains the evidence used for the run.
6. `pipeline.jsonl` contains the technical event log.

The run directory has this main structure:

```text
runs/<run-id>/
  sources/                 Source snapshots
  stage1/stations.json     Station, exit, bus, level, and platform data
  stage1/changes.json      Stage 1 changes
  stage1/reviews.json      Stage 1 review items
  stage2/stations.json     Exit coordinates and provenance
  stage2/exits.geojson     Exit coordinates in GeoJSON
  stage2/changes.json      Stage 2 changes
  stage2/reviews.json      Stage 2 review items
  stage2/coverage.json     Coordinate coverage
  manifest.json            Run status and artifact hashes
  pipeline.jsonl           Machine-readable event log
```

The command uses three exit codes:

| Code | Status | Meaning |
|---:|---|---|
| 0 | `complete` | The run has no outstanding review item. |
| 2 | `review_required` | The artifacts are valid, but a person must review them. |
| 1 | `failed` | A source contract, invariant, or data check failed. |

A failed collection is not evidence that a station or exit was removed. The
last approved release remains in service until a later run is reviewed.

## Preview rail-line geometry

Build a temporary, unapproved network-geometry candidate from a retained LTA
GTFS collection and current OpenStreetMap route relations:

```console
uv run sg-rail-station-data network build-preview \
  --gtfs-directory runs/<run-id>/sources/lta-gtfs \
  --output-dir /tmp/sg-rail-network-preview

uv run python -m http.server 8765 \
  --bind 127.0.0.1 \
  --directory /tmp/sg-rail-network-preview
```

Open `http://127.0.0.1:8765`. The page shows OSM physical rail ways selected
for lines active in the LTA GTFS service snapshot. Enable **Straight GTFS
chords** to compare the curved geometry with direct station-to-station lines.

This command is a source-review prototype, not Stage 3 release production. Its
manifest always sets `publication_allowed` to `false`. It retains each OSM
relation response, source hash, version and timestamp, compares the candidate
with `config/stage3-approved-geometry-baseline.geojson`, and writes
`changes.json`. It does not yet prove connectivity between every consecutive
station. The quarterly workflow does not run or publish it. See the
[maintenance plan](docs/maintenance-plan.md#network-geometry-preview) for the
checks required before promotion.

## Inspect coordinates on the map

Build a review bundle from one candidate run and the last approved Stage 2
release:

```console
uv run sg-rail-station-data review build \
  --run-dir runs/<candidate-run> \
  --approved-stage2 config/stage2-approved-baseline.json \
  --output /tmp/sg-rail-station-review.json

mkdir -p audit/drafts
uv run sg-rail-station-data review serve \
  --bundle /tmp/sg-rail-station-review.json \
  --drafts-dir audit/drafts
```

Open `http://127.0.0.1:8000`. The default view shows all approved exits as
yellow labelled pins. Kartographer, OneMap, OpenStreetMap, LTA, and manual
candidates stay hidden until you enable their source. The neutral background
uses OpenStreetMap tiles for context only.

The alternate filters also use the frozen comparison artifact at
`audit/evidence/coordinate-candidates-v1.geojson`. These points are review
evidence only. They cannot resolve an exit or become approved automatically.

Select an exit to inspect it. Click the map or select a visible candidate to
start a proposal. A saved proposal creates a new immutable file in
`audit/drafts/`. It does not edit configuration, approve a coordinate, or write
to the sign-off ledger.

You can omit `--approved-stage2` to inspect a run before an approved baseline
exists. The app then shows a warning and does not show any point as approved.

## Review and approve changes

A pipeline run only reports changes, warnings, and errors. It does not make a
commit, approve a decision, or update the approved baseline.

The normal review flow is:

1. Pull the latest `main` branch.
2. Download the run artifact from GitHub Actions.
3. Create a branch for the review.
4. Replay the saved sources locally.
5. Inspect each change and its supporting evidence.
6. Record a decision for every human intervention.
7. Add a sign-off event for every approved decision.
8. Apply each approved coordinate decision.
9. Replay the pipeline with the proposed changes.
10. Open a pull request.
11. Merge after CI and human review are complete.

The pull request should include the data changes, evidence, decision records,
sign-offs, updated source guards, and validation results. It must also list each
station, exit, and intervention that changed.

CI should confirm that:

- Every semantic change has a decision.
- Every active intervention has a valid sign-off.
- Evidence and decision hashes match.
- The declared changes match the actual changes.
- The saved sources produce the proposed output.
- No blocking error or unresolved exit remains.

A scheduled workflow must never approve or merge its own result. See the
[maintenance plan](docs/maintenance-plan.md) for the complete proposed system.

## Change logs and pull requests

Keep change logs short and practical. Put the changes and evidence first, then
record the human decisions and validation results. Use these sections:

1. `Changes`
2. `Evidence`
3. `Human decisions`
4. `Validation`

List only data that changed. Use `None` for an empty category. Always include the
exact station or exit identity, and include the decision and sign-off IDs for a
human intervention.

The documentation follows useful ASD-STE100 principles such as clear terms,
active voice, and concise sentences. Natural phrasing is preferred when strict
controlled language would make the text awkward.

See the [change-log format](docs/change-log-format.md) and the repository pull
request template for the required fields.

## Audit records

The audit files serve different purposes:

| File | Purpose |
|---|---|
| `audit/initial-package.json` | Defines the initial decisions, targets, evidence, and hashes. |
| `audit/decision-ledger.jsonl` | Stores the canonical sign-off event history. |
| `audit/signoffs/` | Provides readable sign-off pages by date. |
| `audit/README.md` | Summarizes the current audit state. |

The JSONL ledger is append-only. Do not edit an old event when a decision changes.
Add a new event to approve, reject, revalidate, replace, or retire that decision.

Validate the current audit with:

```console
uv run sg-rail-station-data audit validate
```

Exit code 2 means that the records are valid but still contain pending sign-offs.
The current audit has no pending decisions. Audit validity does not approve a
data release.

Use `audit status` to inspect effective decision states. Convert a saved map
draft with `audit promote-coordinate-draft`, or submit a strict decision with
`audit propose`. Use `approve`, `reject`, `revalidate`, `replace`, and `retire`
to append later lifecycle events. Run `audit render` after an event to update
the readable sign-off pages. `audit apply` writes an approved coordinate change
to the reviewed override and its coordinate ledger. Each command validates the
complete ledger before it writes. See [the audit runbook](audit/README.md) for
the exact commands.

## Source guards

Reviewed rules and hardcoded coordinates are tied to the relevant source page,
revision, station, and exit identity. A source change can invalidate that guard.

Do not change a revision number only to make a run pass. Review the source diff
first. If the relevant data did not change, record a revalidation decision. If
the data changed, record a new decision and its evidence.

Unknown fields, invalid types, duplicate identities, and broken hashes stop the
pipeline instead of producing a degraded result.

## Automation

GitHub Actions runs the pipeline at 02:00 UTC on the first day of January, April,
July, and October. The workflow can also be started manually.

Set the repository secret `LTA_API_KEY` before the first live run. GitHub retains
the run artifact for 90 days. Evidence for an active decision must also exist in
a durable location that outlives the workflow artifact.
