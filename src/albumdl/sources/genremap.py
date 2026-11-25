"""A map of the library. Genres are grouped into families set around a ring so that related ones sit side by side
(hip-hop next to R&B next to soul and funk next to jazz...), with pop in the middle. Each artist is placed between the
families of its genres, so an artist who mixes two styles lands where their colours blend."""
import hashlib
import math
import re
import threading
from collections import Counter
from typing import Dict, List

from .similar import FAIL, deezer_artist, dz, fold

# Ring order matters twice: neighbours on the ring are neighbours in sound, and a genre goes to the first family that matches.
FAMILIES = [
    ("hiphop", "Hip-Hop", ["hip hop", "hip-hop", "hiphop", "rap", "trap", "drill", "grime", "boom bap", "crunk", "g-funk", "horrorcore", "jazz rap"]),
    ("rnb", "R&B", ["r&b", "rnb", "rhythm and blues", "neo soul", "neo-soul", "new jack", "alternative r&b"]),
    ("soul", "Soul & Funk", ["soul", "funk", "motown", "gospel", "christian"]),
    ("jazz", "Jazz", ["jazz", "bebop", "swing", "bossa", "big band"]),
    ("blues", "Blues", ["blues"]),
    ("country", "Country", ["country", "americana", "bluegrass", "honky", "western"]),
    ("folk", "Folk", ["folk", "singer-songwriter", "singer/songwriter", "acoustic"]),
    ("indie", "Indie", ["indie", "alternative", "shoegaze", "dream pop", "lo-fi", "lofi", "slowcore", "bedroom", "chamber pop", "baroque pop", "jangle"]),
    ("rock", "Rock", ["rock", "grunge", "garage rock", "britpop", "surf", "rockabilly", "noise rock"]),
    ("psych", "Psych & Prog", ["progressive", "prog", "psychedelic", "psych", "krautrock", "space rock", "art rock", "math rock", "post-rock", "post rock"]),
    ("punk", "Punk", ["punk", "hardcore", "emo", "screamo", "post-hardcore"]),
    ("metal", "Metal", ["metal", "doom", "sludge", "djent", "thrash", "grindcore", "deathcore"]),
    ("classical", "Classical & Score", ["classical", "orchestral", "soundtrack", "score", "film", "games", "opera", "baroque", "symphon", "chamber", "piano", "musical", "composer"]),
    ("ambient", "Ambient & Experimental", ["ambient", "experimental", "drone", "noise", "new age", "avant", "minimal", "field recording", "sound art"]),
    ("electronic", "Electronic", ["electro", "synth", "idm", "downtempo", "trip hop", "trip-hop", "chillwave", "vaporwave", "glitch", "industrial", "edm", "dubstep", "bass"]),
    ("dance", "Dance", ["house", "techno", "disco", "dance", "trance", "drum and bass", "drum & bass", "jungle", "uk garage", "breakbeat", "club", "hardstyle", "eurodance"]),
    ("world", "Latin & World", ["latin", "reggae", "dancehall", "reggaeton", "salsa", "cumbia", "afro", "world", "brazil", "samba", "mexican", "k-pop", "j-pop", "bollywood", "indian", "african", "asian", "kids"]),
]
POP = ("pop", "Pop", ["pop"])
RING = 0.37
WEAK = {"experimental", "alternative", "avant", "minimal", "industrial", "acoustic", "bass", "club", "chamber", "piano"}
_rules = [(k, [(w, re.compile(r"(?<![a-z])" + re.escape(w))) for w in words]) for k, _, words in FAMILIES + [POP]]
ANCHOR: Dict[str, tuple] = {"pop": (0.5, 0.5)}
HUE: Dict[str, int] = {"pop": 325}
LABEL: Dict[str, str] = {"pop": "Pop", "unknown": "Uncharted"}
for i, (k, label, _) in enumerate(FAMILIES):
    a = -math.pi / 2 + i * 2 * math.pi / len(FAMILIES)
    ANCHOR[k] = (0.5 + RING * math.cos(a), 0.5 + RING * math.sin(a))
    HUE[k] = round(i * 360 / len(FAMILIES))
    LABEL[k] = label

_found: Dict[str, List[str]] = {}
_busy = threading.Lock()


def family(genre: str) -> str:
    """The family whose most specific keyword matches: 'progressive rock' is prog, not rock, and words like
    'experimental' or 'alternative' only count when nothing else matches ('experimental hip hop' is hip-hop)."""
    g, best, score = genre.lower(), "", 0
    for k, words in _rules:
        for w, rx in words:
            n = 1 if w in WEAK else len(w)
            if n > score and rx.search(g):
                best, score = k, n
    return best


