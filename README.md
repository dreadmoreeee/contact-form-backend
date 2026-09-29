# contact-form-backend

A small **self-hosted contact form endpoint**: forms defined in one TOML file, messages delivered by **SMTP** (plain text + HTML, Reply-To set to the visitor) and/or a **webhook**, spam stopped **without a CAPTCHA** by [form-spam-guard](https://github.com/dreadmoreeee/form-spam-guard).

```
POST /f/contact            <- your HTML form (plain post or fetch)
GET  /token?form=contact   <- signed timestamp token for the form
GET  /health
```

Why another form backend:

- **Static sites still need a contact form.** Hosted form services mean a monthly fee, a third party holding your customers' messages, and often a CAPTCHA. This is one small FastAPI process next to your site.
- **Bots get "thank you".** Honeypot, signed timestamp token, content filter (foreign script, `sslip.io`/`ngrok`/shortener links, too many links) and a per-IP rate limit. A blocked submission gets exactly the response a real visitor gets, and nothing is sent; the reason goes to the log.
- **Works without JavaScript.** A plain `<form method="post">` gets a 303 redirect to your thank-you page; `fetch` with `Accept: application/json` gets JSON.
- **Stores nothing by default.** Optional JSON-lines log (metadata only, never message bodies) and optional SQLite log with a retention period.

## Measured result

`python examples/demo_local.py` starts the server with uvicorn on a random local port, a local SMTP sink that delivers nothing, and sends five submissions over real HTTP. Nothing leaves 127.0.0.1. The Russian spam text is the real sample form-spam-guard's tests are built on ("you have a transfer of 193155 rub, collect it here" pointing at an `sslip.io` host); the sender name and address around it, and both customers, are made up.

```
$ python examples/demo_local.py
server http://127.0.0.1:14113, SMTP sink 127.0.0.1:14112 (local only)

submission                      HTTP  answer
------------------------------  ----  ----------------------------------------
Customer (JS: token, fetch)     200   {"ok":true,"message":"Thank you, your message has been sent."}
Customer without JavaScript     303   -> https://example.ca/contact/thanks/
Russian spam + sslip.io link    200   {"ok":true,"message":"Thank you, your message has been sent."}
Bot filling the honeypot        200   {"ok":true,"message":"Thank you, your message has been sent."}
Bot posting 0.2 s after token   200   {"ok":true,"message":"Thank you, your message has been sent."}

server log (decisions):
  INFO contact_form_backend: form=contact outcome=delivered reason=ok check=guard
  INFO contact_form_backend: form=contact outcome=delivered reason=ok check=guard
  INFO form_spam_guard: form blocked: reason=foreign_script check=content key=127.0.0.1
  INFO contact_form_backend: form=contact outcome=dropped reason=foreign_script check=content
  INFO form_spam_guard: form blocked: reason=honeypot_filled check=honeypot key=127.0.0.1
  INFO contact_form_backend: form=contact outcome=dropped reason=honeypot_filled check=honeypot
  INFO form_spam_guard: form blocked: reason=too_fast check=token key=127.0.0.1
  INFO contact_form_backend: form=contact outcome=dropped reason=too_fast check=token

JSON-lines log (no message bodies):
  {"ts": "2026-09-29T11:28:13Z", "form": "contact", "outcome": "delivered", "reason": "ok", "check": "guard", "fields": ["email", "message", "name", "phone"], "delivery": {"email": "sent"}}
  {"ts": "2026-09-29T11:28:13Z", "form": "contact", "outcome": "delivered", "reason": "ok", "check": "guard", "fields": ["email", "message", "name"], "delivery": {"email": "sent"}}
  {"ts": "2026-09-29T11:28:13Z", "form": "contact", "outcome": "dropped", "reason": "foreign_script", "check": "content", "fields": ["email", "message", "name"]}
  {"ts": "2026-09-29T11:28:13Z", "form": "contact", "outcome": "dropped", "reason": "honeypot_filled", "check": "honeypot", "fields": ["email", "message", "name"]}
  {"ts": "2026-09-29T11:28:13Z", "form": "contact", "outcome": "dropped", "reason": "too_fast", "check": "token", "fields": ["email", "message", "name", "phone"]}

emails received by the local sink: 2
--- email 1  (envelope forms@example.ca -> hello@example.ca)
  From: DeMark Studio website <forms@example.ca>
  To: hello@example.ca
  Reply-To: Denise Arsenault <denise@example.com>
  Subject: Website enquiry from Denise Arsenault
  X-Contact-Form: contact
  Content-Type: multipart/alternative; boundary="===============0828945551072255813=="
  parts: text/plain, text/html
--- email 2  (envelope forms@example.ca -> hello@example.ca)
  From: DeMark Studio website <forms@example.ca>
  To: hello@example.ca
  Reply-To: Luc Savoie <luc.savoie@example.ca>
  Subject: Website enquiry from Luc Savoie
  X-Contact-Form: contact
  Content-Type: multipart/alternative; boundary="===============2606598122219670906=="
  parts: text/plain, text/html

$ python -m pytest -q -p no:cacheprovider --import-mode=importlib contact-form-backend
99 passed in 15.00s
```

The tests use FastAPI's `TestClient`, a local SMTP stub on a random port (aiosmtpd when installed) and a local HTTP server for the webhook. No test touches the network.

## Install

```
pip install .                    # needs git: form-spam-guard is installed from GitHub
contact-form-backend --config forms.toml --check     # validate the config
contact-form-backend --config forms.toml --host 127.0.0.1 --port 8081
```

Python 3.10+. Dependencies: FastAPI, uvicorn, python-multipart, form-spam-guard (stdlib only), and `tomli` on 3.10. [examples/deploy.md](examples/deploy.md) has a systemd unit, an nginx block and a Dockerfile.

## Configuration

Everything about forms lives in one TOML file ([examples/forms.toml](examples/forms.toml)); secrets live in environment variables. Unknown keys are errors, so typos do not pass silently.

```toml
[forms.contact]
subject = "Website enquiry from {name}"
allowed_origins = ["https://example.ca"]
success_url = "https://example.ca/contact/thanks/"
error_url = "https://example.ca/contact/?sent=0"
token = "lenient"
allowed_domains = ["example.ca"]

[[forms.contact.fields]]
name = "email"
type = "email"
required = true
```

`[forms.<id>]` (one table per form; the id is the URL: `POST /f/<id>`):

| Key | Default | Meaning |
|---|---|---|
| `fields` | required | `[[forms.<id>.fields]]` entries: `name`, `type` (`text`, `textarea`, `email`, `phone`), `required`, `max_length` (defaults 200 / 5000 / 254 / 40), `label` |
| `subject` | `New message from your website` | `{field}` placeholders; one line, max 150 characters |
| `to` | `CFB_MAIL_TO` | recipients for this form |
| `email` | `true` | send by SMTP (set `false` for webhook only) |
| `reply_to_field` | first `email` field | field whose validated address goes into Reply-To |
| `allowed_origins` | `[]` | CORS allowlist (`https://example.ca`, no trailing slash); the backend's own origin is always allowed |
| `success_url`, `error_url` | none | 303 targets for non-JS posts; errors add `?error=...&fields=...`. Without them a minimal HTML page is shown |
| `thank_you` | `Thank you, your message has been sent.` | JSON `message` and the fallback page text |
| `token` | `required` | `required`, `lenient` (a missing token is accepted, a present one is checked) or `off` |
| `min_age`, `max_age` | `3`, `14400` | seconds a token must be older than / younger than |
| `honeypot` | `website` | name of the decoy field; `""` disables it |
| `content_filter` | `true` | form-spam-guard's `ContentFilter` over the declared fields |
| `allowed_scripts` | `["latin"]` | scripts your visitors write in (`cyrillic`, `chinese`, `arabic`...) |
| `allowed_domains` | `[]` | links to these (and subdomains) are always fine |
| `blocked_hosts` | `[]` | added to the built-in list (`sslip.io`, `nip.io`, `ngrok*`, `trycloudflare.com`, `bit.ly`, `t.me`...) |
| `max_links`, `max_foreign_ratio` | `2`, `0.3` | content filter limits |
| `rate_limit`, `rate_refill_minutes` | `5`, `10` | per-IP burst, then one more message every N minutes; `0` disables |
| `webhook` | none | URL that receives accepted submissions as JSON |

`[server]`: `trust_proxy` (use the last `X-Forwarded-For` entry as client IP), `max_body_bytes` (65536; larger bodies get 413), `nonce_db` (SQLite file for single-use tokens shared by workers; default in memory).

`[log]`: `jsonl` (path; one line per submission with outcome, reason, field names and delivery status, never field values), `sqlite` (path; stores submissions, blocked ones too, so false positives can be reviewed), `retention_days` (30; older SQLite rows are deleted at startup and hourly), `log_ip` (false).

A submission is accepted when at least one channel worked: email, webhook, or the SQLite log. If none is configured, the server warns at startup and answers valid messages with `delivery_failed`.

## Environment variables

| Variable | Meaning |
|---|---|
| `CFB_CONFIG` | config path (default `forms.toml`) |
| `CFB_SECRET` | token signing key, at least 16 bytes, same in every worker. If unset a random one is used and a warning is logged |
| `CFB_SMTP_HOST` | SMTP server; unset = no email |
| `CFB_SMTP_PORT` | default 587 / 465 / 25 depending on security |
| `CFB_SMTP_SECURITY` | `starttls` (default), `ssl` or `none` |
| `CFB_SMTP_USER`, `CFB_SMTP_PASSWORD` | login, when your server needs one |
| `CFB_SMTP_TIMEOUT` | seconds, default 15 |
| `CFB_MAIL_FROM` | From address, on **your** domain (`Website <forms@example.ca>`) |
| `CFB_MAIL_TO` | comma-separated default recipients |
| `CFB_WEBHOOK_SECRET` | optional; signs webhook bodies: `X-Contact-Form-Signature: sha256=<hex HMAC>` |

See [examples/contact-form-backend.env.example](examples/contact-form-backend.env.example).

## The HTML snippet

[examples/contact-form.html](examples/contact-form.html) is a complete form: fields, the honeypot (hidden off-screen, `tabindex="-1"`, `aria-hidden`), an empty `fsg_token` input and about 80 lines of commented vanilla JavaScript. Change the two URLs to your backend and keep the field names in sync with the config.

With JavaScript, the script fetches `/token?form=contact` when the page loads (and every 20 minutes, so a tab left open does not expire), submits with `fetch` and shows the JSON answer in place. Validation errors come back as `{"ok": false, "error": "invalid_fields", "fields": {"email": "invalid_email"}}`.

### Without JavaScript

The form still posts and the visitor is redirected to `success_url`. But no script means no token, and the token cannot be put in a static HTML file: it carries a timestamp and is single-use. Pick one:

- **`token = "lenient"`** (the example config). A missing token is accepted; a present token is still checked for age, signature and reuse. **The trade-off:** a bot that sends the form without a token (most simple bots post the fields they see, with the token empty) skips the timing check entirely. The honeypot, content filter, rate limit and origin check still apply, and they are what stopped the spam in the demo above, but you lose one layer.
- **`token = "required"` with a server-rendered token.** If the page is generated per request (PHP, a template engine, SSI), include `GET /f/contact/fields`, which returns the honeypot and a fresh token as HTML. The page must not be cached (a cached token is expired or already used, and the next visitor's message is dropped silently).
- **`token = "required"` and JavaScript only.** Strongest, but visitors without JavaScript lose their message without knowing it. If you choose this, add a `<noscript>` note with your email address.

## Security notes

- **Silent drop.** Honeypot, token, content and rate-limit failures all return the normal thank-you (same status, same body or redirect). Only request and schema errors (missing field, bad email, body too large, wrong origin) are reported, and even those turn into "thank you" when the honeypot is filled.
- **Only declared fields are used.** Extra fields are ignored, never emailed or forwarded.
- **Header injection.** The Reply-To address must be a plain `local@domain` (no display name, no whitespace, no CR/LF; IDN domains are converted to `xn--`). The subject is flattened to one line. From is always your own address, so SPF/DKIM/DMARC keep working.
- **HTML email.** Every value is HTML-escaped; control and bidi-override characters are removed from all fields.
- **CORS.** Only origins in `allowed_origins` (and the backend's own) get `Access-Control-Allow-Origin`; a POST or token request carrying another `Origin` is refused with 403. Requests with no `Origin` (curl, some bots) are allowed and go through the spam checks.
- **Limits.** Bodies over `max_body_bytes` get 413; file uploads and more than 200 form fields are rejected with 400.
- **Proxy.** Enable `trust_proxy` only when the backend is reachable through your proxy alone; otherwise anyone can choose their rate-limit key.
- **Logs.** The JSON-lines log never contains message text. The SQLite log does (that is its purpose); keep `retention_days` short. form-spam-guard's own logger writes the client IP for blocked submissions at INFO level; raise the `form_spam_guard` logger to WARNING in your logging setup if you do not want IPs in the logs.
- **Secrets** come from the environment, never from the config file.

## Limitations

- **A determined human gets through.** This stops bots and drive-by spam, not a person who opens the page, waits and types a clean message.
- **Rate limit and default replay store are per process.** With several workers, set `nonce_db`; the rate limiter still counts per worker.
- **Delivery is synchronous.** The visitor waits for the SMTP server (timeout `CFB_SMTP_TIMEOUT`). There is no queue or retry: if SMTP and every other channel fail, the visitor is told the message was not sent.
- **An expired token is dropped silently**, like a bot's. The snippet refreshes tokens every 20 minutes, and `max_age` is 4 hours by default; a no-JS page with a server-rendered token left open longer loses its message. The SQLite log shows such drops (`token_expired`).
- **Email only as ASCII local parts** (no SMTPUTF8), and phone validation is a loose format check (7 to 15 digits), not a numbering-plan check.
- **The content filter is a heuristic.** A Latin-only setting drops genuine Cyrillic or Chinese messages; list the scripts your customers use.
- No file uploads, no admin UI, no multi-language error pages.

## Author

Marvin Palencia, founder of [DeMark Studio](https://demarkstudio.ca), Miramichi, New Brunswick, Canada. Portfolio: [marvin.demarkstudio.ca](https://marvin.demarkstudio.ca)

MIT License.
