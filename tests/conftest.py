import email
import email.policy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, Path(__file__).resolve().parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:
    import form_spam_guard  # noqa: F401
except ImportError:
    # a form-spam-guard checkout next to this repository
    for guard_checkout in (ROOT.parent / "form-spam-guard", ROOT.parent.parent / "form-spam-guard"):
        if (guard_checkout / "form_spam_guard").is_dir():
            sys.path.insert(0, str(guard_checkout))
            break

from cfb_helpers import CONFIG, FORBIDDEN_PORTS, Clock, free_port, mail_env  # noqa: E402
from contact_form_backend.devsmtp import SMTPSink  # noqa: E402


class _Msg:
    def __init__(self, mail_from, rcpt_to, data):
        self.mail_from = mail_from
        self.rcpt_to = list(rcpt_to)
        self.data = data

    @property
    def message(self):
        return email.message_from_bytes(self.data, policy=email.policy.default)


class _AiosmtpdSink:
    """Same interface as SMTPSink, backed by aiosmtpd when it is installed."""

    def __init__(self):
        from aiosmtpd.controller import Controller
        from aiosmtpd.smtp import AuthResult

        self._messages = []
        sink = self

        class Handler:
            async def handle_DATA(self, server, session, envelope):
                sink._messages.append(
                    _Msg(envelope.mail_from, envelope.rcpt_tos, envelope.original_content or envelope.content)
                )
                return "250 OK"

        def authenticator(server, session, envelope, mechanism, auth_data):
            return AuthResult(success=True)

        self.host = "127.0.0.1"
        self.port = free_port()
        self._controller = Controller(
            Handler(), hostname=self.host, port=self.port,
            authenticator=authenticator, auth_require_tls=False,
        )

    @property
    def messages(self):
        return list(self._messages)

    def start(self):
        self._controller.start()
        return self

    def stop(self):
        self._controller.stop()


def _make_sink():
    try:
        import aiosmtpd  # noqa: F401
    except ImportError:
        return SMTPSink()
    return _AiosmtpdSink()


@pytest.fixture
def smtp_sink():
    sink = _make_sink()
    assert sink.port not in FORBIDDEN_PORTS
    sink.start()
    try:
        yield sink
    finally:
        sink.stop()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make_client(smtp_sink, clock):
    from fastapi.testclient import TestClient

    from contact_form_backend import create_app, loads_config

    clients = []

    def factory(config_text=CONFIG, env=None, **env_extra):
        cfg = loads_config(config_text)
        app = create_app(cfg, env=env if env is not None else mail_env(smtp_sink, **env_extra), clock=clock)
        client = TestClient(app, raise_server_exceptions=True)
        clients.append(client)
        return client

    yield factory
    for c in clients:
        c.close()
