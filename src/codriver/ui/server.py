"""The web UI server: FastAPI over the same functions the CLI uses.

Start with ``codriver ui``. Binds all interfaces by default so a phone on
the same WLAN can show the HUD while the game is fullscreen; nothing leaves
the LAN and there is no account. Long-running things (capture, run, scan,
voice generation) are background jobs; their events stream to every open
browser tab over one WebSocket.

The config editor is generated from ``defaults.yaml``: every key, its value,
and the comment block above it as help text. Edits go to ``local.yaml`` and
are picked up live by whatever is running.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from ..config import Config
from .jobs import JobBusy, JobManager

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


# --------------------------------------------------------------------------
# config schema from defaults.yaml (comments become help text)
# --------------------------------------------------------------------------

_KEY_RE = re.compile(r"^(\s*)([A-Za-z_][\w]*):\s*(.*?)\s*$")

# The Config tab shows three tiers. Most people only ever need the first one.
ESSENTIAL_KEYS = [
    "runtime.trigger.reaction_buffer_s",
    "stage.curvature.class_speed_bands_kmh",
    "stage.curvature.window_points",
    "audio.voice_pack",
    "audio.gain_db",
    "telemetry.port",
]
EXPERT_PREFIXES = (
    "telemetry.adapter", "telemetry.bind_host", "telemetry.socket_",
    "telemetry.expected_packet_size", "capture.", "replay.", "stage.line.",
    "stage.resample.", "stage.curvature.min_divisor",
    "stage.curvature.comfortable_lateral_g", "stage.curvature.auto_orient",
    "stage.learn.", "stage.hazards.", "runtime.locate.", "runtime.gaps.",
    "runtime.record.dir", "runtime.queue.", "audio.samplerate", "audio.channels",
    "audio.blocksize", "audio.voices_dir", "audio.placeholder_clip_s", "logging.",
)
LABELS = {
    "runtime.trigger.reaction_buffer_s": "Calls earlier or later (seconds before the corner)",
    "stage.curvature.class_speed_bands_kmh": "What the numbers 1 to 6 mean (km/h)",
    "stage.curvature.window_points": "Smooth out false corners",
    "audio.voice_pack": "Voice",
    "audio.gain_db": "Volume (dB)",
    "telemetry.port": "Telemetry port",
    "audio.device": "Audio output device",
    "runtime.record.enabled": "Record my drives for Learn",
}
RANGES = {  # key: (min, max, step) for a slider
    "runtime.trigger.reaction_buffer_s": (0.8, 3.5, 0.1),
    "stage.curvature.window_points": (5, 17, 1),
    "audio.gain_db": (-24, 12, 1),
    "audio.crossfade_ms": (0, 80, 5),
    "audio.placeholder_clip_s": (0.2, 0.8, 0.05),
    "runtime.trigger.min_lead_m": (0, 100, 5),
    "runtime.trigger.max_lead_m": (100, 800, 10),
    "runtime.queue.drop_if_later_than_s": (0, 1.5, 0.1),
    "stage.notes.long_min_m": (40, 300, 10),
    "stage.notes.collapse_window_points": (5, 50, 1),
    "stage.notes.tightens_min_run_points": (4, 30, 1),
    "stage.notes.link_into_max_m": (5, 60, 5),
    "stage.notes.link_and_max_m": (20, 120, 5),
    "stage.notes.distance_call_min_m": (20, 200, 10),
}
UNITS = {"s": "seconds", "ms": "ms", "m": "metres", "kmh": "km/h", "db": "dB",
         "points": "points", "hz": "Hz", "g": "g", "bytes": "bytes", "frames": "frames"}

# Read once when the co-driver (or a recording) starts, not while it runs.
# Everything else under runtime/audio is picked up live by the hot reload.
RESTART_PREFIXES = (
    "telemetry.", "capture.", "replay.", "runtime.record.",
    "audio.voice_pack", "audio.voices_dir", "audio.samplerate", "audio.channels",
    "audio.blocksize", "audio.device", "audio.gain_db",
)


def _label(key: str) -> str:
    """'runtime.trigger.reaction_buffer_s' -> 'Reaction buffer (seconds)'."""
    if key in LABELS:
        return LABELS[key]
    parts = key.split(".")[-1].split("_")
    unit = UNITS.get(parts[-1]) if len(parts) > 1 else None
    words = parts[:-1] if unit else parts
    text = " ".join(words).capitalize()
    return f"{text} ({unit})" if unit else text


def _tier(key: str) -> str:
    if key in ESSENTIAL_KEYS:
        return "essential"
    if key.startswith(EXPERT_PREFIXES):
        return "expert"
    return "more"


def output_devices() -> list[dict]:
    """Audio outputs for the device dropdown. Empty list if audio is unavailable."""
    try:
        import sounddevice as sd

        apis = sd.query_hostapis()
        devices = [d for d in sd.query_devices() if d.get("max_output_channels", 0) > 0]
        # Windows offers every device through several drivers (MME,
        # DirectSound, WASAPI). MME truncates names; WASAPI has the full
        # ones and one entry per device, so prefer it when present.
        wasapi = [d for d in devices if "WASAPI" in apis[d["hostapi"]]["name"]]
        if wasapi:
            devices = wasapi
        seen: set[str] = set()
        out = [{"value": None, "label": "Windows default"}]
        for d in devices:
            if d["name"] in seen:
                continue
            seen.add(d["name"])
            out.append({"value": d["name"], "label": d["name"]})
        return out
    except Exception:
        return []


def config_schema(
    defaults_path: Path,
    merged: dict,
    local: dict,
    options: dict[str, list] | None = None,
) -> list[dict]:
    """Walk defaults.yaml line by line; every scalar/list key becomes a field
    with its comment block as help. Regular YAML only, which ours is.
    ``options`` maps a key to dropdown choices (voice packs, audio devices)."""
    options = options or {}
    fields: list[dict] = []
    stack: list[tuple[int, str]] = []  # (indent, key)
    pending: list[str] = []
    for raw in defaults_path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped:
            pending = []
            continue
        if stripped.startswith("#"):
            text = stripped.lstrip("#").strip()
            if not set(text) <= {"-"}:  # skip rule lines
                pending.append(text)
            continue
        m = _KEY_RE.match(raw)
        if not m:
            continue
        indent, key, value = len(m.group(1)), m.group(2), m.group(3)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join(k for _, k in stack) + ("." if stack else "") + key
        if value == "" or value.startswith("#"):
            stack.append((indent, key))
            pending = []
            continue
        value = value.split(" #")[0].strip()
        current = _dig(merged, path)
        field = {
            "key": path,
            "section": path.split(".")[0],
            "label": _label(path),
            "tier": _tier(path),
            "needs_rebuild": path.startswith("stage."),
            "needs_restart": path.startswith(RESTART_PREFIXES),
            "value": current,
            "default": yaml.safe_load(value),
            "overridden": _dig(local, path, missing=True) is not _MISSING,
            "type": _type_of(current),
            "help": " ".join(pending),
        }
        if path in RANGES:
            field["range"] = list(RANGES[path])
        if path in options:
            field["options"] = options[path]
        fields.append(field)
        pending = []
    # Essentials in the order listed above, everything else as in the file.
    order = {k: i for i, k in enumerate(ESSENTIAL_KEYS)}
    fields.sort(key=lambda f: (0, order[f["key"]]) if f["key"] in order else (1, 0))
    return fields


_MISSING = object()


def _apply_live(cfg: Config) -> None:
    """Settings the process itself holds. Re-read after a config change so
    they act now, not at the next program start. Nothing in the config should
    ever require closing the program; Stop and Start on the Drive tab is the
    most anyone has to do."""
    import logging as _logging

    try:
        _logging.getLogger().setLevel(str(cfg.get("logging.level")).upper())
    except (KeyError, ValueError):
        pass


def _voice_gaps(voices_dir: Path) -> dict[str, list[str]]:
    """Per pack, the vocabulary tokens its manifest does not cover.

    A pack generated before a word was added to the vocabulary still works,
    the missing word plays as a beep, but the UI should say so and point at
    Generate rather than let someone hear a beep at a river and wonder.
    """
    from ..voice.pack import read_manifest
    from ..voice.vocab import VOCABULARY

    out: dict[str, list[str]] = {}
    for manifest_path in sorted(voices_dir.glob("*/manifest.yaml")):
        try:
            have = {str(t) for t in read_manifest(manifest_path.parent)["tokens"]}
        except Exception:  # a broken pack is reported elsewhere
            continue
        gaps = sorted(set(VOCABULARY) - have)
        if gaps:
            out[manifest_path.parent.name] = gaps
    return out


def _dig(node: Any, path: str, missing: bool = False) -> Any:
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING if missing else None
        node = node[part]
    return node


def _type_of(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "list"
    if value is None:
        return "null"
    return "str"


def _set_local(local_path: Path, key: str, value: Any) -> dict:
    data = yaml.safe_load(local_path.read_text(encoding="utf-8")) if local_path.is_file() else {}
    data = data or {}
    node = data
    parts = key.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            raise HTTPException(400, f"{key} collides with a scalar in local.yaml")
    node[parts[-1]] = value
    local_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return data


def _unset_local(local_path: Path, key: str) -> None:
    if not local_path.is_file():
        return
    data = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}
    parts = key.split(".")
    node = data
    trail = []
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return
        trail.append((node, part))
        node = node[part]
    if isinstance(node, dict):
        node.pop(parts[-1], None)
    # prune empty sections
    for parent, part in reversed(trail):
        if parent[part] == {}:
            del parent[part]
    local_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _coerce(value: Any, kind: str) -> Any:
    if kind == "bool":
        return bool(value) if not isinstance(value, str) else value.lower() in ("1", "true", "yes", "on")
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value) if value is not None else None
    if kind == "list":
        if isinstance(value, str):
            return [yaml.safe_load(v.strip()) for v in value.split(",") if v.strip()]
        return list(value)
    return value


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


_SAFE_FILE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,80}\.json$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._\-]{0,80}$")
_SAFE_PACK = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,40}$")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._\-]{0,120}$")


def _inside(base: Path, candidate: Path) -> bool:
    """True when ``candidate``, fully resolved, lives under ``base``. The last
    word on every path built from a request or a file, whatever the name
    check before it let through."""
    try:
        base_r = base.resolve()
        cand_r = candidate.resolve()
    except (OSError, RuntimeError):
        return False
    return cand_r == base_r or base_r in cand_r.parents


def _file_under(base: Path, name: object, what: str) -> Path:
    """``base/<name>`` for a plain file name out of a request or a stage
    file: no separators of either kind (this is Windows), no ``..``, and the
    resolved result must stay under ``base``. 400 otherwise."""
    text = str(name or "").strip()
    if not _SAFE_FILENAME.match(text) or ".." in text:
        raise HTTPException(400, f"not a usable {what} name: {text[:80]!r}")
    path = base / text
    if not _inside(base, path):
        raise HTTPException(400, f"{what} {text[:80]!r} is outside its folder")
    return path


def _stage_path(stages_dir: Path, name: object):
    """``stages/<name>.json`` for a name that cannot leave ``stages/``.

    The name comes from a URL or a request body, so anything with a
    separator (either kind, this is Windows), a drive letter or ``..`` is
    refused before it touches the filesystem, and the result is checked
    to really be under stages/."""
    text = str(name or "")
    if not _SAFE_NAME.match(text) or ".." in text:
        raise HTTPException(400, f"not a stage name: {text!r}")
    path = stages_dir / f"{text}.json"
    if not _inside(stages_dir, path):
        raise HTTPException(400, f"not a stage name: {text!r}")
    return path


def _clean_community(value: object) -> dict:
    """The community block of a shared file, for display: one line each."""
    from ..stage.schema import clean_text

    if not isinstance(value, dict):
        return {}
    return {
        "race": clean_text(value.get("race"), 80),
        "author": clean_text(value.get("author"), 60),
        "tool_version": clean_text(value.get("tool_version"), 20),
        "shared_utc": clean_text(value.get("shared_utc"), 32),
    }
"""Community stage file names: lowercase slug plus .json, nothing that could
climb out of the stages folder."""


MAX_DOWNLOAD_BYTES = 5_000_000


def _http_get(url: str, timeout_s: float = 8.0, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
    """GET with a ceiling. The community repo is configurable, so whatever
    answers there is not trusted to be small; a stage is a few hundred KB."""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "codriver"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"response larger than {max_bytes // 1_000_000} MB, not reading it")
    return data


# --------------------------------------------------------------------------
# Browser-side guards. The API has no login (it is one person's PC), so the
# browser's own rules are what keeps other web pages out of it:
#
# * A page on any site can fire a plain POST at 127.0.0.1 ("simple request",
#   no preflight, the browser just sends it) and, although it cannot read the
#   answer, the side effect happens: a run stopped, a stage shared. Requiring
#   a custom header on every state-changing request turns it into a request
#   that needs a CORS preflight, and nothing here answers preflights.
# * DNS rebinding: a page whose domain first resolves to the attacker and, a
#   minute later, to this PC becomes same-origin with the API, may read
#   everything, and can set the custom header itself. Two things stop it.
#   The UI is only ever opened as localhost or an IP address, so a domain
#   name in the Host header is refused outright. And when a browser sends an
#   Origin, its host must equal the Host header: the real page's Origin and
#   Host are the same address, a rebinding page's Origin is the attacker's
#   domain while Host is this PC. Checked on every state-changing request
#   and on the WebSocket handshake.
# --------------------------------------------------------------------------

PAGE_HEADER = "x-codriver"
_MUTATING = {"POST", "PUT", "DELETE", "PATCH"}


def _hostname_ok(name: str) -> bool:
    """localhost, *.localhost, or a real IP literal. Nothing else is ever a
    legitimate way to open the UI."""
    import ipaddress

    name = (name or "").strip().lower().strip("[]")
    if not name:
        return False
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        return False
    return not addr.is_unspecified  # 0.0.0.0 is a bind address, not a page


def _host_header_ok(host: str) -> bool:
    host = (host or "").strip()
    if host.startswith("["):  # [::1]:8777
        return _hostname_ok(host.split("]")[0] + "]")
    if host.count(":") == 1:  # name:port
        host = host.rsplit(":", 1)[0]
    return _hostname_ok(host)


def _origin_matches_host(origin: str, host: str) -> bool:
    """The Origin's host[:port] must be the Host header, character for
    character after lowercasing. "null" and foreign names fail here."""
    from urllib.parse import urlsplit

    try:
        netloc = urlsplit(origin.strip()).netloc
    except ValueError:
        return False
    return bool(netloc) and netloc.lower() == host.strip().lower()


class BrowserGuard:
    """Pure ASGI middleware, see the block comment above."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        host = headers.get("host", "").strip()
        if not host:
            # Every HTTP/1.1 client, browsers first of all, sends Host. Only an
            # HTTP/1.0 tool may leave it out.
            if scope.get("http_version", "1.1") != "1.0":
                return await self._reject(scope, receive, send, 400, "no Host header")
        elif not _host_header_ok(host):
            return await self._reject(scope, receive, send, 400,
                                      "open the UI as localhost or an IP address, not a domain name")
        origin = headers.get("origin")
        if scope["type"] == "http":
            if scope.get("method", "GET") in _MUTATING and scope.get("path", "").startswith("/api/"):
                if headers.get(PAGE_HEADER) != "1":
                    return await self._reject(scope, receive, send, 403,
                                              "state-changing requests must come from the codriver page (X-Codriver header)")
                if origin is not None and not _origin_matches_host(origin, host):
                    return await self._reject(scope, receive, send, 403,
                                              "request origin does not match the address this UI was opened at")
        elif origin is not None and not _origin_matches_host(origin, host):
            return await self._reject(scope, receive, send, 403, "foreign origin")
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope, receive, send, status: int, detail: str) -> None:
        if scope["type"] == "websocket":
            await receive()  # the connect message, then refuse the handshake
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]})
        await send({"type": "http.response.body", "body": body})

