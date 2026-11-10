/* Pangenome Data Catalog — static app over site-data/catalog.json.
   No build step, no dependencies. Data-type names, groups, descriptions and which rows
   are shown by default all come from the payload (catalog.modality_info). */

const DATA = 'site-data/';
const COHORTS = ['HPRC', 'HGSVC'];
const CSS = { HPRC: 'hprc', HGSVC: 'hgsvc' };
const WHERE = { HPRC: 'AWS S3', HGSVC: 'IGSR FTP' };

let INFO = {};
const label = m => INFO[m]?.label || m;
const short = m => INFO[m]?.short || label(m);
const isExt = m => INFO[m]?.scope === 'external';
const isMain = m => INFO[m]?.tier === 'main';

const PRESETS = [
  { label: 'Long reads + methylation', mods: ['HiFi', 'ONT', 'HiFi_5mC', 'ONT_5mC'] },
  { label: '+ Fiber-seq + RNA', mods: ['HiFi', 'ONT', 'HiFi_5mC', 'ONT_5mC', 'FiberSeq', 'Kinnex_IsoSeq'] },
  { label: 'Everything HGSVC has', mods: ['HiFi', 'ONT', 'Illumina_WGS', 'Kinnex_IsoSeq', 'RNAseq', 'HiC', 'StrandSeq', 'Bionano'] },
];

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (tag, props = {}, kids = []) => {
  const n = Object.assign(document.createElement(tag), props);
  for (const k of [].concat(kids)) if (k != null) n.append(k);
  return n;
};
const txt = s => document.createTextNode(s);
const fmtN = n => n.toLocaleString('en-US');
const fmtBytes = b => {
  if (!b) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0, v = b;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v < 10 && i > 1 ? v.toFixed(1) : Math.round(v)} ${u[i]}`;
};
const plural = (n, w) => `${fmtN(n)} ${w}${n === 1 ? '' : 's'}`;

/* ---------------- state ---------------- */

let CAT = null;
let donors = new Map();   // id -> {id, cohorts:Set, mods:Set, rows:[]}
let MAIN = [];            // main-tier cohort data types: the columns of the donor grid
const state = {
  view: 'overview',
  selected: [],           // ordered — the narrowing list follows the order picked
  cohort: '', q: '', pop: '',
  showAll: false, pickerAll: false,
  sort: { key: 'id', dir: 1 },
};

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
       If you opened this file directly from disk, serve the folder instead, e.g.
       <code>python3 -m http.server</code>.</p></div>`;
    return;
  }
  INFO = CAT.modality_info;
  MAIN = CAT.modalities.filter(m => isMain(m) && !isExt(m));

  for (const row of CAT.samples) {
    let d = donors.get(row.sample_id);
    if (!d) donors.set(row.sample_id, d = { id: row.sample_id, cohorts: new Set(), mods: new Set(), rows: [] });
    d.cohorts.add(row.cohort);
    d.rows.push(row);
    for (const m of row.modalities) d.mods.add(m);
  }

  $('#facts').textContent =
    `${fmtN(CAT.cohorts.HPRC)} HPRC donors · ${fmtN(CAT.cohorts.HGSVC)} HGSVC donors · ` +
    `${CAT.dual_cohort_samples.length} in both`;
  for (const c of COHORTS)
    $(`#head-${CSS[c]}`).textContent = `${fmtN(CAT.cohorts[c])} donors · ${WHERE[c]}`;
  $('#footer-note').textContent =
    `Updated ${CAT.generated_utc.slice(0, 10)} · ${fmtN(CAT.unique_samples)} donors · ` +
    `${fmtN(CAT.totals.files)} files · ${(CAT.totals.bytes / 1e12).toFixed(0)} TB in the HPRC bucket.`;

  renderOverview();
  renderTakeaways();
  renderPicker();
  renderPresets();
  renderShapeCompare();
  renderIntersections();
  renderOtherDonors();
  const pops = [...new Set(CAT.samples.map(s => s.population).filter(Boolean))].sort();
  $('#sample-pop').append(...pops.map(p => el('option', { value: p, textContent: p })));
  wireEvents();
  readHash();
  $('#combine-cohort').value = state.cohort;
  showView(state.view);
  renderDonors();
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

