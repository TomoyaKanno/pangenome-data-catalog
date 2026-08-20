#!/usr/bin/env python3
"""Turn the handoff bundle into the static payloads the web app fetches.

Reads the per-cohort catalogues and file indexes in hprc/, hgsvc3/ and combined/
and writes site-data/ :

    catalog.json            one enriched payload: 507 rows, per-cohort metadata
                            merged in, plus per-sample file counts and bytes
    files/<SAMPLE>.json     per-sample download list, lazy-loaded on demand
    files/_unattributed.json  rows the archives do not attribute to a sample

Nothing here re-derives the science. It reshapes what the build scripts in
hprc/scripts and hgsvc3/scripts already produced. Rerun it after either of those.

Two rules inherited from the cohort READMEs, enforced by assertions below:

  * The unified vocabulary is the only thing the app filters on, but the file
    indexes still use the original per-cohort names. The MAP tables below are
    the single place that translation lives. If a modality appears in a file
    index and is missing from its MAP, the script fails rather than silently
    dropping those files.
  * A zero is not missing data. Cohort-specific modalities are recorded with an
    explicit `ran` flag per cohort so the UI can distinguish "this consortium
    did not run that assay" from "no samples matched your filter".
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict

HPRC_PREFIX = "https://s3-us-west-2.amazonaws.com/human-pangenomics/"
HGSVC_PREFIX = "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/"

# original per-cohort modality name -> unified vocabulary name
HPRC_MAP = {
    "HiC_OmniC": "HiC",
    "ONT": "ONT",
    "Kinnex_IsoSeq": "Kinnex_IsoSeq",
    "HiFi": "HiFi",
    "Illumina_WGS": "Illumina_WGS",
    "FiberSeq": "FiberSeq",
    "HiFi_5mC": "HiFi_5mC",
    "Bionano": "Bionano",
    "Karyotype": "Karyotype",
    "Microarray": "Microarray",
    "TenX": "TenX",
    "StrandSeq": "StrandSeq",
    "MicroC": "MicroC",
    "ONT_directRNA": "ONT_directRNA",
}
HGSVC_MAP = {
    "ONT_UL": "ONT",
    "Kinnex": "Kinnex_IsoSeq",
    "IsoSeq": "Kinnex_IsoSeq",
    "HiC": "HiC",
    "Illumina_WGS": "Illumina_WGS",
    "HiFi": "HiFi",
    "PacBio_CLR": "PacBio_CLR",
    "StrandSeq": "StrandSeq",
    "Bionano": "Bionano",
    "RNAseq": "RNAseq",
    "ChrY_ONT": "ChrY_ONT",
}


def read_csv(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def load_json(path):
    with open(path) as fh:
        return json.load(fh)


def build(root, out):
    combined = load_json(os.path.join(root, "combined/data/combined_catalog.json"))
    hprc = load_json(os.path.join(root, "hprc/data/hprc_catalog.json"))
    hgsvc = load_json(os.path.join(root, "hgsvc3/data/hgsvc3_catalog.json"))

    hprc_by_id = {s["sample_id"]: s for s in hprc["samples"]}
    hgsvc_by_id = {s["sample_id"]: s for s in hgsvc["samples"]}

    # ---- file indexes -> per-sample shards -------------------------------
    hprc_files = defaultdict(list)
    hprc_unattributed = []
    hprc_out_of_cohort = []
    for row in read_csv(os.path.join(root, "hprc/data/hprc_file_index.csv")):
        mod = HPRC_MAP.get(row["modality"])
        if mod is None:
            sys.exit(f"HPRC modality {row['modality']!r} missing from HPRC_MAP")
        rec = [
            mod,
            row["s3_key"],
            int(row["size_bytes"] or 0),
            1 if row["indexed_on_github"] == "True" else 0,
            row["assay_detail"],
            row["source"],
        ]
        sid = row["sample_id"].strip()
        if not sid:
            hprc_unattributed.append(rec)
        elif sid in hprc_by_id:
            hprc_files[sid].append(rec)
        else:
            hprc_out_of_cohort.append([sid] + rec)

    # The HGSVC3 catalogue names four samples GM19129/GM19320/GM20355/GM21487
    # while its own file index spells the same Coriell donors NA19129/NA20355/
    # NA21487. Without this alias those samples get an empty download list and
    # their files land in the unattributed pile. The two spellings never
    # co-occur, which is what makes the aliasing safe rather than a guess.
    alias = {}
    for sid in hgsvc_by_id:
        if sid.startswith("GM"):
            twin = "NA" + sid[2:]
            if twin not in hgsvc_by_id:
                alias[twin] = sid

    hgsvc_files = defaultdict(list)
    hgsvc_unattributed = []
    hgsvc_out_of_cohort = []
    for row in read_csv(os.path.join(root, "hgsvc3/data/hgsvc3_file_index.csv")):
        mod = HGSVC_MAP.get(row["modality"])
        if mod is None:
            sys.exit(f"HGSVC3 modality {row['modality']!r} missing from HGSVC_MAP")
        # keep the original name too: Kinnex and IsoSeq both fold into
        # Kinnex_IsoSeq, and the distinction matters when reading a file list
        rec = [mod, row["rel_path"], row["collection"], row["modality"]]
        sid = row["sample_id"].strip()
        sid = alias.get(sid, sid)
        if not sid:
            hgsvc_unattributed.append(rec)
        elif sid in hgsvc_by_id:
            hgsvc_files[sid].append(rec)
        else:
            # a real file on the HGSVC archive for a donor outside the
            # catalogued 69 (NA24385/HG002 and friends). Keep it visible.
            hgsvc_out_of_cohort.append([sid] + rec)

    files_dir = os.path.join(out, "files")
    os.makedirs(files_dir, exist_ok=True)
    for stale in os.listdir(files_dir):
        os.remove(os.path.join(files_dir, stale))

    # ---- enriched sample rows --------------------------------------------
    samples = []
    for row in combined["samples"]:
        sid = row["sample_id"]
        cohort = row["cohort"]
        rec = {
            "sample_id": sid,
            "cohort": cohort,
            "population": row.get("population") or "",
            "sex": row.get("sex") or "",
            "modalities": sorted(row["modalities"]),
            "in_both_cohorts": row.get("in_both_cohorts", False),
        }

        if cohort == "HPRC":
            src = hprc_by_id.get(sid, {})
            rec["release"] = src.get("release") or "untagged"
            rec["stenciling"] = bool(src.get("fiberseq_ont_stenciling"))
            recs = hprc_files.get(sid, [])
            rec["n_files"] = len(recs)
            rec["bytes"] = sum(r[2] for r in recs)
            # provenance: how many of this sample's files any GitHub index knows
            rec["n_indexed"] = sum(r[3] for r in recs)
        else:
            src = hgsvc_by_id.get(sid, {})
            rec["family_id"] = src.get("family_id") or ""
            rec["relation"] = src.get("relation") or ""
            rec["superpop"] = src.get("superpop") or ""
            rec["project_yr"] = src.get("project_yr") or ""
            rec["strandseq_libs"] = src.get("strandseq_libs")
            # evidence is keyed by ORIGINAL modality names; re-key to unified so
            # the UI can look it up with the same string it filters on
            evidence = {}
            for orig, val in (src.get("evidence") or {}).items():
                unified = HGSVC_MAP.get(orig, orig)
                # Kinnex + IsoSeq collide on Kinnex_IsoSeq; "both" wins over
                # a single-source flag so we never overstate absence
                if evidence.get(unified) in (None, val):
                    evidence[unified] = val
                elif "both" in (evidence[unified], val):
                    evidence[unified] = "both"
                else:
                    evidence[unified] = "mixed"
            rec["evidence"] = evidence
            recs = hgsvc_files.get(sid, [])
            rec["n_files"] = len(recs)
            rec["bytes"] = 0  # the IGSR crawl carries no sizes
        samples.append(rec)

        shard = {"sample_id": sid, "cohort": cohort}
        if cohort == "HPRC":
            shard["prefix"] = HPRC_PREFIX
            shard["cols"] = ["modality", "key", "bytes", "indexed_on_github",
                             "assay_detail", "source"]
            shard["files"] = hprc_files.get(sid, [])
        else:
            shard["prefix"] = HGSVC_PREFIX
            shard["cols"] = ["modality", "path", "collection", "orig_modality"]
            shard["files"] = hgsvc_files.get(sid, [])
        # the 5 dual-cohort samples get one shard per cohort
        name = f"{sid}.{cohort}.json" if row.get("in_both_cohorts") else f"{sid}.json"
        with open(os.path.join(files_dir, name), "w") as fh:
            json.dump(shard, fh, separators=(",", ":"))

    # ---- coverage, with "did this consortium run it" kept separate --------
    ran = {
        "HPRC": {HPRC_MAP[m] for m in hprc["modality_totals"]},
        "HGSVC3": {HGSVC_MAP[m] for m in hgsvc["modality_totals"]},
    }
    coverage = {}
    for mod in combined["unified_modalities"]:
        per = {}
        for cohort in ("HPRC", "HGSVC3"):
            n = sum(1 for s in samples
                    if s["cohort"] == cohort and mod in s["modalities"])
            per[cohort] = {"n": n, "ran": mod in ran[cohort]}
        coverage[mod] = per

    # Cross-check the merge against the per-cohort catalogues. This is a UNION
    # over samples, never a sum over modality totals: Kinnex and IsoSeq both map
    # to Kinnex_IsoSeq and all 12 IsoSeq samples are a subset of the 64 Kinnex
    # ones, so summing would claim 76 samples that do not exist.
    for cohort, cat, cmap in (("HPRC", hprc, HPRC_MAP), ("HGSVC3", hgsvc, HGSVC_MAP)):
        union = defaultdict(set)
        for s in cat["samples"]:
            for orig in s["modalities"]:
                union[cmap[orig]].add(s["sample_id"])
        for mod, per in coverage.items():
            expected = len(union.get(mod, ()))
            if per[cohort]["n"] != expected:
                sys.exit(f"coverage mismatch {cohort}/{mod}: "
                         f"derived {per[cohort]['n']} != cohort catalogue {expected}")

    # Every row of both file indexes must end up in exactly one bucket. This is
    # the check that caught the GM/NA alias collision; keep it.
    n_in = sum(len(read_csv(os.path.join(root, p))) for p in
               ("hprc/data/hprc_file_index.csv", "hgsvc3/data/hgsvc3_file_index.csv"))
    n_out = (sum(len(v) for v in hprc_files.values())
             + sum(len(v) for v in hgsvc_files.values())
             + len(hprc_unattributed) + len(hgsvc_unattributed)
             + len(hprc_out_of_cohort) + len(hgsvc_out_of_cohort))
    if n_in != n_out:
        sys.exit(f"file accounting mismatch: read {n_in}, bucketed {n_out}")

    payload = {
        "generated_utc": combined["generated_utc"],
        "cohorts": combined["cohorts"],
        "unique_samples": combined["unique_samples"],
        "dual_cohort_samples": combined["dual_cohort_samples"],
        "modalities": combined["unified_modalities"],
        "coverage": coverage,
        "intersections": {"HPRC": hprc["intersections"],
                          "HGSVC3": hgsvc["intersections"]},
        "totals": {
            "files": sum(s["n_files"] for s in samples),
            "bytes": sum(s["bytes"] for s in samples),
            "unattributed_files": len(hprc_unattributed) + len(hgsvc_unattributed),
            "out_of_cohort_files": len(hprc_out_of_cohort) + len(hgsvc_out_of_cohort),
        },
        "aliases": alias,
        "samples": samples,
    }
    with open(os.path.join(out, "catalog.json"), "w") as fh:
        json.dump(payload, fh, separators=(",", ":"))

    with open(os.path.join(files_dir, "_unattributed.json"), "w") as fh:
        json.dump({
            "note": "Files the archives do not tie to any catalogued sample. "
                    "'unattributed' rows carry no sample id at all and are mostly "
                    "pooled or multiplexed Strand-seq libraries. 'out_of_cohort' "
                    "rows name a donor who is not in the catalogued cohort - "
                    "NA24385 is GIAB's HG002. Both are real data.",
            "unattributed": {
                "HPRC": {"prefix": HPRC_PREFIX,
                         "cols": ["modality", "key", "bytes", "indexed_on_github",
                                  "assay_detail", "source"],
                         "files": hprc_unattributed},
                "HGSVC3": {"prefix": HGSVC_PREFIX,
                           "cols": ["modality", "path", "collection", "orig_modality"],
                           "files": hgsvc_unattributed},
            },
            "out_of_cohort": {
                "HPRC": {"prefix": HPRC_PREFIX,
                         "cols": ["sample_id", "modality", "key", "bytes",
                                  "indexed_on_github", "assay_detail", "source"],
                         "files": hprc_out_of_cohort},
                "HGSVC3": {"prefix": HGSVC_PREFIX,
                           "cols": ["sample_id", "modality", "path", "collection",
                                    "orig_modality"],
                           "files": hgsvc_out_of_cohort},
            },
        }, fh, separators=(",", ":"))

    cat_kb = os.path.getsize(os.path.join(out, "catalog.json")) / 1024
    print(f"catalog.json          {cat_kb:8.1f} KB  {len(samples)} rows")
    print(f"files/                {len(os.listdir(files_dir)):8d} shards")
    print(f"unattributed          {len(hprc_unattributed):8d} HPRC  "
          f"{len(hgsvc_unattributed)} HGSVC3")
    print(f"out of cohort         {len(hprc_out_of_cohort):8d} HPRC  "
          f"{len(hgsvc_out_of_cohort)} HGSVC3")
    print(f"GM/NA aliases applied {len(alias):8d}  {alias}")
    print(f"attributed files      {payload['totals']['files']:8d}")
    print(f"total bytes           {payload['totals']['bytes'] / 1e12:8.1f} TB (HPRC only)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    out = a.out or os.path.join(a.root, "site-data")
    os.makedirs(out, exist_ok=True)
    build(a.root, out)
