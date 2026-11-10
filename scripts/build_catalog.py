#!/usr/bin/env python3
"""
Build the whole catalogue -- HPRC, HGSVC (phases 2 and 3) and same-donor data held by
other projects -- from the primary archives, then write the static payload the web app
fetches.

    python3 scripts/build_catalog.py --hgsvc-xlsx /path/to/HGSVC_Production_Master-2.xlsx

Only the Python standard library plus pandas/openpyxl (for the spreadsheet) is needed.
Every network read is cached under .cache/ ; pass --refresh to re-crawl. A cold run takes
~10 minutes, almost all of it listing 450k S3 keys and probing BAM headers.

Design rules, each learned from a defect in the previous build:

  * ENUMERATE EXHAUSTIVELY, CLASSIFY AFTERWARD. Filtering by extension on the way in
    silently drops whole assays (Bionano, karyotype, microarray, HG002 Strand-seq .txt.gz).
  * submissions/ IS NOT A STAGING COPY OF working/. Ten donors (the HG084xx whole-blood vs
    LCL pilot) have no working/ directory at all, and Pore-C, Micro-C, Fiber-seq, ONT direct
    RNA, Coriell karyotypes and most microarrays only exist under submissions/. Every
    raw-data submission is classified by the RULES table; anything unmatched is reported.
  * A SAMPLE ID CAN BE SPELLED THREE WAYS. Coriell lines appear as GM12345 and NA12345 in
    the same archive, and the GIAB donors as NA24385 / HG002. ids are canonicalised before
    anything is counted. The previous HGSVC regex ignored GM* entirely, which is why ~6k
    per-sample Strand-seq libraries looked "pooled".
  * METHYLATION IS USUALLY INLINE. PacBio Revio/jasmine and ONT dorado BAMs carry 5mC
    (and 5hmC) as MM/ML tags in the ordinary read files. Counting only dedicated
    "methylation" submissions undercounts it ~4x. probe_bam() reads the first few hundred
    KB of one or two BAMs per sample and looks for the tags.
  * PARENTS ARE NOT THE SAMPLE. working/<child>/raw_data/Illumina/parents/ holds the
    parents' reads. They are tracked as Illumina_parents, not as the child's Illumina_WGS.
"""
import argparse
import concurrent.futures as cf
import csv
import io
import json
import os
import re
import ssl
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".cache")

BUCKET = "human-pangenomics"
S3_LIST = f"https://{BUCKET}.s3.us-west-2.amazonaws.com/"
S3_HTTPS = f"https://s3-us-west-2.amazonaws.com/{BUCKET}/"
IGSR = "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/"
GH = "https://raw.githubusercontent.com/human-pangenomics/"

# --------------------------------------------------------------------------- ids

ID_RE = re.compile(r"(?<![A-Za-z0-9])(HG\d{3,5}|NA\d{5}|GM\d{5})(?![0-9])")
# GIAB donors carry a Coriell id and a GIAB id. HPRC uses the GIAB one, HGSVC the Coriell.
GIAB = {"NA12878": "HG001", "NA24149": "HG003", "NA24143": "HG004", "NA24385": "HG002",
        "NA24631": "HG005", "NA24694": "HG006", "NA24695": "HG007"}


def canon(sid):
    """GM12345 -> NA12345 -> GIAB id where one exists. None stays None."""
    if not sid:
        return None
    sid = sid.strip()
    m = re.match(r"^(NA\d{5}|HG\d{3,5}|GM\d{5})(?![0-9])", sid)
    if not m:
        return None
    sid = m.group(1)
    if sid.startswith("GM"):
        sid = "NA" + sid[2:]
    return GIAB.get(sid, sid)


def ids_in(text):
    return {canon(x) for x in ID_RE.findall(text)}


# --------------------------------------------------------------------------- http

_CTX = None


def _ctx():
    global _CTX
    if _CTX is None:
        _CTX = ssl.create_default_context()
        # python.org builds on macOS ship without a CA bundle; fall back to the system one
        for ca in ("/etc/ssl/cert.pem", "/usr/local/etc/openssl/cert.pem"):
            if os.path.exists(ca):
                try:
                    _CTX.load_verify_locations(ca)
                except Exception:
                    pass
    return _CTX


def http_get(url, rng=None, timeout=120, tries=4):
    hdr = {"User-Agent": "pangenome-data-catalog/2"}
    if rng:
        hdr["Range"] = f"bytes={rng[0]}-{rng[1]}"
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=hdr),
                                        timeout=timeout, context=_ctx()) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                raise
            time.sleep(2 ** i)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 ** i)


def cached(name, fn, refresh=False):
    path = os.path.join(CACHE, name)
    if os.path.exists(path) and not refresh:
        with open(path) as fh:
            return json.load(fh)
    val = fn()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(val, fh)
    return val


def get_text(url, cache_name, refresh=False):
    return cached(cache_name, lambda: http_get(url).decode("utf-8", "replace"), refresh)


# --------------------------------------------------------------------------- S3

NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def s3_page(prefix, token=None, delimiter=None):
    q = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
    if token:
        q["continuation-token"] = token
    if delimiter:
        q["delimiter"] = delimiter
    x = ET.fromstring(http_get(S3_LIST + "?" + urllib.parse.urlencode(q)))
    objs = [(c.find(NS + "Key").text, int(c.find(NS + "Size").text))
            for c in x.findall(NS + "Contents")]
    pre = [c.find(NS + "Prefix").text for c in x.findall(NS + "CommonPrefixes")]
    nxt = x.find(NS + "NextContinuationToken")
    return objs, pre, (nxt.text if nxt is not None else None)


def s3_prefixes(prefix):
    out, tok = [], None
    while True:
        _, pre, tok = s3_page(prefix, tok, "/")
        out += pre
        if not tok:
            return out


def s3_walk(prefix):
    out, tok = [], None
    while True:
        objs, _, tok = s3_page(prefix, tok)
        out += [o for o in objs if not o[0].endswith("/")]
        if not tok:
            return out


def s3_list(top, refresh):
    """Every object under top (e.g. 'working/'), parallelised two prefix levels down."""
    def go():
        lvl1 = s3_prefixes(top)
        lvl2 = []
        with cf.ThreadPoolExecutor(32) as ex:
            for p, kids in zip(lvl1, ex.map(s3_prefixes, lvl1)):
                lvl2 += kids or [p]
        objs = s3_walk_flat(top, lvl2)
        with cf.ThreadPoolExecutor(32) as ex:
            for lst in ex.map(s3_walk, lvl2):
                objs += lst
        return sorted(set(map(tuple, objs)))
    return cached(f"s3_{top.strip('/')}.json", go, refresh)


def s3_walk_flat(top, lvl2):
    """Objects sitting directly in a level-1 prefix are not under any level-2 prefix."""
    lvl1 = {p.rsplit("/", 2)[0] + "/" for p in lvl2}
    out = []
    for p in lvl1 | {top}:
        tok = None
        while True:
            objs, _, tok = s3_page(p, tok, "/")
            out += [o for o in objs if not o[0].endswith("/")]
            if not tok:
                break
    return out


# --------------------------------------------------------------------------- BAM probe


def bgzf_inflate(buf):
    out, i = bytearray(), 0
    while i + 18 <= len(buf) and buf[i:i + 4] == b"\x1f\x8b\x08\x04":
        xlen = struct.unpack("<H", buf[i + 10:i + 12])[0]
        bsize, j = None, i + 12
        while j < i + 12 + xlen:
            slen = struct.unpack("<H", buf[j + 2:j + 4])[0]
            if buf[j] == 66 and buf[j + 1] == 67:
                bsize = struct.unpack("<H", buf[j + 4:j + 6])[0]
            j += 4 + slen
        if bsize is None or i + bsize + 1 > len(buf):
            break
        try:
            out += zlib.decompress(buf[i + 12 + xlen:i + bsize + 1 - 8], -15)
        except zlib.error:
            break
        i += bsize + 1
    return bytes(out)


def probe_bam(url):
    """Read the head of a BAM: does it carry MM base-modification tags, kinetics, and
    which programs wrote it. A negative only means the first ~1k reads had no tags."""
    try:
        d = bgzf_inflate(http_get(url, rng=(0, 400000), timeout=90, tries=3))
    except Exception as e:
        return {"err": type(e).__name__}
    if not d.startswith(b"BAM\x01"):
        return {"err": "not_bam"}
    lt = struct.unpack("<i", d[4:8])[0]
    hdr, body = d[8:8 + lt].decode("latin1", "replace"), d[8 + lt:]
    return {
        # MM:Z:<base><strand><code>[.?],<deltas>;<next mod>... -- every segment, not just the first
        "mods": sorted({m.decode() for tag in re.findall(rb"MMZ([^\x00]*)\x00", body)
                        for m in re.findall(rb"(?:^|;)([ACGTN][+-][a-z0-9]+)", tag)}),
        "kinetics": any(t in body for t in (b"fiB", b"fiZ", b"fpB", b"ipB")),
        "pg": sorted(set(re.findall(r"@PG\tID:([A-Za-z_]+)", hdr)))[:12],
        "n": len(body),
    }


def probe_many(urls, name, refresh):
    def go():
        res = {}
        with cf.ThreadPoolExecutor(24) as ex:
            for u, r in zip(urls, ex.map(probe_bam, urls)):
                res[u] = r
        return res
    old = {} if refresh else cached(name, dict)
    todo = [u for u in urls if u not in old]
    if todo:
        print(f"  probing {len(todo)} BAM heads ...", flush=True)
        with cf.ThreadPoolExecutor(24) as ex:
            for u, r in zip(todo, ex.map(probe_bam, todo)):
                old[u] = r
        with open(os.path.join(CACHE, name), "w") as fh:
            json.dump(old, fh)
    return old


