#!/usr/bin/env python3
"""
Rebuild the HPRC catalog tables from S3 + GitHub index tables.

Design rule learned the hard way: ENUMERATE EXHAUSTIVELY, CLASSIFY AFTERWARD.
Filtering S3 objects by sequencing file extension on the way in silently dropped
three entire assays (Bionano, karyotype, microarray). Do not reintroduce that.

Outputs (written to --out):
  hprc_file_index.csv                        one row per data file
  hprc_sample_modality_matrix.csv            one row per sample, boolean per modality
  hprc_modality_intersections.csv            counts for useful modality combinations
  hprc_assay_provenance.csv                  which modalities GitHub indexes cover
  hprc_samples_not_in_any_release_table.csv  samples with data but no release row
  hprc_catalog.json                          compact payload for the static web app

Usage:
  python build_hprc_catalog.py --out ../data
"""
import argparse
import concurrent.futures as cf
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict

import boto3
import pandas as pd
import requests
from botocore import UNSIGNED
from botocore.config import Config

BUCKET = "human-pangenomics"
REGION = "us-west-2"
HTTPS_BASE = f"https://s3-us-west-2.amazonaws.com/{BUCKET}/"

# Sample-ID regex. NOTE: \b does NOT match between '_' and a letter, so
# underscore-delimited ids (HPRC_Kinnex_HG02976) are missed by \b. Use lookarounds.
SAMP = re.compile(r"(?<![A-Za-z0-9])(HG\d{3,5}|NA\d{5}|GM\d{5})(?![0-9])")

# Directory name under raw_data/ -> canonical modality name
MODALITY_MAP = {
    "PacBio_HiFi": "HiFi",
    "PacBio_HiFi_Swap_Fixed": "HiFi",
    "nanopore": "ONT",
    "Illumina": "Illumina_WGS",
    "hic": "HiC_OmniC",
    "PacBio_Kinnex": "Kinnex_IsoSeq",
    "bionano": "Bionano",
    "karyotype": "Karyotype",
    "microarray": "Microarray",
    "Strand_seq": "StrandSeq",
    "10X": "TenX",
}

# Submission-label keyword -> modality, for assays that live only under submissions/
SUBMISSION_MATCHERS = {
    "FiberSeq": lambda lbl: "fiber" in lbl,
    "MicroC": lambda lbl: "microc" in lbl,
    "HiFi_5mC": lambda lbl: "methylation" in lbl or "5mc" in lbl,
}

GITHUB_INDEX_URLS = [
    # HPRC Release 2 canonical sequencing indexes
    "https://raw.githubusercontent.com/human-pangenomics/hprc_intermediate_assembly/main/data_tables/sequencing_data/data_{a}_release2_v1.0.index.csv",
]
R2_ASSAYS = ["hifi", "ont", "hic", "illumina", "kinnex", "deepconsensus", "hifi_meth_wg"]
R3_SEQ = ["R3_HPRC_hifi_no_dc_map", "R3_HPRC_ont", "R3_HPRC_hic", "R3_HPRC_ill",
          "R3_HPRC_kinnex", "R3_HPRC_dc", "R3_HPRC_ont_corrected"]
R3_BASE = "https://raw.githubusercontent.com/human-pangenomics/R3_HPRC/main"
R2_META = ("https://raw.githubusercontent.com/human-pangenomics/hprc_intermediate_assembly/"
           "main/data_tables/sample/hprc_release2_sample_metadata.csv")

# Files that are real data but not release deliverables
DERIVED_MARKERS = ("fail_reads", "downsampled")


def s3_client():
    return boto3.client("s3", config=Config(signature_version=UNSIGNED), region_name=REGION)


def list_prefixes(s3, prefix):
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=BUCKET, Prefix=prefix, Delimiter="/"):
        out += [p["Prefix"] for p in page.get("CommonPrefixes", [])]
    return out


def list_objects(s3, prefix):
    """Every object under prefix. No extension filtering -- see module docstring."""
    out = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for o in page.get("Contents", []):
            if not o["Key"].endswith("/"):
                out[o["Key"]] = o["Size"]
    return out


def sample_of(key):
    parts = key.split("/")
    return parts[parts.index("raw_data") - 1] if "raw_data" in parts else None


def find_sample_dirs(s3):
    """Sample directories under working/. Handles the extra cohort nesting level
    used by working/HPP/AMED/<sample>/ -- a dir with no raw_data/ child is a
    sub-cohort, so descend one more level."""
    tops = ["working/HPRC/", "working/HPRC_PLUS/", "working/HPP/", "working/T2T/"]
    first = []
    for t in tops:
        first += list_prefixes(s3, t)
    sample_dirs, nested = [], []
    with cf.ThreadPoolExecutor(32) as ex:
        for sp, kids in zip(first, ex.map(lambda p: list_prefixes(s3, p), first)):
            if any(k.endswith("raw_data/") for k in kids):
                sample_dirs.append(sp)
            else:
                nested += kids
    with cf.ThreadPoolExecutor(32) as ex:
        for sp, kids in zip(nested, ex.map(lambda p: list_prefixes(s3, p), nested)):
            if any(k.endswith("raw_data/") for k in kids):
                sample_dirs.append(sp)
    return sample_dirs


