/* Pangenome Data Catalog — static app over site-data/catalog.json.
   No build step, no dependencies. Modality names, labels and groups come from the
   payload (catalog.modality_info), so adding an assay never needs an edit here. */

const DATA = 'site-data/';
const COHORTS = ['HPRC', 'HGSVC'];
const CSS = { HPRC: 'hprc', HGSVC: 'hgsvc' };

let INFO = {};            // modality id -> {label, group, scope, note, legacy}
const label = m => INFO[m]?.label || m;
const isExt = m => INFO[m]?.scope === 'external';
const isLegacy = m => !!INFO[m]?.legacy;

const PRESETS = [
  { label: 'HPRC core five', mods: ['HiFi', 'ONT', 'Illumina_WGS', 'HiC', 'Kinnex_IsoSeq'] },
  { label: '+ Fiber-seq', mods: ['HiFi', 'ONT', 'Illumina_WGS', 'HiC', 'Kinnex_IsoSeq', 'FiberSeq'] },
  { label: '+ both methylomes', mods: ['HiFi', 'ONT', 'Illumina_WGS', 'HiC', 'Kinnex_IsoSeq', 'HiFi_5mC', 'ONT_5mC'] },
  { label: 'Genome + epigenome + expression', mods: ['HiFi', 'HiFi_5mC', 'FiberSeq', 'Kinnex_IsoSeq', 'MAGE_RNA'] },
  { label: 'HGSVC eight-assay set', mods: ['Illumina_WGS', 'HiFi', 'ONT', 'StrandSeq', 'HiC', 'Bionano', 'RNAseq', 'Kinnex_IsoSeq'] },
  { label: 'Blood vs LCL', mods: ['WholeBlood', 'HiFi_5mC'] },
  { label: 'Clear', mods: [] },
];

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (tag, props = {}, kids = []) => {
  const n = Object.assign(document.createElement(tag), props);
  for (const k of [].concat(kids)) if (k != null) n.append(k);
  return n;
};
const fmtN = n => n.toLocaleString('en-US');
const fmtBytes = b => {
  if (!b) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0, v = b;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v < 10 && i > 1 ? v.toFixed(1) : Math.round(v)} ${u[i]}`;
};

/* ---------------- state ---------------- */

let CAT = null;
let donors = new Map();   // sample_id -> {id, cohorts:Set, mods:Set, rows:[]}
const state = {
  view: 'combine',
  selected: [],           // ordered — the cascade depends on insertion order
  combineCohort: '',
  q: '', cohort: '', pop: '', sex: '', release: '', has: '',
  sort: { key: 'sample_id', dir: 1 },
};

/* ---------------- boot ---------------- */

init();

async function init() {
  initTheme();
  try {
    CAT = await fetch(DATA + 'catalog.json').then(r => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      return r.json();
    });
  } catch (err) {
    $('main').innerHTML =
      `<div class="card"><h2>Could not load the catalogue</h2>
       <p class="hint">Fetching <code>${DATA}catalog.json</code> failed: ${err.message}.
       If you are opening this file directly from disk, browsers block <code>fetch</code> on
       <code>file://</code> — serve the folder instead, e.g. <code>python3 -m http.server</code>.</p></div>`;
    return;
  }
  INFO = CAT.modality_info;

  for (const row of CAT.samples) {
    let d = donors.get(row.sample_id);
    if (!d) donors.set(row.sample_id, d = { id: row.sample_id, cohorts: new Set(), mods: new Set(), rows: [] });
    d.cohorts.add(row.cohort);
    d.rows.push(row);
    for (const m of row.modalities) d.mods.add(m);
  }

  renderStats();
  renderPicker();
  renderPresets();
  renderCoverage();
  renderShapeCompare();
  renderIntersections();
  renderOtherDonors();
  initSampleFilters();
  wireEvents();
  readHash();
  $('#combine-cohort').value = state.combineCohort;
  showView(state.view);
  renderCombine();
  renderSamples();
  $('#footer-note').textContent =
    `Catalogue generated ${CAT.generated_utc.slice(0, 10)} · ` +
    `${fmtN(CAT.samples.length)} cohort records over ${fmtN(CAT.unique_samples)} donors · ` +
    `${fmtN(CAT.totals.files)} data files attributed to a donor.`;
}

/* ---------------- header ---------------- */

