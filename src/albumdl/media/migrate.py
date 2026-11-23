import os
import re
import sys

import mutagen

from . import pipeline

AUDIO = (".mp3", ".m4a", ".opus", ".flac", ".ogg")
NO_ARTIST = "No Artist"
DISC = re.compile(r"^(cd|disc|disk)\s*[-_ ]?\d+", re.I)


def is_audio(name: str) -> bool:
    return name.lower().endswith(AUDIO) and not name.startswith(".")


def tags_of(path: str) -> tuple:
    """(artist, album) from a file's tags; album artist wins over track artist."""
    try:
        tags = (mutagen.File(path, easy=True) or {}).get
    except Exception:
        return "", ""
    return (tags("albumartist") or tags("artist") or [""])[0], (tags("album") or [""])[0]


def disc_album(path: str, name: str):
    """(artist, album) if the folder is a multi-disc album: no tracks of its own, and sub-folders that are discs
    (CD 1, Disc 2...) or that all carry the same album tag next to a cover image."""
    names = os.listdir(path)
    if any(is_audio(n) for n in names):
        return None
    discs = [k for k in sorted(names) if not k.startswith(".") and os.path.isdir(os.path.join(path, k))
             and any(is_audio(n) for n in os.listdir(os.path.join(path, k)))]
    if not discs:
        return None
    found = [album_of(os.path.join(path, k), name) for k in discs]
    has_cover = any(n.lower().endswith((".jpg", ".jpeg", ".png")) and not n.startswith(".") for n in names)
    same_album = has_cover and len(discs) > 1 and len({a for _, a in found}) == 1
    return found[0] if all(DISC.match(k) for k in discs) or same_album else None


def album_of(folder: str, default_album: str = "") -> tuple:
    """(artist, album) from the first track that has an artist tag. Without an album tag the folder name is the album."""
    for name in sorted(os.listdir(folder)):
        if is_audio(name):
            artist, album = tags_of(os.path.join(folder, name))
            if artist:
                return artist, album or default_album or os.path.basename(folder)
    return "", default_album or os.path.basename(folder)


def main(root: str, apply: bool) -> None:
    entries = [e for e in sorted(os.listdir(root)) if not e.startswith(".")]
    dirs = [e for e in entries if os.path.isdir(os.path.join(root, e))]
    albums, artists = {}, {}
    for d in dirs:
        path = os.path.join(root, d)
        if any(is_audio(n) for n in os.listdir(path)) and not any(os.path.isdir(os.path.join(path, n)) for n in os.listdir(path)):
            artist, album = album_of(path)
            if not (pipeline.safe(artist).lower() == d.lower() and pipeline.safe(album).lower() != d.lower()):
                albums[d] = (artist, album)
                continue
        elif (multi := disc_album(path, d)) is not None:
            albums[d] = multi
            continue
        artists[d.lower()] = d
    plan, taken = [], set()

    def add(name: str, artist: str, rest: str) -> None:
        artist = artist or NO_ARTIST
        artist_dir = artists.setdefault(pipeline.safe(artist).lower(), pipeline.safe(artist))
        target = os.path.join(artist_dir, rest)
        if target.lower() in taken or os.path.exists(os.path.join(root, target)):
            print(f"SKIP  {name}: {target} already exists")
            return
        taken.add(target.lower())
        plan.append((name, target))
        print(f"MOVE  {name}  ->  {target}")

    for name, (artist, album) in albums.items():
        add(name, artist, pipeline.safe(album))
    for name in entries:
        if os.path.isfile(os.path.join(root, name)) and is_audio(name):
            add(name, tags_of(os.path.join(root, name))[0], name)

    if apply:
        for n, (name, _) in enumerate(plan):
            os.rename(os.path.join(root, name), os.path.join(root, f".moving-{n}"))
        for n, (_, target) in enumerate(plan):
            os.makedirs(os.path.dirname(os.path.join(root, target)), exist_ok=True)
            os.rename(os.path.join(root, f".moving-{n}"), os.path.join(root, target))
        print(f"\nmoved {len(plan)}")
    else:
        print(f"\n{len(plan)} would move. Nothing was changed; run again with --apply to move them.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--apply"]
    main(args[0] if args else os.environ.get("OUTPUT_DIR", "./out"), "--apply" in sys.argv)
