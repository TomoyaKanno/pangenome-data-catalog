# HGSVC tables (phases 2 and 3)

Written by `scripts/build_catalog.py` — see the top-level README for method and caveats.

HGSVC2 and HGSVC3 are one 69-donor cohort: every donor in the HGSVC2 indexes is on the
HGSVC production sheet. `phases` records which file collections hold each donor's data:
`HGSVC2`, `HGSVC3` (IGSR) and `HPRC bucket` (HGSVC donors sequenced by HPRC centres).

| File | What it is |
|---|---|
| `data/hgsvc_file_index.csv` | One row per file: sample, collection, directory, modality, exact size in bytes, url |
| `data/hgsvc_sample_modality_matrix.csv` | 69 donors × assays, with `<assay>__src` = `both` / `sheet_only` / `igsr_only` / `nygc_30x` / `inline` |
| `data/hgsvc_methylation_probe.csv` | Probed IGSR HiFi BAMs |

`sheet_only` means the production sheet records the assay but no file exists on IGSR or in
the HPRC bucket. For Illumina, `nygc_30x` means the 1kGP NYGC 30x genome is on IGSR instead.
