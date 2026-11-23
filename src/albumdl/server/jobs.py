import collections
import difflib
import os
import shutil
import queue
import re
import threading
import time
import uuid
from typing import Dict, List, Optional

from ..media import pipeline
from ..sources import musicbrainz as mb

FINISHED = ("done", "incomplete", "failed", "cancelled")
AUDIO = (".mp3", ".m4a", ".opus")


def level_of(msg: str) -> str:
    low = msg.lower()
    if low.startswith("done"):
        return "ok" if ", 0 failed" in low or "already" in low else "warn"
    if "failed" in low or "no official source" in low or low.startswith("error"):
        return "error"
    if "retry" in low or "not found" in low or "no album playlist" in low:
        return "warn"
    if re.search(r"\bdone \(", low):
        return "ok"
    return "info"


class EventLog:
    def __init__(self, size: int = 4000):
        self.events = collections.deque(maxlen=size)
        self.seq = 0
        self.lock = threading.Lock()

    def emit(self, level: str, src: str, msg: str, job: str = "", track: int = 0) -> None:
        with self.lock:
            self.seq += 1
            self.events.append({"id": self.seq, "t": time.time(), "level": level, "src": src, "msg": msg, "job": job, "track": track})

    def since(self, last: int, limit: int = 600) -> dict:
        with self.lock:
            return {"logs": [e for e in self.events if e["id"] > last][-limit:], "last": self.seq}


def _key(name: str) -> str:
    return re.sub(r"[\W_]+", "", name.lower())


class Library:
    """What is already in the music folder (Artist/Album/). Folder listings are cached for 30 seconds."""

    def __init__(self, out_root: str):
        self.out_root = out_root
        self._cache: Dict[str, tuple] = {}

    def refresh(self) -> None:
        self._cache.clear()

    def _subfolders(self, path: str) -> Dict[str, str]:
        hit = self._cache.get(path)
        if hit and time.time() - hit[0] < 30:
            return hit[1]
        names = {}
        try:
            for e in os.scandir(path):
                if e.is_dir() and not e.name.startswith("."):
                    names[_key(e.name)] = e.name
        except OSError:
            pass
        self._cache[path] = (time.time(), names)
        return names

    def _find(self, path: str, name: str) -> Optional[str]:
        names, key = self._subfolders(path), _key(pipeline.safe(name))
        if key in names:
            return names[key]
        close = difflib.get_close_matches(key, names, n=1, cutoff=0.88) if len(key) > 4 else []
        return names[close[0]] if close else None

    def has_artist(self, name: str) -> bool:
        return self._find(self.out_root, name) is not None

    def owned_album(self, artist: str, title: str) -> Optional[dict]:
        artist_dir = self._find(self.out_root, artist)
        if not artist_dir:
            return None
        path = os.path.join(self.out_root, artist_dir)
        album_dir = self._find(path, title)
        if not album_dir:
            return None
        tracks = size = 0
        try:
            for f in os.scandir(os.path.join(path, album_dir)):
                if f.is_file() and f.name.lower().endswith(AUDIO) and not f.name.startswith("."):
                    tracks += 1
                    size += f.stat().st_size
        except OSError:
            return None
        incomplete = os.path.exists(os.path.join(path, album_dir, pipeline.INCOMPLETE))
        return {"tracks": tracks, "size": size, "incomplete": incomplete} if tracks else None

    def remove_album(self, artist: str, title: str) -> bool:
        artist_dir = self._find(self.out_root, artist)
        album_dir = artist_dir and self._find(os.path.join(self.out_root, artist_dir), title)
        if not album_dir:
            return False
        shutil.rmtree(os.path.join(self.out_root, artist_dir, album_dir))
        try:
            os.rmdir(os.path.join(self.out_root, artist_dir))
        except OSError:
            pass
        self.refresh()
        return True

    def albums(self) -> List[dict]:
        self.refresh()
        out = []
        for artist in sorted(self._subfolders(self.out_root).values(), key=str.lower):
            apath = os.path.join(self.out_root, artist)
            for album in sorted(self._subfolders(apath).values(), key=str.lower):
                tracks = size = 0
                newest = 0.0
                cover = incomplete = False
                try:
                    for f in os.scandir(os.path.join(apath, album)):
                        low = f.name.lower()
                        if f.name.startswith(".") or not f.is_file():
                            continue
                        if low.endswith(AUDIO):
                            st = f.stat()
                            tracks, size, newest = tracks + 1, size + st.st_size, max(newest, st.st_mtime)
                        elif low == "cover.jpg":
                            cover = True
                        elif f.name == pipeline.INCOMPLETE:
                            incomplete = True
                except OSError:
                    continue
                if not tracks and not incomplete:
                    continue
                missing = []
                if incomplete:
                    with open(os.path.join(apath, album, pipeline.INCOMPLETE), encoding="utf-8", errors="replace") as f:
                        parts = f.read().split("\n\n")
                    missing = [l for l in (parts[1].split("\n") if len(parts) > 1 else []) if l.strip()]
                out.append({"artist": artist, "title": album, "tracks": tracks, "size": size, "mtime": newest,
                            "cover": cover, "incomplete": incomplete, "missing": missing})
        return out

    def incomplete_albums(self, only: Optional[set] = None) -> List[dict]:
        from mutagen.id3 import ID3
        out = []
        for row in self.albums():
            if not row["incomplete"] or (only is not None and (row["artist"], row["title"]) not in only):
                continue
            row = dict(row, group_id="")
            songs = sorted(f.path for f in os.scandir(os.path.join(self.out_root, row["artist"], row["title"]))
                           if f.name.lower().endswith(".mp3") and not f.name.startswith("."))
            if songs:
                try:
                    id3 = ID3(songs[0])
                    row["group_id"] = next((str(f.text[0]) for f in id3.getall("TXXX") if f.desc == "MusicBrainz Release Group Id" and f.text), "")
                except Exception:
                    pass
            out.append(row)
        return out

    def cover_path(self, artist: str, album: str) -> Optional[str]:
        artist_dir = self._find(self.out_root, artist)
        album_dir = artist_dir and self._find(os.path.join(self.out_root, artist_dir), album)
        path = album_dir and os.path.join(self.out_root, artist_dir, album_dir, "cover.jpg")
        return path if path and os.path.isfile(path) else None