function renderStats() {
  const cohortMods = CAT.modalities.filter(m => !isExt(m));
  const stats = [
    ['Donors', fmtN(CAT.unique_samples)],
    ['Cohort assay types', fmtN(cohortMods.length)],
    ['Other-project datasets', fmtN(CAT.modalities.length - cohortMods.length)],
    ['Data files attributed', fmtN(CAT.totals.files)],
    ['HPRC volume', `${(CAT.totals.bytes / 1e12).toFixed(0)} TB`],
    ['In both cohorts', String(CAT.dual_cohort_samples.length)],
  ];
  $('#stat-row').replaceChildren(...stats.map(([k, v]) =>
    el('li', {}, [el('span', { className: 'v', textContent: v }), el('span', { className: 'k', textContent: k })])));
}

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem('theme'); } catch { /* storage blocked */ }
  if (saved) document.documentElement.dataset.theme = saved;
  $('#theme-toggle').addEventListener('click', () => {
    const cur = document.documentElement.dataset.theme;
    const next = cur === 'dark' ? 'light' : cur === 'light' ? '' : 'dark';
    try {
      if (next) { document.documentElement.dataset.theme = next; localStorage.setItem('theme', next); }
      else { delete document.documentElement.dataset.theme; localStorage.removeItem('theme'); }
    } catch { /* non-fatal */ }
  });
}

/* ---------------- matching ---------------- */

// With no cohort filter a donor's assays are pooled across both consortia — that is
// what "this sample has all of X" means when planning an analysis. Picking a cohort
// switches to the strict reading: the cohort assays must all come from that consortium.
// External (other-project) data belongs to the donor, so it is on both cohort rows.
function matchingDonors(mods, cohort) {
  const out = [];
  for (const d of donors.values()) {
    if (cohort) {
      const row = d.rows.find(r => r.cohort === cohort);
      if (row && mods.every(m => row.modalities.includes(m))) out.push({ donor: d, row });
    } else if (mods.every(m => d.mods.has(m))) {
      out.push({ donor: d, row: d.rows[0] });
    }
  }
  return out;
}

const countIn = (hits, cohort) => hits.filter(h => h.donor.cohorts.has(cohort)).length;
const FUNCTIONAL_EXT = ['MAGE_RNA', 'Geuvadis_RNA', 'Geuvadis_smallRNA', 'ENCODE_RNA', 'ENCODE_Access',
  'ENCODE_ChIP', 'ENCODE_3D', 'ENCODE_DNAme'];
// donors having ANY of mods
const union = mods => new Set(mods.flatMap(m => matchingDonors([m], '').map(h => h.donor.id))).size;

/* ---------------- shareable URL state ---------------- */

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  const a = p.get('assays');
  if (a) state.selected = a.split(',').filter(m => CAT.modalities.includes(m));
  const c = p.get('cohort');
  if (COHORTS.includes(c)) state.combineCohort = c;
  if (c === 'HGSVC3') state.combineCohort = 'HGSVC';   // links made before the rename
  const v = p.get('view');
  if (['combine', 'coverage', 'samples'].includes(v)) state.view = v;
}

function writeHash() {
  const p = new URLSearchParams();
  if (state.selected.length) p.set('assays', state.selected.join(','));
  if (state.combineCohort) p.set('cohort', state.combineCohort);
  if (state.view !== 'combine') p.set('view', state.view);
  const s = p.toString();
  try {
    history.replaceState(null, '', s ? '#' + s : location.pathname + location.search);
  } catch { /* non-fatal: the URL just stops tracking state */ }
}

function showView(name) {
  state.view = name;
  for (const o of $$('.tabs button')) o.setAttribute('aria-selected', String(o.dataset.view === name));
  for (const v of $$('.view')) v.hidden = v.id !== `view-${name}`;
  writeHash();
}

/* ---------------- combine view ---------------- */

function groups() {
  const out = new Map();
  for (const m of CAT.modalities) {
    const g = INFO[m].group;
    if (!out.has(g)) out.set(g, []);
    out.get(g).push(m);
  }
  return [...out];
}

function ranMarks(m) {
  const cov = CAT.coverage[m];
  const ran = el('span', { className: 'ran' });
  if (isExt(m)) ran.append(el('i', { className: 'ext' }));
  else ran.append(...COHORTS.map(c => el('i', { className: cov[c].ran ? CSS[c] : '' })));
  return ran;
}

function chipTitle(m) {
  const cov = CAT.coverage[m];
  const parts = COHORTS.map(c => `${c}: ${cov[c].ran ? cov[c].n + ' donors' : 'none'}`);
  return `${label(m)} — ${parts.join(' · ')}${INFO[m].note ? '\n' + INFO[m].note : ''}`;
}

function renderPicker() {
  $('#mod-picker').replaceChildren(...groups().map(([name, mods]) => {
    const list = el('div', { className: 'mod-list' });
    for (const m of mods) {
      const btn = el('button', {
        className: 'mod-chip' + (isLegacy(m) ? ' legacy' : '') + (isExt(m) ? ' external' : ''),
        type: 'button', title: chipTitle(m),
      });
      btn.dataset.mod = m;
      btn.setAttribute('aria-pressed', 'false');
      btn.append(ranMarks(m), el('span', { className: 'nm', textContent: label(m) }),
                 el('span', { className: 'ct', textContent: '' }));
      btn.addEventListener('click', () => toggleMod(m));
      list.append(btn);
    }
    const h = el('h3', { textContent: name });
    const kids = [h, list];
    if (mods.some(isExt)) kids.splice(1, 0, el('p', { className: 'group-hint',
      textContent: 'Other projects that sequenced the same 1000 Genomes cell lines.' }));
    return el('div', { className: 'mod-group' }, kids);
  }));
}

