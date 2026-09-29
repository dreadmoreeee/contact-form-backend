import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from cfb_helpers import CONFIG, SPAM, FORBIDDEN_PORTS, good_fields, get_token

JSON = {"Accept": "application/json"}
ORIGIN = {"Origin": "https://shop.example.ca"}


def post_json(client, fields, token, form="contact", headers=None):
    body = dict(fields)
    if token is not None:
        body["fsg_token"] = token
    return client.post("/f/" + form, json=body, headers={**JSON, **(headers or {})})


def test_health(make_client):
    client = make_client()
    assert client.get("/health").json() == {"status": "ok", "forms": 1}


def test_token_endpoint(make_client):
    client = make_client()
    resp = client.get("/token", params={"form": "contact"})
    body = resp.json()
    assert resp.status_code == 200
    assert body["field"] == "fsg_token" and body["honeypot"] == "website"
    assert body["min_age"] == 3 and "." in body["token"]
    assert resp.headers["cache-control"] == "no-store"
    assert client.get("/token", params={"form": "nope"}).status_code == 404


def test_real_message_is_emailed(make_client, smtp_sink, clock):
    client = make_client()
    token = get_token(client, clock)
    resp = post_json(client, good_fields(), token)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "message": "Thank you, your message has been sent."}
    assert len(smtp_sink.messages) == 1
    got = smtp_sink.messages[0]
    msg = got.message
    assert got.rcpt_to == ["owner@shop.example.ca"]
    assert got.mail_from == "forms@shop.example.ca"
    assert msg["Subject"] == "Website enquiry from Dana LeBlanc"
    assert msg["Reply-To"] == "Dana LeBlanc <dana@example.ca>"
    assert msg["From"] == "Website <forms@shop.example.ca>"
    assert msg.get_content_type() == "multipart/alternative"
    plain = msg.get_body(("plain",)).get_content()
    assert "Phone: (506) 555-0142" in plain and "bakery in Miramichi" in plain


