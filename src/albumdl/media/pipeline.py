import glob
import os
import shutil
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

from ..sources import musicbrainz as mb, youtube
from . import tagging

BAD_CHARS = '\\/:*?"<>|\n\r\t'
ATTEMPTS = 3


def safe(s: str) -> str:
    s = "".join("_" if c in BAD_CHARS else c for c in (s or ""))
    return s.strip().strip(".")[:200].strip() or "unknown"


@dataclass
class Options:
    out_root: str
    fmt: str = "mp3"
    jobs: int = 4
    cancel: threading.Event = field(default_factory=threading.Event)


def album_folder(album: mb.Album, out_root: str) -> str:
    return os.path.join(out_root, safe(album.artist), safe(album.title))


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
    # copy to .part then rename, so the music folder never holds half a track
    part = dst + ".part"
    try:
        shutil.copyfile(src, part)
        os.replace(part, dst)
    finally:
        if os.path.exists(part):
            os.remove(part)


def run(album: mb.Album, opts: Options, log: Callable[[str], None] = print,
        on_track: Optional[Callable[..., None]] = None) -> Tuple[int, int]:
    fmt = opts.fmt
    bitrate = tagging.DEFAULT_BITRATE[fmt]
    folder = album_folder(album, opts.out_root)
    total = len(album.tracks)
    note = on_track or (lambda *_, **__: None)
    lock = threading.Lock()

    def say(msg: str) -> None:
        with lock:
            log(msg)

    def target(t: mb.Track) -> str:
        name = f"{t.disc}-{t.number:02d}" if album.disc_total > 1 else f"{t.number:02d}"
        return os.path.join(folder, f"{name} - {safe(t.title)}.{fmt}")

    log(f"{album.artist} - {album.title} ({album.date or '?'}), {total} tracks -> {folder} [{fmt}]")

    todo = []
    for i, t in enumerate(album.tracks, 1):
        if os.path.exists(target(t)):
            log(f"[{i}/{total}] exists: {os.path.basename(target(t))}")
            note(i, "exists", "already downloaded", out_bytes=os.path.getsize(target(t)))
        else:
            todo.append((i, t))
    if not todo:
        log(f"\nDone: all {total} tracks already downloaded")
        return total, 0

    os.makedirs(folder, exist_ok=True)
    for stale in glob.glob(os.path.join(glob.escape(folder), "*.part")):
        os.remove(stale)

    def get_cover() -> bytes:
        path = os.path.join(folder, "cover.jpg")
        try:
            if os.path.exists(path):
                data = open(path, "rb").read()
            else:
                data = tagging.fetch_cover(album)
                if data:
                    with open(path + ".part", "wb") as f:
                        f.write(data)
                    os.replace(path + ".part", path)
            say("cover: " + (f"{len(data) // 1024} KB" if data else "not found"))
            return tagging.to_jpeg(data, max_side=1400) if data else b""
        except Exception as exc:
            say(f"cover failed: {str(exc)[:100]}")
            return b""

    with ThreadPoolExecutor(max_workers=1) as one:
        cover_job = one.submit(get_cover)
        playlist = None
        try:
            url = retrying(lambda: youtube.find_album_playlist(album), opts.cancel)
            if url:
                playlist = retrying(lambda: youtube.playlist_entries(url), opts.cancel)
                for n, e in enumerate(playlist, 1):
                    e["_index"] = n
                log(f"official album playlist: {url} ({len(playlist)} tracks)")
        except Exception as exc:
            log(f"no album playlist ({str(exc)[:120]}), searching per track")

        def attempt(i: int, t: mb.Track, n: int) -> bool:
            note(i, "working", "finding official source", attempts=n)
            t0 = time.time()
            entry, why = None, "official YouTube Music album playlist"
            if playlist:
                entry = youtube.match_playlist(t, playlist, i)
            if entry is None:
                hit = youtube.search_official(t, album)
                entry, why = hit if hit else (None, "")
            t_find = time.time() - t0
            if entry is None:
                reason = youtube.NOTES.get(t.track_id)
                say(f"[{i}/{total}] NO OFFICIAL SOURCE FOUND: {t.title}" + (f" ({reason})" if reason else ""))
                note(i, "failed", "no official source found" + (f" ({reason})" if reason else ""), t_find=t_find, attempts=n)
                return False
            who = entry.get("channel") or entry.get("uploader") or "?"
            url = youtube.entry_url(entry)
            info = {"channel": who, "yt_title": entry.get("title"), "url": url, "why": why, "t_find": t_find, "attempts": n}
            note(i, "working", f"downloading from {who}", **info)
            with tempfile.TemporaryDirectory() as td:
                t1 = time.time()
                src, acodec, abr = youtube.download(url, td)
                info.update(t_dl=time.time() - t1, src_bytes=os.path.getsize(src), acodec=acodec, src_kbps=round(abr or 0))
                note(i, "working", "encoding and tagging", **info)
                tmp = os.path.join(td, f"track.{fmt}")
                tagging.encode(src, tmp, fmt, bitrate, acodec)
                tagging.tag(tmp, fmt, album, t, cover_job.result(), url)
                info.update(out_bytes=os.path.getsize(tmp))
                publish(tmp, target(t))
            say(f"[{i}/{total}] {t.title} <- {entry.get('title')} ({who}) done ({info['src_bytes'] // 1024} KB in {info['t_dl']:.1f}s)")
            note(i, "done", f"{who}: {why}", **info)
            return True

        def work(i: int, t: mb.Track) -> bool:
            for n in range(1, ATTEMPTS + 1):
                if opts.cancel.is_set():
                    note(i, "cancelled", "cancelled")
                    return False
                try:
                    return attempt(i, t, n)
                except Exception as exc:
                    msg = str(exc).splitlines()[0][:200] if str(exc) else repr(exc)
                    if n == ATTEMPTS:
                        say(f"[{i}/{total}] FAILED {t.title}: {msg}")
                        note(i, "failed", msg, attempts=n)
                        return False
                    say(f"[{i}/{total}] retry {n}: {t.title} ({msg})")
                    note(i, "working", f"retrying after error: {msg}", attempts=n)
                    wait_online(opts.cancel)
                    opts.cancel.wait(3 * n)
            return False

        with ThreadPoolExecutor(max_workers=max(1, opts.jobs)) as pool:
            results = list(pool.map(lambda it: work(*it), todo))

    done = sum(results) + (total - len(todo))
    failed = len(results) - sum(results)
    log(f"\nDone: {done}/{total} tracks, {failed} failed")
    return done, failed