function renderPresets() {
  $('#presets').replaceChildren(...PRESETS.map(p => {
    const b = el('button', { type: 'button', textContent: p.label });
    b.addEventListener('click', () => { state.selected = [...p.mods]; renderCombine(); });
    return b;
  }));
}

function toggleMod(m) {
  const i = state.selected.indexOf(m);
  if (i === -1) state.selected.push(m); else state.selected.splice(i, 1);
  renderCombine();
}

function renderCombine() {
  const { selected, combineCohort } = state;
  for (const b of $$('#mod-picker .mod-chip')) {
    const m = b.dataset.mod;
    b.setAttribute('aria-pressed', String(selected.includes(m)));
    b.querySelector('.ct').textContent = fmtN(matchingDonors([m], combineCohort).length);
  }

  const hits = matchingDonors(selected, combineCohort);
  const n = Object.fromEntries(COHORTS.map(c => [c, countIn(hits, c)]));

  $('#result-n').textContent = fmtN(hits.length);
  $('#result-of').textContent = selected.length
    ? `donors with ${selected.map(label).join(' + ')}`
    : 'donors in the catalogue — select assays to narrow';

  // grouped bar — both rows share one scale; each is direct-labelled and
  // carries its own swatch, so identity never rests on colour alone
  const max = Math.max(...Object.values(n), 1);
  $('#split').replaceChildren(...COHORTS.map(c => el('div', {
    className: 'split-row', title: `${fmtN(n[c])} of ${fmtN(CAT.cohorts[c])} ${c} donors`,
  }, [
    el('span', { className: 'lbl' }, [el('i', { style: `background:var(--${CSS[c]})` }),
      document.createTextNode('in ' + c)]),
    el('div', { className: 'track' }, [el('span', { style: `width:${(n[c] / max) * 100}%;background:var(--${CSS[c]})` })]),
    el('span', { className: 'v', textContent: fmtN(n[c]) }),
  ])));

  const both = n.HPRC + n.HGSVC - hits.length;
  $('#result-note').replaceChildren(...(both > 0 && !combineCohort
    ? [el('div', { className: 'callout info' }, [
        el('strong', { textContent: `${both} of these donors appear in both cohorts. ` }),
        document.createTextNode(
          'Their assays are pooled across consortia, so the two cohort figures above sum to more ' +
          'than the total. Pick a single cohort below to require that all assays come from one consortium.')])]
    : []));

  writeHash();
  renderCascade();
  renderCostList();
  renderCombineTable(hits);
}

function renderCascade() {
  const card = $('#cascade-card');
  if (!state.selected.length) { card.hidden = true; return; }
  card.hidden = false;

  const rows = [{ label: 'Any donor', mods: [] }];
  state.selected.forEach((m, i) => rows.push({ label: label(m), mods: state.selected.slice(0, i + 1) }));

  const counts = rows.map(r => {
    const hits = matchingDonors(r.mods, state.combineCohort);
    return { ...r, n: hits.length, h: countIn(hits, 'HPRC'), g: countIn(hits, 'HGSVC') };
  });
  const max = counts[0].n || 1;
  const fill = state.combineCohort ? `var(--${CSS[state.combineCohort]})` : 'var(--axis)';

  $('#cascade').replaceChildren(...counts.map((c, i) => {
    const prev = i ? counts[i - 1].n : null;
    const pct = prev ? Math.round(((prev - c.n) / prev) * 100) : 0;
    const lab = el('div', { className: 'step-label' });
    if (i === 0) lab.textContent = c.label;
    else lab.append(document.createTextNode('+ '), el('b', { textContent: c.label }));
    // one measure per row (donors still matching), so one bar — never a stack
    const track = el('div', { className: 'track', title: `${fmtN(c.n)} donors — HPRC ${fmtN(c.h)}, HGSVC ${fmtN(c.g)}` });
    track.append(el('span', { style: `width:${(c.n / max) * 100}%;background:${fill}` }));
    const nn = el('div', { className: 'n' }, [document.createTextNode(fmtN(c.n))]);
    if (prev !== null && pct > 0) nn.append(el('div', { className: 'drop', textContent: `−${pct}%` }));
    return el('li', {}, [lab, track, nn]);
  }));
}

