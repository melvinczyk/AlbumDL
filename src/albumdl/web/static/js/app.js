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

const VIEWS = { search: 'search', discover: 'discover', library: 'library', queue: 'queue', log: 'log', console: 'log' };
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
  if (name === 'discover' && !discSeed && !discData) loadDiscover();
  scrollTo(0, 0);
}
document.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => show(b.dataset.view)));
addEventListener('hashchange', () => show(location.hash.slice(1)));

let lastQ = '', results = [], title = '', filter = 'All', artistCtx = null, fresh = false;
const selected = new Map();
const typeOf = r => ['Album', 'EP', 'Single'].includes(r.type) ? (r.secondary.length ? r.secondary[0] : r.type) : (r.type || 'Other');
const pick = r => ({ id: r.id, title: r.title, artist: r.artist, ytm: r.ytm });
const skeleton = () => el('div', { class: 'grid' }, Array.from({ length: 8 }, () => el('div', { class: 'card sk' }, el('span', { class: 'cover' }), el('span', { class: 'meta' }, el('b', { text: '\u00a0' }), el('small', { text: '\u00a0' })))));

$('#searchForm').addEventListener('submit', async ev => {
  ev.preventDefault();
  const q = $('#q').value.trim(); if (!q) return; lastQ = q.replace(/\s+[-–—]\s+/g, ' ');
  $('#q').blur(); $('#searchBtn').disabled = true;
  selected.clear(); artistCtx = null; drawActions(); fill($('#artistBar')); fill($('#artistTop')); fill($('#similar'));
  fill($('#results'), skeleton());
  try { await search(q); } catch (e) { fill($('#results'), el('div', { class: 'msg err', text: e.message })); }
  $('#searchBtn').disabled = false;
});
async function search(q, hint = '') {
  if (/(?:youtube\.com|youtu\.be)\//i.test(q)) return importYt(q);
  const link = q.match(/musicbrainz\.org\/(release-group|release)\/([0-9a-f-]{36})/i);
  if (link) return direct(link[1].toLowerCase(), link[2].toLowerCase());
  const split = q.split(/\s+[-–—]\s+/);
  if (split.length > 1) {
    const d = await api('/api/search?artist=' + encodeURIComponent(split[0]) + '&album=' + encodeURIComponent(split.slice(1).join(' - ')));
    if (d.results.length) return rows(d, 'Results');
    const t = await api('/api/search?q=' + encodeURIComponent(split.join(' ')));
    return t.results.length ? rows(t, 'Results') : searchYtm(split.join(' '));
  }
  const { artists } = await api('/api/artists?q=' + encodeURIComponent(q));
  const exact = artists.filter(a => norm(a.name) === norm(q));
  if (exact.length > 1) return pickArtist(exact, artists.filter(a => !exact.includes(a)), undefined, undefined, hint);
  if (exact.length === 1) return showArtist(exact[0], artists);
  const t = await api('/api/search?q=' + encodeURIComponent(q));
  if (t.results.length) return rows(t, 'Results');
  const close = artists.filter(a => a.match >= 0.75).sort((a, b) => b.match - a.match);
  if (close.length && (close[0].score >= 85 || close[0].match >= 0.8)) return showArtist(close[0], artists);
  const d = await api('/api/search?album=' + encodeURIComponent(q));
  if (d.results.length) return rows(d, 'Results');
  if (close.length) return showArtist(close[0], artists);
  searchYtm(q);
}
const norm = s => s.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
const who = a => [a.type, a.country, [a.begin, a.end].filter(Boolean).join('–'), a.disambiguation].filter(Boolean).join(' · ');
function pickArtist(list, others, head = `Which ${list[0].name}?`, sub = `${list.length} artists share this name`, hint = '') {
  fill($('#artistBar')); fill($('#artistTop')); fill($('#similar'));
  fill($('#results'), el('div', { class: 'bar' }, el('h2', { text: head }), el('span', { class: 'sub', text: sub })),
    [...list].sort((x, y) => (y.id === hint) - (x.id === hint)).map(a => el('button', { class: 'lrow', type: 'button', onclick: () => showArtist(a, list.concat(others)) },
      el('span', { class: 'jtext' }, el('b', { text: a.name }), el('small', { text: who(a) || 'No details on MusicBrainz' }), a.id === hint ? el('span', { class: 'st run', text: 'Suggested by Last.fm' }) : null))),
    others.length ? el('div', { class: 'alts', style: 'margin-top:14px' }, el('span', { text: 'Or' }), others.slice(0, 10).map(a =>
      el('button', { class: 'chip', type: 'button', text: a.name + (who(a) ? ' · ' + who(a) : ''), onclick: () => showArtist(a, list.concat(others)) }))) : null);
}
async function importYt(url) {
  const d = await api('/api/yt/playlist?url=' + encodeURIComponent(url));
  const field = (label, key, hint) => { const i = el('input', { type: 'text', value: d[key], 'aria-label': label, placeholder: hint, oninput: () => { d[key] = i.value; } }); return el('label', { class: 'field' }, el('small', { text: label }), i); };
  d.tracks.forEach((t, i) => { t.key = i; });
  const picked = new Set(d.tracks.map(t => t.key)), go = el('button', { class: 'btn go', type: 'button' });
  const sync = () => { go.disabled = !picked.size; go.textContent = `Download ${picked.size} ${picked.size === 1 ? 'track' : 'tracks'}`; };
  go.onclick = async () => {
    go.disabled = true;
    try {
      const r = await api('/api/yt/download', { artist: d.artist, album: d.album, year: d.year, genre: d.genre, tracks: d.tracks.filter(t => picked.has(t.key)) });
      toast(r.queued ? `Queued ${d.album}` : 'Already in the queue', 'ok', { label: 'Queue', fn: () => show('queue') }); tick();
    } catch (e) { toast(e.message, 'error'); }
    sync();
  };
  selected.clear(); drawActions(); results = [];
  fill($('#results'), el('div', { class: 'bar' }, el('h2', { text: 'Import from YouTube' }), el('span', { class: 'sub', text: d.tracks[0].end ? d.tracks.length + ' chapters in one video' : d.tracks.length + ' videos' }), el('span', { style: 'margin-left:auto' }, go)),
    el('div', { class: 'imp' }, field('Artist', 'artist', 'Various Artists'), field('Album', 'album', ''), field('Year', 'year', 'optional'), field('Genre', 'genre', 'optional')),
    el('div', { class: 'tlist' }, d.tracks.map(t => {
      const box = el('input', { type: 'checkbox', checked: true, 'aria-label': 'Include ' + t.title, onchange: () => { box.checked ? picked.add(t.key) : picked.delete(t.key); sync(); } });
      const name = el('input', { type: 'text', value: t.title, 'aria-label': 'Track title', oninput: () => { t.title = name.value; } });
      return el('div', { class: 'trow' }, box, el('span', { class: 'sz', text: String(t.number).padStart(2, '0') }), name, el('span', { class: 'sz', text: t.duration ? Math.floor(t.duration / 60) + ':' + pad(t.duration % 60) : '' }));
    })),
    el('p', { class: 'foot', text: 'Tags come from what you enter here. The cover is the video thumbnail.' }));
  sync();
}
async function searchYtm(q) {
  fill($('#results'), skeleton());
  try {
    const d = await api('/api/ytm/search?q=' + encodeURIComponent(q));
    if (!d.results.length) return searchUploads(q);
    rows(d, 'YouTube Music');
  } catch (e) { fill($('#results'), el('div', { class: 'msg err', text: e.message })); }
}
async function searchUploads(q) {
  fill($('#results'), skeleton());
  try {
    const { results: list } = await api('/api/yt/search?q=' + encodeURIComponent(q));
    if (!list.length) return fill($('#results'), el('div', { class: 'msg', text: 'Nothing found on MusicBrainz, YouTube Music or YouTube. Check the spelling, or paste a link.' }));
    results = []; selected.clear(); drawActions();
    fill($('#results'), el('div', { class: 'bar' }, el('h2', { text: 'YouTube uploads' }), el('span', { class: 'sub', text: 'Pick one to import it' })),
      list.map(u => el('button', { class: 'lrow upl', type: 'button', onclick: () => { fill($('#results'), skeleton()); importYt(u.url).catch(e => fill($('#results'), el('div', { class: 'msg err', text: e.message }))); } },
        cover(u.thumb), el('span', { class: 'jtext' }, el('b', { text: u.title, title: u.title }),
          el('small', { text: [u.kind === 'playlist' ? 'Playlist' : 'Video' + (u.duration ? ' · ' + Math.round(u.duration / 60) + ' min' : ''), u.channel].filter(Boolean).join(' · ') })))),
      el('p', { class: 'foot', text: 'Unofficial uploads: you set the artist, album and track names before downloading.' }));
  } catch (e) { fill($('#results'), el('div', { class: 'msg err', text: e.message })); }
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
  $('#q').value = x.name; selected.clear(); drawActions(); fill($('#artistBar')); fill($('#artistTop')); fill($('#results'), skeleton());
  try {
    if (x.id) return await showArtist({ id: x.id, name: x.name }, []);
    await search(x.name, x.hint);
  } catch (e) { fill($('#results'), el('div', { class: 'msg err', text: e.message })); }
}
let artistTop = null;
const mmss = n => n ? Math.floor(n / 60) + ':' + pad(n % 60) : '';
const genreTags = (line, genres) => { if (genres && genres.length && line && !line.dataset.g) { line.dataset.g = 1; line.append(el('span', { class: 'genres' }, genres.map(g => el('i', { text: g })))); } };
async function showArtist(a, all) {
  artistCtx = a; artistTop = null; selected.clear(); drawActions(); results = [];
  const alts = all.filter(x => x.id !== a.id).slice(0, 5), line = el('p', { class: 'sub' }, el('span', { text: who(a) }));
  const bg = el('span', { class: 'abg' }), art = face({ name: a.name }, 'xl');
  const selAll = el('button', { class: 'btn go', type: 'button', onclick: () => {
    results.filter(r => typeOf(r) === 'Album' && !(r.owned && !r.owned.incomplete && !r.owned.picked)).forEach(r => selected.set(r.id, pick(r))); renderResults(); drawActions(); } }, pic('download', 'k'), 'Select all albums');
  fill($('#artistBar'), el('header', { class: 'ahero' }, bg, art,
    el('div', { class: 'acopy' }, el('small', { text: 'Artist' }), el('h1', { text: a.name }), line,
      el('div', { class: 'dbtns' }, selAll, el('button', { class: 'btn ghost', type: 'button', text: 'Fans also like', onclick: () => $('#similar').scrollIntoView({ behavior: 'smooth', block: 'start' }) })))),
    alts.length ? el('div', { class: 'alts' }, el('span', { text: 'Not them?' }), alts.map(x =>
      el('button', { class: 'chip', type: 'button', text: x.name + (who(x) ? ' · ' + who(x) : ''), onclick: () => showArtist(x, all) })),
      all.length > alts.length + 1 ? el('button', { class: 'chip on', type: 'button', text: `See all ${all.length}`, onclick: () => pickArtist(all, [], 'Artists with this name', `${all.length} matches, exact names first`) }) : null) : null);
  if (!a.type && !a.country && !a.disambiguation) api('/api/artist-info/' + a.id).then(i => { if (artistCtx === a) line.firstChild.textContent = who(i); }).catch(() => {});
  fill($('#artistTop'), el('section', { class: 'rail2 popular' }, el('div', { class: 'rhead' }, el('div', { class: 'rtitle' }, el('small', { text: 'Top songs' }), el('h3', { text: 'Popular' }))),
    Array.from({ length: 5 }, () => el('div', { class: 'song sk' }, el('span', { class: 'n' }), el('span', { class: 'cover' }), el('span', { class: 'st2' }, el('b', { text: ' ' }))))));
  fill($('#results'), skeleton()); fill($('#similar'));
  fansAlsoLike(a, 'close', line);
  api(`/api/artist-top/${a.id}?` + new URLSearchParams({ name: a.name })).then(d => {
    if (artistCtx !== a) return;
    artistTop = { a, d };
    if (d.picture) { const im = el('img', { src: d.picture, alt: '', onload: () => { bg.style.backgroundImage = `url("${d.picture}")`; bg.classList.add('ok'); art.classList.add('gone'); } }); im.remove(); }
    if (d.fans) line.firstChild.textContent = [line.firstChild.textContent, fans(d.fans)].filter(Boolean).join(' · ');
    genreTags(line, d.genres); renderTopSongs();
    if (results.length) renderResults();
  }).catch(() => { if (artistCtx === a) fill($('#artistTop')); });
  try {
    const d = await api('/api/artist/' + a.id);
    if (artistCtx === a) rows({ results: d.albums }, a.name);
  } catch (e) { fill($('#results'), el('div', { class: 'msg err', text: e.message })); }
}
let topMore = false;
const albumFor = s => results.find(r => !r.ytm && norm(r.title) === norm(s.album));
function renderTopSongs() {
  const d = artistTop && artistTop.d;
  if (!d || !d.songs.length) return fill($('#artistTop'));
  const top = Math.max(...d.songs.map(s => s.rank)) || 1, list = d.songs.slice(0, topMore ? 15 : 5);
  fill($('#artistTop'), el('section', { class: 'rail2 popular' },
    el('div', { class: 'rhead' }, el('div', { class: 'rtitle' }, el('small', { text: 'Top songs' }), el('h3', { text: 'Popular' }))),
    el('div', { class: 'songs' }, list.map((s, i) => {
      const get = el('button', { class: 'sget' + (s.have ? ' have' : ''), type: 'button', title: s.have ? 'In your folder' : `Download ${s.title} into ${s.album}`, 'aria-label': s.have ? s.title + ' is in your folder' : 'Download ' + s.title, disabled: s.have,
        onclick: ev => { ev.stopPropagation(); const r = albumFor(s); getSongs({ id: r ? r.id : '', artist: r ? r.artist : s.artist, title: r ? r.title : s.album }, [{ title: s.title }], get); } }, pic(s.have ? 'check' : 'download', s.have ? 'k' : 'c'));
      return el('div', { class: 'song', style: `--i:${i}`, role: 'button', tabindex: 0, title: `Open ${s.album}`,
        onclick: () => { const r = albumFor(s); r ? openSongs(r, s.title) : toast(`${s.album} is not in this artist's MusicBrainz releases. Use the download button.`, 'error'); },
        onkeydown: ev => { if (ev.key === 'Enter') ev.currentTarget.click(); } },
        el('span', { class: 'n', text: i + 1 }), cover(s.cover),
        el('span', { class: 'st2' }, el('b', {}, s.title, s.version ? el('em', { text: ' ' + s.version }) : null, s.explicit ? el('i', { class: 'ex', text: 'E' }) : null), el('small', { text: s.album + ([artistCtx.name, ...(d.names || [])].some(n => norm(n) === norm(s.artist)) ? '' : ' · ' + s.artist) })),
        el('span', { class: 'pop', title: 'Popularity' }, el('i', { style: `--v:${Math.max(8, Math.round(100 * s.rank / top))}%` })),
        el('span', { class: 'dur', text: mmss(s.seconds) }), get);
    })),
    d.songs.length > 5 ? el('button', { class: 'btn sm ghost more', type: 'button', text: topMore ? 'Show less' : 'See more', onclick: () => { topMore = !topMore; renderTopSongs(); } }) : null));
}
async function getSongs(g, tracks, btn) {
  if (btn) { btn.disabled = true; fill(btn, el('span', { class: 'spin' })); }
  try {
    const r = await api('/api/songs', { groups: [{ ...g, tracks }] });
    toast(r.songs === 1 ? `Queued ${tracks[0].title} → ${g.title}` : `Queued ${r.songs} songs from ${g.title}`, 'ok', { label: 'Queue', fn: () => show('queue') }); tick();
    if (btn) { btn.classList.add('have'); fill(btn, pic('check', 'k')); }
    return true;
  } catch (e) { toast(e.message, 'error'); if (btn) { btn.disabled = false; fill(btn, pic('download', 'c')); } return false; }
}
async function openSongs(r, focus) {
  const close = () => { bgEl.remove(); document.removeEventListener('keydown', esc); document.body.classList.remove('locked'); };
  const esc = ev => { if (ev.key === 'Escape') close(); };
  const body = el('div', { class: 'sbody' }, Array.from({ length: 8 }, () => el('div', { class: 'trk sk' }, el('span', {}), el('b', { text: ' ' }))));
  const foot = el('div', { class: 'sfoot' });
  const sub = el('p', { text: [r.artist, (r.date || '').slice(0, 4), typeOf(r)].filter(Boolean).join(' · ') });
  const panel = el('div', { class: 'sheet', role: 'dialog', 'aria-modal': 'true', 'aria-label': r.title },
    el('header', { class: 'shead' }, el('img', { class: 'dblur', src: coverUrl('', r.id, r.artist, r.title, 250), alt: '' }), el('span', { class: 'dart' }, cover(coverUrl('', r.id, r.artist, r.title, 500))),
      el('div', { class: 'dcopy' }, el('small', { text: typeOf(r) }), el('h2', { text: r.title }), sub),
      el('button', { class: 'sx', type: 'button', 'aria-label': 'Close', onclick: () => close() }, pic('x'))),
    body, foot);
  const bgEl = el('div', { class: 'sheet-bg', onclick: ev => { if (ev.target === bgEl) close(); } }, panel);
  document.body.append(bgEl); document.body.classList.add('locked'); document.addEventListener('keydown', esc);
  let d;
  try {
    if (!edCache.has(r.id)) edCache.set(r.id, api('/api/group/' + r.id).then(x => x.editions).catch(() => []));
    const eds = await edCache.get(r.id);
    if (!eds.length) throw new Error('MusicBrainz lists no release for this album');
    d = await api('/api/release/' + eds[0].id);
  } catch (e) { return fill(body, el('div', { class: 'msg err', text: e.message })); }
  const want = new Set(focus ? d.tracks.filter(t => !t.have && norm(t.title).startsWith(norm(focus).slice(0, 12))).slice(0, 1).map(t => t.recording) : []);
  const missing = d.tracks.filter(t => !t.have), have = d.tracks.length - missing.length;
  sub.textContent = [d.artist, (d.original_date || d.date || '').slice(0, 4), d.tracks.length + ' songs', dur(d.total_seconds)].filter(Boolean).join(' · ');
  const go = el('button', { class: 'btn go', type: 'button' }), whole = el('button', { class: 'btn ghost', type: 'button' });
  const sync = () => {
    go.disabled = !want.size; fill(go, pic('download', 'k'), want.size ? `Download ${want.size} ${want.size === 1 ? 'song' : 'songs'}` : 'Pick songs');
    whole.disabled = !missing.length; whole.textContent = !missing.length ? 'Album complete' : have ? `Get the rest (${missing.length})` : 'Whole album';
    boxes.forEach(([t, b, row]) => { b.checked = t.have || want.has(t.recording); row.classList.toggle('on', want.has(t.recording)); });
  };
  const multi = d.disc_total > 1, boxes = [];
  fill(body, have ? el('p', { class: 'shave' }, pic('check', 'c'), `${have} of ${d.tracks.length} songs already in ${d.folder.split('/').slice(-2).join('/')}`) : null,
    d.tracks.map(t => {
      const b = el('input', { type: 'checkbox', disabled: t.have, 'aria-label': (t.have ? 'In your folder: ' : 'Pick ') + t.title });
      const row = el('label', { class: 'trk' + (t.have ? ' have' : '') + (want.has(t.recording) ? ' on' : '') }, b,
        el('span', { class: 'tn', text: (multi ? t.disc + '-' : '') + t.number }),
        el('span', { class: 'tt' }, el('b', { text: t.title }), t.artist ? el('small', { text: t.artist }) : null),
        t.have ? el('span', { class: 'tin' }, pic('check', 'c'), 'In folder') : null,
        el('span', { class: 'dur', text: mmss(t.seconds) }));
      b.onchange = () => { b.checked ? want.add(t.recording) : want.delete(t.recording); sync(); };
      boxes.push([t, b, row]);
      return row;
    }));
  const alln = el('button', { class: 'btn sm ghost', type: 'button', text: 'Pick all missing', onclick: () => { const on = missing.every(t => want.has(t.recording)); missing.forEach(t => on ? want.delete(t.recording) : want.add(t.recording)); sync(); } });
  go.onclick = async () => {
    const tracks = d.tracks.filter(t => want.has(t.recording)).map(t => ({ recording: t.recording, title: t.title }));
    if (await getSongs({ id: r.id, artist: d.artist, title: d.title }, tracks)) close();
  };
  whole.onclick = async () => {
    whole.disabled = true;
    try {
      const x = await api('/api/discography', { name: d.artist, groups: [{ id: r.id, title: r.title, artist: r.artist }], skip_owned: false });
      toast(x.duplicates ? 'Already in the queue' : have ? `Adding the ${missing.length} missing songs of ${d.title}` : `Queued ${d.title}`, 'ok', { label: 'Queue', fn: () => show('queue') }); tick(); close();
    } catch (e) { toast(e.message, 'error'); whole.disabled = false; }
  };
  fill(foot, missing.length ? alln : null, el('span', { class: 'acts' }, whole, go));
  sync();
  const first = boxes.find(([t]) => want.has(t.recording)); if (first) first[2].scrollIntoView({ block: 'center' });
}
function resultCard(r, i) {
  const card = el('button', { class: 'card' + (selected.has(r.id) ? ' on' : ''), style: `--i:${Math.min(i, 16)}`, type: 'button', 'aria-pressed': selected.has(r.id), onclick: () => {
    const on = !selected.has(r.id); on ? selected.set(r.id, pick(r)) : selected.delete(r.id);
    document.querySelectorAll(`.card[data-id="${r.id}"]`).forEach(c => { c.classList.toggle('on', on); c.setAttribute('aria-pressed', on); }); drawActions(); } },
    cover(r.ytm ? r.thumb : coverUrl('', r.id, r.artist, r.title, 250)), el('span', { class: 'tick' }, pic('check', 'c')),
    r.owned ? el('span', { class: 'tag' + (r.owned.incomplete ? ' part' : r.owned.picked ? ' some' : ''), text: r.owned.incomplete ? 'Incomplete' : r.owned.picked ? `${r.owned.tracks} of ${r.owned.of} songs` : 'In folder' }) : null,
    el('span', { class: 'meta' }, el('b', { text: r.title, title: r.title }),
      el('small', { text: [artistCtx ? '' : r.artist, r.date.slice(0, 4), typeOf(r)].filter(Boolean).join(' · ') })));
  card.dataset.id = r.id;
  if (r.ytm) return card;
  return el('div', { class: 'cw', style: `--i:${Math.min(i, 16)}` }, card,
    el('span', { class: 'qacts' }, el('button', { class: 'qpill', type: 'button', title: 'See the songs on this album and pick which to download', 'aria-label': 'Songs on ' + r.title, onclick: () => openSongs(r) }, pic('list', 'k'), 'Songs')));
}
function popularReleases() {
  if (!artistTop || artistTop.a !== artistCtx || !artistCtx || title !== artistCtx.name) return null;
  const used = new Set(), list = artistTop.d.albums.map(x => results.find(r => !used.has(r.id) && norm(r.title) === norm(x.title) && used.add(r.id))).filter(Boolean).slice(0, 10);
  return list.length >= 2 ? rail('Popular releases', 'Most loved', list.map(resultCard), { big: true }) : null;
}
function renderResults() {
  const kinds = ['All', ...new Set(results.map(typeOf))], shown = results.filter(r => filter === 'All' || typeOf(r) === filter);
  const allOn = shown.length && shown.every(r => selected.has(r.id));
  const artistPage = artistCtx && title === artistCtx.name;
  fill($('#results'), popularReleases(),
    el('div', { class: 'bar' }, el('h2', { text: artistPage ? 'Discography' : title }), el('span', { class: 'sub', text: shown.length + (shown.length === 1 ? ' release' : ' releases') }),
      el('button', { class: 'btn sm ghost', style: 'margin-left:auto', type: 'button', text: allOn ? 'Deselect all' : 'Select all', onclick: () => {
        shown.forEach(r => allOn ? selected.delete(r.id) : (r.owned && !r.owned.incomplete && !r.owned.picked) || selected.set(r.id, pick(r))); renderResults(); drawActions(); } })),
    kinds.length > 2 ? el('div', { class: 'alts' }, kinds.map(k => el('button', { class: 'chip' + (k === filter ? ' on' : ''), type: 'button', text: k, onclick: () => { filter = k; renderResults(); } }))) : null,
    el('div', { class: 'grid' + (fresh ? ' fresh' : '') }, shown.map((r, i) => resultCard(r, i))),
    el('p', { class: 'foot' }, [results[0] && results[0].ytm ? el('span', { text: 'These come from YouTube Music, not MusicBrainz. Genre, label and date are filled in from Deezer when it has the album. ' }) : el('span', { text: 'Not here? ' }),
      results[0] && results[0].ytm ? null : el('button', { class: 'btn sm ghost', type: 'button', text: 'Search YouTube Music', onclick: () => searchYtm(lastQ) }), ' ',
      el('button', { class: 'btn sm ghost', type: 'button', text: 'Search YouTube uploads', onclick: () => searchUploads(lastQ) })]));
  fresh = false; drawActions();
}
// Discover: recommendations from the library, "more like this album", and "fans also like" on artist pages.
let discData = null, discSeed = null, discShuffle = 0, discToken = 0;
const artImg = x => x.deezer ? `https://api.deezer.com/artist/${x.deezer}/image?size=big` : '/api/artist-img?name=' + encodeURIComponent(x.name);
const fans = n => !n ? '' : n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M fans' : n >= 1e3 ? Math.round(n / 1e3) + 'K fans' : n + ' fans';
const both = names => names.length > 1 ? `${names[0]} & ${names[1]}` : names[0];
function face(x, size = 'md') {
  const h = [...x.name].reduce((t, c) => (t * 31 + c.codePointAt(0)) % 360, 7);
  const box = el('span', { class: 'face ' + size, style: `--h:${h}` }, el('b', { text: (x.name.match(/[\p{L}\p{N}]/u) || ['?'])[0].toUpperCase() }));
  const img = el('img', { alt: '', loading: 'lazy', src: artImg(x) });
  img.onload = () => img.classList.add('ok'); img.onerror = () => img.remove(); box.append(img);
  return box;
}
function bubble(x, i, note) {
  return el('button', { class: 'bub' + (x.owned ? ' own' : ''), style: `--i:${Math.min(i, 14)}`, type: 'button', title: x.owned ? x.name + ' · in your library' : x.name, onclick: () => { show('search'); openSimilar(x); } },
    face(x), x.match ? el('span', { class: 'match', text: x.match + '%' }) : null, x.owned ? el('span', { class: 'own' }, pic('check', 'k')) : null,
    el('b', { text: x.name }), el('small', { text: note || fans(x.fans) }));
}
function albumCard(a, i, big) {
  const dl = el('button', { class: 'qbtn dl', type: 'button', title: 'Download', 'aria-label': 'Download ' + a.title, onclick: () => quickGet(a, dl) }, pic('download', 'k'));
  return el('div', { class: 'dcard' + (big ? ' big' : ''), style: `--i:${Math.min(i, 14)}` },
    el('button', { class: 'dhit', type: 'button', title: `${a.title} by ${a.artist}`, onclick: () => openAlbum(a) },
      cover(a.cover),
      el('span', { class: 'meta' }, el('b', { text: a.title }), el('small', { text: [a.artist, a.year || ''].filter(Boolean).join(' · ') }),
        a.because && a.because.length ? el('em', { text: 'Fans of ' + both(a.because) }) : null)),
    el('span', { class: 'qacts' }, el('button', { class: 'qbtn', type: 'button', title: 'More like this', 'aria-label': 'More like ' + a.title, onclick: () => likeAlbum(a) }, pic('star', 'k')), dl),
    a.type === 'EP' ? el('span', { class: 'tag ep', text: 'EP' }) : null);
}
// Rails scroll sideways with a trackpad, Shift+wheel, the arrow buttons, the keyboard, or by dragging with a mouse.
function dragScroll(track) {
  let id = null, x0 = 0, s0 = 0, moved = false;
  track.addEventListener('pointerdown', ev => { if (ev.pointerType !== 'mouse' || ev.button !== 0 || track.classList.contains('all')) return; id = ev.pointerId; x0 = ev.clientX; s0 = track.scrollLeft; moved = false; });
  track.addEventListener('pointermove', ev => {
    if (ev.pointerId !== id) return;
    const dx = ev.clientX - x0;
    if (!moved && Math.abs(dx) > 6) { moved = true; track.setPointerCapture(id); track.classList.add('dragging'); }
    if (moved) track.scrollLeft = s0 - dx;
  });
  const end = () => { if (id == null) return; id = null; track.classList.remove('dragging'); };
  track.addEventListener('pointerup', end); track.addEventListener('pointercancel', end);
  track.addEventListener('click', ev => { if (moved) { ev.preventDefault(); ev.stopPropagation(); moved = false; } }, true);
  track.addEventListener('dragstart', ev => ev.preventDefault());
}
function railTools(track, count) {
  track.tabIndex = 0; dragScroll(track);
  const step = d => track.scrollBy({ left: d * track.clientWidth * 0.85, behavior: 'smooth' });
  const left = el('button', { class: 'rarrow', type: 'button', 'aria-label': 'Scroll left', onclick: () => step(-1) }, '‹');
  const right = el('button', { class: 'rarrow', type: 'button', 'aria-label': 'Scroll right', onclick: () => step(1) }, '›');
  const nav = el('span', { class: 'rnav' }, left, right);
  const ends = () => { left.disabled = track.scrollLeft < 4; right.disabled = track.scrollLeft + track.clientWidth >= track.scrollWidth - 4; nav.hidden = track.classList.contains('all') || (left.disabled && right.disabled); };
  track.addEventListener('scroll', ends, { passive: true }); requestAnimationFrame(ends); addEventListener('resize', ends);
  const all = count > 4 ? el('button', { class: 'btn sm ghost seeall', type: 'button', text: 'See all', onclick: () => {
    const on = track.classList.toggle('all'); all.textContent = on ? 'Show less' : 'See all'; track.scrollLeft = 0; ends();
    if (!on) track.closest('section').scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } }) : null;
  return el('span', { class: 'rtools' }, nav, all);
}
function rail(head, sub, items, opts = {}) {
  if (!items.length) return null;
  const track = el('div', { class: 'rtrack' + (opts.big ? ' big' : '') + (opts.round ? ' round' : '') }, items);
  return el('section', { class: 'rail2' },
    el('div', { class: 'rhead' }, opts.lead || null, el('div', { class: 'rtitle' }, sub ? el('small', { text: sub }) : null, el('h3', { text: head })), railTools(track, items.length)),
    track);
}
const railSk = (n = 6, round) => el('section', { class: 'rail2 sk-rail' }, el('div', { class: 'rhead' }, el('div', { class: 'rtitle' }, el('small', { text: ' ' }), el('h3', { text: ' ' }))),
  el('div', { class: 'rtrack' + (round ? ' round' : '') }, Array.from({ length: n }, () => round ? el('span', { class: 'bub sk' }, el('span', { class: 'face md' }), el('b', { text: ' ' })) : el('div', { class: 'dcard sk' }, el('span', { class: 'cover' }), el('span', { class: 'meta' }, el('b', { text: ' ' }), el('small', { text: ' ' }))))));

// The music map: every library artist placed by genre, with genre families as coloured blobs that blend where they meet.
// A circle (the lens) picks the part of the library that Discover works from.
let discMap = null, discArea = null, lens = store.get('lens', null), lensOn = store.get('lensOn', true);
const MW = 1000, MH = 700, SVGNS = 'http://www.w3.org/2000/svg';
const svgEl = (tag, attrs, ...kids) => { const e = document.createElementNS(SVGNS, tag); for (const [k, v] of Object.entries(attrs || {})) if (v != null) e.setAttribute(k, v); e.append(...kids.flat().filter(x => x != null)); return e; };
const hsl = (h, l, a, sat = 85) => `hsla(${h} ${sat}% ${l}% / ${a})`;
const famHue = k => k === 'unknown' ? 205 : ((discMap.anchors.find(a => a.key === k) || {}).hue ?? 205);
const famSat = k => k === 'unknown' ? 12 : 85;
const inLens = a => lens && Math.hypot((a.x - lens.x) * MW, (a.y - lens.y) * MH) <= lens.r * MW;
function areaName(area) {
  if (!area) return 'your whole library';
  const c = {}; area.forEach(a => { c[a.family] = (c[a.family] || 0) + 1; });
  const top = Object.entries(c).sort((x, y) => y[1] - x[1]).map(([k]) => (discMap.families.find(f => f.key === k) || {}).label).filter(Boolean);
  return top.slice(0, 2).join(' & ') + (top.length > 2 ? ` +${top.length - 2}` : '');
}
function buildDiscover() {
  fill($('#disc'), el('div', { id: 'dhero' }, discHero(null)),
    el('section', { id: 'dmap', class: 'mapbox' }, el('div', { class: 'mapsk' }, el('span', { class: 'spin' }), 'Charting your library…')),
    el('div', { id: 'dfeed' }));
}
async function loadDiscover(shuffle) {
  discSeed = null;
  if (shuffle) discShuffle = 1 + Math.floor(Math.random() * 1e6);
  if (!$('#dmap')) buildDiscover();
  if (!discMap) { await loadMap(0); if (!discMap) return; } else if (!$('#dmap svg')) drawMap();
  if (!discMap.artists.length) return;
  loadFeed();
}
async function loadMap(round) {
  let m;
  try { m = await api('/api/discover/map'); } catch (e) { return fill($('#dmap'), el('div', { class: 'msg err', text: e.message })); }
  const first = !discMap; discMap = m;
  if (!m.artists.length) return fill($('#disc'), discHero(null), el('div', { class: 'dempty' }, el('span', { class: 'pi star c' }), el('b', { text: 'Nothing to go on yet' }),
    el('p', { text: 'Download a few albums you love, and this page maps them by genre and finds more like them.' }), el('button', { class: 'btn go', type: 'button', text: 'Search for music', onclick: () => { show('search'); $('#q').focus(); } })));
  if (first && !lens) { const f = m.families.find(f => f.key !== 'unknown') || m.families[0]; lens = fitFamily(f); }
  if ($('#dmap')) drawMap();
  if (m.pending && round < 4) setTimeout(() => { if (discMap === m) loadMap(round + 1); }, 6000);
}
function fitFamily(f) {
  const own = discMap.artists.filter(a => a.family === f.key);
  const d = own.map(a => Math.hypot((a.x - f.x) * MW, (a.y - f.y) * MH) / MW).sort((x, y) => x - y);
  const core = d.length ? d[Math.min(d.length - 1, Math.floor(d.length * 0.7))] : 0;
  return { x: f.x, y: f.y, r: Math.min(0.2, Math.max(0.06, core + 0.03)) };
}
function drawMap() {
  const m = discMap, box = $('#dmap');
  const keys = [...new Set(m.artists.map(a => a.family))];
  const defs = svgEl('defs', {}, keys.map(k => svgEl('radialGradient', { id: 'g-' + k },
    svgEl('stop', { offset: '0', 'stop-color': hsl(famHue(k), 62, .85, famSat(k)) }), svgEl('stop', { offset: '.5', 'stop-color': hsl(famHue(k), 55, .32, famSat(k)) }),
    svgEl('stop', { offset: '1', 'stop-color': hsl(famHue(k), 50, 0, famSat(k)) }))),
    svgEl('filter', { id: 'soft', x: '-20%', y: '-20%', width: '140%', height: '140%' }, svgEl('feGaussianBlur', { stdDeviation: '14' })));
  const used = new Set(keys);
  const guide = svgEl('g', { class: 'mguide' }, svgEl('ellipse', { cx: MW / 2, cy: MH / 2, rx: MW * 0.37, ry: MH * 0.37 }),
    m.anchors.filter(a => !used.has(a.key)).map(a => svgEl('text', { x: a.x * MW, y: a.y * MH, class: 'mghost' }, a.label)));
  const blobs = svgEl('g', { class: 'mblobs', filter: 'url(#soft)' },
    m.families.map(f => svgEl('circle', { cx: f.x * MW, cy: f.y * MH, r: 60 + 42 * Math.sqrt(f.artists), fill: `url(#g-${f.key})`, opacity: .9 })),
    m.artists.slice(0, 400).map(a => svgEl('circle', { cx: a.x * MW, cy: a.y * MH, r: 44 + 8 * Math.sqrt(a.albums), fill: `url(#g-${a.family})`, opacity: .6 })));
  // Labels: genre families first (nudged up or down until they don't overlap), then genre tags and artist names wherever they still fit.
  const boxes = [], hits = b => boxes.some(o => b.x1 < o.x2 && b.x2 > o.x1 && b.y1 < o.y2 && b.y2 > o.y1);
  const place = (text, x, y, size, tries) => {
    const w = text.length * size * 0.62, h = size * 1.1, cx = Math.min(MW - w / 2 - 10, Math.max(w / 2 + 10, x));
    for (const dy of tries) {
      const cy = Math.min(MH - 14, Math.max(size + 6, y + dy)), b = { x1: cx - w / 2, x2: cx + w / 2, y1: cy - h * .8, y2: cy + h * .25 };
      if (!hits(b)) { boxes.push(b); return { x: cx, y: cy }; }
    }
    return null;
  };
  const fs = box.clientWidth && box.clientWidth < 600 ? 40 : 25, step = fs * 1.2;
  const famAt = m.families.map(f => [f, place(f.label.toUpperCase(), f.x * MW, f.y * MH + 8, fs, [0, -step, step, -2 * step, 2 * step, -3 * step, 3 * step])]);
  const tags = svgEl('g', { class: 'mtags' }, m.tags.map(t => { const p = place(t.name, t.x * MW, t.y * MH + 30, 16, [0, 18, -18]); return p && svgEl('text', { x: p.x, y: p.y }, t.name); }));
  const dots = m.artists.map(a => svgEl('circle', { class: 'mdot', cx: a.x * MW, cy: a.y * MH, r: 4.5 + 2 * Math.sqrt(a.albums), fill: hsl(famHue(a.family), 78, 1, famSat(a.family)) },
    svgEl('title', {}, `${a.name} · ${a.albums} ${a.albums === 1 ? 'album' : 'albums'}${a.genres.length ? ' · ' + a.genres.slice(0, 3).join(', ') : ''}`)));
  const names = svgEl('g', { class: 'mnames' }, m.artists.slice(0, 24).map(a => { const p = place(a.name, a.x * MW, a.y * MH - 12 - 2 * Math.sqrt(a.albums), 14, [0, 34]); return p && svgEl('text', { x: p.x, y: p.y }, a.name); }));
  const labels = svgEl('g', { class: 'mlabels' }, famAt.map(([f, p]) => p && svgEl('text', { x: p.x, y: p.y, style: `font-size:${fs}px` }, f.label)));
  const ring = svgEl('circle', { class: 'lring' }), hub = svgEl('circle', { class: 'lhub', r: 7 });
  const lensG = svgEl('g', { class: 'mlens' }, ring, hub);
  const svg = svgEl('svg', { viewBox: `0 0 ${MW} ${MH}`, class: 'mapsvg', role: 'img', 'aria-label': 'Map of your library by genre. Click or drag to choose an area.' },
    defs, guide, blobs, tags, svgEl('g', { class: 'mdots' }, dots), names, labels, lensG);
  const n = el('b'), meta = el('small'), faces = el('span', { class: 'afaces' });
  const focus = el('input', { type: 'range', min: 5, max: 45, value: Math.round((lens ? lens.r : 0.16) * 100), 'aria-label': 'Area size' });
  const whole = el('button', { class: 'chip' + (lensOn ? '' : ' on'), type: 'button', text: 'Whole library' });
  const chips = el('div', { class: 'alts mchips' }, whole, m.families.map(f => el('button', { class: 'chip fam', type: 'button', style: `--h:${famHue(f.key)};--s:${famSat(f.key)}%`, onclick: () => { lens = fitFamily(f); lensOn = true; focus.value = Math.round(lens.r * 100); paint(); commit(); } },
    el('i'), `${f.label} · ${f.artists}`)));
  const paint = () => {
    const on = lensOn && lens;
    lensG.style.display = on ? '' : 'none'; svg.classList.toggle('lensing', !!on); whole.classList.toggle('on', !on);
    if (on) { ring.setAttribute('cx', lens.x * MW); ring.setAttribute('cy', lens.y * MH); ring.setAttribute('r', lens.r * MW); hub.setAttribute('cx', lens.x * MW); hub.setAttribute('cy', lens.y * MH); }
    const area = on ? m.artists.filter(inLens) : m.artists;
    dots.forEach((d, i) => d.classList.toggle('in', !!on && inLens(m.artists[i])));
    n.textContent = on ? (area.length ? areaName(area) : 'Empty space') : 'Your whole library';
    meta.textContent = on && !area.length ? 'Drag the circle onto some dots' : `${area.length} ${area.length === 1 ? 'artist' : 'artists'} · ${area.reduce((t, a) => t + a.albums, 0)} albums` +
      (on ? ' · ' + [...new Set(area.flatMap(a => a.genres.slice(0, 2)))].slice(0, 4).join(', ') : '');
    fill(faces, area.slice(0, 6).map(a => face({ name: a.name }, 'sm')));
  };
  const commit = () => { store.set('lens', lens); store.set('lensOn', lensOn); loadFeed(); };
  const toUnit = ev => { const r = svg.getBoundingClientRect(); return { x: Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width)), y: Math.min(1, Math.max(0, (ev.clientY - r.top) / r.height)) }; };
  let drag = false, from = null;
  svg.addEventListener('pointerdown', ev => {
    if (ev.button !== 0) return;
    const p = toUnit(ev); from = lens && lensOn ? { dx: p.x - lens.x, dy: p.y - lens.y, inside: Math.hypot((p.x - lens.x) * MW, (p.y - lens.y) * MH) <= lens.r * MW } : null;
    if (!(from && from.inside)) { lens = { r: lens ? lens.r : 0.16, x: p.x, y: p.y }; from = { dx: 0, dy: 0 }; }
    lensOn = true; paint();
    if (ev.pointerType === 'touch') return commit();
    drag = true; svg.setPointerCapture(ev.pointerId); svg.classList.add('grabbing');
  });
  svg.addEventListener('pointermove', ev => { if (!drag) return; const p = toUnit(ev); lens.x = p.x - from.dx; lens.y = p.y - from.dy; paint(); });
  const done = () => { if (!drag) return; drag = false; svg.classList.remove('grabbing'); commit(); };
  svg.addEventListener('pointerup', done); svg.addEventListener('pointercancel', done);
  focus.oninput = () => { if (!lens) lens = { x: .5, y: .5, r: .16 }; lens.r = focus.value / 100; lensOn = true; paint(); };
  focus.onchange = commit;
  whole.onclick = () => { lensOn = false; paint(); commit(); };
  fill(box, el('div', { class: 'mhead' }, el('div', { class: 'rtitle' }, el('small', { text: 'Your music map' }), el('h3', { text: 'Pick an area to explore' })),
      m.pending ? el('span', { class: 'mpend' }, el('span', { class: 'spin' }), `Placing ${m.pending} more`) : null),
    el('div', { class: 'mapwrap' }, svg, el('span', { class: 'mhint', text: 'Click or drag the circle' })),
    el('div', { class: 'areabar' }, faces, el('span', { class: 'atext' }, n, meta), el('label', { class: 'focus' }, el('small', { text: 'Narrow' }), focus, el('small', { text: 'Wide' }))),
    chips);
  paint();
}
async function loadFeed() {
  const tok = ++discToken;
  discArea = lensOn && lens && discMap ? discMap.artists.filter(inLens) : null;
  if (discArea && !discArea.length) { fill($('#dhero'), discHero(null)); return fill($('#dfeed'), el('div', { class: 'msg', text: 'No artists inside the circle. Drag it onto some dots, or pick a genre below the map.' })); }
  fill($('#dhero'), discHero(null)); fill($('#dfeed'), railSk(6), railSk(7, true), railSk(6));
  try {
    const d = discArea ? await api('/api/discover/feed', { shuffle: discShuffle, artists: discArea.map(a => a.name) }) : await api('/api/discover/feed?shuffle=' + discShuffle);
    if (tok !== discToken) return;
    discData = d; renderDiscover();
  } catch (e) { if (tok === discToken) fill($('#dfeed'), el('div', { class: 'msg err', text: e.message })); }
}
function discHero(d) {
  const shuffle = el('button', { class: 'btn sm ghost', type: 'button', onclick: () => loadDiscover(true) }, pic('retry'), 'Shuffle');
  const covers = d ? d.picks.slice(0, 5).map(a => a.cover) : [];
  const where = discMap ? areaName(discArea) : 'your library';
  return el('header', { class: 'dhero' },
    el('div', { class: 'dstack' }, covers.map((c, i) => el('img', { src: c, alt: '', style: `--k:${i}`, onload: ev => ev.target.classList.add('ok') }))),
    el('div', { class: 'dcopy' }, el('small', { text: discArea ? 'Exploring' : 'Made for you' }), el('h2', { text: discArea ? where : 'Discover' }),
      el('p', { text: d && d.seeds.length ? `Built from ${d.seeds.slice(0, 3).join(', ')}${d.seeds.length > 3 ? ` and ${d.seeds.length - 3} more` : ''}${discArea ? ' in this part of your library' : ' in your library'}.` : discArea ? 'Finding albums and artists near this part of your library…' : 'Albums and artists picked from what you already have.' }),
      el('div', { class: 'dbtns' }, shuffle)));
}
function renderDiscover() {
  const d = discData;
  if (!$('#dfeed')) { buildDiscover(); if (discMap) drawMap(); }
  fill($('#dhero'), discHero(d));
  if (!d.seeds.length) return fill($('#dfeed'), el('div', { class: 'msg', text: 'Nothing to recommend from here yet.' }));
  fill($('#dfeed'),
    rail('Picked for you', discArea ? 'Fresh albums from ' + areaName(discArea) : 'Fresh albums', d.picks.map((a, i) => albumCard(a, i, true)), { big: true }),
    d.rails.map(r => rail(r.seed, 'Because you have', r.albums.map(albumCard), { lead: face({ name: r.seed }, 'sm') })),
    rail('Artists you might like', 'Your next favourites', d.artists.map((x, i) => bubble(x, i, x.because ? 'Like ' + both(x.because) : '')), { round: true }),
    rail('Under the radar', 'Deeper cuts', d.radar.map((x, i) => bubble(x, i)), { round: true, lead: el('span', { class: 'rbadge' }, pic('bolt', 'k')) }),
    rail('Missing from your shelf', 'More from artists you have', d.gaps.map(albumCard)),
    el('p', { class: 'foot', text: 'Recommendations come from Deezer, ListenBrainz and Last.fm listening data. The map uses the genres in your files.' }));
}
async function likeAlbum(a) {
  discSeed = { artist: a.artist, title: a.title, cover: a.cover }; const tok = ++discToken;
  show('discover');
  const back = el('button', { class: 'btn sm ghost', type: 'button', text: '‹ Discover', onclick: () => { discSeed = null; buildDiscover(); if (discMap) drawMap(); discData ? renderDiscover() : loadDiscover(); } });
  const hero = s => el('header', { class: 'dhero seed' }, s.cover ? el('img', { class: 'dblur', src: s.cover, alt: '' }) : null,
    el('span', { class: 'dart' }, s.cover ? el('img', { src: s.cover, alt: '', onload: ev => ev.target.classList.add('ok') }) : null),
    el('div', { class: 'dcopy' }, el('small', { text: 'More like' }), el('h2', { text: s.title }),
      el('p', { text: [s.artist, s.year || '', (s.genres || []).join(', '), s.label].filter(Boolean).join(' · ') }), el('div', { class: 'dbtns' }, back)));
  fill($('#disc'), hero(discSeed), el('div', { class: 'grid dgrid' }, Array.from({ length: 12 }, () => el('div', { class: 'dcard sk' }, el('span', { class: 'cover' }), el('span', { class: 'meta' }, el('b', { text: ' ' }), el('small', { text: ' ' }))))));
  try {
    const d = await api('/api/discover/album?' + new URLSearchParams({ artist: a.artist, album: a.title }));
    if (tok !== discToken) return;
    const s = Object.assign({}, discSeed, d.seed, { cover: d.seed.cover || discSeed.cover });
    fill($('#disc'), hero(s),
      d.albums.length ? el('div', { class: 'grid dgrid fresh' }, d.albums.map(albumCard)) : el('div', { class: 'msg', text: `No recommendations for ${a.title} yet. Deezer may not know this album.` }),
      rail('Artists in the same orbit', 'Fans also like', d.artists.map((x, i) => bubble(x, i, x.because && x.because.length > 1 ? 'Like ' + both(x.because) : '')), { round: true }));
  } catch (e) { if (tok === discToken) fill($('#disc'), hero(discSeed), el('div', { class: 'msg err', text: e.message })); }
}
function openAlbum(a) {
  show('search'); const q = `${a.artist} - ${a.title}`; $('#q').value = q; lastQ = q.replace(/\s+[-–—]\s+/g, ' ');
  selected.clear(); artistCtx = null; drawActions(); fill($('#artistBar')); fill($('#artistTop')); fill($('#similar')); fill($('#results'), skeleton());
  search(q).catch(e => fill($('#results'), el('div', { class: 'msg err', text: e.message })));
}
async function quickGet(a, btn) {
  btn.disabled = true; fill(btn, el('span', { class: 'spin' }));
  try {
    const { results: rs } = await api('/api/search?' + new URLSearchParams({ artist: a.artist, album: a.title }));
    const t = norm(a.title), same = r => norm(r.title) === t, near = r => norm(r.title).startsWith(t) || t.startsWith(norm(r.title));
    const best = rs.find(r => same(r) && !r.secondary.length) || rs.find(same) || rs.find(r => near(r) && !r.secondary.length);
    if (!best) { toast(`${a.title} isn't on MusicBrainz under that name`, 'error', { label: 'Search', fn: () => openAlbum(a) }); fill(btn, pic('download', 'k')); btn.disabled = false; return; }
    const r = await api('/api/discography', { name: best.artist, groups: [pick(best)], skip_owned: false });
    toast(r.duplicates ? 'Already in the queue' : `Queued ${best.title}`, 'ok', { label: 'Queue', fn: () => show('queue') }); tick();
    btn.classList.add('done'); fill(btn, pic('check', 'k'));
  } catch (e) { toast(e.message, 'error'); fill(btn, pic('download', 'k')); btn.disabled = false; }
}
function fansAlsoLike(a, mode, line) {
  const tok = artistCtx;
  const seg = el('div', { class: 'seg' }, [['close', 'Closest'], ['deep', 'Deeper cuts']].map(([m, t]) =>
    el('button', { class: m === mode ? 'on' : '', type: 'button', text: t, onclick: () => m !== mode && fansAlsoLike(a, m, line) })));
  const head = el('div', { class: 'rhead' }, el('div', { class: 'rtitle' }, el('small', { text: mode === 'deep' ? 'Less known, same orbit' : 'Fans also like' }), el('h3', { text: mode === 'deep' ? 'Deeper cuts' : 'Similar artists' })), seg);
  fill($('#similar'), el('section', { class: 'rail2 simbox' }, head, el('div', { class: 'rtrack round' }, Array.from({ length: 7 }, () => el('span', { class: 'bub sk' }, el('span', { class: 'face md' }), el('b', { text: ' ' }))))));
  api(`/api/similar/${a.id}?` + new URLSearchParams({ name: a.name, mode })).then(({ artists, genres }) => {
    if (artistCtx !== tok) return;
    if (genres && genres.length && line && !line.dataset.g) { line.dataset.g = 1; line.append(el('span', { class: 'genres' }, genres.map(g => el('i', { text: g })))); }
    if (!artists.length) return fill($('#similar'), mode === 'deep' ? el('section', { class: 'rail2 simbox' }, head, el('p', { class: 'sub', text: 'No deeper cuts found for this artist.' })) : null);
    const track = el('div', { class: 'rtrack round' }, artists.map((x, i) => bubble(x, i, x.via ? 'Via ' + both(x.via) : x.owned ? 'In your library' : fans(x.fans))));
    head.append(railTools(track, artists.length));
    fill($('#similar'), el('section', { class: 'rail2 simbox' }, head, track));
  }).catch(() => { if (artistCtx === tok) fill($('#similar')); });
}
$('#likeBtn').addEventListener('click', () => {
  const one = view === 'library' ? [...libSel.values()][0] : [...selected.values()][0];
  if (one) likeAlbum({ artist: one.artist, title: one.title, cover: view === 'library' ? '/api/library/art?' + new URLSearchParams({ artist: one.artist, album: one.title }) : coverUrl('', one.id, one.artist, one.title, 500) });
});
const libSel = new Map(), lkey = a => a.artist + '\u0000' + a.title;
const edCache = new Map();
const edKey = e => [e.title, e.tracks, e.disambiguation, (e.date || '').slice(0, 4)].join('|').toLowerCase();
const edLabel = e => [e.title, e.disambiguation, e.tracks + ' tracks', (e.date || '').slice(0, 4), e.format].filter(Boolean).join(' · ');
async function drawEditions() {
  const row = $('#edRow'), one = selected.size === 1 && view === 'search' ? [...selected.values()][0] : null;
  const hide = () => { row.hidden = true; $('#actions').classList.remove('tall'); };
  if (!one || one.ytm) return hide();
  if (!edCache.has(one.id)) edCache.set(one.id, api('/api/group/' + one.id).then(d => d.editions).catch(() => []));
  const eds = await edCache.get(one.id);
  if (selected.size !== 1 || !selected.has(one.id)) return;
  const seen = new Set(eds[0] ? [edKey(eds[0])] : []), more = eds.slice(1).filter(e => !seen.has(edKey(e)) && seen.add(edKey(e))).slice(0, 15);
  if (!more.length) return hide();
  fill($('#edSel'), el('option', { value: '', text: 'Standard · ' + edLabel(eds[0]) }), more.map(e => el('option', { value: e.id, text: edLabel(e), selected: e.id === one.release })));
  $('#edSel').onchange = ev => { one.release = ev.target.value || undefined; };
  row.hidden = false; $('#actions').classList.add('tall');
}
let full = false;
function drawActions() {
  const lib_ = view === 'library', n = lib_ ? libSel.size : selected.size;
  $('#actions').hidden = !n || (view !== 'search' && !lib_);
  drawEditions();
  $('#editBtn').hidden = !(lib_ && n === 1);
  $('#expBtn').hidden = !lib_;
  $('#likeBtn').hidden = !(n === 1 && (lib_ || ![...selected.values()][0].ytm));
  $('#linkBtn').hidden = !(lib_ && n === 1 && [...libSel.values()][0].missing.length);
  $('#dlBtn').hidden = lib_; $('#retryBtn').hidden = !(lib_ && [...libSel.values()].some(a => a.incomplete));
  if (lib_) {
    $('#actionText').textContent = `${n === 1 ? '1 album' : n + ' albums'} · ${bytes([...libSel.values()].reduce((t, a) => t + a.size, 0))}`;
    $('#delBtn').hidden = false; return;
  }
  const have = [...selected.keys()].filter(id => (results.find(r => r.id === id) || {}).owned).length, own = n && have === n;
  full = own && [...selected.keys()].every(id => { const o = (results.find(r => r.id === id) || {}).owned; return o && !o.incomplete && !o.picked; });
  $('#actionText').textContent = (n === 1 ? '1 album' : n + ' albums') + (have && !own ? ` · ${have} in folder` : '');
  $('#dlLabel').textContent = full ? 'Download again' : have ? 'Add missing songs' : 'Download';
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
    const r = groups[0].ytm
      ? await api('/api/ytm/download', { albums: groups.map(g => g.id) }).then(d => ({ duplicates: d.duplicates }))
      : await api('/api/discography', { name: artistCtx ? artistCtx.name : groups[0].artist, groups, skip_owned: false, replace: full });
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
  const fresh = new Map(lib.map(a => [lkey(a), a])); [...libSel.keys()].forEach(k => fresh.has(k) ? libSel.set(k, fresh.get(k)) : libSel.delete(k));
  renderLibrary(); drawActions();
}
function renderLibrary() {
  const q = $('#libFilter').value.trim().toLowerCase(), inc = lib.filter(a => a.incomplete).length, bare = lib.filter(a => !a.cover).length;
  const noLyr = lib.filter(a => a.lyrics < a.tracks).length, bareData = lib.filter(a => a.gaps.length).length, modes = [['All', lib.length], ['Recent', 0], ['Incomplete', inc], ['Data incomplete', bareData], ['No cover', bare], ['No lyrics', noLyr]];
  fill($('#libChips'), modes.map(([m, c]) => el('button', { class: 'chip' + (m === libMode ? ' on' : ''), type: 'button', text: c ? `${m} · ${c}` : m, onclick: () => { libMode = m; renderLibrary(); } })));
  let rows = lib.filter(a => (!q || (a.artist + ' ' + a.title).toLowerCase().includes(q)) && ({ All: 1, Recent: 1, Incomplete: a.incomplete, 'Data incomplete': a.gaps.length, 'No cover': !a.cover, 'No lyrics': a.lyrics < a.tracks }[libMode]));
  if (libMode === 'Recent') rows = rows.slice().sort((a, b) => b.mtime - a.mtime).slice(0, 40);
  $('#libSum').textContent = lib.length ? `${rows.length} albums · ${rows.reduce((t, a) => t + a.tracks, 0)} tracks · ${bytes(rows.reduce((t, a) => t + a.size, 0))}` : '';
  const row = a => {
    const r = el('button', { class: 'lrow' + (libSel.has(lkey(a)) ? ' on' : ''), type: 'button', 'aria-pressed': libSel.has(lkey(a)), onclick: () => {
      const on = !libSel.has(lkey(a)); on ? libSel.set(lkey(a), a) : libSel.delete(lkey(a)); r.classList.toggle('on', on); r.setAttribute('aria-pressed', on); drawActions(); } },
      a.cover ? cover('/api/library/art?' + new URLSearchParams({ artist: a.artist, album: a.title })) : el('span', { class: 'cover' }),
      el('span', { class: 'jtext' }, el('b', { text: a.title, title: a.title }),
        el('small', { text: [libMode === 'All' ? '' : a.artist, a.tracks + (a.tracks === 1 ? ' track' : ' tracks'), bytes(a.size), a.mtime ? ago(a.mtime) : ''].filter(Boolean).join(' · ') }),
        a.picked ? el('span', { class: 'st run', text: `${a.tracks} of ${a.of} songs · open the album in Search to get the rest` }) : null,
        a.incomplete ? el('span', { class: 'st part', text: a.missing.length ? `Missing ${a.missing.length}: ${a.missing.join(', ')}` : 'Incomplete' }) : null,
        a.gaps.length ? el('span', { class: 'st part', text: 'Data incomplete: no ' + a.gaps.join(', no ') }) : null,
        !a.cover ? el('span', { class: 'st wait', text: 'No cover' }) : null,
        libMode === 'No lyrics' && a.lyrics < a.tracks ? el('span', { class: 'st wait', text: a.lyrics ? `Lyrics ${a.lyrics}/${a.tracks}` : 'No lyrics' }) : null),
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
$('#linkBtn').addEventListener('click', () => { const a = [...libSel.values()][0]; if (a) openLinker(a); });
function openLinker(a) {
  const host = $('#libEdit');
  const rows = a.missing.map(name => {
    const url = el('input', { type: 'text', placeholder: 'Paste the YouTube link for this song', 'aria-label': 'YouTube link for ' + name });
    const go = el('button', { class: 'btn sm go', type: 'button', text: 'Download', onclick: async () => {
      go.disabled = true;
      try {
        await api('/api/library/link', { artist: a.artist, album: a.title, file: name, url: url.value });
        go.textContent = 'Queued'; url.disabled = true; toast('Queued ' + name, 'ok', { label: 'Queue', fn: () => show('queue') }); tick();
      } catch (e) { toast(e.message, 'error'); go.disabled = false; }
    } });
    return el('div', { class: 'linkrow' }, el('b', { text: name }), el('div', { class: 'row', style: 'display:flex;gap:8px' }, url, go));
  });
  fill(host, el('div', { class: 'edit' }, el('div', { class: 'bar' }, el('h2', { text: 'Link missing tracks' }), el('span', { class: 'sub', text: `${a.artist} - ${a.title}` })),
    el('p', { class: 'sub', text: 'Paste a link to the song on YouTube. Only that track is downloaded and tagged, and the rest of the album is left alone. Or use Retry to search again.' }),
    rows, el('div', { style: 'margin-top:12px' }, el('button', { class: 'btn ghost', type: 'button', text: 'Close', onclick: () => { host.hidden = true; } }))));
  host.hidden = false; host.scrollIntoView({ block: 'start', behavior: 'smooth' });
}
async function openLinkerFor(job) {
  await loadLibrary();
  const a = lib.find(x => norm(x.artist) === norm(job.artist) && norm(x.title) === norm(job.title)) || lib.find(x => norm(x.title) === norm(job.title) && x.incomplete);
  if (!a || !a.missing.length) return toast('That album is no longer listed as incomplete', 'error');
  openLinker(a);
}
$('#scanBtn').addEventListener('click', ev => { ev.stopPropagation(); const m = $('#scanMenu'); m.hidden = !m.hidden; $('#scanBtn').setAttribute('aria-expanded', String(!m.hidden)); });
document.addEventListener('click', () => { $('#scanMenu').hidden = true; $('#scanBtn').setAttribute('aria-expanded', 'false'); });
$('#lyrAllBtn').addEventListener('click', async () => {
  try { await api('/api/library/lyrics/find-all', {}); toast('Looking for lyrics. Progress is in the Log.', 'ok', { label: 'Log', fn: () => show('log') }); } catch (e) { toast(e.message, 'error'); }
});
async function openLyrics(a) {
  const host = $('#libEdit'), q = { artist: a.artist, album: a.title }, label = { synced: 'Synced', plain: 'Plain', none: 'None' }, paints = {};
  let d;
  try { d = await api('/api/library/lyrics?' + new URLSearchParams(q)); } catch (e) { return toast(e.message, 'error'); }
  const send = (file, body) => fetch('/api/library/lyrics/save?' + new URLSearchParams({ ...q, file }), { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body })
    .then(async r => { const j = await r.json().catch(() => ({})); if (!r.ok || j.error) throw new Error(j.error || 'HTTP ' + r.status); paints[file](j.state); toast('Lyrics saved', 'ok'); loadLibrary(); });
  const rows = d.tracks.map(t => {
    const st = el('span', { class: 'st' });
    paints[t.file] = s => { st.className = 'st ' + (s === 'none' ? 'wait' : 'done'); st.textContent = label[s]; };
    paints[t.file](t.state);
    const find = el('button', { class: 'btn sm ghost', type: 'button', text: 'Find', onclick: async () => {
      find.disabled = true;
      try { const s = (await api('/api/library/lyrics/find', { ...q, files: [t.file], overwrite: true })).states[t.file]; paints[t.file](s); toast(s === 'none' ? 'No lyrics found online' : 'Lyrics saved', s === 'none' ? 'error' : 'ok'); loadLibrary(); } catch (e) { toast(e.message, 'error'); }
      find.disabled = false;
    } });
    const fileIn = el('input', { type: 'file', accept: '.lrc,.txt,text/plain', hidden: true, onchange: () => { const f = fileIn.files[0]; if (f) send(t.file, f).catch(e => toast(e.message, 'error')).then(() => { fileIn.value = ''; }); } });
    const area = el('textarea', { class: 'lyric-area', rows: 6, hidden: true, placeholder: 'Paste the lyrics here (plain text or .lrc)', 'aria-label': 'Lyrics for ' + t.title });
    const save = el('button', { class: 'btn sm go', type: 'button', text: 'Save', hidden: true, onclick: () => send(t.file, area.value).then(() => { area.hidden = true; save.hidden = true; area.value = ''; }).catch(e => toast(e.message, 'error')) });
    return el('div', { class: 'lyrow' }, el('div', { class: 'lyhead' }, el('b', { text: t.title }), st),
      el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' }, find, el('button', { class: 'btn sm ghost', type: 'button', text: 'File', onclick: () => fileIn.click() }), fileIn,
        el('button', { class: 'btn sm ghost', type: 'button', text: 'Paste', onclick: () => { area.hidden = !area.hidden; save.hidden = area.hidden; } })), area, save);
  });
  const all = el('button', { class: 'btn sm go', type: 'button', text: 'Find missing', style: 'margin-left:auto', onclick: async () => {
    all.disabled = true;
    try { const r = await api('/api/library/lyrics/find', { ...q }); Object.entries(r.states).forEach(([f, s]) => paints[f](s)); toast('Lyrics search finished', 'ok'); loadLibrary(); } catch (e) { toast(e.message, 'error'); }
    all.disabled = false;
  } });
  fill(host, el('div', { class: 'edit' }, el('div', { class: 'bar' }, el('h2', { text: 'Lyrics' }), el('span', { class: 'sub', text: `${a.artist} - ${a.title}` }), all),
    el('p', { class: 'sub', text: 'Saved next to each song as a .lrc file. Find looks it up online; File or Paste uses your own.' }), rows,
    el('div', { style: 'margin-top:12px' }, el('button', { class: 'btn ghost', type: 'button', text: 'Close', onclick: () => { host.hidden = true; } }))));
  host.hidden = false; host.scrollIntoView({ block: 'start', behavior: 'smooth' });
}
$('#expBtn').addEventListener('click', () => openExport([...libSel.values()].map(a => ({ artist: a.artist, title: a.title }))));
$('#expAllBtn').addEventListener('click', () => openExport(null));
const crcTable = (() => { const t = new Uint32Array(256); for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; } return t; })();
const crc32 = buf => { const b = new Uint8Array(buf); let c = 0xFFFFFFFF; for (let i = 0; i < b.length; i++) c = crcTable[(c ^ b[i]) & 0xFF] ^ (c >>> 8); return ((c ^ 0xFFFFFFFF) >>> 0).toString(16).padStart(8, '0'); };
async function openExport(albums) {
  const host = $('#libEdit');
  let m;
  try { m = await api('/api/export/manifest', albums ? { albums } : { all: true }); } catch (e) { return toast(e.message, 'error'); }
  if (!m.files.length) return toast('Nothing to export', 'error');
  const status = el('p', { class: 'sub' }), fillBar = el('i'), meter = el('div', { class: 'meter expmeter' }, fillBar);
  const set = (text, pct) => { status.textContent = text; if (pct != null) fillBar.style.setProperty('--v', pct + '%'); };
  const what = albums ? `${albums.length} ${albums.length === 1 ? 'album' : 'albums'}` : 'Everything';
  let stop = false, busy = false;
  const idle = `${what}: ${m.files.length} files, ${bytes(m.bytes)}.`;
  const sameOnDevice = async (x, file) => crc32(await file.arrayBuffer()) === (await api('/api/export/hashes', { paths: [x.path] }))[x.path];
  const guard = fn => async () => { if (busy) return; busy = true; stop = false; try { await fn(); } catch (e) { set(e.message); toast(e.message, 'error'); } busy = false; };

  const copy = guard(async () => {
    let root;
    try { root = await showDirectoryPicker({ id: 'albumdl-export', mode: 'readwrite' }); } catch (_) { return; }
    const dirs = new Map(), queue = m.files.slice();
    const dirOf = parts => { const k = parts.join('/'); if (!dirs.has(k)) dirs.set(k, (async () => { let d = root; for (const p of parts) d = await d.getDirectoryHandle(p, { create: true }); return d; })()); return dirs.get(k); };
    let done = 0, copied = 0, skipped = 0, failed = 0;
    const one = async x => {
      const parts = x.path.split('/'), name = parts.pop(), dir = await dirOf(parts);
      let fh = null;
      try { fh = await dir.getFileHandle(name); } catch (_) {}
      if (fh) { const f = await fh.getFile(); if (f.size === x.size && await sameOnDevice(x, f)) { skipped++; return; } }
      fh = fh || await dir.getFileHandle(name, { create: true });
      const r = await fetch('/api/export/file?path=' + encodeURIComponent(x.path));
      if (!r.ok) throw new Error('HTTP ' + r.status);
      await r.body.pipeTo(await fh.createWritable());
      copied++;
    };
    const worker = async () => { while (queue.length && !stop) { const x = queue.shift(); try { await one(x); } catch (_) { failed++; } done++; set(`Copied ${copied}, already there ${skipped}${failed ? `, failed ${failed}` : ''} · ${done} of ${m.files.length}`, Math.round(100 * done / m.files.length)); } };
    await Promise.all([worker(), worker(), worker()]);
    set((stop ? 'Stopped. ' : 'Done. ') + `Copied ${copied}, ${skipped} were already there${failed ? `, ${failed} failed` : ''}.`, 100);
  });

  const compare = files => guard(async () => {
    // the picked folder may be the player itself or its music folder: find the depth where the Artist/Album layout starts
    const known = new Set(m.files.map(x => x.path.normalize('NFC'))), rels = files.map(f => f.webkitRelativePath.normalize('NFC').split('/'));
    let depth = 1, hits = -1;
    for (let d = 1; d <= 4; d++) { const c = rels.filter(p => p.length > d && known.has(p.slice(d).join('/'))).length; if (c > hits) { hits = c; depth = d; } }
    const base = rels.length ? rels[0].slice(0, depth).join('/') : '';
    const local = new Map();
    files.forEach((f, i) => { if (rels[i].length > depth) local.set(rels[i].slice(depth).join('/'), f); });
    const need = [], check = [];
    let same = 0;
    for (const x of m.files) {
      const f = local.get(x.path.normalize('NFC'));
      if (!f || f.size !== x.size) need.push(x); else if (Math.abs(f.lastModified / 1000 - x.mtime) <= 2) same++; else check.push([x, f]);
    }
    for (let i = 0; i < check.length && !stop; i++) {
      set(`Checking files that might have changed · ${i + 1} of ${check.length}`, Math.round(100 * i / check.length));
      (await sameOnDevice(...check[i])) ? same++ : need.push(check[i][0]);
    }
    if (stop) return set('Stopped.');
    if (!need.length) return set(`Everything is already on the player (${same} files).`, 100);
    const { token } = await api('/api/export/prepare', { paths: need.map(x => x.path) });
    set(`${need.length} new or changed files (${bytes(need.reduce((t, x) => t + x.size, 0))}), ${same} already there. Your browser is downloading a ZIP of them; unzip it into "${base}" on the player.`, 100);
    location.href = '/api/export/zip?token=' + token;
  })();
  const pickIn = el('input', { type: 'file', webkitdirectory: true, multiple: true, hidden: true, onchange: () => { const files = [...pickIn.files]; pickIn.value = ''; if (files.length) compare(files); } });
  const zipAll = guard(async () => { const { token } = await api('/api/export/prepare', { paths: m.files.map(x => x.path) }); set('Your browser is downloading a ZIP of ' + what.toLowerCase() + '.', 100); location.href = '/api/export/zip?token=' + token; });
  const direct = !!(window.showDirectoryPicker && window.isSecureContext);
  fill(host, el('div', { class: 'edit' }, el('div', { class: 'bar' }, el('h2', { text: 'Export' }), el('span', { class: 'sub', text: what })),
    el('p', { class: 'sub', text: direct
      ? 'Pick the folder on your MP3 player that should hold the artist folders. Files already there with the same checksum are skipped.'
      : 'Pick your MP3 player or its music folder. It is expected to use the same Artist/Album folders as the server. AlbumDL compares it with your library and downloads a ZIP of only what is new or changed. Your browser may say it is uploading; nothing is sent, it only reads the files.' }),
    meter, status,
    el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;margin-top:12px' },
      direct ? el('button', { class: 'btn go', type: 'button', text: 'Choose folder and copy', onclick: copy }) : null,
      el('button', { class: direct ? 'btn ghost' : 'btn go', type: 'button', text: 'Compare with player', onclick: () => pickIn.click() }), pickIn,
      el('button', { class: 'btn ghost', type: 'button', text: 'Download all as ZIP', onclick: zipAll }),
      el('button', { class: 'btn ghost', type: 'button', text: 'Stop', onclick: () => { stop = true; } }),
      el('button', { class: 'btn ghost', type: 'button', text: 'Close', onclick: () => { stop = true; host.hidden = true; } }))));
  set(idle, 0);
  host.hidden = false; host.scrollIntoView({ block: 'start', behavior: 'smooth' });
}
$('#editBtn').addEventListener('click', () => { const a = [...libSel.values()][0]; if (a) openEditor(a.artist, a.title); });
async function openEditor(artist, title) {
  let d;
  try { d = await api('/api/library/album?' + new URLSearchParams({ artist, album: title })); } catch (e) { return toast(e.message, 'error'); }
  const f = { artist: d.artist, album: d.album, year: d.year, genre: d.genre, label: d.label }, titles = {}, host = $('#libEdit');
  d.tracks.forEach(t => { titles[t.file] = t.title; });
  let rename = true, square = true;
  const field = (label, key, hint) => { const i = el('input', { type: 'text', value: f[key], 'aria-label': label, placeholder: hint, oninput: () => { f[key] = i.value; } }); return el('label', { class: 'field' }, el('small', { text: label }), i); };
  const thumb = el('span', { class: 'cover' }), paint = () => fill(thumb, el('img', { alt: '', src: '/api/library/art?' + new URLSearchParams({ artist: d.artist_dir, album: d.album_dir, t: Date.now() }), onload: ev => ev.target.classList.add('ok') }));
  paint();
  const applyCover = async job => { try { await job; toast('Cover updated', 'ok'); paint(); loadLibrary(); } catch (e) { toast(e.message, 'error'); } };
  const fileIn = el('input', { type: 'file', accept: 'image/*', hidden: true, onchange: () => {
    const file = fileIn.files[0]; if (!file) return;
    applyCover(fetch('/api/library/cover?' + new URLSearchParams({ artist: d.artist_dir, album: d.album_dir, square: square ? 1 : 0 }), { method: 'POST', headers: { 'Content-Type': file.type || 'application/octet-stream' }, body: file })
      .then(async r => { const j = await r.json().catch(() => ({})); if (!r.ok || j.error) throw new Error(j.error || 'HTTP ' + r.status); })).then(() => { fileIn.value = ''; });
  } });
  const linkIn = el('input', { type: 'text', placeholder: 'or paste an image link', 'aria-label': 'Image link' });
  const coverRow = el('div', { class: 'coverrow' }, thumb, el('div', { class: 'coverset' },
    el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' }, el('button', { class: 'btn sm', type: 'button', text: 'Choose image', onclick: () => fileIn.click() }), fileIn, linkIn,
      el('button', { class: 'btn sm ghost', type: 'button', text: 'Use link', onclick: () => applyCover(api('/api/library/cover-url', { artist: d.artist_dir, album: d.album_dir, url: linkIn.value, square })).then(() => { linkIn.value = ''; }) })),
    el('label', { class: 'checkline', style: 'margin-top:8px' }, el('input', { type: 'checkbox', checked: true, onchange: ev => { square = ev.target.checked; } }), ' Crop to a square. Any image type is converted to JPG, and the change applies right away.')));
  const save = el('button', { class: 'btn go', type: 'button', text: 'Save tags', onclick: async () => {
    save.disabled = true;
    try {
      await api('/api/library/tags', { artist: d.artist_dir, album: d.album_dir, fields: f, tracks: d.tracks.map(t => ({ file: t.file, title: titles[t.file] })), rename });
      toast('Tags saved', 'ok'); host.hidden = true; libSel.clear(); drawActions(); await loadLibrary();
    } catch (e) { toast(e.message, 'error'); save.disabled = false; }
  } });
  fill(host, el('div', { class: 'edit' },
    el('div', { class: 'bar' }, el('h2', { text: 'Edit tags' }), el('span', { class: 'sub', text: d.tracks.length + (d.tracks.length === 1 ? ' track' : ' tracks') }),
      el('button', { class: 'btn sm ghost', type: 'button', style: 'margin-left:auto', text: 'Lyrics', onclick: () => openLyrics({ artist: d.artist_dir, title: d.album_dir }) })),
    coverRow,
    el('div', { class: 'imp' }, field('Artist', 'artist', ''), field('Album', 'album', ''), field('Year', 'year', '2012'), field('Genre', 'genre', 'optional'), field('Label', 'label', 'optional')),
    el('div', { class: 'tlist' }, d.tracks.map((t, i) => { const inp = el('input', { type: 'text', value: t.title, 'aria-label': 'Track ' + (i + 1) + ' title', oninput: () => { titles[t.file] = inp.value; } }); return el('div', { class: 'erow' }, el('span', { class: 'sz', text: String(i + 1).padStart(2, '0') }), inp); })),
    el('label', { class: 'checkline' }, el('input', { type: 'checkbox', checked: true, onchange: ev => { rename = ev.target.checked; } }), ' Rename the folder and files to match'),
    el('div', { class: 'row', style: 'display:flex;gap:8px;margin-top:12px' }, save, el('button', { class: 'btn ghost', type: 'button', text: 'Cancel', onclick: () => { host.hidden = true; } }))));
  host.hidden = false; host.scrollIntoView({ block: 'start', behavior: 'smooth' });
}
const nodes = new Map(), openSet = new Map();
let busy = false, order = '';
const isOpen = j => openSet.has(j.id) ? openSet.get(j.id) : j.status === 'running';
const kindOf = j => ({ running: 'run', queued: 'wait', done: 'done', incomplete: 'part', failed: 'failed', cancelled: 'cancelled' }[j.status] || 'wait');
function statusText(j) {
  const eta = j.status === 'running' && j.finished > 0 ? (j.total - j.finished) * (j.elapsed / j.finished) : null;
  switch (j.status) {
    case 'running': return `Downloading · ${j.finished}/${j.total}` + (eta != null ? ` · ${dur(eta)} left` : '');
    case 'queued': return 'Waiting';
    case 'done': return `Done · ${j.total} tracks · ${bytes(j.bytes)}` + (j.src_kbps ? ` · source ~${j.src_kbps}k` : '') + (j.gaps && j.gaps.length ? ' · data incomplete' : '');
    case 'incomplete': return `${j.failed} of ${j.total} missing`;
    case 'cancelled': return 'Cancelled';
    default: return 'Failed';
  }
}
function jobNode(j) {
  const n = { j };
  n.name = el('b'); n.by = el('small'); n.st = el('span', { class: 'st' });
  n.act = el('button', { class: 'btn sm', type: 'button' });
  n.link = el('button', { class: 'btn sm hot', type: 'button', text: 'Link tracks', onclick: () => { show('library'); openLinkerFor(n.j); } });
  n.edit = el('button', { class: 'btn sm ghost', type: 'button', text: 'Edit tags', onclick: () => { show('library'); openEditor(n.j.artist, n.j.title); } });
  n.meterFill = el('i'); n.tracks = el('div', { class: 'tracks' }); n.err = el('div', { class: 'jerr' });
  n.root = el('article', { class: 'job' },
    el('div', { class: 'jmain' },
      el('button', { class: 'jtog', type: 'button', onclick: () => toggle(n) }, cover(coverUrl(j.mbid, j.group_id, j.artist, j.title, 150)), el('span', { class: 'jtext' }, n.name, n.by, n.st)),
      el('div', { class: 'jside' }, n.link, n.edit, n.act, el('button', { class: 'jtog chevbtn', type: 'button', 'aria-label': 'Show tracks', onclick: () => toggle(n) }, el('i', { class: 'chev' })))),
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
  n.link.hidden = j.status !== 'incomplete';
  n.edit.hidden = !(j.gaps && j.gaps.length && ['done', 'incomplete'].includes(j.status));
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
    const note = t.state === 'done' ? [t.acodec ? `${t.acodec}${t.src_kbps ? ' ' + t.src_kbps + 'k' : ''} → mp3` : '', t.lyrics === 'synced' || t.lyrics === 'plain' ? t.lyrics + ' lyrics' : '', /unofficial/.test(t.why || '') ? 'unofficial upload' : ''].filter(Boolean).join(' · ') : t.text;
    return el('div', { class: 'tr ' + t.state }, el('span', { class: 'ico ' + t.state }),
      el('div', { style: 'min-width:0' }, el('div', { class: 'ti', text: t.title, title: t.title }), note ? el('small', { text: note }) : null),
      el('span', { class: 'sz', text: t.state === 'done' ? bytes(t.out_bytes || t.src_bytes) : '' }));
  }));
}
const sec = (text, extra) => el('div', { class: 'sec' }, text, extra);
function applyJobs(jobs, batches) {
  const run = jobs.filter(j => j.status === 'running'), wait = jobs.filter(j => j.status === 'queued').reverse(), fin = jobs.filter(j => !['running', 'queued'].includes(j.status));
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
  $('#stats').textContent = `${s.tracks.done} ${s.tracks.done === 1 ? 'track' : 'tracks'} · ${bytes(s.bytes_out)} written · ${bytes(s.disk.free)} free`;
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
