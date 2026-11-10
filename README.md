# Pangenome Data Catalog — HPRC + HGSVC (phases 2 and 3)

**→ [tomoyakanno.github.io/pangenome-data-catalog](https://tomoyakanno.github.io/pangenome-data-catalog/)**

An interactive catalogue of which sequencing **and functional** assays exist for which donors
across the two human pangenome cohorts, plus what other projects have measured on the same
1000 Genomes cell lines. Everything is derived from the primary archives:

```
HPRC    495 donors   s3://human-pangenomics  working/ + every raw-data submissions/ folder
HGSVC    69 donors   IGSR HGSVC2 + HGSVC3 working/, the production sheet, and HGSVC donors
                     sequenced by HPRC centres (hosted in the HPRC bucket)
Other   NYGC 30x, 1KG-ONT, Geuvadis, MAGE and ENCODE, matched on the cell-line id
        557 unique donors, 7 in both cohorts, 32 assay types, 51,483 attributed data files
```

## The app

Three tabs, simplest first:

- **Overview** — one table: data types down the side, HPRC and HGSVC across the top, a bar
  and a donor count in each cell. Rare, older and QC data types sit behind one toggle.
  Click a row to list its donors.
- **Find donors** — pick the data you need; only donors with all of it are listed. Each
  button shows how many donors would remain if you added it. The donor table is a dot grid
  of the main data types; click a donor for download links.
- **About the data** — where it comes from, what to watch for, and the specialist tables.

Selections live in the URL (`#view=donors&assays=HiFi,FiberSeq&cohort=HPRC`), so a filtered
list is a shareable link.

## Rebuild (one command)

```bash
pip install pandas openpyxl        # everything else is the standard library
python3 scripts/build_catalog.py --hgsvc-xlsx /path/to/HGSVC_Production_Master-2.xlsx
python3 -m http.server 8000        # then open http://localhost:8000
```

Anonymous access only. Network reads are cached in `.cache/` (git-ignored); `--refresh`
re-crawls. A cold run is ~10 minutes, mostly listing 456k S3 keys and probing ~2,000 BAM heads.
The script prints `UNCLASSIFIED` for any new submission or IGSR directory it has no rule for,
so new data types surface instead of silently disappearing.

## What changed in the November 2026 re-verification

The earlier build was checked against a full re-listing of both archives. The headline
conclusions it drew about *functional* data were wrong in several places.

| Claim in the old catalogue | What the archives show | Why it was wrong |
|---|---|---|
| HiFi 5mC: 86 HPRC donors | **414** (every one of the 353 core-five donors) | Revio/jasmine BAMs carry 5mC as inline MM/ML tags. The old build only counted dedicated "methylation" submissions. |
| ONT methylation: not catalogued | **415 HPRC** donors with 5mC (most also 5hmC); **65 HGSVC** (nanopolish calls) | Same — dorado BAMs carry the tags inline; HGSVC's `Nanopolish_UW` directory was never classified. |
| "HGSVC has no methylation" | 54 HGSVC donors have HiFi 5mC | From HGSVC donors re-sequenced by HPRC centres (`*_HGSVC_HiFi_Y5`, `UCSC_HGSVC_ONT_R10`, `HGSVC_OMNIC`), which the old build never looked at. |
| Karyotype 40, microarray 40 | **394** and **400** | Only `working/` was scanned; Coriell's QC lives in `submissions/CORIELL_CHOP*` with `GM`/`PG` names and misspelt folders. |
| Fiber-seq 111 | **124** | Includes whole-blood HiFi BAMs that carry Fiber-seq m6A tags (`A+a`, `T-a`), and the hiPSC ONT Fiber-seq. |
| 438 HPRC donors | **495** | ~50 Year-7 donors with only ONT so far, and the 10-donor whole-blood vs LCL pilot (HG084xx) that has no `working/` directory. |
| 5 donors in both cohorts | **7** | HGSVC stored HG001 and HG002 as the literal strings `NA12878 (HG001)` / `NA24385 (HG002)`. |
| 6,143 files are "pooled, unattributable" | 2,289 genuinely unattributed | The HGSVC id regex ignored `GM*` names — most "pooled" Strand-seq was per-sample. |
| HGSVC Illumina: 63/69 "sheet only, not downloadable" | 59 of those are in the **1kGP NYGC 30x** set on IGSR | Now shown as `nygc_30x` with download links. |
| Illumina WGS includes every HPRC child | 57 donors carry **parents'** reads under `Illumina/parents/` | Tracked separately as *Trio parent Illumina*. |
| HGSVC2 ONT-UL, `*_hic_*` dirs | were skipped | Directory-name regexes missed `ONT_UL` and lowercase `hic`; the HGSVC directory list is now explicit. |

Assays that are **new to the catalogue**: Pore-C (5 donors), ONT 5mC/5hmC, whole-blood data
(11; matched LCL vs blood HiFi, Omni-C and Kinnex), iPSC data (3), trio parent Illumina, and
eight same-donor datasets from other projects (below). Data on donors outside both cohorts
(ENCODE EnTEx tissue ONT with 5mC/5hmC, GTEx-style colon HiFi, pediatric blood donors) is
listed in its own table rather than mixed into the cohort counts.

## Coverage now

| Assay | HPRC | HGSVC | | Assay | HPRC | HGSVC |
|---|---:|---:|---|---|---:|---:|
| PacBio HiFi | 423 | 69 | | HiFi 5mC | 414 | 54 |
| ONT | 453 | 64 | | ONT 5mC / 5hmC | 415 | 65 |
| Illumina WGS | 424 | 69 | | Fiber-seq (m6A) | 124 | — |
| Hi-C / Omni-C | 397 | 68 | | Kinnex / Iso-Seq | 379 | 64 |
| Pore-C | 5 | — | | RNA-seq (short) | — | 67 |
| Micro-C | 1 | — | | ONT direct RNA | 1 | — |
| Strand-seq | 9 | 69 | | Whole-blood data | 11 | — |
| Bionano | 42 | 68 | | iPSC data | 3 | — |

Same donor, other projects (cohort donors whose cell line appears there):
NYGC 30x WGS 422 / 67 · 1KG-ONT 111 / 17 · **MAGE RNA-seq 72 / 13** · Geuvadis 57 / 6 ·
ENCODE RNA 36 / 9, ATAC/DNase 11 / 4, Hi-C/ChIA-PET 5 / 4, ChIP-seq 3 / 1, DNAme 2 / 3.

**The practical shape is unchanged in kind but not in degree.** HPRC: 353 donors carry the
core five, and all 353 also have a HiFi methylome inline (317 an ONT one); Fiber-seq is still
the bottleneck at 101. HGSVC: 64 of 69 donors carry eight assays, and all 64 also have ONT
methylation. Expression is the asymmetry: HGSVC has its own short-read RNA-seq for 67 donors,
while on HPRC the best you can do is core five + MAGE RNA-seq from a different culture, 58
donors. Other projects add RNA-seq or ENCODE data for only 167 of the 557 donors in total.

## How the build decides

- **Exhaustive listing, rules afterwards.** `working/` is taken whole (minus `fail_reads` /
  `downsampled`). `submissions/` is classified by the `RULES` table (label and path regexes,
  first match wins) with an explicit `SKIP` list for assemblies, alignments, QC and calls.
  A submission file whose basename is already in that donor's `working/` is not double-counted.
- **IDs are canonicalised** before counting: `GM12345 → NA12345`, GIAB Coriell ids → `HG00x`,
  `PG12345` (Coriell/UW lab spelling) → the matching `HG`/`NA` id.
- **Methylation probe.** For every donor, the first, last and newest-chemistry BAM of each of
  HiFi and ONT is range-read (400 KB) and the reads' MM tags parsed. Results are in
  `hprc/data/hprc_methylation_probe.csv` and `hgsvc/data/hgsvc_methylation_probe.csv`.
  A negative means "not in the probed files", not "absent".
- **HGSVC evidence** per assay: `both` / `sheet_only` / `igsr_only` / `nygc_30x` / `inline`.
  `sheet_only` still means you cannot download it from IGSR.
- **Invariants** (asserted): every file row lands in exactly one of attributed / unattributed /
  out-of-catalogue; coverage is recomputed from the records and must match.

## Repo layout

```
index.html, assets/          the app — plain HTML/CSS/ES modules, no build step
site-data/catalog.json       196 KB payload: records, modality metadata, coverage, intersections
site-data/files/*.json       per-donor file lists + same-donor external links, fetched on demand
site-data/files/_unattributed.json   unattributed, out-of-catalogue and other-donor files
scripts/build_catalog.py     the whole pipeline
hprc/data/                   file index, sample × assay matrix, methylation probe, other donors
hgsvc/data/                  file index, sample × assay matrix (+ __src evidence), probe
external/data/               same-donor datasets by donor
combined/data/               the app payload, pretty-printed
```

## Known limits

- The methylation flags are a lower bound for donors whose probed BAMs predate inline calls.
- Other-project data is the same genome, not the same cells (different culture and passage).
- MAGE is read from ENA (PRJNA851328); ENCODE from its public search API (released, cell lines).
- The 718-sample pooled Strand-seq set (`HGSVC3/working/20260214_Strandseq_Pools`) is a
  separate 1kGP effort and is not counted as HGSVC Strand-seq.
- The HGSVC production spreadsheet is not in this repository; pass its path to rebuild.

## Sources

[human-pangenomics](https://github.com/human-pangenomics) ·
[IGSR HGSVC2](https://www.internationalgenome.org/data-portal/data-collection/hgsvc2) /
[HGSVC3](https://www.internationalgenome.org/data-portal/data-collection/hgsvc3) ·
[1kGP 30x](https://www.internationalgenome.org/data-portal/data-collection/30x-grch38) ·
[1KG-ONT](https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/data_collections/1KG_ONT_VIENNA/) ·
[Geuvadis](https://www.internationalgenome.org/data-portal/data-collection/geuvadis) ·
[MAGE](https://doi.org/10.1038/s41586-024-07708-2) · [ENCODE](https://www.encodeproject.org/).
Underlying data is openly licensed by the respective consortia; this catalogue only indexes it.