function renderCostList() {
  const rest = CAT.modalities.filter(m => !state.selected.includes(m));
  const rows = rest.map(m => ({
    m, n: matchingDonors([...state.selected, m], state.combineCohort).length,
  })).sort((a, b) => b.n - a.n);

  $('#cost-list').replaceChildren(...rows.map(({ m, n }) => {
    const b = el('button', { type: 'button', title: `Add ${label(m)} to the requirement` });
    b.append(el('span', { className: 'nm' + (isLegacy(m) ? ' legacy' : '') + (isExt(m) ? ' ext' : ''), textContent: label(m) }),
             el('span', { className: 'n' + (n ? '' : ' zero'), textContent: fmtN(n) }));
    b.addEventListener('click', () => toggleMod(m));
    return el('li', {}, [b]);
  }));
}

function renderCombineTable(hits) {
  const tb = $('#combine-table tbody');
  const rows = hits.slice(0, 400);
  $('#combine-count').textContent =
    hits.length > rows.length ? `showing first ${rows.length} of ${fmtN(hits.length)}` : `${fmtN(hits.length)} donors`;
  $('#combine-empty').hidden = hits.length > 0;
  $('#combine-empty').textContent = 'No donor carries every assay you selected.';
  tb.replaceChildren(...rows.map(h => sampleRow(h.row, h.donor)));
}

/* ---------------- shared row rendering ---------------- */

function cohortBadge(c) {
  return el('span', { className: `badge cohort-${CSS[c] || 'hgsvc'}`, textContent: c });
}

const EVIDENCE_TEXT = {
  both: 'Asserted by the HGSVC production sheet and present on IGSR.',
  sheet_only: 'Asserted by the production sheet, but no file on IGSR. You cannot download this from here.',
  igsr_only: 'Found on IGSR but not asserted in the production sheet.',
  nygc_30x: 'Not in the HGSVC directories, but the 1000 Genomes NYGC 30x data for this donor is on IGSR (see the drawer).',
  inline: 'Detected as MM/ML base-modification tags inside the ordinary read BAMs — no separate download needed.',
  'inline+5hmC': 'MM/ML tags for both 5mC and 5hmC inside the ordinary ONT BAMs.',
  dedicated: 'A separate methylation submission (BAMs re-processed with kinetics, or pileups).',
  'inline+dedicated': 'Inline MM/ML tags, plus a dedicated methylation submission.',
  'parents only': 'Only the parents’ reads are present, not the sample’s own.',
};
const evText = v => EVIDENCE_TEXT[v] || v;

function provenanceCell(row) {
  const wrap = el('span', { className: 'badge-row' });
  if (row.cohort === 'HPRC') {
    wrap.append(el('span', { className: 'badge', textContent: row.release || 'untagged',
      title: row.release === 'untagged'
        ? 'This sample has primary data but no row in either release metadata table.'
        : `Listed in the HPRC ${row.release} release table(s).` }));
    if (row.n_files) {
      const pct = Math.round((row.n_indexed / row.n_files) * 100);
      wrap.append(el('span', { className: 'badge', textContent: `${pct}% indexed`,
        title: `${row.n_indexed} of ${row.n_files} data files appear in a human-pangenomics GitHub index. ` +
               'Unindexed files are normal data that postdates the last index refresh, or an assay never indexed.' }));
    }
  } else {
    for (const ph of row.phases || []) wrap.append(el('span', { className: 'badge', textContent: ph,
      title: ph === 'sheet only' ? 'No files for this donor in either IGSR collection.' : `Has files in the IGSR ${ph} collection.` }));
    const ev = Object.values(row.evidence || {});
    const sheetOnly = ev.filter(v => v === 'sheet_only').length;
    if (sheetOnly) wrap.append(el('span', { className: 'badge ev-sheet_only',
      title: 'Assays the production sheet records but that have no file on IGSR.' }, [
      el('i', { className: 'dot' }), document.createTextNode(`${sheetOnly} sheet only`)]));
  }
  return wrap;
}

function sampleRow(row, donor) {
  const tr = el('tr', { className: 'clickable' });
  const idCell = el('td', {}, [el('span', { className: 'sample-id', textContent: row.sample_id })]);
  if (row.aka) idCell.append(el('span', { className: 'aka', textContent: ' ' + row.aka }));
  if (donor && donor.cohorts.size > 1)
    idCell.append(el('span', { className: 'badge', textContent: 'both', title: 'This donor appears in both cohorts.' }));
  const nCohort = row.modalities.filter(m => !isExt(m)).length;
  const nExt = row.modalities.length - nCohort;
  tr.append(
    idCell,
    el('td', {}, [cohortBadge(row.cohort)]),
    el('td', { textContent: row.population || '—' }),
    el('td', { textContent: row.sex || '—' }),
    el('td', { className: 'num', textContent: String(nCohort) }),
    el('td', { className: 'num', textContent: nExt ? String(nExt) : '—' }),
    el('td', { className: 'num', textContent: row.n_files ? fmtN(row.n_files) : '—' }),
    el('td', {}, [provenanceCell(row)]));
  tr.addEventListener('click', () => openDrawer(row.sample_id));
  return tr;
}

