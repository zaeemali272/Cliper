"""`cliper` command: start the local server and open the UI in a browser."""
import argparse
import logging
import os
import threading
import webbrowser

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(prog="cliper", description="Most-watched moments -> Shorts/Reels clips")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--no-browser", action="store_true", help="don't open the UI in a browser")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    from .config import BROWSER, COOKIES, FFMPEG, OUTPUT_DIR, PASSWORD  # noqa: PLC0415  (triggers the ffmpeg check early)

    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
    cookies = f"cookies.txt ({COOKIES})" if COOKIES else f"{BROWSER} (used only if YouTube blocks a request)" if BROWSER else "none found"
    print(f"\n  Cliper  ->  {url}\n  clips:   {OUTPUT_DIR}\n  ffmpeg:  {FFMPEG}\n  cookies: {cookies}"
          + (f"\n  password protection: on" if PASSWORD else "") + "\n", flush=True)
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run("cliper.main:app", host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
