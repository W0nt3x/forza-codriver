# codriver

**A rally co-driver for Forza Horizon 6.** Drive a race once, and from then
on a voice calls the corners before they arrive: *"one hundred, left tightens
three"*. English or German.

## Why this exists

I love the point-to-point races in Horizon, but they always felt like half a
rally. A real rally car has two people in it. One drives, the other reads the
notes: what the road does behind the crest, how tight the next corner really
is, whether it keeps tightening after you've committed. Horizon gives you a
blue line on the ground and a few metres of warning, and that's it.

So this is the missing seat. It doesn't touch the game at all: Forza has an
official setting called Data Out that broadcasts its telemetry (position,
speed, suspension...) to whatever's listening, and codriver just listens. It
remembers where the road goes from a lap you drove earlier and speaks the
corners before you get there. The game never knows it's running, which also
means there's nothing here that could get an account banned.

Most of it was tuned by driving the same two stages over and over, changing a
number, driving again. So everything you can tune takes effect while you're
still on the road, and the notes keep improving the more you race a stage.

---

## Getting started

You need Windows, Forza Horizon 6 on the same PC, and about ten minutes.

**1. Get codriver.** Either `git clone` this project (then `update.bat` can
fetch new versions later), or download it as a ZIP (green *Code* button,
*Download ZIP*) and unzip it somewhere.

**2. Double-click `install.bat`.** It looks for Python; if there is none, it
offers to install it for you (one keypress, recommended). Then it sets up a
private Python environment and fetches what it needs. A minute or two. If
you would rather install Python yourself: <https://www.python.org/downloads/>,
3.11 or newer, and tick **"Add python.exe to PATH"** in the installer.

**3. Start it.** Double-click **`start.bat`**. A browser tab opens. Windows
asks once whether to allow it on private networks; say yes, that's what lets
your phone show the HUD later.

**4. Follow the Setup tab**, top to bottom:

- *Game settings.* In Forza: **Settings → HUD and Gameplay → Data Out: On,
  IP 127.0.0.1, Port 5400.** Not 5300. Yes, every guide on the internet says
  5300, and Forza's own docs now warn that the game uses 5200 to 5300 for itself.
  Press **Check** while driving and the page tells you whether the game is
  talking to it.
- *Record a recon lap.* Give it a name, press **Start recording**, drive the
  race once at any speed on a clean line, press **Stop**.
- *Build.* Press **Build**, and the recording becomes pace notes. Have a look
  at them on the **Stages** tab if you're curious; the map is coloured by how
  tight each corner is.
- *Drive.* On the **Drive** tab pick the stage, press **Start co-driver**,
  and race.

**5. Give it a voice.** The first start uses placeholder beeps so you can
already hear the timing. On the **Voice** tab press **Generate** once,
English or German; it needs the internet for about twenty seconds. Then on
the **Config** tab set `audio.voice_pack` to the new pack name.

That's the whole setup. From here on it's two clicks per session, and every
race of a stage gets recorded automatically. Press **Learn** on the stage
now and then and the notes improve.

---

## Updating

