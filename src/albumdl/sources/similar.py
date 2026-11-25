"""Similar artists, fused from ListenBrainz, Deezer and Last.fm so one noisy source can't hijack the list."""
import difflib
import os
import re
import statistics
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional
from urllib.parse import unquote_plus

import requests

from . import musicbrainz as mb
from .musicbrainz import UA

LISTENBRAINZ = "https://labs.api.listenbrainz.org/similar-artists/json"
ALGORITHM = "session_based_days_9000_session_300_contribution_5_threshold_15_limit_50_skip_30"
DEEZER = "https://api.deezer.com"
WEIGHTS = {"deezer": 1.0, "lastfm": 1.0, "listenbrainz": 0.8}
COLLAB = re.compile(r" & |, | x | feat\.? | with ", re.I)
FAIL = (requests.RequestException, ValueError, KeyError, TypeError, SystemExit)
_cache: dict = {}
_ids: dict = {}


def fold(s: str) -> str:
    """Compare names loosely: 'JAŸ-Z' and 'JAY Z' are the same artist."""
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower()
    return re.sub(r"\W+", "", s) or s.strip()


# Deezer: free, no key, about 50 requests per 5 seconds, so space them out and cache for a day.
_dz_lock = threading.Lock()
_dz_last = 0.0
_dz_cache: dict = {}


def dz(path: str, **params) -> dict:
    global _dz_last
    key = path + repr(sorted(params.items()))
    hit = _dz_cache.get(key)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    for attempt in range(2):
        with _dz_lock:
            wait = _dz_last + 0.12 - time.time()
            if wait > 0:
                time.sleep(wait)
            _dz_last = time.time()
        data = requests.get(DEEZER + path, params=params, timeout=12, headers={"User-Agent": UA}).json()
        err = data.get("error") if isinstance(data, dict) else None
        if not err:
            break
        if err.get("code") != 4 or attempt:
            return {}
        time.sleep(2)
    if len(_dz_cache) > 3000:
        for k in list(_dz_cache)[:500]:
            _dz_cache.pop(k, None)
    _dz_cache[key] = (time.time(), data)
    return data


def deezer_artist(name: str, names: Optional[List[str]] = None) -> Optional[dict]:
    """The Deezer artist with this exact name (or one of its aliases) and the most fans."""
    want = {fold(n) for n in (names or [name])} | {fold(name)}
    best = None
    for q in [name] + [n for n in (names or []) if fold(n) != fold(name)][:2]:
        try:
            rows = dz("/search/artist", q=q, limit=8).get("data", [])
        except FAIL:
            continue
        for r in rows:
            if fold(r.get("name", "")) in want and (not best or r.get("nb_fan", 0) > best["fans"]):
                best = {"deezer": r["id"], "name": r["name"], "fans": r.get("nb_fan", 0)}
        if best:
            break
    return best


def identity(artist_id: str, name: str) -> dict:
    """Who this MusicBrainz artist is on the other services, from MusicBrainz's own links. Searching by name
    alone goes wrong for short names: Last.fm autocorrects 'Ye' to 'Yes'."""
    hit = _ids.get(artist_id)
    if hit and time.time() - hit[0] < 604800:
        return hit[1]
    try:
        a = mb._get(f"artist/{artist_id}", inc="url-rels+aliases+genres")
    except FAIL:
        a = {}
    main = a.get("name") or name
    names = list(dict.fromkeys([main, name] + [x["name"] for x in a.get("aliases", []) if x.get("name")]))[:12]
    links = [r.get("url", {}).get("resource", "") for r in a.get("relations", [])]
    dz_ids = [int(m.group(1)) for u in links if (m := re.search(r"deezer\.com/(?:\w+/)?artist/(\d+)", u))]
    lastfm = [unquote_plus(m.group(1)) for u in links if (m := re.search(r"last\.fm/(?:\w+/)?music/([^/?#]+)", u))]
    info = {"name": main, "names": names, "deezer": 0, "fans": 0, "lastfm": [n for n in lastfm if not n.startswith("+")],
            "genres": [g["name"] for g in sorted(a.get("genres", []), key=lambda g: -g.get("count", 0))][:4]}
    for i in dz_ids[:3]:
        try:
            r = dz(f"/artist/{i}")
        except FAIL:
            continue
        if r.get("id") and r.get("nb_fan", 0) >= info["fans"]:
            info["deezer"], info["fans"] = r["id"], r.get("nb_fan", 0)
    if not info["deezer"]:
        found = deezer_artist(main, names)
        if found:
            info["deezer"], info["fans"] = found["deezer"], found["fans"]
    if a:
        _ids[artist_id] = (time.time(), info)
    return info


