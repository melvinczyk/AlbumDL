import difflib
import io
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional

import requests

from ..sources.musicbrainz import UA, Album, Track

def encode(src: str, dst: str) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vn", "-map", "0:a:0", "-map_metadata", "-1",
           "-c:a", "libmp3lame", "-b:a", "320k", dst]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {res.stderr.strip()[:400]}")


def _fields(album: Album, t: Track) -> Dict[str, object]:
    return {
        "title": t.title, "artist": t.artist, "artists": t.artists,
        "albumartist": album.artist, "album": album.title,
        "track": t.number, "tracktotal": sum(1 for x in album.tracks if x.disc == t.disc),
        "disc": t.disc, "disctotal": album.disc_total,
        "date": album.date or album.year, "originaldate": album.original_date,
        "genres": album.genres,
        "label": album.label, "catalog": album.catalog, "barcode": album.barcode,
        "country": album.country, "status": album.status, "type": album.type,
        "compilation": album.compilation,
        "mb_album": album.mbid, "mb_group": album.group_id,
        "mb_track": t.track_id, "mb_recording": t.recording_id,
        "mb_artist": t.artist_ids, "mb_albumartist": album.artist_ids,
        "length_ms": t.length_ms or "", "media": album.media_format,
        "language": album.language, "script": album.script, "asin": album.asin,
        "artist_sort": t.artist_sort, "albumartist_sort": album.artist_sort,
    }


def tag(path: str, album: Album, t: Track, cover: bytes, source_url: str = "") -> None:
    f = _fields(album, t)
    f["source_url"] = source_url
    _tag_mp3(path, f, cover)


def _tag_mp3(path: str, f: dict, cover: bytes) -> None:
    from mutagen.id3 import (APIC, COMM, ID3, TALB, TCMP, TCON, TDOR, TDRC, TENC,
                             TIT2, TLAN, TLEN, TMED, TPE1, TPE2, TPOS, TPUB, TRCK,
                             TXXX, UFID, WOAS, ID3NoHeaderError)
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        id3 = ID3()
    id3.add(TIT2(encoding=3, text=f["title"]))
    id3.add(TPE1(encoding=3, text=f["artists"] if len(f["artists"]) > 1 else f["artist"]))
    id3.add(TPE2(encoding=3, text=f["albumartist"]))
    id3.add(TALB(encoding=3, text=f["album"]))
    id3.add(TRCK(encoding=3, text=f"{f['track']}/{f['tracktotal']}"))
    id3.add(TPOS(encoding=3, text=f"{f['disc']}/{f['disctotal']}"))
    if f["date"]:
        id3.add(TDRC(encoding=3, text=f["date"]))
    if f["originaldate"]:
        id3.add(TDOR(encoding=3, text=f["originaldate"]))
    if f["genres"]:

        std = [g for g in f["genres"] if g in TCON.GENRES]
        id3.add(TCON(encoding=3, text=std[:1] + [g for g in f["genres"] if g not in std[:1]]))
    if f["length_ms"]:
        id3.add(TLEN(encoding=3, text=str(f["length_ms"])))
    if f["media"]:
        id3.add(TMED(encoding=3, text=f["media"]))
    if f["language"]:
        id3.add(TLAN(encoding=3, text=f["language"]))
    id3.add(TENC(encoding=3, text="albumdl"))
    id3.add(COMM(encoding=3, lang="eng", desc="", text="Downloaded with albumdl"))
    if f["source_url"]:
        id3.add(WOAS(url=f["source_url"]))
    if f["label"]:
        id3.add(TPUB(encoding=3, text=f["label"]))
    if f["compilation"]:
        id3.add(TCMP(encoding=3, text="1"))
    txxx = {"MusicBrainz Album Id": f["mb_album"],
            "MusicBrainz Release Group Id": f["mb_group"],
            "MusicBrainz Release Track Id": f["mb_track"],
            "MusicBrainz Artist Id": f["mb_artist"],
            "MusicBrainz Album Artist Id": f["mb_albumartist"],
            "MusicBrainz Album Status": f["status"].lower(),
            "MusicBrainz Album Type": f["type"].lower(),
            "MusicBrainz Album Release Country": f["country"],
            "BARCODE": f["barcode"], "CATALOGNUMBER": f["catalog"],
            "ASIN": f["asin"], "SCRIPT": f["script"],
            "ARTISTSORT": f["artist_sort"], "ALBUMARTISTSORT": f["albumartist_sort"],
            "Source URL": f["source_url"]}
    for desc, val in txxx.items():
        if val:
            id3.add(TXXX(encoding=3, desc=desc, text=val if isinstance(val, list) else [val]))
    if f["mb_recording"]:
        id3.add(UFID(owner="http://musicbrainz.org", data=f["mb_recording"].encode()))
    if cover:
        id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Front Cover", data=cover))
    id3.save(path, v1=2, v2_version=3)


