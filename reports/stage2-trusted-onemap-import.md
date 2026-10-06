# Trusted OneMap baseline import

Subsequent update: the 40 unresolved exits described below have now been imported
separately. See [the supplemental import](stage2-supplemental-import.md).
The counts below describe this earlier OneMap-only import.

The project owner authorized the remaining unreviewed OneMap selections from
`mrt-data-v2` as trusted hardcoded coordinates, while requiring their provenance
to remain distinct from manually reviewed pins.

The import contains **266 coordinates across 70 stations** in
[`stage2-trusted-onemap-baseline.geojson`](../config/stage2-trusted-onemap-baseline.geojson).
All match the old selected coordinates exactly. Ten of the original 276 OneMap
selections were excluded because human-reviewed positions already supersede
them. All 28 human-reviewed positions remain identical to the old approved SSOT;
their ledger is unchanged.

This is a fixed, owner-authorized import. It does not label the entries as
individually inspected or manually positioned, and does not authorize future
OneMap results automatically. No runtime dependency on `mrt-data-v2` is needed.

Each feature retains its current station/exit identity, original exit identity,
OneMap candidate ID, building name, query scope, original selection status,
response-file paths, fetch timestamps, hashes and SgWiki page/revision guard.
The file metadata retains legacy source-artifact paths and SHA-256 hashes.
Output provenance uses `kind: trusted_onemap_baseline`; dragged/reviewed pins
continue to use `kind: human_reviewed`.

Resolution order remains reviewed override, Kartographer, reviewed gap fill,
then trusted OneMap baseline. A subsequent human review supersedes the imported
entry. A changed page/revision guard on an applicable imported entry is an
error; the stale coordinate is not applied. A new Kartographer marker takes
precedence and raises a review warning. Unknown fields, wrong types, duplicate
identities and missing provenance fail validation.

## Offline replay verification

| Source | Resolved exits |
|---|---:|
| SgWiki Kartographer | 280 |
| Human-reviewed pins | 28 |
| Trusted OneMap baseline | 266 |
| Total resolved | **574 / 614** |
| Unresolved | **40 across 23 stations** |

Stage 1 has zero review findings. Stage 2 groups the 40 missing coordinates as
warnings, and the initial comparison still reports that no approved Stage 2
comparison baseline has been supplied. The run returns exit code 2 and does not
create a release.

This is an offline implementation replay of saved source evidence, not a fresh
network result. The temporary replay fixture adjusts only the Woodlands North
response revision (33908 to 38586) to match the newer saved page, retaining its
unchanged overlay ID. That adjusted response is test evidence only; a live run
must retrieve and validate the actual requested revision. No synthetic overlay
was added to repository baselines.

## Remaining exits

| Station | Unresolved exit IDs |
|---|---|
| Bangkit | LRT:A |
| Bugis | MRT:E, MRT:F, MRT:G |
| Coral Edge | LRT:A, LRT:B |
| Cove | LRT:A, LRT:B |
| Damai | LRT:A, LRT:B |
| Expo | MRT:A |
| Fajar | LRT:A |
| Farrer Park | MRT:E |
| Kadaloor | LRT:A, LRT:B |
| Keat Hong | LRT:A |
| Nibong | LRT:A, LRT:B |
| Oasis | LRT:A, LRT:B |
| Petir | LRT:A |
| Punggol Point | LRT:A, LRT:B |
| Raffles Place | MRT:K, MRT:L, MRT:M |
| Riviera | LRT:A, LRT:B |
| Samudera | LRT:A, LRT:B |
| Segar | LRT:A |
| Soo Teck | LRT:A, LRT:B |
| South View | LRT:A |
| Sumang | LRT:A, LRT:B |
| Tanjong Pagar | MRT:J, MRT:K |
| Teck Lee | LRT:A, LRT:B |
