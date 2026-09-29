"""FastAPI application: token endpoint, one POST endpoint per form, health."""

from __future__ import annotations

import html
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import parse_qsl, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from form_spam_guard import (
    ContentFilter,
    Decision,
    Guard,
    Honeypot,
    InMemoryStore,
    NonceStore,
    RateLimiter,
    SQLiteStore,
)

from . import mailer, webhook
from .config import AppConfig, FormConfig, load_config
from .storage import JsonlLog, SQLiteLog
from .validation import validate

log = logging.getLogger("contact_form_backend")

TOKEN_FIELD = "fsg_token"
_NO_STORE = {"Cache-Control": "no-store"}


class _BadRequest(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass
class FormRuntime:
    cfg: FormConfig
    guard: Guard           # every layer, token required
    guard_no_token: Guard  # same honeypot/content/limiter, no token layer

    def origin_allowed(self, origin: str, own_origin: str) -> bool:
        return origin == own_origin or "*" in self.cfg.allowed_origins or (
            origin in self.cfg.allowed_origins
        )

    def check(self, raw: Mapping[str, str], clean: Mapping[str, str], key: Optional[str]) -> Decision:
        token = raw.get(TOKEN_FIELD, "")
        guard = self.guard
        if self.cfg.token == "off" or (self.cfg.token == "lenient" and not token.strip()):
            guard = self.guard_no_token
        payload = {TOKEN_FIELD: token}
        if self.cfg.honeypot:
            payload[self.cfg.honeypot] = raw.get(self.cfg.honeypot, "")
        # Only the declared, validated fields are scanned by the content filter.
        return guard.check(payload, key=key, text="\n".join(clean.values()))


def _build_runtime(
    cfg: FormConfig, secret: str, store: NonceStore, clock: Callable[[], float]
) -> FormRuntime:
    honeypot = Honeypot(field_name=cfg.honeypot) if cfg.honeypot else None
    content = (
        ContentFilter(
            allowed_scripts=list(cfg.allowed_scripts),
            max_foreign_ratio=cfg.max_foreign_ratio,
            blocked_hosts=list(cfg.blocked_hosts),
            allowed_domains=list(cfg.allowed_domains),
            max_links=cfg.max_links,
        )
        if cfg.content_filter
        else None
    )
    limiter = (
        RateLimiter(capacity=cfg.rate_limit, refill_per_second=1.0 / (cfg.rate_refill_minutes * 60))
        if cfg.rate_limit > 0
        else None
    )
    common: Dict[str, Any] = dict(
        honeypot=honeypot, content=content, limiter=limiter, form_id=cfg.id,
        token_field=TOKEN_FIELD, clock=clock,
    )
    guard = Guard(secret, store=store, min_age=cfg.min_age, max_age=cfg.max_age, **common)
    return FormRuntime(cfg, guard, Guard(None, **common))


def _now_iso(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>%s</title>"
        "<meta name=\"viewport\" content=\"width=device-width\"></head>"
        "<body><h1>%s</h1><p>%s</p></body></html>"
        % (html.escape(title), html.escape(title), html.escape(body)),
        status_code=status,
        headers=_NO_STORE,
    )


def _with_query(url: str, params: Mapping[str, str]) -> str:
    return url + ("&" if "?" in url else "?") + urlencode(params)


async def _read_body(request: Request, limit: int) -> bytes:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > limit:
                raise _BadRequest(413, "too_large")
        except ValueError:
            raise _BadRequest(400, "bad_request") from None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise _BadRequest(413, "too_large")
    data = bytes(body)
    request._body = data  # lets request.form() re-read the body we already consumed
    return data


async def _parse_submission(request: Request, limit: int) -> Dict[str, str]:
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype not in (
        "application/json",
        "application/x-www-form-urlencoded",
        "multipart/form-data",
    ):
        raise _BadRequest(415, "unsupported_media_type")
    body = await _read_body(request, limit)
    out: Dict[str, str] = {}
    if ctype == "application/json":
        try:
            obj = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, ValueError):
            raise _BadRequest(400, "bad_json") from None
        if not isinstance(obj, dict):
            raise _BadRequest(400, "bad_json")
        for key, value in obj.items():
            if isinstance(value, bool):
                value = "yes" if value else "no"
            if isinstance(value, (str, int, float)):
                out[str(key)] = str(value)
        return out
    if ctype == "application/x-www-form-urlencoded":
        text = body.decode("utf-8", errors="replace")
        try:
            pairs = parse_qsl(text, keep_blank_values=True, max_num_fields=200)
        except ValueError:
            raise _BadRequest(400, "too_many_fields") from None
        for key, value in pairs:
            out.setdefault(key, value)
        return out
    try:
        form = await request.form(max_files=0, max_fields=200)
    except Exception:  # malformed multipart, or a file upload
        raise _BadRequest(400, "bad_multipart") from None
    try:
        for key in form.keys():
            for value in form.getlist(key):
                if isinstance(value, str):
                    out.setdefault(key, value)
                    break
    finally:
        await form.close()
    return out