class Job:
    def __init__(self, album: mb.Album, opts: pipeline.Options):
        self.id = uuid.uuid4().hex[:10]
        self.album, self.opts = album, opts
        self.status = "queued"
        self.started = self.finished = 0.0
        self.log: List[dict] = []
        self.tracks = {i: {"n": i, "title": t.title, "state": "queued", "text": "",
                           "seconds": round(t.length_ms / 1000) if t.length_ms else None}
                       for i, t in enumerate(album.tracks, 1)}
        self.done = self.failed = 0
        self.error = ""

    def summary(self, full: bool = False) -> dict:
        tr = self.tracks.values()
        end = self.finished or time.time()
        d = {"id": self.id, "title": self.album.title, "artist": self.album.artist,
             "year": self.album.year, "mbid": self.album.mbid, "group_id": self.album.group_id,
             "status": self.status,
             "total": len(self.tracks), "done": self.done, "failed": self.failed,
             "finished": sum(t["state"] in ("done", "exists", "failed", "cancelled") for t in tr),
             "error": self.error, "started": self.started,
             "elapsed": round(end - self.started, 1) if self.started else 0,
             "bytes": sum(t.get("out_bytes") or 0 for t in tr if t["state"] == "done"),
             "retries": sum(max(0, (t.get("attempts") or 1) - 1) for t in tr),
             "src_kbps": round(sum(k) / len(k)) if (k := [t["src_kbps"] for t in tr if t["state"] == "done" and t.get("src_kbps")]) else 0}
        if full:
            d["tracks"] = list(self.tracks.values())
            d["log"] = self.log[-400:]
        return d


class Batch:
    """One "download the whole discography" request, expanded album by album."""

    def __init__(self, artist: str, total: int):
        self.id = uuid.uuid4().hex[:10]
        self.artist, self.total = artist, total
        self.queued = self.skipped = self.failed = 0
        self.status = "running"
        self.current = ""
        self.cancel = threading.Event()

    def summary(self) -> dict:
        return {"id": self.id, "artist": self.artist, "total": self.total, "queued": self.queued,
                "skipped": self.skipped, "failed": self.failed, "status": self.status, "current": self.current,
                "resolved": self.queued + self.skipped + self.failed}


