import re
from pathlib import Path

import pytest
from cfb_helpers import CONFIG

from contact_form_backend.cli import main
from contact_form_backend.config import ConfigError, load_config, loads_config

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_parse_config():
    cfg = loads_config(CONFIG)
    form = cfg.forms["contact"]
    assert [f.name for f in form.fields] == ["name", "email", "phone", "message"]
    assert form.token == "required" and form.honeypot == "website"
    assert form.reply_field().name == "email"
    assert cfg.max_body_bytes == 65536 and cfg.sqlite == "" and cfg.log_ip is False


def test_example_config_is_valid():
    cfg = load_config(str(EXAMPLES / "forms.toml"))
    assert "contact" in cfg.forms


@pytest.mark.parametrize(
    "extra, message",
    [
        ('colour = "blue"', "unknown key 'colour'"),
        ('token = "maybe"', "token must be one of"),
        ('honeypot = "email"', "clashes with a real field"),
        ("min_age = 10\nmax_age = 5", "min_age < max_age"),
        ('allowed_origins = ["https://shop.example.ca/"]', "no trailing /"),
        ('webhook = "/relative"', "absolute http(s) URL"),
        ('success_url = "javascript:alert(1)"', "must start with"),
        ('reply_to_field = "name"', "reply_to_field must name an email field"),
        ("rate_limit = true", "'rate_limit' must be int"),
        ('to = "a@b.ca"', "must be a list of strings"),
    ],
)
def test_bad_form_settings(extra, message):
    key = extra.split(" ", 1)[0]
    text = re.sub(r"(?m)^%s = .*$" % key, "", CONFIG)
    text = text.replace("[forms.contact]\n", "[forms.contact]\n" + extra + "\n", 1)
    with pytest.raises(ConfigError, match=re.escape(message)):
        loads_config(text)


def test_bad_top_level():
    with pytest.raises(ConfigError, match="define at least one form"):
        loads_config("[server]\ntrust_proxy = true\n")
    with pytest.raises(ConfigError, match="unknown top-level"):
        loads_config("[smtp]\nhost = 'x'\n" + CONFIG)
    with pytest.raises(ConfigError, match="form id"):
        loads_config('[forms."a b"]\n[[forms."a b".fields]]\nname = "x"\n')
    with pytest.raises(ConfigError, match="at least one"):
        loads_config("[forms.contact]\nsubject = 'x'\n")
    with pytest.raises(ConfigError, match="type must be one of"):
        loads_config('[forms.c]\n[[forms.c.fields]]\nname = "x"\ntype = "date"\n')
    with pytest.raises(ConfigError, match="duplicate"):
        loads_config('[forms.c]\n[[forms.c.fields]]\nname = "x"\n[[forms.c.fields]]\nname = "x"\n')
    with pytest.raises(ConfigError):
        loads_config("not toml ===")


def test_cli_check(capsys):
    assert main(["--config", str(EXAMPLES / "forms.toml"), "--check"]) == 0
    out = capsys.readouterr().out
    assert "POST /f/contact" in out and "email*:email" in out and "config OK" in out


def test_cli_missing_config(capsys, tmp_path):
    assert main(["--config", str(tmp_path / "missing.toml"), "--check"]) == 2
    assert "config error" in capsys.readouterr().err
