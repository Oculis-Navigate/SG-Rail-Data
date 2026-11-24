# Intervention and baseline sign-off

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
intervention record. D-027 approves the checked-in Stage 2 baseline. D-028
approves the checked-in rail geometry as a comparison baseline only. Of 52 decisions, 43 are currently approved, four are retired and five are replaced.

## Current sign-off status

The project owner approved Cantonment MRT Exits 1–4, Keppel MRT Exits 1–3 and
Prince Edward Road MRT Exits 1–2 on 6 October 2026. The owner also confirmed
that Dhoby Ghaut E, F and G are separate exits. The pipeline combines only the
two source mentions of G so that G retains both destination strings. The rule
is now pinned to SgWiki revision 41955. The owner also approved the complete
184-station, 614-exit Stage 2 comparison baseline. The baseline resolves all
614 exits and has no review finding. On 25 October 2026, the owner approved the
283-way rail geometry reviewed in the temporary map as the initial comparison
baseline. That approval does not authorize a Stage 3 production release.

## Ledger index

| Decision | Sign-off record | Existing intervention | Records | Disposition |
|---|---|---|---:|---|
| D-001 | S-001 | Choa Chu Kang: exit_scope_contracts | 1 | Prior owner authorization |
| D-002 | S-002 | Punggol: exit_scope_contracts | 1 | Prior owner authorization |
| D-003 | S-003 | Sengkang: exit_scope_contracts | 1 | Prior owner authorization |
| D-004 | S-026 → S-040 | Cantonment: exit_inventory_overrides | 1 | Retired: revision 41849 lists MRT Exits 1–4 |
| D-005 | S-027 → S-041 | Keppel: exit_inventory_overrides | 1 | Retired: revision 41852 lists MRT Exits 1–3 |
| D-006 | S-028 → S-042 | Prince Edward Road: exit_inventory_overrides | 1 | Retired: revision 41853 lists MRT Exits 1–2 |
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
| D-027 | S-033 | Approve the initial Stage 2 coordinate baseline | 1 baseline | Direct human sign-off |
| D-028 | S-035 / S-079 | Approve the geometry comparison baseline; revalidate all [184 catalog stations and 186 mapped GTFS points](evidence/2026-11-24-stage3-station-baseline-verification.json) with the November inputs | 1 baseline | Direct human sign-off |
| D-029 | S-037 | Migrate station-code display order | 1 migration | Direct human sign-off |
| D-030 | S-039 | Ang Mo Kio MRT:D closed at revision 39808; no stated alternative | 1 revised rule | Direct human sign-off |
| D-031 | S-044 | Choa Chu Kang MRT:D remains closed at revision 39978; removed advice to use A/C/E | 1 revised rule | Direct human sign-off |
| D-032 | S-046 | Dhoby Ghaut: renew D-026 interpretation for revision 41817; replaced by D-048 | 1 revised guard | Direct human sign-off |
| D-048 | S-081 | Dhoby Ghaut: renew Stage 1 finding acceptance only at revision 41955 | 1 revised guard | Direct human sign-off |
| D-049 | S-083 | Dhoby Ghaut: keep OneMap coordinates A–G and renew their Stage 2 revision guards to 41955 | 7 guards | Direct human sign-off |
| D-050 | S-085 | Replace D-040: 24 November Stage 1 comparison baseline, including four new rail diagrams | 184 stations | Direct human sign-off |
| D-051 | S-087 | Replace D-046: 24 November Stage 2 comparison baseline, source revisions only | 614 unchanged exit positions | Direct human sign-off |
| D-052 | S-089 | Authorize publication of the exact physical-track GeoJSON for the 24 November inventory; not a timetable or a GitHub deployment | 283 ways | Direct human sign-off |
| D-033 | S-048 | Tanjong Pagar MRT:D: include nearby landmarks from revision 41616; Exit E review remains open | 1 exit | Direct human sign-off |
| D-034 | S-050 | Renew Paya Lebar C/D heading and Woodlands station connection to stop 46009 | 2 revised guards | Direct human sign-off |
| D-035 | S-052 | Show exit closures without diversions; accept Tanjong Pagar E closure at revision 41616 | Global display policy, 1 revised guard | Direct human sign-off |
| D-036 | S-054 | Renew Ang Mo Kio MRT:D closure only; replaced by D-037 | 1 revised guard | Direct human sign-off |
| D-039 | S-060 | Accept Cantonment, Keppel and Prince Edward Road as regular catalog entries | 3 stations | Direct human sign-off |
| D-040 | S-062 | Promote the reviewed Stage 1 comparison baseline; no Stage 2 coordinate approval | 184 stations, 614 exits | Direct human sign-off |
| D-041 | S-064 | Accept exact Stage 2 candidate moves, including disclosed City Hall overlap; no baseline promotion | 13 exits | Direct human sign-off |
| D-042 | S-066 | Renew Bayfront A at the unchanged OneMap point | 1 exit | Direct human sign-off |
| D-043 | S-068 / S-069 | Select and apply the retained manual candidate for Kent Ridge E | 1 exit | Direct human sign-off |
| D-044 | S-071 | Renew 79 OneMap and 5 supplemental guards; disregard only the exact City Hall E/F/G unmatched markers at revision 41804 | 84 guards, 3 markers | Direct human sign-off |
| D-045 | S-073 | Retire superseded OneMap fallbacks for the 13 accepted map positions | 13 exits | Direct human sign-off |
| D-046 | S-076 | Promote verified Stage 2 baseline after retiring D-027 at S-075 | 614 exits | Direct human sign-off |
| D-047 | S-078 | Keep D-028 geometry for the November GTFS physical-continuity comparison only | 1 exact DTL way override, 427 pairs checked | Direct human sign-off |
| D-038 | S-058 | Tanjong Pagar MRT:D: display Skywaters Residences and MAS Building; retain raw POI cell | 1 exit | Direct human sign-off |
| D-037 | S-056 | Ang Mo Kio: retain Exit D closure and accept L2 NSL platform group C/D at revision 41909 | 1 revised rule | Direct human sign-off |

## Evidence and limitations

- [Machine-readable ledger](decision-ledger.jsonl): per-decision disposition, attribution, package hash and event chain.
- [Initial package](initial-package.json): exact rule/feature pointers, identities, record hashes, reasons and evidence references.
- [Authorization excerpts](evidence/session-authorization.json): selected user statements and their scope; original message IDs/timestamps were unavailable.
- Legacy per-pin explanations remain missing: all 30 old edit notes and 28 final review notes were blank. Their original files are preserved byte-for-byte.
- Expo A and Farrer Park E remain agent evidence selections under delegated authority. The app could not be opened; saved source evidence was used.
- Source reports are historical evidence, not newly fetched verification. The revision-adjusted offline replay is not eligible for live baseline approval.
- D-027 used saved source snapshots. It approves the comparison baseline and does not claim survey-grade coordinate precision.
- D-028 binds the geometry baseline to LTA GTFS service date 2026-10-05 and the reviewed OSM state. The retained evidence discloses one OSM node edit after review and preserves the earlier reviewed node version. Approval is for future comparison only; connectivity, service-pattern matching and production release remain pending.

## Validate before committing

```console
uv run sg-rail-station-data audit validate
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
uv run sg-rail-station-data audit promote-coordinate-draft \
  --draft-file audit/drafts/DRAFT-<id>.json \
  --decision-id D-<next> \
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
uv run sg-rail-station-data audit apply D-<approved> \
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

Local commit-summary checks and GitHub release promotion are documented in the [dataset release runbook](../docs/dataset-releases.md). A pending
decision may be committed as pending. Do not present it as signed off.