// No cohort chosen: a donor counts if its data, pooled across both cohorts, covers the
// picks. Cohort chosen: everything must come from that cohort's own record.
function matchingDonors(mods, cohort) {
  const out = [];
  for (const d of donors.values()) {
    if (cohort) {
      const row = d.rows.find(r => r.cohort === cohort);
      if (row && mods.every(m => row.modalities.includes(m)))
        out.push({ donor: d, rows: [row], mods: new Set(row.modalities) });
    } else if (mods.every(m => d.mods.has(m))) {
      out.push({ donor: d, rows: d.rows, mods: d.mods });
    }
  }
  return out;
}
const countIn = (hits, cohort) => hits.filter(h => h.donor.cohorts.has(cohort)).length;
const nWith = (mods, cohort = '') => matchingDonors(mods, cohort).length;

function groups(list = CAT.modalities) {
  const out = new Map();
  for (const m of list) {
    const g = INFO[m].group;
    if (!out.has(g)) out.set(g, []);
    out.get(g).push(m);
  }
  return [...out];
}

/* ---------------- URL state ---------------- */

const VIEW_ALIAS = { combine: 'donors', samples: 'donors', coverage: 'overview' };

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  const a = p.get('assays');
  if (a) state.selected = a.split(',').filter(m => CAT.modalities.includes(m));
  const c = p.get('cohort') === 'HGSVC3' ? 'HGSVC' : p.get('cohort');
  if (COHORTS.includes(c)) state.cohort = c;
  const v = VIEW_ALIAS[p.get('view')] || p.get('view');
  if (['overview', 'donors', 'about'].includes(v)) state.view = v;
  else if (a) state.view = 'donors';
}

function writeHash() {
  const p = new URLSearchParams();
  if (state.view !== 'overview') p.set('view', state.view);
  if (state.selected.length) p.set('assays', state.selected.join(','));
  if (state.cohort) p.set('cohort', state.cohort);
  const s = p.toString().replace(/%2C/g, ',');
  try { history.replaceState(null, '', s ? '#' + s : location.pathname + location.search); }
  catch { /* non-fatal */ }
}

function showView(name) {
  state.view = name;
  for (const o of $$('.tabs button')) o.setAttribute('aria-selected', String(o.dataset.view === name));
  for (const v of $$('.view')) v.hidden = v.id !== `view-${name}`;
  writeHash();
}

/* ---------------- overview ---------------- */

function covCell(m, c) {
  const n = CAT.coverage[m][c].n, total = CAT.cohorts[c];
  if (!n) return el('td', { className: 'cov-td' }, [el('span', { className: 'none', textContent: '—',
    title: `No ${c} donor has this.` })]);
  const pct = (n / total) * 100;
  return el('td', { className: 'cov-td' }, [el('div', {
    className: 'cov', title: `${fmtN(n)} of ${fmtN(total)} ${c} donors (${Math.round(pct)}%)`,
  }, [
    el('div', { className: 'bar' }, [el('span', { style: `width:${Math.max(pct, 1.5)}%;background:var(--${CSS[c]})` })]),
    el('span', { className: 'n', textContent: fmtN(n) }),
    el('span', { className: 'pct', textContent: `${pct < 1 ? '<1' : Math.round(pct)}%` }),
  ])]);
}

function renderOverview() {
  const body = [];
  let nMore = 0;
  for (const [g, mods] of groups()) {
    const allMore = mods.every(m => !isMain(m));
    body.push(el('tr', { className: 'group-row' + (allMore ? ' more-row' : '') },
      [el('td', { colSpan: 3, textContent: g })]));
    for (const m of mods) {
      if (!isMain(m)) nMore++;
      const name = el('td', { className: 'name' }, [el('span', { className: 'nm', textContent: label(m) })]);
      if (INFO[m].note) name.append(el('span', { className: 'desc', textContent: INFO[m].note }));
      const tr = el('tr', { className: 'clickable' + (isMain(m) ? '' : ' more-row'), tabIndex: 0 },
        [name, ...COHORTS.map(c => covCell(m, c))]);
      const go = () => { state.selected = [m]; state.q = ''; $('#sample-search').value = ''; showView('donors'); renderDonors(); scrollTo(0, 0); };
      tr.addEventListener('click', go);
      tr.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
      body.push(tr);
    }
  }
  $('#overview-table tbody').replaceChildren(...body);
  const btn = $('#toggle-more');
  const sync = () => {
    $('#overview-table').classList.toggle('show-all', state.showAll);
    btn.textContent = state.showAll ? 'Show fewer' : `Show ${nMore} more data types (rare, older, QC)`;
    btn.setAttribute('aria-expanded', String(state.showAll));
  };
  btn.addEventListener('click', () => { state.showAll = !state.showAll; sync(); });
  sync();
}

