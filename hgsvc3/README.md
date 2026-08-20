# HGSVC3 Data Catalog — handoff bundle

69-sample HGSVC3 assembly cohort, 11 modalities, 18,174 data files.

Two sources, both retained and kept distinguishable:

1. **`HGSVC_Production_Master-2.xlsx`, `Summary` sheet** — the consortium's own
   per-sample status matrix. Authoritative for intent, and for data that exists but
   is held outside IGSR (Illumina at the Broad, for instance).
2. **The IGSR archive** at `https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/data_collections/HGSVC{2,3}/working/`
   — the actual files. Authoritative for what you can download.

Every modality flag carries a companion `__src` column: `both`, `sheet_only`, or
`igsr_only`. **Do not collapse it.** "Sheet says yes, no file on the archive" is a
real and common state — it is 63 of 69 samples for Illumina, because that data lives
at the Broad. Presenting `sheet_only` as simply "available" would send users hunting
for files that were never on IGSR.

## Coverage

| Modality | Samples | Both sources | Sheet only |
|---|---|---|---|
| Illumina_WGS | 69 | 6 | 63 |
| HiFi | 69 | 61 | 8 |
| StrandSeq | 69 | 38 | 31 |
| HiC | 68 | 44 | 24 |
| Bionano | 68 | 47 | 21 |
| RNAseq | 67 | 61 | 6 |
| ONT_UL | 64 | 58 | 6 |
| Kinnex | 64 | 54 | 10 |
| PacBio_CLR | 50 | 25 | 25 |
| IsoSeq | 12 | 12 | 0 |
| ChrY_ONT | 1 | 0 | 1 |

**HGSVC3 is far more uniformly covered than HPRC.** 64 of 69 samples have all eight
of Illumina + HiFi + ONT + Strand-seq + Hi-C + Bionano + RNA-seq + Kinnex. Compare
HPRC, where the equivalent multi-modal intersection collapses to ~100 of 438. If a
user needs many modalities on the same samples, HGSVC3 is the better cohort; if they
need scale on the core five, HPRC is.

**HGSVC3 has short-read RNA-seq (67 samples); HPRC has none.** Accessions:
`PRJEB39684` (HGSVC1/2), `PRJEB58376`, `PRJEB75192`. This is the single biggest
functional-data difference between the two cohorts.

## Files

| File | What it is |
|---|---|
| `data/hgsvc3_catalog.json` | App payload (42 KB): samples, per-modality evidence, totals, intersections |
| `data/hgsvc3_sample_modality_matrix.csv` | 69 × 11 booleans + `__src` provenance + family/relation/population |
| `data/hgsvc3_file_index.csv` | 18,174 files: modality, sample, filename, full HTTPS url |
| `data/hgsvc3_modality_intersections.csv` | Precomputed combination counts |
| `data/hgsvc3_assay_provenance.csv` | Per modality: sheet vs archive counts, caveats |
| `scripts/build_hgsvc3_catalog.py` | Regenerates all of the above |

## Things that will bite you

**The 718-sample Strand-seq pool is a different cohort.** `20260214_Strandseq_Pools/`
holds pooled Strand-seq for >718 1kGP samples — a separate HGSVC Year 3 funded effort
overlapping the Miller ONT and 1KG_ONT_VIENNA long-read cohorts, per its own README.
Crawling it naively inflates Strand-seq from 69 samples to 798. The build script
excludes it by name. If you want it in the app, add it as its own cohort, not as
HGSVC3 Strand-seq.

**Directory-name classification order matters.** Kinnex directories are named
`20250610_JAX_HiFi_kinnex` and `20250922_EEE_HiFi_Kinnex` — test Kinnex *before* HiFi
or they are silently absorbed and Kinnex reads as zero.

**Use HTTPS, not FTP.** The 1000G archive serves the same tree at
`https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/`. Plain FTP is commonly blocked.

**Strand-seq library counts are messy free text** — `14+32`, `19 + 77`,
`26 + cell grow issue`. The parser sums pure sums and otherwise takes the leading
integer. Median is 57 libraries per sample.

**Iso-Seq is legacy.** 12 samples from HGSVC2 Year 2, superseded by Kinnex. Same for
PacBio CLR (50 samples), superseded by HiFi. Both are worth showing but should be
visually de-emphasised relative to current assays.

## Cross-cohort

`../combined/data/combined_catalog.json` merges both cohorts onto a unified modality
vocabulary — 507 rows, 502 unique samples. **Only 5 samples appear in both cohorts**:
HG00733, HG02818, HG03486, NA19036, NA19240. That near-disjointness is itself the
finding: the two consortia sequenced almost entirely different people, so the union
is close to additive and a cohort facet is genuinely informative rather than
cosmetic.

Unified vocabulary maps `ONT_UL`→`ONT`, and `Kinnex`/`IsoSeq`→`Kinnex_IsoSeq`, so
cross-cohort queries compare like with like. HPRC-only modalities (FiberSeq,
HiFi_5mC, MicroC, ONT_directRNA, Microarray, Karyotype, TenX) and HGSVC3-only ones
(RNAseq, PacBio_CLR, ChrY_ONT) both survive the merge with zero counts on the other
side — do not treat a zero as missing data, it means that consortium did not run
that assay.

## Regenerating

```bash
pip install pandas requests openpyxl
cd scripts && python build_hgsvc3_catalog.py \
  --xlsx /path/to/HGSVC_Production_Master-2.xlsx --out ../data
```

Takes about a minute, most of it crawling the archive.
