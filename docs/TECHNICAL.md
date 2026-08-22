# Rally Co-Driver for Forza Horizon 6, technical notes

An external rally pace-note co-driver. It reads the game's official UDP
telemetry stream, matches the car's position against a previously recorded
stage line, and speaks pace notes ahead of corners, like Richard Burns Rally
or DiRT.

No game files are touched, no memory is read, nothing is injected. Everything
runs off the official "Data Out" feature. See docs/TECHNICAL.md for why that is a
hard constraint rather than a preference.

**Status: complete.** Capture/replay, stage building, the live
co-driver loop, and voice packs. Generate a voice once with TTS, and the
runtime concatenates the pre-rendered clips, no synthesis ever happens in
the hot path.

The packet layout is **verified against a real FH6 capture**: position, speed
and the local velocity vector all agree with each other and with an implied
tyre radius of 0.322 m. See "Known findings" below for the one field that
does not behave as documented.

---

## Setup

```
pip install -e .[dev]
```

In game: `SETTINGS → HUD AND GAMEPLAY`

| Setting | Value |
|---|---|
| Data Out | On |
| Data Out IP Address | `127.0.0.1` |
| Data Out IP Port | `5400` |

**Not 5300.** The official Forza docs say to avoid ports 5200-5300, because
the game binds its own outgoing socket somewhere in that range. Nearly every
tutorial, SimHub guide and GitHub README online tells you to use 5300 anyway.
Change the port in `config/defaults.yaml` if 5400 clashes with something.

The game supports exactly **one** Data Out target. If you also run SimHub or a
motion rig, you need a UDP splitter in front of both. This project does not
provide one.

---

## Try it without the game

```
python -m codriver synth -o recordings/synthetic.fzr
python -m codriver info   recordings/synthetic.fzr
python -m codriver verify recordings/synthetic.fzr
python -m codriver replay recordings/synthetic.fzr
```

`synth` generates a capture with correct packet structure, plausible physics,
a stationary start, a mid-stage pause and a jump. It exercises the whole
toolchain, and it proves nothing about what FH6 actually puts on the wire, only a real recording can do that.

## Record a real drive

```
python -m codriver capture --name my-stage --fixture tests/fixtures/packet_real.bin
```

Drive. `Ctrl-C` to stop. Then, before trusting anything downstream:

```
python -m codriver verify recordings/my-stage.fzr
```

This runs the empirical checks from the development rules against your recording. The
one that matters is `speed_vs_position`: it compares how far the car actually
moved against how fast it said it was going. Those two numbers come from
separately decoded regions of the packet and only agree if both are read from
the right bytes, which is what catches a misplaced FH6 12-byte insert, the
failure mode that otherwise shows up as coordinates that look like plausible
floats.

Then develop against the recording instead of the game:

```
python -m codriver replay recordings/my-stage.fzr --loop
```

## Build a stage

```
python -m codriver build recordings/my-stage.fzr --name my-stage
python -m codriver gpx   stages/my-stage.json
```

`build` picks the longest continuous run of driving out of the recording,
resamples it to even 3 m spacing, classifies every point by corner severity,
runs the five-step reduction from the note algorithm, and writes `stages/my-stage.json`. It
prints the notes as it goes, reading those against a replay of the recon lap
is the fastest way to tell whether your thresholds are right.

```
slalom, 0.72 km, 8 notes, 241 points at 3.0 m

   0.021 km  +  21.0 m   2 left
   0.081 km  +  59.9 m   2 right
   0.159 km  +  77.9 m   70 2 left
   0.392 km  +  77.9 m   70 2 right and jump and 2 left
```