Double-click **`update.bat`**. It fetches the newest version and keeps your
settings, stages, recordings and voices (they are yours, not the program's).
This works when the project was cloned with git; a ZIP download cannot update
itself, the script tells you what to copy where in that case.

## How it works

The telemetry only ever says where the car is *right now*. It knows nothing
about the road ahead (the game's GPS line lives in memory that's off limits).
So the recon lap is the map: you drive the stage once, codriver records the
line, and from then on the live stream is only used to find your position on
that line. Everything ahead is read from the recording. Which is, funnily
enough, exactly how real rallying works: crews drive the stage before the
event and write their notes down.

Corners come from geometry. The recorded line gets resampled to one point
every three metres, then for every point a circle is fitted through its
neighbours about thirty metres either side. The radius of that circle says
how tight the road is there, and turns into a number from 1 (hairpin) to 6
(barely a bend) based on the speed you could comfortably carry through it.
A few rules then boil thousands of classified points down to a handful of
calls: a corner that opens out again isn't called, a corner that tightens is
called once at its start with the tighter number, and corners close together
get joined into one phrase ("three right into two left"). Jumps aren't
guessed from the terrain, by the way. They're read straight off the
suspension: all four wheels fully extended at the same time means you were
flying.

Timing is the whole game. A call has to be *finished* about two seconds
before the corner, not started then, so the trigger distance is your current
speed times the length of the phrase plus your reaction buffer. At 80 km/h a
short call fires about sixty metres out; at 180 it's closer to two hundred.
And two calls never talk over each other. If they'd collide, the less
important one gives way, and a call that can't finish in time anymore gets
dropped instead of spoken mid-corner. A co-driver that babbles while you're
sideways is worse than none.

Finding you on the line uses a deliberately short search window around your
last known position. Sounds like an implementation detail, but it's what
keeps the co-driver from jumping to the wrong arm of a switchback where the
road doubles back twenty metres from itself. If the telemetry stops, the
call queue is cleared; Forza goes silent during pauses, rewinds and after the
finish, so gaps are normal. And when you reappear somewhere else it finds you
again.

The voice is pre-recorded words glued together. Every word of the vocabulary
gets generated once with text-to-speech, trimmed and normalised, and at
runtime the phrase is just assembled from clips. Nothing is synthesised while
you drive, because that would make the timing unpredictable, and see above.

The long version with the packet layout, the maths and what came out of real
captures is in [docs/TECHNICAL.md](docs/TECHNICAL.md).

## Learning from your drives

The recon lap is one line on one day. Every later drive of the same stage is
recorded too (you can turn that off), and **Learn** on the Stages tab folds
them in. The line becomes the average of where the car actually went across
all your drives, so wobbles from the recon lap smooth out and phantom
corners disappear. And each corner remembers the slowest speed you've
actually carried through it, shown next to the note. A "3" stays a "3", but
now you know you take your 3s at about 80 in this car. No geometry can tell
you that.

Three or four drives make a noticeable difference. The previous version of
the stage is kept as a `.json.bak` next to it, in case Learn makes things
worse (it shouldn't, but I've been wrong before).

## Your phone as the HUD

The game runs fullscreen, so a second window on the PC is useless. The Setup
tab shows a QR code instead: scan it with a phone on the same WLAN, prop the
phone next to your wheel, done. It shows the next call in large type, the
distance to it, and a log of what was said. Audio still comes from the PC.

Nothing leaves your WLAN and there's no account anywhere. If you'd rather
the UI weren't reachable from other devices at all, start with
`start.bat --local-only`.

## Tuning

There's no correct setting for how a co-driver should call corners. Real
crews spend years developing a shared system and no two are alike, so the
defaults here are a starting point, not an answer.

Everything adjustable is on the **Config** tab, each value with a plain
explanation next to it. Changes are saved to `config/local.yaml` and apply
within half a second, even while driving. Values under *stage* act when you
build or learn a stage (so rebuild afterwards); *runtime* and *audio* act
live.

The three worth touching first:

- **`runtime.trigger.reaction_buffer_s`** (default 1.8). How many seconds
  before the corner the call should be finished. If calls feel late, raise
  it. This one setting decides more than everything else combined, and it's
  very personal.
- **`stage.curvature.class_speed_bands_kmh`**. What 1 to 6 mean, as speeds.
  Lower the last number and gentle high-speed kinks stop being called.
- **`stage.curvature.window_points`** (default 11). How far the corner
  detector looks to each side. Higher smooths away wobbles from your recon
  driving; lower catches more detail but invents corners.

Save your settings as a named preset once something feels right. If two
people drive on the same install, make a preset each: a co-driver calibrated
to someone else's pace always feels slightly off, and you'll blame the tool
instead of the config.

## Voices

**Generate** on the Voice tab makes a complete pack in about twenty seconds:
English (`en-GB-RyanNeural`, a British male) or German (`de-DE-ConradNeural`,
the "hundert, links, zieht zu, eins" one). Other voices work too, from the
terminal: `python -m codriver voice generate --lang de --voice
de-DE-KatjaNeural`. There's also an offline engine (`--engine sapi`) that
uses the voices built into Windows and needs no internet. It sounds like it,
but it works.

Packs are plain folders under `voices/` with a `manifest.yaml` and one WAV
per word. Record your own voice, or your friend's, drop the files in, set
`audio.voice_pack`, done. A word the pack doesn't have gets spoken as a beep,
deliberately audible, so you notice instead of silently missing calls.

## The terminal

Everything the UI does is also a command: `python -m codriver --help`. The
main ones are `capture`, `build`, `run`, `learn`, `voice generate` and
`replay`. That last one plays a recording back into the co-driver without
the game running, which is how most of this was actually developed. Tuning
against replays beats restarting a race forty times.

## When something doesn't work

- **"Waiting for telemetry" forever.** Press **Check** on the Setup tab
  while driving. It scans the likely ports and tells you where the game is
  actually sending. Nine times out of ten the game is still on port 5300, or
  Data Out is off. And you have to be *driving*: menus, pauses and replays
  send nothing.
- **Calls feel late or early.** `runtime.trigger.reaction_buffer_s`. Applies
  live, so you can adjust it mid-race.
- **Corners that aren't there.** Your recon line wobbled. Raise
  `stage.curvature.window_points` to 13 and rebuild, or just drive the stage
  a few more times and press **Learn**.
- **Gentle bends being called at 180 km/h.** Lower the last number of
  `class_speed_bands_kmh`, rebuild.
- **The first corner of a stage is called late.** Known and half-intended:
  it sits closer to the start than the lead distance, so the call fires
  immediately rather than being skipped.
- **No sound, or sound on the wrong device.** `audio.device` takes the name
  or number of an output device. Empty means Windows default.
- **Port already in use.** Only one program can listen on the telemetry
  port. Stop a running capture, a second co-driver instance, or SimHub.

## Known limits

- Forza sends Data Out to exactly one address. If you also run SimHub or a
  motion rig, you need a UDP splitter in front of both.
- Built for point-to-point races. Multi-lap circuits are untested; the stage
  simply ends where the recon recording ended.
- Windows only for now. The offline voice engine and the batch starters are
  the Windows-specific parts, the rest is plain Python.

*Not affiliated with Microsoft, Turn 10 or Playground Games. Forza Horizon
is their trademark.*

## Credits

The corner classification follows the approach described at
[voidcomputing.hu/blog/rally-pace-notes](https://voidcomputing.hu/blog/rally-pace-notes/),
which is worth reading even if you never use this tool. Ideas about how a
pace-note tool should feel came from
[PacenotePal](https://github.com/Koenvh1/PacenotePal) and the community
co-driver packs for Richard Burns Rally. Voices are generated with
Microsoft's neural text-to-speech.
