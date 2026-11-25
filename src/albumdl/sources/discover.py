"""Album recommendations: walk from albums you have to related artists on Deezer, then pick their albums that fit."""
import math
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

from ..media.pipeline import title_key
from .similar import FAIL, deezer_artist, dz, fold, identity, related

SKIP = re.compile(r"\b(live|remix(es|ed)?|instrumentals?|karaoke|a ?cappella|sped up|slowed|commentary|interview|tribute|8-?bit|best of|greatest hits|the hits|hits collection|collection|essentials?|anthology|soundtrack|music from|inspired by)\b", re.I)
EDITION = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*|\s+-\s+(deluxe|expanded|remaster|anniversary|edition|special).*$", re.I)
Owned = Callable[[str, str], object]
_seen: dict = {}


def _artist(name: str) -> Optional[dict]:
    hit = _seen.get(fold(name))
    if hit and time.time() - hit[0] < 604800:
        return hit[1]
    found = deezer_artist(name)
    _seen[fold(name)] = (time.time(), found)
    return found


def albums_of(dz_id: int, artist: str) -> List[dict]:
    """An artist's albums and EPs, one row per album however many deluxe editions Deezer lists."""
    try:
        rows = dz(f"/artist/{dz_id}/albums", limit=100).get("data", [])
    except FAIL:
        return []
    out: Dict[str, dict] = {}
    for r in rows:
        title = r.get("title", "")
        if r.get("record_type") not in ("album", "ep") or SKIP.search(title):
            continue
        base = EDITION.sub("", title).strip() or title
        year = int((r.get("release_date") or "0")[:4] or 0)
        row = {"title": base, "artist": artist, "deezer": r["id"], "cover": r.get("cover_big") or r.get("cover_medium") or "",
               "year": year, "fans": r.get("fans", 0), "genre": r.get("genre_id", 0), "type": "EP" if r["record_type"] == "ep" else "Album"}
        prev = out.get(fold(base))
        if not prev:
            out[fold(base)] = row
            continue
        prev["fans"] = max(prev["fans"], row["fans"])
        if year and (not prev["year"] or year < prev["year"]):
            prev["year"], prev["cover"] = year, row["cover"] or prev["cover"]
    return list(out.values())


def _fit(a: dict, year: int, genre: int) -> float:
    """How good a pick this album is: popular with fans, and close in era and genre to what it is meant to be like."""
    s = (a["fans"] + 10) ** 0.3
    if year and a["year"]:
        s *= 0.4 + 0.6 * math.exp(-abs(a["year"] - year) / 8)
    if genre:
        s *= 1.25 if a["genre"] == genre else 0.8
    return s * (0.55 if a["type"] == "EP" else 1)


def _pmap(fn, items, workers=6):
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(fn, items))


def recommend(seeds: List[dict], owned: Owned, skip: set, year: int = 0, genre: int = 0,
              per_artist: int = 2, artists: int = 18) -> dict:
    """seeds: [{name, weight, deezer?}]. Every related artist collects score from each seed that points to it."""
    for s, found in zip(seeds, _pmap(lambda s: None if s.get("deezer") else _artist(s["name"]), seeds)):
        if found:
            s["deezer"] = found["deezer"]
    seeds = [s for s in seeds if s.get("deezer")]
    pool: Dict[str, dict] = {}
    for s, items in zip(seeds, _pmap(lambda s: related(s["deezer"]), seeds)):
        for rank, r in enumerate(items):
            k = fold(r["name"])
            if k in skip:
                continue
            row = pool.setdefault(k, dict(r, score=0.0, because=[]))
            row["score"] += s.get("weight", 1) / (rank + 5)
            row["because"].append(s["name"])
    cands = sorted(pool.values(), key=lambda r: -r["score"] * (1 + 0.15 * (len(r["because"]) - 1)))[:artists]
    top = max((c["score"] for c in cands), default=1) or 1
    out = []
    for c, items in zip(cands, _pmap(lambda c: albums_of(c["deezer"], c["name"]), cands)):
        c["match"] = round(100 * min(1.0, c["score"] / top) ** 0.5)
        ranked = sorted(items, key=lambda a: -_fit(a, year, genre))
        best = _fit(ranked[0], year, genre) if ranked else 1
        for a in [a for a in ranked if not owned(a["artist"], a["title"])][:per_artist]:
            out.append(dict(a, because=c["because"], score=c["score"] * (0.5 + 0.5 * _fit(a, year, genre) / best)))
    out.sort(key=lambda a: -a["score"])
    return {"albums": out, "artists": cands, "seeds": seeds}


def _spread(albums: List[dict], n: int) -> List[dict]:
    """Best albums first, but only one per artist until every artist has had a turn."""
    first, rest, seen = [], [], set()
    for a in albums:
        (rest if fold(a["artist"]) in seen else first).append(a)
        seen.add(fold(a["artist"]))
    return (first + rest)[:n]


