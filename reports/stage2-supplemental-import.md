# Remaining coordinate import — 2026-10-05

The project owner accepted the remaining candidate coordinates and asked for
an evidence-based choice between the two conflicting OneMap positions. The
[frozen supplemental baseline](../config/stage2-trusted-supplemental-baseline.geojson)
contains 40 entries across 23 stations: 31 LTA, seven OSM and two OneMap.
The [machine-readable audit](stage2-supplemental-import.json) retains every
candidate considered, its original feature index, the selected coordinates
and the reason for selection.

The 31 LTA entries each had one saved LTA candidate: 30 LRT exits and Bugis F.
Five OSM entries had no other saved source: Raffles Place K/L/M and Tanjong Pagar
J/K. Bugis E/G use tagged OSM entrances, with LTA candidates approximately 9.1 m
and 14.3 m away respectively. These 38 entries are owner-accepted candidates,
not individually positioned or ground-verified pins.

## The two OneMap conflicts

Coordinates below are latitude, longitude. GeoJSON stores longitude first.

| Exit | Selected OneMap point | Supporting saved evidence | Alternative |
|---|---|---|---|
| Expo A | 1.335321724253238, 103.9619429582246 | Southern point, 2.8 m from OSM node 5168796839; essentially coincident with the LTA candidate | Northern point is 19.8 m from that OSM entrance; the two OneMap points are 22.6 m apart |
| Farrer Park E | 1.311863732195899, 103.8526350747985 | Eastern point, 9.8 m from OSM node 2402426984 and 2.8 m from the LTA candidate | Western point is 57.2 m from that OSM entrance; the two OneMap points are 47.5 m apart |

The saved SBS locality maps were visually inspected:

- Expo: `mrt-data-v2/outputs/20260930T234320957682Z/maps/sbs/expo/dtl-xpo.png`.
  Exit A is shown beside Expo Foyer 2, south of the main Downtown Line section.
- Farrer Park: `mrt-data-v2/outputs/20260930T234320957682Z/maps/sbs/farrer-park/nel-frp.png`.
  Exit E is shown on the Northumberland Road side at the southwest station end.

The diagrams support the general placement but do not establish exact surveyed
coordinates. The choices are inferences from the saved evidence; provider
agreement does not establish that the sources are independent. No new points
were estimated from pixels. Original OneMap coordinates were retained exactly.

The review app could not be opened live: the sandbox denied its local-server
bind with `Operation not permitted`. Attempts to open the individual OSM nodes
through the web tool also failed. Therefore this audit uses saved operator
images and saved OSM/LTA records, not a successful live app inspection or fresh
online verification. Both choices explicitly record
`decision_method: assistant_evidence_selection` and remain distinct from
`human_reviewed` pins.

## Guards and validation

Each entry is pinned to its SgWiki page ID and revision, station name/codes and
exact exit identity. Changes to applicable guards block publication and leave
the stale coordinate unapplied. A later human review supersedes an imported
entry. Kartographer takes precedence over the import and emits a review warning.
No live LTA, OSM or OneMap query can silently update these positions.

The original candidate bundle SHA-256 is
`cccac719c2a641e15df739aa943c1d3260d00d6d48ee09d6ed487b17faf66c0d`.
All retained audit candidates were compared with the original bundle and matched
exactly. The baseline and audit are self-contained; the pipeline does not read
the old repository at runtime.

All 121 tests pass, including stale guards, missing identities, source
precedence, invalid types, unknown fields, duplicate/overlapping imports and
comparison against the earlier output schema.

## Offline replay

Run: `20261005T070319535649Z-6b524d07f1e7`, under
`/private/tmp/mrt-wayfinding-supplemental-replay`.

| Selected source class | Exit identities |
|---|---:|
| SgWiki Kartographer | 280 |
| Human-reviewed coordinates | 28 |
| Earlier trusted OneMap import | 266 |
| Supplemental LTA | 31 |
| Supplemental OSM | 7 |
| Supplemental OneMap conflict selections | 2 |
| **Total resolved** | **614** |
| Unresolved | 0 |

All previously resolved 574 exit records are unchanged, including coordinates
and provenance. There are no Stage 1 reviews or Stage 2 coordinate findings.
The sole remaining run warning is `approved_stage2_baseline_unavailable`:
the first complete Stage 2 output still needs approval before publication.
No release was created.

This is an implementation replay, not a fresh source collection. Its temporary
fixture adjusts the Woodlands North mapdata response revision from 33908 to
38586 to match the saved newer page, retaining the same overlay ID. That fixture
is not current-source evidence and must not be promoted as an approved live
baseline. A live run must fetch the requested revision and recompute coverage.