class JobManager:
    """Runs one album at a time (tracks inside an album download in parallel)."""

    def __init__(self, out_root: str, parallel: int, events: EventLog, library: Library):
        self.out_root, self.parallel, self.events, self.library = out_root, parallel, events, library
        self.jobs: Dict[str, Job] = {}
        self.order: List[str] = []
        self.batches: Dict[str, Batch] = {}
        self.pending: "queue.Queue[Job]" = queue.Queue()
        self.lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, album: mb.Album) -> Job:
        job = Job(album, pipeline.Options(out_root=self.out_root, jobs=self.parallel))
        with self.lock:
            self.jobs[job.id] = job
            self.order.append(job.id)
        self.pending.put(job)
        self.events.emit("info", "queue", f"queued: {album.artist} - {album.title}", job.id)
        return job

    def active_groups(self) -> set:
        with self.lock:
            return {j.album.group_id for j in self.jobs.values() if j.status in ("queued", "running")}

    def listing(self) -> List[Job]:
        with self.lock:
            return [self.jobs[i] for i in reversed(self.order)]

    def cancel(self, job: Job) -> None:
        job.opts.cancel.set()
        if job.status == "queued":
            job.status, job.finished = "cancelled", time.time()
        self.events.emit("warn", "queue", f"cancel requested: {job.album.title}", job.id)

    def retry(self, job: Job) -> Job:
        return self.submit(job.album)

    def clear_finished(self) -> int:
        with self.lock:
            gone = [i for i in self.order if self.jobs[i].status in FINISHED]
            for i in gone:
                del self.jobs[i]
            self.order = [i for i in self.order if i not in gone]
        return len(gone)

    def totals(self) -> dict:
        jobs = self.listing()
        tracks = [t for j in jobs for t in j.tracks.values()]
        return {"jobs": {k: sum(j.status == k for j in jobs) for k in ("queued", "running", *FINISHED)},
                "tracks": {"done": sum(t["state"] == "done" for t in tracks), "failed": sum(t["state"] == "failed" for t in tracks)},
                "bytes_out": sum(t.get("out_bytes") or 0 for t in tracks if t["state"] == "done")}

    def discography(self, artist: str, groups: List[dict], skip_owned: bool) -> Batch:
        batch = Batch(artist, len(groups))
        with self.lock:
            self.batches[batch.id] = batch
        self.events.emit("info", "queue", f"discography: {artist}, {len(groups)} releases")
        threading.Thread(target=self._expand, args=(batch, groups, skip_owned), daemon=True).start()
        return batch

    def _expand(self, batch: Batch, groups: List[dict], skip_owned: bool) -> None:
        for g in groups:
            if batch.cancel.is_set():
                break
            batch.current = g["title"]
            have = self.library.owned_album(g["artist"], g["title"])
            if skip_owned and have and not have["incomplete"]:
                batch.skipped += 1
                self.events.emit("info", "queue", f"discography: skipped {g['title']} (already in the folder)")
                continue
            try:
                release = g.get("release")
                if not release:
                    editions = mb.group_editions(g["id"])
                    if not editions:
                        raise RuntimeError("no releases listed")
                    release = editions[0]["id"]
                album = mb.get_release(release)
                if g.get("release") and album.edition and album.edition.lower() not in album.title.lower():
                    album.title = f"{album.title} ({album.edition})"
                self.submit(album)
                batch.queued += 1
            except BaseException as exc:
                batch.failed += 1
                self.events.emit("error", "queue", f"discography: {g['title']} failed ({str(exc).replace('error: ', '')[:120]})")
        batch.current = ""
        batch.status = "cancelled" if batch.cancel.is_set() else "done"
        self.events.emit("ok" if batch.status == "done" else "warn", "queue",
                         f"discography {batch.status}: {batch.artist} ({batch.queued} queued, {batch.skipped} owned, {batch.failed} failed)")

    def _worker(self) -> None:
        while True:
            job = self.pending.get()
            if job.status != "queued" or job.opts.cancel.is_set():
                job.status, job.finished = "cancelled", time.time()
                continue
            job.status, job.started = "running", time.time()

            def log(msg: str, job=job) -> None:
                for line in msg.strip("\n").split("\n"):
                    m = re.match(r"\[(\d+)/\d+\]", line)
                    entry = {"t": time.time(), "level": level_of(line), "msg": line, "track": int(m.group(1)) if m else 0}
                    job.log.append(entry)
                    self.events.emit(entry["level"], "download", line, job.id, entry["track"])

            def on_track(i: int, state: str, text: str, job=job, **info) -> None:
                job.tracks[i].update(state=state, text=text, **info)

            try:
                job.done, job.failed = pipeline.run(job.album, job.opts, log, on_track)
                job.status = ("cancelled" if job.opts.cancel.is_set() else "done" if not job.failed
                              else "failed" if not job.done else "incomplete")
            except BaseException as exc:
                job.error = str(exc) or repr(exc)
                job.status = "failed"
                log(f"error: {job.error}")
            job.finished = time.time()
            self.library.refresh()
            self.events.emit("ok" if job.status == "done" else "error" if job.status == "failed" else "warn", "queue",
                             f"{job.status}: {job.album.artist} - {job.album.title} ({job.done} ok, {job.failed} failed, {job.finished - job.started:.0f}s)", job.id)