def has_5mc(r):
    return bool(r) and any(m.startswith("C+m") for m in r.get("mods", []))


def has_6ma(r):
    return bool(r) and any(m.startswith("A+a") for m in r.get("mods", []))


def has_5hmc(r):
    return bool(r) and any(m.startswith("C+h") for m in r.get("mods", []))


# =========================================================================== HPRC

WORKING_ASSAY = {
    "PacBio_HiFi": "HiFi", "PacBio_HiFi_Swap_Fixed": "HiFi", "nanopore": "ONT",
    "Illumina": "Illumina_WGS", "hic": "HiC", "PacBio_Kinnex": "Kinnex_IsoSeq",
    "bionano": "Bionano", "karyotype": "Karyotype", "microarray": "Microarray",
    "Strand_seq": "StrandSeq", "10X": "TenX",
}
DERIVED_MARKERS = ("fail_reads", "downsampled")
# never a deliverable on its own: checksums, transfer manifests, plots, logs
JUNK = re.compile(r"(\.md5|\.log|\.png|\.svg|\.html|\.stdout|\.stderr|\.swp|\.DS_Store|"
                  r"release-transfer-manifests/.*|\.json|\.fofn)$", re.I)
SIDECAR = re.compile(r"\.(pbi|bai|crai|csi|tbi|fai|gzi)$", re.I)
REPORT = re.compile(r"\.(txt|csv|tsv|stats|md|pdf|xml|out|readlen|summary)$|summary[^/]*\.txt\.gz$",
                    re.I)
# submissions/ mixes raw reads with analyses, so it gets an allowlist (working/ does not)
SUB_RAW = re.compile(r"\.(bam|cram|ubam|fastq|fastq\.gz|fq\.gz|pod5|fast5|tar|tar\.gz|bnx|bnx\.gz|"
                     r"cmap|idat|pdf|txt\.gz)$|\.(pod5|fast5)\.tar$", re.I)

# Raw-data submissions, first match wins. (label regex, path regex or None, modality,
# assay_detail, tissue). Submissions not listed here and not matched by SKIP are printed
# as UNCLASSIFIED so a new data type cannot slip in unnoticed.
RULES = [
    (r"", r"PoreC", "PoreC", "ONT Pore-C", ""),
    (r"", r"direct_RNA", "ONT_directRNA", "RNA004 m6A", ""),
    (r"MicroC", None, "MicroC", "", ""),
    (r"EnTEX", None, "ONT", "EnTEx tissue", "tissue"),
    (r"Colon", None, "HiFi", "colon tissue", "tissue"),
    (r"hiPSC", None, "FiberSeq", "ONT", "iPSC"),
    (r"fiber", None, "FiberSeq", "", ""),
    (r"^HG002-IsoSeq$", None, "Kinnex_IsoSeq", "Iso-Seq", ""),
    (r"^(?!KINNEX_DOWNSTREAM|YALE_KINNEX).*kinnex", None, "Kinnex_IsoSeq", "Kinnex", ""),
    (r"methyl|5mC|_CPG", None, "HiFi_5mC", "methylation submission", ""),
    (r"CORIELL_CHOP", r"(?i)/kary\w*\s*/", "Karyotype", "Coriell", ""),
    (r"CORIELL_CHOP", r"(?i)/micro\w*\s*/", "Microarray", "Coriell", ""),
    (r"CORIELL_CHOP", r"\.idat$", "Microarray", "Coriell", ""),
    (r"CORIELL_CHOP", r"\.pdf$", "Karyotype", "Coriell", ""),
    (r"CORIELL_WB_LCL_PILOT", r"/WB/", "HiFi", "whole blood, aligned", "blood"),
    (r"CORIELL_WB_LCL_PILOT", r"/LCL/", "HiFi", "LCL, aligned", ""),
    (r"hic_and_parents", r"\.cram$", "Illumina_WGS", "1000G parent", ""),
    (r"omni-?c|hic|hi-c", None, "HiC", "", ""),
    (r"strandseq", None, "StrandSeq", "", ""),
    (r"^WB_LCL_PILOT$", None, "HiC", "Omni-C", ""),
    (r"WASHU_PED_DATA", r"/PacBio/blood/", "HiFi_5mC", "blood", "blood"),
    (r"WASHU_PED_DATA", r"/DeepConsensus/(blood|HG\d+/v[\d.]+_blood)", "HiFi", "DeepConsensus, blood", "blood"),
    (r"WASHU_PED_DATA", r"/DeepConsensus/cell", "HiFi", "DeepConsensus", ""),
    (r"", r"/(Ilmn|illumina|Illumina)[^/]*/", "Illumina_WGS", "", ""),
    (r"BLOOD", None, "HiFi", "blood", "blood"),
    (r"_Blood$", None, "HiFi", "whole blood", "blood"),
    (r"WUSTL_CELL_HIFI", None, "HiFi", "", ""),
    (r"", r"/(bionano|Bionano)/", "Bionano", "", ""),
    (r"bionano", None, "Bionano", "", ""),
    (r"", r"/(Ilmn|illumina|Illumina)[^/]*/", "Illumina_WGS", "", ""),
    (r"", r"/PacBio_HiFi/", "HiFi", "", ""),
    (r"", r"\.(fast5|pod5)(\.tar)?$|_R1041_|ubam$", "ONT", "", ""),
    (r"nanopore|ONT|R1041|PromPlus|DUPLEX|MITEN|SHASTA|_PED_DATA|^UCSC_", None, "ONT", "", ""),
    (r"HiFi|HIFI|REVIO|DEEPCONS|SUBREADS|sequel|^RU_Y\d|TOPUP|TopUp|topoff|25X|READSBAM4DC|"
     r"AMED_HPRC|TECHNOPOLE_DEEP|UW_reads_", None, "HiFi", "", ""),
    (r"Illumina|ILMN|1000G|TRUSEQ|_ILL_|HG005_CHILD|HPRC_PLUS_GIAB|EXTRAMURAL_SAMPLES|"
     r"FILT_FASTQ", None, "Illumina_WGS", "", ""),
]
# Analyses, assemblies, alignments, variant calls, QC -- not primary data.
SKIP = re.compile(
    r"ASSEM|ASM|_asms|verkko|hifiasm|QC|NUCFLAG|FLAGGER|Flagger|ALIGNMENT|Alignments|"
    r"MAPPINGS|COVERAGE|VARIANT|Variant|DEEPVARIANT|PanGenie|pangenie|PANGENOME|_mc_|"
    r"REPEAT|repeats|RM_BEDS|SEDEF|TRF|HSAT|HUMAS|DNA_BRNN|VERITYMAP|Y1-BIGCHAINS|CAT_|"
    r"ENSEMBL|GENBANK|MEI|CpG_Island|SV_Calls|KaryoScope|nonBDNA|pclai|limma|KINNEX_DOWN|"
    r"YALE_KINNEX|terra_data|flagger_test|BIN_AND_YAK|YAKS|MERYL|panpatch|PANMITO|"
    r"Polishing|polishing|GIRAFFE|READ_STATS|benchmark|Evaluation|HMM|cleaned|commons|"
    r"merfin|RU-nanopore-HG002$|RU-bionano|HG00119_decontaminated|R3_ONT_hifiasm|herro|"
    r"testset|MSSM-PSR|DATA_USE|Primrose_Y1|PSU-|INT_|F1_|TEST|REASSEMBLY|DORADO-DUPLEX|"
    r"HIFI-METHYLATION-SUMMARY|CHM13-HIFI|HG002-METHYLATION|DEEPCONSENSUS_v1pt2$|"
    r"HPRC_Release2_Primrose|RU_Y4$|WUSTL_HPRC_HiFi_Y4$|WB_LCL_PILOT_V2|15:31", re.I)


def classify_submission(label, key):
    for lab_re, path_re, mod, detail, tissue in RULES:
        if lab_re and not re.search(lab_re, label, re.I):
            continue
        if path_re and not re.search(path_re, key):
            continue
        return mod, detail, tissue
    return None


def hprc_sample_dirs(working):
    """working/<group>[/<subgroup>]/<sample>/raw_data/... -> {sample_dir_path: sample}"""
    out = {}
    for k, _ in working:
        if "/raw_data/" in k:
            d = k.split("/raw_data/")[0]
            out[d] = d.rsplit("/", 1)[1]
    return out