function renderTakeaways() {
  const pct = (m, c) => CAT.coverage[m][c].n / CAT.cohorts[c];
  const both = [], onlyH = [], onlyG = [];
  for (const m of MAIN) {
    const h = pct(m, 'HPRC'), g = pct(m, 'HGSVC');
    if (h >= 0.5 && g >= 0.5) both.push(short(m));
    else if (g < 0.15 && h > g) onlyH.push(`${short(m)} (${CAT.coverage[m].HPRC.n} of ${CAT.cohorts.HPRC})`);
    else if (h < 0.15 && g > h) onlyG.push(`${short(m)} (${CAT.coverage[m].HGSVC.n} of ${CAT.cohorts.HGSVC})`);
  }
  const li = (head, items) => items.length
    ? el('li', {}, [el('strong', { textContent: head + ' ' }), txt(items.join(', ') + '.')]) : null;
  $('#takeaways').replaceChildren(...[
    li('Most donors in both cohorts:', both),
    li('HPRC only, or nearly:', onlyH),
    li('HGSVC only, or nearly:', onlyG),
  ].filter(Boolean));
}

/* ---------------- find donors: picker ---------------- */

function renderPicker() {
  $('#mod-picker').replaceChildren(...groups().map(([name, mods]) => {
    const chips = mods.map(m => {
      const b = el('button', { className: 'chip', type: 'button', title: INFO[m].note || label(m) });
      b.dataset.mod = m;
      b.append(el('span', { className: 'nm', textContent: label(m) }), el('span', { className: 'ct' }));
      b.addEventListener('click', () => toggleMod(m));
      return b;
    });
    const g = el('div', { className: 'pick-group' }, [el('h3', { textContent: name }),
      el('div', { className: 'chips' }, chips)]);
    g.dataset.mods = mods.join(',');
    return g;
  }));
  $('#picker-more').addEventListener('click', () => { state.pickerAll = !state.pickerAll; renderDonors(); });
}

function renderPresets() {
  const mk = (text, mods) => {
    const b = el('button', { type: 'button', textContent: text });
    b.addEventListener('click', () => { state.selected = [...mods]; renderDonors(); });
    return b;
  };
  $('#presets').replaceChildren(el('span', { className: 'lbl', textContent: 'Examples:' }),
    ...PRESETS.map(p => mk(p.label, p.mods)), mk('Clear', []));
}

function toggleMod(m) {
  const i = state.selected.indexOf(m);
  if (i === -1) state.selected.push(m); else state.selected.splice(i, 1);
  renderDonors();
}

function syncPicker() {
  const { selected, cohort, pickerAll } = state;
  let hidden = 0;
  for (const g of $$('#mod-picker .pick-group')) {
    let any = false;
    for (const b of $$('.chip', g)) {
      const m = b.dataset.mod, on = selected.includes(m);
      const show = pickerAll || isMain(m) || on;
      b.hidden = !show;
      if (show) any = true; else hidden++;
      b.setAttribute('aria-pressed', String(on));
      const n = on ? null : nWith([...selected, m], cohort);
      b.querySelector('.ct').textContent = on ? '✓' : fmtN(n);
      b.classList.toggle('zero', n === 0);
    }
    g.hidden = !any;
  }
  const btn = $('#picker-more');
  btn.textContent = pickerAll ? 'Fewer data types' : `More data types (${hidden})`;
  btn.hidden = !pickerAll && hidden === 0;
  btn.setAttribute('aria-expanded', String(pickerAll));
}

/* ---------------- find donors: result + table ---------------- */

function currentHits() {
  const q = state.q.trim().toLowerCase();
  return matchingDonors(state.selected, state.cohort).filter(h => {
    const r0 = h.rows[0];
    return (!state.pop || h.rows.some(r => r.population === state.pop)) &&
      (!q || h.donor.id.toLowerCase().includes(q) || h.rows.some(r => (r.aka || '').toLowerCase().includes(q)) ||
       (r0.population || '').toLowerCase().includes(q));
  });
}