def _fetch(url: str, tries: int = 3, timeout: int = 25) -> Optional[bytes]:
    for attempt in range(tries):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        except requests.RequestException:
            if attempt + 1 < tries:
                time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 200:
            return r.content if len(r.content) >= 1000 else None
        if r.status_code < 500 and r.status_code != 429:
            return None
        if attempt + 1 < tries:
            time.sleep(2 * (attempt + 1))
    return None


def _itunes(artist: str, title: str, size: int = 3000, tries: int = 3) -> Optional[bytes]:
    try:
        r = requests.get("https://itunes.apple.com/search", timeout=15,
                         params={"term": f"{artist} {title}", "entity": "album", "limit": 5})
        results = r.json().get("results", [])
    except (requests.RequestException, ValueError):
        return None
    want = f"{artist} {title}".lower()
    for res in results:
        got = f"{res.get('artistName', '')} {res.get('collectionName', '')}".lower()
        if difflib.SequenceMatcher(None, want, got).ratio() >= 0.6 and res.get("artworkUrl100"):
            data = _fetch(res["artworkUrl100"].replace("100x100bb", f"{size}x{size}bb"), tries)
            if data:
                return data
    return None


def fetch_cover(album: Album) -> Optional[bytes]:
    def caa(kind: str, mid: str) -> Optional[bytes]:
        return _fetch(f"https://coverartarchive.org/{kind}/{mid}/front")


    pool = ThreadPoolExecutor(max_workers=3)
    rel = pool.submit(caa, "release", album.mbid)
    grp = pool.submit(caa, "release-group", album.group_id) if album.group_id else None
    itn = pool.submit(_itunes, album.artist, album.title)
    first = rel.result() or (grp.result() if grp else None)
    found = [d for d in (first, itn.result()) if d]
    pool.shutdown(wait=False)
    if not found:
        return None
    from PIL import Image

    def area(d: bytes) -> int:
        w, h = Image.open(io.BytesIO(d)).size
        return w * h
    return to_jpeg(max(found, key=area))


def to_jpeg(data: bytes, max_side: Optional[int] = None) -> bytes:
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    is_jpeg = data.startswith(b"\xff\xd8")
    if is_jpeg and (not max_side or max(img.size) <= max_side):
        return data
    img = img.convert("RGB")
    if max_side and max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=95)
    return out.getvalue()


_covers: dict = {}


def cover_for(release: str, group: str, artist: str, title: str, size: int) -> Optional[bytes]:
    """A small cover for the web page: Cover Art Archive first, then iTunes. Cached, including misses."""
    key = (release, group, size)
    hit = _covers.get(key)
    if hit and time.time() - hit[0] < (86400 if hit[1] else 600):
        return hit[1]
    data = None
    for kind, mbid in (("release", release), ("release-group", group)):
        if mbid and not data:
            data = _fetch(f"https://coverartarchive.org/{kind}/{mbid}/front-{size}", tries=1, timeout=8)
    if not data and artist and title:
        data = _itunes(artist, title, size, tries=1)
    if len(_covers) > 400:
        _covers.pop(next(iter(_covers)))
    _covers[key] = (time.time(), data)
    return data
