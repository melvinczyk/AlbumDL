import difflib
import os
import re
import time
from typing import List

import requests

from .musicbrainz import UA

LISTENBRAINZ = "https://labs.api.listenbrainz.org/similar-artists/json"
ALGORITHM = "session_based_days_9000_session_300_contribution_5_threshold_15_limit_50_skip_30"
_cache: dict = {}


def _lastfm(artist_id: str, name: str, key: str) -> List[dict]:
    for ref in ({"mbid": artist_id}, {"artist": name, "autocorrect": 1}):
        data = requests.get("https://ws.audioscrobbler.com/2.0/", timeout=15, headers={"User-Agent": UA},
                            params={"method": "artist.getsimilar", "api_key": key, "format": "json", "limit": 16, **ref}).json()
        rows = data.get("similarartists", {}).get("artist", [])
        if rows:
            return [{"id": r.get("mbid", ""), "name": r["name"]} for r in rows]
    return []


def _listenbrainz(artist_id: str) -> List[dict]:
    rows = requests.get(LISTENBRAINZ, timeout=20, headers={"User-Agent": UA},
                        params={"artist_mbids": artist_id, "algorithm": ALGORITHM}).json()
    return [{"id": r["artist_mbid"], "name": r["name"]} for r in rows if r["artist_mbid"] != artist_id][:16]


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


def similar_artists(artist_id: str, name: str) -> List[dict]:
    hit = _cache.get(artist_id)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    key, rows = os.environ.get("ALBUMDL_LASTFM", ""), []
    for source in ([lambda: _lastfm(artist_id, name, key)] if key else []) + [lambda: _listenbrainz(artist_id)]:
        try:
            rows = source()
        except (requests.RequestException, ValueError, KeyError):
            continue
        if rows:
            break
    if rows:
        _cache[artist_id] = (time.time(), rows)
    return rows
