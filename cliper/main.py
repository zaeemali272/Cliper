"""Cliper — find the most-watched parts of a YouTube/Twitch video and cut them into Shorts."""
from __future__ import annotations

import io
import secrets
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Dict, Any

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, Depends, UploadFile, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr

from . import auth, billing, copyright, db, sources
from .config import DATA_DIR, PASSWORD, WEB_DIR
from .jobs import JobManager

# Initialize SQLite database on startup
db.init_db()

app = FastAPI(title="Cliper SaaS")
manager = JobManager()

app.include_router(billing.router)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Opt-in password gate (CLIPER_PASSWORD) for when the server is reachable from the internet."""
    if PASSWORD:
        import base64  # noqa: PLC0415

        ok = False
        auth_hdr = request.headers.get("authorization", "")
        if auth_hdr.lower().startswith("basic "):
            try:
                _, _, pw = base64.b64decode(auth_hdr[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw, PASSWORD)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Response("Password required", 401, headers={"WWW-Authenticate": 'Basic realm="Cliper"'})
    return await call_next(request)


# ---------------------------------------------------------------- AUTH MODELS & ENDPOINTS

class SignupIn(BaseModel):
    email: str
    password: str


class LoginIn(BaseModel):
    email: str
    password: str


@app.post("/api/auth/signup")
def signup(body: SignupIn, response: Response):
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Valid email required")
    if len(body.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    existing = db.get_user_by_email(email)
    if existing:
        raise HTTPException(400, "An account with this email already exists")

    pwd_hash, salt = auth.hash_password(body.password)
    user = db.create_user(email, pwd_hash, salt, trial_days=7)
    token = db.create_session(user["id"])

    response.set_cookie(
        key=auth.COOKIE_NAME,
        value=token,
        max_age=30 * 86400,
        httponly=True,
        samesite="lax",
        secure=False  # Set to True if enforcing HTTPS strictly behind reverse proxy
    )
    return {"user": user, "token": token}


@app.post("/api/auth/login")
def login(body: LoginIn, response: Response):
    user = db.get_user_by_email(body.email)
    if not user:
        raise HTTPException(400, "Invalid email or password")

    if not auth.verify_password(body.password, user["password_hash"], user["salt"]):
        raise HTTPException(400, "Invalid email or password")

    token = db.create_session(user["id"])
    response.set_cookie(
        key=auth.COOKIE_NAME,
        value=token,
        max_age=30 * 86400,
        httponly=True,
        samesite="lax"
    )
    return {"user": user, "token": token}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, current_user: Dict[str, Any] = Depends(auth.get_optional_user)):
    token = request.cookies.get(auth.COOKIE_NAME) or request.headers.get("X-Cliper-Session")
    if token:
        db.delete_session(token)
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


@app.get("/api/auth/me")
def get_me(current_user: Dict[str, Any] = Depends(auth.get_current_user)):
    return {
        "user": current_user,
        "quotas": {
            "analyze": db.check_daily_quota(current_user["id"], "analyze"),
            "generate": db.check_daily_quota(current_user["id"], "generate"),
        }
    }


# ---------------------------------------------------------------- COOKIE MANAGMENT ENDPOINTS

class CookieIn(BaseModel):
    cookies: str


@app.get("/api/cookies")
def get_cookie_status():
    cookie_file = DATA_DIR / "cookies.txt"
    return {"has_cookies": cookie_file.is_file() and cookie_file.stat().st_size > 0}


@app.post("/api/cookies")
def save_cookies(body: CookieIn, current_user: Dict[str, Any] = Depends(auth.get_current_user)):
    text = body.cookies.strip()
    if not text:
        raise HTTPException(400, "Cookie text cannot be empty")
    cookie_file = DATA_DIR / "cookies.txt"
    cookie_file.write_text(text)
    return {"ok": True}


@app.delete("/api/cookies")
def clear_cookies(current_user: Dict[str, Any] = Depends(auth.get_current_user)):
    cookie_file = DATA_DIR / "cookies.txt"
    if cookie_file.exists():
        cookie_file.unlink()
    return {"ok": True}


# ---------------------------------------------------------------- JOB ENDPOINTS

class AnalyzeIn(BaseModel):
    url: str
    force: bool = False


class GenerateIn(BaseModel):
    count: int = 10
    min_len: int = 8
    max_len: int = 15
    layout: str = "auto"
    mode: str | None = None
    captions: str = "auto"
    punch: bool | None = None
    progress: bool | None = None
    title: bool | None = None
    grade: bool | None = None
    mirror: bool | None = None
    speed: float | None = None
    pitch: float | None = None
    max_height: int | None = None


@app.post("/api/analyze")
def analyze(body: AnalyzeIn, current_user: Dict[str, Any] = Depends(auth.get_current_user)):
    quota = db.check_daily_quota(current_user["id"], "analyze")
    if not quota["allowed"]:
        raise HTTPException(status_code=429, detail=quota["reason"])

    try:
        job = manager.analyze(body.url, force=body.force)
    except ValueError as e:
        raise HTTPException(400, str(e))

    db.record_usage(current_user["id"], "analyze")
    db.link_job_to_user(job.id, current_user["id"], body.url, job.public().get("title", ""))

    return job.public()


@app.get("/api/jobs")
def list_jobs(current_user: Dict[str, Any] = Depends(auth.get_current_user)):
    user_job_ids = set(db.get_user_job_ids(current_user["id"]))
    return manager.list(allowed_ids=user_job_ids)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, current_user: Dict[str, Any] = Depends(auth.get_current_user)):
    if not db.is_job_owned_by(job_id, current_user["id"]):
        raise HTTPException(404, "Job not found")
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job.public()


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, current_user: Dict[str, Any] = Depends(auth.get_current_user)):
    if not db.is_job_owned_by(job_id, current_user["id"]):
        raise HTTPException(404, "Job not found")
    manager.delete(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/generate")
def generate(job_id: str, body: GenerateIn, current_user: Dict[str, Any] = Depends(auth.get_current_user)):
    if not db.is_job_owned_by(job_id, current_user["id"]):
        raise HTTPException(404, "Job not found")

    quota = db.check_daily_quota(current_user["id"], "generate")
    if not quota["allowed"]:
        raise HTTPException(status_code=429, detail=quota["reason"])

    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.public().get("status") not in ("ready", "done", "error"):
        raise HTTPException(409, "Job is busy")
    if not (job.dir / "signal.npy").exists():
        raise HTTPException(409, "Job has not been analyzed")

    manager.generate(job, body.model_dump())
    db.record_usage(current_user["id"], "generate")

    return job.public()


@app.get("/api/jobs/{job_id}/clips/{name}")
def clip_file(job_id: str, name: str, current_user: Dict[str, Any] = Depends(auth.get_current_user)):
    if not db.is_job_owned_by(job_id, current_user["id"]):
        raise HTTPException(404, "Clip not found")

    job = manager.get(job_id)
    path = job.dir / "clips" / name if job else None
    if not path or not path.is_file() or not name.endswith(".mp4") or "/" in name:
        raise HTTPException(404, "Clip not found")
    return FileResponse(path, media_type="video/mp4", filename=f"{job_id}_{name}")


@app.get("/api/jobs/{job_id}/clips.zip")
def clips_zip(job_id: str, current_user: Dict[str, Any] = Depends(auth.get_current_user)):
    if not db.is_job_owned_by(job_id, current_user["id"]):
        raise HTTPException(404, "Clips not found")

    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    files = sorted((job.dir / "clips").glob("clip_*.mp4"))
    if not files:
        raise HTTPException(404, "No clips yet")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for f in files:
            z.write(f, f.name)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="cliper_{job_id}.zip"'}
    )


@app.post("/api/jobs/{job_id}/clips/{name}/check")
def check_clip(job_id: str, name: str, current_user: Dict[str, Any] = Depends(auth.get_current_user)):
    if not db.is_job_owned_by(job_id, current_user["id"]):
        raise HTTPException(404, "Clip not found")

    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    data = job.public()
    clip = next((c for c in data.get("clips", []) if c.get("file") == name and c.get("status") == "done"), None)
    if not clip:
        raise HTTPException(404, "Clip not found")
    info_path = job.dir / "info.info.json"
    info = __import__("json").loads(info_path.read_text()) if info_path.exists() else None
    opts = data.get("options") or {}
    with tempfile.TemporaryDirectory(dir=DATA_DIR) as tmp:
        report = copyright.assess(
            job.dir / "clips" / name,
            Path(tmp),
            info,
            clip["start"],
            float(opts.get("speed") or 1.0),
            bool(opts.get("mirror"))
        )
    manager._set_clip(job, clip["rank"], check=report)
    return report


@app.post("/api/check")
def check_upload(
    file: UploadFile = File(...),
    source_url: str = Form(""),
    source_start: float = Form(0.0),
    speed: float = Form(1.0),
    current_user: Dict[str, Any] = Depends(auth.get_current_user)
):
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
            except Exception as e:
                raise HTTPException(400, f"Could not read source URL: {e}")
        return copyright.assess(clip, work, info, source_start if info else None, speed or 1.0)
    finally:
        shutil.rmtree(work, ignore_errors=True)


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
