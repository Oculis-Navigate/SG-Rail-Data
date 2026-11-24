# Unreleased review since `caa0384` — 23 November 2026

This is a working-tree review, not release notes for a published version. The comparison point is the 1 November commit `caa0384` (D-029, consumer-facing station-code display order). Before that, `cbf2b96` approved the Stage 2 comparison baseline and `ac4994d` added the comparison-only rail geometry baseline. No dataset tag was created in this review. D-040 and D-046 promote the Stage 1 and Stage 2 comparison baselines; the geometry baseline remains unchanged.

## Changes

- **Published station/exit additions, removals, coordinate moves and geometry changes:** none. The decisions below change how the next candidate will be built; they are not evidence of nine new exits or two newly closed exits.
- **Closure parser:** recognize the exact SgWiki `(Closed)` annotation as closed with no stated alternative. Parse explicit alternatives for source evidence and reject unknown annotations. D-035 keeps closure status but suppresses all passenger-facing diversion advice.
- **Map parser:** accept `Exit X` and `[Exit-X]Exit X` only when all labels match the marker symbol, both in collection and Stage 2. Preserve the source title. Allow the preview PNG URL to omit `revid`; reject contradictory, empty or duplicate values. The MapData request and response remain bound to the page revision and overlay.
- **Landmarks:** support a reviewed, exact POI row as additional destinations for one exit. D-033 is scoped to Tanjong Pagar MRT:D; it does not apply a generic comma-splitting rule to every station.
- **Contributor workflow:** replace the quarterly/manual GitHub live-collection workflow locally with contributor-run live collection, review and offline replay. `scripts/dataset.py` prepares and verifies versioned changesets and renders notes. Native Git hooks check staged data, commit summaries and validation; PR CI independently replays candidates. The proposed GitHub deployment uses administrator dispatch, a protected environment, version tags and reviewed release notes. See [setup and remaining repository settings](../docs/dataset-releases.md). These workflow changes have not been pushed or enabled on GitHub. No persistent runner or exit node was configured.

## Evidence

The [decision ledger](../audit/decision-ledger.jsonl) is authoritative. Its last committed event was S-037; S-038 through S-076 are local additions. The evidence packages below retain source revisions, hashes, exact findings and the authorization scope. They document what SgWiki reported, not independently surveyed physical changes.

## Human decisions

| Scope | Decision / final event | Approved or retired change |
|---|---|---|
| Ang Mo Kio MRT:D | [D-030](../audit/decisions/D-030.json) / S-039 | At revision 39808 keep the source-reported closure; stop directing users to B. No verified reopening date. The platform finding was not renewed. |
| Cantonment MRT:1–4 | D-004 / S-040, retired | Revision 41849 lists all four exits directly. Remove the partial-inventory override, not an exit. |
| Keppel MRT:1–3 | D-005 / S-041, retired | Revision 41852 lists all three exits directly. Same identities, now sourced from the table. |
| Prince Edward Road MRT:1–2 | D-006 / S-042, retired | Revision 41853 lists both exits directly. Same identities, no override. See the [three-station evidence](../audit/evidence/2026-11-22-exit-inventory-retirements.json). |
| Choa Chu Kang MRT:D | [D-031](../audit/decisions/D-031.json) / S-044 | At revision 39978 keep D closed; remove the old A/C/E routing advice. BPLRT groups 1/3 and 2/4 and shared Exit E retain their interpretation. No coordinate change approved. |
| Dhoby Ghaut MRT:E/F/G | [D-032](../audit/decisions/D-032.json) / S-046 | Renew D-026 at revision 41817. E, F and G are distinct scoped identities. The two G rows describe **one G** serving The Atrium@Orchard and Macdonald House. Retain the narrow NEL table-span exception. |
| Tanjong Pagar MRT:D | [D-033](../audit/decisions/D-033.json) / S-048 | Keep Prince Edward Road as the location; per [D-038](../audit/decisions/D-038.json) / S-058 display Skywaters Residences and MAS Building, followed by Shenton Way Terminal and CC32 Prince Edward Road as nearby destinations at revision 41616. No transfer or Exit E routing approval. |
| Paya Lebar, B2 CCL | [D-034](../audit/decisions/D-034.json) / S-050 | Renew 34530 → 41822. Keep C and D as distinct platform faces with source group `C/D` and the heading “No train service at this platform.” Do not infer permanent closure. |
| Woodlands, stop 46009 | D-034 / S-050 | Renew 37676 → 39894. Keep Woodlands Bus Interchange as a station-level connection; do not assign the unlabeled bus row to a specific exit. |
| All closures; Tanjong Pagar MRT:E | [D-035](../audit/decisions/D-035.json) / S-052 | Report closed without directing passengers to another exit. Accept the closure finding at 41616; retain conflicting D/H directions as evidence only. |

## Validation and outstanding decisions

- Final baseline replay `20261123T135507Z-stage2-promoted` exited 0 after clean candidate replay `20261123T135507Z-stage2-verification`. All 614 exit coordinates resolved; no review findings or baseline differences. D-044 renewed the remaining guards and scoped the City Hall E/F/G exclusion to exact source markers. D-045 retired 13 superseded fallback points; D-046 promotes the resulting baseline. City Hall A–D retain the earlier approved positions with the Esplanade overlap disclosed. Ledger: **46 decisions, 40 approved, 4 retired, 2 replaced**, none pending. No external publication.
- Last live run: `20261123T102639Z-pl-woodlands-live`. Collection completed: 184 SgWiki pages, 94 embedded maps, 186 GTFS active-station records and 5,210 LTA bus stops. Stage 1 failed; Stage 2 did not start; no release was created.
- **Ang Mo Kio:** D-036 / S-054 renewed MRT Exit D's closure; D-037 / S-056 retained it and accepted L2 NSL C/D at revision 41909. Replay `20261123T123457Z-amk-platform` has no remaining AMK finding. D-039 / S-060 approved all three catalog entries. Candidate `20261123T130008Z-stage1-approved-candidate` was checked against the prior readiness run and promoted under D-040 / S-062. Replay `20261123T130023Z-stage2-start` completed Stage 1 with zero findings and baseline differences. Stage 2 ran: 541/614 exits resolved; 162 findings and 268 baseline differences; no release.
- **Stage 2 movement review:** D-041 / S-064 accepts the exact 13 candidate moves from `20261123T130023Z-stage2-start`, including City Hall A–D with the Esplanade overlap disclosed. Not an approval of unmatched markers or of the failed Stage 2 dataset as a whole.
- **Stage 2 selections:** Bayfront A keeps OneMap at revision 41890 (D-042); Kent Ridge E uses the retained manual candidate at revision 41023 (D-043). Replay `20261123T134946Z-bayfront-kent-ridge` resolved 543/614 with 71 still unresolved. Stage 2 baseline unchanged.
- **Original Stage 2 preflight:** 86 coordinate records have stale revision pins (1 reviewed, 80 trusted OneMap, 5 supplemental). These are review candidates, **not 86 verified coordinate movements**. No coordinate guard was renewed.
- **Operational follow-up:** batch reporting of independent review failures was recommended; the production pipeline still stops on blocking contracts. Replay the retained inputs after review. Do not promote until the candidate reproduces, all blocking findings are resolved, and GitHub protections are configured.