def _client_ip(request: Request, trust_proxy: bool) -> Optional[str]:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]  # the address your own proxy saw
    return request.client.host if request.client else None


def create_app(
    config: AppConfig,
    *,
    env: Optional[Mapping[str, str]] = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """Build the ASGI app. ``env`` defaults to ``os.environ`` (SMTP, secret, webhook secret)."""
    env = os.environ if env is None else env
    secret = env.get("CFB_SECRET", "")
    if not secret:
        secret = secrets.token_urlsafe(32)
        log.warning(
            "CFB_SECRET is not set: using a random secret. Tokens will not survive a "
            "restart and will not work across several workers."
        )
    elif len(secret.encode("utf-8")) < 16:
        raise ValueError("CFB_SECRET must be at least 16 bytes long")
    mail = mailer.MailSettings.from_env(env)
    webhook_secret = env.get("CFB_WEBHOOK_SECRET", "") or None
    store: NonceStore = SQLiteStore(config.nonce_db) if config.nonce_db else InMemoryStore()
    runtimes = {fid: _build_runtime(cfg, secret, store, clock) for fid, cfg in config.forms.items()}
    jsonl = JsonlLog(config.jsonl) if config.jsonl else None
    sqlite_log = SQLiteLog(config.sqlite, config.retention_days, clock) if config.sqlite else None

    for fid, cfg in config.forms.items():
        has_mail = cfg.email and mail is not None and bool(cfg.to or mail.to)
        if not (has_mail or cfg.webhook or sqlite_log):
            log.warning(
                "form %s has no delivery channel (SMTP env vars, webhook or SQLite log): "
                "valid messages will be answered with an error", fid,
            )

    app = FastAPI(title="contact-form-backend", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.runtimes = runtimes
    app.state.sqlite_log = sqlite_log

    def record(rt: FormRuntime, outcome: str, reason: str, check: str, ip: Optional[str],
               fields: Mapping[str, str], extra: Optional[Mapping[str, Any]] = None) -> None:
        log.info("form=%s outcome=%s reason=%s check=%s", rt.cfg.id, outcome, reason, check)
        if jsonl is not None:
            entry: Dict[str, Any] = {
                "ts": _now_iso(clock), "form": rt.cfg.id, "outcome": outcome,
                "reason": reason, "check": check, "fields": sorted(fields),
            }
            if config.log_ip:
                entry["ip"] = ip
            if extra:
                entry.update(extra)
            try:
                jsonl.write(entry)
            except OSError:
                log.exception("could not write the JSON-lines log")
        if sqlite_log is not None and outcome != "delivered":
            # Delivered messages are stored by deliver() (the log is a channel there).
            try:
                sqlite_log.add(rt.cfg.id, outcome, reason, fields, ip if config.log_ip else None)
            except Exception:
                log.exception("could not write the SQLite log")

    def deliver(rt: FormRuntime, fields: Mapping[str, str], ip: Optional[str]) -> Dict[str, str]:
        cfg = rt.cfg
        results: Dict[str, str] = {}
        submitted_at = _now_iso(clock)
        recipients = list(cfg.to or (mail.to if mail else ()))
        if cfg.email and mail is not None and recipients:
            try:
                msg = mailer.build_message(cfg, fields, mail, recipients, submitted_at)
                mailer.send_message(msg, mail, recipients)
                results["email"] = "sent"
            except Exception as exc:
                log.error("form=%s email failed: %s: %s", cfg.id, type(exc).__name__, exc)
                results["email"] = "failed"
        if cfg.webhook:
            payload = {"form": cfg.id, "submitted_at": submitted_at, "fields": dict(fields)}
            try:
                webhook.post_json(cfg.webhook, payload, webhook_secret)
                results["webhook"] = "sent"
            except Exception as exc:
                log.error("form=%s webhook failed: %s: %s", cfg.id, type(exc).__name__, exc)
                results["webhook"] = "failed"
        if sqlite_log is not None:
            try:
                sqlite_log.add(cfg.id, "delivered", "ok", fields, ip if config.log_ip else None)
                results["sqlite"] = "sent"
            except Exception:
                log.exception("could not write the SQLite log")
                results["sqlite"] = "failed"
        return results

    def cors(response: Response, rt: FormRuntime, request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin and rt.origin_allowed(origin, _own_origin(request)):
            response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        return response

    def wants_json(request: Request) -> bool:
        accept = request.headers.get("accept", "").lower()
        ctype = request.headers.get("content-type", "").lower()
        return "application/json" in accept or ctype.startswith("application/json")

    def thank_you(rt: FormRuntime, request: Request, as_json: bool) -> Response:
        if as_json:
            resp: Response = JSONResponse(
                {"ok": True, "message": rt.cfg.thank_you}, headers=_NO_STORE
            )
        elif rt.cfg.success_url:
            resp = RedirectResponse(rt.cfg.success_url, status_code=303)
        else:
            resp = _page("Thank you", rt.cfg.thank_you)
        return cors(resp, rt, request)

    def failure(rt: FormRuntime, request: Request, as_json: bool, status: int, code: str,
                errors: Optional[Mapping[str, str]] = None) -> Response:
        errors = dict(errors or {})
        if as_json:
            body: Dict[str, Any] = {"ok": False, "error": code}
            if errors:
                body["fields"] = errors
            resp: Response = JSONResponse(body, status_code=status, headers=_NO_STORE)
        elif rt.cfg.error_url:
            params = {"error": code}
            if errors:
                params["fields"] = ",".join(errors)
            resp = RedirectResponse(_with_query(rt.cfg.error_url, params), status_code=303)
        else:
            detail = "Your message was not sent (%s)." % code
            if errors:
                titles = {s.name: s.title for s in rt.cfg.fields}
                detail += " Please check: " + ", ".join(
                    "%s (%s)" % (titles.get(n, n), e.replace("_", " ")) for n, e in errors.items()
                )
            resp = _page("Message not sent", detail, status)
        return cors(resp, rt, request)

    def lookup(form_id: str) -> Optional[FormRuntime]:
        return runtimes.get(form_id)

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {"status": "ok", "forms": len(runtimes)}

    @app.get("/token")
    def token(request: Request, form: str = "") -> Response:
        rt = lookup(form)
        if rt is None:
            return JSONResponse({"ok": False, "error": "unknown_form"}, status_code=404)
        origin = request.headers.get("origin")
        if origin and not rt.origin_allowed(origin, _own_origin(request)):
            return JSONResponse({"ok": False, "error": "origin_not_allowed"}, status_code=403)
        body: Dict[str, Any] = {
            "token": rt.guard.signer.issue(rt.cfg.id) if rt.guard.signer else "",
            "field": TOKEN_FIELD,
            "honeypot": rt.cfg.honeypot,
            "min_age": rt.cfg.min_age,
            "max_age": rt.cfg.max_age,
        }
        return cors(JSONResponse(body, headers=_NO_STORE), rt, request)

    @app.get("/f/{form_id}/fields")
    def fields_fragment(form_id: str, request: Request) -> Response:
        """Honeypot + a fresh token as an HTML fragment, for server-side includes."""
        rt = lookup(form_id)
        if rt is None:
            return JSONResponse({"ok": False, "error": "unknown_form"}, status_code=404)
        return cors(HTMLResponse(rt.guard.render_fields(), headers=_NO_STORE), rt, request)

    @app.options("/f/{form_id}")
    def preflight(form_id: str, request: Request) -> Response:
        rt = lookup(form_id)
        origin = request.headers.get("origin")
        if rt is None or not origin or not rt.origin_allowed(origin, _own_origin(request)):
            return Response(status_code=403)
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Accept",
                "Access-Control-Max-Age": "600",
                "Vary": "Origin",
            },
        )

    @app.post("/f/{form_id}")
    async def submit(form_id: str, request: Request) -> Response:
        rt = lookup(form_id)
        if rt is None:
            return JSONResponse({"ok": False, "error": "unknown_form"}, status_code=404)
        as_json = wants_json(request)
        origin = request.headers.get("origin")
        if origin and not rt.origin_allowed(origin, _own_origin(request)):
            log.info("form=%s outcome=rejected reason=origin_not_allowed origin=%s", form_id, origin)
            return JSONResponse({"ok": False, "error": "origin_not_allowed"}, status_code=403)
        try:
            raw = await _parse_submission(request, config.max_body_bytes)
        except _BadRequest as exc:
            return failure(rt, request, as_json, exc.status, exc.code)
        ip = _client_ip(request, config.trust_proxy)
        clean, errors = validate(rt.cfg.fields, raw)

        if errors:
            if rt.guard.honeypot is not None and rt.guard.honeypot.is_filled(raw):
                record(rt, "dropped", "honeypot_filled", "honeypot", ip, clean)
                return thank_you(rt, request, as_json)
            record(rt, "invalid", "invalid_fields", "schema", ip, clean,
                   {"errors": errors})
            return failure(rt, request, as_json, 400, "invalid_fields", errors)

        decision = rt.check(raw, clean, ip)
        if not decision.allowed:
            # Silent drop: the bot gets exactly what a real visitor gets.
            record(rt, "dropped", decision.reason, decision.check, ip, clean)
            return thank_you(rt, request, as_json)

        results = await run_in_threadpool(deliver, rt, clean, ip)
        if not any(v == "sent" for v in results.values()):
            record(rt, "failed", "delivery_failed", "delivery", ip, clean, {"delivery": results})
            return failure(rt, request, as_json, 502, "delivery_failed")
        record(rt, "delivered", "ok", decision.check, ip, clean, {"delivery": results})
        return thank_you(rt, request, as_json)

    return app


def _own_origin(request: Request) -> str:
    return "%s://%s" % (request.url.scheme, request.headers.get("host", request.url.netloc))


def app_from_env() -> FastAPI:
    """Factory for ``uvicorn --factory contact_form_backend.app:app_from_env``."""
    return create_app(load_config(os.environ.get("CFB_CONFIG", "forms.toml")))