function renderDonors() {
  syncPicker();
  const { selected, cohort } = state;
  const all = matchingDonors(selected, cohort);
  const nH = countIn(all, 'HPRC'), nG = countIn(all, 'HGSVC');

  $('#result-n').textContent = fmtN(all.length);
  const of = $('#result-of');
  const sw = c => el('span', { className: 'sw', style: `background:var(--${CSS[c]})` });
  const lead = selected.length
    ? `donor${all.length === 1 ? ' has' : 's have'} ${selected.length === 1 ? label(selected[0]) : `all ${selected.length} picks`}`
    : 'donors in total';
  const parts = [txt(lead + (cohort ? ` in ${cohort}` : ''))];
  if (!cohort) {
    parts.push(el('span', { className: 'split' }, [sw('HPRC'), txt(`${fmtN(nH)} HPRC`), sw('HGSVC'), txt(`${fmtN(nG)} HGSVC`)]));
    const both = nH + nG - all.length;
    if (both > 0) parts.push(el('span', { className: 'muted', textContent: `(${both} in both)` }));
  }
  of.replaceChildren(...parts);

  renderCascade();
  renderDonorTable(currentHits());
  writeHash();
}

function renderCascade() {
  const wrap = $('#cascade-card');
  wrap.hidden = state.selected.length < 2;
  if (wrap.hidden) return;
  const steps = [{ label: 'All donors', mods: [] },
    ...state.selected.map((m, i) => ({ label: label(m), mods: state.selected.slice(0, i + 1) }))]
    .map(s => ({ ...s, n: nWith(s.mods, state.cohort) }));
  const max = steps[0].n || 1;
  const fill = state.cohort ? `var(--${CSS[state.cohort]})` : 'var(--text-muted)';
  $('#cascade').replaceChildren(...steps.map((s, i) => {
    const lab = el('div', { className: 'step-label' });
    if (i === 0) lab.textContent = s.label; else lab.append(txt('+ '), el('b', { textContent: s.label }));
    const track = el('div', { className: 'track', title: `${fmtN(s.n)} donors` },
      [el('span', { style: `width:${(s.n / max) * 100}%;background:${fill}` })]);
    return el('li', {}, [lab, track, el('div', { className: 'n', textContent: fmtN(s.n) })]);
  }));
}

function renderDonorTable(hits) {
  const req = new Set(state.selected);
  // header: a group row above the short column names
  const top = [el('th', { colSpan: 3 })];
  for (const [g, mods] of groups(MAIN)) top.push(el('th', { colSpan: mods.length, className: 'grp', textContent: g }));
  top.push(el('th', { colSpan: 2 }));
  const sortTh = (text, key, cls = '') => {
    const th = el('th', { className: 'sortable ' + cls, textContent: text });
    if (state.sort.key === key) th.append(txt(state.sort.dir > 0 ? ' ↑' : ' ↓'));
    th.addEventListener('click', () => {
      state.sort = { key, dir: state.sort.key === key ? -state.sort.dir : 1 };
      renderDonorTable(currentHits());
    });
    return th;
  };
  const second = [sortTh('Donor', 'id'), sortTh('Cohort', 'cohort'), sortTh('Population', 'pop'),
    ...MAIN.map(m => el('th', { className: 'dotcol' + (req.has(m) ? ' req' : ''), textContent: short(m), title: label(m) })),
    sortTh('Other', 'other', 'num'), sortTh('Files', 'files', 'num')];
  second[MAIN.length + 3].title = 'Other data for this donor: rarer data types and data from other projects';
  $('#sample-table thead').replaceChildren(el('tr', { className: 'grp-row' }, top), el('tr', {}, second));

  const info = hits.map(h => {
    const others = [...h.mods].filter(m => !MAIN.includes(m));
    return { h, id: h.donor.id, cohort: [...h.donor.cohorts].join(' '), pop: h.rows[0].population || '',
      other: others.length, others, files: h.rows.reduce((a, r) => a + (r.n_files || 0), 0) };
  });
  const { key, dir } = state.sort;
  info.sort((a, b) => (typeof a[key] === 'number' ? a[key] - b[key] : String(a[key]).localeCompare(String(b[key]))) * dir
    || a.id.localeCompare(b.id));

  $('#sample-empty').hidden = info.length > 0;
  $('#sample-table').hidden = info.length === 0;
  $('#sample-table tbody').replaceChildren(...info.map(x => {
    const { h } = x;
    const aka = h.rows.map(r => r.aka).find(Boolean);
    const idCell = el('td', {}, [el('span', { className: 'sample-id', textContent: x.id })]);
    if (aka) idCell.append(el('span', { className: 'aka', textContent: ` ${aka}` }));
    const tr = el('tr', { className: 'clickable', tabIndex: 0 }, [
      idCell,
      el('td', { className: 'nowrap' }, h.rows.map(r => cohortBadge(r.cohort))),
      el('td', { textContent: x.pop && x.pop !== 'N/A' ? x.pop : '—' }),
      ...MAIN.map(m => {
        const on = h.mods.has(m);
        return el('td', { className: 'dotcol' + (req.has(m) ? ' req' : ''), title: `${label(m)}: ${on ? 'yes' : 'no'}` },
          [el('span', { className: 'dot' + (on ? ' on' : ''), role: 'img', ariaLabel: on ? 'yes' : 'no' })]);
      }),
      el('td', { className: 'num', textContent: x.other ? `+${x.other}` : '', title: x.others.map(label).join('\n') }),
      el('td', { className: 'num', textContent: x.files ? fmtN(x.files) : '—' }),
    ]);
    tr.addEventListener('click', () => openDrawer(x.id));
    tr.addEventListener('keydown', e => { if (e.key === 'Enter') openDrawer(x.id); });
    return tr;
  }));
}