/* ---------------- coverage view ---------------- */

function renderCoverage() {
  const cell = (m, c) => {
    const cov = CAT.coverage[m][c];
    if (!cov.ran) return el('td', { className: 'num' }, [
      el('span', { className: 'not-run', textContent: isExt(m) ? 'none' : 'not run',
        title: isExt(m) ? `No ${c} donor appears in this dataset.`
                        : `No ${c} donor has this assay. This is not missing data.` })]);
    const pct = (cov.n / CAT.cohorts[c]) * 100;
    const bar = el('div', { className: 'bar' }, [
      el('span', { style: `width:${pct}%;background:var(--${CSS[c]})` })]);
    return el('td', { className: 'num' }, [el('div', {
      className: 'cov-cell', title: `${cov.n} of ${CAT.cohorts[c]} ${c} donors (${pct.toFixed(0)}%)`,
    }, [bar, el('span', { className: 'n', textContent: fmtN(cov.n) })])]);
  };

  const body = [];
  for (const [g, mods] of groups()) {
    body.push(el('tr', { className: 'group-row' }, [el('td', { colSpan: 4, textContent: g })]));
    const sorted = mods.slice().sort((a, b) =>
      matchingDonors([b], '').length - matchingDonors([a], '').length);
    for (const m of sorted) {
      const nm = el('td', {}, [el('span', { textContent: label(m),
        className: isLegacy(m) ? 'legacy-name' : '' })]);
      if (INFO[m].note) nm.title = INFO[m].note;
      body.push(el('tr', {}, [nm, ...COHORTS.map(c => cell(m, c)),
        el('td', { className: 'num', textContent: fmtN(matchingDonors([m], '').length) })]));
    }
  }
  $('#coverage-table tbody').replaceChildren(...body);
}

function renderShapeCompare() {
  const core5 = ['HiFi', 'ONT', 'Illumina_WGS', 'HiC', 'Kinnex_IsoSeq'];
  const eight = ['Illumina_WGS', 'HiFi', 'ONT', 'StrandSeq', 'HiC', 'Bionano', 'RNAseq', 'Kinnex_IsoSeq'];
  const pct = (n, d) => `${Math.round((n / d) * 100)}%`;
  const N = (mods, c) => matchingDonors(mods, c).length;
  const hS = CAT.cohorts.HPRC, gS = CAT.cohorts.HGSVC;
  const h5 = N(core5, 'HPRC'), hF = N([...core5, 'FiberSeq'], 'HPRC');
  const hM = N([...core5, 'HiFi_5mC', 'ONT_5mC'], 'HPRC');
  const g8 = N(eight, 'HGSVC'), h8 = N(eight, 'HPRC');

  $('#shape-compare').replaceChildren(
    el('div', { className: 'callout info' }, [
      el('strong', { textContent: 'Need scale on the core assays → HPRC. ' }),
      document.createTextNode(`${h5} of its ${hS} donors (${pct(h5, hS)}) carry all five core assays, and ` +
        `${hM} of those also have both a HiFi and an ONT methylome inline. It thins out at Fiber-seq: ${hF}.`)]),
    el('div', { className: 'callout' }, [
      el('strong', { textContent: 'Need many assays on the same donor → HGSVC. ' }),
      document.createTextNode(`${g8} of its ${gS} donors (${pct(g8, gS)}) carry all eight of ` +
        `Illumina, HiFi, ONT, Strand-seq, Hi-C, Bionano, RNA-seq and Kinnex/Iso-Seq. ` +
        `In HPRC the same eight-way intersection is ${h8}.`)]),
    el('div', { className: 'callout' }, [
      el('strong', { textContent: 'The cohorts barely overlap. ' }),
      document.createTextNode(`Only ${CAT.dual_cohort_samples.length} donors appear in both ` +
        `(${CAT.dual_cohort_samples.join(', ')}), so the union is close to additive.`)]),
    el('div', { className: 'callout' }, [
      el('strong', { textContent: 'Other projects add functional data for a minority of donors. ' }),
      document.createTextNode(`${fmtN(union(FUNCTIONAL_EXT))} of ${fmtN(CAT.unique_samples)} donors have RNA-seq or ` +
        `ENCODE data from another project on the same cell line: ${N(['MAGE_RNA'], '')} MAGE, ` +
        `${N(['Geuvadis_RNA'], '')} Geuvadis, ${union(FUNCTIONAL_EXT.filter(m => m.startsWith('ENCODE')))} ENCODE. ` +
        `Separately, ${N(['NYGC_30x'], '')} have the 1kGP NYGC 30x genome.`)]));
}

