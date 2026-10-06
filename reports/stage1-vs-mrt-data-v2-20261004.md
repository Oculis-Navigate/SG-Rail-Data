# Stage 1 comparison with `mrt-data-v2`

> This historical comparison predates the final partial-inventory overrides and
> grouped-platform normalization. See
> [`mrt-wayfinding-vs-mrt-data-v2-20261005.md`](mrt-wayfinding-vs-mrt-data-v2-20261005.md)
> for the current comparison.

Compared the saved `mrt-data-v2` MVP Stage 1 artifact with the new `mrt-wayfinding` Stage 1 replay.

> The replay uses saved real SgWiki and LTA BusStops evidence. Its GTFS inventory is a temporary catalogue mirror because this sandbox cannot resolve DataMall. Operational-status fields are excluded from the comparison.

## Result

| Check | Result |
|---|---:|
| Stations | 184 in both |
| Station-code mismatches | 0 |
| SgWiki page/revision mismatches | 0 |
| Exit-inventory status mismatches | 0 |
| Areas differing | 0 stations |
| Platforms differing | 0 stations |
| Raw SgWiki bus evidence differing | 0 stations |
| Common exit semantics differing | 0 exits |
| Closed-exit evidence | identical |

The semantic source data is unchanged. `mrt-wayfinding` preserves all 607 SgWiki bus rows, all 706 level/area records, all 438 platform records, and the same three partial inventories: Cantonment, Keppel and Prince Edward Road. It also trims the erroneous trailing space from `Bedok North `.

## Intended exit-identity changes

`mrt-data-v2` had 613 identities. `mrt-wayfinding` has 612 because Choa Chu Kang Exit E is represented once as a shared MRT/LRT physical exit instead of twice.

- **Cantonment**: `UNSCOPED:1, UNSCOPED:3, UNSCOPED:4` → `MRT:1, MRT:3, MRT:4`
- **Choa Chu Kang**: `LRT:E, MRT:E` → `SHARED:E`
- **Keppel**: `UNSCOPED:2, UNSCOPED:3` → `MRT:2, MRT:3`
- **Prince Edward Road**: `UNSCOPED:1, UNSCOPED:2` → `MRT:1, MRT:2`
- **Punggol**: `UNSCOPED:A, UNSCOPED:B, UNSCOPED:C, UNSCOPED:D` → `SHARED:A, SHARED:B, SHARED:C, SHARED:D`
- **Sengkang**: `UNSCOPED:A, UNSCOPED:B, UNSCOPED:C, UNSCOPED:D` → `SHARED:A, SHARED:B, SHARED:C, SHARED:D`

Punggol and Sengkang now use shared MRT/LRT identities. The three partial TEL stations use their station codes to classify bus-derived exits as MRT instead of leaving them unscoped.

## Bukit Panjang reviewed corrections

All eight original SgWiki bus rows remain in the evidence ledger. Six are accepted into wayfinding and two are explicitly excluded.

| Exit | `mrt-data-v2` | `mrt-wayfinding` |
|---|---|---|
| `LRT:A` | 45009, 44029, 44031, 44259, 44909, 44859 | 45009, 44031, 44859 |
| `LRT:B` | — | 45009 |
| `MRT:A1` | — | 44029 |
| `MRT:A2` | — | 44029 |
| `MRT:B` | — | 44021 |
| `MRT:C` | — | 44251 |

- Bus stop `44259` is retained but excluded from exit wayfinding.
- Bus stop `44909` is retained but excluded from exit wayfinding.
- `45009` resolves to both LRT A and LRT B.
- `44029` resolves to MRT A1 and A2.
- `44021` resolves to MRT B.
- `44251` resolves to MRT C.
- `44031` and `44859` resolve to LRT A.

## Review ledger

`mrt-data-v2` emitted 191 Stage 1 review items; `mrt-wayfinding` emits 190. The underlying SgWiki review evidence is retained. Layout reviews are now separated into 67 advisories and 11 blocking layout reviews, five previously ambiguous bus mappings are resolved by reviewed rules, three operator additions are called out explicitly, and Dhoby Ghaut Exit G records its repeated source evidence as a deterministic normalization.

## Artifacts

- New run: `/Users/ryanyeo/Projects/talk/mrt-wayfinding/runs/saved-evidence-v2-comparison-20261004`
- New Stage 1: `/Users/ryanyeo/Projects/talk/mrt-wayfinding/runs/saved-evidence-v2-comparison-20261004/stage1/stations.json`
- `mrt-data-v2` Stage 1: `/Users/ryanyeo/Projects/talk/mrt-data-v2/outputs/mvp-data/mvp-full/stage1.json`
- Machine-readable comparison: `/Users/ryanyeo/Projects/talk/mrt-wayfinding/reports/stage1-vs-mrt-data-v2-20261004.json`
