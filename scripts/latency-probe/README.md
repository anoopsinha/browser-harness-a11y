# Latency probe

Answers one question before any hosting work is done: **if a screen reader runs
on a hosted machine, does its speech arrive fast enough to be usable?**

A blind reader navigates by interrupting speech constantly — arrow, arrow, cut
it off, move on. So the number that matters is not bandwidth but the delay
between causing a sound and hearing it. If that delay is small, hosting the
browser is viable and no semantic mirror is needed. If it is large, it is not,
whatever else is true.

## What is already known

Network round trip from this machine, measured with TCP connect times:

| region | RTT |
| --- | --- |
| us-west-1 | 8 ms |
| us-west-2 | 30 ms |
| us-east-1 | 70 ms |
| ap-northeast-1 | 118 ms |
| eu-west-1 | 155 ms |

At 8 ms to us-west-1 the network is not the constraint. What remains unmeasured
is the remote-desktop audio stack — encode, buffer, decode — and that is what
this probe measures.

## How it measures

Two clicks land in one microphone recording: one played by the listener's own
machine at 1000 Hz, one played by the machine under test at 2000 Hz. The gap
between them is the added delay.

Nothing has to agree about time, because both sounds are found in the same
recording — and the microphone, the speakers and the detector all cancel out of
the difference. Two tones rather than two of the same, because when the delay is
short the sounds overlap, and amplitude alone cannot then say where one ended
and the other began. Each tone is picked out with a Goertzel filter on a 250 Hz
bin it sits at the centre of.

Measuring through real speakers and a real microphone is deliberate. It includes
everything the person's ear actually receives, rather than the parts that are
convenient to instrument.

## Running it

On the machine under test:

```bash
python3 remote_click.py            # listens on 8899, stdlib only
curl localhost:8899/click          # confirm it is audible there
```

On the listener's machine, with the speakers up and the room quiet:

```bash
python3 measure.py --target http://HOST:8899 --label rdp
```

Run it against `http://127.0.0.1:8899` first as a control. That reads 0 ms here,
so the probe adds nothing measurable at its 4 ms resolution and the remote
figure can be read directly.

Needs `ffmpeg` and microphone permission for the terminal. `--device` selects
the input; list them with
`ffmpeg -f avfoundation -list_devices true -i ""`.

## Reading the result

These are starting thresholds to confirm with a real reader, not settled
figures — the honest version of this table is the one written after somebody
has actually used it.

| median | reading |
| --- | --- |
| under 100 ms | likely fine; go on to the subjective test |
| 100–250 ms | noticeable; usable for slow navigation, poor for rapid skimming |
| over 250 ms | interruption feels broken; hosting the browser fails for screen-reader users |

Watch p90 as much as the median. Steady lag can be adapted to; lag that varies
cannot, because the reader cannot learn when the speech will stop.

## The half this cannot measure

The number is necessary and not sufficient. The decision needs a person:

1. A blind participant connects, with the remote screen reader at **their own**
   usual speech rate — not a default.
2. They arrow through a list of about thirty items at the speed they normally
   read.
3. They interrupt mid-word repeatedly, as they would when skimming.
4. They fill in a form field and hear the result.

The question to ask afterwards is not "did it work" but "would you use this for
an hour". Two other things fall out of that session and are worth recording:
whether a braille display was needed — remote desktop carries none — and
whether the remote screen reader and browser pairing is one they actually use.
