#!/usr/bin/env python3
"""How long does a sound made on the hosted box take to reach the listener?

That is the number that decides whether a screen reader can run on a hosted
machine at all. A blind reader navigates by interrupting speech constantly, so
what matters is not bandwidth but the delay between pressing a key and hearing
the consequence.

Method — two onsets, one recording, no clock synchronisation:

    play a click on THIS machine  ──┐
    ask the remote box to click  ───┤ both land in one microphone recording
                                    └─ the gap between them is the added delay

Because both clicks are found in the same recording, nothing has to agree about
time, and the microphone, speaker and detector cancel out of the result.

Run it twice. Once against a server on this machine, which measures the probe's
own overhead, and once against the hosted box:

    python3 remote_click.py &                         # in another shell
    python3 measure.py --target http://127.0.0.1:8899 --label baseline
    python3 measure.py --target http://HOST:8899      --label rdp

The difference between the two is the remote-desktop audio path. Needs ffmpeg
and a microphone that can hear the speakers — a laptop's built-in pair is fine,
and using real speakers rather than a virtual device is deliberate: it measures
what the person's ear actually receives.
"""
import argparse
import math
import statistics
import subprocess
import sys
import time
import urllib.request
import wave
from array import array
from pathlib import Path

RATE = 16000
WINDOW = 64          # 4 ms at 16 kHz, and a 250 Hz bin — both tones sit on a centre
NEAR_TONE = 1000     # the listener's own click
FAR_TONE = 2000      # the click made on the machine under test
SILENCE_GAP = 0.030  # a tone dipping for less than this is still the same sound
REFRACTORY = 0.5     # one onset per trial: a 40 ms tone is not four events


def record(seconds, device, out):
    """Capture the default input to a mono WAV for the length of the run."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "avfoundation", "-i", f":{device}",
           "-ac", "1", "-ar", str(RATE), "-t", str(seconds), str(out)]
    if sys.platform.startswith("linux"):
        cmd[cmd.index("avfoundation")] = "alsa"
        cmd[cmd.index(f":{device}")] = "default"
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _tone_track(samples, freq):
    """Per-window energy at one frequency (Goertzel).

    Amplitude alone cannot separate the two clicks when they overlap, which is
    exactly what happens when the delay is short — and a short delay is the
    result we most want to be able to trust.
    """
    k = int(0.5 + (WINDOW * freq) / RATE)
    coeff = 2.0 * math.cos(2.0 * math.pi * k / WINDOW)
    out = []
    for i in range(0, len(samples) - WINDOW, WINDOW):
        s1 = s2 = 0.0
        for n in range(WINDOW):
            s0 = samples[i + n] + coeff * s1 - s2
            s2, s1 = s1, s0
        out.append(math.sqrt(abs(s1 * s1 + s2 * s2 - coeff * s1 * s2)))
    return out


def _onsets(track, floor_mult=8.0):
    """Times where this tone starts, relative to the room's own level."""
    if not track:
        return []
    quiet = statistics.median(track)
    threshold = max(quiet * floor_mult, 500)
    hits, armed = [], True
    for i, lv in enumerate(track):
        t = i * WINDOW / RATE
        if lv > threshold and armed:
            hits.append(t)
            armed = False
        elif lv <= threshold and not armed and (t - hits[-1]) > REFRACTORY:
            # Refractory, not just silence: a tone wobbling around the threshold
            # would otherwise be counted several times and pair with itself.
            armed = True
    return hits, threshold, quiet


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        samples = array("h")
        samples.frombytes(w.readframes(w.getnframes()))
    return samples


def click_local(click_wav):
    if sys.platform == "darwin":
        return subprocess.Popen(["afplay", str(click_wav)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return subprocess.Popen(["aplay", "-q", str(click_wav)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="http://host:8899")
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--label", default="run")
    ap.add_argument("--device", default="0", help="avfoundation audio input index")
    ap.add_argument("--spacing", type=float, default=2.0, help="seconds between trials")
    args = ap.parse_args()

    here = Path(__file__).parent
    click_wav = here / "click-near.wav"
    sys.path.insert(0, str(here))
    from remote_click import make_click
    if not click_wav.exists():
        make_click(click_wav, hz=NEAR_TONE)

    total = args.trials * args.spacing + 3
    wav = here / f"capture-{args.label}.wav"
    print(f"[{args.label}] recording {total:.0f}s — keep the room quiet, speakers audible")
    rec = record(total, args.device, wav)
    time.sleep(2.0)  # let ffmpeg open the device before the first trial

    for i in range(args.trials):
        click_local(click_wav)
        try:
            urllib.request.urlopen(args.target + "/click", timeout=5).read()
        except Exception as e:
            print(f"  trial {i+1}: remote unreachable — {e}")
        time.sleep(args.spacing)

    rec.wait()
    if not wav.exists():
        sys.exit("ffmpeg produced no recording — check --device with:\n"
                 "  ffmpeg -f avfoundation -list_devices true -i \"\"")

    samples = read_wav(wav)
    near, near_thr, near_quiet = _onsets(_tone_track(samples, NEAR_TONE))
    far, far_thr, far_quiet = _onsets(_tone_track(samples, FAR_TONE))
    print(f"  heard {len(near)} local clicks, {len(far)} remote clicks")
    if not near or not far:
        print(f"  (levels — local tone floor {near_quiet:.0f}/threshold {near_thr:.0f}, "
              f"remote tone floor {far_quiet:.0f}/threshold {far_thr:.0f})")
        sys.exit("could not hear both tones. Check the speaker volume is up, and that\n"
                 "the machine under test is actually playing through speakers this mic hears.")

    # Each local click is answered by the next remote click within a second.
    deltas = []
    for t in near:
        later = [f for f in far if 0 <= f - t < 1.0]
        if later:
            deltas.append((later[0] - t) * 1000)

    print(f"[{args.label}] {len(deltas)} of {args.trials} trials paired")
    if not deltas:
        sys.exit("no pairs found — is the remote actually audible on this machine's speakers?")
    deltas.sort()
    p = lambda q: deltas[min(len(deltas) - 1, int(len(deltas) * q))]
    print(f"  median {statistics.median(deltas):6.0f} ms")
    print(f"  p90    {p(0.9):6.0f} ms")
    print(f"  spread {deltas[0]:.0f}–{deltas[-1]:.0f} ms")
    print(f"\n  raw: {[round(d) for d in deltas]}")
    print(f"\n  Subtract the baseline run from this one; the remainder is the\n"
          f"  remote-desktop audio path.")


if __name__ == "__main__":
    sys.exit(main())
