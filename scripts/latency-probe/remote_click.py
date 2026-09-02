#!/usr/bin/env python3
"""Plays a click, on request, on the machine under test. Standard library only.

Runs on the hosted box. GET /click plays a short tone through that machine's
speakers, which the remote-desktop session then has to carry back to whoever is
listening — the path this whole probe exists to measure.

    python3 remote_click.py [--port 8899]

Windows uses winsound, which is a direct call and adds almost nothing. macOS and
Linux shell out to a player, whose process spawn (tens of ms) lands inside the
measurement — the baseline run in measure.py is what cancels it.
"""
import argparse
import platform
import subprocess
import sys
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from math import pi, sin
from pathlib import Path
from struct import pack

CLICK = Path(__file__).with_name("click.wav")
SYSTEM = platform.system()


def make_click(path, ms=40, hz=2000, rate=16000):
    """A short sine burst with a hard attack — easy to find, hard to mistake.

    2000 Hz here and 1000 Hz for the listener's own click. Two tones rather than
    two of the same, because when the delay is small the sounds overlap, and a
    single amplitude trace cannot then say where one ended and the other began.
    Both sit on a bin centre for the window size the detector uses.
    """
    frames = bytearray()
    for i in range(int(rate * ms / 1000)):
        # No fade in: the onset must be abrupt or we measure the envelope.
        frames += pack("<h", int(28000 * sin(2 * pi * hz * i / rate)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def play():
    if SYSTEM == "Windows":
        import winsound
        winsound.Beep(2000, 40)
        return
    cmd = ["afplay", str(CLICK)] if SYSTEM == "Darwin" else ["aplay", "-q", str(CLICK)]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/click"):
            play()
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass  # the probe is timing this; logging to stderr is not free


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()
    if SYSTEM != "Windows" and not CLICK.exists():
        make_click(CLICK)
    print(f"[remote_click] {SYSTEM}, listening on 0.0.0.0:{args.port}", flush=True)
    print("[remote_click] check you can hear it:  curl localhost:%d/click" % args.port, flush=True)
    HTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