function renderIntersections() {
  const rows = [];
  for (const [cohort, obj] of Object.entries(CAT.intersections))
    for (const [k, v] of Object.entries(obj)) rows.push([cohort, k, v]);
  $('#intersections-table tbody').replaceChildren(...rows.map(([c, k, v]) =>
    el('tr', {}, [el('td', {}, [cohortBadge(c)]), el('td', { textContent: k }),
      el('td', { className: 'num', textContent: fmtN(v) })])));
}

function renderOtherDonors() {
  const rows = (CAT.other_donors || []).slice().sort((a, b) => b.bytes - a.bytes);
  $('#other-donors tbody').replaceChildren(...rows.map(d => el('tr', {}, [
    el('td', {}, [el('span', { className: 'sample-id', textContent: d.donor })]),
    el('td', { textContent: d.modalities.map(label).join(', ') }),
    el('td', { textContent: d.context.join(', ') || '—' }),
    el('td', { className: 'num', textContent: fmtN(d.files) }),
    el('td', { className: 'num', textContent: fmtBytes(d.bytes) }),
    el('td', { className: 'small', textContent: d.submissions.join(', ') })])));
}

/* ---------------- samples view ---------------- */

function initSampleFilters() {
  const pops = [...new Set(CAT.samples.map(s => s.population).filter(Boolean))].sort();
  $('#sample-pop').append(...pops.map(p => el('option', { value: p, textContent: p })));
  for (const [g, mods] of groups()) {
    const og = el('optgroup', { label: g });
    og.append(...mods.map(m => el('option', { value: m, textContent: label(m) })));
    $('#sample-has').append(og);
  }
}

function filteredSamples() {
  const q = state.q.trim().toLowerCase();
  let rows = CAT.samples.filter(s =>
    (!state.cohort || s.cohort === state.cohort) &&
    (!state.pop || s.population === state.pop) &&
    (!state.sex || (s.sex || '').toLowerCase() === state.sex) &&
    (!state.release || (s.release || '').split('+').includes(state.release)) &&
    (!state.has || s.modalities.includes(state.has)) &&
    (!q || s.sample_id.toLowerCase().includes(q) || (s.aka || '').toLowerCase().includes(q) ||
     (s.population || '').toLowerCase().includes(q)));

  const { key, dir } = state.sort;
  const val = s => key === 'n_mod' ? s.modalities.filter(m => !isExt(m)).length
    : key === 'n_ext' ? s.modalities.filter(isExt).length
    : key === 'n_files' ? (s.n_files || 0)
    : key === 'bytes' ? (s.bytes || 0)
    : (s[key] || '');
  rows.sort((a, b) => {
    const x = val(a), y = val(b);
    return (typeof x === 'number' ? x - y : String(x).localeCompare(String(y))) * dir;
  });
  return rows;
}

function renderSamples() {
  const rows = filteredSamples();
  const shown = rows.slice(0, 400);
  $('#sample-count').textContent = rows.length > shown.length
    ? `showing first ${shown.length} of ${fmtN(rows.length)} records`
    : `${fmtN(rows.length)} records`;
  $('#sample-empty').hidden = rows.length > 0;
  $('#sample-table tbody').replaceChildren(...shown.map(r => {
    const tr = sampleRow(r, donors.get(r.sample_id));
    tr.insertBefore(el('td', { className: 'num', textContent: r.bytes ? fmtBytes(r.bytes) : '—' }),
                    tr.children[7]);
    return tr;
  }));
}

/* ---------------- drawer ---------------- */

const shardCache = new Map();
async function loadShard(sampleId, cohort, dual) {
  const name = dual ? `${sampleId}.${cohort}.json` : `${sampleId}.json`;
  if (!shardCache.has(name)) {
    shardCache.set(name, fetch(DATA + 'files/' + name)
      .then(r => (r.ok ? r.json() : null)).catch(() => null));
  }
  return shardCache.get(name);
}