function cohortBadge(c) {
  return el('span', { className: `badge cohort-${CSS[c] || 'hgsvc'}`, textContent: c });
}

/* ---------------- about ---------------- */

function renderShapeCompare() {
  const core5 = ['HiFi', 'ONT', 'Illumina_WGS', 'HiC', 'Kinnex_IsoSeq'];
  const eight = ['Illumina_WGS', 'HiFi', 'ONT', 'StrandSeq', 'HiC', 'Bionano', 'RNAseq', 'Kinnex_IsoSeq'];
  const hS = CAT.cohorts.HPRC, gS = CAT.cohorts.HGSVC;
  const h5 = nWith(core5, 'HPRC'), hM = nWith([...core5, 'HiFi_5mC'], 'HPRC'), hF = nWith([...core5, 'FiberSeq'], 'HPRC');
  const g8 = nWith(eight, 'HGSVC');
  $('#shape-compare').replaceChildren(
    el('div', { className: 'callout info' }, [
      el('strong', { textContent: 'For many donors: HPRC. ' }),
      txt(`${h5} of ${hS} donors have long reads, short reads, Hi-C and full-length RNA together; ` +
        `${hM} of those also have methylation, and ${hF} also have Fiber-seq.`)]),
    el('div', { className: 'callout' }, [
      el('strong', { textContent: 'For many data types on each donor: HGSVC. ' }),
      txt(`${g8} of ${gS} donors have eight data types at once, including short-read RNA-seq, ` +
        `Strand-seq and Bionano, which HPRC mostly lacks.`)]),
    el('div', { className: 'callout' }, [
      el('strong', { textContent: 'They are different people. ' }),
      txt(`Only ${CAT.dual_cohort_samples.length} donors are in both (${CAT.dual_cohort_samples.join(', ')}).`)]));
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
    el('td', { textContent: d.modalities.map(short).join(', ') }),
    el('td', { textContent: d.context.join(', ') || '—' }),
    el('td', { className: 'num', textContent: fmtN(d.files) }),
    el('td', { className: 'num', textContent: fmtBytes(d.bytes) || '—' })])));
}

/* ---------------- donor panel ---------------- */

const shardCache = new Map();
function loadShard(id, cohort, dual) {
  const name = dual ? `${id}.${cohort}.json` : `${id}.json`;
  if (!shardCache.has(name))
    shardCache.set(name, fetch(DATA + 'files/' + name).then(r => (r.ok ? r.json() : null)).catch(() => null));
  return shardCache.get(name);
}

