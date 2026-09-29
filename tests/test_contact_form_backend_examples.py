import re
import subprocess
import sys
from pathlib import Path

from contact_form_backend.config import load_config

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def test_snippet_matches_example_config():
    cfg = load_config(str(EXAMPLES / "forms.toml")).forms["contact"]
    html = (EXAMPLES / "contact-form.html").read_text(encoding="utf-8")
    names = set(re.findall(r'name="([a-z_]+)"', html))
    assert {f.name for f in cfg.fields} | {cfg.honeypot, "fsg_token"} == names
    assert 'action="https://forms.example.ca/f/contact"' in html
    assert "/token?form=contact" in html
    assert 'tabindex="-1"' in html and 'autocomplete="off"' in html
    assert "Accept: \"application/json\"" in html


def test_env_example_has_no_inline_comments():
    for line in (EXAMPLES / "contact-form-backend.env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            assert "#" not in line, line


def test_every_source_file_is_ascii():
    for path in list(ROOT.rglob("*.py")) + list(ROOT.rglob("*.md")) + list(EXAMPLES.iterdir()):
        if path.is_file() and "__pycache__" not in path.parts:
            assert path.read_bytes().isascii(), path


def test_local_demo_runs_end_to_end():
    out = subprocess.run(
        [sys.executable, str(EXAMPLES / "demo_local.py")],
        capture_output=True, text=True, timeout=60, env={**__import__("os").environ, "PYTHONUTF8": "1"},
    )
    assert out.returncode == 0, out.stdout + out.stderr
    text = out.stdout
    for reason in ("foreign_script", "honeypot_filled", "too_fast"):
        assert "outcome=dropped reason=%s" % reason in text
    assert text.count("outcome=delivered") == 2
    assert "emails received by the local sink: 2" in text
    assert "Reply-To: Denise Arsenault <denise@example.com>" in text
