import collections
import glob
import os
import re
import shutil
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

from ..sources import lyrics, musicbrainz as mb, youtube
from . import metadata, tagging

BAD_CHARS = '\\/:*?"<>|\n\r\t'
ATTEMPTS = 5
ROUNDS = 3
INCOMPLETE = "INCOMPLETE.txt"
# errors that will not go away by trying the same video again
DEAD = re.compile(r"confirm your age|sign in|private video|video unavailable|not available|members-only|removed", re.I)


class SourceFailed(Exception):
    """This video could not be downloaded; another one will be tried."""


def safe(s: str) -> str:
    s = "".join("_" if c in BAD_CHARS else c for c in (s or ""))
    return s.strip().strip(".")[:200].strip() or "unknown"


@dataclass
class Options:
    out_root: str
    jobs: int = 4
    cancel: threading.Event = field(default_factory=threading.Event)


def album_folder(album: mb.Album, out_root: str) -> str:
    return album.folder or os.path.join(out_root, safe(album.artist), safe(album.title))


def read_marker(folder: str) -> list:
    try:
        with open(os.path.join(folder, INCOMPLETE), encoding="utf-8", errors="replace") as f:
            parts = f.read().split("\n\n")
    except OSError:
        return []
    return [l for l in (parts[1].split("\n") if len(parts) > 1 else []) if l.strip()]


def write_marker(folder: str, names: list) -> None:
    marker = os.path.join(folder, INCOMPLETE)
    if not names:
        if os.path.exists(marker):
            os.remove(marker)
        return
    with open(marker, "w", encoding="utf-8") as f:
        f.write("This album is incomplete. These tracks could not be downloaded:\n\n")
        f.writelines(n + "\n" for n in names)
        f.write("\nDownload the album again to fill them in.\n")


def wait_online(cancel: threading.Event, timeout: float = 600) -> None:
    end = time.time() + timeout
    while not cancel.is_set():
        try:
            socket.create_connection(("www.youtube.com", 443), timeout=4).close()
            return
        except OSError:
            if time.time() > end:
                return
            cancel.wait(5)


def retrying(fn: Callable, cancel: threading.Event, attempts: int = 3):
    for n in range(attempts):
        try:
            return fn()
        except Exception:
            if n == attempts - 1 or cancel.is_set():
                raise
            wait_online(cancel)
            cancel.wait(2 * (n + 1))


def publish(src: str, dst: str) -> None:
    # copy to .part then rename, so the music folder never holds half a track (and an old file is replaced in one step)
    part = dst + ".part"
    try:
        shutil.copyfile(src, part)
        os.replace(part, dst)
    finally:
        if os.path.exists(part):
            os.remove(part)


