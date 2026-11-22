import base64
import difflib
import io
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import requests

from ..sources.musicbrainz import UA, Album, Track

DEFAULT_BITRATE = {"mp3": 320, "m4a": 256, "opus": 192}


def encode(src: str, dst: str, fmt: str, bitrate: int, acodec: str) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vn",
           "-map", "0:a:0", "-map_metadata", "-1"]
    if fmt == "mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", f"{bitrate}k"]
    elif fmt == "m4a":
        cmd += ["-c:a", "copy"] if acodec.startswith("mp4a") else ["-c:a", "aac", "-b:a", f"{bitrate}k"]
    else:
        cmd += ["-c:a", "copy"] if acodec == "opus" else ["-c:a", "libopus", "-b:a", f"{bitrate}k"]
    cmd.append(dst)
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
        "genres": album.genres, "isrc": t.isrcs[0] if t.isrcs else "",
        "label": album.label, "catalog": album.catalog, "barcode": album.barcode,
        "country": album.country, "status": album.status, "type": album.type,
        "compilation": album.compilation,
        "mb_album": album.mbid, "mb_group": album.group_id,
        "mb_track": t.track_id, "mb_recording": t.recording_id,
        "mb_artist": t.artist_ids, "mb_albumartist": album.artist_ids,
        "length_ms": t.length_ms or "", "media": album.media_format,
        "language": album.language, "script": album.script, "asin": album.asin,
        "artist_sort": t.artist_sort, "albumartist_sort": album.artist_sort,
        "composers": t.composers, "lyricists": t.lyricists,
        "producers": t.producers, "mixers": t.mixers, "engineers": t.engineers,
        "performers": t.performers,
    }


def tag(path: str, fmt: str, album: Album, t: Track, cover: bytes,
        source_url: str = "") -> None:
    f = _fields(album, t)
    f["source_url"] = source_url
    {"mp3": _tag_mp3, "m4a": _tag_m4a, "opus": _tag_opus}[fmt](path, f, cover)


def _tag_mp3(path: str, f: dict, cover: bytes) -> None:
    from mutagen.id3 import (APIC, COMM, ID3, TALB, TCMP, TCOM, TCON, TDOR,
                             TDRC, TENC, TEXT, TIPL, TIT2, TLAN, TLEN, TMCL,
                             TMED, TPE1, TPE2, TPOS, TPUB, TRCK, TSRC, TXXX,
                             UFID, WOAS, ID3NoHeaderError)
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
    if f["composers"]:
        id3.add(TCOM(encoding=3, text=f["composers"]))
    if f["lyricists"]:
        id3.add(TEXT(encoding=3, text=f["lyricists"]))
    people = [[role, n] for role, names in (("producer", f["producers"]), ("mix", f["mixers"]),
              ("engineer", f["engineers"])) for n in names]
    if people:
        id3.add(TIPL(encoding=3, people=people))
    if f["performers"]:
        id3.add(TMCL(encoding=3, people=[list(p) for p in f["performers"]]))
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
    if f["isrc"]:
        id3.add(TSRC(encoding=3, text=f["isrc"]))
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
            "PRODUCER": f["producers"], "MIXER": f["mixers"], "ENGINEER": f["engineers"],
            "Source URL": f["source_url"]}
    for desc, val in txxx.items():
        if val:
            id3.add(TXXX(encoding=3, desc=desc, text=val if isinstance(val, list) else [val]))
    if f["mb_recording"]:
        id3.add(UFID(owner="http://musicbrainz.org", data=f["mb_recording"].encode()))
    if cover:
        id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Front Cover", data=cover))
    id3.save(path, v1=2, v2_version=3)


def _ff(v: object) -> List[bytes]:
    vals = v if isinstance(v, list) else [v]
    return [str(x).encode() for x in vals if x]


