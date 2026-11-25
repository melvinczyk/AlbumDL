import functools
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import requests
from fastapi import Body, Depends, FastAPI, Path as PathParam, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from ..media import pipeline, tagging
from ..sources import musicbrainz as mb
from ..sources import lyrics, youtube
from ..sources import discover, genremap
from ..sources.similar import artist_photo, deeper, similar_artists
from .jobs import FINISHED, EventLog, JobManager, Library

WEB = Path(__file__).parent.parent / "web"
MBID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
MbId = Annotated[str, PathParam(pattern=f"^{MBID}$")]
JobId = Annotated[str, PathParam(pattern="^[0-9a-f]{10}$")]


@dataclass(frozen=True)
class Settings:
    out_root: str = "./out"
    jobs: int = 4
    host: str = "127.0.0.1"
    port: int = 8080

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(env("OUTPUT_DIR", "./out"), int(env("ALBUMDL_JOBS", "4")), env("ALBUMDL_HOST", "127.0.0.1"), int(env("ALBUMDL_PORT", "8080")))


def same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "cross-origin request refused")


def guarded(fn):
    """MusicBrainz code exits with a message when it is unreachable; turn that into a 503."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except SystemExit as exc:
            raise HTTPException(503, str(exc).replace("error: ", ""))
    return wrapper


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, dependencies=[Depends(same_origin)])
    events = EventLog()
    library = Library(settings.out_root)
    jobs = JobManager(settings.out_root, settings.jobs, events, library)
    mb.HOOK = lambda level, msg: events.emit(level, "musicbrainz", msg)

    @app.exception_handler(HTTPException)
    async def http_error(_, exc):
        return JSONResponse({"error": exc.detail}, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def bad_request(_, exc):
        return JSONResponse({"error": "bad request"}, 400)

    @app.exception_handler(Exception)
    async def server_error(request, exc):
        events.emit("error", "server", f"{request.url.path}: {exc}")
        return JSONResponse({"error": str(exc) or repr(exc)}, 500)

    @app.middleware("http")
    async def no_store(request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif request.url.path.endswith((".js", ".css")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    def find_job(jid: str):
        job = jobs.jobs.get(jid)
        if not job:
            raise HTTPException(404, "no such job")
        return job

    @app.get("/")
    def index():
        return FileResponse(WEB / "templates" / "index.html")

    @app.get("/api/config")
    def config():
        return {"out_root": settings.out_root}

    @app.get("/api/stats")
    def stats():
        folder = settings.out_root if os.path.isdir(settings.out_root) else os.path.dirname(settings.out_root) or "."
        try:
            free = shutil.disk_usage(folder).free
        except OSError:
            free = 0
        m = dict(mb.STATS)
        return {**jobs.totals(), "disk": {"free": free}, "mb": {**m, "avg_ms": round(m["ms"] / max(1, m["requests"] - m["errors"]))}}

    @app.get("/api/logs")
    def logs(since: int = 0):
        return events.since(since)

    @app.get("/api/cover")
    def cover(release: str = "", group: str = "", artist: str = "", title: str = "", size: int = 250):
        if not all(re.fullmatch(MBID, x) for x in (release, group) if x):
            raise HTTPException(400, "bad id")
        data = tagging.cover_for(release, group, artist[:120], title[:120], 500 if size > 250 else 250)
        if not data:
            raise HTTPException(404, "no cover")
        return Response(data, media_type=("image/png" if data.startswith(b"\x89PNG") else "image/jpeg"), headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/search")
    @guarded
    def search(artist: str = "", album: str = "", q: str = ""):
        if q.strip():
            rows = mb.search_text(q.strip()[:200])
            for r in rows:
                r["owned"] = library.owned_album(r["artist"], r["title"])
            return {"results": rows}
        artist, album = artist.strip(), album.strip()
        for text in (album, artist):
            m = re.search(rf"musicbrainz\.org/(release-group|release)/({MBID})", text, re.I)
            if m:
                return {"direct": {"kind": m.group(1).lower(), "id": m.group(2).lower()}}
        if not artist and not album:
            return {"results": []}
        rows = mb.search_albums(artist, album)
        for r in rows:
            r["owned"] = library.owned_album(r["artist"], r["title"])
        events.emit("info", "search", f"search artist={artist!r} album={album!r}: {len(rows)} albums")
        return {"results": rows}

    @app.get("/api/ytm/search")
    def ytm_search(q: str = ""):
        rows = youtube.ytm_search(q.strip()[:200]) if q.strip() else []
        want = {w for w in mb._words(q) if w not in {"by", "album", "ep", "single", "full"}}
        if want:
            def coverage(r):
                return len(want & set(mb._words(f"{r['artist']} {r['title']}"))) / len(want)
            rows = sorted((r for r in rows if coverage(r) >= 0.6), key=lambda r: -coverage(r))
        for r in rows:
            r["owned"] = library.owned_album(r["artist"], r["title"])
        events.emit("info", "search", f"youtube music search {q!r}: {len(rows)} albums")
        return {"results": rows}

    @app.get("/api/yt/search")
    def yt_search(q: str = ""):
        return {"results": youtube.search_uploads(q.strip()[:200]) if q.strip() else []}

    @app.get("/api/yt/playlist")
    def yt_playlist(url: str = ""):
        try:
            return youtube.import_playlist(url.strip()[:300])
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/yt/download")
    def yt_download(body: dict = Body(...)):
        artist, title = str(body.get("artist", "")).strip()[:120], str(body.get("album", "")).strip()[:160]
        tracks = [t for t in body.get("tracks", []) if isinstance(t, dict) and re.fullmatch(r"[\w-]{11}", str(t.get("id", ""))) and str(t.get("title", "")).strip()][:200]
        if not artist or not title or not tracks:
            raise HTTPException(400, "an artist, an album name and at least one track are needed")
        year = re.sub(r"\D", "", str(body.get("year", "")))[:4]
        album = mb.Album(mbid="", group_id="", title=title, artist=artist, artists=[artist], artist_ids=[], date=year, type="Album",
                         genres=[str(body.get("genre", "")).strip()[:40]] if str(body.get("genre", "")).strip() else [],
                         compilation=artist.lower() == "various artists", cover_url=f"https://i.ytimg.com/vi/{tracks[0]['id']}/maxresdefault.jpg")
        for n, t in enumerate(tracks, 1):
            secs = t.get("duration") if isinstance(t.get("duration"), (int, float)) else None
            start, end = (float(t["start"]), float(t["end"])) if isinstance(t.get("start"), (int, float)) and isinstance(t.get("end"), (int, float)) and t["end"] > t["start"] >= 0 else (0.0, 0.0)
            album.tracks.append(mb.Track(title=str(t["title"]).strip()[:200], number=n, disc=1, length_ms=int(secs * 1000) if secs else None,
                                         artist=artist, artists=[artist], artist_ids=[], recording_id="", track_id=f"{t['id']}-{n}", source=t["id"], start=start, end=end))
        if (pipeline.safe(artist), pipeline.safe(title)) in {(pipeline.safe(j.album.artist), pipeline.safe(j.album.title)) for j in jobs.listing() if j.status in ("queued", "running")}:
            return {"queued": 0, "duplicates": 1}
        jobs.submit(album)
        return {"queued": 1, "duplicates": 0}

    @app.post("/api/ytm/download")
    def ytm_download(body: dict = Body(...)):
        ids = [str(i) for i in body.get("albums", []) if re.fullmatch(r"MPREb_[\w-]+", str(i))][:20]
        if not ids:
            raise HTTPException(400, "nothing valid to download")
        busy = {(pipeline.safe(j.album.artist), pipeline.safe(j.album.title)) for j in jobs.listing() if j.status in ("queued", "running")}
        queued = duplicates = 0
        for browse_id in ids:
            album = youtube.ytm_album(browse_id)
            if (pipeline.safe(album.artist), pipeline.safe(album.title)) in busy:
                duplicates += 1
            else:
                jobs.submit(album)
                queued += 1
        return {"queued": queued, "duplicates": duplicates}

    @app.get("/api/artists")
    @guarded
    def artists(q: str = ""):
        return {"artists": mb.search_artists(q.strip(), 40) if q.strip() else []}

    @app.get("/api/artist-info/{aid}")
    @guarded
    def artist_info(aid: MbId):
        return mb.artist_info(aid)

    @app.get("/api/artist/{aid}")
    @guarded
    def artist(aid: MbId):
        albums = mb.artist_albums(aid)
        for r in albums:
            r["owned"] = library.owned_album(r["artist"], r["title"])
        return {"albums": albums}

    @app.get("/api/similar/{aid}")
    def similar(aid: MbId, name: str = "", mode: str = ""):
        if mode == "deep":
            rows, genres = deeper(aid, name[:120]), []
        else:
            d = similar_artists(aid, name[:120])
            rows, genres = d["artists"][:24], d["genres"]
        return {"artists": [dict(a, owned=library.has_artist(a["name"])) for a in rows], "genres": genres}

    @app.get("/api/discover/feed")
    def discover_feed(shuffle: int = 0):
        return discover.feed(library.albums(), library.owned_album, library.has_artist, shuffle)

    @app.post("/api/discover/feed")
    def discover_area(body: dict = Body(default={})):
        area = [str(n)[:200] for n in body.get("artists") or [] if isinstance(n, str)][:400]
        return discover.feed(library.albums(), library.owned_album, library.has_artist, int(body.get("shuffle") or 0), area or None)

    @app.get("/api/discover/map")
    def discover_map():
        return genremap.music_map(library.albums())

    @app.get("/api/discover/album")
    def discover_album(artist: str = "", album: str = ""):
        if not artist.strip() or not album.strip():
            raise HTTPException(400, "artist and album are needed")
        return discover.like_album(artist.strip()[:120], album.strip()[:200], library.owned_album)

    @app.get("/api/artist-img")
    def artist_img(name: str = ""):
        data = artist_photo(name.strip()[:120]) if name.strip() else None
        if not data:
            raise HTTPException(404, "no photo")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/group/{gid}")
    @guarded
    def group(gid: MbId):
        return {"editions": mb.group_editions(gid)}

    @app.get("/api/release/{rid}")
    @guarded
    def release(rid: MbId):
        album = mb.get_release(rid)
        folder = library.folder_of(album.artist, album.title) or pipeline.album_folder(album, settings.out_root)
        have = pipeline.present(folder, album.tracks, lambda t: pipeline.track_file(album, t))
        return {
            "mbid": album.mbid, "group_id": album.group_id, "title": album.title, "artist": album.artist,
            "date": album.date, "original_date": album.original_date, "label": album.label, "genres": album.genres,
            "disc_total": album.disc_total, "total_seconds": sum(t.length_ms or 0 for t in album.tracks) // 1000,
            "folder": folder, "owned": library.owned_album(album.artist, album.title),
            "tracks": [{"disc": t.disc, "number": t.number, "title": t.title,
                        "seconds": round(t.length_ms / 1000) if t.length_ms else None,
                        "artist": t.artist if t.artist != album.artist else "", "recording": t.recording_id,
                        "have": i in have} for i, t in enumerate(album.tracks, 1)],
        }

    @app.get("/api/jobs")
    def list_jobs():
        return {"jobs": [j.summary() for j in jobs.listing()], "batches": [b.summary() for b in reversed(list(jobs.batches.values()))][:5]}

    @app.get("/api/jobs/{jid}")
    def job_detail(jid: JobId):
        return find_job(jid).summary(full=True)

    @app.post("/api/download")
    @guarded
    def download(body: dict = Body(...)):
        mbid = str(body.get("mbid", "")).lower()
        if not re.fullmatch(MBID, mbid):
            raise HTTPException(400, "bad release id")
        return {"job": jobs.submit(mb.get_release(mbid)).summary()}

    @app.post("/api/discography")
    @guarded
    def discography(body: dict = Body(...)):
        groups = [{"id": str(g.get("id", "")).lower(), "title": str(g.get("title", "")), "artist": str(g.get("artist", "")),
                   "release": r if re.fullmatch(MBID, r := str(g.get("release", "")).lower()) else "", "replace": bool(body.get("replace"))}
                  for g in body.get("groups", []) if isinstance(g, dict)][:1500]
        if not groups or not all(re.fullmatch(MBID, g["id"]) for g in groups):
            raise HTTPException(400, "nothing valid to download")
        name = str(body.get("name") or groups[0]["artist"])[:120]
        queued = jobs.active_groups()
        fresh = [g for g in groups if g["id"] not in queued]
        if not fresh:
            return {"batch": None, "duplicates": len(groups)}
        return {"batch": jobs.discography(name, fresh, bool(body.get("skip_owned", True))).summary(), "duplicates": len(groups) - len(fresh)}

    @app.post("/api/songs")
    def songs(body: dict = Body(...)):
        """Single songs, each saved into its album's folder (Artist/Album/## - Title.mp3) like a partial album download."""
        groups = []
        for g in body.get("groups", [])[:50]:
            if not isinstance(g, dict):
                continue
            gid, rid = str(g.get("id") or "").lower(), str(g.get("release") or "").lower()
            tracks = [{"recording": str(t.get("recording") or "")[:36], "title": str(t.get("title") or "")[:200]}
                      for t in (g.get("tracks") or [])[:200] if isinstance(t, dict) and (t.get("recording") or t.get("title"))]
            artist, title = str(g.get("artist") or "").strip()[:200], str(g.get("title") or "").strip()[:200]
            if (gid and not re.fullmatch(MBID, gid)) or (rid and not re.fullmatch(MBID, rid)) or not tracks or not artist or not title:
                raise HTTPException(400, "each album needs an artist, a title and at least one song")
            groups.append({"id": gid, "release": rid, "artist": artist, "title": title, "tracks": tracks})
        if not groups:
            raise HTTPException(400, "no songs to download")
        n = sum(len(g["tracks"]) for g in groups)
        name = str(body.get("name") or (groups[0]["tracks"][0]["title"] if n == 1 else f"{n} songs"))[:120]
        return {"batch": jobs.discography(name, groups, False).summary(), "songs": n}

    @app.get("/api/artist-top/{aid}")
    def artist_top(aid: MbId, name: str = ""):
        return discover.artist_top(aid, name[:120], library.songs_in)

    @app.get("/api/library")
    def library_list():
        return {"albums": library.albums()}

    thumbs: dict = {}

    @app.get("/api/library/art")
    def library_art(artist: str = "", album: str = ""):
        path = library.cover_path(artist[:200], album[:200])
        if not path:
            raise HTTPException(404, "no cover")
        key = (path, os.path.getmtime(path))
        if key not in thumbs:
            if len(thumbs) > 600:
                thumbs.pop(next(iter(thumbs)))
            with open(path, "rb") as f:
                thumbs[key] = tagging.to_jpeg(f.read(), max_side=200)
        return Response(thumbs[key], media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})

    @app.post("/api/library/link")
    @guarded
    def library_link(body: dict = Body(...)):
        artist, title, file = (str(body.get(k, "")).strip()[:200] for k in ("artist", "album", "file"))
        video = re.search(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/)([\w-]{11})", str(body.get("url", "")))
        if not video:
            raise HTTPException(400, "that is not a YouTube video link")
        row = next(iter(library.incomplete_albums({(artist, title)})), None)
        m = re.match(r"(?:(\d+)-)?(\d+) - ", file)
        if not row or not m:
            raise HTTPException(404, "that album or track is no longer listed as incomplete")
        if row["mbid"]:
            album = mb.get_release(row["mbid"])
        else:
            gid = row["group_id"] or (next(iter(mb.search_albums(row["artist"], row["title"], limit=5)), {}).get("id", ""))
            editions = mb.group_editions(gid) if gid else []
            if not editions:
                raise HTTPException(404, "MusicBrainz has no release for this album, so the track cannot be matched")
            album = mb.get_release(editions[0]["id"])
        disc, number = (int(m.group(1)) if m.group(1) else None), int(m.group(2))
        track = next((t for t in album.tracks if t.number == number and (disc is None or t.disc == disc)), None)
        if not track:
            raise HTTPException(404, "that track number is not in the MusicBrainz release")
        counts = {}
        for t in album.tracks:
            counts[t.disc] = counts.get(t.disc, 0) + 1
        album.disc_counts, album.partial = counts, True
        album.folder = os.path.join(settings.out_root, row["artist"], row["title"])
        track.source = video.group(1)
        album.tracks = [track]
        jobs.submit(album)
        return {"queued": 1}

    def apply_cover(artist: str, album: str, data: bytes, square: bool) -> dict:
        if not data or len(data) > 20_000_000:
            raise HTTPException(400, "the image is empty or larger than 20 MB")
        d = library.album_detail(artist[:200], album[:200])
        if not d:
            raise HTTPException(404, "album not found")
        if (pipeline.safe(d["artist"]), pipeline.safe(d["album"])) in {(pipeline.safe(j.album.artist), pipeline.safe(j.album.title)) for j in jobs.listing() if j.status in ("queued", "running")}:
            raise HTTPException(409, "that album is still downloading")
        try:
            tagging.set_cover(os.path.join(settings.out_root, d["artist_dir"], d["album_dir"]), data, square)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.post("/api/library/cover")
    async def library_cover(request: Request, artist: str = "", album: str = "", square: int = 1):
        data = await request.body()
        return await run_in_threadpool(apply_cover, artist, album, data, bool(square))

    @app.post("/api/library/cover-url")
    def library_cover_url(body: dict = Body(...)):
        url = str(body.get("url", "")).strip()
        if not re.match(r"https?://", url, re.I):
            raise HTTPException(400, "paste a full image link starting with http")
        try:
            with requests.get(url, stream=True, timeout=20, headers={"User-Agent": mb.UA}) as r:
                r.raise_for_status()
                data = r.raw.read(20_000_001, decode_content=True)
        except requests.RequestException as exc:
            raise HTTPException(400, f"could not download that link ({type(exc).__name__})")
        return apply_cover(str(body.get("artist", "")), str(body.get("album", "")), data, bool(body.get("square", True)))

    def lyric_meta(path: str, d: dict):
        from mutagen.mp3 import MP3
        m = MP3(path)
        tags = m.tags or {}
        return (str(tags.get("TPE1") or d["artist"]), str(tags.get("TIT2") or ""), str(tags.get("TALB") or d["album"]), m.info.length)

    def lyric_album(artist: str, album: str):
        d = library.album_detail(artist[:200], album[:200])
        if not d:
            raise HTTPException(404, "album not found")
        return d, os.path.join(settings.out_root, d["artist_dir"], d["album_dir"])

    def find_lyrics(d: dict, folder: str, files: list, overwrite: bool) -> dict:
        def one(name: str):
            path = os.path.join(folder, name)
            if not overwrite and lyrics.existing(path):
                return name, lyrics.state(path)
            try:
                a, t, al, dur = lyric_meta(path, d)
                res = lyrics.find(a, t, al, dur)
                return name, lyrics.save(path, res["synced"] or res["plain"]) if res else lyrics.state(path)
            except Exception:
                return name, lyrics.state(path)
        with ThreadPoolExecutor(max_workers=4) as pool:
            return dict(pool.map(one, files))

    @app.get("/api/library/lyrics")
    def lyrics_list(artist: str = "", album: str = ""):
        d, folder = lyric_album(artist, album)
        return {"tracks": [{"file": t["file"], "title": t["title"], "state": lyrics.state(os.path.join(folder, t["file"]))} for t in d["tracks"]]}

    @app.post("/api/library/lyrics/find")
    def lyrics_find(body: dict = Body(...)):
        d, folder = lyric_album(str(body.get("artist", "")), str(body.get("album", "")))
        names = [t["file"] for t in d["tracks"]]
        files = [f for f in (body.get("files") or names) if f in names]
        return {"states": find_lyrics(d, folder, files, bool(body.get("overwrite")))}

    @app.post("/api/library/lyrics/save")
    async def lyrics_save(request: Request, artist: str = "", album: str = "", file: str = ""):
        raw = await request.body()
        if not raw or len(raw) > 1_000_000:
            raise HTTPException(400, "the lyrics are empty or larger than 1 MB")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
        d, folder = lyric_album(artist, album)
        if file not in [t["file"] for t in d["tracks"]]:
            raise HTTPException(404, "track not found")
        return {"state": await run_in_threadpool(lyrics.save, os.path.join(folder, file), text)}

    lyric_run = {"running": False}

    @app.post("/api/library/lyrics/find-all")
    def lyrics_find_all(body: dict = Body(default={})):
        if lyric_run["running"]:
            raise HTTPException(409, "already searching for lyrics")
        only = {(str(a.get("artist", "")), str(a.get("title", ""))) for a in body.get("albums", []) if isinstance(a, dict)} or None
        lyric_run["running"] = True

        def run():
            found = total = 0
            events.emit("info", "lyrics", "looking for missing lyrics")
            try:
                for row in library.albums():
                    if row["lyrics"] >= row["tracks"] or (only is not None and (row["artist"], row["title"]) not in only):
                        continue
                    try:
                        d, folder = lyric_album(row["artist"], row["title"])
                        missing = [t["file"] for t in d["tracks"] if lyrics.state(os.path.join(folder, t["file"])) == "none"]
                        got = sum(1 for s in find_lyrics(d, folder, missing, False).values() if s != "none")
                    except Exception as exc:
                        events.emit("warn", "lyrics", f"{row['artist']} - {row['title']}: {str(exc)[:100]}")
                        continue
                    found, total = found + got, total + len(missing)
                    events.emit("ok" if got == len(missing) else "info", "lyrics", f"{row['artist']} - {row['title']}: {got} of {len(missing)} found")
            finally:
                lyric_run["running"] = False
            events.emit("ok", "lyrics", f"lyrics done: {found} of {total} found")
        threading.Thread(target=run, daemon=True).start()
        return {"started": True}

    @app.get("/api/library/album")
    def library_album(artist: str = "", album: str = ""):
        d = library.album_detail(artist[:200], album[:200])
        if not d:
            raise HTTPException(404, "album not found")
        return d

    @app.post("/api/library/tags")
    def library_tags(body: dict = Body(...)):
        clean = lambda v: str(v or "").strip()[:200]
        f = body.get("fields") or {}
        fields = {k: clean(f.get(k)) for k in ("artist", "album", "year", "genre", "label")}
        if fields["year"] and not re.fullmatch(r"\d{4}(-\d{2}-\d{2})?", fields["year"]):
            raise HTTPException(400, "the year should look like 2012")
        titles = {str(t.get("file")): clean(t.get("title")) for t in body.get("tracks", []) if isinstance(t, dict) and os.path.basename(str(t.get("file"))) == str(t.get("file"))}
        artist, album = clean(body.get("artist")), clean(body.get("album"))
        if (pipeline.safe(artist), pipeline.safe(album)) in {(pipeline.safe(j.album.artist), pipeline.safe(j.album.title)) for j in jobs.listing() if j.status in ("queued", "running")}:
            raise HTTPException(409, "that album is still downloading")
        try:
            return library.write_tags(artist, album, fields, titles, bool(body.get("rename")))
        except LookupError as exc:
            raise HTTPException(404, str(exc))
        except FileExistsError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/library/retry-incomplete")
    @guarded
    def retry_incomplete(body: dict = Body(default={})):
        only = {(str(a.get("artist", "")), str(a.get("title", ""))) for a in body.get("albums", []) if isinstance(a, dict)} if body.get("albums") else None
        groups, queued = [], jobs.active_groups()
        for row in library.incomplete_albums(only):
            gid = row["group_id"]
            if not gid:
                found = mb.search_albums(row["artist"], row["title"], limit=5)
                gid = found[0]["id"] if found else ""
            if gid and gid not in queued:
                groups.append({"id": gid, "title": row["title"], "artist": row["artist"], "release": row["mbid"],
                               "folder": os.path.join(settings.out_root, row["artist"], row["title"])})
        if groups:
            jobs.discography("Incomplete albums", groups, False)
        return {"queued": len(groups)}

    @app.post("/api/library/delete")
    def delete_albums(body: dict = Body(...)):
        albums = [(str(a.get("artist", "")), str(a.get("title", ""))) for a in body.get("albums", []) if isinstance(a, dict)][:5000]
        busy = {(pipeline.safe(j.album.artist), pipeline.safe(j.album.title)) for j in jobs.listing() if j.status in ("queued", "running")}
        deleted = skipped = 0
        for artist, title in albums:
            if (pipeline.safe(artist), pipeline.safe(title)) in busy:
                skipped += 1
            elif library.remove_album(artist, title):
                deleted += 1
                events.emit("warn", "library", f"deleted {artist} - {title}")
        return {"deleted": deleted, "skipped": skipped}

    def lib_file(rel: str) -> str:
        root = os.path.realpath(settings.out_root)
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            raise HTTPException(404, f"not in the library: {rel[:120]}")
        return full

    def export_list(only) -> list:
        root, out = settings.out_root, []
        folders = [os.path.join(root, a, t) for a, t in only] if only is not None else [root]
        for top in folders:
            for dirpath, dirs, files in os.walk(top):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for name in sorted(files):
                    if name.startswith(".") or name.endswith(".part") or name == pipeline.INCOMPLETE:
                        continue
                    st = os.stat(os.path.join(dirpath, name))
                    rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
                    out.append({"path": rel, "size": st.st_size, "mtime": int(st.st_mtime)})
        return out

    @app.post("/api/export/manifest")
    def export_manifest(body: dict = Body(default={})):
        only = None if body.get("all") else [(str(a.get("artist", "")), str(a.get("title", ""))) for a in body.get("albums", []) if isinstance(a, dict)]
        files = export_list(only)
        return {"files": files, "bytes": sum(f["size"] for f in files)}

    @app.get("/api/export/file")
    def export_file(path: str = ""):
        return FileResponse(lib_file(path), media_type="application/octet-stream")

    crcs: dict = {}

    def crc_of(full: str) -> str:
        st = os.stat(full)
        key = (full, st.st_mtime, st.st_size)
        if key not in crcs:
            crc = 0
            with open(full, "rb") as f:
                while chunk := f.read(1 << 20):
                    crc = zlib.crc32(chunk, crc)
            crcs[key] = f"{crc & 0xFFFFFFFF:08x}"
        return crcs[key]

    @app.post("/api/export/hashes")
    def export_hashes(body: dict = Body(...)):
        paths = [str(p) for p in body.get("paths", [])][:20000]
        with ThreadPoolExecutor(max_workers=4) as pool:
            return dict(zip(paths, pool.map(lambda p: crc_of(lib_file(p)), paths)))

    zips: dict = {}

    @app.post("/api/export/prepare")
    def export_prepare(body: dict = Body(...)):
        paths = [str(p) for p in body.get("paths", [])]
        for p in paths:
            lib_file(p)
        for k in [k for k, v in zips.items() if time.time() - v[0] > 3600]:
            zips.pop(k, None)
        token = uuid.uuid4().hex
        zips[token] = (time.time(), paths)
        return {"token": token}

    class _Sink:
        def __init__(self):
            self.parts = []

        def write(self, b):
            self.parts.append(bytes(b))
            return len(b)

        def flush(self):
            pass

        def take(self) -> bytes:
            data, self.parts = b"".join(self.parts), []
            return data

    @app.get("/api/export/zip")
    def export_zip(token: str = ""):
        hit = zips.get(token)
        if not hit:
            raise HTTPException(404, "that download has expired; start the export again")

        def stream():
            sink = _Sink()
            with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for rel in hit[1]:
                    try:
                        zf.write(lib_file(rel), arcname=rel)
                    except HTTPException:
                        continue
                    yield sink.take()
            yield sink.take()
        name = time.strftime("albumdl-%Y-%m-%d.zip")
        return StreamingResponse(stream(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    covers = {"running": False}

    @app.post("/api/library/covers")
    def find_covers():
        if covers["running"]:
            raise HTTPException(409, "already searching for covers")
        covers["running"] = True

        def run():
            events.emit("info", "covers", "looking for albums without cover.jpg")
            try:
                tagging.fix_covers(settings.out_root, lambda level, msg: events.emit(level, "covers", msg))
            finally:
                covers["running"] = False
        threading.Thread(target=run, daemon=True).start()
        return {"started": True}

    @app.post("/api/batches/{bid}/cancel")
    def cancel_batch(bid: JobId):
        batch = jobs.batches.get(bid)
        if not batch:
            raise HTTPException(404, "no such batch")
        batch.cancel.set()
        return {"ok": True}

    @app.post("/api/jobs/clear")
    def clear_jobs():
        return {"cleared": jobs.clear_finished()}

    @app.post("/api/jobs/{jid}/cancel")
    def cancel_job(jid: JobId):
        jobs.cancel(find_job(jid))
        return {"ok": True}

    @app.post("/api/jobs/{jid}/retry")
    def retry_job(jid: JobId):
        job = find_job(jid)
        if job.status not in FINISHED:
            raise HTTPException(409, "job is still active")
        return {"job": jobs.retry(job).summary()}

    app.mount("/static", StaticFiles(directory=WEB / "static"))
    events.emit("info", "server", f"started, saving to {settings.out_root}")
    return app
