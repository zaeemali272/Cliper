"""Tiny on-disk job system: one folder per job, state in job.json, work on a thread pool."""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from . import analysis, captions, clipper, framing, modes, seo, sources
from .config import CAPTIONS_SUPPORTED, JOB_WORKERS, JOBS_DIR, MAX_HEIGHT, OUTPUT_DIR, RENDER_WORKERS

log = logging.getLogger("cliper.jobs")


class Job:
    def __init__(self, job_id: str, data: dict | None = None):
        self.id = job_id
        self.dir = JOBS_DIR / job_id
        self.lock = threading.Lock()
        self.data = data or {}

    @property
    def path(self) -> Path:
        return self.dir / "job.json"

    def update(self, **kv) -> None:
        with self.lock:
            msg = kv.get("message") or kv.get("error")
            if msg and msg != self.data.get("message"):
                entries = self.data.setdefault("log", [])
                # "Doing X (12/40)" updates the previous "Doing X (8/40)" line instead of stacking up
                if entries and "(" in msg and entries[-1]["msg"].split(" (")[0] == msg.split(" (")[0]:
                    entries[-1] = {"t": time.time(), "msg": msg}
                else:
                    entries.append({"t": time.time(), "msg": msg})
                    log.info("[%s] %s", self.id, msg)
                self.data["log"] = entries[-40:]
            self.data.update(kv)
            self.data["updated"] = time.time()
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data))
            tmp.replace(self.path)

    def public(self) -> dict:
        with self.lock:
            data = dict(self.data)
            clips = data.get("clips")
            if clips:
                info = {}
                info_path = self.dir / "info.info.json"
                if info_path.exists():
                    try:
                        info = json.loads(info_path.read_text())
                    except Exception:
                        pass
                if not info:
                    info = {"title": data.get("title"), "uploader": data.get("uploader")}
                updated_clips = []
                for c in clips:
                    if "seo" not in c:
                        c = dict(c)
                        c["seo"] = seo.generate_clip_seo(c, info)
                    updated_clips.append(c)
                data["clips"] = updated_clips
            return data


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.pool = ThreadPoolExecutor(max_workers=JOB_WORKERS)
        self._load()

    def _load(self) -> None:
        for p in JOBS_DIR.glob("*/job.json"):
            try:
                data = json.loads(p.read_text())
                job = Job(p.parent.name, data)
                # anything that was mid-flight when the server died is now failed
                if data.get("status") in ("analyzing", "generating"):
                    job.update(status="error", error="Server restarted during processing")
                self.jobs[job.id] = job
            except Exception:
                continue
        self._dedupe()

    def _dedupe(self) -> None:
        """Same video analysed twice (older builds did that): keep only the newest job per video."""
        by_video: dict[str, list[Job]] = {}
        for j in self.jobs.values():
            key = j.data.get("video_id") or j.data.get("url")
            if key:
                by_video.setdefault(key, []).append(j)
        for jobs in by_video.values():
            jobs.sort(key=lambda j: -(j.data.get("created") or 0))
            for j in jobs[1:]:
                log.info("removing duplicate job %s for %s", j.id, j.data.get("title"))
                self.delete(j.id)

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def delete(self, job_id: str) -> None:
        job = self.jobs.pop(job_id, None)
        if job:
            shutil.rmtree(job.dir, ignore_errors=True)

    def list(self, allowed_ids: set[str] | None = None) -> list[dict]:
        rows = []
        for j in self.jobs.values():
            if allowed_ids is not None and j.id not in allowed_ids:
                continue
            d = j.public()
            rows.append({k: d.get(k) for k in ("id", "url", "title", "status", "platform", "duration",
                                                "thumbnail", "created", "clip_count")})
        return sorted(rows, key=lambda r: -(r.get("created") or 0))

    # ---------------------------------------------------------------- analyze

    def find_by_url(self, url: str) -> Job | None:
        url = sources.normalize_url(url)
        vid = sources.video_key(url)
        best = None
        for j in self.jobs.values():
            d = j.data
            if d.get("status") not in ("ready", "done"):
                continue
            if d.get("url") == url or (vid and d.get("video_id") and d["video_id"] in (vid, "v" + vid)):
                if best is None or (d.get("created") or 0) > (best.data.get("created") or 0):
                    best = j
        return best

    def analyze(self, url: str, force: bool = False) -> Job:
        url = sources.normalize_url(url)
        platform = sources.detect_platform(url)
        if not force:
            existing = self.find_by_url(url)
            if existing:
                return existing
        job = Job(uuid.uuid4().hex[:12])
        job.dir.mkdir(parents=True, exist_ok=True)
        job.update(id=job.id, url=url, platform=platform, status="analyzing", progress=0.0,
                   message="Fetching video info", created=time.time())
        self.jobs[job.id] = job
        self.pool.submit(self._run_analyze, job)
        return job

    def _run_analyze(self, job: Job) -> None:
        try:
            d = job.public()
            info = sources.fetch_info(d["url"], job.dir, want_subs=d["platform"] == "youtube")
            job.update(title=info.get("title"), duration=int(info.get("duration") or 0),
                       thumbnail=info.get("thumbnail"), uploader=info.get("uploader") or info.get("channel"),
                       video_id=info.get("id"), progress=0.3, message="Finding the most-watched parts")

            log.info("[%s] %s (%ss, %s)", job.id, info.get("title"), info.get("duration"), d["platform"])

            def prog(msg, frac):
                job.update(message=msg, progress=0.3 + 0.6 * frac)

            sig, source = sources.build_signal(d["url"], info, job.dir, prog)
            np.save(job.dir / "signal.npy", sig)
            mode, why = modes.detect_mode(info)
            job.update(mode=mode, mode_reason=why)

            sub = sources.find_subtitle_file(job.dir)
            words = captions.words_from_json3(sub) if sub else []
            job.update(status="ready", progress=1.0, message="Ready", signal_source=source,
                       signal=analysis.downsample(sig, 240), highlights=analysis.highlights(sig, 12),
                       has_captions=bool(words), whisper_available=captions.whisper_available(),
                       captions_supported=CAPTIONS_SUPPORTED)
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] analysis failed", job.id)
            job.update(status="error", error=_err(e), message="Failed: " + _err(e))

    # ---------------------------------------------------------------- generate

    def generate(self, job: Job, opts: dict) -> None:
        count = int(max(1, min(60, opts.get("count", 10))))
        min_len = int(max(3, min(180, opts.get("min_len", 8))))
        max_len = int(max(min_len, min(180, opts.get("max_len", 15))))
        layout = opts.get("layout") if opts.get("layout") in ("auto", "crop", "blur", "original") else "auto"
        mode_auto = opts.get("mode") not in modes.MODES
        mode = (job.public().get("mode") or "talking") if mode_auto else opts["mode"]
        style = modes.style_defaults(mode)
        for k in ("punch", "progress", "title", "grade", "mirror"):
            if opts.get(k) is not None:
                style[k] = bool(opts[k])
        for k in ("speed", "pitch"):
            if opts.get(k) is not None:
                style[k] = float(max(0.8, min(1.3, float(opts[k]))))
        captions_mode = opts.get("captions", "auto")  # auto | whisper | off
        style["max_height"] = int(opts.get("max_height") or MAX_HEIGHT)
        job.update(status="generating", progress=0.0, message="Choosing clips",
                   options={"count": count, "min_len": min_len, "max_len": max_len, "layout": layout,
                            "mode": None if mode_auto else mode, **style, "captions": captions_mode}, clips=[])
        self.pool.submit(self._run_generate, job, count, min_len, max_len, layout, mode, mode_auto, style, captions_mode)

    def _run_generate(self, job: Job, count: int, min_len: int, max_len: int, layout: str, mode: str,
                      mode_auto: bool, style: dict, cmode: str) -> None:
        try:
            info = json.loads((job.dir / "info.info.json").read_text())
            sig = np.load(job.dir / "signal.npy")
            out_dir = job.dir / "clips"
            shutil.rmtree(out_dir, ignore_errors=True)
            out_dir.mkdir()

            sub = sources.find_subtitle_file(job.dir)
            words = captions.words_from_json3(sub) if (sub and cmode != "off") else []
            if not words and cmode != "off" and captions.whisper_available():
                words = self._transcribe_hot_regions(job, info, sig, count, min_len, max_len, out_dir)
            if not CAPTIONS_SUPPORTED or cmode == "off":
                style["captions"] = False

            job.update(message="Choosing clips from transcript" if words else "Choosing clips")
            plan = analysis.pick_clips_transcript(sig, words, count, min_len, max_len) if words \
                else [{**c, "text": "", "hook": "", "phrases": []} for c in analysis.pick_clips(sig, count, min_len, max_len)]
            clips = [{**c, "status": "queued", "file": f"clip_{c['rank']:02d}.mp4", "framing": None, "seo": seo.generate_clip_seo(c, info)} for c in plan]
            export_dir = OUTPUT_DIR / _safe_name(info.get("title") or job.id)
            export_dir.mkdir(parents=True, exist_ok=True)
            for old in export_dir.glob("clip_*.mp4"):
                old.unlink()
            job.update(clips=clips, clip_count=len(clips), message=f"Rendering {len(clips)} clips",
                       output_dir=str(export_dir))

            def one(c: dict) -> None:
                self._set_clip(job, c["rank"], status="rendering")
                log.info("[%s] clip %s (%.0f-%.0fs): framing", job.id, c["rank"], c["start"], c["end"])
                cmode_ = modes.mode_at(info, c["start"], mode) if mode_auto else mode
                fplan = {"type": layout} if layout in ("blur", "original") else None
                if fplan is None:
                    try:
                        faces = framing.detect_faces(framing.sample_frames(info, c["start"], c["end"]))
                        fplan = framing.plan(cmode_, faces, layout)
                    except Exception as e:  # noqa: BLE001
                        log.warning("[%s] framing failed, centre crop: %s", job.id, e)
                        fplan = {"type": "crop_center"}
                self._set_clip(job, c["rank"], framing=fplan["type"], mode=cmode_)
                speed = float(style.get("speed", 1.0) or 1.0)
                ass = None
                if style.get("captions") or style.get("title") or style.get("progress"):
                    ass = out_dir / f"{c['rank']}.ass"
                    ass.write_text(captions.build_ass(
                        words, c["start"], c["end"], fplan["type"] != "original", speed=speed,
                        title=c.get("hook") if style.get("title") else None,
                        progress=bool(style.get("progress")), captions=bool(style.get("captions")) and bool(words)),
                        encoding="utf-8")
                punch = [((a - c["start"]) / speed, (b - c["start"]) / speed)
                         for i, (a, b) in enumerate(c.get("phrases") or []) if i % 2 == 1]
                out = out_dir / c["file"]
                try:
                    clipper.render_clip(info, c["start"], c["end"], out, fplan, style, ass, punch)
                except Exception as e:  # noqa: BLE001
                    log.warning("[%s] clip %s direct render failed, retrying via yt-dlp: %s", job.id, c["rank"], _err(e))
                    self._set_clip(job, c["rank"], note="retrying via yt-dlp")
                    clipper.render_clip_via_ytdlp(job.dir / "info.info.json", c["start"], c["end"], out,
                                                  fplan, style, ass, punch, out_dir)
                if ass:
                    ass.unlink(missing_ok=True)
                try:
                    shutil.copy2(out, export_dir / out.name)
                except OSError as e:
                    log.warning("[%s] could not copy clip to %s: %s", job.id, export_dir, e)
                self._set_clip(job, c["rank"], status="done", size=out.stat().st_size)

            done = 0
            with ThreadPoolExecutor(max_workers=RENDER_WORKERS) as pool:
                futs = {pool.submit(one, c): c for c in clips}
                for f in as_completed(futs):
                    c = futs[f]
                    try:
                        f.result()
                    except Exception as e:  # noqa: BLE001
                        log.warning("[%s] clip %s failed: %s", job.id, c["rank"], _err(e))
                        self._set_clip(job, c["rank"], status="error", error=_err(e))
                    done += 1
                    job.update(progress=done / len(clips), message=f"Rendered {done}/{len(clips)}")
            job.update(status="done", progress=1.0, message="All clips rendered")
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] generation failed", job.id)
            job.update(status="error", error=_err(e), message="Failed: " + _err(e))

    def _transcribe_hot_regions(self, job: Job, info: dict, sig: np.ndarray, count: int, min_len: int,
                                max_len: int, out_dir: Path) -> list:
        """No platform captions: run local Whisper only on the hottest stretches, not the whole video."""
        win = max(30, max_len * 2)
        regions = analysis.pick_clips(sig, min(count * 2, 40), win, win)
        words: list = []
        for i, r in enumerate(regions, 1):
            job.update(message=f"Transcribing hot regions with Whisper ({i}/{len(regions)})",
                       progress=0.3 * i / len(regions))
            wav = out_dir / f"region_{i}.wav"
            try:
                clipper.extract_audio(info, r["start"], r["end"], wav)
                words += [(s + r["start"], e + r["start"], t) for s, e, t in captions.transcribe_with_whisper(wav)]
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] whisper region %s failed: %s", job.id, i, e)
            finally:
                wav.unlink(missing_ok=True)
        words.sort()
        return words

    def _set_clip(self, job: Job, rank: int, **kv) -> None:
        with job.lock:
            for c in job.data.get("clips", []):
                if c["rank"] == rank:
                    c.update(kv)
        job.update()


def _safe_name(title: str, limit: int = 60) -> str:
    keep = "".join(ch if ch.isalnum() or ch in " -_()[]" else " " for ch in title)
    return " ".join(keep.split())[:limit].strip() or "video"


def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:600]