// Plain-language line for a data type that has no files of its own to list.
function noFileNote(m, ev) {
  if (ev === 'sheet_only') return 'Recorded by HGSVC, but not on the public archive';
  if (ev === 'nygc_30x') return 'Use the 1000 Genomes 30x genome, listed below';
  if (m === 'HiFi_5mC') return 'Already inside the PacBio HiFi read files';
  if (m === 'ONT_5mC') return 'Already inside the Nanopore read files';
  if (m === 'FiberSeq') return 'Tags found inside the PacBio HiFi read files';
  if (m === 'WholeBlood') return 'See the files marked “blood”';
  if (m === 'iPSC') return 'See the files marked “iPSC”';
  return 'No separate files';
}

async function openDrawer(id) {
  const d = donors.get(id);
  if (!d) return;
  const dual = d.cohorts.size > 1, first = d.rows[0];
  const aka = d.rows.map(r => r.aka).find(Boolean);

  $('#drawer-title').textContent = id;
  $('#drawer-meta').replaceChildren(...d.rows.map(r => cohortBadge(r.cohort)),
    txt(' ' + [first.population === 'N/A' ? '' : first.population, first.sex,
      aka ? `also called ${aka}` : ''].filter(Boolean).join(' · ')));
  $('#drawer').classList.add('open');
  $('#drawer').setAttribute('aria-hidden', 'false');
  $('#drawer-scrim').classList.add('open');
  const body = $('#drawer-body');
  body.replaceChildren(el('p', { className: 'hint', textContent: 'Loading…' }));

  const parts = [];
  let external = null;
  for (const row of d.rows) {
    const shard = await loadShard(id, row.cohort, dual);
    if (shard?.external && Object.keys(shard.external).length) external = shard.external;
    parts.push(el('h3', { textContent: `Available from ${row.cohort} · ${WHERE[row.cohort]}` }));
    parts.push(...renderRecord(row, shard));
  }
  if (external) {
    parts.push(el('h3', { textContent: 'From other projects, same cell line' }));
    parts.push(...renderExternal(external));
  }
  parts.push(renderDetails(d));
  body.replaceChildren(...parts);
  body.scrollTop = 0;
}

function renderRecord(row, shard) {
  const isHprc = row.cohort === 'HPRC';
  const files = (shard?.files || []).filter(f => !isHprc || f[6] === 'data');
  const byMod = new Map();
  for (const f of files) { if (!byMod.has(f[0])) byMod.set(f[0], []); byMod.get(f[0]).push(f); }
  const out = [];

  for (const m of CAT.modalities.filter(m => !isExt(m) && row.modalities.includes(m))) {
    const fl = byMod.get(m), ev = row.evidence?.[m];
    if (!fl || ev === 'sheet_only') {
      out.push(el('div', { className: 'avail-row' + (ev === 'sheet_only' ? ' missing' : '') }, [
        el('span', { className: 'nm', textContent: label(m) }),
        el('span', { className: 'note', textContent: noFileNote(m, ev) })]));
      continue;
    }
    const det = el('details', { className: 'file-group' });
    const bytes = isHprc ? fl.reduce((a, f) => a + f[2], 0) : 0;
    det.append(el('summary', {}, [txt(label(m)),
      el('span', { className: 'fg-n', textContent: [plural(fl.length, 'file'), fmtBytes(bytes)].filter(Boolean).join(' · ') })]));
    const ul = el('ul', { className: 'file-list' });
    for (const f of fl.slice(0, 200)) {
      const url = shard.prefix + f[1];
      const li = el('li', {}, [el('a', { href: url, textContent: f[1].split('/').pop(), title: url, rel: 'noopener' })]);
      if (isHprc) {
        if (f[7]) li.append(el('span', { className: 'badge', textContent: f[7] }));
        li.append(el('span', { className: 'sz', textContent: fmtBytes(f[2]) }));
      } else if (f[3]) li.append(el('span', { className: 'badge', textContent: f[3] }));
      ul.append(li);
    }
    if (fl.length > 200) ul.append(el('li', {}, [el('span', { className: 'hint',
      textContent: `…and ${fl.length - 200} more. “Download URL list” has all of them.` })]));
    det.append(ul);
    out.push(det);
  }

  if (shard?.files.length) {
    const urls = shard.files.map(f => shard.prefix + f[1]);
    out.push(el('div', { className: 'controls tight' }, [
      btn('Copy all URLs', () => navigator.clipboard.writeText(urls.join('\n'))),
      btn('Download URL list', () => downloadText(`${row.sample_id}_${row.cohort}_urls.txt`, urls.join('\n')))]));
  }
  return out;
}

