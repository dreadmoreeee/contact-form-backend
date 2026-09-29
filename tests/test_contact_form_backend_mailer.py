import pytest
from cfb_helpers import CONFIG, mail_env

from contact_form_backend import mailer
from contact_form_backend.config import loads_config
from contact_form_backend.mailer import MailSettings, build_message, render_subject, send_message

FORM = loads_config(CONFIG).forms["contact"]
VALUES = {
    "name": "H\u00e9l\u00e8ne Th\u00e9riault",
    "email": "helene@example.ca",
    "message": "Line one\nLine <two> & three",
}


def settings(**over):
    env = {
        "CFB_SMTP_HOST": "smtp.example.ca",
        "CFB_MAIL_FROM": "Website <forms@shop.example.ca>",
        "CFB_MAIL_TO": "a@shop.example.ca, b@shop.example.ca",
    }
    env.update(over)
    return MailSettings.from_env(env)


def test_settings_from_env_defaults():
    s = settings()
    assert (s.host, s.port, s.security, s.user) == ("smtp.example.ca", 587, "starttls", "")
    assert s.to == ("a@shop.example.ca", "b@shop.example.ca")
    assert s.sender_address == "forms@shop.example.ca"
    assert settings(CFB_SMTP_SECURITY="ssl").port == 465
    assert settings(CFB_SMTP_SECURITY="none", CFB_SMTP_PORT="2525").port == 2525
    assert MailSettings.from_env({}) is None


@pytest.mark.parametrize(
    "over, message",
    [
        ({"CFB_SMTP_SECURITY": "tls13"}, "CFB_SMTP_SECURITY"),
        ({"CFB_MAIL_FROM": ""}, "CFB_MAIL_FROM"),
        ({"CFB_MAIL_TO": "a@shop.example.ca, nope"}, "CFB_MAIL_TO"),
    ],
)
def test_settings_errors(over, message):
    with pytest.raises(ValueError, match=message):
        settings(**over)


def test_render_subject():
    assert render_subject("From {name} ({missing})", {"name": "Dana"}) == "From Dana ()"
    assert render_subject("{name}", {"name": "a\r\nBcc: x"}) == "a Bcc: x"
    assert render_subject("{name.__class__}", {"name": "x"}) == "{name.__class__}"
    assert len(render_subject("{m}", {"m": "x" * 500})) == 150


def test_build_message():
    msg = build_message(FORM, VALUES, settings(), ["a@shop.example.ca"], "2026-09-29T12:00:00Z")
    assert msg["Subject"] == "Website enquiry from H\u00e9l\u00e8ne Th\u00e9riault"
    assert msg["Reply-To"].addresses[0].addr_spec == "helene@example.ca"
    assert msg["Reply-To"].addresses[0].display_name == "H\u00e9l\u00e8ne Th\u00e9riault"
    assert msg["To"] == "a@shop.example.ca" and msg["X-Contact-Form"] == "contact"
    assert msg["Message-ID"].endswith("@shop.example.ca>")
    plain = msg.get_body(("plain",)).get_content()
    assert "Message:\nLine one\nLine <two> & three" in plain
    html = msg.get_body(("html",)).get_content()
    assert "Line &lt;two&gt; &amp; three" in html
    raw = msg.as_bytes()
    assert raw.isascii()  # encoded words / transfer encoding for non-ASCII


def test_no_reply_to_without_email():
    msg = build_message(FORM, {"name": "x", "message": "y"}, settings(), ["a@x.ca"], "now")
    assert msg["Reply-To"] is None


def test_send_via_local_sink_with_auth(smtp_sink):
    env = mail_env(smtp_sink, CFB_SMTP_USER="user", CFB_SMTP_PASSWORD="pw")
    s = MailSettings.from_env(env)
    msg = build_message(FORM, VALUES, s, list(s.to), "now")
    send_message(msg, s, list(s.to))
    got = smtp_sink.messages[0]
    assert got.rcpt_to == ["owner@shop.example.ca"]
    assert got.message["Subject"] == "Website enquiry from H\u00e9l\u00e8ne Th\u00e9riault"


class FakeSMTP:
    calls = []

    def __init__(self, host, port, local_hostname=None, timeout=None, context=None):
        FakeSMTP.calls.append(("connect", type(self).__name__, host, port, context is not None))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        FakeSMTP.calls.append(("quit",))

    def ehlo(self):
        FakeSMTP.calls.append(("ehlo",))

    def starttls(self, context=None):
        FakeSMTP.calls.append(("starttls", context is not None))

    def login(self, user, password):
        FakeSMTP.calls.append(("login", user, password))

    def send_message(self, msg, from_addr=None, to_addrs=None):
        FakeSMTP.calls.append(("send", from_addr, tuple(to_addrs)))


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.calls = []
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP.calls


def test_starttls_and_login(fake_smtp):
    s = settings(CFB_SMTP_USER="u", CFB_SMTP_PASSWORD="p")
    send_message(build_message(FORM, VALUES, s, ["a@x.ca"], "now"), s, ["a@x.ca"])
    assert fake_smtp == [
        ("connect", "FakeSMTP", "smtp.example.ca", 587, False),
        ("ehlo",), ("starttls", True), ("ehlo",), ("login", "u", "p"),
        ("send", "forms@shop.example.ca", ("a@x.ca",)), ("quit",),
    ]


def test_implicit_ssl(fake_smtp):
    s = settings(CFB_SMTP_SECURITY="ssl")
    send_message(build_message(FORM, VALUES, s, ["a@x.ca"], "now"), s, ["a@x.ca"])
    assert fake_smtp[0] == ("connect", "FakeSMTPSSL", "smtp.example.ca", 465, True)
    assert not any(c[0] in ("starttls", "login") for c in fake_smtp)
