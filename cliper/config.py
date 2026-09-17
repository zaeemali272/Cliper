import os
import shutil
import subprocess
import sys
from pathlib import Path

from platformdirs import user_data_dir, user_downloads_dir

# Some Python builds (uv-managed on NixOS, etc.) ship without a CA bundle; point everything at certifi.
try:
    import certifi  # noqa: F401

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"

# Clips and job state. Defaults to the OS's per-user data dir (e.g. ~/.local/share/cliper,
# %LOCALAPPDATA%\cliper, ~/Library/Application Support/cliper).
DATA_DIR = Path(os.environ.get("CLIPER_DATA") or user_data_dir("cliper", appauthor=False))
JOBS_DIR = DATA_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

# yt-dlp is a Python dependency, so run it through the same interpreter - no PATH lookups.
YTDLP: list[str] = [sys.executable, "-m", "yt_dlp"]

# Finished clips are copied here, one folder per video: ~/Downloads/Cliper/<title>/clip_01.mp4
OUTPUT_DIR = Path(os.environ.get("CLIPER_OUTPUT") or Path(user_downloads_dir()) / "Cliper")

_cookies_env = os.environ.get("CLIPER_COOKIES")
if _cookies_env:
    COOKIES = _cookies_env
elif (DATA_DIR / "cookies.txt").exists():
    COOKIES = str(DATA_DIR / "cookies.txt")
else:
    COOKIES = None


def _detect_browser() -> str | None:
    """A browser whose cookies yt-dlp can read (--cookies-from-browser), used only if YouTube
    refuses a plain request. CLIPER_BROWSER=firefox|chrome|chromium|brave|edge|none overrides."""
    pref = os.environ.get("CLIPER_BROWSER")
    if pref:
        return None if pref.lower() == "none" else pref.lower()
    home = Path.home()
    if sys.platform == "win32":
        appdata, local = Path(os.environ.get("APPDATA", "")), Path(os.environ.get("LOCALAPPDATA", ""))
        cands = [("firefox", appdata / "Mozilla/Firefox"), ("chrome", local / "Google/Chrome"),
                 ("edge", local / "Microsoft/Edge"), ("brave", local / "BraveSoftware/Brave-Browser"),
                 ("chromium", local / "Chromium")]
    elif sys.platform == "darwin":
        lib = home / "Library/Application Support"
        cands = [("firefox", lib / "Firefox"), ("chrome", lib / "Google/Chrome"), ("brave", lib / "BraveSoftware/Brave-Browser"),
                 ("edge", lib / "Microsoft Edge"), ("chromium", lib / "Chromium")]
    else:
        cfg = home / ".config"
        cands = [("firefox", home / ".mozilla/firefox"), ("firefox", home / "snap/firefox/common/.mozilla/firefox"),
                 ("chrome", cfg / "google-chrome"), ("brave", cfg / "BraveSoftware/Brave-Browser"),
                 ("chromium", cfg / "chromium"), ("edge", cfg / "microsoft-edge")]
    for name, path in cands:
        if path.exists():
            return name
    return None


BROWSER = _detect_browser()
# Optional HTTP basic-auth password when exposing the server publicly (username is anything).
PASSWORD = os.environ.get("CLIPER_PASSWORD") or None

# Max source resolution to pull. A 9:16 crop of a 1080p frame is only 608 px wide, so 4K sources
# are worth it for a sharp 1080x1920 output.
MAX_HEIGHT = int(os.environ.get("CLIPER_MAX_HEIGHT", "2160"))
# Parallel ffmpeg renders per job.
RENDER_WORKERS = int(os.environ.get("CLIPER_RENDER_WORKERS", "2"))
# Parallel jobs overall.
JOB_WORKERS = int(os.environ.get("CLIPER_JOB_WORKERS", "2"))


def _find_ffmpeg() -> str:
    """Env var, then PATH, then a static build downloaded once into the package dir."""
    if os.environ.get("FFMPEG_BIN"):
        return os.environ["FFMPEG_BIN"]
    found = shutil.which("ffmpeg")
    if found:
        return found
    from static_ffmpeg import run  # noqa: PLC0415

    print("ffmpeg not found on PATH - downloading a static build (one time)...", flush=True)
    ffmpeg, _ffprobe = run.get_or_fetch_platform_executables_else_raise()
    return ffmpeg


FFMPEG = _find_ffmpeg()


def _ffmpeg_supports(what: str, flag: str) -> bool:
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", flag], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split()[1:2] == [what] for line in out.splitlines() if line.strip())


# Distro ffmpeg builds occasionally lack libass; captions are silently skipped then.
CAPTIONS_SUPPORTED = _ffmpeg_supports("ass", "-filters")
if not CAPTIONS_SUPPORTED:
    print("warning: this ffmpeg has no libass - captions will be disabled. "
          "Set FFMPEG_BIN to a full build or uninstall ffmpeg to use the bundled one.", flush=True)
