# Initial intervention sign-off — 2026-10-05

**Readable ledger:** [browse sign-offs by date](signoffs/README.md), starting with
[5 October 2026](signoffs/2026-10-05.md). Each decision includes its status,
explanation, evidence and affected records.

This is a retrospective inventory of the existing configuration, recorded by Codex
from prior instructions and retained evidence. It is not a fresh human signature,
a new field survey or a Stage 2 release approval. No runtime data was changed.

The initial 25 decision groups cover 354 configured intervention records: 20
Stage 1 rules and 334 coordinate entries (28 reviewed pins, 266 trusted OneMap
points, 38 accepted LTA/OSM points and two agent-selected OneMap conflict
points). D-026 clarifies the Dhoby Ghaut interpretation without adding an
intervention record. All 26 decisions now have recorded approval.

## Current sign-off status

The project owner approved Cantonment MRT Exits 1–4, Keppel MRT Exits 1–3 and
Prince Edward Road MRT Exits 1–2 on 6 October 2026. The owner also confirmed
that Dhoby Ghaut E, F and G are separate exits. The pipeline combines only the
two source mentions of G so that G retains both destination strings. The rule
remains pinned to SgWiki revision 37293.

## Ledger index

| Decision | Sign-off record | Existing intervention | Records | Disposition |
|---|---|---|---:|---|
| D-001 | S-001 | Choa Chu Kang: exit_scope_contracts | 1 | Prior owner authorization |
| D-002 | S-002 | Punggol: exit_scope_contracts | 1 | Prior owner authorization |
| D-003 | S-003 | Sengkang: exit_scope_contracts | 1 | Prior owner authorization |
| D-004 | S-026 | Cantonment: exit_inventory_overrides | 1 | Direct human sign-off |
| D-005 | S-027 | Keppel: exit_inventory_overrides | 1 | Direct human sign-off |
| D-006 | S-028 | Prince Edward Road: exit_inventory_overrides | 1 | Direct human sign-off |
| D-007 | S-007 | Bukit Panjang: bus_exit_overrides | 1 | Prior owner authorization |
| D-008 | S-008 | Woodlands: station_connection_contracts | 1 | Prior owner authorization |
| D-009 | S-009 | Ang Mo Kio: review_acceptances | 1 | Prior owner authorization |
| D-010 | S-010 | Bukit Panjang: review_acceptances | 1 | Prior owner authorization |
| D-011 | S-011 | Choa Chu Kang: review_acceptances | 1 | Prior owner authorization |
| D-012 | S-012 | Clementi: review_acceptances | 1 | Prior owner authorization |
| D-013 | S-029 | Dhoby Ghaut: review_acceptances | 1 | Direct human sign-off |
| D-014 | S-014 | Jurong East: review_acceptances | 1 | Prior owner authorization |
| D-015 | S-015 | Orchard: review_acceptances | 1 | Prior owner authorization |
| D-016 | S-016 | Outram Park: review_acceptances | 1 | Prior owner authorization |
| D-017 | S-017 | Paya Lebar: review_acceptances | 1 | Prior owner authorization |
| D-018 | S-018 | Queenstown: review_acceptances | 1 | Prior owner authorization |
| D-019 | S-019 | Tanah Merah: review_acceptances | 1 | Prior owner authorization |
| D-020 | S-020 | Tanjong Pagar: review_acceptances | 1 | Prior owner authorization |
| D-021 | S-021 | Retain 28 legacy reviewed coordinates and the original 30-event ledger | 28 | Prior owner authorization |
| D-022 | S-022 | Trust 266 historical OneMap selections | 266 | Prior owner authorization |
| D-023 | S-023 | Accept 38 remaining LTA/OSM coordinates | 38 | Prior owner authorization |
| D-024 | S-024 | Select Expo MRT:A OneMap candidate | 1 | Prior delegated selection |
| D-025 | S-025 | Select Farrer Park MRT:E OneMap candidate | 1 | Prior delegated selection |
| D-026 | S-031 | Clarify Dhoby Ghaut repeated Exit G evidence | 0 | Direct human sign-off |

## Evidence and limitations

- [Machine-readable ledger](decision-ledger.jsonl): per-decision disposition, attribution, package hash and event chain.
- [Initial package](initial-package.json): exact rule/feature pointers, identities, record hashes, reasons and evidence references.
- [Authorization excerpts](evidence/session-authorization.json): selected user statements and their scope; original message IDs/timestamps were unavailable.
- Legacy per-pin explanations remain missing: all 30 old edit notes and 28 final review notes were blank. Their original files are preserved byte-for-byte.
- Expo A and Farrer Park E remain agent evidence selections under delegated authority. The app could not be opened; saved source evidence was used.
- Source reports are historical evidence, not newly fetched verification. The revision-adjusted offline replay is not eligible for live baseline approval.

## Validate before committing

```console
uv run mrt-wayfinding audit validate
uv run python -m unittest discover -s tests -v
```

Audit exit 0 means the inventory is valid with no pending groups; 2 means
integrity checks passed but sign-offs remain pending; 1 means invalid or changed
evidence, missing coverage, or a broken ledger. None grants release approval.

The files in `audit/bootstrap/` are immutable copies of the initial evidence.
Their names and content must match the hashes in `initial-package.json`. This
keeps the initial audit verifiable after an approved live configuration change.
Do not edit historical rows, the initial package, or bootstrap copies.

The map saves coordinate changes as drafts. Promote one draft to a strict audit
proposal with this command:

```console
uv run mrt-wayfinding audit promote-coordinate-draft \
  --draft-file audit/drafts/DRAFT-<id>.json \
  --decision-id D-026 \
  --bundle-file <review-bundle.json> \
  --stage1-file <run>/stage1/stations.json \
  --evidence-file audit/evidence/<retained-source> \
  --recorded-by <actor> \
  --note "Prepared for review."
```

The command verifies and retains the draft, review bundle, Stage 1 data, and each
local evidence file. It builds a complete reviewed-coordinate override. The
proposal contains two whole-document targets: the append-only coordinate ledger
and the matching reviewed GeoJSON. It updates the feature, revision, source
guard, ledger head, and ledger file hash together. It validates the proposed
documents with the Stage 2 contracts. It does not edit either frozen baseline.
It appends a `pending_review` event and does not grant approval.

After a human approves the exact decision, apply it:

```console
uv run mrt-wayfinding audit apply D-026 \
  --recorded-by <actor> \
  --note "Applied the approved reviewed-coordinate documents."
```

`apply` accepts only the complete ledger and reviewed GeoJSON target pair. It
rejects pending, rejected, replaced, or retired decisions. It requires both
current files to match the approved before-documents, so a repeated application
fails as stale. It writes the exact after-documents, validates their linked
hashes and Stage 2 contracts,
and records an `apply` event. The decision stays approved so it can be
revalidated, replaced, or retired through the normal lifecycle. If a write or
event append fails, the command restores both original files. Other audit target
types are not supported by this command.

Use `replace` when a new strict decision supersedes an approved strict decision.
The old and new decisions must cover the same targets. Each new before value must
equal the old after value. One replacement event marks the old decision as
replaced and the new decision as approved. Direct approval of overlapping strict
decisions fails.

Commit-summary CI enforcement and release promotion remain planned. A pending
decision may be committed as pending. Do not present it as signed off. No commit
has been made as part of creating this ledger.