def feed(library: List[dict], owned: Owned, has_artist: Callable[[str], bool], shuffle: int = 0, area: Optional[List[str]] = None) -> dict:
    """Recommendations from the library, or from one area of it (a list of artist names picked on the map):
    artists with more albums, and ones added lately, count for more. Artists anywhere in the library are never recommended."""
    by: Dict[str, dict] = {}
    for a in library:
        row = by.setdefault(fold(a["artist"]), {"name": a["artist"], "count": 0, "mtime": 0})
        row["count"] += 1
        row["mtime"] = max(row["mtime"], a.get("mtime") or 0)
    if not by:
        return {"seeds": [], "picks": [], "rails": [], "artists": [], "radar": [], "gaps": []}
    recent = {k for k, _ in sorted(by.items(), key=lambda kv: -kv[1]["mtime"])[:5]}
    for k, r in by.items():
        r["weight"] = 1 + math.log(r["count"]) + (0.6 if k in recent else 0)
    keep = {fold(n) for n in area} if area else set(by)
    rng, pool, seeds = random.Random(shuffle), [r for k, r in by.items() if k in keep], []
    while pool and len(seeds) < (6 if not area else min(8, max(3, len(pool)))):
        pick = max(pool, key=lambda r: r["weight"]) if not shuffle else rng.choices(pool, [r["weight"] for r in pool])[0]
        pool.remove(pick)
        seeds.append({"name": pick["name"], "weight": pick["weight"]})
    rec = recommend(seeds, owned, set(by), per_artist=2, artists=20)
    rails, used = [], set()
    for s in rec["seeds"]:
        albums = _spread([a for a in rec["albums"] if s["name"] in a["because"] and a["deezer"] not in used], 12)
        if len(albums) >= 4 and len(rails) < 4:
            rails.append({"seed": s["name"], "albums": albums})
            used |= {a["deezer"] for a in albums}
    gaps = []
    for s, items in zip(rec["seeds"], _pmap(lambda s: albums_of(s["deezer"], s["name"]), rec["seeds"])):
        gaps += sorted([a for a in items if a["type"] == "Album" and not owned(a["artist"], a["title"])], key=lambda a: -a["fans"])[:2]
    gaps.sort(key=lambda a: -a["fans"])
    fans = sorted(c["fans"] for c in rec["artists"] if c["fans"])
    median = fans[len(fans) // 2] if fans else 0
    return {
        "seeds": [s["name"] for s in rec["seeds"]],
        "picks": _spread(rec["albums"], 16),
        "rails": rails,
        "artists": [dict(c, owned=has_artist(c["name"])) for c in rec["artists"][:16]],
        "radar": [c for c in rec["artists"] if median and c["fans"] < median * 0.6][:12],
        "gaps": gaps[:12],
    }


def like_album(artist: str, title: str, owned: Owned) -> dict:
    """Albums like this one: from artists related to its artist (and to their closest neighbours), near it in era and genre."""
    seed = {"title": title, "artist": artist, "cover": "", "year": 0, "genre": 0}
    try:
        rows = dz("/search/album", q=f'artist:"{artist}" album:"{title}"', limit=10).get("data", [])
    except FAIL:
        rows = []
    want = fold(EDITION.sub("", title) or title)
    hit = next((r for r in rows if fold(EDITION.sub("", r.get("title", "")) or r.get("title", "")) == want), rows[0] if rows else None)
    me = _artist(artist)
    if hit:
        seed.update(cover=hit.get("cover_big", ""), genre=hit.get("genre_id", 0), deezer=hit["id"])
        if not me and hit.get("artist", {}).get("id"):
            me = {"deezer": hit["artist"]["id"], "name": hit["artist"].get("name", artist)}
        try:
            full = dz(f"/album/{hit['id']}")
            seed["year"] = int((full.get("release_date") or "0")[:4] or 0)
            seed["label"] = full.get("label", "")
            seed["genres"] = [g["name"] for g in full.get("genres", {}).get("data", [])]
        except FAIL:
            pass
    if not me:
        return {"seed": seed, "albums": [], "artists": []}
    near = related(me["deezer"])[:3]
    seeds = [{"name": me["name"], "deezer": me["deezer"], "weight": 1.0}] + [{"name": r["name"], "deezer": r["deezer"], "weight": 0.35} for r in near]
    rec = recommend(seeds, owned, {fold(artist), fold(me["name"])}, year=seed["year"], genre=seed["genre"], per_artist=1, artists=24)
    return {"seed": seed, "albums": rec["albums"][:24], "artists": rec["artists"][:12]}


def artist_top(artist_id: str, name: str, songs_in: Callable[[str, str], set]) -> dict:
    """An artist's most played songs and most loved albums on Deezer, marking songs already in the library."""
    who = identity(artist_id, name)
    out = {"picture": "", "fans": who["fans"], "genres": who["genres"], "names": who["names"], "songs": [], "albums": []}
    if not who["deezer"]:
        return out
    try:
        info = dz(f"/artist/{who['deezer']}")
        top = dz(f"/artist/{who['deezer']}/top", limit=40).get("data", [])
    except FAIL:
        return out
    out["picture"] = info.get("picture_xl") or info.get("picture_big") or ""
    seen, have = set(), {}
    for t in top:
        title = t.get("title_short") or t.get("title") or ""
        album = (t.get("album") or {}).get("title", "")
        if not title or title_key(title) in seen or not album:
            continue
        seen.add(title_key(title))
        base = EDITION.sub("", album).strip() or album
        credit = (t.get("artist") or {}).get("name") or who["name"]
        if base not in have:
            have[base] = set().union(*(songs_in(n, base) for n in dict.fromkeys([credit, who["name"], name])))
        out["songs"].append({"title": title, "version": t.get("title_version") or "", "album": base, "artist": credit,
                             "cover": (t.get("album") or {}).get("cover_medium", ""), "seconds": t.get("duration") or 0,
                             "rank": t.get("rank") or 0, "explicit": bool(t.get("explicit_lyrics")), "have": title_key(title) in have[base]})
        if len(out["songs"]) >= 15:
            break
    out["albums"] = [{"title": a["title"], "fans": a["fans"], "year": a["year"], "type": a["type"]}
                     for a in sorted(albums_of(who["deezer"], who["name"]), key=lambda a: -a["fans"])[:12]]
    return out
