"""Voice pack tooling: voice generate / check / say."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from ..config import Config
from ._common import err


def cmd_voice_generate(args: argparse.Namespace, cfg: Config) -> int:
    from ..voice.generate import GenerationError, generate_pack

    # An unnamed non-English pack lands in a directory named after its
    # language, so `--lang de` does not silently overwrite the English pack.
    name = args.name or ("default" if args.lang == "en" else args.lang)
    out_dir = Path(args.output or (Path(cfg.get("audio.voices_dir")) / name))
    try:
        result = generate_pack(
            out_dir,
            engine=args.engine,
            voice=args.voice,
            samplerate=cfg.get("audio.samplerate"),
            rate=args.rate,
            language=args.lang,
        )
    except GenerationError as exc:
        err(f"error: {exc}")
        return 1
    longest = max(result.durations.items(), key=lambda kv: kv[1])
    err(
        f"wrote {result.clips} clips ({result.total_seconds:.1f}s of audio) to "
        f"{result.pack_dir}\n"
        f"engine {result.engine}, voice {result.voice}; "
        f"longest clip '{longest[0]}' at {longest[1]:.2f}s"
    )
    if out_dir.name != cfg.get("audio.voice_pack"):
        err(
            f"note: config uses audio.voice_pack='{cfg.get('audio.voice_pack')}' "
            f"-- set it to '{out_dir.name}' to use this pack"
        )
    print(out_dir)
    return 0


def cmd_voice_check(args: argparse.Namespace, cfg: Config) -> int:
    from ..stage.notes import required_tokens
    from ..stage.schema import load
    from ..voice.pack import VoicePackError, check_pack, stage_coverage

    pack_dir = Path(cfg.get("audio.voices_dir")) / (
        args.pack or cfg.get("audio.voice_pack")
    )
    try:
        report = check_pack(pack_dir, samplerate=cfg.get("audio.samplerate"))
    except VoicePackError as exc:
        err(f"error: {exc}")
        return 1

    print(f"voice pack '{report.name}' at {report.path}")
    print(f"  {len(report.tokens)} tokens, {report.total_seconds:.1f}s of audio")
    for missing in report.missing_files:
        print(f"  MISSING FILE  {missing}")
    for bad in report.bad_files:
        print(f"  UNREADABLE    {bad}")

    exit_code = 0 if report.ok else 1
    for stage_path in args.stage or []:
        stage = load(stage_path)
        needed = required_tokens(stage.notes)
        covered, missing = stage_coverage(pack_dir, needed)
        if missing:
            exit_code = 1
            print(
                f"  stage {stage.name}: {len(covered)}/{len(needed)} tokens "
                f"covered, MISSING: {', '.join(sorted(missing))}"
            )
        else:
            print(f"  stage {stage.name}: all {len(needed)} tokens covered")
    if report.ok and not args.stage:
        print("  pack is consistent.")
    return exit_code


def cmd_voice_say(args: argparse.Namespace, cfg: Config) -> int:
    """Audition a phrase through the real playback path, no game needed."""
    from ..runtime.player import BeepBank, make_player
    from ..voice.pack import load_configured_bank

    if args.pack:
        cfg.data["audio"]["voice_pack"] = args.pack
    beeps = BeepBank(
        samplerate=cfg.get("audio.samplerate"),
        base_clip_s=cfg.get("audio.placeholder_clip_s"),
        crossfade_s=cfg.get("audio.crossfade_ms") / 1000.0,
    )
    bank = load_configured_bank(cfg, beeps)
    tokens = args.tokens
    duration = bank.duration(tokens)
    err(f"{' '.join(tokens)}  ({duration:.2f}s)")
    player = make_player(
        samplerate=cfg.get("audio.samplerate"),
        blocksize=cfg.get("audio.blocksize"),
        device=cfg.get("audio.device"),
        gain_db=cfg.get("audio.gain_db"),
    )
    try:
        player.play(bank.render(tokens))
        time.sleep(duration + 0.3)
    finally:
        player.close()
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("voice", help="voice pack tooling")
    voice_sub = p.add_subparsers(dest="voice_command", required=True)

    v = voice_sub.add_parser(
        "generate",
        help="generate a voice pack with TTS (offline result; network only "
        "needed while generating with the edge engine)",
    )
    v.add_argument(
        "--name",
        default=None,
        help="pack name under voices/ (default: 'default', or the language "
        "code for non-English packs)",
    )
    v.add_argument(
        "--lang",
        default="en",
        choices=["en", "de"],
        help="vocabulary language; also picks the default TTS voice",
    )
    v.add_argument("-o", "--output", help="explicit output directory")
    v.add_argument(
        "--engine",
        default="edge",
        choices=["edge", "sapi"],
        help="edge = MS neural voices (network, best); sapi = offline Windows voices",
    )
    v.add_argument(
        "--voice",
        default=None,
        help="voice name (edge: e.g. en-GB-RyanNeural; sapi: installed voice name)",
    )
    v.add_argument(
        "--rate",
        default="+15%",
        help="speaking rate for the edge engine; co-drivers talk briskly",
    )
    v.set_defaults(func=cmd_voice_generate)

    v = voice_sub.add_parser(
        "check",
        help="validate a pack; with stages, report which tokens they need "
        "that the pack is missing",
    )
    v.add_argument("--pack", default=None, help="pack name (default: config)")
    v.add_argument("stage", nargs="*", help="stage JSON file(s) to check coverage for")
    v.set_defaults(func=cmd_voice_check)

    v = voice_sub.add_parser("say", help="speak a phrase now, through the real path")
    v.add_argument("tokens", nargs="+", help="e.g.: 100 left tightens 1")
    v.add_argument("--pack", default=None, help="override audio.voice_pack for this call")
    v.set_defaults(func=cmd_voice_say)