def _tag_m4a(path: str, f: dict, cover: bytes) -> None:
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
    m = MP4(path)
    m["\xa9nam"] = [f["title"]]
    m["\xa9ART"] = [f["artist"]]
    m["aART"] = [f["albumartist"]]
    m["\xa9alb"] = [f["album"]]
    m["trkn"] = [(f["track"], f["tracktotal"])]
    m["disk"] = [(f["disc"], f["disctotal"])]
    if f["date"]:
        m["\xa9day"] = [f["date"]]
    if f["genres"]:
        m["\xa9gen"] = [", ".join(f["genres"])]
    if f["compilation"]:
        m["cpil"] = True
    if f["composers"]:
        m["\xa9wrt"] = [", ".join(f["composers"])]
    m["\xa9too"] = ["albumdl"]
    m["\xa9cmt"] = ["Downloaded with albumdl"]
    if f["artist_sort"]:
        m["soar"] = [f["artist_sort"]]
    if f["albumartist_sort"]:
        m["soaa"] = [f["albumartist_sort"]]
    free = {"ISRC": f["isrc"], "LABEL": f["label"], "CATALOGNUMBER": f["catalog"],
            "LYRICIST": f["lyricists"], "PRODUCER": f["producers"], "MIXER": f["mixers"],
            "ENGINEER": f["engineers"], "MEDIA": f["media"], "LANGUAGE": f["language"],
            "SCRIPT": f["script"], "ASIN": f["asin"], "SOURCE URL": f["source_url"],
            "PERFORMER": [f"{n} ({r})" for r, n in f["performers"]],
            "BARCODE": f["barcode"], "ORIGINALDATE": f["originaldate"],
            "MusicBrainz Album Id": f["mb_album"],
            "MusicBrainz Release Group Id": f["mb_group"],
            "MusicBrainz Release Track Id": f["mb_track"],
            "MusicBrainz Track Id": f["mb_recording"],
            "MusicBrainz Artist Id": f["mb_artist"],
            "MusicBrainz Album Artist Id": f["mb_albumartist"],
            "MusicBrainz Album Status": f["status"].lower(),
            "MusicBrainz Album Type": f["type"].lower(),
            "MusicBrainz Album Release Country": f["country"]}
    for k, v in free.items():
        vals = _ff(v)
        if vals:
            m[f"----:com.apple.iTunes:{k}"] = [MP4FreeForm(x) for x in vals]
    if cover:
        m["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    m.save()


def _tag_opus(path: str, f: dict, cover: bytes) -> None:
    from mutagen.flac import Picture
    from mutagen.oggopus import OggOpus
    from PIL import Image
    o = OggOpus(path)
    vc = {"TITLE": f["title"], "ARTIST": f["artists"] if len(f["artists"]) > 1 else f["artist"],
          "ALBUMARTIST": f["albumartist"], "ALBUM": f["album"],
          "TRACKNUMBER": f["track"], "TRACKTOTAL": f["tracktotal"],
          "DISCNUMBER": f["disc"], "DISCTOTAL": f["disctotal"],
          "DATE": f["date"], "ORIGINALDATE": f["originaldate"],
          "GENRE": f["genres"], "ISRC": f["isrc"], "LABEL": f["label"],
          "CATALOGNUMBER": f["catalog"], "BARCODE": f["barcode"],
          "RELEASECOUNTRY": f["country"], "RELEASESTATUS": f["status"].lower(),
          "RELEASETYPE": f["type"].lower(),
          "COMPILATION": "1" if f["compilation"] else "",
          "COMPOSER": f["composers"], "LYRICIST": f["lyricists"],
          "PRODUCER": f["producers"], "MIXER": f["mixers"], "ENGINEER": f["engineers"],
          "PERFORMER": [f"{n} ({r})" for r, n in f["performers"]],
          "MEDIA": f["media"], "LANGUAGE": f["language"], "SCRIPT": f["script"],
          "ASIN": f["asin"], "ARTISTSORT": f["artist_sort"],
          "ALBUMARTISTSORT": f["albumartist_sort"], "SOURCE": f["source_url"],
          "COMMENT": "Downloaded with albumdl", "ENCODER": "albumdl",
          "MUSICBRAINZ_ALBUMID": f["mb_album"],
          "MUSICBRAINZ_RELEASEGROUPID": f["mb_group"],
          "MUSICBRAINZ_RELEASETRACKID": f["mb_track"],
          "MUSICBRAINZ_TRACKID": f["mb_recording"],
          "MUSICBRAINZ_ARTISTID": f["mb_artist"],
          "MUSICBRAINZ_ALBUMARTISTID": f["mb_albumartist"]}
    for k, v in vc.items():
        vals = [str(x) for x in (v if isinstance(v, list) else [v]) if x]
        if vals:
            o[k] = vals
    if cover:
        pic = Picture()
        pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "Front Cover", cover
        pic.width, pic.height = Image.open(io.BytesIO(cover)).size
        pic.depth = 24
        o["METADATA_BLOCK_PICTURE"] = [base64.b64encode(pic.write()).decode()]
    o.save()


def _fetch(url: str, **params) -> Optional[bytes]:
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=25)
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 200:
            return r.content if len(r.content) >= 1000 else None
        if r.status_code < 500 and r.status_code != 429:
            return None
        time.sleep(2 * (attempt + 1))
    return None


def _itunes(album: Album) -> Optional[bytes]:
    try:
        r = requests.get("https://itunes.apple.com/search", timeout=30,
                         params={"term": f"{album.artist} {album.title}",
                                 "entity": "album", "limit": 5})
        results = r.json().get("results", [])
    except (requests.RequestException, ValueError):
        return None
    want = f"{album.artist} {album.title}".lower()
    for res in results:
        got = f"{res.get('artistName', '')} {res.get('collectionName', '')}".lower()
        if difflib.SequenceMatcher(None, want, got).ratio() >= 0.6 and res.get("artworkUrl100"):
            data = _fetch(res["artworkUrl100"].replace("100x100bb", "3000x3000bb"))
            if data:
                return data
    return None


def fetch_cover(album: Album) -> Optional[bytes]:
    def caa(kind: str, mid: str) -> Optional[bytes]:
        return _fetch(f"https://coverartarchive.org/{kind}/{mid}/front")


    pool = ThreadPoolExecutor(max_workers=3)
    rel = pool.submit(caa, "release", album.mbid)
    grp = pool.submit(caa, "release-group", album.group_id) if album.group_id else None
    itn = pool.submit(_itunes, album)
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
