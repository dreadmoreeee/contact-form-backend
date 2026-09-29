"""Shared test data and helpers (imported by conftest and the tests)."""

import socket
import time

FORBIDDEN_PORTS = {8765, 8737, 8787}

# "Vam perevod 193155 rub. zabrat tut https://5d58cc63.sslip.io/x" in Cyrillic:
# the real spam message from form-spam-guard's tests.
SPAM = (
    "\u0412\u0430\u043c \u043f\u0435\u0440\u0435\u0432\u043e\u0434 193155 "
    "\u0440\u0443\u0431. \u0437\u0430\u0431\u0440\u0430\u0442\u044c \u0442\u0443\u0442 "
    "https://5d58cc63.sslip.io/x"
)

CONFIG = """
[forms.contact]
subject = "Website enquiry from {name}"
allowed_origins = ["https://shop.example.ca"]
success_url = "https://shop.example.ca/thanks/"
error_url = "https://shop.example.ca/contact/?sent=0"
allowed_domains = ["shop.example.ca"]
rate_limit = 5

[[forms.contact.fields]]
name = "name"
required = true
max_length = 100

[[forms.contact.fields]]
name = "email"
type = "email"
required = true

[[forms.contact.fields]]
name = "phone"
type = "phone"

[[forms.contact.fields]]
name = "message"
type = "textarea"
required = true
max_length = 2000
"""


def free_port() -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in FORBIDDEN_PORTS:
            return port


class Clock:
    def __init__(self, start: float = 1_790_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def mail_env(sink, **extra):
    env = {
        "CFB_SECRET": "test-secret-0123456789abcdef",
        "CFB_SMTP_HOST": "127.0.0.1",
        "CFB_SMTP_PORT": str(sink.port),
        "CFB_SMTP_SECURITY": "none",
        "CFB_MAIL_FROM": "Website <forms@shop.example.ca>",
        "CFB_MAIL_TO": "owner@shop.example.ca",
    }
    env.update(extra)
    return env


def good_fields(**over):
    fields = {
        "name": "Dana LeBlanc",
        "email": "dana@example.ca",
        "phone": "(506) 555-0142",
        "message": "Hi, we run a bakery in Miramichi and would like a quote for a new website.",
    }
    fields.update(over)
    return fields


def get_token(client, clock, form="contact", age=10):
    resp = client.get("/token", params={"form": form})
    assert resp.status_code == 200
    token = resp.json()["token"]
    clock.advance(age)
    return token
