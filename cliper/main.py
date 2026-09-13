"""Cliper — find the most-watched parts of a YouTube/Twitch video and cut them into Shorts."""
from __future__ import annotations

import io
import secrets
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import copyright, sources
from .config import DATA_DIR, PASSWORD, WEB_DIR
from .jobs import JobManager

app = FastAPI(title="Cliper")
manager = JobManager()


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Opt-in password gate (CLIPER_PASSWORD) for when the server is reachable from the internet."""
    if PASSWORD:
        import base64  # noqa: PLC0415

        ok = False
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                _, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw, PASSWORD)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Response("Password required", 401, headers={"WWW-Authenticate": 'Basic realm="Cliper"'})
    return await call_next(request)


class AnalyzeIn(BaseModel):
    url: str
    force: bool = False  # re-analyse even if this video already has a job


class GenerateIn(BaseModel):
    count: int = 10
    min_len: int = 8
    max_len: int = 15
    layout: str = "auto"
    mode: str | None = None
    captions: str = "auto"
    # style overrides; None = use the detected mode's defaults
    punch: bool | None = None
    progress: bool | None = None
    title: bool | None = None
    grade: bool | None = None
    mirror: bool | None = None
    speed: float | None = None
    pitch: float | None = None
    max_height: int | None = None  # source resolution cap; default = best available


@app.post("/api/analyze")
def analyze(body: AnalyzeIn):
    try:
        job = manager.analyze(body.url, force=body.force)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return job.public()


@app.get("/api/jobs")
def list_jobs():
    return manager.list()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.public()


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    manager.delete(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/generate")
def generate(job_id: str, body: GenerateIn):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if job.public().get("status") not in ("ready", "done", "error"):
        raise HTTPException(409, "job is busy")
    if not (job.dir / "signal.npy").exists():
        raise HTTPException(409, "job has not been analyzed")
    manager.generate(job, body.model_dump())
    return job.public()


@app.get("/api/jobs/{job_id}/clips/{name}")
def clip_file(job_id: str, name: str):
    job = manager.get(job_id)
    path = job.dir / "clips" / name if job else None
    if not path or not path.is_file() or not name.endswith(".mp4") or "/" in name:
        raise HTTPException(404, "clip not found")
    return FileResponse(path, media_type="video/mp4", filename=f"{job_id}_{name}")


@app.get("/api/jobs/{job_id}/clips.zip")
def clips_zip(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    files = sorted((job.dir / "clips").glob("clip_*.mp4"))
    if not files:
        raise HTTPException(404, "no clips yet")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for f in files:
            z.write(f, f.name)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="cliper_{job_id}.zip"'})


@app.post("/api/jobs/{job_id}/clips/{name}/check")
def check_clip(job_id: str, name: str):
    """Copyright-risk estimate for a rendered clip, compared against its own source segment."""
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    data = job.public()
    clip = next((c for c in data.get("clips", []) if c.get("file") == name and c.get("status") == "done"), None)
    if not clip:
        raise HTTPException(404, "clip not found")
    info_path = job.dir / "info.info.json"
    info = __import__("json").loads(info_path.read_text()) if info_path.exists() else None
    opts = data.get("options") or {}
    with tempfile.TemporaryDirectory(dir=DATA_DIR) as tmp:
        report = copyright.assess(job.dir / "clips" / name, Path(tmp), info, clip["start"],
                                  float(opts.get("speed") or 1.0), bool(opts.get("mirror")))
    manager._set_clip(job, clip["rank"], check=report)
    return report


@app.post("/api/check")
def check_upload(file: UploadFile = File(...), source_url: str = Form(""), source_start: float = Form(0.0),
                 speed: float = Form(1.0)):
    """Copyright-risk estimate for any uploaded video; add the source URL + start time to compare."""
    work = DATA_DIR / "checks" / uuid.uuid4().hex[:10]
    work.mkdir(parents=True, exist_ok=True)
    try:
        clip = work / ("upload" + Path(file.filename or "v.mp4").suffix.lower())
        with clip.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        info = None
        if source_url.strip():
            try:
                info = sources.fetch_info(sources.normalize_url(source_url), work, want_subs=False)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(400, f"could not read source URL: {e}")
        return copyright.assess(clip, work, info, source_start if info else None, speed or 1.0)
    finally:
        shutil.rmtree(work, ignore_errors=True)


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