def fetch_hprc_metadata(refresh):
    """GitHub release tables: (keys any index references, per-sample metadata)."""
    r2 = ["hifi", "ont", "hic", "illumina", "kinnex", "deepconsensus", "hifi_meth_wg"]
    r3 = ["R3_HPRC_hifi_no_dc_map", "R3_HPRC_ont", "R3_HPRC_hic", "R3_HPRC_ill",
          "R3_HPRC_kinnex", "R3_HPRC_dc", "R3_HPRC_ont_corrected"]
    urls = [f"{GH}hprc_intermediate_assembly/main/data_tables/sequencing_data/"
            f"data_{a}_release2_v1.0.index.csv" for a in r2]
    urls += [f"{GH}R3_HPRC/main/sequence_data/{f}.csv" for f in r3]
    keys = set()
    for u in urls:
        try:
            t = get_text(u, "gh/" + u.rsplit("/", 1)[1] + ".json", refresh)
        except Exception as e:
            print(f"  WARN {u.rsplit('/', 1)[1]}: {e}", file=sys.stderr)
            continue
        keys.update(m.rstrip(",;\"'") for m in re.findall(rf"s3://{BUCKET}/([^\s,;\"']+)", t))
    meta = {}
    for u, rel in [(f"{GH}R3_HPRC/main/sample/R3_HPRC_metadata.csv", "R3"),
                   (f"{GH}hprc_intermediate_assembly/main/data_tables/sample/"
                    "hprc_release2_sample_metadata.csv", "R2")]:
        t = get_text(u, "gh/" + u.rsplit("/", 1)[1] + ".json", refresh)
        for row in csv.DictReader(io.StringIO(t)):
            row = {k.lower(): v for k, v in row.items() if k}
            sid = canon(row.get("sample_id"))
            if not sid:
                continue
            m = meta.setdefault(sid, {"releases": set()})
            m["releases"].add(rel)
            for f_in, f_out in (("population_abbreviation", "population"), ("sex", "sex"),
                                ("superpopulation", "superpop"),
                                ("superpopulation_abbreviation", "superpop")):
                if row.get(f_in) and not m.get(f_out):
                    m[f_out] = row[f_in]
    return keys, meta


# Folder names in the bucket that are a different spelling of a catalogued donor.
# NA08441: submissions/*--WB_LCL_PILOT/NA08441/ holds Omni-C with the same library and
# flowcell ids (LIB141069, 23HY3FLT3) as WB_LCL_PILOT_OMNIC/HG08441/ -- the same sample.
HPRC_ALIASES = {"NA08441": "HG08441"}


def build_hprc(refresh, hgsvc_ids=frozenset()):
    """hgsvc_ids: donors on the HGSVC roster. HPRC centres sequenced some of them, in
    submissions labelled *HGSVC*; those files are credited to the HGSVC record. The label
    alone is not enough -- the same submissions also contain HPRC-only donors."""
    print("HPRC: listing s3://human-pangenomics ...", flush=True)
    working = s3_list("working/", refresh)
    subs = s3_list("submissions/", refresh)
    print(f"  {len(working):,} working/ keys, {len(subs):,} submissions/ keys")
    gh_keys, meta = fetch_hprc_metadata(refresh)

    rows = []
    roster = {}            # canonical id -> working dir name (or None)
    for d, raw in hprc_sample_dirs(working).items():
        sid = canon(raw) or raw   # T2T dirs such as mHomSap3 keep their own name
        roster[sid] = raw

    # ---- working/ --------------------------------------------------------
    for key, size in working:
        if "/raw_data/" not in key or JUNK.search(key):
            continue
        if any(m in key.lower() for m in DERIVED_MARKERS):
            continue
        d, rest = key.split("/raw_data/", 1)
        raw = d.rsplit("/", 1)[1]
        assay = rest.split("/")[0]
        mod = WORKING_ASSAY.get(assay, assay)
        detail = "swap_fixed_reissue" if assay.endswith("Swap_Fixed") else ""
        if mod == "Illumina_WGS" and "/parents/" in rest:
            mod, detail = "Illumina_parents", "trio parent"
        rows.append(dict(sample_id=canon(raw) or raw, modality=mod, assay_detail=detail,
                         tissue="", s3_key=key, size_bytes=size, source="working",
                         for_hgsvc=False, submission="", sub_dir=""))

    # ---- submissions/ ----------------------------------------------------
    # every id that appears as a folder in a raw-data submission, so a PG-prefixed folder
    # can be resolved to HG or NA no matter which submission is read first
    known = set(roster)
    for key, _ in subs:
        if not SKIP.search(key.split("/")[1].split("--", 1)[-1]):
            for seg in key.split("/")[2:-1]:
                if ID_RE.fullmatch(seg.split("_")[0]):
                    known.add(HPRC_ALIASES.get(canon(seg), canon(seg)))
    have = {(r["sample_id"], os.path.basename(r["s3_key"])) for r in rows}
    unclassified, other_donor = Counter(), []
    for key, size in subs:
        sub = key.split("/")[1]
        label = sub.split("--", 1)[-1]
        if SKIP.search(label) or JUNK.search(key) or not SUB_RAW.search(key):
            continue
        if any(m in key.lower() for m in DERIVED_MARKERS):
            continue
        c = classify_submission(label, key)
        if c is None:
            unclassified[label] += 1
            continue
        mod, detail, tissue = c
        if mod not in ("Karyotype",) and key.lower().endswith(".pdf"):
            continue   # Fiber-seq QC reports etc.
        if mod == "FiberSeq" and not detail:
            detail = "ONT" if re.search(r"ONT|R1041", label + key, re.I) else "PacBio"
        if "stenciling" in key.lower():
            detail = "ONT chromatin stencilling 6mA"
        if "iPSC" in key:
            tissue = "iPSC"
        # attribution: a path segment that IS an id beats ids buried in a filename
        segs = key.split("/")[2:]
        seg_ids = [canon(s) for s in segs[:-1] if canon(s) and ID_RE.fullmatch(s.split("_")[0])]
        file_ids = ids_in(segs[-1])
        sid = seg_ids[-1] if seg_ids else (next(iter(file_ids)) if len(file_ids) == 1 else None)
        if sid is None:   # run folders such as 05_04_26_PRO114P_UL_HPRC_HG00642_1/bam_pass/
            dir_ids = ids_in("/".join(segs[:-1]))
            sid = next(iter(dir_ids)) if len(dir_ids) == 1 else None
        note = ""
        pg = re.search(r"/PG(\d{5})[^/]*/", key) if sid is None else None
        if pg:   # Coriell/UW lab spelling of the same line: PG18523 = NA18523, PG08434 = HG08434
            sid = next((c for c in ("HG" + pg.group(1), "NA" + pg.group(1)) if c in known),
                       "NA" + pg.group(1))
            note = f"named PG{pg.group(1)}"
        if sid in HPRC_ALIASES:
            note, sid = f"folder named {sid}", HPRC_ALIASES[sid]
        if seg_ids and file_ids and seg_ids[-1] not in file_ids:
            note = f"filename names {'/'.join(sorted(file_ids))}"
        if sid is None:
            donor = [d for d in re.findall(r"(MGISTL-PAN\d+|PAN\d{3}|EnTEX_[A-Z0-9]+|SM-[A-Z0-9]+)",
                                           "/".join(key.split("/")[2:])) if d != "EnTEX_ONT"]
            if donor:
                other_donor.append(dict(donor=donor[0], submission=label, modality=mod,
                                        assay_detail=detail, tissue=tissue, s3_key=key,
                                        size_bytes=size))
                continue
        if (sid, os.path.basename(key)) in have:
            continue   # the same file was promoted to working/
        rows.append(dict(sample_id=sid, modality=mod,
                         assay_detail="; ".join(x for x in (detail, note) if x),
                         tissue=tissue, s3_key=key, size_bytes=size, source="submissions",
                         for_hgsvc=bool(re.search(r"HGSVC", label)) and sid in hgsvc_ids,
                         submission=label, sub_dir=sub))

    if unclassified:
        print("  UNCLASSIFIED submissions (add a RULES or SKIP entry):", file=sys.stderr)
        for lbl, n in unclassified.most_common():
            print(f"    {lbl}: {n}", file=sys.stderr)

    # Long-read data puts a donor in the catalogue. Illumina-only / QC-only ids are 1000G
    # parents and Coriell candidates that were screened but never sequenced.
    LONG = {"HiFi", "ONT", "FiberSeq", "Kinnex_IsoSeq", "HiC", "HiFi_5mC", "PoreC", "MicroC",
            "ONT_directRNA"}
    for r in rows:
        if (r["source"] == "submissions" and r["sample_id"] and r["modality"] in LONG
                and not r["for_hgsvc"]):
            roster.setdefault(r["sample_id"], None)

    # ---- inline methylation -------------------------------------------------
    pick = defaultdict(list)
    for r in rows:
        k = r["s3_key"]
        if ((r["sample_id"] in roster or (r["for_hgsvc"] and r["sample_id"]))
                and r["modality"] in ("HiFi", "ONT") and k.endswith(".bam") and "stencil" not in k):
            pick[(r["sample_id"], r["modality"], r["tissue"], r["for_hgsvc"])].append(S3_HTTPS + k)
    # first, last and the newest-chemistry BAM: old Sequel runs predate inline 5mC
    for g, lst in pick.items():
        lst.sort()
        revio = [u for u in lst if "/m84" in u or "_m84" in u or "R1041" in u]
        pick[g] = list(dict.fromkeys([lst[0], lst[-1]] + revio[-1:]))
    probe = probe_many(sorted(u for v in pick.values() for u in v), "probe_hprc.json", refresh)
    inline = defaultdict(set)   # (sid, 'HiFi_5mC'|'ONT_5mC') -> {"5mC","5hmC"}
    inline_hgsvc = defaultdict(set)
    probe_rows = []
    for (sid, mod, tissue, for_hgsvc), urls in pick.items():
        for u in urls:
            p = probe.get(u, {})
            probe_rows.append(dict(sample_id=sid, modality=mod, tissue=tissue, url=u,
                                   mods=",".join(p.get("mods", [])), kinetics=p.get("kinetics"),
                                   programs=",".join(p.get("pg", [])), error=p.get("err", "")))
            tgt = "HiFi_5mC" if mod == "HiFi" else "ONT_5mC"
            box = inline_hgsvc if for_hgsvc else inline
            if has_5mc(p):
                box[(sid, tgt)].add("5mC")
            if has_5hmc(p):
                box[(sid, tgt)].add("5hmC")
            if has_6ma(p):
                box[(sid, "FiberSeq")].add("6mA")

    # ---- matrix ----------------------------------------------------------
    for r in rows:
        r["in_catalogue"] = r["sample_id"] in roster and not r["for_hgsvc"]
        r["indexed_on_github"] = r["s3_key"] in gh_keys
        r["kind"] = ("index" if SIDECAR.search(r["s3_key"]) else
                     "report" if REPORT.search(r["s3_key"]) and r["modality"] != "Karyotype"
                     else "data")
    samples = {}
    for sid in roster:
        m = meta.get(sid, {})
        rel = sorted(m.get("releases", []))
        samples[sid] = dict(sample_id=sid, cohort="HPRC", aka=roster[sid] if roster[sid] != sid else "",
                            population=m.get("population", ""), sex=(m.get("sex") or "").lower(),
                            superpop=m.get("superpop", ""),
                            release="+".join(rel) or "untagged",
                            modalities=set(), evidence={}, tissues=set())
    for r in rows:
        s = samples.get(r["sample_id"])
        if s is None or r["kind"] != "data" or r["for_hgsvc"]:
            continue   # a data type is credited only where its files are listed
        s["modalities"].add(r["modality"])
        if r["tissue"]:
            s["tissues"].add(r["tissue"])
        if r["modality"] == "HiFi_5mC":
            s["evidence"]["HiFi_5mC"] = "dedicated"
    for (sid, mod), marks in inline.items():
        if sid not in samples:
            continue
        s = samples[sid]
        if mod == "FiberSeq":
            s["modalities"].add(mod)
            if "FiberSeq" not in s["evidence"]:
                s["evidence"]["FiberSeq"] = "inline m6A"
            continue
        if mod == "HiFi_5mC" and "5mC" not in marks:
            continue
        s["modalities"].add(mod)
        prev = s["evidence"].get(mod)
        s["evidence"][mod] = "inline+dedicated" if prev == "dedicated" else "inline"
        if mod == "ONT_5mC" and "5hmC" in marks:
            s["evidence"]["ONT_5mC"] += "+5hmC"
    for s in samples.values():
        if "blood" in s["tissues"]:
            s["modalities"].add("WholeBlood")
        if "iPSC" in s["tissues"]:
            s["modalities"].add("iPSC")
        if "Illumina_parents" in s["modalities"] and "Illumina_WGS" not in s["modalities"]:
            s["evidence"]["Illumina_parents"] = "parents only"
    print(f"  {len(samples)} donors, {sum(1 for r in rows if r['kind'] == 'data'):,} data files, "
          f"{len(other_donor)} files on non-catalogue donors")
    hgsvc_rows = [r for r in rows if r["for_hgsvc"]]
    rows = [r for r in rows if not r["for_hgsvc"]]
    return samples, rows, other_donor, probe_rows, hgsvc_rows, inline_hgsvc


