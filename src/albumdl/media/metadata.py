import os
import re
from typing import Dict, List

import requests

from ..sources.musicbrainz import UA, Album
from .tagging import _clean, _same

FIELDS = ("genres", "label", "barcode", "date", "type")


def _json(url: str, **params) -> dict:
    try:
        return requests.get(url, params=params, timeout=15, headers={"User-Agent": UA}).json()
    except (requests.RequestException, ValueError):
        return {}


def deezer(album: Album) -> Dict[str, object]:
    rows = _json("https://api.deezer.com/search/album", q=f'artist:"{album.artist}" album:"{_clean(album.title)}"', limit=5).get("data", [])
    hit = next((r for r in rows if _same(album.artist, album.title, (r.get("artist") or {}).get("name", ""), r.get("title", ""))), None)
    d = _json(f"https://api.deezer.com/album/{hit['id']}") if hit else {}
    return {"genres": [g["name"] for g in d.get("genres", {}).get("data", [])][:3], "label": d.get("label") or "",
            "barcode": d.get("upc") or "", "date": d.get("release_date") or "", "type": {"ep": "EP", "album": "Album", "single": "Single"}.get(d.get("record_type") or "", "")}


def itunes(album: Album) -> Dict[str, object]:
    rows = _json("https://itunes.apple.com/search", term=f"{album.artist} {_clean(album.title)}", entity="album", limit=8).get("results", [])
    hit = next((r for r in rows if _same(album.artist, album.title, r.get("artistName", ""), r.get("collectionName", ""))), None)
    return {"genres": [hit["primaryGenreName"]] if hit and hit.get("primaryGenreName") else [], "date": (hit or {}).get("releaseDate", "")[:10]}


def lastfm(album: Album) -> Dict[str, object]:
    key = os.environ.get("ALBUMDL_LASTFM", "")
    if not key:
        return {}
    d = _json("https://ws.audioscrobbler.com/2.0/", method="album.getinfo", api_key=key, artist=album.artist, album=album.title, autocorrect=1, format="json")
    tags = [t["name"].title() for t in (d.get("album") or {}).get("tags", {}).get("tag", []) if isinstance(t, dict)]
    return {"genres": tags[:3]}


# Order matters: a field is filled by the first source that has it. MusicBrainz (or YouTube Music) data is never overwritten.
SOURCES: List[tuple] = [("Deezer", deezer), ("iTunes", itunes), ("Last.fm", lastfm)]


def _missing(album: Album) -> set:
    return {f for f, empty in (("genres", not album.genres), ("label", not album.label), ("barcode", not album.barcode),
                               ("date", len(album.date or "") < 10), ("type", not album.type)) if empty}


def fill(album: Album) -> Dict[str, str]:
    """Fill gaps in the album's metadata from the lower-priority sources. Returns {field: source}."""
    filled: Dict[str, str] = {}
    for name, source in SOURCES:
        need = _missing(album)
        if not need:
            break
        try:
            got = source(album)
        except Exception:
            continue
        for field in need:
            value = got.get(field)
            if not value:
                continue
            if field == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                continue
            if field == "date" and album.date and not value.startswith(album.date[:4]):
                continue
            setattr(album, field, value)
            filled[field] = name
    return filled