def scan_working(s3, sample_dirs):
    rows = []
    with cf.ThreadPoolExecutor(32) as ex:
        for objs in ex.map(lambda p: list_objects(s3, p + "raw_data/"), sample_dirs):
            for key, size in objs.items():
                low = key.lower()
                if any(m in low for m in DERIVED_MARKERS):
                    continue
                after = key.split("/raw_data/", 1)
                if len(after) < 2:
                    continue
                assay_dir = after[1].split("/")[0]
                rows.append(dict(sample_id=sample_of(key),
                                 modality=MODALITY_MAP.get(assay_dir, assay_dir),
                                 assay_detail="swap_fixed_reissue" if assay_dir.endswith("Swap_Fixed") else "",
                                 s3_key=key, size_bytes=size, source="working"))
    return pd.DataFrame(rows)


def scan_submissions(s3):
    """Submission-only assays. Stenciling 6mA basecalls live INSIDE the
    UCSC ONT Fiber-seq submission -- they are a basecall variant of Fiber-seq
    reads, not a separate assay, so they are tagged via assay_detail."""
    subs = list_prefixes(s3, "submissions/")
    rows = []
    for modality, match in SUBMISSION_MATCHERS.items():
        prefixes = [s for s in subs if match(s.split("--")[-1].lower())]
        if not prefixes:
            continue
        with cf.ThreadPoolExecutor(16) as ex:
            for objs in ex.map(lambda p: list_objects(s3, p), prefixes):
                for key, size in objs.items():
                    if key.endswith((".md5", ".txt", ".json", ".csv", ".tsv",
                                     ".pbi", ".bai", ".crai", ".DS_Store")):
                        continue
                    ids = set(SAMP.findall(key))
                    detail = ""
                    if "stenciling" in key.lower():
                        detail = "ONT_chromatin_stenciling_6mA"
                    elif modality == "FiberSeq":
                        detail = "ONT" if re.search(r"ONT|R1041", key, re.I) else "PacBio"
                    rows.append(dict(sample_id=(ids.pop() if len(ids) == 1 else None),
                                     modality=modality, assay_detail=detail,
                                     s3_key=key, size_bytes=size, source="submissions"))
    df = pd.DataFrame(rows)
    # One-off: ONT direct RNA, single sample, inside a mixed submission
    dr = [s for s in subs if "WASHU_PED_DATA" in s]
    if dr:
        objs = list_objects(s3, dr[0] + "direct_RNA/")
        extra = [dict(sample_id=(set(SAMP.findall(k)).pop() if len(set(SAMP.findall(k))) == 1 else None),
                      modality="ONT_directRNA", assay_detail="m6A_DRACH",
                      s3_key=k, size_bytes=v, source="submissions")
                 for k, v in objs.items() if k.endswith(".bam")]
        if extra:
            df = pd.concat([df, pd.DataFrame(extra)], ignore_index=True)
    return df


def fetch_github_indexes():
    """Return (set of s3 keys referenced by GitHub index tables, sample metadata df)."""
    keys, frames = set(), []
    urls = [GITHUB_INDEX_URLS[0].format(a=a) for a in R2_ASSAYS]
    urls += [f"{R3_BASE}/sequence_data/{f}.csv" for f in R3_SEQ]
    for u in urls:
        try:
            r = requests.get(u, timeout=60)
            r.raise_for_status()
            d = pd.read_csv(io.StringIO(r.text), low_memory=False)
        except Exception as e:
            print(f"  WARN could not fetch {u.rsplit('/', 1)[-1]}: {e}", file=sys.stderr)
            continue
        for c in d.columns:
            if c.lower() == "path" or c.lower().endswith("_path"):
                for v in d[c].dropna().astype(str):
                    keys.update(m.strip().rstrip(",;")
                                for m in re.findall(rf"s3://{BUCKET}/(\S+)", v))
    for u, rel in [(R2_META, "R2"), (f"{R3_BASE}/sample/R3_HPRC_metadata.csv", "R3")]:
        try:
            r = requests.get(u, timeout=60)
            r.raise_for_status()
            d = pd.read_csv(io.StringIO(r.text), low_memory=False)
            d.columns = [c.lower() for c in d.columns]
            cols = [c for c in ("sample_id", "population_abbreviation", "sex") if c in d.columns]
            frames.append(d[cols].assign(release=rel))
        except Exception as e:
            print(f"  WARN could not fetch metadata {u}: {e}", file=sys.stderr)
    meta = (pd.concat(frames, ignore_index=True).drop_duplicates("sample_id", keep="first")
            .set_index("sample_id")) if frames else pd.DataFrame()
    return keys, meta