# =========================================================================== HGSVC

# Every raw-data directory, named explicitly. New directories print as UNCLASSIFIED.
HGSVC_DIRS = {
    "HGSVC2": {
        "20190925_PUR_PacBio_HiFi": "HiFi", "20191004_Illumina": "Illumina_WGS",
        "20191005_YRI_PacBio_NA19240_HiFi": "HiFi", "20191031_CHS_PacBio_HG00512_HiFi": "HiFi",
        "20191031_CHS_PacBio_HG00513_HiFi": "HiFi", "20191205_YRI_PacBio_NA19238_HIFI": "HiFi",
        "20191205_YRI_PacBio_NA19239_HIFI": "HiFi", "20200108_PacBio_CLR_JAX": "PacBio_CLR",
        "20200117_Bionano_optical_maps": "Bionano", "20200120_Strandseq": "StrandSeq",
        "20200203_PacBio_CLR_EEE": "PacBio_CLR", "20200212_PacBio_CLR_Devine": "PacBio_CLR",
        "20200422_Strandseq": "StrandSeq", "20200509_HiFi_CHS-PUR-YRI_trio-hap-data": "HiFi",
        "20200512_Hi-C": "HiC", "20200627_RNAseq_JAX": "RNAseq",
        "20200722_PUR_PacBio_HG00732_HiFi": "HiFi",
        "20200731_CHS_PacBio_HG00514_CLR_reseq": "PacBio_CLR",
        "20200731_CHS_PacBio_HG00514_HiFi_reseq": "HiFi",
        "20200805_NYGC_NA24385_Illumina_data": "Illumina_WGS",
        "20210607_Strandseq": "StrandSeq", "20210822_UW_EEE_ONT_UL": "ONT",
        "20210824_Strandseq": "StrandSeq", "20210830_Strandseq": "StrandSeq",
        "20210902_bionano_hgsvc": "Bionano", "20220119_Strandseq": "StrandSeq",
        "20220207_Strandseq": "StrandSeq", "20220302_Strandseq": "StrandSeq",
        "20220506_Strandseq": "StrandSeq", "20230515_Shi_hic_files": "HiC",
    },
    "HGSVC3": {
        "2021_Hi-C_JAX": "HiC", "2021_RNAseq_JAX": "RNAseq", "20210509_UW_HiFi": "HiFi",
        "20210728_PacBio_Iso-Seq_JAX": "Kinnex_IsoSeq", "20211013_ONT_Rebasecalled": "ONT",
        "20211207_UMIGS_HiFi": "HiFi", "20211213_Nanopolish": "ONT_5mC",
        "20220401_bionano_hgsvc": "Bionano", "20220418_UW_HiFi": "HiFi",
        "20220711_Strandseq": "StrandSeq", "20220803_Strandseq": "StrandSeq",
        "20220831_JAX_HiFi": "HiFi", "20230501_HGSVC_UL_ONT-UW": "ONT",
        "20230512_HGSVC_EEE_HIFI": "HiFi", "20230703_HGSVC_EEE_HIFI": "HiFi",
        "20230706_HGSVC_EEE_UL_ONT": "ONT", "20230905_HGSVC_EEE_UL_ONT": "ONT",
        "20231126_UW_HiFi": "HiFi", "20231223_Nanopolish_UW": "ONT_5mC",
        "20240117_UW_HiFi": "HiFi", "20240124_UW_HiFi": "HiFi",
        "20240816_JAX_ONT_guppy6_Rebasecalled": "ONT",
        "20250610_JAX_HiFi_kinnex": "Kinnex_IsoSeq", "20250922_EEE_HiFi_Kinnex": "Kinnex_IsoSeq",
        "202505012_USC_HighCovHiC": "HiC",
    },
}
# A separate 718-sample pooled Strand-seq effort on the 1kGP panel, not the assembly cohort.
HGSVC_EXCLUDE = ("20260214_Strandseq_Pools",)
HGSVC_DIR_HINT = re.compile(r"HiFi|HIFI|ONT|Illumina|Strandseq|Hi-?C|hic|RNAseq|Iso-?Seq|"
                            r"[Kk]innex|[Bb]ionano|CLR|Nanopolish", re.I)
DERIVED_DIR = re.compile(r"assembl|callset|hap-assm|PAV|Freeze|genotyp|_SV|_calls|Alignments|verkko|"
                         r"hifiasm|clair3|cutesv|debreak|deepvariant|delly|dipcall|hificnv|"
                         r"pbsv|sniffles|svim|svision|MELT|Lumpy|Integration|inversion|vamos|"
                         r"CEN_|centromere|Repeat|phase3|sigY|Example", re.I)


def igsr_ls(path):
    try:
        t = http_get(IGSR + path, timeout=120).decode("utf-8", "replace")
    except Exception:
        return []
    return [n for n in re.findall(r'<a href="([^"?/][^"]*)"', t) if n != "../"]


def igsr_crawl(refresh):
    def go():
        targets, unknown = [], []
        for coll, known in HGSVC_DIRS.items():
            for d in igsr_ls(f"data_collections/{coll}/working/"):
                name = d.rstrip("/")
                if not d.endswith("/") or name in HGSVC_EXCLUDE:
                    continue
                if name in known:
                    targets.append((coll, name, known[name]))
                elif HGSVC_DIR_HINT.search(name) and not DERIVED_DIR.search(name):
                    unknown.append(f"{coll}/{name}")

        def rec(coll, name, mod):
            out, level = [], [f"data_collections/{coll}/working/{name}/"]
            for _ in range(7):
                nxt = []
                with cf.ThreadPoolExecutor(8) as ex:
                    for p, kids in zip(level, ex.map(igsr_ls, level)):
                        for k in kids:
                            (nxt.append(p + k) if k.endswith("/") else out.append([coll, name, mod, p + k]))
                if not nxt:
                    break
                level = nxt
            return out
        files = []
        with cf.ThreadPoolExecutor(12) as ex:
            for lst in ex.map(lambda t: rec(*t), targets):
                files += lst
        return {"files": files, "unknown_dirs": unknown}
    return cached("igsr_hgsvc.json", go, refresh)


HGSVC_DATA = re.compile(r"\.(bam|cram|fastq\.gz|fq\.gz|fastq|fasta\.gz|fa\.gz|bnx\.gz|bnx|cmap|"
                        r"h5|hic|txt\.gz|tsv\.gz|m5|tar|tar\.gz|fast5|pod5|xmap|smap|ubam)$", re.I)


