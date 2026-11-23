import difflib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus

from yt_dlp import YoutubeDL

from .musicbrainz import Album, Track


BAD_WORDS = ("live", "remix", "cover", "karaoke", "instrumental", "sped up",
             "slowed", "reverb", "8d", "nightcore", "reaction", "tutorial",
             "mashup", "acoustic", "demo", "tribute", "piano version",
             "full album", "full ep", "full demo", "full length")


VERSION_WORDS = ("demo", "live", "remix", "acoustic", "instrumental", "extended",
                 "edit", "reprise", "excerpt", "excerpts", "single", "radio",
                 "mix", "session", "karaoke", "cover")
SOFT = {"demo"}
MIN_SCORE = 45
MAX_DURATION_DIFF = 10

NOTES: Dict[str, str] = {}
_gate = threading.Semaphore(3)


def ydl(opts: dict) -> YoutubeDL:
    backoff = lambda n: min(2 ** n, 30)
    return YoutubeDL({"quiet": True, "no_warnings": True, "socket_timeout": 20,
                      "retries": 10, "fragment_retries": 10, "extractor_retries": 3,
                      "retry_sleep_functions": {"http": backoff, "fragment": backoff, "extractor": backoff}, **opts})


def core(s: str) -> str:
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", (s or "").lower())
    words = " ".join(re.sub(r"[^\w]+", " ", s).split())
    return words or re.sub(r"\s+", "", (s or "").replace("\u2026", "..."))


def compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", core(s))


def version_words(title: str) -> set:
    low = (title or "").lower()
    return {w for w in VERSION_WORDS if re.search(rf"\b{re.escape(w)}\b", low)}


def title_variants(title: str) -> List[str]:
    """"Song / Hidden Song" is usually listed as just "Song" on streaming sites, so the first part is also an acceptable title."""
    parts = [p.strip() for p in re.split(r"\s+/{1,2}\s+", title or "") if p.strip()]
    return [title, parts[0]] if len(parts) > 1 else [title]


def title_matches(candidate: str, wanted: str, album_title: str = "", loose: bool = False) -> bool:
    return any(_title_matches(candidate, w, album_title, loose) for w in title_variants(wanted))


def _title_matches(candidate: str, wanted: str, album_title: str = "", loose: bool = False) -> bool:
    tt, ct = core(wanted), core(candidate)
    if not tt or not ct or version_words(candidate) - version_words(album_title) - (SOFT if loose else set()) != version_words(wanted):
        return False
    return (tt == ct or difflib.SequenceMatcher(None, tt, ct).ratio() >= 0.85
            or re.search(rf"\b{re.escape(tt)}\b", ct) is not None)


def search(query: str, n: int = 8) -> List[dict]:
    with _gate, ydl({"extract_flat": True, "skip_download": True}) as y:
        info = y.extract_info(f"ytsearch{n}:{query}", download=False)
    return [e for e in (info or {}).get("entries", []) if e]


def playlist_entries(url: str) -> List[dict]:
    with ydl({"extract_flat": True, "skip_download": True}) as y:
        info = y.extract_info(url, download=False) or {}
    return [e for e in info.get("entries", []) if e]


def find_album_playlist(album: Album) -> Optional[str]:
    from ytmusicapi import YTMusic
    yt = YTMusic()
    results = yt.search(f"{album.artist} {album.title}", filter="albums", limit=10)
    cands = [r for r in results
             if core(r.get("title", "")) == core(album.title)
             and any(compact(a["name"]) in compact(album.artist) or
                     compact(album.artist) in compact(a["name"])
                     for a in r.get("artists", []))][:3]
    best = None
    for r in cands:
        info = yt.get_album(r["browseId"])
        pid, n = info.get("audioPlaylistId"), info.get("trackCount")
        if not pid or not n:
            continue
        diff = abs(n - len(album.tracks))
        if diff <= 6 and (best is None or diff < best[0]):
            best = (diff, pid)
        if diff == 0:
            break
    return f"https://www.youtube.com/playlist?list={best[1]}" if best else None


