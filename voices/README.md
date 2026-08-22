Voice packs live here, one folder per pack (`default/`, `de/`, ...), each with
a `manifest.yaml` and one WAV per word. They are not checked in because they
are generated in about twenty seconds: press Generate on the Voice tab, or run

    python -m codriver voice generate            # English
    python -m codriver voice generate --lang de  # German

You can also record your own: drop WAVs into a folder, list them in a
`manifest.yaml` (token -> file), and set `audio.voice_pack` to the folder name.
