'use strict';
const $ = s => document.querySelector(s);
const coverUrl = (release, group, artist, title, size) => '/api/cover?' + new URLSearchParams({ release: release || '', group: group || '', artist, title, size });
const el = (tag, a, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(a || {})) {
    if (v === false || v == null) continue;
    if (k === 'class') e.className = v; else if (k === 'text') e.textContent = v; else if (k === 'style') e.style.cssText = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k.includes('-') || k === 'type') e.setAttribute(k, v === true ? '' : v);
    else { try { e[k] = v; } catch (_) { e.setAttribute(k, v); } }
  }
  e.append(...kids.flat(Infinity).filter(x => x != null && x !== false));
  return e;
};
const fill = (node, ...kids) => { node.replaceChildren(...kids.flat(Infinity).filter(x => x != null && x !== false)); return node; };
const avatar = name => { const i = el('img', { class: 'av', alt: '', loading: 'lazy', src: '/api/artist-img?name=' + encodeURIComponent(name) }); i.onerror = () => i.remove(); return i; };
const pic = (name, color = '') => el('span', { class: ['pi', name, color].filter(Boolean).join(' ') });
const store = {
  get(k, d) { try { const v = localStorage.getItem('albumdl.' + k); return v == null ? d : JSON.parse(v); } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem('albumdl.' + k, JSON.stringify(v)); } catch (_) {} },
};
const api = async (path, body) => {
  const r = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({ error: 'bad response' }));
  if (!r.ok || data.error) throw new Error(data.error || ('HTTP ' + r.status));
  return data;
};
const pad = n => String(n).padStart(2, '0');
const dur = s => { s = Math.round(s || 0); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return h ? `${h}h ${m}m` : m ? `${m}m` : `${s}s`; };
const bytes = n => { n = n || 0; const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return (i > 1 ? n.toFixed(1) : Math.round(n)) + ' ' + u[i]; };
const clock = t => { const d = new Date(t * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
function toast(text, kind = 'info', action) {
  const t = el('div', { class: 'toast' + (kind === 'error' ? ' bad' : '') }, pic(kind === 'error' ? 'x' : 'check', kind === 'error' ? 'r' : 'c'), el('span', { text }),
    action ? el('button', { class: 'btn sm', type: 'button', text: action.label, onclick: () => { action.fn(); t.remove(); } }) : null);
  $('#toasts').append(t); setTimeout(() => t.remove(), kind === 'error' ? 8000 : 4000);
}
function cover(url) {
  const box = el('span', { class: 'cover' }), img = el('img', { alt: '', loading: 'lazy', decoding: 'async', src: url });
  img.onload = () => img.classList.add('ok'); box.append(img);
  return box;
}

const VIEWS = { search: 'search', library: 'library', queue: 'queue', log: 'log', discover: 'search', console: 'log' };
let view = 'search';
function show(name) {
  name = VIEWS[name] || 'search'; view = name; store.set('view', name);
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'v-' + name));
  document.querySelectorAll('nav button').forEach(b => b.setAttribute('aria-selected', String(b.dataset.view === name)));
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  drawActions();
  if (name === 'log') { unseen = 0; badges(); renderLog(); }
  if (name === 'queue') tick();
  if (name === 'library') loadLibrary();
  scrollTo(0, 0);
}
document.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => show(b.dataset.view)));
addEventListener('hashchange', () => show(location.hash.slice(1)));

let results = [], title = '', filter = 'All', artistCtx = null, fresh = false;
const selected = new Map();
const typeOf = r => ['Album', 'EP', 'Single'].includes(r.type) ? (r.secondary.length ? r.secondary[0] : r.type) : (r.type || 'Other');
const pick = r => ({ id: r.id, title: r.title, artist: r.artist });
const skeleton = () => el('div', { class: 'grid' }, Array.from({ length: 8 }, () => el('div', { class: 'card sk' }, el('span', { class: 'cover' }), el('span', { class: 'meta' }, el('b', { text: '\u00a0' }), el('small', { text: '\u00a0' })))));