def read_hgsvc_sheet(xlsx):
    import pandas as pd
    raw = pd.read_excel(xlsx, sheet_name="Summary", header=None)
    group = raw.iloc[0].ffill()
    cols = [f"{g} | {h}" if pd.notna(g) and str(g) != "nan" else str(h)
            for g, h in zip(group, raw.iloc[1])]
    S = raw.iloc[2:].copy()
    S.columns = cols
    return S[S["SampleID"].notna()].reset_index(drop=True)


SHEET_COLS = {
    "Illumina_WGS": "Illumina | Illumina available?",
    "HiFi": "PacBio HiFi | Data generation complete?",
    "ONT": "ONT Ultra-long | Data generation complete?",
    "StrandSeq": "Strand-Seq | Libraries (count)",
    "HiC": "Hi-C | Status",
    "Bionano": "BioNano | Status",
    "RNAseq": "RNAseq | Status",
    "IsoSeq": "Iso-Seq | Status",
    "Kinnex": "Kinnex - in progress | nan",
    "ChrY_ONT": "Y chr ONT | Status",
    "PacBio_CLR": "PacBio CLR | Coverage (X)",
}


def truthy(v):
    import pandas as pd
    if pd.isna(v):
        return False
    s = str(v).strip().lower()
    if s in ("no", "nan", "", "ignore", "ignore?", "n/a", "-", "na"):
        return False
    return (s.startswith("yes") or "complete" in s
            or s.startswith(("jax", "uw", "broad", "rsii", "sqi"))
            or re.match(r"^[\d.]+$", s) is not None)


def lib_count(v):
    import pandas as pd
    if pd.isna(v):
        return None
    s = str(v).strip()
    if re.match(r"^\s*\d+(\s*\+\s*\d+)*\s*$", s):
        return sum(int(x) for x in re.findall(r"\d+", s))
    m = re.match(r"^\s*(\d+)", s)
    return int(m.group(1)) if m else None


def build_hgsvc(xlsx, nygc, refresh, from_hprc=(), inline_from_hprc=None):
    print("HGSVC: reading production sheet + crawling IGSR ...", flush=True)
    S = read_hgsvc_sheet(xlsx)
    samples = {}
    for _, r in S.iterrows():
        raw = str(r["SampleID"]).strip()                      # e.g. "NA24385 (HG002)"
        sid = canon(raw)
        aka = sorted(({raw.split()[0]} | set(re.findall(r"\((\w+)\)", raw))) - {sid})
        sheet = {}
        for mod, col in SHEET_COLS.items():
            sheet[mod] = (lib_count(r[col]) is not None) if mod == "StrandSeq" else truthy(r[col])
        get = lambda c: (None if str(r.get(c)) == "nan" else r.get(c))
        samples[sid] = dict(
            sample_id=sid, cohort="HGSVC", aka=", ".join(aka),
            population=get("Sample Information | Population") or "",
            superpop=get("Sample Information | SuperPopulation") or "",
            sex=str(get("Sample Information | Sex") or "").lower(),
            family_id=str(get("Sample Information | FamilyID") or ""),
            relation=get("Sample Information | Relation") or "",
            project_yr=str(get("Project") or ""),
            strandseq_libs=lib_count(r[SHEET_COLS["StrandSeq"]]),
            sheet=sheet, modalities=set(), evidence={}, phases=set())

    crawl = igsr_crawl(refresh)
    for u in (u for u in crawl["unknown_dirs"] if not DERIVED_DIR.search(u)):
        print(f"  UNCLASSIFIED IGSR dir (add to HGSVC_DIRS): {u}", file=sys.stderr)

    # HGSVC2's own sequence index names the sample for every Strand-seq library, so
    # use it where the path is ambiguous (pooled flowcell directories).
    by_file = {}
    for f in ("HGSVC2/HGSVC2_sequence.index", "HGSVC2/HGSVC2_pacbio.index",
              "HGSVC2/HGSVC2_bionano.index"):
        t = get_text(IGSR + "data_collections/" + f, "igsr/" + f.replace("/", "_") + ".json", refresh)
        for line in t.splitlines():
            if line.startswith("#"):
                continue
            p = line.split("\t")
            if len(p) > 9:
                by_file[os.path.basename(p[0])] = canon(p[9])

    rows = []
    for coll, dname, mod, path in crawl["files"]:
        fn = os.path.basename(path)
        if JUNK.search(fn) or not HGSVC_DATA.search(fn):
            continue
        if dname == "20230515_Shi_hic_files":
            detail = "contact map (.hic)"
        elif mod == "ONT_5mC":
            detail = "nanopolish calls"
        elif mod == "Kinnex_IsoSeq":
            detail = "Iso-Seq" if "Iso-Seq" in dname else "Kinnex"
        else:
            detail = ""
        rel = path.split(f"{dname}/", 1)[1]
        seg_ids = [canon(s) for s in rel.split("/")[:-1] if canon(s)]
        file_ids = ids_in(fn)
        sid = (seg_ids[-1] if seg_ids else
               next(iter(file_ids)) if len(file_ids) == 1 else by_file.get(fn))
        rows.append(dict(sample_id=sid, collection=coll, directory=dname, modality=mod,
                         assay_detail=detail, rel_path=path, url=IGSR + path,
                         in_catalogue=sid in samples))

    # HGSVC donors sequenced by HPRC centres, hosted in the HPRC bucket
    for r in from_hprc:
        if r["kind"] != "data":
            continue
        sid = r["sample_id"]
        rows.append(dict(sample_id=sid, collection="HPRC bucket", directory=r["submission"],
                         sub_dir=r["sub_dir"],
                         modality=r["modality"], assay_detail=r["assay_detail"],
                         rel_path=S3_HTTPS + r["s3_key"], url=S3_HTTPS + r["s3_key"],
                         in_catalogue=sid in samples))
    for (sid, mod), marks in (inline_from_hprc or {}).items():
        if sid in samples:
            samples[sid]["modalities"].add(mod)
            samples[sid]["evidence"][mod] = ("inline m6A" if mod == "FiberSeq" else
                                             "inline" + ("+5hmC" if "5hmC" in marks else ""))

    # HGSVC HiFi BAMs: some carry kinetics or 5mC tags
    pick = defaultdict(list)
    for r in rows:
        if r["in_catalogue"] and r["modality"] == "HiFi" and r["rel_path"].endswith(".bam"):
            if len(pick[r["sample_id"]]) < 2:
                pick[r["sample_id"]].append(r["url"])
    probe = probe_many(sorted(u for v in pick.values() for u in v), "probe_hgsvc.json", refresh)
    probe_rows = []
    for sid, urls in pick.items():
        for u in urls:
            p = probe.get(u, {})
            probe_rows.append(dict(sample_id=sid, modality="HiFi", tissue="", url=u,
                                   mods=",".join(p.get("mods", [])), kinetics=p.get("kinetics"),
                                   programs=",".join(p.get("pg", [])), error=p.get("err", "")))
            if has_5mc(p):
                samples[sid]["modalities"].add("HiFi_5mC")
                samples[sid]["evidence"]["HiFi_5mC"] = "inline"

    # merge sheet + archive (+ NYGC 30x for Illumina), keeping the evidence source
    on_igsr = defaultdict(set)
    for r in rows:
        if r["in_catalogue"] and r["assay_detail"] != "contact map (.hic)":
            on_igsr[r["sample_id"]].add(r["modality"])
            samples[r["sample_id"]]["phases"].add(r["collection"])
    fold = {"IsoSeq": "Kinnex_IsoSeq", "Kinnex": "Kinnex_IsoSeq"}
    for sid, s in samples.items():
        sheet = defaultdict(bool)
        for m, v in s["sheet"].items():
            sheet[fold.get(m, m)] |= v
        for m in set(sheet) | on_igsr[sid]:
            a, b = sheet[m], m in on_igsr[sid]
            if m == "Illumina_WGS" and not b and sid in nygc:
                b, ev = True, "nygc_30x"
            else:
                ev = "both" if a and b else "sheet_only" if a else "igsr_only" if b else ""
            if ev:
                s["modalities"].add(m)
                s["evidence"][m] = ev
        s["phases"] = sorted(s["phases"]) or ["sheet only"]
        del s["sheet"]
    print(f"  {len(samples)} donors, {len(rows):,} files "
          f"({sum(1 for r in rows if not r['sample_id']):,} unattributed)")
    return samples, rows, probe_rows


# =========================================================================== external


ENCODE_GROUPS = {
    "ENCODE_RNA": ("total RNA-seq", "polyA plus RNA-seq", "small RNA-seq", "microRNA-seq",
                   "long read RNA-seq", "RAMPAGE", "CAGE", "PRO-cap", "PRO-seq", "GRO-cap"),
    "ENCODE_Access": ("ATAC-seq", "DNase-seq", "FAIRE-seq", "MNase-seq"),
    "ENCODE_ChIP": ("TF ChIP-seq", "Histone ChIP-seq", "Mint-ChIP-seq", "Control ChIP-seq"),
    "ENCODE_3D": ("intact Hi-C", "in situ Hi-C", "dilution Hi-C", "ChIA-PET", "HiChIP"),
    "ENCODE_DNAme": ("WGBS", "RRBS", "DNAme array"),
}


