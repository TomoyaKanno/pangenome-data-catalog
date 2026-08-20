# HPRC Data Catalog — handoff bundle

Backing data for an interactive HPRC data-availability app. Everything here was
derived by enumerating `s3://human-pangenomics` directly; the human-pangenomics
GitHub org supplied sample metadata and explanations.

**Headline numbers:** 438 samples, 14 modalities, 19,807 data files, ~758 TB.

## Start here

`data/hprc_catalog.json` (114 KB) is the whole app payload. Ship it as a static
asset — no backend, no database. It contains per-sample modality lists, per-modality
totals, and precomputed intersection counts.

```js
const cat = await fetch('/data/hprc_catalog.json').then(r => r.json());

// browse by category
const fiberseq = cat.samples.filter(s => s.modalities.includes('FiberSeq'));

// search by sample
const byId = new Map(cat.samples.map(s => [s.sample_id, s]));
byId.get('HG002').modalities;

// combine modalities
const want = ['HiFi', 'ONT', 'FiberSeq'];
const all  = cat.samples.filter(s => want.every(m => s.modalities.includes(m)));
```

For download lists, `data/hprc_file_index.csv` has one row per file with a ready
`https_url`. It is 6 MB — load it lazily or split per modality at build time; do not
bundle it into the initial payload.

## Files

| File | What it is |
|---|---|
| `data/hprc_catalog.json` | App payload: samples, modality totals, intersections |
| `data/hprc_sample_modality_matrix.csv` | 438 × 14 booleans + release/population/sex |
| `data/hprc_file_index.csv` | 19,807 files: sample, modality, s3_key, https_url, size |
| `data/hprc_modality_intersections.csv` | Precomputed counts for 8 combinations |
| `data/hprc_assay_provenance.csv` | Per modality: GitHub coverage, caveats |
| `data/hprc_samples_not_in_any_release_table.csv` | 50 samples with data but no release row |
| `scripts/build_hprc_catalog.py` | Regenerates all of the above (~2 min) |

## The modalities

Core five, present for most samples: `HiFi` (402), `Illumina_WGS` (401), `ONT` (399),
`HiC_OmniC` (380), `Kinnex_IsoSeq` (368).

Everything else is sparse: `FiberSeq` (111), `HiFi_5mC` (86), `Bionano` (42),
`Karyotype` (40), `Microarray` (40), `TenX` (10), `StrandSeq` (8), `MicroC` (1),
`ONT_directRNA` (1).

**The most useful thing this data says:** 346 samples have all five core modalities,
but only 100 add Fiber-seq and only 44 also have HiFi 5mC. Any multi-modal analysis
is a ~100-sample study, not a 400-sample one. Make that falloff visible in the UI —
it is the main thing a user needs to learn.

## Things that will bite you

**GitHub index tables are not comprehensive.** They resolve about 10.3k of the 19.8k
data files. Eight modalities have no GitHub index at all — S3 is the only source.
`indexed_on_github=False` in the file index is a provenance note, not a quality
signal; it usually means the file postdates the last index refresh.

**`Stenciling_6mA` is not a modality.** Those ONT 6mA chromatin-stenciling basecalls
live inside the UCSC ONT Fiber-seq submission — they are a basecall variant of
Fiber-seq reads for 19 samples. They are tracked by `assay_detail` in the file index
and the `FiberSeq_ONT_stenciling` flag in the matrix. An earlier draft of this
analysis wrongly counted them separately; do not reintroduce that.

**Never filter S3 objects by file extension.** Enumerate exhaustively, classify
afterward. A sequencing-extension filter silently dropped Bionano, karyotype and
microarray entirely, plus HG002's Strand-seq (which is `.txt.gz`). The script's
docstring says this too, because it is the single easiest way to corrupt these numbers.

**Surface `release`, don't hide it.** Values are `R2`, `R3`, `untagged`. 50 samples
have primary data but no row in either release metadata table. Users will otherwise
conflate "HPRC has this data" with "this sample is in the release I am assembling
against," and those differ.

## Regenerating

```bash
pip install boto3 pandas requests
cd scripts && python build_hprc_catalog.py --out ../data
```

Anonymous S3 access, no credentials. Needs network access to
`human-pangenomics.s3.us-west-2.amazonaws.com` and `raw.githubusercontent.com`.

## Useful upstream sources

- `human-pangenomics/R3_HPRC` — 345-sample R3 cohort, per-sample coverage and N50
- `human-pangenomics/hprc_intermediate_assembly/data_tables/` — canonical R2 sequencing indexes, sample metadata
- `human-pangenomics/hprc-data-explorer` — the consortium's own explorer (Next.js); curated but only 232 samples and 5 assay types. Worth reading for schema ideas.
- `human-pangenomics/HPRC_metadata` — production metadata, SRA/BioProject mappings

## Next phase

HGSVC3 will be catalogued into this same schema and merged, sourced from the 1000G FTP
and a production spreadsheet. Expect a `cohort` column (`HPRC` / `HGSVC3`) to appear in
the matrix and file index; write the UI so a cohort facet can be added without a
schema rewrite.