NUM = re.compile(r"^\W*(?:track|tr\.?|#)?\s*0*(\d{1,3})\b[\s\].:)\-–_]*", re.I)


def clean_titles(titles: List[str], shared: bool = True) -> List[Tuple[int, str]]:
    """Strip what every title shares (the album name) and the track numbers, so only the song names are left."""
    prefix = os.path.commonprefix(titles) if shared and len(titles) > 1 else ""
    prefix = prefix[:max(prefix.rfind(c) for c in " ])-") + 1]
    out = []
    for n, raw in enumerate(titles, 1):
        rest = re.sub(r"^\W*\d{1,2}:\d{2}(?::\d{2})?\b[\s\-–.)]*", "", raw[len(prefix):].strip())
        m = NUM.match(rest)
        number, rest = (int(m.group(1)), rest[m.end():]) if m else (n, rest)
        out.append((number, rest.strip(" -–_[]()") or raw))
    return out


def import_playlist(url: str) -> dict:
    """Read a YouTube playlist (or one video) so it can be turned into an album by hand."""
    host = re.match(r"https?://(?:www\.|m\.|music\.)?(youtube\.com|youtu\.be)/", url)
    if not host:
        raise ValueError("that is not a YouTube link")
    pl = re.search(r"[?&]list=([\w-]+)", url)
    with ydl({"extract_flat": True, "skip_download": True}) as y:
        info = y.extract_info(f"https://www.youtube.com/playlist?list={pl.group(1)}" if pl else url, download=False) or {}
    entries = [e for e in info.get("entries", [info]) if e and e.get("id") and e.get("title")]
    if not entries:
        raise ValueError("no videos found in that link")
    chapters = []
    if len(entries) == 1:
        with ydl({"skip_download": True}) as y:
            video = y.extract_info(f"https://www.youtube.com/watch?v={entries[0]['id']}", download=False) or {}
        chapters = [c for c in video.get("chapters") or [] if c.get("title") and c.get("end_time")]
        info = dict(info, title=info.get("title") if pl else video.get("title") or entries[0]["title"])
    name = re.sub(r"\s*[\[(]\s*(?:official\s+)?(?:OST|Soundtrack|Full\s+(?:Album|EP))(?:\s*[-–]\s*HD|\s+Stream)?\s*[\])]\s*", " ", info.get("title") or entries[0]["title"], flags=re.I)
    name = re.sub(r"^\s*\[[^\]]*\]\s*", "", name)
    found_year = re.search(r"\s[-–]\s*((?:19|20)\d{2})\s*$", name.strip(" -–|"))
    name = (name.strip(" -–|")[:found_year.start()] if found_year else name).strip(" -–|")
    artist = name.split(" - ", 1)[0].strip() if " - " in name else "Various Artists"
    album = name.split(" - ", 1)[1].strip() if " - " in name else name
    if len(chapters) > 1:
        vid = entries[0]["id"]
        tracks = [{"id": vid, "title": t, "number": n, "duration": round(c["end_time"] - c["start_time"]), "start": c["start_time"], "end": c["end_time"]}
                  for c, (n, t) in zip(chapters, clean_titles([c["title"] for c in chapters], shared=False))]
        tracks = [dict(t, number=i) for i, t in enumerate(tracks, 1)]
    else:
        tracks = [{"id": e["id"], "title": t, "number": n, "duration": e.get("duration")}
                  for e, (n, t) in zip(entries, clean_titles([e["title"] for e in entries]))]
    if len(tracks) == 1 and not tracks[0].get("end"):
        tracks[0]["title"] = album
    return {"artist": artist, "album": album, "year": found_year.group(1) if found_year else "", "genre": "Soundtrack" if re.search(r"\bOST\b|soundtrack", info.get("title") or "", re.I) else "",
            "thumb": f"https://i.ytimg.com/vi/{entries[0]['id']}/maxresdefault.jpg", "tracks": tracks}


