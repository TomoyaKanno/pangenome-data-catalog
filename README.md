# Pangenome Data Catalog — HPRC + HGSVC3

**→ [tomoyakanno.github.io/pangenome-data-catalog](https://tomoyakanno.github.io/pangenome-data-catalog/)**

An interactive catalogue of which sequencing assays exist for which samples across the
two major human pangenome cohorts. Everything was derived by enumerating the primary
archives directly — consortium index tables and production spreadsheets were used for
metadata and explanation, never as the sole authority on what exists.

```
hprc/       438 samples, 14 assays, 19,807 files, ~758 TB   (s3://human-pangenomics)
hgsvc3/      69 samples, 11 assays, 18,174 files            (IGSR + production sheet)
combined/   507 records, 502 unique donors                  (unified vocabulary)
```

## What the app does

Three views over one 141 KB static payload:

- **Combine modalities** — require several assays at once and watch the matching donor
  count collapse in real time. A cascade chart shows the falloff step by step, and a
  *cost of adding one more* panel shows what each additional assay would cost you
  before you commit to it. This is the headline feature: it makes visible that
  multi-modal work on HPRC is a ~100-donor study, not a 400-donor one.
- **Coverage** — per-assay coverage in each cohort, normalised within cohort so 402/438
  and 69/69 stay comparable. Assays a consortium never ran read *not run*, never `0`.
- **All samples** — searchable, sortable, filterable by cohort, population, sex and
  release, with CSV export.

Clicking any row opens a drawer with that donor's metadata, per-assay provenance and a
lazily-loaded file list with direct download URLs.

Selections live in the URL, so a filtered view is a shareable link:
[`#assays=HiFi,ONT,FiberSeq&cohort=HPRC`](https://tomoyakanno.github.io/pangenome-data-catalog/#assays=HiFi,ONT,FiberSeq&cohort=HPRC).

## What the data actually says

**The two cohorts are nearly disjoint.** Only 5 donors appear in both: HG00733, HG02818,
HG03486, NA19036, NA19240. The union is therefore close to additive, and the cohort
facet is genuinely informative rather than decoration.

**They have opposite shapes, and this is the most useful thing the app shows:**

- **HPRC — large, sparse beyond the core.** 346 of 438 donors carry the core five
  (HiFi, ONT, Illumina, Hi-C, Kinnex), but only 100 add Fiber-seq and only 44 also have
  HiFi 5mC.
- **HGSVC3 — small, near-complete.** 64 of 69 donors carry eight assays simultaneously
  (Illumina, HiFi, ONT, Strand-seq, Hi-C, Bionano, RNA-seq, Kinnex).

So: **need scale on the core assays → HPRC; need many assays on the same donor → HGSVC3.**

**Complementary specialities.** HGSVC3 has short-read RNA-seq (67 donors) and Strand-seq
at scale (69) — HPRC has essentially neither. HPRC has Fiber-seq (111), HiFi 5mC (86),
Micro-C (1, HG002 only) and ONT direct RNA (1) — HGSVC3 has none of those. A zero in the
coverage table means *that consortium did not run that assay*, not that data is missing.

## Provenance, in one line

Neither consortium's index tables are complete. For HPRC, GitHub index tables resolve
about 9.7k of 19.8k files and eight assays have no index at all. For HGSVC3, the
production spreadsheet asserts data that is not on IGSR (63 of 69 Illumina rows, held at
the Broad). Both catalogues therefore carry explicit provenance — `indexed_on_github`
for HPRC, `<modality>__src` for HGSVC3 — surfaced in the UI on every row. It is the
difference between "this exists" and "you can download this".

## Repo layout

```
index.html, assets/         the app — plain HTML/CSS/ES modules, no build step
site-data/catalog.json      141 KB app payload (507 records, coverage, intersections)
site-data/files/*.json      per-donor download lists, fetched on demand
scripts/prep_site_data.py   rebuilds site-data/ from the cohort bundles below
hprc/, hgsvc3/, combined/   source-of-truth data + per-cohort READMEs and build scripts
```

Each cohort directory has its own `README.md` with the caveats specific to it.
**Read those before writing code against the data** — each contains at least one trap
that silently corrupts counts.

## Running it locally

The app fetches JSON, so it needs to be served over HTTP rather than opened as a file:

```bash
python3 -m http.server 8000    # then open http://localhost:8000
```

## Regenerating

After either cohort's data changes, rebuild the cohort bundle and then the site payload:

```bash
pip install boto3 pandas requests openpyxl

cd hprc/scripts   && python build_hprc_catalog.py --out ../data
cd hgsvc3/scripts && python build_hgsvc3_catalog.py \
    --xlsx /path/to/HGSVC_Production_Master-2.xlsx --out ../data
python scripts/prep_site_data.py
```

Both cohort builds use anonymous access, no credentials. HPRC needs
`human-pangenomics.s3.us-west-2.amazonaws.com` and `raw.githubusercontent.com`; HGSVC3
needs `ftp.1000genomes.ebi.ac.uk` over HTTPS.

`prep_site_data.py` will refuse to write if its invariants break — every file index row
must land in exactly one bucket, and derived coverage must match what the cohort build
scripts published. Those assertions have already caught two real defects; leave them in.

## Known data quirks the prep step handles

- **GM/NA aliasing.** The HGSVC3 catalogue names four donors `GM19129`, `GM19320`,
  `GM20355`, `GM21487` while its own file index spells the same Coriell donors `NA…`.
  Unaliased, those donors show an empty download list while their files sit unattributed.
- **6,143 files have no sample attribution** — overwhelmingly pooled or multiplexed
  Strand-seq libraries. A further 20 name donors outside the catalogued cohorts,
  including `NA24385` (GIAB's HG002). Both groups are preserved in
  `site-data/files/_unattributed.json` rather than dropped.
- **Kinnex and Iso-Seq are overlapping sets**, not disjoint ones — all 12 HGSVC3 Iso-Seq
  donors are among the 64 Kinnex donors, so the unified `Kinnex_IsoSeq` count is 64, not 76.

## Sources

- [`human-pangenomics`](https://github.com/human-pangenomics) — S3 bucket and index tables
- [IGSR / HGSVC3](https://www.internationalgenome.org/data-portal/data-collection/hgsvc3) — the 1000 Genomes archive

Underlying data is openly licensed by the respective consortia; this catalogue only indexes it.
