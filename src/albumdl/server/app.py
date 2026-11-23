import functools
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from fastapi import Body, Depends, FastAPI, Path as PathParam, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from ..media import pipeline, tagging
from ..sources import musicbrainz as mb
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
    def search(artist: str = "", album: str = ""):
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

    @app.get("/api/artists")
    @guarded
    def artists(q: str = ""):
        return {"artists": mb.search_artists(q.strip()) if q.strip() else []}

    @app.get("/api/artist/{aid}")
    @guarded
    def artist(aid: MbId):
        albums = mb.artist_albums(aid)
        for r in albums:
            r["owned"] = library.owned_album(r["artist"], r["title"])
        return {"albums": albums}

    @app.get("/api/group/{gid}")
    @guarded
    def group(gid: MbId):
        return {"editions": mb.group_editions(gid)}

    @app.get("/api/release/{rid}")
    @guarded
    def release(rid: MbId):
        album = mb.get_release(rid)
        folder = pipeline.album_folder(album, settings.out_root)
        return {
            "mbid": album.mbid, "group_id": album.group_id, "title": album.title, "artist": album.artist,
            "date": album.date, "original_date": album.original_date, "label": album.label, "genres": album.genres,
            "disc_total": album.disc_total, "total_seconds": sum(t.length_ms or 0 for t in album.tracks) // 1000,
            "folder": folder, "owned": library.owned_album(album.artist, album.title),
            "tracks": [{"disc": t.disc, "number": t.number, "title": t.title,
                        "seconds": round(t.length_ms / 1000) if t.length_ms else None,
                        "artist": t.artist if t.artist != album.artist else ""} for t in album.tracks],
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
        groups = [{"id": str(g.get("id", "")).lower(), "title": str(g.get("title", "")), "artist": str(g.get("artist", ""))}
                  for g in body.get("groups", []) if isinstance(g, dict)][:150]
        if not groups or not all(re.fullmatch(MBID, g["id"]) for g in groups):
            raise HTTPException(400, "nothing valid to download")
        name = str(body.get("name") or groups[0]["artist"])[:120]
        return {"batch": jobs.discography(name, groups, bool(body.get("skip_owned", True))).summary()}

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