def search_uploads(query: str) -> List[dict]:
    """Playlists and long videos on YouTube itself, for music that is not on YouTube Music."""
    def playlists() -> List[dict]:
        with ydl({"extract_flat": True, "skip_download": True, "playlistend": 10}) as y:
            info = y.extract_info(f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp=EgIQAw%3D%3D", download=False) or {}
        return [{"kind": "playlist", "url": e["url"], "title": e.get("title") or "", "channel": e.get("channel") or e.get("uploader") or "",
                 "thumb": (e.get("thumbnails") or [{}])[0].get("url", ""), "duration": None}
                for e in info.get("entries", []) if e and e.get("url") and "list=" in e["url"]][:8]

    def videos() -> List[dict]:
        return [{"kind": "video", "url": f"https://www.youtube.com/watch?v={e['id']}", "title": e.get("title") or "", "channel": e.get("channel") or e.get("uploader") or "",
                 "thumb": f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg", "duration": e.get("duration")}
                for e in search(f"{query} full album", 10) if e.get("id") and (e.get("duration") or 0) >= 600][:5]

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(playlists), pool.submit(videos)
        out = []
        for f in (a, b):
            try:
                out += f.result()
            except Exception:
                pass
    return out


def ytm_search(query: str) -> List[dict]:
    from ytmusicapi import YTMusic
    out = []
    for r in YTMusic().search(query, filter="albums", limit=12):
        if r.get("browseId"):
            out.append({"id": r["browseId"], "title": r.get("title", ""), "artist": " & ".join(a["name"] for a in r.get("artists", [])) or "Unknown",
                        "type": r.get("type") or "Album", "secondary": [], "date": str(r.get("year") or ""),
                        "thumb": (r.get("thumbnails") or [{}])[-1].get("url", ""), "ytm": True})
    return out


def ytm_album(browse_id: str) -> Album:
    from ytmusicapi import YTMusic
    a = YTMusic().get_album(browse_id)
    names = [x["name"] for x in a.get("artists", [])] or ["Unknown"]
    album = Album(mbid="", group_id="", title=a["title"], artist=" & ".join(names), artists=names, artist_ids=[],
                  date=str(a.get("year") or ""), type=a.get("type") or "Album")
    for n, t in enumerate((t for t in a.get("tracks", []) if t.get("videoId") and t.get("isAvailable", True)), 1):
        who = [x["name"] for x in t.get("artists", []) if x.get("name")] or names
        album.tracks.append(Track(title=t["title"], number=n, disc=1, length_ms=(t.get("duration_seconds") or 0) * 1000 or None,
                                  artist=" & ".join(who), artists=who, artist_ids=[], recording_id="", track_id=t["videoId"]))
    if not album.tracks:
        raise RuntimeError("that album has no downloadable tracks")
    return album


def score(entry: dict, track: Track, album: Album, loose: bool = False) -> float:
    title = entry.get("title") or ""
    channel = entry.get("channel") or entry.get("uploader") or ""
    expected = f"{track.title} {album.title}".lower()
    s = 0.0
    dur = entry.get("duration")
    if track.length_ms and dur:
        d = abs(dur - track.length_ms / 1000)
        if d > MAX_DURATION_DIFF:
            return -100
        s += 50 if d <= 2 else 30 if d <= 5 else 5
    if channel.endswith("- Topic"):
        s += 40
    if any(core(a) and core(a) in core(f"{channel} {title}") for a in track.artists):
        s += 15
    ct = core(title)
    tt = max((core(v) for v in title_variants(track.title)), key=lambda v: (bool(v) and v in ct, difflib.SequenceMatcher(None, v, ct).ratio()))
    if tt:
        s += 30 if tt in ct else 30 * difflib.SequenceMatcher(None, tt, ct).ratio()
    low = title.lower()
    for w in BAD_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", low) and w not in expected and not (loose and w in SOFT):
            s -= 60
    if tt and tt == ct:
        s += 10
    if "lyric" in low and "lyric" not in expected:
        s -= 25
    if "official audio" in low:
        s += 10
    if "music video" in low or "official video" in low:
        s -= 10
    return s


def by_artist(entry: dict, track: Track, album: Album) -> bool:
    text = core(f"{entry.get('channel') or entry.get('uploader') or ''} {entry.get('title') or ''}")
    return any(core(a) and core(a) in text for a in track.artists + [album.artist])


def bad_version(title: str, track: Track, album: Album, loose: bool = False) -> bool:
    low, expected = title.lower(), f"{track.title} {album.title}".lower()
    return any(re.search(rf"\b{re.escape(w)}\b", low) and w not in expected and not (loose and w in SOFT)
               for w in BAD_WORDS)


def official_reason(info: dict, track: Track, album: Album) -> Optional[str]:
    chan = info.get("channel") or info.get("uploader") or ""
    desc = info.get("description") or ""
    title = info.get("title") or ""
    if bad_version(title, track, album):
        return None
    m = re.search(r"Provided to YouTube by (.+)", desc)
    if m and "Auto-generated by YouTube" in desc:

        lines = [l.strip() for l in desc.split("\n") if l.strip()]
        credit = next((l for l in lines if " · " in l), "")
        artist_ok = any(compact(a) in compact(credit) for a in track.artists + [album.artist])
        if artist_ok:
            alb = lines[lines.index(credit) + 1] if credit in lines[:-1] else ""
            return f"label-delivered Topic track ({m.group(1).strip()}; album: {alb or '?'})"
    if info.get("channel_is_verified"):
        cc = compact(chan)
        if any(compact(a) and compact(a) in cc for a in track.artists + [album.artist]):
            return "verified artist channel"
        if album.label and compact(album.label) and compact(album.label) in cc:
            return "verified label channel"
    return None


_infos: dict = {}


def video_info(entry: dict) -> Optional[dict]:
    if entry.get("id") in _infos:
        return _infos[entry["id"]]
    info = _video_info(entry)
    if info and len(_infos) < 500:
        _infos[entry["id"]] = info
    return info


def _video_info(entry: dict) -> Optional[dict]:
    for attempt in range(2):
        for extra in ({}, {"extractor_args": {"youtube": {"player_client": ["web_embedded"]}}}):
            try:
                with _gate, ydl({"skip_download": True, **extra}) as y:
                    return y.extract_info(entry_url(entry), download=False) or None
            except Exception:
                continue
        time.sleep(2 * (attempt + 1))
    return None


def match_playlist(track: Track, entries: List[dict], index: int, album: Album, skip: frozenset = frozenset()) -> Optional[dict]:
    best = None
    for e in entries:
        if e.get("id") in skip or not title_matches(e.get("title") or "", track.title, album.title):
            continue
        ratio = max(difflib.SequenceMatcher(None, core(v), core(e["title"])).ratio() for v in title_variants(track.title))
        dur = e.get("duration")
        gap = abs(dur - track.length_ms / 1000) if dur and track.length_ms else 0
        key = (ratio, e.get("_index") == index, -gap)
        if best is None or key > best[0]:
            best = (key, e)
    if best:
        return best[1]
    if len(entries) == len(album.tracks) and 0 < index <= len(entries):
        e = entries[index - 1]
        dur = e.get("duration")
        if e.get("id") in skip:
            return None
        if not (dur and track.length_ms) or abs(dur - track.length_ms / 1000) <= MAX_DURATION_DIFF:
            return dict(e, by_position=True)
    return None


def find_song(track: Track, album: Album, skip: frozenset = frozenset()) -> Optional[dict]:
    """YouTube Music song search: official uploads that name this album, matched by album, artist and length."""
    from ytmusicapi import YTMusic
    for r in YTMusic().search(f"{album.artist} {album.title} {track.title}", filter="songs", limit=12):
        if not r.get("videoId") or r["videoId"] in skip or core((r.get("album") or {}).get("name", "")) != core(album.title):
            continue
        if not any(compact(a["name"]) in compact(album.artist) or compact(album.artist) in compact(a["name"]) for a in r.get("artists", [])):
            continue
        dur = r.get("duration_seconds")
        if track.length_ms and dur and abs(dur - track.length_ms / 1000) > MAX_DURATION_DIFF:
            continue
        if title_matches(r.get("title", ""), track.title, album.title):
            return {"id": r["videoId"], "title": r["title"], "channel": " & ".join(a["name"] for a in r["artists"]), "duration": dur}
    return None


def search_official(track: Track, album: Album, skip: frozenset = frozenset()) -> Optional[Tuple[dict, str]]:
    artist = track.artists[0]
    variants = title_variants(track.title)
    name = variants[1] if len(variants) > 1 else track.title
    queries = [f"{artist} {name} {album.title}", f"{artist} {name} official audio",
               f"{artist} - Topic {name}", f"{artist} - {name}"]
    seen, pool = set(skip), []
    found = {"official": None, "unofficial": None, "unchecked": 0}

    def scan(entries: List[dict], loose: bool):
        ranked = []
        for e in entries:
            sc = score(e, track, album, loose)
            if sc < MIN_SCORE:
                continue
            title = e.get("title") or ""
            if title_matches(title, track.title, album.title, loose) or (
                    (e.get("channel") or e.get("uploader"))
                    and any(core(v) and re.search(rf"\b{re.escape(core(v))}\b", core(title))
                            and version_words(title) - version_words(album.title) - (SOFT if loose else set()) == version_words(v) for v in title_variants(track.title))):
                ranked.append((sc, e))
        for sc, e in sorted(ranked, key=lambda x: -x[0])[:4]:
            info = video_info(e)
            if info is None:
                found["unchecked"] += 1
                continue
            dur = info.get("duration")
            if track.length_ms and dur and abs(dur - track.length_ms / 1000) > MAX_DURATION_DIFF:
                continue
            if not track.length_ms and dur and dur > 900:
                continue
            why = official_reason(info, track, album)
            e = dict(e, channel=info.get("channel") or e.get("channel"), duration=dur or e.get("duration"))
            if not why:
                if found["unofficial"] is None and by_artist(e, track, album) and not bad_version(e.get("title") or "", track, album, loose):
                    found["unofficial"] = (e, "unofficial upload (no official source found)")
            elif why.startswith("label-delivered"):
                return e, why
            elif found["official"] is None:
                found["official"] = (e, why)
        return None

    for q in queries:
        new = []
        for e in search(q):
            if e.get("id") not in seen:
                seen.add(e.get("id"))
                new.append(e)
        pool += new
        hit = scan(new, False)
        if hit:
            return hit
    if not (found["official"] or found["unofficial"]):
        hit = scan(pool, True)
        if hit:
            return hit
    hit = found["official"] or found["unofficial"]
    if hit is None and found["unchecked"]:
        NOTES[track.track_id] = f"{found['unchecked']} candidate(s) could not be checked because YouTube refused the request"
    return hit


def entry_url(entry: dict) -> str:
    return entry.get("webpage_url") or entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}"


CLIENTS = (None, ("web_embedded",), ("tv",), ("web_safari",), ("android_vr",))
_good_client = [None]


def download(url: str, tmpdir: str) -> Tuple[str, str, float]:
    opts = {"format": "bestaudio/best", "noprogress": True, "noplaylist": True,
            "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s")}
    last: Optional[Exception] = None


    for clients in sorted(CLIENTS, key=lambda c: c != _good_client[0]):
        o = dict(opts)
        if clients:
            o["extractor_args"] = {"youtube": {"player_client": list(clients)}}
        try:
            with ydl(o) as y:
                info = y.extract_info(url, download=True)
            dl = info["requested_downloads"][0]
            _good_client[0] = clients
            return (dl["filepath"], dl.get("acodec") or info.get("acodec") or "",
                    dl.get("abr") or info.get("abr") or 0)
        except Exception as exc:
            last = exc
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
    raise last
