import difflib
import io
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional, Tuple

import requests

from ..sources.musicbrainz import UA, Album, Track

def encode(src: str, dst: str, start: float = 0.0, end: float = 0.0) -> None:
    clip = ["-ss", f"{start:.3f}", "-to", f"{end:.3f}"] if end else []
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src, *clip, "-vn", "-map", "0:a:0", "-map_metadata", "-1",
           "-c:a", "libmp3lame", "-b:a", "320k", dst]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {res.stderr.strip()[:400]}")


def _fields(album: Album, t: Track) -> Dict[str, object]:
    return {
        "title": t.title, "artist": t.artist, "artists": t.artists,
        "albumartist": album.artist, "album": album.title,
        "track": t.number, "tracktotal": album.disc_counts.get(t.disc) or sum(1 for x in album.tracks if x.disc == t.disc),
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


def _clean(s: str) -> str:
    return re.sub(r"\s*[\(\[].*?[\)\]]", "", s or "").strip() or (s or "")


def _same(artist: str, title: str, got_artist: str, got_title: str) -> bool:
    want = f"{_clean(artist)} {_clean(title)}".lower()
    got = f"{got_artist} {_clean(got_title)}".lower()
    return difflib.SequenceMatcher(None, want, got).ratio() >= 0.6


def _search(url: str, params: dict) -> list:
    try:
        return requests.get(url, params=params, timeout=15, headers={"User-Agent": UA}).json().get("results", [])
    except (requests.RequestException, ValueError):
        return []


def _itunes(artist: str, title: str, size: int = 3000, tries: int = 3) -> Optional[bytes]:
    for term in dict.fromkeys((f"{artist} {title}", f"{artist} {_clean(title)}")):
        for res in _search("https://itunes.apple.com/search", {"term": term, "entity": "album", "limit": 8}):
            if res.get("artworkUrl100") and _same(artist, title, res.get("artistName", ""), res.get("collectionName", "")):
                data = _fetch(res["artworkUrl100"].replace("100x100bb", f"{size}x{size}bb"), tries)
                if data:
                    return data
    return None


def _deezer(artist: str, title: str, size: int = 1000, tries: int = 3) -> Optional[bytes]:
    try:
        rows = requests.get("https://api.deezer.com/search/album", timeout=15,
                            params={"q": f'artist:"{artist}" album:"{_clean(title)}"', "limit": 8}).json().get("data", [])
    except (requests.RequestException, ValueError):
        return None
    key = "cover_xl" if size > 500 else "cover_big"
    for res in rows:
        if res.get(key) and _same(artist, title, (res.get("artist") or {}).get("name", ""), res.get("title", "")):
            data = _fetch(res[key], tries)
            if data:
                return data
    return None


def _ytmusic(artist: str, title: str, size: int = 1200) -> Optional[bytes]:
    try:
        from ytmusicapi import YTMusic
        for res in YTMusic().search(f"{artist} {_clean(title)}", filter="albums", limit=6):
            thumbs = res.get("thumbnails") or []
            if thumbs and _same(artist, title, " ".join(a["name"] for a in res.get("artists", [])), res.get("title", "")):
                data = _fetch(re.sub(r"=w\d+-h\d+.*$", f"=w{size}-h{size}", thumbs[-1]["url"]), 2)
                if data:
                    return data
    except Exception:
        pass
    return None


def cover_jpeg(data: bytes, square: bool = True, max_side: int = 2400) -> bytes:
    """Any common image format in, a plain RGB JPEG out."""
    from PIL import Image, ImageOps
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise ValueError("that file is not an image I can read; try JPG, PNG or WebP")
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        img = Image.new("RGB", rgba.size, (255, 255, 255))
        img.paste(rgba, mask=rgba.split()[3])
    else:
        img = img.convert("RGB")
    if square and img.width != img.height:
        side = min(img.size)
        left, top = (img.width - side) // 2, (img.height - side) // 2
        img = img.crop((left, top, left + side, top + side))
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=95, optimize=True)
    return out.getvalue()


def set_cover(folder: str, data: bytes, square: bool = True) -> None:
    from mutagen.id3 import APIC, ID3
    big = cover_jpeg(data, square, 2400)
    small = cover_jpeg(big, False, 1400)
    path = os.path.join(folder, "cover.jpg")
    with open(path + ".part", "wb") as f:
        f.write(big)
    os.replace(path + ".part", path)
    for name in sorted(os.listdir(folder)):
        if name.lower().endswith(".mp3") and not name.startswith("."):
            tags = ID3(os.path.join(folder, name))
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Front Cover", data=small))
            tags.save(os.path.join(folder, name), v1=2, v2_version=3)