def related(dz_id: int) -> List[dict]:
    if not dz_id:
        return []
    try:
        rows = dz(f"/artist/{dz_id}/related", limit=40).get("data", [])
    except FAIL:
        return []
    return [{"name": r["name"], "deezer": r["id"], "fans": r.get("nb_fan", 0)} for r in rows if r.get("name")]


def _listenbrainz(artist_id: str) -> List[dict]:
    rows = requests.get(LISTENBRAINZ, timeout=20, headers={"User-Agent": UA},
                        params={"artist_mbids": artist_id, "algorithm": ALGORITHM}).json()
    return [{"id": r["artist_mbid"], "name": r["name"]} for r in rows if r["artist_mbid"] != artist_id]


def _lastfm_one(key: str, ref: dict) -> List[dict]:
    data = requests.get("https://ws.audioscrobbler.com/2.0/", timeout=15, headers={"User-Agent": UA},
                        params={"method": "artist.getsimilar", "api_key": key, "format": "json", "limit": 30, "autocorrect": 0, **ref}).json()
    return [{"name": r["name"], "hint": r.get("mbid", "")} for r in data.get("similarartists", {}).get("artist", [])]


def _lastfm(artist_id: str, who: dict, key: str, others: List[dict]) -> List[dict]:
    """Last.fm's MusicBrainz ids are only a hint, and its name lookup can land on another artist. Try the MBID and the
    names MusicBrainz links to, never autocorrect, and keep whichever list agrees most with the other sources."""
    refs = [{"mbid": artist_id}] + [{"artist": n} for n in dict.fromkeys(who["lastfm"] + who["names"][:2])][:3]
    seen = {fold(r["name"]) for r in others}
    best, best_overlap = [], -1
    for ref in refs:
        try:
            rows = _lastfm_one(key, ref)
        except FAIL:
            continue
        if not rows:
            continue
        overlap = sum(fold(r["name"]) in seen for r in rows)
        if not seen:
            return rows
        if overlap > best_overlap:
            best, best_overlap = rows, overlap
        if overlap >= 4:
            break
    return best if best_overlap > 0 else []


def fuse(lists: Dict[str, List[dict]], skip: set) -> List[dict]:
    """Rank artists by how high every source puts them; an artist several sources agree on rises to the top."""
    rows: Dict[str, dict] = {}
    for src, items in lists.items():
        for rank, r in enumerate(items[:40]):
            k = fold(r["name"])
            if not k or k in skip:
                continue
            row = rows.setdefault(k, {"name": r["name"], "id": "", "hint": "", "deezer": 0, "fans": 0, "score": 0.0, "sources": []})
            row["score"] += WEIGHTS[src] / (rank + 6)
            if src not in row["sources"]:
                row["sources"].append(src)
            for f in ("id", "hint", "deezer", "fans"):
                if r.get(f) and not row[f]:
                    row[f] = r[f]
            if src == "lastfm":
                row["name"] = r["name"]
    top = sum(WEIGHTS[s] / 6 for s, items in lists.items() if items) or 1
    long = [k for k in skip if len(k) >= 4]
    out = sorted((r for k, r in rows.items() if not any(s in k for s in long)
                  and (len(r["sources"]) > 1 or not COLLAB.search(r["name"]))), key=lambda r: -r["score"])
    for r in out:
        r["match"] = round(100 * min(1.0, r["score"] / top) ** 0.5)
    return out