$('#searchForm').addEventListener('submit', async ev => {
  ev.preventDefault();
  const q = $('#q').value.trim(); if (!q) return;
  $('#q').blur(); $('#searchBtn').disabled = true;
  selected.clear(); artistCtx = null; drawActions(); fill($('#artistBar')); fill($('#similar'));
  fill($('#results'), skeleton());
  try { await search(q); } catch (e) { fill($('#results'), el('div', { class: 'msg err', text: e.message })); }
  $('#searchBtn').disabled = false;
});
async function search(q) {
  const link = q.match(/musicbrainz\.org\/(release-group|release)\/([0-9a-f-]{36})/i);
  if (link) return direct(link[1].toLowerCase(), link[2].toLowerCase());
  const split = q.split(/\s+[-–—]\s+/);
  if (split.length > 1) return rows(await api('/api/search?artist=' + encodeURIComponent(split[0]) + '&album=' + encodeURIComponent(split.slice(1).join(' - '))), 'Results');
  const { artists } = await api('/api/artists?q=' + encodeURIComponent(q));
  if (artists.length && artists[0].score >= 85) return showArtist(artists[0], artists);
  const d = await api('/api/search?album=' + encodeURIComponent(q));
  if (d.results.length || !artists.length) return rows(d, 'Results');
  showArtist(artists[0], artists);
}
async function direct(kind, id) {
  let rid = id;
  if (kind === 'release-group') rid = (await api('/api/group/' + id)).editions[0].id;
  const a = await api('/api/release/' + rid);
  rows({ results: [{ id: a.group_id, title: a.title, artist: a.artist, type: a.type || 'Album', secondary: [], date: a.original_date || a.date || '', owned: a.owned }] }, 'Results');
}
function rows(d, name) {
  results = d.results; title = name; filter = 'All'; fresh = true;
  if (!results.length) return fill($('#results'), el('div', { class: 'msg', text: 'No albums found. Check the spelling, or search the artist alone.' }));
  if (results.length === 1) selected.set(results[0].id, pick(results[0]));
  renderResults();
}
async function openSimilar(x) {
  $('#q').value = x.name; selected.clear(); drawActions(); fill($('#artistBar')); fill($('#results'), skeleton());
  try {
    if (x.id) return await showArtist({ id: x.id, name: x.name }, []);
    const { artists } = await api('/api/artists?q=' + encodeURIComponent(x.name));
    if (!artists.length) throw new Error('No artist found for ' + x.name);
    await showArtist(artists[0], artists);
  } catch (e) { fill($('#results'), el('div', { class: 'msg err', text: e.message })); }
}
async function showArtist(a, all) {
  artistCtx = a; selected.clear(); drawActions(); results = [];
  const alts = all.filter(x => x.id !== a.id).slice(0, 4);
  fill($('#artistBar'), alts.length ? el('div', { class: 'alts' }, el('span', { text: 'Not them?' }), alts.map(x =>
    el('button', { class: 'chip', type: 'button', text: x.name + (x.disambiguation ? ' · ' + x.disambiguation : ''), onclick: () => showArtist(x, all) }))) : null);
  fill($('#results'), skeleton()); fill($('#similar'));
  api(`/api/similar/${a.id}?name=${encodeURIComponent(a.name)}`).then(({ artists }) => {
    if (artistCtx !== a || !artists.length) return;
    fill($('#similar'), el('div', { class: 'alts' }, el('span', { text: 'Similar' }), artists.map(x => el('button', { class: 'chip sim' + (x.owned ? ' own' : ''), type: 'button', title: x.owned ? 'In your library' : '', onclick: () => openSimilar(x) }, avatar(x.name), x.name, x.owned ? pic('check', 'c') : null))));
  }).catch(() => {});
  try {
    const d = await api('/api/artist/' + a.id);
    rows({ results: d.albums }, a.name);
  } catch (e) { fill($('#results'), el('div', { class: 'msg err', text: e.message })); }
}
function renderResults() {
  const kinds = ['All', ...new Set(results.map(typeOf))], shown = results.filter(r => filter === 'All' || typeOf(r) === filter);
  const allOn = shown.length && shown.every(r => selected.has(r.id));
  fill($('#results'),
    el('div', { class: 'bar' }, artistCtx && title === artistCtx.name ? avatar(title) : null, el('h2', { text: title }), el('span', { class: 'sub', text: shown.length + (shown.length === 1 ? ' release' : ' releases') }),
      el('button', { class: 'btn sm ghost', style: 'margin-left:auto', type: 'button', text: allOn ? 'Deselect all' : 'Select all', onclick: () => {
        shown.forEach(r => allOn ? selected.delete(r.id) : (r.owned && !r.owned.incomplete) || selected.set(r.id, pick(r))); renderResults(); drawActions(); } })),
    kinds.length > 2 ? el('div', { class: 'alts' }, kinds.map(k => el('button', { class: 'chip' + (k === filter ? ' on' : ''), type: 'button', text: k, onclick: () => { filter = k; renderResults(); } }))) : null,
    el('div', { class: 'grid' + (fresh ? ' fresh' : '') }, shown.map((r, i) => {
      const card = el('button', { class: 'card' + (selected.has(r.id) ? ' on' : ''), style: `--i:${Math.min(i, 16)}`, type: 'button', 'aria-pressed': selected.has(r.id), onclick: () => {
        const on = !selected.has(r.id); on ? selected.set(r.id, pick(r)) : selected.delete(r.id);
        card.classList.toggle('on', on); card.setAttribute('aria-pressed', on); drawActions(); } },
        cover(coverUrl('', r.id, r.artist, r.title, 250)), el('span', { class: 'tick' }, pic('check', 'c')),
        r.owned ? el('span', { class: 'tag' + (r.owned.incomplete ? ' part' : ''), text: r.owned.incomplete ? 'Incomplete' : 'In folder' }) : null,
        el('span', { class: 'meta' }, el('b', { text: r.title, title: r.title }),
          el('small', { text: [artistCtx ? '' : r.artist, r.date.slice(0, 4), typeOf(r)].filter(Boolean).join(' · ') })));
      return card;
    })));
  fresh = false; drawActions();
}
const libSel = new Map(), lkey = a => a.artist + '\u0000' + a.title;
function drawActions() {
  const lib_ = view === 'library', n = lib_ ? libSel.size : selected.size;
  $('#actions').hidden = !n || (view !== 'search' && !lib_);
  $('#dlBtn').hidden = lib_; $('#retryBtn').hidden = !(lib_ && [...libSel.values()].some(a => a.incomplete));
  if (lib_) {
    $('#actionText').textContent = `${n === 1 ? '1 album' : n + ' albums'} · ${bytes([...libSel.values()].reduce((t, a) => t + a.size, 0))}`;
    $('#delBtn').hidden = false; return;
  }
  const have = [...selected.keys()].filter(id => (results.find(r => r.id === id) || {}).owned).length, own = n && have === n;
  $('#actionText').textContent = (n === 1 ? '1 album' : n + ' albums') + (have && !own ? ` · ${have} in folder` : '');
  $('#dlLabel').textContent = own ? 'Download again' : 'Download';
  $('#delBtn').hidden = !own; if (!own) resetDelete();
}
$('#clearSel').addEventListener('click', () => { if (view === 'library') { libSel.clear(); renderLibrary(); } else { selected.clear(); if (results.length) renderResults(); } drawActions(); });
let delTimer = null;
function resetDelete() { clearTimeout(delTimer); delTimer = null; $('#delBtn').textContent = 'Delete'; $('#delBtn').classList.remove('armed'); }
$('#delBtn').addEventListener('click', async () => {
  const btn = $('#delBtn');
  const lib_ = view === 'library';
  if (!delTimer) { btn.textContent = `Delete ${lib_ ? libSel.size : selected.size} from disk?`; btn.classList.add('armed'); delTimer = setTimeout(resetDelete, 4000); return; }
  resetDelete(); btn.disabled = true;
  const groups = lib_ ? [...libSel.values()] : [...selected.values()];
  try {
    const r = await api('/api/library/delete', { albums: groups });
    groups.forEach(g => { const row = results.find(x => x.id === g.id); if (row && !r.skipped) row.owned = null; });
    toast(r.skipped ? `Deleted ${r.deleted}. ${r.skipped} still downloading.` : `Deleted ${r.deleted} ${r.deleted === 1 ? 'album' : 'albums'}`, 'ok');
    if (lib_) { libSel.clear(); await loadLibrary(); } else { selected.clear(); renderResults(); }
  } catch (e) { toast(e.message, 'error'); }
  btn.disabled = false;
});
$('#retryBtn').addEventListener('click', async () => {
  const albums = [...libSel.values()].filter(a => a.incomplete);
  try {
    const r = await api('/api/library/retry-incomplete', { albums });
    toast(r.queued ? `Queued ${r.queued} ${r.queued === 1 ? 'album' : 'albums'}` : 'Nothing to queue', 'ok', { label: 'Queue', fn: () => show('queue') });
    libSel.clear(); renderLibrary(); drawActions(); tick();
  } catch (e) { toast(e.message, 'error'); }
});
$('#dlBtn').addEventListener('click', async () => {
  const groups = [...selected.values()], btn = $('#dlBtn'); btn.disabled = true;
  try {
    const r = await api('/api/discography', { name: artistCtx ? artistCtx.name : groups[0].artist, groups, skip_owned: false });
    const added = groups.length - r.duplicates;
    toast(!added ? 'Already in the queue' : (groups.length === 1 ? `Queued ${groups[0].title}` : `Queued ${added} albums`) + (r.duplicates && added ? `, ${r.duplicates} already queued` : ''), 'ok', { label: 'Queue', fn: () => show('queue') });
    selected.clear(); drawActions(); renderResults(); tick();
  } catch (e) { toast(e.message, 'error'); }
  btn.disabled = false;
});