def _http_post_json(
    url: str, payload: dict, timeout_s: float = 60.0, headers: dict | None = None
) -> dict:
    """POST JSON, get JSON. An HTTP error carries the server's own message,
    which for the relay is the reason a share was refused."""
    import urllib.error
    import urllib.request

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"User-Agent": "codriver", "Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error") or detail
        except Exception:
            pass
        raise RuntimeError(f"{exc.code}: {detail[:300]}") from None


def _stage_detail_dict(st) -> dict:
    """What the Stages tab draws: line, markings, notes. Shared by a stage on
    disk and a community preview, so both look the same on the map."""
    return {
        "name": st.name,
        "length_m": st.length_m,
        "spacing_m": st.spacing_m,
        "line": [[round(p.x, 1), round(p.z, 1)] for p in st.line],
        "markings": [m.label for m in st.markings],
        "notes": [
            {"at_m": n.at_m, "text": n.text, "index": n.index, "kind": n.kind,
             "severity": n.severity, "radius_m": n.radius_m,
             "observed_kmh": n.observed_kmh, "length_m": n.length_m}
            for n in st.notes
        ],
        "source": st.source,
        "generator": st.generator,
    }


def lan_ip() -> str:
    """The address a phone on the same network should use. No packet is sent
    -- connecting a UDP socket only picks the outbound interface."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _stage_summary(path: Path) -> dict | None:
    from ..stage.schema import StageError, load

    try:
        st = load(path)
    except (StageError, OSError, ValueError):
        return None
    return {
        "name": path.stem,
        "file": path.name,
        "length_m": st.length_m,
        "notes": len(st.notes),
        "points": len(st.line),
        "learned_runs": len(st.generator.get("learned_from_runs", [])),
        "source": st.source.get("capture"),
    }


# --------------------------------------------------------------------------
# the app
# --------------------------------------------------------------------------


def create_app(cfg: Config, root: Path, host_for_links: str | None = None, port: int = 8777) -> FastAPI:
    jobs = JobManager()
    sockets: set[WebSocket] = set()
    queue: asyncio.Queue = asyncio.Queue()
    loop_holder: dict[str, asyncio.AbstractEventLoop] = {}

    # All project folders resolve against the config's project root, the
    # same way the runtime resolves them, so what the UI writes is what
    # `run` finds, wherever the process was started from.
    stages_dir = root / "stages"
    recordings_dir = cfg.path("capture.dir")
    runs_dir = cfg.path("runtime.record.dir")
    voices_dir = cfg.path("audio.voices_dir")

    def fanout(event: dict) -> None:
        loop = loop_holder.get("loop")
        if loop is not None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

    jobs.subscribe(fanout)

    async def pump() -> None:
        while True:
            event = await queue.get()
            dead = []
            for ws in list(sockets):
                try:
                    await ws.send_text(json.dumps(event, default=str))
                except Exception:
                    dead.append(ws)
            for ws in dead:
                sockets.discard(ws)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        loop_holder["loop"] = asyncio.get_running_loop()
        task = asyncio.create_task(pump())
        try:
            yield
        finally:
            task.cancel()
            jobs.stop(timeout_s=2.0)

    app = FastAPI(title="codriver", docs_url=None, redoc_url=None, lifespan=lifespan)

    # -- static --------------------------------------------------------------

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
            "<rect width='64' height='64' rx='14' fill='#0f1115'/>"
            "<path d='M14 40 Q32 10 50 40' fill='none' stroke='#2fb3ff' "
            "stroke-width='8' stroke-linecap='round'/>"
            "<circle cx='50' cy='40' r='6' fill='#fff'/></svg>"
        )
        return Response(svg, media_type="image/svg+xml")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # -- state ---------------------------------------------------------------

    @app.get("/api/state")
    async def state() -> dict:
        cfg.poll()
        ip = host_for_links or lan_ip()
        return {
            "version": __import__("codriver").__version__,
            "lan_url": f"http://{ip}:{port}",
            "telemetry_port": cfg.get("telemetry.port"),
            "voice_pack": cfg.get("audio.voice_pack"),
            "job": jobs.status(),
            "recent": list(jobs.history)[-40:],
            "stages": [s for s in (_stage_summary(p) for p in sorted(stages_dir.glob("*.json"))) if s],
            "recordings": [
                {"file": p.name, "bytes": p.stat().st_size}
                for p in sorted(recordings_dir.glob("*.fzr"))
            ],
            "voices": [
                p.parent.name for p in sorted(voices_dir.glob("*/manifest.yaml"))
            ],
            "voice_gaps": _voice_gaps(voices_dir),
        }

    @app.get("/api/qr.svg")
    async def qr() -> Response:
        import qrcode
        import qrcode.image.svg

        ip = host_for_links or lan_ip()
        img = qrcode.make(f"http://{ip}:{port}", image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
        buf = io.BytesIO()
        img.save(buf)
        return Response(buf.getvalue(), media_type="image/svg+xml")

    # -- jobs ----------------------------------------------------------------

    def _start(kind: str, fn, label: str = "") -> dict:
        try:
            jobs.start(kind, fn, label)
        except JobBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "job": jobs.status()}

    @app.post("/api/stop")
    async def stop() -> dict:
        ended = await asyncio.get_running_loop().run_in_executor(None, jobs.stop)
        return {"ok": ended, "job": jobs.status()}

    @app.post("/api/scan")
    async def scan(body: dict | None = None) -> dict:
        from ..net import scan as scanner

        duration = float((body or {}).get("duration", 20.0))

        def job(emit, should_stop):
            ports = scanner.parse_port_spec(scanner.DEFAULT_SPEC)
            configured = cfg.get("telemetry.port")
            ports = sorted({configured, *ports})
            emit({"kind": "scan_started", "ports": len(ports), "duration": duration})
            result = scanner.scan(
                ports,
                duration_s=duration,
                on_first_hit=lambda hit: emit({"kind": "scan_hit", "port": hit.port,
                                               "looks_like_fh6": hit.looks_like_fh6}),
            )
            emit({
                "kind": "scan_done",
                "found": [{"port": h.port, "packets": h.packets, "fh6": h.looks_like_fh6}
                          for h in result.found],
                "refused": sorted(result.refused),
                "skipped_reserved": sorted(result.skipped_reserved),
                "reserved": [scanner.RESERVED_LO, scanner.RESERVED_HI],
                "configured": configured,
            })
            return result

        return _start("scan", job, "scanning ports")

    @app.post("/api/capture")
    async def capture(body: dict) -> dict:
        from ..record.capture import default_capture_path
        from ..record.recon import capture_stream

        name = re.sub(r"[^\w\-]+", "-", str(body.get("name") or "")).strip("-").lower() or None
        path = default_capture_path(recordings_dir, name)

        def job(emit, should_stop):
            return capture_stream(cfg, path, note=body.get("note", ""),
                                  on_event=emit, should_stop=should_stop)

        return _start("capture", job, f"recording {path.name}")

    @app.post("/api/run")
    async def run(body: dict) -> dict:
        from ..runtime.run import run_stage
        from ..stage.schema import load

        name = body.get("stage")
        path = _stage_path(stages_dir, name)
        if not path.is_file():
            raise HTTPException(404, f"no stage {name}")
        stage = load(path)
        record_dir = runs_dir if cfg.get("runtime.record.enabled") and body.get("record", True) else None

        def job(emit, should_stop):
            return run_stage(stage, cfg, silent=bool(body.get("silent", False)), hud=False,
                             record_dir=record_dir, on_event=emit, should_stop=should_stop)

        return _start("run", job, f"driving {stage.name}")

    # -- stages --------------------------------------------------------------

    @app.post("/api/build")
    async def build(body: dict) -> dict:
        from ..stage.build import build_stage
        from ..stage.schema import save

        capture = _file_under(recordings_dir, body.get("capture"), "recording")
        if not capture.is_file():
            raise HTTPException(404, f"no recording {capture.name}")
        name = re.sub(r"[^\w\-]+", "-", str(body.get("name") or capture.stem)).strip("-").lower()
        try:
            stage, report = await asyncio.get_running_loop().run_in_executor(
                None, lambda: build_stage(capture, cfg, name=name)
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        save(stage, stages_dir / f"{name}.json")
        return {"ok": True, "stage": name, "report": report.render(),
                "notes": [n.text for n in stage.notes]}

    @app.get("/api/stages/{name}")
    async def stage_detail(name: str) -> dict:
        from ..stage.schema import load
        from ..stage.learn import runs_for_stage

        path = _stage_path(stages_dir, name)
        if not path.is_file():
            raise HTTPException(404, f"no stage {name}")
        st = load(path)
        return {**_stage_detail_dict(st), "runs": [p.name for p in runs_for_stage(st, runs_dir)]}

    @app.delete("/api/stages/{name}")
    async def stage_delete(name: str) -> dict:
        path = _stage_path(stages_dir, name)
        if not path.is_file():
            raise HTTPException(404, f"no stage {name}")
        path.unlink()
        return {"ok": True}

    @app.post("/api/stages/{name}/rebuild")
    async def stage_rebuild(name: str) -> dict:
        from ..stage.build import build_stage
        from ..stage.schema import load, save

        path = _stage_path(stages_dir, name)
        if not path.is_file():
            raise HTTPException(404, f"no stage {name}")
        st = load(path)
        try:
            # source.capture was written by the builder, but the file may have
            # come from anywhere since, and anywhere can write anything there.
            capture = _file_under(recordings_dir, st.source.get("capture", ""), "recording")
        except HTTPException as exc:
            raise HTTPException(
                400, "this stage does not name a usable recording of its own; "
                     "record and build it on the Setup tab instead",
            ) from exc
        if not capture.is_file():
            raise HTTPException(400, f"source recording {capture.name} is gone")
        stage, report = await asyncio.get_running_loop().run_in_executor(
            None, lambda: build_stage(capture, cfg, name=name)
        )
        save(stage, path)
        return {"ok": True, "report": report.render(), "notes": len(stage.notes)}

    @app.post("/api/stages/{name}/learn")
    async def stage_learn(name: str) -> dict:
        from ..stage.learn import learn_stage, runs_for_stage
        from ..stage.schema import load, save

        path = _stage_path(stages_dir, name)
        if not path.is_file():
            raise HTTPException(404, f"no stage {name}")
        st = load(path)
        runs = runs_for_stage(st, runs_dir)
        if not runs:
            raise HTTPException(400, "no recorded runs for this stage yet, drive it first")
        learned, report = await asyncio.get_running_loop().run_in_executor(
            None, lambda: learn_stage(st, cfg, runs)
        )
        path.with_suffix(".json.bak").write_bytes(path.read_bytes())
        save(learned, path)
        return {"ok": True, "report": report.render(), "notes": len(learned.notes)}

    # -- config --------------------------------------------------------------

    @app.get("/api/config")
    async def config_get() -> dict:
        cfg.poll()
        local = yaml.safe_load(cfg.local_path.read_text(encoding="utf-8")) if cfg.local_path.is_file() else {}
        voices = [p.parent.name for p in sorted(voices_dir.glob("*/manifest.yaml"))]
        pack_options = [{"value": v, "label": v} for v in voices]
        current = str(cfg.get("audio.voice_pack"))
        if current not in voices:
            # Show the truth: the configured pack does not exist. A dropdown
            # that silently displays its first entry instead is how people end
            # up hearing beeps while believing a voice is selected.
            pack_options.insert(0, {"value": current, "label": f"{current} (not generated yet)"})
        options = {
            "audio.voice_pack": pack_options,
            "audio.device": await asyncio.get_running_loop().run_in_executor(None, output_devices),
        }
        return {"fields": config_schema(cfg.defaults_path, cfg.data, local or {}, options)}

    @app.put("/api/config")
    async def config_put(body: dict) -> dict:
        key = str(body.get("key", ""))
        try:
            current = cfg.get(key)
        except KeyError:
            raise HTTPException(404, f"unknown config key {key}")
        value = _coerce(body.get("value"), _type_of(current) if current is not None else body.get("type", "str"))
        if key == "audio.device" and value in ("", "default", "None"):
            value = None  # the Windows default output
        _set_local(cfg.local_path, key, value)
        cfg.poll(immediate=True)
        _apply_live(cfg)
        return {"ok": True, "key": key, "value": cfg.get(key)}

    @app.delete("/api/config/{key:path}")
    async def config_delete(key: str) -> dict:
        _unset_local(cfg.local_path, key)
        cfg.poll(immediate=True)
        _apply_live(cfg)
        return {"ok": True, "key": key, "value": cfg.get(key, None)}

    # -- voice ---------------------------------------------------------------

    @app.post("/api/voice/generate")
    async def voice_generate(body: dict) -> dict:
        from ..voice.generate import generate_pack

        lang = body.get("lang", "en")
        name = str(body.get("name") or ("default" if lang == "en" else lang)).strip().lower()
        if not _SAFE_PACK.match(name):
            raise HTTPException(400, "pack name: lowercase letters, digits, - and _, nothing else")
        pack_dir = voices_dir / name
        if not _inside(voices_dir, pack_dir):
            raise HTTPException(400, "pack name is outside the voices folder")
        engine = body.get("engine", "edge")
        voice = body.get("voice") or None

        def job(emit, should_stop):
            emit({"kind": "voice_started", "name": name, "lang": lang, "engine": engine})
            result = generate_pack(pack_dir, engine=engine, voice=voice,
                                   samplerate=cfg.get("audio.samplerate"), language=lang)
            # If the configured pack does not exist, this new one becomes the
            # active voice right away. Nobody should generate a voice and then
            # hear beeps because a dropdown still pointed at a missing folder.
            configured = str(cfg.get("audio.voice_pack"))
            selected = False
            if not (voices_dir / configured / "manifest.yaml").is_file():
                _set_local(cfg.local_path, "audio.voice_pack", name)
                cfg.poll(immediate=True)
                selected = True
            emit({"kind": "voice_done", "name": name, "clips": result.clips,
                  "seconds": result.total_seconds, "voice": result.voice,
                  "selected": selected})
            return result

        return _start("voice", job, f"generating voice '{name}'")

    @app.post("/api/voice/say")
    async def voice_say(body: dict) -> dict:
        from ..runtime.player import BeepBank, make_player
        from ..voice.pack import load_configured_bank

        tokens = [t for t in str(body.get("text", "")).split() if t]
        if not tokens:
            raise HTTPException(400, "nothing to say")
        pack = str(body.get("pack") or "").strip() or None
        if pack:
            _file_under(voices_dir, pack, "voice pack")  # validation only; the loader builds the path

        def speak() -> float:
            snapshot = dict(cfg.data["audio"])
            if pack:
                cfg.data["audio"]["voice_pack"] = pack
            try:
                beeps = BeepBank(samplerate=cfg.get("audio.samplerate"),
                                 base_clip_s=cfg.get("audio.placeholder_clip_s"),
                                 crossfade_s=cfg.get("audio.crossfade_ms") / 1000.0)
                bank = load_configured_bank(cfg, beeps)
            finally:
                cfg.data["audio"] = snapshot
            duration = bank.duration(tokens)
            player = make_player(cfg.get("audio.samplerate"), cfg.get("audio.blocksize"),
                                 cfg.get("audio.device"), cfg.get("audio.gain_db"))
            try:
                player.play(bank.render(tokens))
                time.sleep(duration + 0.2)
            finally:
                player.close()
            return duration

        duration = await asyncio.get_running_loop().run_in_executor(None, speak)
        return {"ok": True, "duration_s": duration}

    # -- community stages ----------------------------------------------------
    # A separate, public GitHub repo holds shared stage files plus an
    # index.json. Reading it needs no account; sharing goes through GitHub's
    # upload page, which turns a drag and drop into a pull request.

    app.state.fetch = _http_get  # tests swap this for a fake
    app.state.post_json = _http_post_json

    def _community() -> tuple[str, str] | None:
        repo = str(cfg.get("community.repo", "") or "").strip()
        return (repo, str(cfg.get("community.branch", "main") or "main")) if repo else None

    @app.get("/api/community")
    async def community_list() -> dict:
        target = _community()
        if target is None:
            return {"available": False, "reason": "no community repo configured (community.repo)"}
        repo, branch = target
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/index.json"
        try:
            raw = await asyncio.get_running_loop().run_in_executor(None, lambda: app.state.fetch(url))
            data = json.loads(raw)
        except Exception as exc:
            return {"available": False, "repo": repo, "url": f"https://github.com/{repo}",
                    "reason": f"could not load the community index ({exc})"}
        installed = {p.stem for p in stages_dir.glob("*.json")}
        stages = []
        for s in data.get("stages", []):
            file = str(s.get("file", ""))
            if not _SAFE_FILE.match(file):
                continue
            stages.append({**s, "installed": file[:-5] in installed})
        return {"available": True, "repo": repo, "url": f"https://github.com/{repo}", "stages": stages}

    @app.get("/api/community/preview/{file}")
    async def community_preview(file: str) -> dict:
        """A shared stage's map and notes without installing it."""
        from ..stage.schema import StageError, from_dict

        target = _community()
        if target is None:
            raise HTTPException(400, "no community repo configured")
        repo, branch = target
        if not _SAFE_FILE.match(file):
            raise HTTPException(400, f"not a stage file name: {file!r}")
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/stages/{file}"
        try:
            raw = await asyncio.get_running_loop().run_in_executor(None, lambda: app.state.fetch(url))
            data = json.loads(raw)
            stage = from_dict(data)
        except StageError as exc:
            raise HTTPException(502, f"the shared file is not a valid stage: {exc}") from exc
        except Exception as exc:
            raise HTTPException(502, f"download failed: {exc}") from exc
        return {
            **_stage_detail_dict(stage),
            "runs": [],
            "file": file,
            "installed": (stages_dir / file).is_file(),
            "community": _clean_community(data.get("community")),
        }

    @app.post("/api/community/install")
    async def community_install(body: dict) -> dict:
        from ..stage.schema import StageError, from_dict, save

        target = _community()
        if target is None:
            raise HTTPException(400, "no community repo configured")
        repo, branch = target
        file = str(body.get("file", ""))
        if not _SAFE_FILE.match(file):
            raise HTTPException(400, f"not a stage file name: {file!r}")
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/stages/{file}"
        try:
            raw = await asyncio.get_running_loop().run_in_executor(None, lambda: app.state.fetch(url))
            stage = from_dict(json.loads(raw))
        except StageError as exc:
            raise HTTPException(502, f"the shared file is not a valid stage: {exc}") from exc
        except Exception as exc:
            raise HTTPException(502, f"download failed: {exc}") from exc
        dest = _file_under(stages_dir, file, "stage")
        if dest.exists() and not body.get("overwrite"):
            raise HTTPException(409, f"you already have a stage named {file[:-5]}")
        stage.generator = {**stage.generator, "installed_from": f"{repo}/stages/{file}"}
        stage.name = file[:-5]  # the file name is the name, whatever the JSON said inside
        save(stage, dest)
        return {"ok": True, "name": stage.name, "file": file}

    @app.post("/api/stages/{name}/share")
    async def stage_share(name: str, body: dict | None = None) -> dict:
        import webbrowser

        from ..stage.schema import load, to_dict

        path = _stage_path(stages_dir, name)
        if not path.is_file():
            raise HTTPException(404, f"no stage {name}")
        target = _community()
        if target is None:
            raise HTTPException(400, "no community repo configured")
        repo, branch = target
        st = load(path)
        if not st.notes:
            raise HTTPException(400, "this stage has no notes; nothing worth sharing yet")
        data = to_dict(st)
        # Nothing personal goes out: the stage is geometry and notes. The
        # recording itself stays on this PC; only its hash travels, so the
        # same recon is recognisable if shared twice.
        data["community"] = {
            "race": st.name,
            "author": str((body or {}).get("author", "")).strip()[:60],
            "tool_version": __import__("codriver").__version__,
            "shared_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        share_dir = stages_dir / "share"
        share_dir.mkdir(parents=True, exist_ok=True)
        out = share_dir / f"{name}.json"
        out.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")

        # One click when a relay is configured: it opens the pull request for
        # the player (relay/README.md). If it is down or refuses, fall back to
        # the upload page and say why, rather than failing the share.
        relay = str(cfg.get("community.relay_url", "") or "").strip()
        secret = str(cfg.get("community.relay_secret", "") or "").strip()
        relay_error = None
        if relay:
            try:
                reply = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: app.state.post_json(
                        relay.rstrip("/") + "/share",
                        {"file": f"{name}.json", "stage": data},
                        60.0,
                        {"X-Codriver-Secret": secret} if secret else None,
                    ),
                )
                if not isinstance(reply, dict) or not reply.get("ok") or not reply.get("pr_url"):
                    raise RuntimeError((reply or {}).get("error") if isinstance(reply, dict) else "relay gave no answer")
                return {
                    "ok": True, "via": "relay", "path": str(out),
                    "pr_url": str(reply["pr_url"]), "updated": bool(reply.get("updated")),
                }
            except Exception as exc:
                relay_error = str(exc)
                logging.getLogger(__name__).warning(
                    "community relay failed, falling back to the upload page: %s", exc
                )
        upload_url = f"https://github.com/{repo}/upload/{branch}/stages"
        try:
            import os

            os.startfile(share_dir)  # type: ignore[attr-defined]  # Windows: open the folder
        except Exception:
            pass
        try:
            webbrowser.open(upload_url)
        except Exception:
            pass
        return {"ok": True, "via": "manual", "path": str(out), "upload_url": upload_url,
                "relay_error": relay_error}

    # -- websocket -----------------------------------------------------------

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        sockets.add(websocket)
        try:
            for event in list(jobs.history)[-40:]:
                await websocket.send_text(json.dumps(event, default=str))
            while True:
                await websocket.receive_text()  # keepalive pings from the client
        except WebSocketDisconnect:
            pass
        finally:
            sockets.discard(websocket)

    from ..stage.schema import StageError
    from fastapi.responses import JSONResponse

    @app.exception_handler(StageError)
    async def _bad_stage(_request, exc: StageError) -> JSONResponse:
        # A broken or foreign file is the request's problem, not the server's.
        return JSONResponse({"detail": f"not a usable stage file: {exc}"}, status_code=400)

    app.add_middleware(BrowserGuard)
    return app


def serve(cfg: Config, root: Path, host: str = "0.0.0.0", port: int = 8777, open_browser: bool = True) -> None:
    import threading
    import webbrowser

    import uvicorn

    app = create_app(cfg, root, port=port)
    url_local = f"http://127.0.0.1:{port}"
    print(f"codriver UI: {url_local}", flush=True)
    if host == "0.0.0.0":
        print(f"  on your phone (same WLAN): http://{lan_ip()}:{port}", flush=True)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url_local)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