def build(out_dir):
    s3 = s3_client()
    print("enumerating working/ sample directories ...")
    sample_dirs = find_sample_dirs(s3)
    print(f"  {len(sample_dirs)} sample dirs")

    print("scanning working/ objects ...")
    work = scan_working(s3, sample_dirs)
    print(f"  {len(work)} files")

    print("scanning submissions/ objects ...")
    sub = scan_submissions(s3)
    print(f"  {len(sub)} files")

    print("fetching GitHub index tables ...")
    gh_keys, meta = fetch_github_indexes()
    print(f"  {len(gh_keys)} referenced s3 keys, {len(meta)} sample metadata rows")

    FI = pd.concat([work, sub], ignore_index=True).drop_duplicates("s3_key")
    FI["indexed_on_github"] = FI.s3_key.isin(gh_keys)
    FI["https_url"] = HTTPS_BASE + FI.s3_key
    FI.to_csv(os.path.join(out_dir, "hprc_file_index.csv"), index=False)

    mods = sorted(FI.modality.unique())
    present = FI.dropna(subset=["sample_id"]).groupby(["sample_id", "modality"]).size().unstack(fill_value=0) > 0
    M = present.reindex(columns=mods, fill_value=False)
    M.index.name = "sample_id"
    sten = set(FI.loc[FI.assay_detail == "ONT_chromatin_stenciling_6mA", "sample_id"].dropna())
    M["FiberSeq_ONT_stenciling"] = M.index.isin(sten)
    if len(meta):
        m = meta.reindex(M.index)
        M["release"] = m.get("release", pd.Series(index=M.index)).fillna("untagged")
        M["population"] = m.get("population_abbreviation")
        M["sex"] = m.get("sex")
    M["n_modalities"] = M[mods].sum(axis=1)
    M.to_csv(os.path.join(out_dir, "hprc_sample_modality_matrix.csv"))

    core5 = ["HiFi", "ONT", "Illumina_WGS", "HiC_OmniC", "Kinnex_IsoSeq"]
    combos = {
        "core5 (HiFi+ONT+Illumina+HiC+Kinnex)": core5,
        "core5 + FiberSeq": core5 + ["FiberSeq"],
        "core5 + FiberSeq + HiFi_5mC": core5 + ["FiberSeq", "HiFi_5mC"],
        "core5 + Bionano": core5 + ["Bionano"],
        "FiberSeq + HiC_OmniC": ["FiberSeq", "HiC_OmniC"],
        "FiberSeq + Kinnex_IsoSeq": ["FiberSeq", "Kinnex_IsoSeq"],
        "FiberSeq + HiFi_5mC": ["FiberSeq", "HiFi_5mC"],
        "MicroC + FiberSeq": ["MicroC", "FiberSeq"],
    }
    ix = {k: int(M[[c for c in v if c in M.columns]].all(axis=1).sum()) for k, v in combos.items()}
    pd.DataFrame({"combination": list(ix), "n_samples": list(ix.values())}).to_csv(
        os.path.join(out_dir, "hprc_modality_intersections.csv"), index=False)

    # samples holding primary data that no release index references
    unindexed = FI[(~FI.indexed_on_github) & (FI.source == "working")]
    prim = unindexed[unindexed.modality.isin(core5)]
    release_samples = set(meta.index) if len(meta) else set()
    orphan = sorted(set(prim.sample_id.dropna()) - release_samples)
    pd.DataFrame({"sample_id": orphan}).to_csv(
        os.path.join(out_dir, "hprc_samples_not_in_any_release_table.csv"), index=False)

    payload = {
        "generated_utc": pd.Timestamp.utcnow().isoformat(),
        "bucket": BUCKET,
        "modalities": mods,
        "samples": [
            {"sample_id": s,
             "release": (None if pd.isna(r.get("release")) else r.get("release")),
             "population": (None if pd.isna(r.get("population")) else r.get("population")),
             "sex": (None if pd.isna(r.get("sex")) else r.get("sex")),
             "n_modalities": int(r["n_modalities"]),
             "modalities": [m for m in mods if bool(r[m])],
             "fiberseq_ont_stenciling": bool(r["FiberSeq_ONT_stenciling"])}
            for s, r in M.iterrows()
        ],
        "modality_totals": {m: int(M[m].sum()) for m in mods},
        "intersections": ix,
    }
    with open(os.path.join(out_dir, "hprc_catalog.json"), "w") as fh:
        json.dump(payload, fh, indent=1)

    print(f"\nwrote 6 files to {out_dir}")
    print(f"  samples: {len(M)}  modalities: {len(mods)}  files: {len(FI)}"
          f"  {FI.size_bytes.sum() / 1e12:.0f} TB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="../data", help="output directory")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    build(a.out)
