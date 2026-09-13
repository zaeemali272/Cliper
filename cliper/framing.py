"""Decide how to fit a landscape clip into 9:16: where the faces are, whether there is a facecam
over gameplay, whether two people should be stacked. Uses OpenCV's YuNet detector (a 230 KB model
fetched once)."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import numpy as np

from .config import DATA_DIR, FFMPEG, MAX_HEIGHT

log = logging.getLogger("cliper.framing")

MODEL_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
MODEL_PATH = DATA_DIR / "models" / "yunet.onnx"
W, H = 480, 270  # detection resolution (frames are squashed to 16:9; coords are fractions anyway)
FPS = 2


def _model() -> Path | None:
    if MODEL_PATH.exists():
        return MODEL_PATH
    try:
        import requests  # noqa: PLC0415

        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        MODEL_PATH.write_bytes(requests.get(MODEL_URL, timeout=60).content)
        return MODEL_PATH
    except Exception as e:  # noqa: BLE001
        log.warning("face model download failed: %s", e)
        return None


def sample_frames(info: dict, start: float, end: float) -> np.ndarray:
    """Low-res frames at FPS from the source, straight from the stream."""
    from .sources import pick_streams  # noqa: PLC0415

    streams = pick_streams(info, min(MAX_HEIGHT, 720))
    url, headers = streams[0]
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if headers:
        cmd += ["-headers", headers]
    cmd += ["-ss", f"{start:.3f}", "-i", url, "-t", f"{end - start:.3f}", "-an",
            "-vf", f"fps={FPS},scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    raw = subprocess.run(cmd, capture_output=True, timeout=300).stdout
    n = len(raw) // (W * H * 3)
    return np.frombuffer(raw[: n * W * H * 3], np.uint8).reshape(n, H, W, 3)


def detect_faces(frames: np.ndarray) -> list[list[tuple[float, float, float, float]]]:
    """Per frame: [(cx, cy, w, h)] as fractions of the frame, largest first."""
    model = _model()
    if model is None or len(frames) == 0:
        return [[] for _ in frames]
    import cv2  # noqa: PLC0415

    try:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
    except AttributeError:
        pass
    det = cv2.FaceDetectorYN.create(str(model), "", (W, H), score_threshold=0.6)
    out = []
    for f in frames:
        _, faces = det.detect(np.ascontiguousarray(f))
        boxes = []
        for x in (faces if faces is not None else []):
            bx, by, bw, bh = float(x[0]), float(x[1]), float(x[2]), float(x[3])
            boxes.append(((bx + bw / 2) / W, (by + bh / 2) / H, bw / W, bh / H))
        boxes.sort(key=lambda b: -b[2])
        out.append(boxes)
    return out


def _track_primary(faces: list[list[tuple[float, float, float, float]]]) -> list:
    """Pick one face per frame, preferring the one nearest to where it was last seen; a new face
    only takes over if it is clearly bigger. Stops the crop jumping to a bystander for a frame."""
    out: list = []
    last = None
    for f in faces:
        if not f:
            out.append(None)
            continue
        pick = f[0]
        if last is not None:
            near = min(f, key=lambda b: abs(b[0] - last[0]) + abs(b[1] - last[1]))
            if abs(near[0] - last[0]) < 0.25 and f[0][2] < near[2] * 1.6:
                pick = near
        out.append(pick)
        last = pick
    return out


def plan(mode: str, faces: list[list[tuple[float, float, float, float]]], layout: str = "auto") -> dict:
    """Return a framing plan:
      {"type": "crop", "track": [(t, cx), ...]}     9:16 crop following the main face (cx = fraction of width)
      {"type": "split", "cx": [a, b]}               two people stacked (podcast)
      {"type": "stack_game", "cam": (x, y, w, h)}    game on top, facecam below
      {"type": "blur"} / {"type": "crop_center"} / {"type": "original"}
    """
    if layout in ("blur", "original"):
        return {"type": layout}
    if layout == "crop" and not faces:
        return {"type": "crop_center"}
    n = len(faces)
    with_face = [f for f in faces if f]
    coverage = len(with_face) / n if n else 0.0
    if mode == "music" and layout == "auto":
        return {"type": "blur"}

    primary = _track_primary(faces)
    sizes = [p[3] for p in primary if p]
    med_h = float(np.median(sizes)) if sizes else 0.0

    # Gameplay with a facecam in a corner: small face, near an edge, present most of the time.
    if mode == "gameplay" and layout == "auto":
        if coverage >= 0.5 and med_h < 0.28:
            cxs = [p[0] for p in primary if p]
            cys = [p[1] for p in primary if p]
            cx, cy = float(np.median(cxs)), float(np.median(cys))
            if cx < 0.35 or cx > 0.65 or cy < 0.35 or cy > 0.65:
                # cam panel on top is 1080x720 (aspect 1.5): a box of that aspect around the face,
                # wide enough to show the webcam frame but not blown up into pixels
                face_w = float(np.median([p[2] for p in primary if p]))
                box_w = min(0.6, max(face_w * 6.0, 0.26))
                box_h = min(1.0, box_w * (16 / 9) / 1.5)
                x = min(max(cx - box_w / 2, 0.0), 1 - box_w)
                y = min(max(cy - box_h * 0.5, 0.0), 1 - box_h)
                return {"type": "stack_game", "cam": (x, y, box_w, box_h)}
        return {"type": "blur"}

    # Two people talking side by side -> stack them.
    if mode in ("podcast", "talking") and layout == "auto" and n:
        pairs = [f for f in faces if len(f) >= 2 and f[1][2] >= 0.5 * f[0][2] and abs(f[0][0] - f[1][0]) > 0.25]
        if len(pairs) / n >= 0.5:
            a = float(np.median([min(f[0][0], f[1][0]) for f in pairs]))
            b = float(np.median([max(f[0][0], f[1][0]) for f in pairs]))
            return {"type": "split", "cx": [a, b]}

    if coverage < 0.3:
        return {"type": "blur"} if (mode == "gameplay" or layout == "auto" and mode == "irl" and coverage == 0) else {"type": "crop_center"}

    # Follow the main face: one x per sampled frame, gaps filled, smoothed, with a dead-band so the
    # crop doesn't jitter on every tiny head move.
    xs = np.array([p[0] if p else np.nan for p in primary], dtype=np.float32)
    idx = np.arange(len(xs))
    good = ~np.isnan(xs)
    xs = np.interp(idx, idx[good], xs[good])
    k = min(5, len(xs))
    xs = np.convolve(np.pad(xs, (k // 2, k - 1 - k // 2), mode="edge"), np.ones(k) / k, mode="valid")
    track: list[tuple[float, float]] = []
    last = None
    for i, x in enumerate(xs):
        x = float(min(max(x, 0.0), 1.0))
        if last is None or abs(x - last) > 0.06 or i == len(xs) - 1:
            track.append((i / FPS, x))
            last = x
    return {"type": "crop", "track": track}
