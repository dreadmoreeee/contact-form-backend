"""Local end-to-end demo: real server, real HTTP, a local SMTP sink. Nothing leaves 127.0.0.1.

    python examples/demo_local.py

Starts the backend with uvicorn on a random free port, starts the dev SMTP
sink (which delivers nothing), then sends: a real-looking customer enquiry
(JavaScript path: token, then fetch), the same kind of message without
JavaScript (plain form post, lenient token mode), the real Cyrillic spam
sample with an sslip.io link, a bot that fills the honeypot, and a bot that
posts 0.2 s after getting its token. It prints the HTTP answers, the
server's decision log, the JSON-lines log and the headers of the emails that
reached the sink.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    import form_spam_guard  # noqa: F401

    EXTRA_PATH = []
except ImportError:  # a checkout next to this one
    # a form-spam-guard checkout next to this repository
    EXTRA_PATH = [str(p / "form-spam-guard") for p in (ROOT.parent, ROOT.parent.parent)
                  if (p / "form-spam-guard" / "form_spam_guard").is_dir()][:1]
    sys.path[:0] = EXTRA_PATH

from contact_form_backend.devsmtp import SMTPSink  # noqa: E402

FORBIDDEN = {8765, 8737, 8787}

CUSTOMER = {
    "name": "Denise Arsenault",
    "email": "denise@example.com",
    "phone": "(506) 555-0187",
    "message": (
        "Hi, I run a small hardware store on Water Street in Miramichi. Our website is "
        "ten years old and hard to use on phones. Could you send me a quote for a new "
        "site with our hours, a product catalogue and a contact page? Thanks, Denise"
    ),
}
NO_JS_CUSTOMER = {
    "name": "Luc Savoie",
    "email": "luc.savoie@example.ca",
    "message": "Bonjour, do you also build bilingual sites (English and French)? Merci!",
}
# The real spam message from form-spam-guard's tests:
# "Vam perevod 193155 rub. zabrat tut https://5d58cc63.sslip.io/x" in Cyrillic.
SPAM = {
    "name": "\u0410\u043d\u043d\u0430",
    "email": "anna.k91@example.com",
    "message": (
        "\u0412\u0430\u043c \u043f\u0435\u0440\u0435\u0432\u043e\u0434 193155 "
        "\u0440\u0443\u0431. \u0437\u0430\u0431\u0440\u0430\u0442\u044c \u0442\u0443\u0442 "
        "https://5d58cc63.sslip.io/x"
    ),
}
BOT = {
    "name": "John Smith",
    "email": "seo.expert@example.net",
    "website": "https://cheap-seo.example.net",
    "message": "Hello, I can bring your site to the first page of Google. Reply for prices.",
}


def free_port() -> int:
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in FORBIDDEN:
            return port


def request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read().decode("utf-8")
    result = (resp.status, resp.getheader("Location"), data)
    conn.close()
    return result


def token(port):
    return json.loads(request(port, "GET", "/token?form=contact")[2])["token"]


def post_json(port, fields, tok):
    body = json.dumps({**fields, "fsg_token": tok})
    return request(port, "POST", "/f/contact", body,
                   {"Content-Type": "application/json", "Accept": "application/json"})


def post_form(port, fields):
    return request(port, "POST", "/f/contact", urlencode(fields),
                   {"Content-Type": "application/x-www-form-urlencoded"})


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cfb-demo-"))
    jsonl = tmp / "submissions.jsonl"
    config = (ROOT / "examples" / "forms.toml").read_text(encoding="utf-8")
    config = config.replace('jsonl = ""', 'jsonl = "%s"' % jsonl.as_posix())
    (tmp / "forms.toml").write_text(config, encoding="utf-8")

    sink = SMTPSink().start()
    port = free_port()
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": os.pathsep.join([str(ROOT)] + EXTRA_PATH + [env.get("PYTHONPATH", "")]),
        "PYTHONUTF8": "1",
        "CFB_SECRET": "demo-only-secret-0123456789",
        "CFB_SMTP_HOST": "127.0.0.1",
        "CFB_SMTP_PORT": str(sink.port),
        "CFB_SMTP_SECURITY": "none",
        "CFB_MAIL_FROM": "DeMark Studio website <forms@example.ca>",
        "CFB_MAIL_TO": "hello@example.ca",
    })
    server = subprocess.Popen(
        [sys.executable, "-m", "contact_form_backend", "--config", str(tmp / "forms.toml"),
         "--host", "127.0.0.1", "--port", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        for _ in range(100):
            try:
                if request(port, "GET", "/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        print("server http://127.0.0.1:%d, SMTP sink 127.0.0.1:%d (local only)\n" % (port, sink.port))

        t_customer, t_spam, t_bot = token(port), token(port), token(port)
        time.sleep(3.5)  # a person takes longer than min_age (3 s) to fill in the form
        rows = [
            ("Customer (JS: token, fetch)", post_json(port, CUSTOMER, t_customer)),
            ("Customer without JavaScript", post_form(port, NO_JS_CUSTOMER)),
            ("Russian spam + sslip.io link", post_json(port, SPAM, t_spam)),
            ("Bot filling the honeypot", post_json(port, BOT, t_bot)),
        ]
        fast = token(port)
        time.sleep(0.2)
        rows.append(("Bot posting 0.2 s after token", post_json(port, CUSTOMER, fast)))

        print("%-30s  %-4s  %s" % ("submission", "HTTP", "answer"))
        print("%-30s  %-4s  %s" % ("-" * 30, "----", "-" * 40))
        for label, (status, location, body) in rows:
            print("%-30s  %-4d  %s" % (label, status, ("-> " + location) if location else body))
    finally:
        time.sleep(0.3)
        server.terminate()
        output, _ = server.communicate(timeout=10)
        sink.stop()

    print("\nserver log (decisions):")
    for line in output.splitlines():
        if " contact_form_backend: " in line or " form_spam_guard: " in line:
            print("  " + line.split(" ", 2)[2])  # drop the date and time
    print("\nJSON-lines log (no message bodies):")
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        print("  " + line)
    print("\nemails received by the local sink: %d" % len(sink.messages))
    for i, got in enumerate(sink.messages, 1):
        msg = got.message
        print("--- email %d  (envelope %s -> %s)" % (i, got.mail_from, ", ".join(got.rcpt_to)))
        for name in ("From", "To", "Reply-To", "Subject", "X-Contact-Form", "Content-Type"):
            print("  %s: %s" % (name, msg[name]))
        parts = [p.get_content_type() for p in msg.iter_parts()]
        print("  parts: %s" % ", ".join(parts))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
