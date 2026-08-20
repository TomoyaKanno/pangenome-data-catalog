/* Pangenome Data Catalog — static app over site-data/catalog.json.
   No build step, no dependencies. */

const DATA = 'site-data/';

const LABEL = {
  HiFi: 'PacBio HiFi', ONT: 'ONT long reads', Illumina_WGS: 'Illumina WGS',
  HiC: 'Hi-C / Omni-C', StrandSeq: 'Strand-seq', Bionano: 'Bionano optical',
  Kinnex_IsoSeq: 'Kinnex / Iso-Seq', RNAseq: 'RNA-seq (short read)',
  ONT_directRNA: 'ONT direct RNA', FiberSeq: 'Fiber-seq', HiFi_5mC: 'HiFi 5mC',
  MicroC: 'Micro-C', PacBio_CLR: 'PacBio CLR', Microarray: 'Microarray',
  Karyotype: 'Karyotype', TenX: '10x Genomics', ChrY_ONT: 'chrY ONT',
};
const GROUPS = [
  { name: 'Core assembly',    mods: ['HiFi', 'ONT', 'Illumina_WGS'] },
  { name: 'Structural & 3D',  mods: ['HiC', 'StrandSeq', 'Bionano'] },
  { name: 'Transcriptome',    mods: ['Kinnex_IsoSeq', 'RNAseq', 'ONT_directRNA'] },
  { name: 'Epigenetic',       mods: ['FiberSeq', 'HiFi_5mC', 'MicroC'] },
  { name: 'Other & legacy',   mods: ['PacBio_CLR', 'Microarray', 'Karyotype', 'TenX', 'ChrY_ONT'] },
];
// superseded assays — shown, but visually de-emphasised per the cohort READMEs
const LEGACY = new Set(['PacBio_CLR', 'Microarray', 'Karyotype', 'TenX']);

const PRESETS = [
  { label: 'HPRC core five', mods: ['HiFi', 'ONT', 'Illumina_WGS', 'HiC', 'Kinnex_IsoSeq'] },
  { label: 'Core five + Fiber-seq', mods: ['HiFi', 'ONT', 'Illumina_WGS', 'HiC', 'Kinnex_IsoSeq', 'FiberSeq'] },
  { label: 'Core five + Fiber-seq + HiFi 5mC', mods: ['HiFi', 'ONT', 'Illumina_WGS', 'HiC', 'Kinnex_IsoSeq', 'FiberSeq', 'HiFi_5mC'] },
  { label: 'HGSVC3 eight-assay set', mods: ['Illumina_WGS', 'HiFi', 'ONT', 'StrandSeq', 'HiC', 'Bionano', 'RNAseq', 'Kinnex_IsoSeq'] },
  { label: 'Clear', mods: [] },
];

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (tag, props = {}, kids = []) => {
  const n = Object.assign(document.createElement(tag), props);
  for (const k of [].concat(kids)) n.append(k);
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
  q: '', cohort: '', pop: '', sex: '', release: '',
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
  initSampleFilters();
  wireEvents();
  readHash();
  $('#combine-cohort').value = state.combineCohort;
  showView(state.view);
  renderCombine();
  renderSamples();
  $('#footer-note').textContent =
    `Catalogue generated ${new Date(CAT.generated_utc).toISOString().slice(0, 10)} · ` +
    `${fmtN(CAT.samples.length)} cohort records over ${fmtN(CAT.unique_samples)} donors · ` +
    `${fmtN(CAT.totals.files)} files attributed to a sample.`;
}

/* ---------------- header ---------------- */

function renderStats() {
  const stats = [
    ['Donors', fmtN(CAT.unique_samples)],
    ['Cohorts', '2'],
    ['Assay types', fmtN(CAT.modalities.length)],
    ['Files indexed', fmtN(CAT.totals.files + CAT.totals.unattributed_files + CAT.totals.out_of_cohort_files)],
    ['HPRC volume', `${(CAT.totals.bytes / 1e12).toFixed(0)} TB`],
    ['In both cohorts', String(CAT.dual_cohort_samples.length)],
  ];
  $('#stat-row').replaceChildren(...stats.map(([k, v]) =>
    el('li', {}, [el('span', { className: 'v', textContent: v }), el('span', { className: 'k', textContent: k })])));
}