def _jitter(name: str, spread: float) -> tuple:
    h = hashlib.md5(name.lower().encode()).digest()
    a, r = h[0] / 255 * 2 * math.pi, spread * math.sqrt(h[1] / 255)
    return r * math.cos(a), r * math.sin(a)


def _lookup(names: List[str]) -> None:
    """Genres for artists whose files carry none, from the genres Deezer gives their albums. Runs in the background."""
    if not _busy.acquire(blocking=False):
        return
    try:
        try:
            ids = {g["id"]: g["name"] for g in dz("/genre").get("data", []) if g.get("id")}
        except FAIL:
            ids = {}
        from .discover import _artist, albums_of
        for name in names:
            got: List[str] = []
            try:
                a = _artist(name)
                if a:
                    counts = Counter(x["genre"] for x in albums_of(a["deezer"], name) if x["genre"] in ids and x["genre"])
                    got = [ids[g] for g, _ in counts.most_common(2)]
            except FAIL:
                pass
            _found[fold(name)] = got
    finally:
        _busy.release()


def music_map(library: List[dict]) -> dict:
    by: Dict[str, dict] = {}
    for a in library:
        row = by.setdefault(fold(a["artist"]), {"name": a["artist"], "albums": 0, "genres": Counter()})
        row["albums"] += 1
        for j, g in enumerate(a.get("genres") or []):
            row["genres"][g.strip().lower()] += 1 / (j + 1)
    missing = [r["name"] for k, r in by.items() if not r["genres"] and k not in _found]
    if missing:
        threading.Thread(target=_lookup, args=(missing,), daemon=True).start()
    artists = []
    for k, r in by.items():
        if not r["genres"] and _found.get(k):
            r["genres"] = Counter({g.lower(): 1 / (j + 1) for j, g in enumerate(_found[k])})
        weights: Counter = Counter()
        for g, w in r["genres"].items():
            f = family(g)
            if f:
                weights[f] += w
        if weights:
            total = sum(weights.values())
            x = sum(ANCHOR[f][0] * w for f, w in weights.items()) / total
            y = sum(ANCHOR[f][1] * w for f, w in weights.items()) / total
            top = weights.most_common(1)[0][0]
            dx, dy = _jitter(r["name"], 0.075 if top == "pop" else 0.055)
        else:
            top, (x, y) = "unknown", (0.93, 0.93)
            dx, dy = _jitter(r["name"], 0.04)
        artists.append({"name": r["name"], "albums": r["albums"], "family": top,
                        "genres": [g for g, _ in r["genres"].most_common(4)],
                        "x": round(min(0.985, max(0.015, x + dx)), 4), "y": round(min(0.985, max(0.015, y + dy)), 4)})
    fams: Dict[str, dict] = {}
    for a in artists:
        f = fams.setdefault(a["family"], {"key": a["family"], "label": LABEL[a["family"]], "hue": HUE.get(a["family"], 210),
                                          "artists": 0, "albums": 0, "sx": 0.0, "sy": 0.0})
        f["artists"] += 1
        f["albums"] += a["albums"]
        f["sx"] += a["x"]
        f["sy"] += a["y"]
    for f in fams.values():
        f["x"], f["y"] = round(f.pop("sx") / f["artists"], 4), round(f.pop("sy") / f["artists"], 4)
    tags: Dict[str, list] = {}
    for a in artists:
        for g in a["genres"][:3]:
            tags.setdefault(g, []).append(a)
    labels = [{"name": g, "artists": len(rows), "x": round(sum(a["x"] for a in rows) / len(rows), 4), "y": round(sum(a["y"] for a in rows) / len(rows), 4)}
              for g, rows in sorted(tags.items(), key=lambda kv: -len(kv[1])) if len(rows) > 1 and fold(g) not in {fold(f["label"]) for f in fams.values()}][:18]
    return {"artists": sorted(artists, key=lambda a: -a["albums"]), "families": sorted(fams.values(), key=lambda f: -f["artists"]),
            "tags": labels, "anchors": [{"key": k, "label": LABEL[k], "hue": HUE[k], "x": round(ANCHOR[k][0], 4), "y": round(ANCHOR[k][1], 4)} for k in ANCHOR],
            "pending": len([n for n in missing if fold(n) not in _found])}