def build_external(refresh):
    """Same donors, other projects. Keyed by canonical id."""
    print("External: NYGC 30x, 1KG-ONT, Geuvadis, MAGE, ENCODE ...", flush=True)
    ext = defaultdict(lambda: defaultdict(list))

    def idx(path):
        return get_text(IGSR + path, "igsr/" + path.replace("/", "_") + ".json", refresh)

    for f in ("data_collections/1000G_2504_high_coverage/1000G_2504_high_coverage.sequence.index",
              "data_collections/1000G_2504_high_coverage/1000G_698_related_high_coverage.sequence.index"):
        for line in idx(f).splitlines():
            p = line.split("\t")
            if line.startswith("#") or len(p) < 10:
                continue
            ext[canon(p[9])]["NYGC_30x"].append(p[0].replace("ftp://", "https://"))

    for n in igsr_ls_cached("data_collections/1KG_ONT_VIENNA/hg38/", refresh):
        if n.endswith(".hg38.cram"):
            ext[canon(n.split(".")[0])]["ONT_1KG"].append(
                IGSR + "data_collections/1KG_ONT_VIENNA/hg38/" + n)

    for line in idx("data_collections/geuvadis/geuvadis.sequence.index").splitlines():
        p = line.split("\t")
        if line.startswith("#") or len(p) < 22:
            continue
        sid = canon(p[9].split(":")[-1])
        ext[sid]["Geuvadis_smallRNA" if "small" in p[21].lower() else "Geuvadis_RNA"].append(
            p[0].replace("ftp://", "https://"))

    mage = get_text("https://www.ebi.ac.uk/ena/portal/api/filereport?accession=PRJNA851328"
                    "&result=read_run&fields=sample_alias,run_accession,fastq_ftp&format=tsv",
                    "ena/mage.json", refresh)
    for row in csv.DictReader(io.StringIO(mage), delimiter="\t"):
        sid = canon(row.get("sample_alias"))
        if sid:
            ext[sid]["MAGE_RNA"] += ["https://" + u for u in row["fastq_ftp"].split(";") if u]

    enc = cached("encode_experiments.json", lambda: json.loads(http_get(
        "https://www.encodeproject.org/search/?type=Experiment&status=released"
        "&replicates.library.biosample.donor.organism.scientific_name=Homo+sapiens"
        "&biosample_ontology.classification=cell+line&field=accession&field=assay_title"
        "&field=biosample_ontology.term_name&field=target.label&limit=all&format=json",
        timeout=300))["@graph"], refresh)
    group_of = {a: g for g, lst in ENCODE_GROUPS.items() for a in lst}
    for e in enc:
        term = e.get("biosample_ontology", {}).get("term_name", "")
        if not re.fullmatch(r"GM\d{5}", term):
            continue
        g = group_of.get(e.get("assay_title"))
        if g:
            tgt = (e.get("target") or {}).get("label")
            ext[canon(term)][g].append(e["accession"] + "|" + e["assay_title"]
                                       + (f"|{tgt}" if tgt else ""))
    return {k: dict(v) for k, v in ext.items() if k}


def igsr_ls_cached(path, refresh):
    return cached("igsr/ls_" + path.strip("/").replace("/", "_") + ".json",
                  lambda: igsr_ls(path), refresh)


# =========================================================================== site


# Presentation metadata the app reads: id, label, short column label, tier, description.
# tier "main" rows are shown by default; "more" rows sit behind a toggle.
G_SEQ, G_EPI, G_RNA, G_3D = "Genome sequence", "DNA methylation & chromatin", "RNA", "3D genome & structure"
G_QC, G_EXT = "Other sample types & QC", "From other projects, same cell lines"
MODALITIES = [dict(zip(("id", "label", "short", "group", "scope", "tier", "note"), m)) for m in [
    ("HiFi", "PacBio HiFi long reads", "HiFi", G_SEQ, "cohort", "main",
     "Accurate long reads; the backbone of the assemblies."),
    ("ONT", "Nanopore long reads", "ONT", G_SEQ, "cohort", "main",
     "Oxford Nanopore, including ultra-long reads."),
    ("Illumina_WGS", "Illumina short reads", "Illumina", G_SEQ, "cohort", "main",
     "Short-read whole genome. For HGSVC this is mostly the 1000 Genomes 30x data."),
    ("Illumina_parents", "Parents' Illumina reads", "Parents", G_SEQ, "cohort", "more",
     "Short reads from the donor's parents, used for trio phasing."),
    ("PacBio_CLR", "PacBio CLR long reads (older)", "CLR", G_SEQ, "cohort", "more",
     "Earlier, noisier PacBio reads; superseded by HiFi."),
    ("TenX", "10x linked reads (older)", "10x", G_SEQ, "cohort", "more", ""),
    ("ChrY_ONT", "chrY-targeted Nanopore", "chrY", G_SEQ, "cohort", "more", ""),
    ("HiFi_5mC", "DNA methylation, from HiFi", "5mC HiFi", G_EPI, "cohort", "main",
     "5mC at CpG sites. Usually already inside the HiFi read files."),
    ("ONT_5mC", "DNA methylation, from Nanopore", "5mC ONT", G_EPI, "cohort", "main",
     "5mC and often 5hmC. Usually already inside the Nanopore read files."),
    ("FiberSeq", "Fiber-seq", "Fiber-seq", G_EPI, "cohort", "main",
     "Open chromatin and nucleosome positions on single DNA molecules."),
    ("Kinnex_IsoSeq", "Full-length RNA (Kinnex / Iso-Seq)", "Kinnex", G_RNA, "cohort", "main",
     "Long-read transcripts."),
    ("RNAseq", "RNA-seq (short read)", "RNA-seq", G_RNA, "cohort", "main", "Gene expression."),
    ("ONT_directRNA", "Nanopore direct RNA", "dRNA", G_RNA, "cohort", "more",
     "Native RNA with m6A calls."),
    ("HiC", "Hi-C / Omni-C", "Hi-C", G_3D, "cohort", "main",
     "Chromosome contacts; used for phasing and 3D structure."),
    ("StrandSeq", "Strand-seq", "Strand-seq", G_3D, "cohort", "main",
     "Single-cell strand sequencing; phasing and inversions."),
    ("Bionano", "Bionano optical maps", "Bionano", G_3D, "cohort", "main",
     "Large structural variants."),
    ("PoreC", "Pore-C", "Pore-C", G_3D, "cohort", "more", "Multi-way chromosome contacts on Nanopore."),
    ("MicroC", "Micro-C", "Micro-C", G_3D, "cohort", "more", "Nucleosome-resolution contacts."),
    ("WholeBlood", "Whole-blood samples", "Blood", G_QC, "cohort", "more",
     "Data from the donor's blood rather than the cell line, so the two can be compared."),
    ("iPSC", "iPSC samples", "iPSC", G_QC, "cohort", "more",
     "Data from induced pluripotent stem cells."),
    ("Microarray", "SNP microarray (cell-line QC)", "Array", G_QC, "cohort", "more", ""),
    ("Karyotype", "Karyotype (cell-line QC)", "Karyotype", G_QC, "cohort", "more", ""),
    ("NYGC_30x", "1000 Genomes 30x genome", "1kGP 30x", G_EXT, "external", "main",
     "Illumina short reads from the 1000 Genomes Project (NYGC)."),
    ("MAGE_RNA", "MAGE RNA-seq", "MAGE", G_EXT, "external", "main",
     "Gene expression in 731 1000 Genomes cell lines (Taylor et al. 2024)."),
    ("Geuvadis_RNA", "Geuvadis RNA-seq", "Geuvadis", G_EXT, "external", "main",
     "Gene expression (Lappalainen et al. 2013)."),
    ("ENCODE_RNA", "ENCODE RNA-seq", "ENCODE RNA", G_EXT, "external", "main", ""),
    ("ENCODE_Access", "ENCODE ATAC-seq / DNase-seq", "ENCODE ATAC", G_EXT, "external", "main",
     "Open chromatin."),
    ("ONT_1KG", "1000 Genomes Nanopore (Vienna)", "1KG ONT", G_EXT, "external", "more",
     "Older R9.4.1 reads, no methylation calls."),
    ("Geuvadis_smallRNA", "Geuvadis small RNA", "small RNA", G_EXT, "external", "more", ""),
    ("ENCODE_ChIP", "ENCODE ChIP-seq", "ENCODE ChIP", G_EXT, "external", "more", ""),
    ("ENCODE_3D", "ENCODE Hi-C / ChIA-PET", "ENCODE 3D", G_EXT, "external", "more", ""),
    ("ENCODE_DNAme", "ENCODE DNA methylation", "ENCODE 5mC", G_EXT, "external", "more",
     "RRBS, WGBS or methylation array."),
]]


def write_csv(path, rows, cols=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cols = cols or (list(rows[0]) if rows else [])
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})


S3_BROWSE = f"https://s3-us-west-2.amazonaws.com/{BUCKET}/index.html?prefix="


