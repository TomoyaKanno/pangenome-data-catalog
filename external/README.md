# Same donor, other projects

`data/external_by_donor.csv`: one row per (donor, dataset) with the item count and the first
URL / accession. Full lists are in each donor's `site-data/files/<id>.json` under `external`.

| Dataset | Source |
|---|---|
| NYGC_30x | IGSR `1000G_2504_high_coverage` (+698 related) sequence indexes |
| ONT_1KG | IGSR `1KG_ONT_VIENNA/hg38/` (R9.4.1, no methylation tags) |
| Geuvadis_RNA / _smallRNA | IGSR `geuvadis.sequence.index` |
| MAGE_RNA | ENA PRJNA851328 |
| ENCODE_* | ENCODE search API, released human cell-line experiments, term name `GM#####` |

Matched on the canonical cell-line id. Different culture and passage from the cohorts' own data.
