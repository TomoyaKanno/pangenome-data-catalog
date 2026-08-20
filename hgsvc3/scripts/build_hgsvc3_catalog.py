#!/usr/bin/env python3
"""
Rebuild the HGSVC3 catalog from the production spreadsheet + the IGSR archive.

Two independent sources, deliberately kept distinguishable:
  1. HGSVC_Production_Master*.xlsx, "Summary" sheet -- the consortium's own
     per-sample status matrix. Authoritative for INTENT and for data held
     elsewhere (e.g. Illumina at the Broad).
  2. https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/data_collections/HGSVC{2,3}/working/
     -- the actual files. Authoritative for what you can DOWNLOAD.

Every modality flag carries a `<modality>__src` column: "both", "sheet_only",
or "igsr_only". Never collapse that away -- "the sheet says yes but no file is
on the archive" is a real and common state, not an error.

NOTE ON PROTOCOL: the 1000G archive is reachable over HTTPS, not FTP (plain FTP
is commonly blocked). Use the https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/ prefix.

Usage:
  python build_hgsvc3_catalog.py --xlsx /path/to/HGSVC_Production_Master-2.xlsx --out ../data
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import sys

import pandas as pd
import requests

HTTP_BASE = "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/"
SAMP = re.compile(r"(?<![A-Za-z0-9])(HG\d{5}|NA\d{5})(?![0-9])")

# Spreadsheet "Summary" sheet: modality -> column label (group | header)
STATUS_COLS = {
    "Illumina_WGS": "Illumina | Illumina available?",
    "HiFi": "PacBio HiFi | Data generation complete?",
    "ONT_UL": "ONT Ultra-long | Data generation complete?",
    "StrandSeq": "Strand-Seq | Libraries (count)",
    "HiC": "Hi-C | Status",
    "Bionano": "BioNano | Status",
    "RNAseq": "RNAseq | Status",
    "IsoSeq": "Iso-Seq | Status",
    "Kinnex": "Kinnex - in progress | nan",
    "ChrY_ONT": "Y chr ONT | Status",
    "PacBio_CLR": "PacBio CLR | Coverage (X)",
}
MODALITIES = list(STATUS_COLS)

# Archive directory-name patterns. ORDER MATTERS: Kinnex dirs are named
# "..._HiFi_kinnex", so Kinnex must be tested before HiFi or it is absorbed.
DIR_PATTERNS = [
    ("Kinnex", r"[Kk]innex"),
    ("IsoSeq", r"Iso-Seq|IsoSeq"),
    ("RNAseq", r"RNAseq"),
    ("StrandSeq", r"Strandseq|Strand_seq"),
    ("Bionano", r"[Bb]ionano"),
    ("HiC", r"Hi-C|HiC|HIC"),
    ("ONT_UL", r"UL_ONT|ONT-UW|ONT_Rebasecalled|Rebasecalled|Nanopolish|JAX_ONT"),
    ("PacBio_CLR", r"PacBio_CLR|CLR"),
    ("Illumina_WGS", r"Illumina"),
    ("HiFi", r"HiFi|HIFI|CCS"),
]
DATA_EXT = (".fastq.gz", ".fq.gz", ".bam", ".ubam", ".cram", ".fasta.gz",
            ".fa.gz", ".h5", ".bnx.gz", ".cmap", ".tar.gz", ".txt.gz", ".vcf.gz")

# Excluded: a SEPARATE 718-sample pooled Strand-seq effort on the 1kGP panel
# (HGSVC Yr3 funded, overlapping the Miller ONT and 1KG_ONT_VIENNA cohorts).
# It is not the 69-sample HGSVC3 assembly cohort. Do not merge it in.
EXCLUDE_DIRS = ("Strandseq_Pools",)


def truthy(v):
    """Spreadsheet status cell -> bool. Values are free text: 'yes', 'no',
    'yes (HGSVC2_Yr2)', 'JAX - complete', 'UW - 17.9M FLNC', 'ignore', counts."""
    if pd.isna(v):
        return False
    s = str(v).strip().lower()
    if s in ("no", "nan", "", "ignore", "ignore?", "n/a", "-", "na"):
        return False
    return (s.startswith("yes") or "complete" in s
            or s.startswith(("jax", "uw", "broad", "rsii", "sqi"))
            or re.match(r"^[\d.]+$", s) is not None)


def lib_count(v):
    """Strand-seq library counts include sums ('14+32') and notes
    ('26 + cell grow issue'). Sum pure sums; else take the leading integer."""
    if pd.isna(v):
        return None
    s = str(v).strip()
    if re.match(r"^\s*\d+(\s*\+\s*\d+)*\s*$", s):
        return sum(int(x) for x in re.findall(r"\d+", s))
    m = re.match(r"^\s*(\d+)", s)
    return int(m.group(1)) if m else None


def read_summary(xlsx):
    """The Summary sheet has a two-row header: row 0 is a merged group label,
    row 1 the column name. Join them into 'group | header'."""
    raw = pd.read_excel(xlsx, sheet_name="Summary", header=None)
    group = raw.iloc[0].ffill()
    head = raw.iloc[1]
    cols = [f"{g} | {h}" if pd.notna(g) and str(g) != "nan" else str(h)
            for g, h in zip(group, head)]
    S = raw.iloc[2:].copy()
    S.columns = cols
    S = S[S["SampleID"].notna()].reset_index(drop=True)
    return S


def ls(path):
    """List an archive directory over HTTPS. Returns entry names (dirs end '/')."""
    try:
        r = requests.get(HTTP_BASE + path, timeout=90)
        if r.status_code != 200:
            return []
        return [n for n in re.findall(r'<a href="([^"?][^"]*)"', r.text)
                if not n.startswith("/") and n != "../"]
    except Exception as e:
        print(f"  WARN {path}: {e}", file=sys.stderr)
        return []


def classify_dir(name):
    for mod, pat in DIR_PATTERNS:
        if re.search(pat, name):
            return mod
    return None


def crawl(args, maxdepth=3):
    collection, dirname, modality = args
    out = []

    def rec(rel, depth):
        for item in ls(rel):
            if item.endswith("/"):
                if depth < maxdepth:
                    rec(rel + item, depth + 1)
            elif item.endswith(DATA_EXT):
                ids = set(SAMP.findall(item)) or set(SAMP.findall(rel))
                out.append(dict(cohort="HGSVC3", collection=collection,
                                modality=modality,
                                sample_id=(ids.pop() if len(ids) == 1 else None),
                                filename=item, rel_path=rel + item,
                                url=HTTP_BASE + rel + item))

    rec(f"data_collections/{collection}/working/{dirname}", 1)
    return out


def build(xlsx, out_dir):
    print("reading spreadsheet ...")
    S = read_summary(xlsx)
    roster = S["SampleID"].astype(str).str.strip()
    H = pd.DataFrame(index=pd.Index(roster, name="sample_id"))
    H["family_id"] = S["Sample Information | FamilyID"].values
    H["relation"] = S["Sample Information | Relation"].values
    H["population"] = S["Sample Information | Population"].values
    H["superpop"] = S["Sample Information | SuperPopulation"].values
    H["sex"] = S["Sample Information | Sex"].values
    H["project_yr"] = S["Project"].values
    H["StrandSeq_libs"] = [lib_count(v) for v in S[STATUS_COLS["StrandSeq"]]]
    sheet = {}
    for mod, col in STATUS_COLS.items():
        sheet[mod] = (H.StrandSeq_libs.notna().values if mod == "StrandSeq"
                      else S[col].map(truthy).values)
    print(f"  {len(H)} samples")

    print("enumerating IGSR archive ...")
    targets = []
    for coll in ("HGSVC3", "HGSVC2"):
        for d in ls(f"data_collections/{coll}/working/"):
            if not d.endswith("/") or any(x in d for x in EXCLUDE_DIRS):
                continue
            mod = classify_dir(d)
            if mod:
                targets.append((coll, d, mod))
    print(f"  {len(targets)} data directories")

    print("crawling for files (this takes a minute) ...")
    files = []
    with cf.ThreadPoolExecutor(10) as ex:
        for lst in ex.map(crawl, targets):
            files += lst
    FI = pd.DataFrame(files)
    FI.to_csv(os.path.join(out_dir, "hgsvc3_file_index.csv"), index=False)
    print(f"  {len(FI)} data files")

    archive = (FI.dropna(subset=["sample_id"]).groupby(["sample_id", "modality"])
               .size().unstack(fill_value=0) > 0) if len(FI) else pd.DataFrame()

    M = pd.DataFrame(index=H.index)
    for mod in MODALITIES:
        sh = pd.Series(sheet[mod], index=H.index).astype(bool)
        ar = (archive.reindex(H.index)[mod].fillna(False).astype(bool)
              if mod in getattr(archive, "columns", []) else pd.Series(False, index=H.index))
        M[mod] = sh | ar
        M[mod + "__src"] = ["both" if a and b else "sheet_only" if a
                            else "igsr_only" if b else ""
                            for a, b in zip(sh, ar)]
    for c in ("family_id", "relation", "population", "superpop", "sex",
              "project_yr", "StrandSeq_libs"):
        M[c] = H[c]
    M["n_modalities"] = M[MODALITIES].sum(axis=1)
    M.to_csv(os.path.join(out_dir, "hgsvc3_sample_modality_matrix.csv"))

    core4 = ["Illumina_WGS", "HiFi", "ONT_UL", "StrandSeq"]
    combos = {
        "core4 (Illumina+HiFi+ONT+StrandSeq)": core4,
        "core4 + HiC + Bionano": core4 + ["HiC", "Bionano"],
        "core4 + RNAseq": core4 + ["RNAseq"],
        "core4 + RNAseq + Kinnex": core4 + ["RNAseq", "Kinnex"],
        "assembly+3D+transcriptome (8)": core4 + ["HiC", "Bionano", "RNAseq", "Kinnex"],
        "RNAseq + Kinnex": ["RNAseq", "Kinnex"],
        "RNAseq + IsoSeq": ["RNAseq", "IsoSeq"],
    }
    ix = {k: int(M[v].all(axis=1).sum()) for k, v in combos.items()}
    pd.DataFrame({"combination": list(ix), "n_samples": list(ix.values())}).to_csv(
        os.path.join(out_dir, "hgsvc3_modality_intersections.csv"), index=False)

    payload = {
        "generated_utc": pd.Timestamp.utcnow().isoformat(),
        "cohort": "HGSVC3",
        "modalities": MODALITIES,
        "samples": [
            {"sample_id": s,
             "family_id": None if pd.isna(r.family_id) else str(r.family_id),
             "relation": None if pd.isna(r.relation) else r.relation,
             "population": None if pd.isna(r.population) else r.population,
             "superpop": None if pd.isna(r.superpop) else r.superpop,
             "sex": None if pd.isna(r.sex) else r.sex,
             "project_yr": None if pd.isna(r.project_yr) else str(r.project_yr),
             "strandseq_libs": None if pd.isna(r.StrandSeq_libs) else int(r.StrandSeq_libs),
             "n_modalities": int(r.n_modalities),
             "modalities": [m for m in MODALITIES if bool(r[m])],
             "evidence": {m: r[m + "__src"] for m in MODALITIES if r[m + "__src"]}}
            for s, r in M.iterrows()],
        "modality_totals": {m: int(M[m].sum()) for m in MODALITIES},
        "intersections": ix,
    }
    with open(os.path.join(out_dir, "hgsvc3_catalog.json"), "w") as fh:
        json.dump(payload, fh, indent=1)

    print(f"\nwrote 4 files to {out_dir}")
    print(f"  samples: {len(M)}  modalities: {len(MODALITIES)}  files: {len(FI)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", required=True, help="HGSVC_Production_Master*.xlsx")
    ap.add_argument("--out", default="../data")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    build(a.xlsx, a.out)
