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
    if "failed" in low or "no source found" in low or low.startswith("error"):
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


def pick_tracks(tracks: List[mb.Track], wanted: List[dict]) -> List[mb.Track]:
    """The release's tracks for the songs asked for, by MusicBrainz recording id, else by title."""
    out = []
    for w in wanted:
        rec, key = str(w.get("recording") or ""), pipeline.title_key(str(w.get("title") or ""))
        hit = next((t for t in tracks if rec and t.recording_id == rec), None) \
            or next((t for t in tracks if key and pipeline.title_key(t.title) == key), None) \
            or next((t for t in tracks if key and len(key) > 3 and difflib.SequenceMatcher(None, key, pipeline.title_key(t.title)).ratio() >= 0.88), None)
        if hit and hit not in out:
            out.append(hit)
    return sorted(out, key=lambda t: (t.disc, t.number))


def _key(name: str) -> str:
    return re.sub(r"[\W_]+", "", name.lower())


class Library:
    """What is already in the music folder (Artist/Album/). Folder listings are cached for 30 seconds."""

    def __init__(self, out_root: str):
        self.out_root = out_root
        self._cache: Dict[str, tuple] = {}
        self._tags: Dict[tuple, tuple] = {}

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
        digits = re.findall(r"\d+", key)
        close = [c for c in difflib.get_close_matches(key, names, n=3, cutoff=0.88) if re.findall(r"\d+", c) == digits] if len(key) > 4 else []
        return names[close[0]] if close else None

    def folder_of(self, artist: str, title: str) -> str:
        artist_dir = self._find(self.out_root, artist)
        album_dir = artist_dir and self._find(os.path.join(self.out_root, artist_dir), title)
        return os.path.join(self.out_root, artist_dir, album_dir) if album_dir else ""

    def songs_in(self, artist: str, title: str) -> set:
        """Loose title keys of the songs already saved for this album."""
        folder = self.folder_of(artist, title)
        try:
            return {pipeline.title_key(m.group(1)) for f in os.scandir(folder) if (m := pipeline.SONG_FILE.match(f.name))} if folder else set()
        except OSError:
            return set()

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
        first = min((f.name for f in os.scandir(os.path.join(path, album_dir)) if f.name.lower().endswith(".mp3") and not f.name.startswith(".")), default="")
        of = self._total(os.path.join(path, album_dir, first)) if first else 0
        return {"tracks": tracks, "size": size, "incomplete": incomplete, "of": of, "picked": not incomplete and tracks < of} if tracks else None

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
                first = ""
                audio_bases, lyric_bases = [], set()
                try:
                    for f in os.scandir(os.path.join(apath, album)):
                        low = f.name.lower()
                        if f.name.startswith(".") or not f.is_file():
                            continue
                        if low.endswith(AUDIO):
                            audio_bases.append(os.path.splitext(low)[0])
                            if low.endswith(".mp3") and (not first or f.name < first):
                                first = f.name
                            st = f.stat()
                            tracks, size, newest = tracks + 1, size + st.st_size, max(newest, st.st_mtime)
                        elif low == "cover.jpg":
                            cover = True
                        elif low.endswith((".lrc", ".txt")) and f.name != pipeline.INCOMPLETE:
                            lyric_bases.add(os.path.splitext(low)[0])
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
                            "cover": cover, "incomplete": incomplete, "missing": missing,
                            "lyrics": sum(1 for b in audio_bases if b in lyric_bases),
                            "gaps": self._gaps(os.path.join(apath, album, first)) if first else [],
                            "of": self._total(os.path.join(apath, album, first)) if first else 0,
                            "genres": self._genres(os.path.join(apath, album, first)) if first else []})
                out[-1]["picked"] = not incomplete and tracks < out[-1]["of"]
        return out

    def _gaps(self, path: str) -> List[str]:
        """What the first track's tags lack. An album you saved in the tag editor counts as reviewed."""
        try:
            key = (path, os.path.getmtime(path))
        except OSError:
            return []
        if key not in self._tags:
            gaps, total, genres = [], 0, []
            try:
                from mutagen.id3 import ID3
                t = ID3(path)
                n, d = str(t.get("TRCK") or "").partition("/")[2], str(t.get("TPOS") or "1/1").partition("/")[2]
                total = int(n) if n.isdigit() and (not d or d == "1") else 0
                genres = [g.strip() for x in (t["TCON"].text if "TCON" in t else []) for g in re.split(r"[;/,]", str(x)) if g.strip()][:6]
                if not any(f.desc == "ALBUMDL_REVIEWED" for f in t.getall("TXXX")):
                    if not str(t.get("TCON") or "").strip():
                        gaps.append("genre")
                    if not re.fullmatch(r"\d{4}", str(t.get("TDRC") or "")[:4]):
                        gaps.append("year")
            except Exception:
                pass
            self._tags[key] = (gaps, total, genres)
        return self._tags[key][0]

    def _genres(self, path: str) -> List[str]:
        self._gaps(path)
        try:
            return self._tags.get((path, os.path.getmtime(path)), ([], 0, []))[2]
        except OSError:
            return []

    def _total(self, path: str) -> int:
        """How many tracks the album has, from the first song's 'track n/total' tag (single-disc albums only)."""
        self._gaps(path)
        try:
            return self._tags.get((path, os.path.getmtime(path)), ([], 0, []))[1]
        except OSError:
            return 0

    def album_detail(self, artist: str, title: str) -> Optional[dict]:
        from mutagen.id3 import ID3
        ad = self._find(self.out_root, artist)
        al = ad and self._find(os.path.join(self.out_root, ad), title)
        if not al:
            return None
        folder = os.path.join(self.out_root, ad, al)
        names = sorted(f.name for f in os.scandir(folder) if f.is_file() and f.name.lower().endswith(".mp3") and not f.name.startswith("."))
        if not names:
            return None
        tracks = []
        for n in names:
            try:
                tracks.append({"file": n, "title": str(ID3(os.path.join(folder, n)).get("TIT2") or n[:-4])})
            except Exception:
                tracks.append({"file": n, "title": n[:-4]})
        try:
            t = ID3(os.path.join(folder, names[0]))
        except Exception:
            t = {}
        get = lambda k: str(t.get(k) or "") if t != {} else ""
        return {"artist_dir": ad, "album_dir": al, "artist": get("TPE2") or get("TPE1") or ad, "album": get("TALB") or al,
                "year": get("TDRC")[:4], "genre": get("TCON"), "label": get("TPUB"), "tracks": tracks}

    def write_tags(self, artist: str, title: str, fields: dict, titles: dict, rename: bool) -> dict:
        from mutagen.id3 import ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPUB, TXXX
        d = self.album_detail(artist, title)
        if not d:
            raise LookupError("that album is no longer in the folder")
        folder = os.path.join(self.out_root, d["artist_dir"], d["album_dir"])
        names = {}
        for t in d["tracks"]:
            path = os.path.join(folder, t["file"])
            new = (titles.get(t["file"]) or "").strip() or t["title"]
            names[t["file"]] = new
            tags = ID3(path)
            tags.setall("TIT2", [TIT2(encoding=3, text=new)])
            if fields["artist"]:
                if str(tags.get("TPE1") or "") in ("", d["artist"]):
                    tags.setall("TPE1", [TPE1(encoding=3, text=fields["artist"])])
                tags.setall("TPE2", [TPE2(encoding=3, text=fields["artist"])])
            for key, frame, value in (("album", TALB, fields["album"]), ("year", TDRC, fields["year"]), ("genre", TCON, fields["genre"]), ("label", TPUB, fields["label"])):
                frame_id = frame.__name__
                if value:
                    tags.setall(frame_id, [frame(encoding=3, text=value)])
                elif key != "album":
                    tags.delall(frame_id)
            tags.delall("TXXX:ALBUMDL_REVIEWED")
            tags.add(TXXX(encoding=3, desc="ALBUMDL_REVIEWED", text=["1"]))
            tags.save(path, v1=2, v2_version=3)
        new_artist, new_album = fields["artist"] or d["artist"], fields["album"] or d["album"]
        if rename:
            target = os.path.join(self.out_root, pipeline.safe(new_artist), pipeline.safe(new_album))
            if os.path.abspath(target).lower() != os.path.abspath(folder).lower() and os.path.exists(target):
                raise FileExistsError("a folder with that artist and album name already exists")
            for old, new in names.items():
                prefix = old.split(" - ", 1)[0] + " - " if " - " in old else ""
                dest = os.path.join(folder, f"{prefix}{pipeline.safe(new)}.mp3")
                if dest != os.path.join(folder, old) and not os.path.exists(dest):
                    os.replace(os.path.join(folder, old), dest)
                    for ext in (".lrc", ".txt"):
                        side = os.path.join(folder, old[:-4] + ext)
                        if os.path.exists(side):
                            os.replace(side, dest[:-4] + ext)
            if os.path.abspath(target) != os.path.abspath(folder):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.rename(folder, target)
                try:
                    os.rmdir(os.path.dirname(folder))
                except OSError:
                    pass
        self.refresh()
        return {"artist": new_artist, "album": new_album}

    def incomplete_albums(self, only: Optional[set] = None) -> List[dict]:
        from mutagen.id3 import ID3
        out = []
        for row in self.albums():
            if not row["incomplete"] or (only is not None and (row["artist"], row["title"]) not in only):
                continue
            row = dict(row, group_id="", mbid="")
            songs = sorted(f.path for f in os.scandir(os.path.join(self.out_root, row["artist"], row["title"]))
                           if f.name.lower().endswith(".mp3") and not f.name.startswith("."))
            if songs:
                try:
                    id3 = ID3(songs[0])
                    row["group_id"] = next((str(f.text[0]) for f in id3.getall("TXXX") if f.desc == "MusicBrainz Release Group Id" and f.text), "")
                    row["mbid"] = next((str(f.text[0]) for f in id3.getall("TXXX") if f.desc == "MusicBrainz Album Id" and f.text), "")
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
             "gaps": [g for g, miss in (("genre", not self.album.genres), ("year", len(self.album.date or self.album.original_date or "") < 4)) if miss],
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

    def submit(self, album: mb.Album, replace: bool = False) -> Job:
        job = Job(album, pipeline.Options(out_root=self.out_root, jobs=self.parallel, replace=replace))
        with self.lock:
            self.jobs[job.id] = job
            self.order.append(job.id)
            done = [i for i in self.order if self.jobs[i].status in FINISHED]
            for i in done[:max(0, len(done) - 300)]:
                del self.jobs[i]
                self.order.remove(i)
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
            if skip_owned and have and not have["incomplete"] and not have.get("picked"):
                batch.skipped += 1
                self.events.emit("info", "queue", f"discography: skipped {g['title']} (already in the folder)")
                continue
            try:
                if not g["id"]:
                    g["id"] = mb.find_group(g["artist"], g["title"])
                if not g["id"] and len(g.get("tracks") or []) == 1:
                    alt = mb.find_song_group(g["artist"], g["tracks"][0]["title"])
                    if alt:
                        self.events.emit("info", "queue", f"{g['tracks'][0]['title']}: saving it with {alt['title']}, the album MusicBrainz has it on")
                        g["id"], g["title"] = alt["id"], alt["title"]
                if not g["id"]:
                    raise RuntimeError("no matching album on MusicBrainz")
                release = g.get("release")
                if not release:
                    editions = mb.group_editions(g["id"])
                    if not editions:
                        raise RuntimeError("no releases listed")
                    release = editions[0]["id"]
                album = mb.get_release(release)
                if g.get("release") and not g.get("folder") and album.edition and album.edition.lower() not in album.title.lower():
                    album.title = f"{album.title} ({album.edition})"
                album.folder = g.get("folder") or self.library.folder_of(album.artist, album.title) or ""
                if g.get("tracks"):
                    picked = pick_tracks(album.tracks, g["tracks"])
                    if not picked:
                        raise RuntimeError("those songs are not on the MusicBrainz release")
                    album.disc_counts = dict(collections.Counter(t.disc for t in album.tracks))
                    album.tracks, album.partial = picked, True
                self.submit(album, bool(g.get("replace")))
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