function initTheme() {
  const saved = localStorage.getItem('theme');
  if (saved) document.documentElement.dataset.theme = saved;
  $('#theme-toggle').addEventListener('click', () => {
    const cur = document.documentElement.dataset.theme;
    const next = cur === 'dark' ? 'light' : cur === 'light' ? '' : 'dark';
    if (next) { document.documentElement.dataset.theme = next; localStorage.setItem('theme', next); }
    else { delete document.documentElement.dataset.theme; localStorage.removeItem('theme'); }
  });
}

/* ---------------- matching ---------------- */

// With no cohort filter a donor's assays are pooled across both consortia — that is
// what "this sample has all of X" means when planning an analysis. Picking a cohort
// switches to the strict reading: the assays must all come from that consortium.
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

/* ---------------- shareable URL state ---------------- */

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  const a = p.get('assays');
  if (a) state.selected = a.split(',').filter(m => CAT.modalities.includes(m));
  const c = p.get('cohort');
  if (c === 'HPRC' || c === 'HGSVC3') state.combineCohort = c;
  const v = p.get('view');
  if (['combine', 'coverage', 'samples'].includes(v)) state.view = v;
}

function writeHash() {
  const p = new URLSearchParams();
  if (state.selected.length) p.set('assays', state.selected.join(','));
  if (state.combineCohort) p.set('cohort', state.combineCohort);
  if (state.view !== 'combine') p.set('view', state.view);
  const s = p.toString();
  // some browsers reject history writes on file:// — the app must still work there
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

function renderPicker() {
  const host = $('#mod-picker');
  host.replaceChildren(...GROUPS.map(g => {
    const list = el('div', { className: 'mod-list' });
    for (const m of g.mods) {
      const cov = CAT.coverage[m];
      const total = cov.HPRC.n + cov.HGSVC3.n;
      const ran = el('span', { className: 'ran' });
      ran.append(el('i', { className: cov.HPRC.ran ? 'hprc' : '' }),
                 el('i', { className: cov.HGSVC3.ran ? 'hgsvc' : '' }));
      const btn = el('button', {
        className: 'mod-chip' + (LEGACY.has(m) ? ' legacy' : ''),
        type: 'button',
        title: `${LABEL[m]} — HPRC: ${cov.HPRC.ran ? cov.HPRC.n + ' samples' : 'not run'} · ` +
               `HGSVC3: ${cov.HGSVC3.ran ? cov.HGSVC3.n + ' samples' : 'not run'}`,
      });
      btn.dataset.mod = m;
      btn.setAttribute('aria-pressed', 'false');
      btn.append(ran, el('span', { className: 'nm', textContent: LABEL[m] }),
                 el('span', { className: 'ct', textContent: fmtN(total) }));
      btn.addEventListener('click', () => toggleMod(m));
      list.append(btn);
    }
    return el('div', { className: 'mod-group' }, [el('h3', { textContent: g.name }), list]);
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
    // unique donors, not the sum of the two cohort figures — otherwise a chip
    // disagrees with the cost list beside it for the 5 dual-cohort donors
    b.querySelector('.ct').textContent = fmtN(matchingDonors([m], combineCohort).length);
  }

  const hits = matchingDonors(selected, combineCohort);
  const nH = countIn(hits, 'HPRC'), nG = countIn(hits, 'HGSVC3');

  $('#result-n').textContent = fmtN(hits.length);
  $('#result-of').textContent = selected.length
    ? `donors with ${selected.map(m => LABEL[m]).join(' + ')}`
    : 'donors in the catalogue — select assays to narrow';

  // grouped bar — both rows share one scale; each is direct-labelled and
  // carries its own swatch, so identity never rests on colour alone
  const max = Math.max(nH, nG, 1);
  const splitRow = (cls, name, n, of) => el('div', {
    className: 'split-row', title: `${fmtN(n)} of ${fmtN(of)} ${name} samples`,
  }, [
    el('span', { className: 'lbl' }, [el('i', { style: `background:var(--${cls})` }),
      document.createTextNode(name)]),
    el('div', { className: 'track' }, [el('span', { style: `width:${(n / max) * 100}%;background:var(--${cls})` })]),
    el('span', { className: 'v', textContent: fmtN(n) }),
  ]);
  $('#split').replaceChildren(
    splitRow('hprc', 'in HPRC', nH, CAT.cohorts.HPRC),
    splitRow('hgsvc', 'in HGSVC3', nG, CAT.cohorts.HGSVC3));

  const both = nH + nG - hits.length;
  $('#result-note').replaceChildren(...(both > 0 && !combineCohort
    ? [el('div', { className: 'callout info' }, [
        el('strong', { textContent: `${both} of these donors appear in both cohorts. ` }),
        document.createTextNode(
          'Their assays are pooled across consortia, so the two cohort figures above sum to more ' +
          'than the total. Pick a single cohort below to require that all assays come from one consortium.')])]
    : []));

  writeHash();
  renderCascade(hits.length);
  renderCostList();
  renderCombineTable(hits);
}

function renderCascade(finalN) {
  const card = $('#cascade-card');
  if (!state.selected.length) { card.hidden = true; return; }
  card.hidden = false;

  const rows = [{ label: 'Any donor', mods: [] }];
  state.selected.forEach((m, i) => rows.push({ label: LABEL[m], mods: state.selected.slice(0, i + 1) }));

  const counts = rows.map(r => {
    const hits = matchingDonors(r.mods, state.combineCohort);
    return { ...r, n: hits.length, h: countIn(hits, 'HPRC'), g: countIn(hits, 'HGSVC3') };
  });
  const max = counts[0].n || 1;

  $('#cascade').replaceChildren(...counts.map((c, i) => {
    const prev = i ? counts[i - 1].n : null;
    const pct = prev ? Math.round(((prev - c.n) / prev) * 100) : 0;
    const label = el('div', { className: 'step-label' });
    if (i === 0) label.textContent = c.label;
    else { label.append(document.createTextNode('+ '), el('b', { textContent: c.label })); }

    // one measure per row (donors still matching), so one bar — never a stack.
    // Under a cohort filter the bar *is* that cohort, so it wears its hue;
    // with both cohorts pooled no single hue would be honest, so it goes neutral.
    const fill = state.combineCohort === 'HPRC' ? 'var(--hprc)'
      : state.combineCohort === 'HGSVC3' ? 'var(--hgsvc)' : 'var(--axis)';
    const track = el('div', { className: 'track', title: `${fmtN(c.n)} donors — HPRC ${fmtN(c.h)}, HGSVC3 ${fmtN(c.g)}` });
    track.append(el('span', { style: `width:${(c.n / max) * 100}%;background:${fill}` }));

    const n = el('div', { className: 'n' }, [document.createTextNode(fmtN(c.n))]);
    if (prev !== null && pct > 0) n.append(el('div', { className: 'drop', textContent: `−${pct}%` }));
    return el('li', {}, [label, track, n]);
  }));
}

function renderCostList() {
  const rest = CAT.modalities.filter(m => !state.selected.includes(m));
  const rows = rest.map(m => ({
    m, n: matchingDonors([...state.selected, m], state.combineCohort).length,
  })).sort((a, b) => b.n - a.n);

  $('#cost-list').replaceChildren(...rows.map(({ m, n }) => {
    const b = el('button', { type: 'button', title: `Add ${LABEL[m]} to the requirement` });
    b.append(el('span', { className: 'nm' + (LEGACY.has(m) ? ' legacy' : ''), textContent: LABEL[m] }),
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
  return el('span', { className: `badge cohort-${c === 'HPRC' ? 'hprc' : 'hgsvc'}`, textContent: c });
}

function provenanceCell(row) {
  const wrap = el('span', { className: 'badge-row' });
  if (row.cohort === 'HPRC') {
    wrap.append(el('span', { className: 'badge', textContent: row.release || 'untagged',
      title: row.release === 'untagged'
        ? 'This sample has primary data but no row in either release metadata table.'
        : `Listed in the HPRC ${row.release} release table.` }));
    if (row.n_files) {
      const pct = Math.round((row.n_indexed / row.n_files) * 100);
      wrap.append(el('span', { className: 'badge', textContent: `${pct}% indexed`,
        title: `${row.n_indexed} of ${row.n_files} files appear in a human-pangenomics GitHub index. ` +
               'Unindexed files are normal data that postdates the last index refresh.' }));
    }
    if (row.stenciling) wrap.append(el('span', { className: 'badge', textContent: '6mA stencilling',
      title: 'Has ONT 6mA chromatin-stencilling basecalls inside the Fiber-seq submission. Not a separate assay.' }));
  } else {
    const sheetOnly = Object.values(row.evidence || {}).filter(v => v === 'sheet_only').length;
    const total = Object.keys(row.evidence || {}).length;
    if (sheetOnly) wrap.append(el('span', { className: 'badge ev-sheet_only' }, [
      el('i', { className: 'dot' }), document.createTextNode(`${sheetOnly}/${total} sheet only`)]));
    else wrap.append(el('span', { className: 'badge ev-both' }, [
      el('i', { className: 'dot' }), document.createTextNode('all on IGSR')]));
    if (row.relation) wrap.append(el('span', { className: 'badge', textContent: row.relation }));
  }
  return wrap;
}

function sampleRow(row, donor) {
  const tr = el('tr', { className: 'clickable' });
  const idCell = el('td', {}, [el('span', { className: 'sample-id', textContent: row.sample_id })]);
  if (donor && donor.cohorts.size > 1)
    idCell.append(el('span', { className: 'badge', textContent: 'both', title: 'This donor appears in both cohorts.' }));
  tr.append(
    idCell,
    el('td', {}, [cohortBadge(row.cohort)]),
    el('td', { textContent: row.population || '—' }),
    el('td', { textContent: row.sex || '—' }),
    el('td', { className: 'num', textContent: String(row.modalities.length) }),
    el('td', { className: 'num', textContent: row.n_files ? fmtN(row.n_files) : '—' }),
    el('td', {}, [provenanceCell(row)]));
  tr.addEventListener('click', () => openDrawer(row.sample_id));
  return tr;
}

/* ---------------- coverage view ---------------- */

function renderCoverage() {
  const sizes = { HPRC: CAT.cohorts.HPRC, HGSVC3: CAT.cohorts.HGSVC3 };
  const rows = CAT.modalities.slice().sort((a, b) => {
    const t = c => CAT.coverage[c].HPRC.n + CAT.coverage[c].HGSVC3.n;
    return t(b) - t(a);
  });

  const cell = (cov, cohort) => {
    if (!cov.ran) return el('td', { className: 'num' }, [
      el('span', { className: 'not-run', textContent: 'not run',
        title: `${cohort} did not run this assay. This is not missing data.` })]);
    const pct = (cov.n / sizes[cohort]) * 100;
    const bar = el('div', { className: 'bar' }, [
      el('span', { style: `width:${pct}%;background:var(--${cohort === 'HPRC' ? 'hprc' : 'hgsvc'})` })]);
    return el('td', { className: 'num' }, [el('div', {
      className: 'cov-cell', title: `${cov.n} of ${sizes[cohort]} ${cohort} samples (${pct.toFixed(0)}%)`,
    }, [bar, el('span', { className: 'n', textContent: fmtN(cov.n) })])]);
  };

  $('#coverage-table tbody').replaceChildren(...rows.map(m => {
    const cov = CAT.coverage[m];
    const nm = el('td', {}, [el('span', {
      textContent: LABEL[m], style: LEGACY.has(m) ? 'color:var(--text-muted);font-style:italic' : '' })]);
    if (LEGACY.has(m)) nm.title = 'Legacy assay, superseded by a current one.';
    return el('tr', {}, [nm, cell(cov.HPRC, 'HPRC'), cell(cov.HGSVC3, 'HGSVC3'),
      el('td', { className: 'num', textContent: fmtN(matchingDonors([m], '').length) })]);
  }));
}

function renderShapeCompare() {
  const core5 = ['HiFi', 'ONT', 'Illumina_WGS', 'HiC', 'Kinnex_IsoSeq'];
  const eight = ['Illumina_WGS', 'HiFi', 'ONT', 'StrandSeq', 'HiC', 'Bionano', 'RNAseq', 'Kinnex_IsoSeq'];
  const pct = (n, d) => `${Math.round((n / d) * 100)}%`;
  const hN = matchingDonors(core5, 'HPRC').length, gN = matchingDonors(eight, 'HGSVC3').length;
  const hS = CAT.cohorts.HPRC, gS = CAT.cohorts.HGSVC3;

  $('#shape-compare').replaceChildren(
    el('div', { className: 'callout info' }, [
      el('strong', { textContent: 'Need scale on the core assays → HPRC. ' }),
      document.createTextNode(`${hN} of its ${hS} samples (${pct(hN, hS)}) carry all five core assays — ` +
        `far more than HGSVC3 holds in total. But it thins out fast beyond them.`)]),
    el('div', { className: 'callout' }, [
      el('strong', { textContent: 'Need many assays on the same donor → HGSVC3. ' }),
      document.createTextNode(`${gN} of its ${gS} samples (${pct(gN, gS)}) carry all eight of ` +
        `Illumina, HiFi, ONT, Strand-seq, Hi-C, Bionano, RNA-seq and Kinnex simultaneously. ` +
        `The equivalent HPRC intersection collapses to about 100.`)]),
    el('div', { className: 'callout' }, [
      el('strong', { textContent: 'The cohorts barely overlap. ' }),
      document.createTextNode(`Only ${CAT.dual_cohort_samples.length} donors appear in both ` +
        `(${CAT.dual_cohort_samples.join(', ')}), so the union is close to additive.`)]));
}

function renderIntersections() {
  const rows = [];
  for (const [cohort, obj] of Object.entries(CAT.intersections))
    for (const [k, v] of Object.entries(obj)) rows.push([cohort, k, v]);
  $('#intersections-table tbody').replaceChildren(...rows.map(([c, k, v]) =>
    el('tr', {}, [el('td', {}, [cohortBadge(c)]), el('td', { textContent: k }),
      el('td', { className: 'num', textContent: fmtN(v) })])));
}

/* ---------------- samples view ---------------- */

function initSampleFilters() {
  const pops = [...new Set(CAT.samples.map(s => s.population).filter(Boolean))].sort();
  $('#sample-pop').append(...pops.map(p => el('option', { value: p, textContent: p })));
}

function filteredSamples() {
  const q = state.q.trim().toLowerCase();
  let rows = CAT.samples.filter(s =>
    (!state.cohort || s.cohort === state.cohort) &&
    (!state.pop || s.population === state.pop) &&
    (!state.sex || (s.sex || '').toLowerCase() === state.sex) &&
    (!state.release || s.release === state.release) &&
    (!q || s.sample_id.toLowerCase().includes(q) || (s.population || '').toLowerCase().includes(q)));

  const { key, dir } = state.sort;
  const val = s => key === 'n_mod' ? s.modalities.length
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
                    tr.children[6]);
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

  $('#drawer-title').textContent = sampleId;
  $('#drawer-meta').replaceChildren(...d.rows.map(r => cohortBadge(r.cohort)),
    document.createTextNode(' ' + (d.rows[0].population || '') + (d.rows[0].sex ? ` · ${d.rows[0].sex}` : '')));
  $('#drawer').classList.add('open');
  $('#drawer').setAttribute('aria-hidden', 'false');
  $('#drawer-scrim').classList.add('open');

  const body = $('#drawer-body');
  body.replaceChildren(el('p', { className: 'hint', textContent: 'Loading file list…' }));
  const parts = [];

  for (const row of d.rows) {
    parts.push(el('h3', { textContent: dual ? `${row.cohort} record` : 'Record' }));

    const kv = el('dl', { className: 'kv' });
    const add = (k, v) => { if (v) kv.append(el('dt', { textContent: k }), el('dd', { textContent: v })); };
    add('Population', row.population);
    add('Sex', row.sex);
    if (row.cohort === 'HPRC') {
      add('Release', row.release);
      add('Files indexed', row.n_files ? `${fmtN(row.n_indexed)} of ${fmtN(row.n_files)} on GitHub` : null);
      add('Total size', row.bytes ? fmtBytes(row.bytes) : null);
    } else {
      add('Family', row.family_id);
      add('Relation', row.relation);
      add('Super-population', row.superpop);
      add('Project year', row.project_yr);
      add('Strand-seq libraries', row.strandseq_libs ? String(row.strandseq_libs) : null);
    }
    parts.push(kv);

    parts.push(el('h3', { textContent: 'Assays' }));
    const badges = el('div', { className: 'badge-row' });
    for (const m of row.modalities) {
      const ev = row.evidence?.[m];
      const b = el('span', { className: 'badge' + (ev ? ` ev-${ev}` : '') });
      if (ev) b.append(el('i', { className: 'dot' }));
      b.append(document.createTextNode(LABEL[m] || m));
      if (ev) b.title = ev === 'both' ? 'Asserted by the production sheet and present on IGSR.'
        : ev === 'sheet_only' ? 'Asserted by the production sheet, but no file on IGSR — often held at the Broad. You cannot download this from here.'
        : ev === 'igsr_only' ? 'Found on IGSR but not asserted in the production sheet.'
        : 'Mixed evidence across the assays folded into this label.';
      badges.append(b);
    }
    parts.push(badges);

    if (row.cohort === 'HGSVC3') {
      const so = Object.entries(row.evidence || {}).filter(([, v]) => v === 'sheet_only').map(([k]) => LABEL[k] || k);
      if (so.length) parts.push(el('div', { className: 'callout' }, [
        el('strong', { textContent: 'Asserted but not downloadable here: ' }),
        document.createTextNode(so.join(', ') + '. The production sheet records these, but no matching file is on the IGSR archive.')]));
    }

    const shard = await loadShard(sampleId, row.cohort, dual);
    parts.push(el('h3', { textContent: 'Files' }));
    parts.push(...renderFiles(shard, row));
  }

  body.replaceChildren(...parts);
}

function renderFiles(shard, row) {
  if (!shard || !shard.files.length) {
    return [el('p', { className: 'hint', textContent:
      'No files on the archive are attributed to this sample. For HGSVC3 that usually means the ' +
      'data is asserted by the production sheet but held elsewhere, or its libraries are pooled.' })];
  }
  const byMod = new Map();
  for (const f of shard.files) {
    if (!byMod.has(f[0])) byMod.set(f[0], []);
    byMod.get(f[0]).push(f);
  }
  const isHprc = shard.cohort === 'HPRC';
  const out = [];

  const allUrls = shard.files.map(f => shard.prefix + f[1]);
  const bar = el('div', { className: 'controls' });
  bar.append(
    btn('Copy all URLs', () => navigator.clipboard.writeText(allUrls.join('\n'))),
    btn('Download URL list', () => downloadText(`${row.sample_id}_${row.cohort}_urls.txt`, allUrls.join('\n'))));
  out.push(bar);

  for (const [mod, files] of [...byMod].sort((a, b) => b[1].length - a[1].length)) {
    const det = el('details', { className: 'file-group' });
    const sum = el('summary');
    sum.append(document.createTextNode(LABEL[mod] || mod));
    if (isHprc) {
      const bytes = files.reduce((a, f) => a + f[2], 0);
      sum.append(el('span', { className: 'fg-n', textContent: `${files.length} files · ${fmtBytes(bytes)}` }));
    } else {
      sum.append(el('span', { className: 'fg-n', textContent: `${files.length} files` }));
    }
    const ul = el('ul', { className: 'file-list' });
    for (const f of files.slice(0, 200)) {
      const url = shard.prefix + f[1];
      const li = el('li', {}, [el('a', { href: url, textContent: f[1].split('/').pop(), title: url, rel: 'noopener' })]);
      if (isHprc) {
        li.append(el('span', { className: 'sz', textContent: fmtBytes(f[2]) }));
        if (!f[3]) li.append(el('span', { className: 'badge', textContent: 'unindexed',
          title: 'Not in any human-pangenomics GitHub index — usually means it postdates the last refresh.' }));
        if (f[4]) li.append(el('span', { className: 'badge', textContent: f[4] }));
      } else if (f[3] && f[3] !== mod) {
        li.append(el('span', { className: 'badge', textContent: f[3], title: 'Original assay name before the unified vocabulary folded it in.' }));
      }
      ul.append(li);
    }
    if (files.length > 200) ul.append(el('li', {}, [el('span', { className: 'hint',
      textContent: `…and ${files.length - 200} more. Use “Download URL list” for the complete set.` })]));
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
  for (const t of $$('.tabs button')) {
    t.addEventListener('click', () => showView(t.dataset.view));
  }

  $('#combine-cohort').addEventListener('change', e => { state.combineCohort = e.target.value; renderCombine(); });
  $('#combine-export').addEventListener('click', () => {
    const hits = matchingDonors(state.selected, state.combineCohort);
    exportCsv('matching_samples.csv', hits.map(h => h.row));
  });
  $('#combine-link').addEventListener('click', () => {
    navigator.clipboard.writeText(location.href);
    const b = $('#combine-link'); b.textContent = 'Link copied ✓';
    setTimeout(() => (b.textContent = 'Copy link to this selection'), 1500);
  });

  const bind = (sel, key) => $(sel).addEventListener('input', e => { state[key] = e.target.value; renderSamples(); });
  bind('#sample-search', 'q');
  for (const [sel, key] of [['#sample-cohort', 'cohort'], ['#sample-pop', 'pop'], ['#sample-sex', 'sex'], ['#sample-release', 'release']])
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
  const cols = ['sample_id', 'cohort', 'population', 'sex', 'release', 'n_files', 'bytes', 'modalities'];
  const esc = v => {
    const s = Array.isArray(v) ? v.join(';') : v ?? '';
    return /[",\n]/.test(s) ? `"${String(s).replace(/"/g, '""')}"` : s;
  };
  const csv = [cols.join(','), ...rows.map(r => cols.map(c => esc(r[c])).join(','))].join('\n');
  downloadText(name, csv);
}