const ago = t => { const d = (Date.now() / 1000 - t) / 86400; return d < 1 ? 'today' : d < 2 ? 'yesterday' : d < 30 ? Math.floor(d) + 'd ago' : d < 365 ? Math.floor(d / 30) + 'mo ago' : Math.floor(d / 365) + 'y ago'; };
let lib = [], libMode = 'All';
async function loadLibrary() {
  if (!lib.length) fill($('#libList'), el('div', { class: 'grid' }, Array.from({ length: 6 }, () => el('div', { class: 'lrow sk' }, el('span', { class: 'cover' })))));
  try { lib = (await api('/api/library')).albums; } catch (e) { return fill($('#libList'), el('div', { class: 'msg err', text: e.message })); }
  const keys = new Set(lib.map(lkey)); [...libSel.keys()].forEach(k => keys.has(k) || libSel.delete(k));
  renderLibrary(); drawActions();
}
function renderLibrary() {
  const q = $('#libFilter').value.trim().toLowerCase(), inc = lib.filter(a => a.incomplete).length, bare = lib.filter(a => !a.cover).length;
  const modes = [['All', lib.length], ['Recent', 0], ['Incomplete', inc], ['No cover', bare]];
  fill($('#libChips'), modes.map(([m, c]) => el('button', { class: 'chip' + (m === libMode ? ' on' : ''), type: 'button', text: c ? `${m} · ${c}` : m, onclick: () => { libMode = m; renderLibrary(); } })));
  let rows = lib.filter(a => (!q || (a.artist + ' ' + a.title).toLowerCase().includes(q)) && ({ All: 1, Recent: 1, Incomplete: a.incomplete, 'No cover': !a.cover }[libMode]));
  if (libMode === 'Recent') rows = rows.slice().sort((a, b) => b.mtime - a.mtime).slice(0, 40);
  $('#libSum').textContent = lib.length ? `${rows.length} albums · ${rows.reduce((t, a) => t + a.tracks, 0)} tracks · ${bytes(rows.reduce((t, a) => t + a.size, 0))}` : '';
  const row = a => {
    const r = el('button', { class: 'lrow' + (libSel.has(lkey(a)) ? ' on' : ''), type: 'button', 'aria-pressed': libSel.has(lkey(a)), onclick: () => {
      const on = !libSel.has(lkey(a)); on ? libSel.set(lkey(a), a) : libSel.delete(lkey(a)); r.classList.toggle('on', on); r.setAttribute('aria-pressed', on); drawActions(); } },
      a.cover ? cover('/api/library/art?' + new URLSearchParams({ artist: a.artist, album: a.title })) : el('span', { class: 'cover' }),
      el('span', { class: 'jtext' }, el('b', { text: a.title, title: a.title }),
        el('small', { text: [libMode === 'All' ? '' : a.artist, a.tracks + (a.tracks === 1 ? ' track' : ' tracks'), bytes(a.size), a.mtime ? ago(a.mtime) : ''].filter(Boolean).join(' · ') }),
        a.incomplete ? el('span', { class: 'st part', text: a.missing.length ? `Missing ${a.missing.length}: ${a.missing.join(', ')}` : 'Incomplete' }) : !a.cover ? el('span', { class: 'st wait', text: 'No cover' }) : null),
      el('span', { class: 'tick' }, pic('check', 'c')));
    return r;
  };
  if (!rows.length) return fill($('#libList'), el('div', { class: 'empty', text: lib.length ? 'No albums match.' : 'No albums in the music folder yet.' }));
  const out = [];
  if (libMode === 'All') { let last = null; rows.forEach(a => { if (a.artist !== last) { last = a.artist; out.push(el('div', { class: 'sec', text: a.artist })); } out.push(row(a)); }); }
  else rows.forEach(a => out.push(row(a)));
  fill($('#libList'), out);
}
$('#libFilter').addEventListener('input', renderLibrary);
const nodes = new Map(), openSet = new Map();
let busy = false, order = '';
const isOpen = j => openSet.has(j.id) ? openSet.get(j.id) : j.status === 'running';
const kindOf = j => ({ running: 'run', queued: 'wait', done: 'done', incomplete: 'part', failed: 'failed', cancelled: 'cancelled' }[j.status] || 'wait');
function statusText(j) {
  const eta = j.status === 'running' && j.finished > 0 ? (j.total - j.finished) * (j.elapsed / j.finished) : null;
  switch (j.status) {
    case 'running': return `Downloading · ${j.finished}/${j.total}` + (eta != null ? ` · ${dur(eta)} left` : '');
    case 'queued': return 'Waiting';
    case 'done': return `Done · ${j.total} tracks · ${bytes(j.bytes)}` + (j.src_kbps ? ` · source ~${j.src_kbps}k` : '');
    case 'incomplete': return `${j.failed} of ${j.total} missing`;
    case 'cancelled': return 'Cancelled';
    default: return 'Failed';
  }
}
function jobNode(j) {
  const n = { j };
  n.name = el('b'); n.by = el('small'); n.st = el('span', { class: 'st' });
  n.act = el('button', { class: 'btn sm', type: 'button' });
  n.meterFill = el('i'); n.tracks = el('div', { class: 'tracks' }); n.err = el('div', { class: 'jerr' });
  n.root = el('article', { class: 'job' },
    el('div', { class: 'jmain' },
      el('button', { class: 'jtog', type: 'button', onclick: () => toggle(n) }, cover(coverUrl(j.mbid, j.group_id, j.artist, j.title, 150)), el('span', { class: 'jtext' }, n.name, n.by, n.st)),
      el('div', { class: 'jside' }, n.act, el('button', { class: 'jtog chevbtn', type: 'button', 'aria-label': 'Show tracks', onclick: () => toggle(n) }, el('i', { class: 'chev' })))),
    el('div', { class: 'meter' }, n.meterFill), n.err, el('div', { class: 'twrap' }, el('div', { class: 'tin' }, n.tracks)));
  return n;
}
function toggle(n) {
  const open = !isOpen(n.j); openSet.set(n.j.id, open); paint(n);
  if (open) api('/api/jobs/' + n.j.id).then(d => paintTracks(n, d)).catch(() => {});
}
function paint(n) {
  const j = n.j, k = kindOf(j), open = isOpen(j), active = j.status === 'running' || j.status === 'queued';
  n.root.className = `job ${k}${open ? ' open' : ''}`;
  n.name.textContent = j.title; n.name.title = j.title; n.by.textContent = [j.artist, j.year].filter(Boolean).join(' · ');
  n.st.className = 'st ' + k; n.st.textContent = statusText(j);
  n.meterFill.style.setProperty('--v', (j.total ? Math.round(100 * j.finished / j.total) : 0) + '%');
  n.act.hidden = j.status === 'done';
  n.act.textContent = active ? 'Cancel' : 'Retry'; n.act.className = 'btn sm ' + (active ? 'ghost' : 'hot');
  n.act.onclick = async () => {
    try { await api(`/api/jobs/${j.id}/${active ? 'cancel' : 'retry'}`, {}); } catch (e) { toast(e.message, 'error'); } tick();
  };
  n.err.hidden = !j.error; n.err.textContent = j.error || '';
}
function paintTracks(n, d) {
  const sig = JSON.stringify(d.tracks.map(t => [t.n, t.state, t.text, t.out_bytes, t.src_bytes]));
  if (n.sig === sig) return; n.sig = sig;
  fill(n.tracks, d.tracks.map(t => {
    const note = t.state === 'done' ? [t.acodec ? `${t.acodec}${t.src_kbps ? ' ' + t.src_kbps + 'k' : ''} → mp3` : '', /unofficial/.test(t.why || '') ? 'unofficial upload' : ''].filter(Boolean).join(' · ') : t.text;
    return el('div', { class: 'tr ' + t.state }, el('span', { class: 'ico ' + t.state }),
      el('div', { style: 'min-width:0' }, el('div', { class: 'ti', text: t.title, title: t.title }), note ? el('small', { text: note }) : null),
      el('span', { class: 'sz', text: t.state === 'done' ? bytes(t.out_bytes || t.src_bytes) : '' }));
  }));
}
const sec = (text, extra) => el('div', { class: 'sec' }, text, extra);
function applyJobs(jobs, batches) {
  const run = jobs.filter(j => j.status === 'running'), wait = jobs.filter(j => j.status === 'queued'), fin = jobs.filter(j => !['running', 'queued'].includes(j.status)).reverse();
  const adding = batches.filter(b => b.status === 'running');
  busy = run.length + wait.length > 0 || adding.length > 0;
  const active = run.length + wait.length, bq = $('#bQueue'); bq.hidden = !active; bq.textContent = active;
  const pill = $('#pill'), r = run[0];
  pill.className = 'pill' + (busy ? ' busy' : ''); pill.classList.toggle('warn', slow);
  $('#pillText').textContent = r ? `${r.title} · ${r.finished}/${r.total}` : wait.length ? `${wait.length} waiting` : adding.length ? `Adding ${adding[0].artist}` : slow ? 'MusicBrainz slow' : 'Idle';
  for (const id of [...nodes.keys()]) if (!jobs.some(j => j.id === id)) { nodes.get(id).root.remove(); nodes.delete(id); openSet.delete(id); }
  jobs.forEach(j => { let n = nodes.get(j.id); if (!n) nodes.set(j.id, n = jobNode(j)); n.j = j; paint(n); });
  fill($('#batches'), adding.map(b => el('div', { class: 'adding' }, el('span', { class: 'spin' }),
    el('span', { text: `Adding ${b.artist} · ${b.resolved}/${b.total}` }),
    el('button', { class: 'btn sm ghost', type: 'button', text: 'Stop', onclick: async () => { try { await api('/api/batches/' + b.id + '/cancel', {}); } catch (e) { toast(e.message, 'error'); } tick(); } }))));
  const sig = [run, wait, fin].map(l => l.map(j => j.id).join()).join('|');
  if (sig !== order || !jobs.length) {
    order = sig;
    const list = (l) => l.map(j => nodes.get(j.id).root);
    fill($('#jobs'), jobs.length ? [
      run.length ? [sec('Now'), list(run)] : null,
      wait.length ? [sec(`Up next · ${wait.length}`), list(wait)] : null,
      fin.length ? [sec(`Finished · ${fin.length}`, el('button', { class: 'btn sm ghost', type: 'button', text: 'Clear', onclick: clearFinished })), list(fin)] : null,
    ] : el('div', { class: 'empty' }, el('p', { text: 'Nothing queued yet.' }), el('button', { class: 'btn go', type: 'button', text: 'Find an album', onclick: () => show('search') })));
  }
}
async function clearFinished() { try { await api('/api/jobs/clear', {}); } catch (e) { toast(e.message, 'error'); } tick(); }