def hic_breakdown(records, hprc_rows, hgsvc_rows):
    """Where the Hi-C / Omni-C behind each cohort's count actually sits. 'split' partitions
    the cohort's donors (each donor in exactly one line); 'sources' lists every location
    with files, and a donor can appear in more than one."""
    def src():
        return defaultdict(lambda: dict(donors=set(), files=0, bytes=0, url="", note=""))

    def finish(sources, order):
        out = []
        for where in sorted(sources, key=lambda w: (order(w), -len(sources[w]["donors"]))):
            v = sources[where]
            out.append(dict(where=where, donors=len(v["donors"]), files=v["files"],
                            bytes=v["bytes"], url=v["url"], note=v["note"]))
        return out

    out = {}
    # ---- HPRC ----
    recs = [r for r in records if r["cohort"] == "HPRC"]
    has = {r["sample_id"] for r in recs if "HiC" in r["modalities"]}
    sources, in_work = src(), set()
    loose = dict(unattributed=0, other_ids=set())
    for r in hprc_rows:
        if r["modality"] != "HiC" or r["kind"] != "data":
            continue
        if not r["sample_id"]:
            loose["unattributed"] += 1
            continue
        if not r["in_catalogue"]:
            loose["other_ids"].add(r["sample_id"])
            continue
        if r["source"] == "working":
            where, in_work = "working/<donor>/raw_data/hic/", in_work | {r["sample_id"]}
            sources[where]["url"] = S3_BROWSE + "working/"
            sources[where]["note"] = "The curated per-donor folders"
        else:
            where = f"submissions/…{r['submission']}/"
            sources[where]["url"] = S3_BROWSE + f"submissions/{r['sub_dir']}/"
            alias = re.search(r"folder named (\w+)", r["assay_detail"])
            if alias:
                sources[where]["note"] = (f"Folder is named {alias.group(1)}; same libraries as "
                                          f"{r['sample_id']}, so likely a duplicate upload")
        v = sources[where]
        v["donors"].add(r["sample_id"])
        v["files"] += 1
        v["bytes"] += int(r["size_bytes"])
    out["HPRC"] = dict(
        total=len(has), cohort=len(recs),
        split=[dict(label="In the donor's working/ folder", n=len(has & in_work), ids=[]),
               dict(label="Only in a submissions/ folder", n=len(has - in_work),
                    ids=sorted(has - in_work)),
               dict(label="No Hi-C yet", n=len(recs) - len(has), ids=[], none=True)],
        sources=finish(sources, lambda w: 0 if w.startswith("working") else 1),
        unattributed_files=loose["unattributed"], other_donors=sorted(loose["other_ids"]))

    # ---- HGSVC ----
    recs = [r for r in records if r["cohort"] == "HGSVC"]
    has = {r["sample_id"] for r in recs if "HiC" in r["modalities"]}
    sheet_only = {r["sample_id"] for r in recs if r["evidence"].get("HiC") == "sheet_only"}
    sources, on_igsr, on_s3 = src(), set(), set()
    loose = dict(unattributed=0, other_ids=set())
    for r in hgsvc_rows:
        if r["modality"] != "HiC":
            continue
        maps = r["assay_detail"] == "contact map (.hic)"
        if not r["sample_id"]:
            loose["unattributed"] += 1
            continue
        if not r["in_catalogue"]:
            loose["other_ids"].add(r["sample_id"])
            continue
        if r["collection"] == "HPRC bucket":
            where = f"HPRC bucket · submissions/…{r['directory']}/"
            sources[where]["url"] = S3_BROWSE + f"submissions/{r['sub_dir']}/"
            sources[where]["note"] = "HGSVC donors sequenced by an HPRC centre (Omni-C)"
            on_s3.add(r["sample_id"])
        else:
            where = f"IGSR · {r['collection']}/working/{r['directory']}/"
            sources[where]["url"] = IGSR + f"data_collections/{r['collection']}/working/{r['directory']}/"
            if maps:
                sources[where]["note"] = "Processed contact maps (.hic), not reads; not counted"
            else:
                on_igsr.add(r["sample_id"])
        v = sources[where]
        v["donors"].add(r["sample_id"])
        v["files"] += 1
    out["HGSVC"] = dict(
        total=len(has), cohort=len(recs),
        split=[dict(label="Reads on IGSR", n=len(has & on_igsr), ids=[]),
               dict(label="Reads only in the HPRC bucket", n=len((has & on_s3) - on_igsr),
                    ids=sorted((has & on_s3) - on_igsr)),
               dict(label="Recorded by HGSVC, but not public", n=len(sheet_only),
                    ids=sorted(sheet_only), missing=True),
               dict(label="No Hi-C", n=len(recs) - len(has), ids=sorted(
                   r["sample_id"] for r in recs if r["sample_id"] not in has), none=True)],
        sources=finish(sources, lambda w: (0 if w.startswith("IGSR") else 1, "Shi_hic" in w)),
        unattributed_files=loose["unattributed"], other_donors=sorted(loose["other_ids"]))
    for c in out.values():   # the split must partition the cohort
        assert sum(x["n"] for x in c["split"]) == c["cohort"], c["split"]
        assert sum(x["n"] for x in c["split"] if not x.get("none")) == c["total"], c["split"]
    return out