def _square(data: bytes) -> bytes:
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGB")
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    out = io.BytesIO()
    img.crop((left, top, left + side, top + side)).save(out, "JPEG", quality=95)
    return out.getvalue()


def fetch_cover(album: Album) -> Tuple[Optional[bytes], str]:
    if album.cover_url:
        for url in (album.cover_url, album.cover_url.replace("maxresdefault", "hqdefault")):
            data = _fetch(url, 2)
            if data:
                return _square(data), "YouTube thumbnail"
        return None, ""
    def caa(kind: str, mid: str) -> Optional[bytes]:
        return _fetch(f"https://coverartarchive.org/{kind}/{mid}/front")

    jobs = {"Cover Art Archive": lambda: caa("release", album.mbid) if album.mbid else None,
            "Cover Art Archive (group)": lambda: caa("release-group", album.group_id) if album.group_id else None,
            "iTunes": lambda: _itunes(album.artist, album.title),
            "Deezer": lambda: _deezer(album.artist, album.title)}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {name: pool.submit(fn) for name, fn in jobs.items()}
        found = {name: f.result() for name, f in futures.items() if f.result()}
    if not found:
        data = _ytmusic(album.artist, album.title)
        found = {"YouTube Music": data} if data else {}
    if not found:
        return None, ""
    from PIL import Image

    def area(d: bytes) -> int:
        w, h = Image.open(io.BytesIO(d)).size
        return w * h
    name = max(found, key=lambda n: area(found[n]))
    return to_jpeg(found[name]), name


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
    key = (release, group, artist, title, size)
    hit = _covers.get(key)
    if hit and time.time() - hit[0] < (86400 if hit[1] else 600):
        return hit[1]
    data = None
    for kind, mbid in (("release", release), ("release-group", group)):
        if mbid and not data:
            data = _fetch(f"https://coverartarchive.org/{kind}/{mbid}/front-{size}", tries=1, timeout=8)
    if not data and artist and title:
        data = _itunes(artist, title, size, tries=1) or _deezer(artist, title, size, tries=1)
    if len(_covers) > 400:
        _covers.pop(next(iter(_covers)))
    _covers[key] = (time.time(), data)
    return data


def fix_covers(out_root: str, log) -> None:
    from mutagen.id3 import APIC, ID3
    fixed = missing = 0
    for artist in sorted(os.scandir(out_root), key=lambda e: e.name.lower()):
        if not artist.is_dir() or artist.name.startswith("."):
            continue
        for alb in sorted(os.scandir(artist.path), key=lambda e: e.name.lower()):
            songs = sorted(f.path for f in os.scandir(alb.path) if f.name.lower().endswith(".mp3") and not f.name.startswith(".")) if alb.is_dir() and not alb.name.startswith(".") else []
            jpg = os.path.join(alb.path, "cover.jpg")
            if not songs or os.path.exists(jpg):
                continue
            try:
                id3 = ID3(songs[0])
                txx = {f.desc: str(f.text[0]) for f in id3.getall("TXXX") if f.text}
                name = str((id3.get("TPE2") or id3.get("TPE1") or [artist.name])[0])
                album = Album(mbid=txx.get("MusicBrainz Album Id", ""), group_id=txx.get("MusicBrainz Release Group Id", ""),
                              title=str((id3.get("TALB") or [alb.name])[0]), artist=name, artists=[name], artist_ids=[])
                data, source = fetch_cover(album)
            except Exception as exc:
                log("warn", f"{artist.name} - {alb.name}: {str(exc)[:100]}")
                continue
            if not data:
                missing += 1
                log("warn", f"no cover found anywhere: {artist.name} - {alb.name}")
                continue
            with open(jpg + ".part", "wb") as f:
                f.write(data)
            os.replace(jpg + ".part", jpg)
            small = to_jpeg(data, max_side=1400)
            for path in songs:
                try:
                    tags = ID3(path)
                    if not tags.getall("APIC"):
                        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Front Cover", data=small))
                        tags.save(path, v1=2, v2_version=3)
                except Exception:
                    pass
            fixed += 1
            log("ok", f"cover added from {source}: {artist.name} - {alb.name}")
    log("ok" if not missing else "warn", f"covers done: {fixed} added, {missing} still missing")