async function openDrawer(sampleId) {
  const d = donors.get(sampleId);
  if (!d) return;
  const dual = d.cohorts.size > 1;
  const first = d.rows[0];

  $('#drawer-title').textContent = sampleId + (first.aka ? `  (${first.aka})` : '');
  $('#drawer-meta').replaceChildren(...d.rows.map(r => cohortBadge(r.cohort)),
    document.createTextNode(' ' + (first.population || '') + (first.sex ? ` · ${first.sex}` : '')));
  $('#drawer').classList.add('open');
  $('#drawer').setAttribute('aria-hidden', 'false');
  $('#drawer-scrim').classList.add('open');

  const body = $('#drawer-body');
  body.replaceChildren(el('p', { className: 'hint', textContent: 'Loading file list…' }));
  const parts = [];
  let external = null;

  for (const row of d.rows) {
    parts.push(el('h3', { textContent: `${row.cohort} record` }));

    const kv = el('dl', { className: 'kv' });
    const add = (k, v) => { if (v) kv.append(el('dt', { textContent: k }), el('dd', { textContent: v })); };
    add('Population', row.population);
    add('Super-population', row.superpop);
    add('Sex', row.sex);
    if (row.cohort === 'HPRC') {
      add('Release', row.release);
      add('Files indexed', row.n_files ? `${fmtN(row.n_indexed)} of ${fmtN(row.n_files)} on GitHub` : null);
      add('Total size', row.bytes ? fmtBytes(row.bytes) : null);
    } else {
      add('IGSR collections', (row.phases || []).join(', '));
      add('Family', row.family_id);
      add('Relation', row.relation);
      add('Project year', row.project_yr);
      add('Strand-seq libraries', row.strandseq_libs ? String(row.strandseq_libs) : null);
    }
    parts.push(kv);

    parts.push(el('h3', { textContent: 'Assays' }));
    const badges = el('div', { className: 'badge-row' });
    for (const m of row.modalities.filter(m => !isExt(m))) {
      const ev = row.evidence?.[m];
      const cls = ev === 'sheet_only' ? ' ev-sheet_only' : ev === 'both' || ev === 'nygc_30x' ? ' ev-both' : '';
      const b = el('span', { className: 'badge' + cls });
      if (ev) b.append(el('i', { className: 'dot' }));
      b.append(document.createTextNode(label(m) + (ev && ev !== 'both' ? ` · ${ev.replace('_', ' ')}` : '')));
      b.title = [INFO[m].note, ev ? evText(ev) : ''].filter(Boolean).join('\n');
      badges.append(b);
    }
    parts.push(badges);

    if (row.cohort === 'HGSVC') {
      const so = Object.entries(row.evidence || {}).filter(([, v]) => v === 'sheet_only').map(([k]) => label(k));
      if (so.length) parts.push(el('div', { className: 'callout' }, [
        el('strong', { textContent: 'Asserted but not downloadable here: ' }),
        document.createTextNode(so.join(', ') + '. The production sheet records these, but no matching file is on IGSR.')]));
    }

    const shard = await loadShard(sampleId, row.cohort, dual);
    if (shard?.external && Object.keys(shard.external).length) external = shard.external;
    parts.push(el('h3', { textContent: 'Files' }));
    parts.push(...renderFiles(shard, row));
  }

  parts.push(el('h3', { textContent: 'Same donor, other projects' }));
  parts.push(...renderExternal(external));
  body.replaceChildren(...parts);
}

function renderExternal(ext) {
  if (!ext) return [el('p', { className: 'hint', textContent:
    'This donor does not appear in NYGC 30x, 1KG-ONT, Geuvadis, MAGE or ENCODE.' })];
  const out = [];
  for (const [m, items] of Object.entries(ext).sort()) {
    const det = el('details', { className: 'file-group' });
    const sum = el('summary', {}, [document.createTextNode(label(m)),
      el('span', { className: 'fg-n', textContent: `${items.length} ${m.startsWith('ENCODE') ? 'experiments' : 'files'}` })]);
    const ul = el('ul', { className: 'file-list' });
    for (const it of items.slice(0, 200)) {
      if (m.startsWith('ENCODE')) {
        const [acc, assay, target] = it.split('|');
        ul.append(el('li', {}, [
          el('a', { href: `https://www.encodeproject.org/experiments/${acc}/`, textContent: acc, rel: 'noopener', target: '_blank' }),
          el('span', { className: 'badge', textContent: assay + (target ? ` · ${target}` : '') })]));
      } else {
        ul.append(el('li', {}, [el('a', { href: it, textContent: it.split('/').pop(), title: it, rel: 'noopener' })]));
      }
    }
    if (items.length > 200) ul.append(el('li', {}, [el('span', { className: 'hint', textContent: `…and ${items.length - 200} more.` })]));
    det.append(sum, ul);
    out.push(det);
  }
  return out;
}

