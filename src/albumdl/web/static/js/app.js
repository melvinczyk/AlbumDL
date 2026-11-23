'use strict';
const $ = s => document.querySelector(s);
const coverUrl = (release, group, artist, title, size) => '/api/cover?' + new URLSearchParams({ release: release || '', group: group || '', artist, title, size });
const el = (tag, a, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(a || {})) {
    if (v === false || v == null) continue;
    if (k === 'class') e.className = v; else if (k === 'text') e.textContent = v; else if (k === 'style') e.style.cssText = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k.includes('-') || k === 'type' || k === 'list') e.setAttribute(k, v === true ? '' : v);
    else { try { e[k] = v; } catch (_) { e.setAttribute(k, v); } }
  }
  e.append(...kids.flat(2).filter(x => x != null && x !== false));
  return e;
};
const fill = (node, ...kids) => { node.replaceChildren(...kids.flat(2).filter(x => x != null && x !== false)); return node; };
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
const mmss = s => s == null ? '' : Math.floor(s / 60) + ':' + pad(Math.round(s % 60));
const dur = s => { s = Math.round(s || 0); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return h ? `${h}h ${m}m` : m ? `${m}m ${pad(s % 60)}s` : `${s}s`; };
const bytes = n => { n = n || 0; const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return (i > 1 ? n.toFixed(1) : Math.round(n)) + ' ' + u[i]; };
const clock = (t, ms) => { const d = new Date(t * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` + (ms ? '.' + String(d.getMilliseconds()).padStart(3, '0') : ''); };
function toast(text, kind = 'info', action) {
  const t = el('div', { class: 'toast' + (kind === 'error' ? ' bad' : '') }, pic(kind === 'error' ? 'x' : 'check', kind === 'error' ? 'r' : 'c'), el('span', { text }),
    action ? el('button', { class: 'btn sm', type: 'button', text: action.label, onclick: () => { action.fn(); t.remove(); } }) : null);
  $('#toasts').append(t); setTimeout(() => t.remove(), kind === 'error' ? 8000 : 4000);
}
function cover(urls) {
  const box = el('span', { class: 'cover' }), img = el('img', { alt: '', loading: 'lazy', decoding: 'async' });
  let i = 0; const next = () => { if (i < urls.length) img.src = urls[i++]; };
  img.onload = () => img.classList.add('ok'); img.onerror = next; box.append(img); next();
  return box;
}
let view = 'discover';
function show(name) {
  if (!document.getElementById('v-' + name)) name = 'discover';
  view = name; store.set('view', name);
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'v-' + name));
  document.querySelectorAll('nav button').forEach(b => b.setAttribute('aria-selected', String(b.dataset.view === name)));
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  if (name === 'console') { unseen = 0; badges(); renderConsole(); }
  if (name === 'queue') tick();
}
document.querySelectorAll('nav button').forEach(b => b.addEventListener('click', () => show(b.dataset.view)));
addEventListener('hashchange', () => show(location.hash.slice(1)));
let errAt = 0, lastErrs = null;
function applyStats(s) {
  $('#sTracks').textContent = `${s.tracks.done} tracks downloaded`; $('#sWritten').textContent = bytes(s.bytes_out) + ' written'; $('#sDisk').textContent = bytes(s.disk.free) + ' free';
  if (lastErrs !== null && s.mb.errors > lastErrs) errAt = Date.now();
  lastErrs = s.mb.errors;
  const slow = Date.now() - errAt < 30000;
  $('#led').className = 'led' + (slow ? ' warn' : ''); $('#led').title = slow ? 'MusicBrainz is slow or throttling; retrying' : 'MusicBrainz reachable';
}
let results = [], rFilter = 'All', current = null, artistCtx = null, confirming = null;
const selected = new Map();
const typeOf = r => ['Album', 'EP', 'Single'].includes(r.type) ? (r.secondary.length ? r.secondary[0] : r.type) : (r.type || 'Other');
$('#searchForm').addEventListener('submit', async ev => {
  ev.preventDefault();
  const artist = $('#artist').value.trim(), album = $('#album').value.trim(), m = $('#searchMsg');
  if (!artist && !album) return;
  fill($('#results')); fill($('#artistInfo')); fill($('#detail')); selected.clear(); artistCtx = null;
  fill(m, el('div', { class: 'msg' }, el('span', { class: 'spin' }), ' Searching…'));
  $('#searchBtn').disabled = true;
  try {
    if (artist && !album && !/musicbrainz\.org/i.test(artist)) {
      const { artists } = await api('/api/artists?q=' + encodeURIComponent(artist));
      if (artists.length) { fill(m); $('#searchBtn').disabled = false; return showArtist(artists[0], artists); }
    }
    const d = await api('/api/search?artist=' + encodeURIComponent(artist) + '&album=' + encodeURIComponent(album));
    if (d.direct) { fill(m); return d.direct.kind === 'release' ? showRelease(d.direct.id) : showGroup(d.direct.id); }
    results = d.results; rFilter = 'All';
    fill(m, !results.length && el('div', { class: 'msg' }, 'No albums found. Check the spelling or search the artist alone.'));
    renderResults();
  } catch (e) { fill(m, el('div', { class: 'msg err', text: e.message })); }
  $('#searchBtn').disabled = false;
});
async function queueAlbums(groups, skipOwned, name) {
  const r = await api('/api/discography', { name, groups, skip_owned: skipOwned });
  toast(`Queuing ${r.batch.total} albums`, 'ok', { label: 'View queue', fn: () => show('queue') }); tick();
}
function renderResults(title) {
  const host = $('#results'); if (!results.length) return fill(host);
  const kinds = ['All', ...new Set(results.map(typeOf))], rows = results.filter(r => rFilter === 'All' || typeOf(r) === rFilter);
  const bar = el('div', { class: 'head' });
  const drawBar = () => {
    const n = selected.size, todo = rows.filter(r => !r.owned || r.owned.incomplete).length;
    const go = el('button', { class: 'btn ' + (confirming ? 'hot' : 'go'), type: 'button', disabled: !n && !todo }, pic('download', 'k'),
      n ? `Download selected (${n})` : confirming ? `Confirm: queue ${todo} albums` : `Download discography (${todo})`);
    go.onclick = async () => {
      if (n) {
        go.disabled = true;
        try { await queueAlbums([...selected.values()], false, artistCtx ? artistCtx.name : [...selected.values()][0].artist); selected.clear(); renderResults(title); } catch (e) { toast(e.message, 'error'); go.disabled = false; }
      } else if (!confirming) {
        confirming = setTimeout(() => { confirming = null; drawBar(); }, 4000); drawBar();
      } else {
        clearTimeout(confirming); confirming = null; go.disabled = true;
        try { await queueAlbums(rows.map(r => ({ id: r.id, title: r.title, artist: r.artist })), true, artistCtx.name); } catch (e) { toast(e.message, 'error'); }
        drawBar();
      }
    };
    fill(bar, el('button', { class: 'btn sm ghost', type: 'button', text: 'Select all shown', onclick: () => { rows.forEach(r => selected.set(r.id, { id: r.id, title: r.title, artist: r.artist })); renderResults(title); } }),
      n ? el('button', { class: 'btn sm ghost', type: 'button', text: 'Clear selection', onclick: () => { selected.clear(); renderResults(title); } }) : null,
      artistCtx || n ? el('span', { class: 'push' }, go) : null);
  };
  fill(host,
    el('div', { class: 'head' }, el('h2', { text: title || 'Results' }), el('span', { class: 'counts', text: `${rows.length} of ${results.length}` }),
      kinds.length > 2 ? el('span', { class: 'row push', style: 'gap:5px' }, kinds.map(k => el('button', { class: 'chip' + (k === rFilter ? ' on' : ''), type: 'button', text: k, onclick: () => { rFilter = k; renderResults(title); } }))) : null),
    bar,
    el('div', { class: 'grid' }, rows.map(r => {
      const card = el('div', { class: 'card' + (current && current.group_id === r.id ? ' sel' : '') + (selected.has(r.id) ? ' picked' : '') });
      const box = el('input', { type: 'checkbox', checked: selected.has(r.id), 'aria-label': 'Select ' + r.title, onchange: ev => {
        ev.target.checked ? selected.set(r.id, { id: r.id, title: r.title, artist: r.artist }) : selected.delete(r.id);
        card.classList.toggle('picked', ev.target.checked); clearTimeout(confirming); confirming = null; drawBar(); } });
      card.append(el('label', { class: 'pick', title: 'Select' }, box),
        r.owned ? el('span', { class: 'own' + (r.owned.incomplete ? ' incomplete' : '') }, pic('check', 'k'), r.owned.incomplete ? 'Incomplete' : 'In folder') : null,
        el('button', { class: 'open', type: 'button', onclick: () => showGroup(r.id) }, cover([coverUrl('', r.id, r.artist, r.title, 250)]),
          el('span', { class: 'meta' }, el('span', { class: 't', text: r.title, title: r.title }), el('span', { class: 's', text: r.artist }),
            el('span', { class: 's', text: [r.date.slice(0, 4), typeOf(r)].filter(Boolean).join(' · ') }))));
      return card;
    })));
  drawBar();
}
async function showArtist(a, all) {
  const info = $('#artistInfo'), m = $('#searchMsg');
  fill($('#results')); fill($('#detail')); current = null; selected.clear(); artistCtx = { id: a.id, name: a.name };
  fill(info, el('div', { class: 'panel' }, el('div', { class: 'row' }, el('h3', { text: a.name, style: 'font-size:24px' }),
    ...[a.type, a.country, [a.begin, a.end].filter(Boolean).join('–'), a.disambiguation].filter(Boolean).map(t => el('span', { class: 'chip', text: t }))),
    all.length > 1 ? el('div', { class: 'row', style: 'margin-top:10px;gap:6px' }, el('small', { text: 'Not the right artist?' }), ...all.filter(x => x.id !== a.id).slice(0, 4).map(x =>
      el('button', { class: 'chip', type: 'button', text: x.name + (x.disambiguation ? ' · ' + x.disambiguation : ''), onclick: () => showArtist(x, all) }))) : null));
  fill(m, el('div', { class: 'msg' }, el('span', { class: 'spin' }), ` Loading ${a.name}'s releases…`));
  try {
    const d = await api('/api/artist/' + a.id);
    fill(m); results = d.albums; rFilter = 'All'; renderResults('Releases');
  } catch (e) { fill(m, el('div', { class: 'msg err', text: e.message })); }
}
async function showGroup(gid) {
  const host = $('#detail');
  fill(host, el('div', { class: 'msg' }, el('span', { class: 'spin' }), ' Loading…'));
  try {
    const { editions } = await api('/api/group/' + gid);
    if (!editions.length) throw new Error('No releases found for that album.');
    await showRelease(editions[0].id, editions);
  } catch (e) { fill(host, el('div', { class: 'msg err', text: e.message })); }
}
async function showRelease(mbid, editions) {
  const host = $('#detail');
  fill(host, el('div', { class: 'msg' }, el('span', { class: 'spin' }), ' Loading tracklist…'));
  try {
    const a = await api('/api/release/' + mbid);
    if (!editions && a.group_id) editions = (await api('/api/group/' + a.group_id)).editions;
    current = a; renderDetail(a, editions || []);
    host.scrollIntoView({ block: 'start' });
  } catch (e) { fill(host, el('div', { class: 'msg err', text: e.message })); }
}
function renderDetail(a, editions) {
  const status = el('div');
  const dl = el('button', { class: 'btn go', type: 'button' }, pic('download', 'k'), a.owned ? 'Download again' : 'Download album');
  dl.addEventListener('click', async () => {
    dl.disabled = true;
    try {
      await api('/api/download', { mbid: a.mbid });
      toast(`Queued ${a.title}`, 'ok', { label: 'View queue', fn: () => show('queue') }); tick();
    } catch (e) { fill(status, el('div', { class: 'msg err', text: e.message })); toast(e.message, 'error'); }
    dl.disabled = false;
  });
  const edSel = editions.length > 1 ? el('select', { 'aria-label': 'Edition', onchange: ev => showRelease(ev.target.value, editions) },
    editions.map(e => el('option', { value: e.id, selected: e.id === a.mbid, text: [e.date, e.country, e.format, e.tracks + ' tracks', e.status !== 'Official' ? e.status : '', e.disambiguation].filter(Boolean).join(' · ') }))) : null;
  const multi = a.disc_total > 1;
  fill($('#detail'), el('div', { class: 'panel detail' }, cover([coverUrl(a.mbid, a.group_id, a.artist, a.title, 500)]),
    el('div', { style: 'min-width:0' },
      el('h3', { text: a.title, style: 'font-size:24px' }), el('div', { class: 'hint', text: a.artist, style: 'font-size:15px' }),
      el('div', { class: 'chips' }, [(a.original_date || a.date || '').slice(0, 4), a.label, `${a.tracks.length} tracks`, dur(a.total_seconds), ...a.genres.slice(0, 3)].filter(Boolean).map(t => el('span', { class: 'chip', text: t })),
        el('a', { class: 'chip', href: 'https://musicbrainz.org/release/' + a.mbid, target: '_blank', rel: 'noopener', text: 'MusicBrainz ↗' })),
      a.owned ? el('div', { class: 'msg', style: `margin-bottom:10px;border-color:var(--${a.owned.incomplete ? 'amber' : 'mint'})` }, pic('check', 'c'),
        a.owned.incomplete ? ' Incomplete: some tracks are missing. Download again to fill them in.' : ` Already in the folder (${a.owned.tracks} tracks, ${bytes(a.owned.size)}). Downloading again replaces everything.`) : null,
      edSel ? el('div', { class: 'row', style: 'margin-bottom:8px' }, el('small', { text: 'Edition' }), edSel) : null,
      el('div', { class: 'row' }, dl),
      el('details', {}, el('summary', { text: `Tracklist (${a.tracks.length})` }), el('div', { class: 'scroll-x' }, el('table', { class: 'tl' },
        el('tbody', {}, a.tracks.map(t => el('tr', {}, el('td', { class: 'n', text: (multi ? t.disc + '-' : '') + t.number }), el('td', {}, t.title, t.artist ? el('small', { text: t.artist }) : null), el('td', { class: 'd', text: mmss(t.seconds) }))))))),
      status, el('p', { class: 'hint', text: 'Saves to ' + a.folder }))));
}
const jobNodes = new Map(), tabOf = {};
let busy = false, openJob = null;
function renderBatches(batches) {
  const sig = JSON.stringify(batches.map(b => [b.id, b.resolved, b.queued, b.skipped, b.failed, b.status, b.current]));
  if (renderBatches.sig === sig) return; renderBatches.sig = sig;
  fill($('#batches'), batches.map(b => {
    const pct = b.total ? Math.round(100 * b.resolved / b.total) : 0, run = b.status === 'running';
    return el('div', { class: 'panel' },
      el('div', { class: 'row' }, el('h3', { text: 'Queuing: ' + b.artist }), el('span', { class: 'st ' + (run ? '' : b.status), text: b.status }),
        run ? el('button', { class: 'btn sm bad push', type: 'button', text: 'Stop', onclick: async () => { try { await api('/api/batches/' + b.id + '/cancel', {}); } catch (e) { toast(e.message, 'error'); } tick(); } }) : null),
      el('div', { class: 'bar' + (run ? '' : ' done') }, el('i', { style: `width:${pct}%` }), el('b', { text: `${b.resolved}/${b.total}` })),
      el('div', { class: 'sum', text: `${b.queued} queued, ${b.skipped} already in the folder, ${b.failed} failed` + (b.current ? `. Looking up ${b.current}` : '') }));
  }));
}
function syncOpen() {
  jobNodes.forEach((n, id) => { n.root.classList.toggle('open', id === openJob); n.more.textContent = id === openJob ? 'Hide' : 'Details'; });
}
function applyJobs(jobs, batches = []) {
  renderBatches(batches);
  const act = jobs.filter(j => j.status === 'queued' || j.status === 'running'), expanding = batches.some(b => b.status === 'running');
  busy = act.length > 0 || expanding;
  const bq = $('#bQueue'); bq.hidden = !(act.length || expanding); bq.textContent = act.length || '…';
  $('#queueCounts').textContent = jobs.length ? `${jobs.length} downloads, ${act.length} active` : '';
  const run = jobs.find(j => j.status === 'running');
  $('#sMain').textContent = run ? `Downloading ${run.title}: ${run.finished}/${run.total}` + (act.length > 1 ? ` (+${act.length - 1} queued)` : '') : act.length ? `${act.length} waiting` : 'Idle';
  const host = $('#jobs'), ids = jobs.map(j => j.id), nodes = jobs.map(j => updateJob(j));
  for (const id of [...jobNodes.keys()]) if (!ids.includes(id)) { jobNodes.get(id).root.remove(); jobNodes.delete(id); }
  if (!jobs.length) return fill(host, el('div', { class: 'panel empty' }, 'Nothing queued. Find an album in Discover and press Download.'));
  if (host.firstChild && !host.firstChild.classList.contains('job')) host.replaceChildren();
  nodes.forEach((n, i) => { if (host.children[i] !== n) host.insertBefore(n, host.children[i] || null); });
  if (openJob && !ids.includes(openJob)) { openJob = null; fill($('#jobDetail')); delete $('#jobDetail').dataset.job; }
  syncOpen();
}
function updateJob(j) {
  let n = jobNodes.get(j.id);
  if (!n) {
    n = { rows: new Map() };
    n.name = el('div', { class: 'name' }); n.by = el('div', { class: 'by' }); n.st = el('span', { class: 'st' });
    n.cancel = el('button', { class: 'btn sm bad', type: 'button', text: 'Cancel', onclick: async () => { try { await api('/api/jobs/' + j.id + '/cancel', {}); } catch (e) { toast(e.message, 'error'); } tick(); } });
    n.retry = el('button', { class: 'btn sm hot', type: 'button', text: 'Retry', onclick: async () => { try { await api('/api/jobs/' + j.id + '/retry', {}); toast('Download started again', 'ok'); } catch (e) { toast(e.message, 'error'); } tick(); } });
    n.more = el('button', { class: 'btn sm ghost', type: 'button', text: 'Details', onclick: () => { openJob = openJob === j.id ? null : j.id; if (!openJob) { fill($('#jobDetail')); delete $('#jobDetail').dataset.job; } syncOpen(); tick().then(() => openJob && $('#jobDetail').scrollIntoView({ block: 'nearest' })); } });
    n.fillBar = el('i'); n.barText = el('b'); n.bar = el('div', { class: 'bar' }, n.fillBar, n.barText);
    n.sum = el('div', { class: 'sum' }); n.err = el('div', { class: 'msg err', hidden: true });
    n.root = el('article', { class: 'job' }, el('div', { class: 'jcover' }, cover([coverUrl(j.mbid, j.group_id, j.artist, j.title, 250)]), n.st),
      el('div', { class: 'jbody' }, n.name, n.by, n.bar, n.sum, n.err, el('div', { class: 'acts' }, n.cancel, n.retry, n.more)));
    jobNodes.set(j.id, n);
  }
  const pct = j.total ? Math.round(100 * j.finished / j.total) : 0, active = j.status === 'running' || j.status === 'queued';
  n.name.textContent = j.title; n.name.title = j.title; n.by.textContent = [j.artist, j.year].filter(Boolean).join(' · ');
  n.st.textContent = j.status; n.st.className = 'st ' + j.status; n.cancel.hidden = !active; n.retry.hidden = active;
  n.bar.className = 'bar ' + (active ? '' : j.status); n.fillBar.style.width = (j.status === 'queued' ? 0 : pct) + '%'; n.barText.textContent = j.status === 'queued' ? 'waiting' : `${j.finished}/${j.total}`;
  const eta = j.status === 'running' && j.finished > 0 ? (j.total - j.finished) * (j.elapsed / j.finished) : null;
  n.sum.textContent = [j.started ? dur(j.elapsed) : '', eta != null ? `~${dur(eta)} left` : '', j.bytes ? bytes(j.bytes) : '', j.failed ? j.failed + ' failed' : '', j.retries ? j.retries + ' retries' : ''].filter(Boolean).join(' · ');
  n.err.hidden = !j.error; n.err.textContent = j.error;
  return n.root;
}
function trackRow(n, t) {
  let r = n.rows.get(t.n);
  if (!r) { r = { ico: el('span', { class: 'ico' }), title: el('td'), src: el('td'), size: el('td', { class: 'd' }) }; r.tr = el('tr', {}, el('td', {}, r.ico), el('td', { class: 'n', text: t.n }), r.title, r.src, r.size); n.rows.set(t.n, r); }
  r.ico.className = 'ico ' + t.state; r.tr.className = t.state === 'failed' ? 'err' : '';
  fill(r.title, t.title, t.state !== 'done' && t.text ? el('small', { text: t.text }) : null);
  fill(r.src, t.channel ? el('a', { href: t.url, target: '_blank', rel: 'noopener', text: t.channel }) : null, t.acodec ? el('small', { text: `source ${t.acodec}${t.src_kbps ? ' ' + t.src_kbps + 'k' : ''} → saved as mp3` }) : null);
  fill(r.size, t.out_bytes ? bytes(t.out_bytes) : t.src_bytes ? bytes(t.src_bytes) : '');
  return r.tr;
}
function renderDetailPane(d) {
  const n = jobNodes.get(d.id), host = $('#jobDetail'); if (!n) return;
  if (host.dataset.job !== d.id) {
    host.dataset.job = d.id;
    fill(host, el('div', { class: 'panel' }, el('div', { class: 'row' }, el('h3', { id: 'dTitle' }), el('button', { class: 'btn sm ghost push', type: 'button', text: 'Close', onclick: () => { openJob = null; fill(host); delete host.dataset.job; syncOpen(); } })),
      el('div', { class: 'jtabs', id: 'dTabs' }), el('div', { id: 'dPane' })));
  }
  const tab = tabOf[d.id] || 'tracks';
  $('#dTitle').textContent = `${d.artist} - ${d.title}`;
  fill($('#dTabs'), [['tracks', 'Tracks'], ['log', 'Log']].map(([k, l]) => el('button', { class: 'chip' + (k === tab ? ' on' : ''), type: 'button', text: l, onclick: () => { tabOf[d.id] = k; renderDetailPane(d); } })));
  if (tab === 'tracks') fill($('#dPane'), el('div', { class: 'scroll-x' }, el('table', { class: 'tl' }, el('thead', {}, el('tr', {}, ['', '#', 'Title', 'Source', 'Size'].map(h => el('th', { text: h })))), el('tbody', {}, d.tracks.map(t => trackRow(n, t))))));
  else { const box = el('div', { class: 'jlog' }, d.log.map(e => el('div', { class: e.level }, el('time', { text: clock(e.t, true) }), e.msg))); fill($('#dPane'), box); box.scrollTop = box.scrollHeight; }
}
$('#clearBtn').addEventListener('click', async () => { try { const r = await api('/api/jobs/clear', {}); toast(`Cleared ${r.cleared} finished downloads`, 'ok'); } catch (e) { toast(e.message, 'error'); } tick(); });
const LEVELS = ['debug', 'info', 'ok', 'warn', 'error'];
let logs = [], lastLog = 0, unseen = 0, follow = true, wiped = 0;
const lvOn = new Set(store.get('levels', LEVELS));
LEVELS.forEach(l => $('#lvls').append(el('button', { class: 'chip' + (lvOn.has(l) ? ' on' : ''), type: 'button', text: l, onclick: ev => { lvOn.has(l) ? lvOn.delete(l) : lvOn.add(l); ev.target.classList.toggle('on'); store.set('levels', [...lvOn]); renderConsole(); } })));
const passes = e => e.id > wiped && lvOn.has(e.level) && (!$('#fSrc').value || e.src === $('#fSrc').value) && (!$('#fText').value || (e.src + ' ' + e.msg).toLowerCase().includes($('#fText').value.toLowerCase()));
const line = e => el('div', { class: 'ln ' + e.level }, el('time', { text: clock(e.t, true) }), el('span', { class: 'lv', text: e.level }), el('span', { class: 'sr', text: e.src }), el('span', { class: 'tx', text: e.msg }));
function counts(n) { const c = { warn: 0, error: 0 }; logs.forEach(e => { if (e.id > wiped && c[e.level] != null) c[e.level]++; }); $('#logCounts').textContent = `${n} lines, ${c.error} errors, ${c.warn} warnings`; }
function renderConsole() { const rows = logs.filter(passes).slice(-1000); fill($('#term'), rows.map(line)); counts(rows.length); if (follow) $('#term').scrollTop = $('#term').scrollHeight; }
function applyLogs(d) {
  if (!d.logs.length) return;
  lastLog = d.last; logs.push(...d.logs); if (logs.length > 3000) logs = logs.slice(-2000);
  const sel = $('#fSrc'), known = new Set([...sel.options].map(o => o.value));
  d.logs.forEach(e => { if (!known.has(e.src)) { known.add(e.src); sel.append(el('option', { value: e.src, text: e.src })); } });
  if (view === 'console') { const term = $('#term'); term.append(...d.logs.filter(passes).map(line)); while (term.children.length > 1200) term.firstChild.remove(); counts(term.children.length); if (follow) term.scrollTop = term.scrollHeight; }
  else { unseen += d.logs.filter(e => e.level === 'error' || e.level === 'warn').length; badges(); }
}
function badges() { const b = $('#bLog'); b.hidden = !unseen; b.textContent = unseen > 99 ? '99+' : unseen; }
$('#fSrc').addEventListener('change', renderConsole); $('#fText').addEventListener('input', renderConsole);
$('#followBtn').addEventListener('click', () => { follow = !follow; $('#followBtn').textContent = follow ? 'Following' : 'Paused'; if (follow) renderConsole(); });
$('#term').addEventListener('wheel', () => { const t = $('#term'); if (follow && t.scrollTop + t.clientHeight < t.scrollHeight - 40) { follow = false; $('#followBtn').textContent = 'Paused'; } }, { passive: true });
$('#wipeBtn').addEventListener('click', () => { wiped = lastLog; renderConsole(); });
const plain = () => logs.filter(passes).map(e => `${clock(e.t, true)} ${e.level.toUpperCase().padEnd(5)} ${e.src.padEnd(11)} ${e.msg}`).join('\n');
$('#copyBtn').addEventListener('click', async () => { try { await navigator.clipboard.writeText(plain()); toast('Log copied', 'ok'); } catch (_) { toast('Copy was blocked by the browser', 'error'); } });
$('#saveBtn').addEventListener('click', () => { const a = el('a', { href: URL.createObjectURL(new Blob([plain() + '\n'], { type: 'text/plain' })), download: 'albumdl.log' }); document.body.append(a); a.click(); a.remove(); });
let timer = null, offline = false, ticks = 0;
async function tick() {
  clearTimeout(timer);
  try {
    const [j, l, s] = await Promise.all([api('/api/jobs'), api('/api/logs?since=' + lastLog), ticks++ % 3 === 0 ? api('/api/stats') : null]);
    offline = false; if (s) applyStats(s); applyJobs(j.jobs, j.batches); applyLogs(l);
    if (view === 'queue' && openJob) {
      const d = await api('/api/jobs/' + openJob).catch(() => null);
      if (d) renderDetailPane(d);
    }
  } catch (e) {
    if (!offline) toast('Lost contact with the server; retrying…', 'error');
    offline = true; $('#sMain').textContent = 'Server unreachable'; $('#led').className = 'led bad';
  }
  timer = setTimeout(tick, busy || view === 'console' ? 2000 : 6000);
}

document.addEventListener('keydown', ev => { if (ev.key === '/' && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) { ev.preventDefault(); show('discover'); $('#artist').focus(); } });
(async () => {
  show((location.hash || '').slice(1) || store.get('view', 'discover'));
  tick();
})();