`gpx` writes a file you can drop on
[gpsvisualizer.com/map_input](https://www.gpsvisualizer.com/map_input), one
track per corner class so they colour separately, and every note as a labelled
waypoint. That is how you see at a glance whether the bands are calling half
the stage a 4.

The build also checks something the geometry cannot check for itself. The note algorithm's
circle fit says "Right if `divisor > 0`", which is only true for one handedness
of the coordinate frame, and nothing guarantees FH6 uses the one the formula
was written for. So the classified direction is compared against the steering
the recon lap actually recorded, and the report says how well they agree. Below
30% it flips them and says so; between 30% and 70% it refuses to guess and
warns.

Stage files are hand-editable by design. Notes are positioned by distance
along the stage and carry their token list, so fixing three corners the
generator got wrong means editing three lines, not regenerating.

## Drive with the co-driver

```
python -m codriver run stages/my-stage.json
```

Works identically against the game and against `codriver replay`, it cannot
tell the difference. With a voice pack in `voices/` the notes are spoken;
without one they are placeholder beeps with word-like durations (severity 1-6
ride a pitch scale, high = tight), so timing is tunable either way. The HUD
shows the tracking state, distance along stage, and each call as it fires
with the lead distance in force:

```
>> 150 left tightens 3   [ 0.360 km, lead 137 m, 1.39s]
>> 250 jump into crest   [ 1.917 km, lead 130 m, 1.31s]
```

The rules it lives by: a note must *finish* `reaction_buffer_s` before
its corner; two phrases never overlap; a note that can no longer finish in
time is dropped, not played late; when two notes contend for the mouth the
less severe loses, including a mild note whose phrase would make an
upcoming hairpin call late. Localisation searches only a window around the
last confirmed position (a global search would snap to the wrong arm of the
switchback staircase), stream gaps suspend the queue, and a position jump
after a gap is treated as a rewind: flush everything, re-localise, rebuild.

Edit `config/local.yaml` while driving, lead times, search windows and beep
lengths all take effect within half a second.

## Learn from every drive

`run` records the telemetry it hears into `recordings/runs/<stage>_<time>.fzr`
(the game sends it anyway; `--no-record` turns it off). Fold those drives back
into the stage:

```
python -m codriver learn stages/my-stage.json
```

Two things happen. The **line becomes the median of where the car actually
went**, recon plus every run, point by point, localised with the same
constrained search the runtime uses, so a cut across a field is rejected the
way it would be live. Recon wobble averages out. And **every corner remembers
the slowest speed driven through it**, shown next to the note:

```
0.495 km  +135.0 m   100 3 left            ~ 78 km/h
1.365 km  + 96.0 m   70 4 left long        ~115 km/h
```

That is the information geometry cannot produce: grip, car, camber and nerve
folded into one number. A "3" stays a "3", but you can see that *you* take
your 3s at ~80 and your 4s at ~110 in this car, which is what the severity
scale is supposed to mean. The previous stage is kept as `.json.bak`.

A corner that runs longer than `stage.notes.long_min_m` (120 m) is called
"long". This is also what a long corner that flickers between two classes
becomes, one note, not two "4 right" in quick succession.

## Voice packs

```
python -m codriver voice generate                  # MS neural TTS (network while generating)
python -m codriver voice generate --engine sapi    # offline Windows voices
python -m codriver voice check stages/my-stage.json
python -m codriver voice say 100 left tightens 1
```

`generate` speaks the whole the audio design vocabulary (~41 words) through TTS once,
then applies the the audio design clip rules, silence trimmed hard at both ends, RMS
normalised across the bank, resampled to 48 kHz mono 16-bit, and writes
`voices/default/` with a manifest. Default voice is `en-GB-RyanNeural` at
+15% rate; co-drivers talk briskly. Runtime never touches TTS: phrases are
concatenated from the pre-rendered clips with a crossfade, and the
scheduler's lead-time maths automatically uses the real word lengths.

`check` validates a pack (every manifest entry exists and decodes) and, given
stage files, answers the question that matters: can this pack speak *this*
stage? Missing tokens are listed by name. At runtime a missing token warns
loudly and falls back to a beep mid-phrase, audible, correctly timed, and
impossible to miss.

Packs are swappable directories: record your own clips, drop them in
`voices/mine/` with a manifest, set `audio.voice_pack: mine`.

## Commands

| | |
|---|---|
| `fields` | print the packet layout this build uses |
| `listen` | decode and print live telemetry; writes nothing |
| `capture` | record raw datagrams to a `.fzr` file |
| `synth` | generate a synthetic capture |
| `info` | summarise a capture: rate, packet sizes, stream gaps |
| `verify` | run the the development rules layout checks against a capture |
| `decode` | export a capture to NDJSON, one row per frame |
| `replay` | pump a capture back out over UDP at original timing |
| `run` | the co-driver: localise on a stage and speak its notes |
| `voice generate` | build a voice pack with TTS (edge or offline sapi) |
| `voice check` | validate a pack; report missing tokens per stage |
| `voice say` | audition a phrase through the real playback path |
| `scan` | find which UDP port the game is sending to |
| `build` | turn a recon capture into a stage JSON |
| `notes` | print a stage's pace notes (`--tokens` lists what a voice pack needs) |
| `gpx` | export a stage for gpsvisualizer.com |
| `learn` | fold recorded runs into a stage: averaged line, observed speeds |
| `config` | print the merged configuration |

`--speed`, `--loop` and `--max-gap` on `replay` are the tuning-loop
ergonomics: play a stage back at 4×, on repeat, with the two-minute menu pause
clamped to a second.

---

## Configuration

Everything tunable lives in `config/defaults.yaml`, which is **hot-reloaded**:
edit and save while the co-driver is running and the change takes effect
within about half a second. That is deliberate and load-bearing, the values
that decide whether this *feels* like a co-driver (where the 1-6 severity
boundaries sit, how much smoothing, "into" vs "and", how lead time scales with
speed) are not code problems and cannot be solved by writing more code. They
are found by driving, listening and adjusting, dozens of times.

Don't edit `defaults.yaml` to tune. Create `config/local.yaml` and override
only the keys you are changing, it is deep-merged on top, gitignored, and a
key that doesn't exist in `defaults.yaml` is reported as a probable typo
rather than silently ignored.

The file carries the full the note algorithm/the runtime design/the audio design key set. Every section is read by the part of the pipeline it names.

---

## Layout

```
config/defaults.yaml        every threshold, hot-reloaded
src/codriver/
  config.py                 dotted access, mtime reload, typo warnings
  audio.py                  resample / crossfade / clip assembly, shared
  adapters/base.py          TelemetryFrame, game-agnostic, fixed units
  adapters/fh6.py           the ONLY module that knows the 324-byte packet
  net/udp.py                sockets; knows nothing about packet contents
  record/capture.py         .fzr raw datagram log
  record/replay.py          deadline-scheduled UDP playback
  record/synth.py           synthetic captures
  record/verify.py          the the development rules empirical checks
  stage/line.py             capture -> driving segments -> raw line
  stage/resample.py         step 0 of the note algorithm: subdivide, smooth, space evenly
  stage/curvature.py        step 1 of the note algorithm: circle fit -> severity class
  stage/notes.py            the note algorithm steps 2-6, plus telemetry hazards
  stage/build.py            the pipeline, driven entirely by config
  stage/learn.py            many runs in, one better stage out
  stage/schema.py           stage file format, hand-editable
  stage/gpx.py              debug export for gpsvisualizer
  runtime/locate.py         constrained localisation on the stage line
  runtime/scheduler.py      trigger timing and queue discipline
  runtime/player.py         beep bank + low-latency output stream
  runtime/run.py            the live loop, hot-reloading config
  voice/vocab.py            token -> spoken text, the the audio design vocabulary
  voice/pack.py             manifest, loader, WavBank
  voice/generate.py         TTS generation + the audio design clip post-processing
  cli/                      one module per command group
voices/
  default/                  generated voice pack (manifest + wav per token)
```

The FH6 dependency is confined to `adapters/fh6.py`. A second adapter
(Assetto Corsa Rally, DiRT Rally 2.0, EA WRC) should need no changes anywhere
else.

### The capture format

`.fzr` stores **raw datagrams plus arrival timestamps**, not parsed rows.
The original design specified NDJSON for the raw recon file; this deviates
deliberately, because the architecture notes also says the offset table must be verified
empirically and may be wrong. If your first recording of a stage is stored
already-parsed and an offset later turns out to be off by 12 bytes, every
recording made before the fix is landfill. A byte log re-parses forever.
NDJSON is still there, as an export (`decode`) derived on demand.

Records are length-prefixed and append-only, so a capture killed mid-drive
still reads back cleanly up to the last complete record.

### Replay timing

Pacing comes from arrival time, not `TimestampMS`. The game emits nothing at
all during pauses, rewinds and after the finish line, so there is no packet
from which to read a timestamp across exactly the gaps that matter, and the
field can overflow to zero mid-stage. The payload is forwarded byte-for-byte
with `TimestampMS` untouched.

On Windows the default timer period is ~15.6 ms, coarser than a 60 Hz frame,
so replay requests a 1 ms period and schedules against absolute deadlines with
a short spin at the end. `replay` reports its achieved scheduling error, if
playback is not faithful you should be able to see it rather than assume it.
Typical: mean 0.1 ms, max under 1 ms.

---

## Tests

```
python -m pytest
```

`tests/test_fh6_layout.py` re-transcribes the offset table from the Data Out spec
independently of the adapter, writes a distinct sentinel at every documented
byte offset, and asserts each is read back under the right name. The
duplication is the point: a typo shows up as a disagreement between two
readings of the spec.

`tests/test_verify.py` includes a capture deliberately re-cut as if the FH6
12-byte insert didn't exist, the exact mistake a parser copied from FM7 or
FH5 makes, and asserts the layout check fails on it. A check that only passes
on good data proves nothing.

`tests/test_stage_geometry.py` tests the classifier against circular arcs,
where curvature is 1/r everywhere and there is a right answer at every point, a constant-radius arc must produce exactly one class, and tighter arcs must
produce more severe ones.

`tests/test_stage_notes.py` gives each of the note algorithm's five reduction steps its own
test built from hand-written markings, so a step that stops firing is caught
by name rather than by the note count quietly drifting.

`tests/test_real_capture.py` runs against `tests/fixtures/packet_real.bin`, a
real 324-byte datagram from the game. The capture-level test skips until you
add `real_capture.fzr` alongside it.

---

## Known findings

**`DistanceTraveled` is not metres.** Measured against a real 6,893-packet
capture, offset 292 tracks the distance actually covered at a tight ratio of
**0.788**, constant across every steering, speed and time bucket, with 0.3%
spread. The field is exactly where the table says it is (the whole tail block
checks out: `CurrentRaceTime` advances at 1.0000, `Fuel` sits in 0..1), it
simply does not count metres. The runtime already avoids it as a primary
signal; treat it as not being a distance at all. Stage distance is computed by
integrating the resampled line, which is unaffected.

**`WheelInPuddle` type is unsettled.** the Data Out spec types offset 132 as `S32`;
the official Forza sled spec types the same 16 bytes as `f32`
(`WheelInPuddleDepth`). Offset and width are identical either way, so nothing
downstream shifts, only the interpretation. Both readings are decoded, and
`verify` prints both. Drive through water once and it settles itself.

**Gear 11 appears in real captures.** Probably neutral or a sentinel rather
than an 11-speed gearbox. Harmless, worth knowing before the runtime reads gear.
