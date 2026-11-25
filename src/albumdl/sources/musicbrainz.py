import collections
import difflib
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import List, Optional

import requests

API = "https://musicbrainz.org/ws/2"
UA = "albumdl/0.1" + (f" ( {os.environ['ALBUMDL_CONTACT']} )" if os.environ.get("ALBUMDL_CONTACT") else "")
_last_call = 0.0
_lock = threading.Lock()


STATS = {"requests": 0, "cached": 0, "retries": 0, "errors": 0, "ms": 0.0}
HOOK = None


def _note(level: str, msg: str) -> None:
    if HOOK:
        try:
            HOOK(level, msg)
        except Exception:
            pass


_cache: dict = {}
CACHE_TTL, CACHE_MAX = 900, 300


def _get(path: str, **params) -> dict:
    key = (path, tuple(sorted(params.items())))
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        STATS["cached"] += 1
        _note("debug", f"GET {path} (cached)")
        return hit[1]
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.time():
            STATS["cached"] += 1
            return hit[1]
        data = _get_locked(path, **params)
        if len(_cache) >= CACHE_MAX:
            _cache.pop(next(iter(_cache)), None)
        _cache[key] = (time.time() + CACHE_TTL, data)
        return data


def _get_locked(path: str, **params) -> dict:
    global _last_call
    params["fmt"] = "json"
    err = "no response"
    for attempt in range(8):
        wait = 1.0 - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        t0 = time.time()
        STATS["requests"] += 1
        try:
            r = requests.get(f"{API}/{path}", params=params,
                             headers={"User-Agent": UA}, timeout=30)
        except requests.RequestException as exc:
            _last_call = time.time()
            err = type(exc).__name__
            STATS["errors"] += 1
            STATS["retries"] += 1
            _note("warn", f"GET {path} failed ({err}), retrying")
            time.sleep(min(2 * (attempt + 1), 15))
            continue
        _last_call = time.time()
        STATS["ms"] += (_last_call - t0) * 1000
        _note("debug" if r.ok else "warn", f"GET {path} -> {r.status_code} in {(_last_call - t0) * 1000:.0f} ms")
        if r.status_code in (429, 502, 503, 504):
            err = f"HTTP {r.status_code}"
            STATS["errors"] += 1
            STATS["retries"] += 1
            try:
                hint = float(r.headers.get("Retry-After", 1))
            except ValueError:
                hint = 1.0
            time.sleep(min(max(hint, 1.0), 10) + 0.5 * attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise SystemExit(f"error: MusicBrainz is unreachable ({err}); check the network and try again")


@dataclass
class Track:
    title: str
    number: int
    disc: int
    length_ms: Optional[int]
    artist: str
    artists: List[str]
    artist_ids: List[str]
    recording_id: str
    track_id: str
    artist_sort: str = ""
    source: str = ""
    start: float = 0.0
    end: float = 0.0


@dataclass
class Album:
    mbid: str
    group_id: str
    title: str
    artist: str
    artists: List[str]
    artist_ids: List[str]
    date: str = ""
    original_date: str = ""
    country: str = ""
    status: str = ""
    type: str = ""
    label: str = ""
    catalog: str = ""
    barcode: str = ""
    genres: List[str] = field(default_factory=list)
    compilation: bool = False
    disc_total: int = 1
    artist_sort: str = ""
    media_format: str = ""
    language: str = ""
    script: str = ""
    asin: str = ""
    cover_url: str = ""
    edition: str = ""
    partial: bool = False
    folder: str = ""
    disc_counts: dict = field(default_factory=dict)
    tracks: List[Track] = field(default_factory=list)

    @property
    def year(self) -> str:
        return (self.original_date or self.date or "")[:4]


def _credit(credit: list) -> tuple:
    text = "".join((c.get("name") or c["artist"]["name"]) + c.get("joinphrase", "")
                   for c in credit)
    return (text, [c["artist"]["name"] for c in credit],
            [c["artist"]["id"] for c in credit])


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


TYPE_RANK = {"Album": 0, "EP": 1, "Single": 2}


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _group_rows(data: dict) -> List[dict]:
    out = []
    for g in data.get("release-groups", []):
        text, _, _ = _credit(g.get("artist-credit", []))
        out.append({
            "id": g["id"], "title": g["title"], "artist": text,
            "type": g.get("primary-type") or "", "secondary": g.get("secondary-types", []),
            "date": g.get("first-release-date", ""), "score": int(g.get("score", 0)),
        })
    return out


def search_albums(artist: str, album: str, limit: int = 50) -> List[dict]:
    if not artist and not album:
        return []
    parts = []
    if album:
        parts.append(f'releasegroup:"{_q(album)}"')
    if artist:
        parts.append(f'artist:"{_q(artist)}"')
    rows = _group_rows(_get("release-group", query=" AND ".join(parts), limit=limit))
    if not rows and artist and album:
        everything = _group_rows(_get("release-group", query=f'artist:"{_q(artist)}"', limit=100))
        scored = [(difflib.SequenceMatcher(None, _norm(r["title"]), _norm(album)).ratio(), r)
                  for r in everything]
        rows = [r for sc, r in sorted(scored, key=lambda x: -x[0]) if sc >= 0.6][:10]
    want = _norm(album)
    rows.sort(key=lambda r: (bool(album) and _norm(r["title"]) != want,
                             TYPE_RANK.get(r["type"], 3), bool(r["secondary"]),
                             -r["score"] if album else 0, r["date"] or "9999"))
    return rows


def find_group(artist: str, title: str) -> str:
    """The release group for an album named on another service (Deezer titles carry '(Deluxe)' and the like)."""
    base = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*", " ", title).strip() or title
    rows = search_albums(artist, base, limit=15)
    want = _norm(base)
    same = [r for r in rows if _norm(r["title"]) == want and _fold(artist) in _fold(r["artist"])]
    return same[0]["id"] if same else ""


def find_song_group(artist: str, song: str) -> dict:
    """The studio album a song first appeared on, for when the album another service names is not on MusicBrainz."""
    data = _get("recording", query=f'recording:"{_q(song)}" AND artist:"{_q(artist)}" AND primarytype:album AND status:official', limit=25)
    best = None
    for rec in data.get("recordings", []):
        if _norm(rec.get("title", "")) != _norm(song):
            continue
        for rel in rec.get("releases", []):
            g = rel.get("release-group", {})
            if g.get("primary-type") != "Album" or g.get("secondary-types") or not g.get("id"):
                continue
            row = {"id": g["id"], "title": g.get("title") or rel.get("title", ""), "date": rel.get("date") or "9999"}
            if not best or row["date"] < best["date"]:
                best = row
    return best or {}


def _fold(s: str) -> str:
    return re.sub(r"[\W_]+", "", "".join(c for c in unicodedata.normalize("NFD", s.lower()) if not unicodedata.combining(c)))


def _words(s: str) -> List[str]:
    return re.findall(r"\w+", "".join(c for c in unicodedata.normalize("NFD", s.lower()) if not unicodedata.combining(c)))


def search_text(text: str, limit: int = 25) -> List[dict]:
    """Free text like "title fight kingston" or "kingston by title fight": keep releases whose artist and title together contain every word."""
    year = re.search(r"\b(?:19|20)\d{2}\b", text)
    words = [w for w in _words(re.sub(r"\b(?:19|20)\d{2}\b", " ", text)) if w not in {"by", "album", "ep", "single", "full"}]
    if len(words) < 2:
        return []
    joined = " ".join(words)
    rows = _group_rows(_get("release-group", query=f"releasegroup:({joined}) OR artist:({joined})", limit=limit))
    want = set(words)
    rows = [r for r in rows if want <= set(_words(f"{r['artist']} {r['title']}"))]
    rows.sort(key=lambda r: (bool(year) and not r["date"].startswith(year.group()), TYPE_RANK.get(r["type"], 3), bool(r["secondary"]), -r["score"]))
    return rows


def search_artists(name: str, limit: int = 6) -> List[dict]:
    data = _get("artist", query=f'artist:"{_q(name)}"', limit=50)
    if not data.get("artists"):
        data = _get("artist", query=name, limit=50)
    out = []
    for a in data.get("artists", []):
        span = a.get("life-span") or {}
        out.append({"id": a["id"], "name": a["name"], "type": a.get("type") or "",
                    "country": a.get("country") or "", "disambiguation": a.get("disambiguation") or "",
                    "begin": (span.get("begin") or "")[:4], "end": (span.get("end") or "")[:4],
                    "score": int(a.get("score", 0)),
                    "match": round(difflib.SequenceMatcher(None, _fold(name), _fold(a["name"])).ratio(), 2)})
    out.sort(key=lambda a: _fold(a["name"]) != _fold(name))
    return out[:limit]


def artist_info(artist_id: str) -> dict:
    a = _get(f"artist/{artist_id}")
    span = a.get("life-span") or {}
    return {"id": a["id"], "name": a["name"], "type": a.get("type") or "", "country": a.get("country") or "",
            "disambiguation": a.get("disambiguation") or "", "begin": (span.get("begin") or "")[:4], "end": (span.get("end") or "")[:4]}


def artist_albums(artist_id: str) -> List[dict]:
    rows, offset = [], 0
    while True:
        data = _get("release-group", artist=artist_id, inc="artist-credits", limit=100, offset=offset)
        rows += _group_rows(data)
        offset += 100
        if offset >= data.get("release-group-count", 0) or offset >= 1500:
            break
    rows.sort(key=lambda r: (TYPE_RANK.get(r["type"], 3), bool(r["secondary"]), r["date"] or "9999"))
    return rows


def group_editions(group_id: str) -> List[dict]:
    data = _get("release", **{"release-group": group_id, "inc": "media", "limit": 100})
    out = []
    for r in data.get("releases", []):
        media = r.get("media", [])
        out.append({
            "id": r["id"], "title": r["title"], "date": r.get("date", ""),
            "country": r.get("country", ""), "status": r.get("status", ""),
            "tracks": sum(m.get("track-count", 0) for m in media),
            "format": (media[0].get("format") if media else "") or "",
            "discs": len(media), "disambiguation": r.get("disambiguation", ""),
        })
    official = [r for r in out if r["status"] == "Official"] or out
    common = collections.Counter(r["tracks"] for r in official).most_common(1)
    common = common[0][0] if common else 0
    out.sort(key=lambda r: (r["status"] != "Official", r["tracks"] != common,
                            r["format"] not in ("CD", "Digital Media"),
                            r["country"] not in ("XW", "US", "GB"), r["date"] or "9999"))
    return out


def get_release(mbid: str) -> Album:
    inc = "recordings+artist-credits+labels+genres+release-groups+media"
    r = _get(f"release/{mbid}", inc=inc)
    group = r.get("release-group", {})
    text, names, ids = _credit(r.get("artist-credit", []))
    label = catalog = ""
    for li in r.get("label-info", []):
        if li.get("label"):
            label = li["label"]["name"]
        catalog = li.get("catalog-number") or ""
        if label:
            break
    genres = r.get("genres") or group.get("genres") or []
    if not genres and group.get("id"):
        g = _get(f"release-group/{group['id']}", inc="genres+tags")
        genres = g.get("genres") or [t for t in g.get("tags", []) if t.get("count", 0) > 0]
    genres = [g["name"].title() for g in sorted(genres, key=lambda g: -g.get("count", 0))][:5]
    secondary = group.get("secondary-types", [])
    album = Album(
        mbid=r["id"], group_id=group.get("id", ""), title=r["title"],
        artist=text, artists=names, artist_ids=ids,
        date=r.get("date") or "", original_date=group.get("first-release-date") or "",
        country=r.get("country") or "", status=r.get("status") or "",
        type=group.get("primary-type") or "", label=label, catalog=catalog,
        barcode=r.get("barcode") or "", genres=genres,
        compilation="Compilation" in secondary or text == "Various Artists",
        disc_total=len(r.get("media", [])) or 1,
        artist_sort=(r.get("artist-credit") or [{}])[0].get("artist", {}).get("sort-name", ""),
        media_format=(r.get("media") or [{}])[0].get("format") or "",
        language=r.get("text-representation", {}).get("language") or "",
        script=r.get("text-representation", {}).get("script") or "",
        asin=r.get("asin") or "", edition=r.get("disambiguation") or "",
    )
    for medium in r.get("media", []):
        disc = medium.get("position", 1)
        for t in medium.get("tracks", []):
            rec = t.get("recording", {})
            ttext, tnames, tids = _credit(t.get("artist-credit") or rec.get("artist-credit") or [])
            album.tracks.append(Track(
                title=t["title"], number=t["position"], disc=disc,
                length_ms=t.get("length") or rec.get("length"),
                artist=ttext or text, artists=tnames or names, artist_ids=tids or ids,
                recording_id=rec.get("id", ""),
                track_id=t["id"],
                artist_sort=((t.get("artist-credit") or rec.get("artist-credit") or [{}])[0]
                             .get("artist", {}).get("sort-name", "")),
            ))
    return album