def similar_artists(artist_id: str, name: str) -> dict:
    """Each row has an `id` only when ListenBrainz confirmed which band it is; otherwise the page asks the user to pick."""
    hit = _cache.get(artist_id)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    who = identity(artist_id, name)
    with ThreadPoolExecutor(2) as ex:
        lb_job = ex.submit(_listenbrainz, artist_id)
        dz_job = ex.submit(related, who["deezer"])
        try:
            lb = lb_job.result()
        except FAIL:
            lb = []
        dzr = dz_job.result()
    lists = {"listenbrainz": lb, "deezer": dzr}
    key = os.environ.get("ALBUMDL_LASTFM", "")
    if key:
        lists["lastfm"] = _lastfm(artist_id, who, key, lb + dzr)
    rows = fuse(lists, {fold(n) for n in who["names"]})[:30]
    out = {"artists": rows, "genres": who["genres"], "fans": who["fans"]}
    if rows:
        _cache[artist_id] = (time.time(), out)
    return out


def deeper(artist_id: str, name: str) -> List[dict]:
    """Artists two steps away that several close matches lead to, but with far fewer fans: the deep cuts."""
    first = similar_artists(artist_id, name)
    hop = [r for r in first["artists"] if r["deezer"]][:10]
    if not hop:
        return []
    known = {fold(r["name"]) for r in first["artists"]} | {fold(n) for n in identity(artist_id, name)["names"]}
    with ThreadPoolExecutor(5) as ex:
        lists = list(ex.map(lambda r: related(r["deezer"]), hop))
    pool: Dict[str, dict] = {}
    for via, items in zip(hop, lists):
        for rank, r in enumerate(items):
            k = fold(r["name"])
            if k in known:
                continue
            row = pool.setdefault(k, dict(r, id="", hint="", score=0.0, via=[]))
            row["score"] += (via["match"] / 100) / (rank + 4)
            row["via"].append(via["name"])
    fans = [r["fans"] for r in hop if r["fans"]] + [first["fans"]]
    ceiling = statistics.median(fans) if fans else 0
    for r in pool.values():
        r["score"] *= 1 + 0.25 * (len(r["via"]) - 1)
    rows = sorted(pool.values(), key=lambda r: -r["score"])
    picked = [r for r in rows if len(r["via"]) > 1 and (not ceiling or r["fans"] < ceiling * 0.25)]
    if len(picked) < 10:
        picked += [r for r in rows if r not in picked and (not ceiling or r["fans"] < ceiling * 0.5)][:10 - len(picked)]
    top = max((r["score"] for r in picked), default=1) or 1
    for r in picked:
        r["match"] = round(100 * min(1.0, r["score"] / top) ** 0.5 * 0.9)
    return picked[:20]


_photos: dict = {}


def artist_photo(name: str):
    key = name.lower()
    hit = _photos.get(key)
    if hit and time.time() - hit[0] < (604800 if hit[1] else 3600):
        return hit[1]
    data = None
    try:
        rows = requests.get("https://api.deezer.com/search/artist", params={"q": name, "limit": 3}, timeout=10).json().get("data", [])
        want = re.sub(r"\W+", "", key)
        for r in rows:
            got = re.sub(r"\W+", "", r.get("name", "").lower())
            if r.get("picture_medium") and difflib.SequenceMatcher(None, want, got).ratio() >= 0.85 and "/images/artist//" not in r["picture_medium"]:
                img = requests.get(r["picture_medium"], timeout=10)
                if img.status_code == 200 and len(img.content) > 1000:
                    data = img.content
                    break
    except (requests.RequestException, ValueError):
        pass
    if len(_photos) > 400:
        _photos.pop(next(iter(_photos)))
    _photos[key] = (time.time(), data)
    return data
