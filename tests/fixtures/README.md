# Test fixtures

Nothing here yet. `tests/test_real_capture.py` skips until you add:

- `packet_real.bin` — one raw 324-byte datagram from the actual game
- `real_capture.fzr` — a short real recording (a minute is plenty)

Produce both in one drive:

    python -m codriver capture --name real --fixture tests/fixtures/packet_real.bin
    cp recordings/real.fzr tests/fixtures/real_capture.fzr

Include a few seconds stationary at the start (the `stationary` check needs
30+ frames at rest), some sustained driving above 20 km/h, and ideally a
puddle — that last one settles whether bytes 132..147 are s32 flags or f32
depths.

Everything else in the suite only proves the code is self-consistent. These
two files are the only thing that can tell you the offset table matches what
FH6 actually sends.
