# HPRC tables

Written by `scripts/build_catalog.py` — see the top-level README for method and caveats.

| File | What it is |
|---|---|
| `data/hprc_file_index.csv` | One row per file from `working/` and raw-data `submissions/`: sample, modality, assay_detail, tissue, kind (data/index/report), s3_key, size, source, indexed_on_github, in_catalogue |
| `data/hprc_sample_modality_matrix.csv` | 495 donors × assay booleans, with `<assay>__src` evidence (e.g. `inline`, `dedicated`) |
| `data/hprc_methylation_probe.csv` | Every probed BAM: MM modification codes found, kinetics present, programs in the header |
| `data/hprc_other_donors_file_index.csv` | EnTEx tissue, colon tissue and pediatric blood donors who are not cohort members |

`release` is `R2`, `R3`, `R2+R3` or `untagged` (data present, no release-table row).