const MODES = { All: 0, Info: 1, Warn: 2, Error: 3 }, RANK = { debug: 0, info: 1, ok: 1, warn: 2, error: 3 };
let logs = [], lastLog = 0, unseen = 0, wiped = 0, follow = true, mode = store.get('mode', 'Info');
if (!(mode in MODES)) mode = 'Info';
const drawSeg = () => fill($('#lvls'), Object.keys(MODES).map(m => el('button', { class: m === mode ? 'on' : '', type: 'button', text: m, onclick: () => { mode = m; store.set('mode', m); drawSeg(); renderLog(); } })));
drawSeg();
const passes = e => e.id > wiped && RANK[e.level] >= MODES[mode] && (!$('#fText').value || (e.src + ' ' + e.msg).toLowerCase().includes($('#fText').value.toLowerCase()));
const line = e => el('div', { class: 'ln ' + e.level }, el('time', { text: clock(e.t) }), el('span', { class: 'tx' }, el('span', { class: 'sr', text: e.src + ' ' }), e.msg));
function toEnd() { const t = $('#term'); t.scrollTop = t.scrollHeight; }
function renderLog() { fill($('#term'), logs.filter(passes).slice(-800).map(line)); if (follow) toEnd(); $('#latestBtn').hidden = follow; }
function applyLogs(d) {
  if (!d.logs.length) return;
  lastLog = d.last; logs.push(...d.logs); if (logs.length > 2500) logs = logs.slice(-1500);
  if (view === 'log') { const t = $('#term'); t.append(...d.logs.filter(passes).map(line)); while (t.children.length > 900) t.firstChild.remove(); if (follow) toEnd(); }
  else { unseen += d.logs.filter(e => RANK[e.level] >= 2).length; badges(); }
}
function badges() { const b = $('#bLog'); b.hidden = !unseen; b.textContent = unseen > 99 ? '99+' : unseen; }
$('#fText').addEventListener('input', renderLog);
$('#term').addEventListener('scroll', () => { const t = $('#term'); follow = t.scrollTop + t.clientHeight >= t.scrollHeight - 30; $('#latestBtn').hidden = follow; }, { passive: true });
$('#latestBtn').addEventListener('click', () => { follow = true; toEnd(); $('#latestBtn').hidden = true; });
$('#wipeBtn').addEventListener('click', () => { wiped = lastLog; renderLog(); });
$('#copyBtn').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(logs.filter(passes).map(e => `${clock(e.t)} ${e.level.toUpperCase().padEnd(5)} ${e.src.padEnd(11)} ${e.msg}`).join('\n')); toast('Log copied', 'ok'); }
  catch (_) { toast('Copy was blocked by the browser', 'error'); }
});

