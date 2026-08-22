# stages

Your pace-note stages live here, one `<name>.json` per stage. They get here in
three ways: **Build** on the Setup tab (from a recon recording), **Install**
from the Community section on the Stages tab, or by dropping in a file someone
sent you. codriver creates this folder on its own if it is missing, you never
have to make it by hand.

- `<name>.json` is the stage: the line, the notes, where it came from, the
  settings used. It is plain JSON and meant to be hand-edited if a corner is
  called wrong; Rebuild overwrites it, so edit after the last rebuild.
- `<name>.json.bak` is the previous version, written by Learn, in case Learn
  made things worse.
- `share/` holds the clean copies Share writes for upload to the community
  repository.

Stage files are not tracked by git (`.gitignore`), they are yours.