function renderExternal(ext) {
  const order = CAT.modalities.filter(m => ext[m]);
  return order.map(m => {
    const items = ext[m], enc = m.startsWith('ENCODE');
    const det = el('details', { className: 'file-group' });
    det.append(el('summary', {}, [txt(label(m)),
      el('span', { className: 'fg-n', textContent: plural(items.length, enc ? 'experiment' : 'file') })]));
    const ul = el('ul', { className: 'file-list' });
    for (const it of items.slice(0, 200)) {
      if (enc) {
        const [acc, assay, target] = it.split('|');
        ul.append(el('li', {}, [
          el('a', { href: `https://www.encodeproject.org/experiments/${acc}/`, textContent: acc, rel: 'noopener', target: '_blank' }),
          el('span', { className: 'badge', textContent: assay + (target ? ` · ${target}` : '') })]));
      } else {
        ul.append(el('li', {}, [el('a', { href: it, textContent: it.split('/').pop(), title: it, rel: 'noopener' })]));
      }
    }
    det.append(ul);
    return det;
  });
}

function renderDetails(d) {
  const det = el('details', { className: 'more-details' }, [el('summary', { textContent: 'More about this donor' })]);
  for (const row of d.rows) {
    const kv = el('dl', { className: 'kv' });
    const add = (k, v) => { if (v && v !== '-') kv.append(el('dt', { textContent: k }), el('dd', { textContent: v })); };
    add('Cohort', row.cohort);
    add('Population', [row.population, row.superpop].filter(Boolean).join(' · '));
    add('Sex', row.sex);
    if (row.cohort === 'HPRC') {
      add('HPRC release', row.release === 'untagged' ? 'not in a release table yet' : row.release);
      add('Total size', fmtBytes(row.bytes));
      add('In HPRC index', row.n_files ? `${fmtN(row.n_indexed)} of ${fmtN(row.n_files)} files` : '');
    } else {
      add('Files held in', (row.phases || []).join(', '));
      add('Family', row.family_id);
      add('Relation', row.relation);
      add('Strand-seq libraries', row.strandseq_libs ? String(row.strandseq_libs) : '');
    }
    det.append(kv);
  }
  return det;
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
  $('#combine-cohort').addEventListener('change', e => { state.cohort = e.target.value; renderDonors(); });
  $('#sample-pop').addEventListener('change', e => { state.pop = e.target.value; renderDonorTable(currentHits()); });
  $('#sample-search').addEventListener('input', e => { state.q = e.target.value; renderDonorTable(currentHits()); });
  $('#sample-export').addEventListener('click', exportCsv);
  $('#combine-link').addEventListener('click', () => {
    navigator.clipboard.writeText(location.href);
    const b = $('#combine-link'); b.textContent = 'Link copied ✓';
    setTimeout(() => (b.textContent = 'Copy link'), 1500);
  });
  $('#drawer-close').addEventListener('click', closeDrawer);
  $('#drawer-scrim').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });
  // a link pasted into an open tab, or back/forward
  window.addEventListener('hashchange', () => {
    state.selected = []; state.cohort = ''; state.view = 'overview';
    readHash();
    $('#combine-cohort').value = state.cohort;
    showView(state.view);
    renderDonors();
  });
}

function exportCsv() {
  const cols = ['donor', 'also_called', 'cohorts', 'population', 'sex', 'files', ...MAIN, 'other_data'];
  const esc = v => { const s = String(v ?? ''); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const lines = currentHits().map(h => [
    h.donor.id, h.rows.map(r => r.aka).find(Boolean) || '', h.rows.map(r => r.cohort).join(';'),
    h.rows[0].population || '', h.rows[0].sex || '', h.rows.reduce((a, r) => a + (r.n_files || 0), 0),
    ...MAIN.map(m => (h.mods.has(m) ? 'yes' : 'no')),
    [...h.mods].filter(m => !MAIN.includes(m)).join(';')].map(esc).join(','));
  downloadText('donors.csv', [cols.join(','), ...lines].join('\n'));
}
