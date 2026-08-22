"""The community relay is JavaScript (a Cloudflare Worker). Its check script
runs under node with a fake GitHub; this wraps it into the suite so a change
to worker.js cannot silently break Share for everyone."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_relay_worker_opens_a_pull_request_the_way_the_app_expects():
    result = subprocess.run(
        [shutil.which("node"), str(ROOT / "tests" / "relay_worker_check.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "relay worker check: ok" in result.stdout


def test_worker_source_and_config_are_consistent():
    """The worker and its wrangler config must agree on the repo, and the
    worker must keep the same file-name rule the app uses, or a name the app
    accepts could be refused at the relay."""
    import re

    from codriver.ui.server import _SAFE_FILE

    worker = (ROOT / "relay" / "worker.js").read_text(encoding="utf-8")
    m = re.search(r"const SAFE_FILE = /(.+)/;", worker)
    assert m, "worker has no SAFE_FILE"
    assert m.group(1).replace("\\-", "-") == _SAFE_FILE.pattern.replace("\\-", "-")
    toml = (ROOT / "relay" / "wrangler.toml").read_text(encoding="utf-8")
    assert 'REPO = "W0nt3x/codriver-stages"' in toml