$('#coversBtn').addEventListener('click', async () => {
  try { await api('/api/library/covers', {}); toast('Looking for missing covers. Progress is in the Log.', 'ok', { label: 'Log', fn: () => show('log') }); }
  catch (e) { toast(e.message, 'error'); }
});
let timer = null, offline = false, ticks = 0, errAt = 0, lastErrs = null, slow = false;
function applyStats(s) {
  if (lastErrs !== null && s.mb.errors > lastErrs) errAt = Date.now();
  lastErrs = s.mb.errors; slow = Date.now() - errAt < 30000;
  $('#stats').textContent = `${s.tracks.done} ${s.tracks.done === 1 ? 'track' : 'tracks'} ·${bytes(s.bytes_out)} written · ${bytes(s.disk.free)} free`;
}
async function tick() {
  clearTimeout(timer);
  if (document.hidden) { timer = setTimeout(tick, 8000); return; }
  try {
    const [j, l, s] = await Promise.all([api('/api/jobs'), api('/api/logs?since=' + lastLog), ticks++ % 3 === 0 ? api('/api/stats') : null]);
    if (offline) { offline = false; toast('Reconnected', 'ok'); }
    if (s) applyStats(s); applyJobs(j.jobs, j.batches); applyLogs(l);
    if (view === 'queue') {
      const open = j.jobs.filter(x => isOpen(x));
      (await Promise.all(open.map(x => api('/api/jobs/' + x.id).catch(() => null)))).forEach(d => d && nodes.get(d.id) && paintTracks(nodes.get(d.id), d));
    }
  } catch (e) {
    if (!offline) toast('Lost contact with the server. Retrying…', 'error');
    offline = true; $('#pillText').textContent = 'Offline'; $('#pill').className = 'pill bad';
  }
  timer = setTimeout(tick, busy || view === 'log' ? 2000 : 6000);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
document.addEventListener('keydown', ev => { if (ev.key === '/' && !/INPUT|TEXTAREA/.test(document.activeElement.tagName)) { ev.preventDefault(); show('search'); $('#q').focus(); } });
show((location.hash || '').slice(1) || store.get('view', 'search'));
tick();