def run(album: mb.Album, opts: Options, log: Callable[[str], None] = print,
        on_track: Optional[Callable[..., None]] = None) -> Tuple[int, int]:
    """Download an album, replacing anything already there. Returns (done, failed)."""
    folder = album_folder(album, opts.out_root)
    total = len(album.tracks)
    note = on_track or (lambda *_, **__: None)
    lock = threading.Lock()

    def say(msg: str) -> None:
        with lock:
            log(msg)

    def target(t: mb.Track) -> str:
        name = f"{t.disc}-{t.number:02d}" if album.disc_total > 1 else f"{t.number:02d}"
        return os.path.join(folder, f"{name} - {safe(t.title)}.mp3")

    log(f"{album.artist} - {album.title} ({album.date or '?'}), {total} tracks -> {folder}")
    imported = all(t.source for t in album.tracks)
    try:
        filled = {} if imported else metadata.fill(album)
        if filled:
            log("metadata filled: " + ", ".join(f"{f} from {src}" for f, src in filled.items()))
    except Exception as exc:
        log(f"metadata lookup failed: {str(exc)[:100]}")
    os.makedirs(folder, exist_ok=True)
    for stale in glob.glob(os.path.join(glob.escape(folder), "*.part")):
        os.remove(stale)

    def get_cover() -> bytes:
        path = os.path.join(folder, "cover.jpg")
        try:
            data, source = (None, "") if album.partial and os.path.exists(path) else tagging.fetch_cover(album)
            if data:
                with open(path + ".part", "wb") as f:
                    f.write(data)
                os.replace(path + ".part", path)
            elif os.path.exists(path):
                data = open(path, "rb").read()
            say("cover: " + (f"{len(data) // 1024} KB from {source}" if source else "kept the existing one" if data else "not found on any source"))
            return tagging.to_jpeg(data, max_side=1400) if data else b""
        except Exception as exc:
            say(f"cover failed: {str(exc)[:100]}")
            return b""

    src_tmp = tempfile.TemporaryDirectory()
    shared_locks = {t.source: threading.Lock() for t in album.tracks if t.end}
    bad: set = set()
    fails: collections.Counter = collections.Counter()
    shared_files: dict = {}

    def shared(vid: str):
        with shared_locks[vid]:
            if vid not in shared_files:
                folder_ = os.path.join(src_tmp.name, vid)
                os.makedirs(folder_, exist_ok=True)
                shared_files[vid] = youtube.download(f"https://www.youtube.com/watch?v={vid}", folder_)
            return shared_files[vid]

    with ThreadPoolExecutor(max_workers=1) as one:
        cover_job = one.submit(get_cover)
        playlist = None
        try:
            url = None if imported else retrying(lambda: youtube.find_album_playlist(album), opts.cancel)
            if url:
                playlist = retrying(lambda: youtube.playlist_entries(url), opts.cancel)
                for n, e in enumerate(playlist, 1):
                    e["_index"] = n
                log(f"official album playlist: {url} ({len(playlist)} tracks)")
        except Exception as exc:
            log(f"no album playlist ({str(exc)[:120]}), searching per track")

        def attempt(i: int, t: mb.Track, n: int) -> bool:
            note(i, "working", "finding source", attempts=n)
            t0 = time.time()
            entry, why = None, "official YouTube Music album playlist"
            if t.source:
                entry, why = {"id": t.source, "title": t.title, "channel": "YouTube link"}, "imported from a YouTube link"
            elif playlist:
                entry = youtube.match_playlist(t, playlist, i, album, frozenset(bad))
                if entry and entry.get("by_position"):
                    why = "official YouTube Music album playlist (matched by position and length)"
            if entry is None and not t.source:
                song = retrying(lambda: youtube.find_song(t, album, frozenset(bad)), opts.cancel)
                if song:
                    entry, why = song, "official YouTube Music song"
            if entry is None and not t.source:
                hit = youtube.search_official(t, album, frozenset(bad))
                entry, why = hit if hit else (None, "")
            t_find = time.time() - t0
            if entry is None:
                reason = youtube.NOTES.get(t.track_id)
                say(f"[{i}/{total}] NO SOURCE FOUND: {t.title}" + (f" ({reason})" if reason else ""))
                note(i, "failed", "no source found" + (f" ({reason})" if reason else ""), t_find=t_find, attempts=n)
                return False
            who = entry.get("channel") or entry.get("uploader") or "?"
            url = youtube.entry_url(entry)
            info = {"channel": who, "yt_title": entry.get("title"), "url": url, "why": why, "t_find": t_find, "attempts": n}
            note(i, "working", f"downloading from {who}" if not t.end else "cutting the chapter out of the video", **info)
            with tempfile.TemporaryDirectory() as td:
                t1 = time.time()
                try:
                    src, acodec, abr = shared(t.source) if t.end else youtube.download(url, td)
                except Exception as exc:
                    vid = entry.get("id")
                    if not vid or t.source:
                        raise
                    with lock:
                        fails[vid] += 1
                        dead = bool(DEAD.search(str(exc))) or fails[vid] >= 2
                        if dead:
                            bad.add(vid)
                    if not dead:
                        raise
                    say(f"[{i}/{total}] {who}'s upload will not download ({str(exc).splitlines()[0][:120]}); trying another source")
                    raise SourceFailed(str(exc).splitlines()[0][:200]) from exc
                info.update(t_dl=time.time() - t1, src_bytes=os.path.getsize(src), acodec=acodec, src_kbps=round(abr or 0))
                note(i, "working", "encoding and tagging", **info)
                tmp = os.path.join(td, "track.mp3")
                tagging.encode(src, tmp, t.start, t.end)
                tagging.tag(tmp, album, t, cover_job.result(), url)
                info.update(out_bytes=os.path.getsize(tmp))
                publish(tmp, target(t))
                try:
                    info.update(lyrics=lyrics.attach(target(t), t.artist, t.title, album.title, t.length_ms / 1000 if t.length_ms else None))
                except Exception:
                    pass
            say(f"[{i}/{total}] {t.title} <- {entry.get('title')} ({who}) done ({info['src_bytes'] // 1024} KB in {info['t_dl']:.1f}s)")
            note(i, "done", f"{who}: {why}", **info)
            return True

        def work(i: int, t: mb.Track) -> str:
            for n in range(1, ATTEMPTS + 1):
                if opts.cancel.is_set():
                    note(i, "cancelled", "cancelled")
                    return "cancelled"
                try:
                    return "ok" if attempt(i, t, n) else "missing"
                except Exception as exc:
                    msg = str(exc).splitlines()[0][:200] if str(exc) else repr(exc)
                    if n == ATTEMPTS:
                        say(f"[{i}/{total}] FAILED {t.title}: {msg}")
                        note(i, "failed", msg, attempts=n)
                        return "error"
                    say(f"[{i}/{total}] retry {n}: {t.title} ({msg})")
                    note(i, "working", f"retrying after error: {msg}", attempts=n)
                    wait_online(opts.cancel)
                    opts.cancel.wait(2 if isinstance(exc, SourceFailed) else min(90, 8 * 2 ** (n - 1)))
            return "error"

        outcome = {}
        pending = list(enumerate(album.tracks, 1))
        for rnd in range(1, ROUNDS + 1):
            with ThreadPoolExecutor(max_workers=max(1, opts.jobs)) as pool:
                for (i, _), result in zip(pending, pool.map(lambda it: work(*it), pending)):
                    outcome[i] = result
            pending = [(i, t) for i, t in pending if outcome[i] == "error"]
            if not pending or rnd == ROUNDS or opts.cancel.is_set():
                break
            say(f"{len(pending)} tracks failed; trying them again in {60 * rnd}s")
            opts.cancel.wait(60 * rnd)

    src_tmp.cleanup()
    missing = [(i, t) for i, t in enumerate(album.tracks, 1) if outcome.get(i) != "ok"]
    names = [os.path.basename(target(t))[:-4] for _, t in missing]
    if album.partial:
        fixed = {os.path.basename(target(t))[:-4] for i, t in enumerate(album.tracks, 1) if outcome.get(i) == "ok"}
        write_marker(folder, [n for n in read_marker(folder) if n not in fixed] + [n for n in names if n not in read_marker(folder)])
    else:
        write_marker(folder, names)
    done = total - len(missing)
    log(f"\nDone: {done}/{total} tracks, {len(missing)} failed")
    return done, len(missing)
