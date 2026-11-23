import difflib
import os
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

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
    return " ".join(re.sub(r"[^\w]+", " ", s).split())


def compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", core(s))


def version_words(title: str) -> set:
    low = (title or "").lower()
    return {w for w in VERSION_WORDS if re.search(rf"\b{re.escape(w)}\b", low)}


def title_matches(candidate: str, wanted: str, album_title: str = "", loose: bool = False) -> bool:
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
    tt, ct = core(track.title), core(title)
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


def match_playlist(track: Track, entries: List[dict], index: int, album: Album) -> Optional[dict]:
    best = None
    for e in entries:
        if not title_matches(e.get("title") or "", track.title, album.title):
            continue
        ratio = difflib.SequenceMatcher(None, core(track.title), core(e["title"])).ratio()
        dur = e.get("duration")
        gap = abs(dur - track.length_ms / 1000) if dur and track.length_ms else 0
        key = (ratio, e.get("_index") == index, -gap)
        if best is None or key > best[0]:
            best = (key, e)
    return best[1] if best else None


def search_official(track: Track, album: Album) -> Optional[Tuple[dict, str]]:
    artist = track.artists[0]
    queries = [f"{artist} {track.title} {album.title}", f"{artist} {track.title} official audio",
               f"{artist} - Topic {track.title}", f"{artist} - {track.title}"]
    seen, pool = set(), []
    found = {"official": None, "unofficial": None, "unchecked": 0}

    def scan(entries: List[dict], loose: bool):
        ranked = []
        for e in entries:
            sc = score(e, track, album, loose)
            if sc < MIN_SCORE:
                continue
            title = e.get("title") or ""
            if title_matches(title, track.title, album.title, loose) or (
                    (e.get("channel") or e.get("uploader")) and re.search(rf"\b{re.escape(core(track.title))}\b", core(title))
                    and version_words(title) - version_words(album.title) - (SOFT if loose else set()) == version_words(track.title)):
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