def test_no_js_urlencoded_post_redirects(make_client, smtp_sink, clock):
    client = make_client()
    token = get_token(client, clock)
    resp = client.post(
        "/f/contact", data={**good_fields(), "fsg_token": token, "website": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "https://shop.example.ca/thanks/"
    assert len(smtp_sink.messages) == 1


def test_multipart_post(make_client, smtp_sink, clock):
    client = make_client()
    token = get_token(client, clock)
    resp = client.post(
        "/f/contact", files={k: (None, v) for k, v in {**good_fields(), "fsg_token": token}.items()},
        headers=JSON,
    )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert len(smtp_sink.messages) == 1


def test_multipart_file_upload_is_rejected(make_client, smtp_sink, clock):
    client = make_client()
    token = get_token(client, clock)
    resp = client.post(
        "/f/contact", data={**good_fields(), "fsg_token": token},
        files={"attachment": ("a.txt", b"hello", "text/plain")}, headers=JSON,
    )
    assert resp.status_code == 400 and resp.json()["error"] == "bad_multipart"
    assert smtp_sink.messages == []


def test_html_thank_you_page_when_no_success_url(make_client, clock):
    client = make_client(CONFIG.replace('success_url = "https://shop.example.ca/thanks/"\n', ""))
    token = get_token(client, clock)
    resp = client.post("/f/contact", data={**good_fields(), "fsg_token": token})
    assert resp.status_code == 200
    assert "<h1>Thank you</h1>" in resp.text


@pytest.mark.parametrize(
    "case",
    ["honeypot", "too_fast", "spam", "latin_spam", "no_token", "bad_token", "other_form_token"],
)
def test_bots_get_the_normal_thank_you(make_client, smtp_sink, clock, caplog, case):
    client = make_client()
    real = post_json(client, good_fields(), get_token(client, clock))
    fields, token = good_fields(), get_token(client, clock)
    expected = {
        "honeypot": "honeypot_filled", "too_fast": "too_fast", "spam": "foreign_script",
        "latin_spam": "blocked_link_host", "no_token": "missing_token",
        "bad_token": "bad_signature", "other_form_token": "bad_signature",
    }[case]
    if case == "honeypot":
        fields["website"] = "http://cheap-seo.example"
    elif case == "too_fast":
        token = client.get("/token", params={"form": "contact"}).json()["token"]
        clock.advance(0.5)
    elif case == "spam":
        fields["message"] = SPAM
    elif case == "latin_spam":
        fields["message"] = "Vam perevod 193155 rub. zabrat tut https://5d58cc63.sslip.io/x"
    elif case == "no_token":
        token = None
    elif case == "bad_token":
        token = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    elif case == "other_form_token":
        from form_spam_guard import TokenSigner

        token = TokenSigner("test-secret-0123456789abcdef").issue("newsletter")
        clock.advance(10)
    caplog.set_level("INFO", logger="contact_form_backend")
    bot = post_json(client, fields, token)
    assert bot.status_code == real.status_code == 200
    assert bot.json() == real.json()
    assert len(smtp_sink.messages) == 1  # only the real one
    assert "outcome=dropped reason=%s" % expected in caplog.text


def test_bot_redirect_is_identical_for_no_js(make_client, smtp_sink, clock):
    client = make_client()
    resp = client.post(
        "/f/contact", data={**good_fields(), "website": "spam", "fsg_token": get_token(client, clock)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "https://shop.example.ca/thanks/"
    assert smtp_sink.messages == []


def test_replayed_token_is_dropped(make_client, smtp_sink, clock):
    client = make_client()
    token = get_token(client, clock)
    assert post_json(client, good_fields(), token).json()["ok"] is True
    assert post_json(client, good_fields(), token).json()["ok"] is True
    assert len(smtp_sink.messages) == 1


def test_expired_token_is_dropped(make_client, smtp_sink, clock):
    client = make_client()
    token = get_token(client, clock, age=4 * 3600 + 1)
    assert post_json(client, good_fields(), token).status_code == 200
    assert smtp_sink.messages == []


def test_lenient_mode_accepts_missing_token_only(make_client, smtp_sink, clock):
    client = make_client(CONFIG.replace("rate_limit = 5", 'rate_limit = 5\ntoken = "lenient"'))
    assert post_json(client, good_fields(), None).status_code == 200
    assert len(smtp_sink.messages) == 1
    # a present but too-young token is still judged
    token = client.get("/token", params={"form": "contact"}).json()["token"]
    post_json(client, good_fields(), token)
    # honeypot still applies without a token
    post_json(client, good_fields(website="x"), None)
    assert len(smtp_sink.messages) == 1


def test_token_off(make_client, smtp_sink):
    client = make_client(CONFIG.replace("rate_limit = 5", 'rate_limit = 5\ntoken = "off"'))
    assert post_json(client, good_fields(), None).json()["ok"] is True
    assert len(smtp_sink.messages) == 1


def test_rate_limit(make_client, smtp_sink, clock):
    client = make_client()
    for _ in range(7):
        post_json(client, good_fields(), get_token(client, clock))
    assert len(smtp_sink.messages) == 5


def test_validation_errors_json(make_client, smtp_sink, clock):
    client = make_client()
    token = get_token(client, clock)
    resp = post_json(client, good_fields(email="not-an-email", phone="call me", name=""), token)
    assert resp.status_code == 400
    assert resp.json() == {
        "ok": False, "error": "invalid_fields",
        "fields": {"name": "required", "email": "invalid_email", "phone": "invalid_phone"},
    }
    # the token was not consumed: fixing the form and resending works
    assert post_json(client, good_fields(), token).json()["ok"] is True
    assert len(smtp_sink.messages) == 1


def test_validation_errors_redirect_for_no_js(make_client, clock):
    client = make_client()
    resp = client.post(
        "/f/contact", data={**good_fields(email="x"), "fsg_token": get_token(client, clock)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == (
        "https://shop.example.ca/contact/?sent=0&error=invalid_fields&fields=email"
    )


def test_validation_error_page_is_escaped(make_client, clock):
    client = make_client(CONFIG.replace('error_url = "https://shop.example.ca/contact/?sent=0"\n', ""))
    resp = client.post("/f/contact", data={"name": "<script>x</script>"})
    assert resp.status_code == 400
    assert "<script>" not in resp.text and "Email (required)" in resp.text


def test_invalid_but_honeypot_filled_is_silent(make_client, clock):
    client = make_client()
    resp = post_json(client, {"website": "http://x.example", "email": "bad"}, None)
    assert resp.status_code == 200 and resp.json()["ok"] is True


def test_too_long_field(make_client, clock):
    client = make_client()
    resp = post_json(client, good_fields(message="a" * 2001), get_token(client, clock))
    assert resp.json()["fields"] == {"message": "too_long"}


def test_unknown_form_and_unsupported_type(make_client):
    client = make_client()
    assert client.post("/f/nope", json={}).status_code == 404
    resp = client.post("/f/contact", content=b"hello", headers={"Content-Type": "text/plain", **JSON})
    assert resp.status_code == 415


def test_body_too_large(make_client, smtp_sink):
    client = make_client()
    resp = client.post("/f/contact", json={"message": "a" * 70000}, headers=JSON)
    assert resp.status_code == 413
    assert smtp_sink.messages == []


def test_too_many_fields(make_client):
    client = make_client()
    body = "&".join("f%d=x" % i for i in range(201))
    resp = client.post("/f/contact", content=body, headers={
        "Content-Type": "application/x-www-form-urlencoded", **JSON})
    assert resp.status_code == 400 and resp.json()["error"] == "too_many_fields"


def test_bad_json(make_client):
    client = make_client()
    resp = client.post("/f/contact", content=b"[1,2]", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400 and resp.json()["error"] == "bad_json"


def test_extra_fields_are_not_delivered(make_client, smtp_sink, clock):
    client = make_client()
    post_json(client, good_fields(secret_admin="1", message="Hello there, quote please."),
              get_token(client, clock))
    msg = smtp_sink.messages[0].message
    assert "secret_admin" not in msg.get_body(("plain",)).get_content()


def test_html_part_is_escaped(make_client, smtp_sink, clock):
    client = make_client()
    evil = '<img src=x onerror="alert(1)"> & <b>bold</b>'
    post_json(client, good_fields(message="Question: " + evil), get_token(client, clock))
    html = smtp_sink.messages[0].message.get_body(("html",)).get_content()
    assert "<img" not in html and "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
    assert "&amp;" in html and "&lt;b&gt;bold&lt;/b&gt;" in html


def test_header_injection_in_name_is_harmless(make_client, smtp_sink, clock):
    client = make_client()
    name = "Dana\r\nBcc: victim@example.com"
    post_json(client, good_fields(name=name), get_token(client, clock))
    got = smtp_sink.messages[0]
    assert got.rcpt_to == ["owner@shop.example.ca"]
    assert got.message["Bcc"] is None
    assert got.message["Subject"] == "Website enquiry from Dana Bcc: victim@example.com"


def test_cors_allowed_origin(make_client, clock):
    client = make_client()
    resp = client.get("/token", params={"form": "contact"}, headers=ORIGIN)
    assert resp.headers["access-control-allow-origin"] == "https://shop.example.ca"
    clock.advance(10)
    resp = post_json(client, good_fields(), resp.json()["token"], headers=ORIGIN)
    assert resp.headers["access-control-allow-origin"] == "https://shop.example.ca"
    pre = client.options("/f/contact", headers={**ORIGIN, "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 204
    assert "POST" in pre.headers["access-control-allow-methods"]


def test_cors_other_origin_is_refused(make_client, smtp_sink, clock):
    client = make_client()
    evil = {"Origin": "https://evil.example"}
    assert client.get("/token", params={"form": "contact"}, headers=evil).status_code == 403
    assert client.options("/f/contact", headers=evil).status_code == 403
    resp = post_json(client, good_fields(), get_token(client, clock), headers=evil)
    assert resp.status_code == 403 and "access-control-allow-origin" not in resp.headers
    assert smtp_sink.messages == []


def test_smtp_failure_reports_error(make_client, smtp_sink, clock, caplog):
    from cfb_helpers import free_port

    dead = free_port()
    assert dead not in FORBIDDEN_PORTS
    client = make_client(CFB_SMTP_PORT=str(dead))
    resp = post_json(client, good_fields(), get_token(client, clock))
    assert resp.status_code == 502 and resp.json() == {"ok": False, "error": "delivery_failed"}
    resp = client.post(
        "/f/contact", data={**good_fields(), "fsg_token": get_token(client, clock)},
        follow_redirects=False,
    )
    assert resp.headers["location"].endswith("error=delivery_failed")
    assert "email failed" in caplog.text


class _Hook(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        _Hook.received.append((dict(self.headers), self.rfile.read(length)))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def hook_server():
    _Hook.received = []
    server = HTTPServer(("127.0.0.1", 0), _Hook)
    assert server.server_address[1] not in FORBIDDEN_PORTS
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d/hook" % server.server_address[1]
    server.shutdown()
    server.server_close()


def test_webhook_receives_signed_json(make_client, smtp_sink, clock, hook_server):
    import hashlib
    import hmac

    cfg = CONFIG.replace("rate_limit = 5", 'rate_limit = 5\nwebhook = "%s"' % hook_server)
    client = make_client(cfg, CFB_WEBHOOK_SECRET="hook-secret")
    assert post_json(client, good_fields(), get_token(client, clock)).json()["ok"] is True
    headers, body = _Hook.received[0]
    payload = json.loads(body)
    assert payload["form"] == "contact" and payload["fields"]["email"] == "dana@example.ca"
    expected = hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()
    assert headers["X-Contact-Form-Signature"] == "sha256=" + expected
    assert len(smtp_sink.messages) == 1


def test_webhook_only_delivery(make_client, smtp_sink, clock, hook_server):
    cfg = CONFIG.replace("rate_limit = 5", 'rate_limit = 5\nemail = false\nwebhook = "%s"' % hook_server)
    client = make_client(cfg)
    assert post_json(client, good_fields(), get_token(client, clock)).json()["ok"] is True
    assert smtp_sink.messages == [] and len(_Hook.received) == 1


def test_no_channel_is_an_error(make_client, clock):
    client = make_client(env={"CFB_SECRET": "test-secret-0123456789abcdef"})
    resp = post_json(client, good_fields(), get_token(client, clock))
    assert resp.status_code == 502


def test_jsonl_log_has_no_message_body(make_client, clock, tmp_path):
    path = tmp_path / "log.jsonl"
    client = make_client('[log]\njsonl = "%s"\n' % path.as_posix() + CONFIG)
    post_json(client, good_fields(), get_token(client, clock))
    post_json(client, good_fields(message=SPAM), get_token(client, clock))
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert [(x["outcome"], x["reason"]) for x in lines] == [
        ("delivered", "ok"), ("dropped", "foreign_script"),
    ]
    assert lines[0]["delivery"] == {"email": "sent"}
    assert "ip" not in lines[0]
    text = path.read_text(encoding="utf-8")
    assert "Miramichi" not in text and "dana@" not in text


def test_sqlite_log_and_retention(make_client, clock, tmp_path):
    path = tmp_path / "subs.sqlite3"
    cfg = '[log]\nsqlite = "%s"\nretention_days = 7\nlog_ip = true\n' % path.as_posix() + CONFIG
    client = make_client(cfg)
    post_json(client, good_fields(), get_token(client, clock))
    post_json(client, good_fields(website="x"), get_token(client, clock))
    log = client.app.state.sqlite_log
    rows = log.rows()
    assert [(r["outcome"], r["reason"]) for r in rows] == [
        ("delivered", "ok"), ("dropped", "honeypot_filled"),
    ]
    assert rows[0]["data"]["name"] == "Dana LeBlanc" and rows[0]["ip"] == "testclient"
    clock.advance(8 * 86400)
    assert log.purge() == 2
    assert log.rows() == []


def test_fields_fragment_for_server_side_include(make_client, clock):
    client = make_client()
    resp = client.get("/f/contact/fields")
    assert resp.status_code == 200
    assert 'name="website"' in resp.text and 'name="fsg_token"' in resp.text
    token = resp.text.split('name="fsg_token" value="')[1].split('"')[0]
    clock.advance(10)
    assert post_json(client, good_fields(), token).json()["ok"] is True


def test_trust_proxy_uses_last_forwarded_for(make_client, smtp_sink, clock):
    client = make_client("[server]\ntrust_proxy = true\n" + CONFIG.replace("rate_limit = 5", "rate_limit = 1"))
    for ip in ("1.1.1.1", "2.2.2.2"):
        headers = {"X-Forwarded-For": "9.9.9.9, " + ip}
        post_json(client, good_fields(), get_token(client, clock), headers=headers)
    assert len(smtp_sink.messages) == 2
    post_json(client, good_fields(), get_token(client, clock), headers={"X-Forwarded-For": "2.2.2.2"})
    assert len(smtp_sink.messages) == 2
