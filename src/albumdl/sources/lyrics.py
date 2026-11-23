import difflib
import os
import re
from typing import List, Optional

import requests

from .musicbrainz import UA

API = "https://lrclib.net/api"
STAMP = re.compile(r"^\s*\[\d{1,3}:\d{2}", re.M)


def _titles(title: str) -> List[str]:
    """The title as listed, then without brackets and without a "/ bonus song" part."""
    out = [title]
    plain = re.sub(r"\s*[\(\[].*?[\)\]]", "", title).strip()
    first = re.split(r"\s+/{1,2}\s+", plain)[0].strip()
    return list(dict.fromkeys(out + [plain, first]))


def _get(path: str, **params) -> object:
    try:
        r = requests.get(f"{API}/{path}", params=params, headers={"User-Agent": UA}, timeout=12)
        return r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def find(artist: str, title: str, album: str = "", duration: Optional[float] = None) -> Optional[dict]:
    """Best match from LRCLIB as {"synced", "plain"}, or None. Synced lyrics are preferred."""
    for name in _titles(title):
        exact = _get("get", artist_name=artist, track_name=name, album_name=album, **({"duration": round(duration)} if duration else {}))
        if isinstance(exact, dict) and (exact.get("syncedLyrics") or exact.get("plainLyrics")):
            return {"synced": exact.get("syncedLyrics") or "", "plain": exact.get("plainLyrics") or ""}
        rows = _get("search", track_name=name, artist_name=artist)
        best = None
        for r in rows if isinstance(rows, list) else []:
            if r.get("instrumental") or not (r.get("syncedLyrics") or r.get("plainLyrics")):
                continue
            gap = abs((r.get("duration") or 0) - duration) if duration else 0
            if duration and gap > 4:
                continue
            key = (bool(r.get("syncedLyrics")), difflib.SequenceMatcher(None, (r.get("albumName") or "").lower(), album.lower()).ratio(), -gap)
            if best is None or key > best[0]:
                best = (key, r)
        if best:
            return {"synced": best[1].get("syncedLyrics") or "", "plain": best[1].get("plainLyrics") or ""}
    return None


def sidecar(mp3: str) -> str:
    return os.path.splitext(mp3)[0] + ".lrc"


def existing(mp3: str) -> Optional[str]:
    base = os.path.splitext(mp3)[0]
    return next((p for p in (base + ".lrc", base + ".txt") if os.path.isfile(p)), None)


def state(mp3: str) -> str:
    """"synced", "plain" or "none" for the lyrics file next to a song."""
    path = existing(mp3)
    if not path:
        return "none"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "synced" if STAMP.search(f.read(4000)) else "plain"
    except OSError:
        return "none"


def save(mp3: str, text: str) -> str:
    text = text.replace("\r\n", "\n").lstrip("﻿").strip() + "\n"
    path = sidecar(mp3)
    with open(path + ".part", "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(path + ".part", path)
    return "synced" if STAMP.search(text) else "plain"


def attach(mp3: str, artist: str, title: str, album: str, duration: Optional[float]) -> str:
    """Write lyrics next to a song unless it already has some. Returns "synced", "plain", "existing" or ""."""
    if existing(mp3):
        return "existing"
    res = find(artist, title, album, duration)
    if not res:
        return ""
    return save(mp3, res["synced"] or res["plain"])