function renderFiles(shard, row) {
  if (!shard || !shard.files.length) {
    return [el('p', { className: 'hint', textContent:
      'No files on the archive are attributed to this record. For HGSVC that usually means the ' +
      'data is asserted by the production sheet but held elsewhere.' })];
  }
  const isHprc = shard.cohort === 'HPRC';
  const files = isHprc ? shard.files.filter(f => f[6] !== 'index') : shard.files;
  const byMod = new Map();
  for (const f of files) {
    if (!byMod.has(f[0])) byMod.set(f[0], []);
    byMod.get(f[0]).push(f);
  }
  const out = [];
  const allUrls = shard.files.map(f => shard.prefix + f[1]);
  const bar = el('div', { className: 'controls' });
  bar.append(
    btn('Copy all URLs', () => navigator.clipboard.writeText(allUrls.join('\n'))),
    btn('Download URL list', () => downloadText(`${row.sample_id}_${row.cohort}_urls.txt`, allUrls.join('\n'))));
  if (isHprc && files.length < shard.files.length)
    bar.append(el('span', { className: 'count-note', textContent: `${shard.files.length - files.length} index files (.pbi/.bai/.crai) hidden; included in the URL list` }));
  out.push(bar);

  for (const [mod, fl] of [...byMod].sort((a, b) => b[1].length - a[1].length)) {
    const det = el('details', { className: 'file-group' });
    const sum = el('summary');
    sum.append(document.createTextNode(label(mod)));
    if (isHprc) {
      const bytes = fl.reduce((a, f) => a + f[2], 0);
      sum.append(el('span', { className: 'fg-n', textContent: `${fl.length} files · ${fmtBytes(bytes)}` }));
    } else {
      sum.append(el('span', { className: 'fg-n', textContent: `${fl.length} files` }));
    }
    const ul = el('ul', { className: 'file-list' });
    for (const f of fl.slice(0, 200)) {
      const url = shard.prefix + f[1];
      const li = el('li', {}, [el('a', { href: url, textContent: f[1].split('/').pop(), title: url, rel: 'noopener' })]);
      if (isHprc) {
        li.append(el('span', { className: 'sz', textContent: fmtBytes(f[2]) }));
        if (f[7]) li.append(el('span', { className: 'badge', textContent: f[7] }));
        if (f[4]) li.append(el('span', { className: 'badge', textContent: f[4] }));
        if (f[6] === 'report') li.append(el('span', { className: 'badge', textContent: 'report' }));
        if (!f[3]) li.append(el('span', { className: 'badge muted', textContent: 'unindexed',
          title: 'Not in any human-pangenomics GitHub index — usually means it postdates the last refresh.' }));
      } else {
        li.append(el('span', { className: 'badge', textContent: f[2] }));
        if (f[3]) li.append(el('span', { className: 'badge', textContent: f[3] }));
      }
      ul.append(li);
    }
    if (fl.length > 200) ul.append(el('li', {}, [el('span', { className: 'hint',
      textContent: `…and ${fl.length - 200} more. Use “Download URL list” for the complete set.` })]));
    det.append(sum, ul);
    out.push(det);
  }
  return out;
}

function btn(text, fn) {
  const b = el('button', { className: 'btn', type: 'button', textContent: text });
  b.addEventListener('click', () => { fn(); b.textContent = 'Done ✓'; setTimeout(() => (b.textContent = text), 1400); });
  return b;
}

function downloadText(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));
  const a = el('a', { href: url, download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function closeDrawer() {
  $('#drawer').classList.remove('open');
  $('#drawer').setAttribute('aria-hidden', 'true');
  $('#drawer-scrim').classList.remove('open');
}

/* ---------------- events ---------------- */

function wireEvents() {
  for (const t of $$('.tabs button')) t.addEventListener('click', () => showView(t.dataset.view));

  $('#combine-cohort').addEventListener('change', e => { state.combineCohort = e.target.value; renderCombine(); });
  $('#combine-export').addEventListener('click', () => {
    exportCsv('matching_samples.csv', matchingDonors(state.selected, state.combineCohort).map(h => h.row));
  });
  $('#combine-link').addEventListener('click', () => {
    navigator.clipboard.writeText(location.href);
    const b = $('#combine-link'); b.textContent = 'Link copied ✓';
    setTimeout(() => (b.textContent = 'Copy link to this selection'), 1500);
  });

  $('#sample-search').addEventListener('input', e => { state.q = e.target.value; renderSamples(); });
  for (const [sel, key] of [['#sample-cohort', 'cohort'], ['#sample-pop', 'pop'], ['#sample-sex', 'sex'],
                            ['#sample-release', 'release'], ['#sample-has', 'has']])
    $(sel).addEventListener('change', e => { state[key] = e.target.value; renderSamples(); });
  $('#sample-export').addEventListener('click', () => exportCsv('samples.csv', filteredSamples()));

  for (const th of $$('#sample-table th.sortable')) {
    th.addEventListener('click', () => {
      const k = th.dataset.key;
      state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : 1 };
      renderSamples();
    });
  }

  $('#drawer-close').addEventListener('click', closeDrawer);
  $('#drawer-scrim').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });
}

function exportCsv(name, rows) {
  const cols = ['sample_id', 'aka', 'cohort', 'population', 'sex', 'release', 'phases', 'n_files', 'bytes', 'modalities'];
  const esc = v => {
    const s = Array.isArray(v) ? v.join(';') : String(v ?? '');
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csv = [cols.join(','), ...rows.map(r => cols.map(c => esc(r[c])).join(','))].join('\n');
  downloadText(name, csv);
}
