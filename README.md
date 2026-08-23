# codriver

**A rally co-driver for Forza Horizon 6.** Drive a race once, and from then
on a voice calls the corners before they arrive: *"one hundred, left tightens
three"*. English or German.

> **Status: early beta.** It works and I drive with it most evenings, but it
> is young software by one person, tuned on a handful of stages. Expect rough
> edges. If something breaks,
> [open an issue](https://github.com/W0nt3x/forza-codriver/issues) and say
> what you did.

## Why this exists

I love the point-to-point races in Horizon, but they always felt like half a
rally. A real rally car has two people in it. One drives, the other reads the
notes: what the road does behind the crest, how tight the next corner really
is, whether it keeps tightening after you've committed. Horizon gives you a
blue line on the ground and a few metres of warning, and that's it.

So this is the missing seat. Forza's official Data Out setting broadcasts
the car's telemetry to whatever is listening; codriver listens, remembers the
road from a lap you drove earlier, and speaks the corners before you get
there. Same category of tool as SimHub or a motion rig: nothing in the game
is touched.

Everything you can tune takes effect while you're still on the road, and
the notes keep improving the more you race a stage.

---

## Getting started

You need Windows, Forza Horizon 6 on the same PC, and about ten minutes.

**1. Get codriver.** `git clone` this project (then `update.bat` fetches new
versions later), or download it as a ZIP (green *Code* button, *Download
ZIP*) and unzip it somewhere.

**2. Double-click `install.bat`.** It looks for Python, offers to install it
if there is none, and sets up a private environment. A minute or two. If you
would rather install Python yourself: <https://www.python.org/downloads/>,
3.11 or newer, tick **"Add python.exe to PATH"**.

**3. Start it.** Double-click **`start.bat`**. A browser tab opens. Windows
asks once whether to allow it on private networks; say yes, that's the phone
HUD.

**4. Follow the Setup tab**, top to bottom:

- *Game settings.* In Forza: **Settings → HUD and Gameplay → Data Out: On,
  IP 127.0.0.1, Port 5400.** Not 5300, whatever the guides say:
  [Forza's Data Out documentation](https://support.forza.net/hc/en-us/articles/51744149102611-Forza-Horizon-6-Data-Out-Documentation)
  reserves 5200 to 5300 for the game itself. Press **Check** while driving
  and the page tells you whether the game is talking to it.
- *Record a recon lap.* Give it a name, press **Start recording**, drive the
  race once at any speed on a clean line, press **Stop**.
- *Build.* Press **Build**. The **Stages** tab shows the notes and a map
  coloured by how tight each corner is.
- *Drive.* On the **Drive** tab pick the stage, press **Start co-driver**,
  and race.

**5. Give it a voice.** The first start uses placeholder beeps. On the
**Voice** tab press **Generate** once, English or German; it needs the
internet for about twenty seconds.

That's it. Every race of a stage gets recorded; press **Learn** on the stage
now and then and the notes improve.

---

## Updating

Double-click **`update.bat`**. It fetches the newest version and keeps your
settings, stages, recordings and voices. A ZIP download cannot update itself;
the script tells you what to copy where in that case.

When an update adds words to the vocabulary, your voice pack plays them as
beeps until you press **Generate** again (the Voice tab marks the pack).
Stages get the new calls on the next **Rebuild** or **Learn**.

## How it works

The telemetry only ever says where the car is *right now*; the road ahead is
not in it. So the recon lap is the map: you drive the stage once, codriver
records the line, and from then on the live stream is only used to find your
position on that line. Everything ahead is read from the recording, which is
how real rallying works too: crews drive the stage before the event and
write their notes down.

Corners come from geometry. The recorded line is resampled to one point
every three metres, then for every point a circle is fitted through its
neighbours about thirty metres either side. The radius says how tight the
road is there and turns into a number from 1 (hairpin) to 6 (barely a bend)
based on the speed you could comfortably carry through it. A few rules boil
thousands of classified points down to a handful of calls: a corner that
opens out again isn't called, a corner that tightens is called once at its
start with the tighter number, and corners close together get joined into
one phrase ("three right into two left"). Jumps are read off the suspension,
all four wheels fully extended at once, and water off the wheels the game
reports as wet.

Timing is the whole game. A call has to be *finished* about two seconds
before the corner, not started then, so the trigger distance is your current
speed times the length of the phrase plus your reaction buffer. At 80 km/h a
short call fires about sixty metres out; at 180 it's closer to two hundred.
Two calls never talk over each other: if they'd collide, the less important
one gives way, and a call that can't finish in time is dropped instead of
spoken mid-corner.

Finding you on the line uses a short search window around your last known
position, which is what keeps the co-driver from jumping to the wrong arm of
a switchback. If the telemetry stops, the queue is cleared; Forza goes silent
during pauses, rewinds and after the finish, so gaps are normal. When you
reappear somewhere else it finds you again.

The voice is pre-recorded words glued together. Every word gets generated
once with text-to-speech, trimmed and normalised, and at runtime the phrase
is assembled from clips. Nothing is synthesised while you drive.

The long version with the packet layout, the maths and what came out of real
captures is in [docs/TECHNICAL.md](docs/TECHNICAL.md).

## Learning from your drives

The recon lap is one line on one day. Every later drive of the same stage is
recorded too (you can turn that off), and **Learn** on the Stages tab folds
them in: the line becomes the average of where the car actually went, so
recon wobbles smooth out and phantom corners disappear, and each corner
remembers the slowest speed you've carried through it, shown next to the
note. A "3" stays a "3", but now you know you take your 3s at about 80 in
this car.

Three or four drives make a noticeable difference. The previous version of
the stage is kept as a `.json.bak` next to it.

## Community stages

The **Stages** tab has a Community section listing stages other players
shared, named after the race in Forza. Click one to see its map and notes;
press **Install** and it is ready to drive. The files come from
[codriver-stages](https://github.com/W0nt3x/codriver-stages), a public
repository that holds nothing but stage files.

Built a good one? Press **Share** on the stage. codriver writes a clean copy
(the line, the notes, the speeds you drove; no recording) to `stages/share/`
and a small relay opens the pull request for you. You get the link, and the
stage appears in everyone's list once it is merged. If the relay doesn't
answer, Share opens the folder and GitHub's upload page instead. Name stages
exactly like the race in Forza.

Running your own community repository? The relay is a Cloudflare Worker in
[relay/](relay/); `community.repo`, `community.relay_url` and
`community.relay_secret` point the app at yours.

## Your phone as the HUD

The game runs fullscreen, so the Setup tab shows a QR code: scan it with a
phone on the same WLAN, prop the phone next to your wheel. It shows the next
call in large type, the distance to it, and a log of what was said. Audio
still comes from the PC. If you'd rather the UI weren't reachable from other
devices, start with `start.bat --local-only`.

## Tuning

There's no correct setting for how a co-driver should call corners; real
crews spend years developing their own system. The defaults here are a
starting point.

Everything adjustable is on the **Config** tab, each value with an
explanation next to it. Changes land in `config/local.yaml` and apply within
half a second, even while driving. Values under *stage* need a rebuild; a few
(voice pack, audio device, telemetry port) are read when the co-driver
starts, and the tab marks them.

The three worth touching first:

- **`runtime.trigger.reaction_buffer_s`** (default 1.8). How many seconds
  before the corner the call should be finished. If calls feel late, raise
  it. This one setting decides more than everything else combined.
- **`stage.curvature.class_speed_bands_kmh`**. What 1 to 6 mean, as speeds.
  Lower the last number and gentle high-speed kinks stop being called.
- **`stage.curvature.window_points`** (default 11). How far the corner
  detector looks to each side. Higher smooths away recon wobbles; lower
  catches more detail but invents corners.

Save your settings as a named preset once something feels right. Two people
on one install should have a preset each.

## Voices

**Generate** on the Voice tab makes a complete pack in about twenty seconds:
English (`en-GB-RyanNeural`) or German (`de-DE-ConradNeural`). Other voices
work from the terminal: `python -m codriver voice generate --lang de --voice
de-DE-KatjaNeural`. These come through the unofficial `edge-tts` package,
not an API Microsoft promises anyone, and it has broken for a few weeks now
and then. If that door closes, the offline engine (`--engine sapi`) keeps
working with the voices built into Windows, and a pack you already generated
is yours either way.

Packs are plain folders under `voices/` with a `manifest.yaml` and one WAV
per word. Record your own, drop the files in, set `audio.voice_pack`. A word
the pack doesn't have is spoken as a beep, so you notice.

## The terminal

Everything the UI does is also a command: `python -m codriver --help`. The
main ones are `capture`, `build`, `run`, `learn`, `voice generate` and
`replay`. The last one plays a recording back into the co-driver without the
game running, which is how most of this was developed.

## Coming soon

Rough order, no dates.

- **Real voices.** Recorded human co-drivers instead of text-to-speech, and
  recording your own pack from the Voice tab.
- **UI.** A Overlay showing direction arrows.
- **Better pace notes.** Sharper severity, fewer phantom corners on bumpy
  recon lines, smarter "into" and "and" linking, and the words the vocabulary
  already has but the generator does not use yet: *don't cut*, *narrows*,
  *keep left*, *square*.
- **More from the telemetry.** Surface changes, narrowing roads, water
  thresholds calibrated on a real ford.
- **Learn, properly.** Letting your drives move the calls themselves, so a
  corner you always brake harder for gets the tighter number.
- **Easier install.** A single download with no Python step.
- **Community.** Ratings for shared stages and automatic checks on uploads.

Have a wish that is not on the list? Open an issue.

## When something doesn't work

- **"Waiting for telemetry" forever.** Press **Check** on the Setup tab
  while driving. It stays out of 5200 to 5300 (listening there can break
  Data Out), so if it finds nothing the game is almost certainly still on
  5300: set it to 5400 in Forza. And you have to be *driving*; menus, pauses
  and replays send nothing.
- **Calls feel late or early.** `runtime.trigger.reaction_buffer_s`, applies
  live.
- **Corners that aren't there.** Raise `stage.curvature.window_points` to 13
  and rebuild, or drive the stage a few more times and press **Learn**.
- **Gentle bends being called at 180 km/h.** Lower the last number of
  `class_speed_bands_kmh`, rebuild.
- **The first corner of a stage is called late.** Known: it sits closer to
  the start than the lead distance, so the call fires immediately rather than
  being skipped.
- **No sound, or sound on the wrong device.** `audio.device` takes the name
  or number of an output device. Empty means Windows default.
- **Port already in use.** Only one program can listen on the telemetry
  port. Stop a running capture, a second co-driver instance, or SimHub.

## Known limits

- Forza sends Data Out to exactly one address. If you also run SimHub or a
  motion rig, you need a UDP splitter in front of both.
- Built for point-to-point races. Multi-lap circuits are untested; the stage
  ends where the recon recording ended.
- Windows only for now. The offline voice engine and the batch starters are
  the Windows-specific parts, the rest is plain Python.

*Not affiliated with Microsoft, Turn 10 or Playground Games. Forza Horizon
is their trademark.*

## Support

codriver is free and stays free. If it makes your rallies better:
[![Support on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/wontex)

## Credits

The corner classification follows
[voidcomputing.hu/blog/rally-pace-notes](https://voidcomputing.hu/blog/rally-pace-notes/),
worth reading even if you never use this tool. Ideas about how a pace-note
tool should feel came from
[PacenotePal](https://github.com/Koenvh1/PacenotePal) and the community
co-driver packs for Richard Burns Rally. Voices are Microsoft's neural voices
via the unofficial `edge-tts` package.