def build_site(hprc, hprc_rows, other, hprc_probe, hgsvc, hgsvc_rows, hgsvc_probe, ext, out):
    mods = [m["id"] for m in MODALITIES]
    ext_mods = {m["id"] for m in MODALITIES if m["scope"] == "external"}

    # ---- per-cohort bundles (CSV, for people who want the tables) ------------
    write_csv(os.path.join(ROOT, "hprc/data/hprc_file_index.csv"), hprc_rows,
              ["sample_id", "modality", "assay_detail", "tissue", "kind", "s3_key",
               "size_bytes", "source", "indexed_on_github", "in_catalogue"])
    write_csv(os.path.join(ROOT, "hprc/data/hprc_methylation_probe.csv"), hprc_probe)
    write_csv(os.path.join(ROOT, "hprc/data/hprc_other_donors_file_index.csv"), other)
    write_csv(os.path.join(ROOT, "hgsvc/data/hgsvc_file_index.csv"), hgsvc_rows,
              ["sample_id", "collection", "directory", "modality", "assay_detail",
               "rel_path", "url", "in_catalogue"])
    write_csv(os.path.join(ROOT, "hgsvc/data/hgsvc_methylation_probe.csv"), hgsvc_probe)
    ext_rows = [dict(sample_id=s, modality=m, n_items=len(v), first=v[0])
                for s, d in sorted(ext.items()) for m, v in sorted(d.items())]
    write_csv(os.path.join(ROOT, "external/data/external_by_donor.csv"), ext_rows)

    # ---- records ---------------------------------------------------------
    records = []
    for s in list(hprc.values()) + list(hgsvc.values()):
        s = dict(s)
        s["modalities"] = set(s["modalities"])
        s.pop("tissues", None)
        records.append(s)
    donors = defaultdict(list)
    for r in records:
        donors[r["sample_id"]].append(r)
    dual = sorted(d for d, rs in donors.items() if len(rs) > 1)

    # external data attaches to the donor, so it is identical on both cohort rows
    for r in records:
        e = ext.get(r["sample_id"], {})
        for m in e:
            if m in ext_mods:
                r["modalities"].add(m)

    def mat(rs, cohort):
        cols = [m for m in mods]
        out_rows = []
        for r in rs:
            row = {k: r.get(k, "") for k in ("sample_id", "aka", "population", "superpop", "sex",
                                            "release", "phases", "family_id", "relation",
                                            "project_yr", "strandseq_libs")}
            row["phases"] = ";".join(r.get("phases", []))
            for m in cols:
                row[m] = m in r["modalities"]
                if r["evidence"].get(m):
                    row[m + "__src"] = r["evidence"][m]
            out_rows.append(row)
        head = (["sample_id", "aka", "population", "superpop", "sex"]
                + (["release"] if cohort == "HPRC" else
                   ["phases", "family_id", "relation", "project_yr", "strandseq_libs"])
                + [x for m in cols for x in (m, m + "__src")])
        write_csv(os.path.join(ROOT, f"{cohort.lower()}/data/{cohort.lower()}_sample_modality_matrix.csv"),
                  out_rows, head)
    mat([r for r in records if r["cohort"] == "HPRC"], "HPRC")
    mat([r for r in records if r["cohort"] == "HGSVC"], "HGSVC")

    # ---- per-donor file shards --------------------------------------------
    files_dir = os.path.join(out, "files")
    os.makedirs(files_dir, exist_ok=True)
    for stale in os.listdir(files_dir):
        os.remove(os.path.join(files_dir, stale))
    hp = defaultdict(list)
    for r in hprc_rows:
        if r["in_catalogue"]:
            hp[r["sample_id"]].append([r["modality"], r["s3_key"], int(r["size_bytes"]),
                                       1 if r["indexed_on_github"] else 0, r["assay_detail"],
                                       r["source"], r["kind"], r["tissue"]])
    hg = defaultdict(list)
    for r in hgsvc_rows:
        if r["in_catalogue"]:
            hg[r["sample_id"]].append([r["modality"], r["url"], r["collection"],
                                       r["assay_detail"], r["directory"]])
    for r in records:
        sid = r["sample_id"]
        if r["cohort"] == "HPRC":
            fl = hp.get(sid, [])
            data = [f for f in fl if f[6] == "data"]
            r["n_files"], r["bytes"] = len(data), sum(f[2] for f in data)
            r["n_indexed"] = sum(f[3] for f in data)
            shard = dict(sample_id=sid, cohort="HPRC", prefix=S3_HTTPS,
                         cols=["modality", "key", "bytes", "indexed_on_github", "assay_detail",
                               "source", "kind", "tissue"], files=fl)
        else:
            fl = hg.get(sid, [])
            r["n_files"], r["bytes"] = len(fl), 0
            shard = dict(sample_id=sid, cohort="HGSVC", prefix="",
                         cols=["modality", "url", "collection", "assay_detail", "directory"],
                         files=fl)
        shard["external"] = ext.get(sid, {})
        name = f"{sid}.{r['cohort']}.json" if sid in dual else f"{sid}.json"
        with open(os.path.join(files_dir, name), "w") as fh:
            json.dump(shard, fh, separators=(",", ":"))

    # ---- coverage -----------------------------------------------------------
    cohorts = {c: sum(1 for r in records if r["cohort"] == c) for c in ("HPRC", "HGSVC")}
    coverage = {}
    for m in mods:
        coverage[m] = {}
        for c in cohorts:
            n = sum(1 for r in records if r["cohort"] == c and m in r["modalities"])
            coverage[m][c] = {"n": n, "ran": n > 0}

    # ---- intersections (cross-check for the live filter) -----------------------
    def inter(cohort, want):
        return sum(1 for r in records if r["cohort"] == cohort and all(m in r["modalities"] for m in want))
    core5 = ["HiFi", "ONT", "Illumina_WGS", "HiC", "Kinnex_IsoSeq"]
    eight = ["Illumina_WGS", "HiFi", "ONT", "StrandSeq", "HiC", "Bionano", "RNAseq", "Kinnex_IsoSeq"]
    combos = {
        "HPRC": {
            "core5 (HiFi+ONT+Illumina+HiC+Kinnex)": core5,
            "core5 + HiFi 5mC": core5 + ["HiFi_5mC"],
            "core5 + ONT 5mC": core5 + ["ONT_5mC"],
            "core5 + Fiber-seq": core5 + ["FiberSeq"],
            "core5 + Fiber-seq + HiFi 5mC + ONT 5mC": core5 + ["FiberSeq", "HiFi_5mC", "ONT_5mC"],
            "core5 + MAGE RNA-seq": core5 + ["MAGE_RNA"],
            "Fiber-seq + Kinnex + MAGE RNA-seq": ["FiberSeq", "Kinnex_IsoSeq", "MAGE_RNA"],
            "HiFi 5mC + Whole blood (LCL vs blood)": ["HiFi_5mC", "WholeBlood"],
        },
        "HGSVC": {
            "eight-assay set (Ill,HiFi,ONT,SS,HiC,BN,RNA,Kinnex)": eight,
            "eight + ONT 5mC": eight + ["ONT_5mC"],
            "eight + MAGE RNA-seq": eight + ["MAGE_RNA"],
            "RNA-seq + Kinnex/Iso-Seq": ["RNAseq", "Kinnex_IsoSeq"],
            "HGSVC RNA-seq + Geuvadis + MAGE": ["RNAseq", "Geuvadis_RNA", "MAGE_RNA"],
        },
    }
    intersections = {c: {k: inter(c, v) for k, v in d.items()} for c, d in combos.items()}

    # ---- unattributed / out of catalogue -----------------------------------
    un_h = [[r["modality"], r["s3_key"], int(r["size_bytes"]), r["assay_detail"], r["source"]]
            for r in hprc_rows if not r["sample_id"]]
    oc_h = [[r["sample_id"], r["modality"], r["s3_key"], int(r["size_bytes"]), r["assay_detail"],
             r["source"]] for r in hprc_rows if r["sample_id"] and not r["in_catalogue"]]
    un_g = [[r["modality"], r["url"], r["collection"], r["directory"]]
            for r in hgsvc_rows if not r["sample_id"]]
    oc_g = [[r["sample_id"], r["modality"], r["url"], r["collection"], r["directory"]]
            for r in hgsvc_rows if r["sample_id"] and not r["in_catalogue"]]
    od = defaultdict(lambda: {"files": 0, "bytes": 0, "modalities": set(), "submissions": set(),
                              "tissue": set()})
    for r in other:
        d = od[r["donor"]]
        d["files"] += 1
        d["bytes"] += int(r["size_bytes"])
        d["modalities"].add(r["modality"])
        d["submissions"].add(r["submission"])
        if r["tissue"] or r["assay_detail"]:
            d["tissue"].add(r["tissue"] or r["assay_detail"])
    other_summary = [dict(donor=k, files=v["files"], bytes=v["bytes"],
                          modalities=sorted(v["modalities"]), submissions=sorted(v["submissions"]),
                          context=sorted(v["tissue"])) for k, v in sorted(od.items())]
    with open(os.path.join(files_dir, "_unattributed.json"), "w") as fh:
        json.dump({
            "note": "Files the archives do not tie to a catalogued donor. 'unattributed' has no "
                    "sample id at all (mostly pooled Strand-seq flowcells and multiplexed Kinnex "
                    "pools). 'out_of_catalogue' names a donor with no long-read data here "
                    "(1000G trio parents, Coriell lines screened but not sequenced). "
                    "'other_donors' are EnTEx/GTEx tissue donors and MGI pediatric blood donors "
                    "hosted in the HPRC bucket. All are real data.",
            "unattributed": {"HPRC": {"prefix": S3_HTTPS, "cols": ["modality", "key", "bytes",
                                                                    "assay_detail", "source"],
                                      "files": un_h},
                             "HGSVC": {"prefix": "", "cols": ["modality", "url", "collection",
                                                                 "directory"], "files": un_g}},
            "out_of_catalogue": {"HPRC": {"prefix": S3_HTTPS,
                                          "cols": ["sample_id", "modality", "key", "bytes",
                                                   "assay_detail", "source"], "files": oc_h},
                                 "HGSVC": {"prefix": "",
                                           "cols": ["sample_id", "modality", "url", "collection",
                                                    "directory"], "files": oc_g}},
            "other_donors": {"prefix": S3_HTTPS, "cols": ["donor", "modality", "key", "bytes",
                                                          "assay_detail", "tissue", "submission"],
                             "files": [[r["donor"], r["modality"], r["s3_key"], int(r["size_bytes"]),
                                        r["assay_detail"], r["tissue"], r["submission"]]
                                       for r in other]},
        }, fh, separators=(",", ":"))

    # ---- invariants -------------------------------------------------------
    n_hp = sum(len(v) for v in hp.values()) + len(un_h) + len(oc_h)
    assert n_hp == len(hprc_rows), f"HPRC file accounting {n_hp} != {len(hprc_rows)}"
    n_hg = sum(len(v) for v in hg.values()) + len(un_g) + len(oc_g)
    assert n_hg == len(hgsvc_rows), f"HGSVC file accounting {n_hg} != {len(hgsvc_rows)}"
    for c in cohorts:
        for m in mods:
            direct = sum(1 for r in records if r["cohort"] == c and m in r["modalities"])
            assert direct == coverage[m][c]["n"]

    # ---- payload -------------------------------------------------------------
    samples = []
    for r in records:
        rec = {k: r.get(k) for k in ("sample_id", "cohort", "aka", "population", "superpop", "sex",
                                     "n_files", "bytes") if r.get(k) not in (None, "")}
        rec["modalities"] = sorted(r["modalities"])
        rec["evidence"] = r["evidence"]
        if r["cohort"] == "HPRC":
            rec["release"] = r["release"]
            rec["n_indexed"] = r["n_indexed"]
        else:
            for k in ("family_id", "relation", "project_yr", "strandseq_libs", "phases"):
                if r.get(k) not in (None, "", "nan"):
                    rec[k] = r[k]
        samples.append(rec)
    payload = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cohorts": cohorts,
        "unique_samples": len(donors),
        "dual_cohort_samples": dual,
        "modalities": mods,
        "modality_info": {m["id"]: {k: m[k] for k in ("label", "short", "group", "scope", "tier",
                                                       "note")} for m in MODALITIES},
        "coverage": coverage,
        "intersections": intersections,
        "totals": {
            "files": sum(r["n_files"] for r in records),
            "bytes": sum(r["bytes"] for r in records),
            "unattributed_files": len(un_h) + len(un_g),
            "out_of_catalogue_files": len(oc_h) + len(oc_g),
            "other_donor_files": len(other),
            "external_donors": sum(1 for d in donors if ext.get(d)),
        },
        "other_donors": other_summary,
        "breakdowns": {"HiC": hic_breakdown(records, hprc_rows, hgsvc_rows)},
        "samples": samples,
    }
    with open(os.path.join(out, "catalog.json"), "w") as fh:
        json.dump(payload, fh, separators=(",", ":"), default=list)
    with open(os.path.join(ROOT, "combined/data/combined_catalog.json"), "w") as fh:
        json.dump(payload, fh, indent=1, default=list)
    print(f"\ncatalog.json  {os.path.getsize(os.path.join(out, 'catalog.json')) / 1024:.0f} KB  "
          f"{len(records)} records / {len(donors)} donors, dual-cohort {dual}")
    for m in mods:
        print(f"  {m:18s} HPRC {coverage[m]['HPRC']['n']:4d}   HGSVC {coverage[m]['HGSVC']['n']:3d}")
    print("intersections", json.dumps(intersections, indent=1))
    return payload


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hgsvc-xlsx", required=True, help="HGSVC_Production_Master*.xlsx")
    ap.add_argument("--refresh", action="store_true", help="ignore .cache/ and re-crawl")
    a = ap.parse_args()
    os.makedirs(CACHE, exist_ok=True)
    ext = build_external(a.refresh)
    nygc = {k for k, v in ext.items() if "NYGC_30x" in v}
    hgsvc_ids = frozenset(canon(str(x)) for x in read_hgsvc_sheet(a.hgsvc_xlsx)["SampleID"])
    hprc, hprc_rows, other, hprc_probe, to_hgsvc, inline_hgsvc = build_hprc(a.refresh, hgsvc_ids)
    hgsvc, hgsvc_rows, hgsvc_probe = build_hgsvc(a.hgsvc_xlsx, nygc, a.refresh, to_hgsvc, inline_hgsvc)
    build_site(hprc, hprc_rows, other, hprc_probe, hgsvc, hgsvc_rows, hgsvc_probe, ext,
               os.path.join(ROOT, "site-data"))


if __name__ == "__main__":
    main()
