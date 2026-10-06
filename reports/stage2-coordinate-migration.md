# Stage 2 coordinate migration

> This audit predates the separately authorized
> [trusted OneMap import](stage2-trusted-onemap-import.md). With that import,
> the offline implementation replay resolves 574/614 exits; 40 remain unresolved.

The approved Stage 1 baseline contains **614** exit identities. The saved `mrt-data-v2` candidate bundle contains 279 Kartographer candidate records representing 278 distinct raw features and current identities. Two CCK records reference the same raw feature because the old model expanded it into MRT and LRT identities; the new model keeps it once as `SHARED:E`. All 28 human-reviewed coordinates map exactly and none overlaps Kartographer.

This provides 306 currently resolved identities and leaves **308** exits across **90** stations for human review. These counts audit the saved evidence; every pipeline run reparses its own revision-pinned MapData.

A strict full replay of the saved raw overlays recovered two exact markers that
the old candidate bundle had omitted: `cantonment/MRT:2` and `keppel/MRT:1`.
That replay resolved 280 identities from Kartographer plus the 28 reviewed gap
fills, leaving **306** exits across **88** stations unresolved. A live run still
recomputes these counts from the current page revisions.

| Evidence | Features | Current identities | Automatically trusted |
|---|---:|---:|---|
| SgWiki Kartographer | 279 candidate records / 278 raw features | 278 | Yes, after strict source validation and exact identity matching |
| Human-reviewed gap fills | 28 | 28 | Yes, while the pinned SgWiki page revision still matches |
| OneMap, LTA GeoJSON, OSM and manual proposals | — | — | No; links may be shown as review evidence only |

The original coordinate edit ledger is retained byte-for-byte at `config/stage2-coordinate-ledger.json`; its SHA-256 and entry hashes are referenced by the migrated reviewed-coordinate file.

The complete station-by-station unresolved inventory and identity mappings are in `reports/stage2-coordinate-migration.json`.
