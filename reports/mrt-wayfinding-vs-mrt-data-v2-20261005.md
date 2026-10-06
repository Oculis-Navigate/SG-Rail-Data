# `mrt-wayfinding` compared with `mrt-data-v2`

> Historical comparison before the owner authorized the frozen OneMap import.
> The subsequent [trusted OneMap import](stage2-trusted-onemap-import.md) adds
> 266 trusted positions, bringing replay coverage to 574/614 with 40 unresolved.

This comparison uses the approved `mrt-wayfinding` Stage 1 baseline, the full
offline Stage 2 replay, and these `mrt-data-v2` artifacts:

- `outputs/mvp-data/mvp-full/stage1.json`
- `outputs/mvp-data/mvp-full/stage2.json`
- `review_app/data/candidates.geojson`
- `data/exit-coordinate-ssot.geojson`

The Stage 2 replay uses the saved raw Kartographer overlays. It is suitable for
comparing the implementations, but a live run can change coverage when SgWiki
page revisions or overlays change.

## Stage 1

| Measure | `mrt-data-v2` | `mrt-wayfinding` |
|---|---:|---:|
| Stations | 184 | 184 active through LTA GTFS |
| Exit identities | 613 | 614 |
| Complete exit inventories | 181 | 184 |
| Partial exit inventories | 3 | 0 |
| Area/level records | 706 | 706 |
| Platform records | 438 | 446 |
| Raw SgWiki bus rows retained | 607 | 607: 605 accepted and 2 explicitly excluded |
| Pending Stage 1 review items | 191 | 0 after revision-pinned reviewed decisions |
| Closed exits | 3 | 3 |

Station codes, source page identities, source revisions, area records, raw bus
evidence, and common exit semantics match. The current model adds or changes
only reviewed semantics:

- Cantonment has the reviewed complete inventory `MRT:1` through `MRT:4`; Exit
  2 was absent from the old partial bus-only inventory.
- Keppel has the reviewed complete inventory `MRT:1` through `MRT:3`; Exit 1
  was absent from the old partial bus-only inventory.
- Prince Edward Road's two bus-derived exits are explicitly MRT-scoped.
- Choa Chu Kang's duplicate `MRT:E` and `LRT:E` records become one physical
  `SHARED:E` identity.
- Punggol and Sengkang exits are represented as shared MRT/LRT identities.
- Bukit Panjang uses the reviewed MRT/LRT bus-to-exit mapping; bus stops 44259
  and 44909 remain in evidence but are excluded from exit wayfinding.
- Combined platform labels are expanded into individual platform records while
  preserving the original label in `source_group_label`. This adds eight
  records across Ang Mo Kio, Choa Chu Kang, Jurong East, Paya Lebar and Tanah
  Merah without changing the source evidence.
- Source-reported closures, Chinatown Exit G operating hours, Woodlands' bus
  interchange relationship, rail service diagrams, and empty layout formatting
  tables are retained as structured records.

## Stage 2

| Measure | `mrt-data-v2` experimental Stage 2 | `mrt-wayfinding` |
|---|---:|---:|
| Exit identities | 613 | 614 |
| Coordinates selected/resolved | 553 | 308 |
| SgWiki Kartographer selected | 277 | 280 |
| OneMap selected automatically | 276 | 0 |
| Human-reviewed selected | 0 in the experimental artifact | 28 |
| Unresolved exits | 60 across 35 stations | 306 across 88 stations |
| Publication allowed | No | No, pending review |

The old candidate bundle contained 872 coordinate candidates: 528 OneMap, 279
Kartographer, 46 LTA GeoJSON, 12 OSM, and 7 reviewed-manual candidates. Its
Stage 2 builder selected 277 Kartographer coordinates and then filled 276 gaps
from OneMap, but labelled all 553 selections experimental and did not permit
publication.

The new resolution count reconciles exactly:

- 277 Kartographer coordinates are byte-for-byte the same as the old selected
  Kartographer coordinates.
- All 28 coordinates from the later `mrt-data-v2` human-approved SSOT are
  preserved exactly, including its ledger hashes. Ten replace old experimental
  OneMap positions; 18 resolve exits that the old Stage 2 artifact left
  unresolved.
- Three additional Kartographer identities now resolve: Cantonment Exit 2,
  Keppel Exit 1, and Choa Chu Kang shared Exit E.
- The other 266 experimental OneMap selections deliberately return to
  unresolved status across 70 stations. OneMap, OSM, LTA and operator material
  is review evidence, never an automatic fallback.

The 28 reviewed coordinates retain the exact final positions approved in
`mrt-data-v2`. Their original evidence bases were 10 LTA GeoJSON candidates, 10
OneMap candidates, seven manually proposed positions, and one OSM candidate;
the approval and edit ledger, rather than those candidate sources, is now the
authority.

Ten of those approvals supersede coordinates that the old experimental Stage 2
had selected from OneMap:

| Exit | Movement from old experimental position |
|---|---:|
| Bukit Panjang `MRT:A1` | 17.5 m |
| Bukit Panjang `MRT:A2` | 8.9 m |
| Bukit Panjang `MRT:B` | 7.2 m |
| Downtown `MRT:A` | 7.1 m |
| Downtown `MRT:E` | 23.7 m |
| Downtown `MRT:F` | 20.4 m |
| Gardens by the Bay `MRT:2` | 103.0 m |
| Telok Ayer `MRT:A` | 15.7 m |
| Telok Ayer `MRT:D` | 6.6 m |
| Telok Ayer `MRT:E` | 11.3 m |

These are not new automated changes: they are the existing human-approved
positions migrated from the old SSOT. The remaining 18 reviewed coordinates
and three additional Kartographer matches resolve 21 identities across 14
stations that the old experimental Stage 2 left unresolved or did not contain.

Therefore the new dataset has lower provisional coordinate coverage but a much
stronger meaning: every resolved coordinate has either revision-matched SgWiki
Kartographer provenance or an explicit human approval. No unreviewed OneMap
coordinate appears as authoritative data.
