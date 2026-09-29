"""SMTP delivery: settings from environment variables, plain text + HTML email."""

from __future__ import annotations

import html
import re
import functools
import smtplib
import socket
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from typing import Mapping, Optional, Sequence, Tuple

from .config import FormConfig
from .validation import normalize_email

SECURITY_MODES = ("starttls", "ssl", "none")
_DEFAULT_PORTS = {"starttls": 587, "ssl": 465, "none": 25}
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_-]*)\}")


@dataclass(frozen=True)
class MailSettings:
    host: str
    port: int
    security: str
    user: str
    password: str
    sender: str
    to: Tuple[str, ...]
    timeout: float = 15.0

    @property
    def sender_address(self) -> str:
        return parseaddr(self.sender)[1]

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Optional["MailSettings"]:
        """Read ``CFB_SMTP_*`` / ``CFB_MAIL_*``. None when no host is set."""
        host = env.get("CFB_SMTP_HOST", "").strip()
        if not host:
            return None
        security = env.get("CFB_SMTP_SECURITY", "starttls").strip().lower() or "starttls"
        if security not in SECURITY_MODES:
            raise ValueError("CFB_SMTP_SECURITY must be one of %s" % ", ".join(SECURITY_MODES))
        port_text = env.get("CFB_SMTP_PORT", "").strip()
        port = int(port_text) if port_text else _DEFAULT_PORTS[security]
        sender = env.get("CFB_MAIL_FROM", "").strip()
        if not sender or normalize_email(parseaddr(sender)[1]) is None:
            raise ValueError("CFB_MAIL_FROM must be set to an address on your own domain")
        if "\n" in sender or "\r" in sender:
            raise ValueError("CFB_MAIL_FROM must be a single line")
        to = tuple(a.strip() for a in env.get("CFB_MAIL_TO", "").split(",") if a.strip())
        for addr in to:
            if normalize_email(addr) is None:
                raise ValueError("CFB_MAIL_TO contains an invalid address: %r" % addr)
        timeout = float(env.get("CFB_SMTP_TIMEOUT", "15") or 15)
        return cls(
            host=host,
            port=port,
            security=security,
            user=env.get("CFB_SMTP_USER", ""),
            password=env.get("CFB_SMTP_PASSWORD", ""),
            sender=sender,
            to=to,
            timeout=timeout,
        )


def render_subject(template: str, values: Mapping[str, str]) -> str:
    """Fill ``{field}`` placeholders; the result is one line of at most 150 chars."""
    text = _PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), template)
    text = " ".join(text.split())
    return (text[:147] + "...") if len(text) > 150 else text


def build_message(
    form: FormConfig,
    values: Mapping[str, str],
    settings: MailSettings,
    recipients: Sequence[str],
    submitted_at: str,
) -> EmailMessage:
    """Multipart plain text + HTML email. Every value is HTML-escaped in the HTML part.

    From is always your own address (``CFB_MAIL_FROM``); the visitor's
    validated email goes into Reply-To so "Reply" answers them.
    """
    msg = EmailMessage()
    msg["Subject"] = render_subject(form.subject, values) or "New message from your website"
    msg["From"] = settings.sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=False, usegmt=True)
    msg["Message-ID"] = make_msgid(domain=settings.sender_address.rsplit("@", 1)[-1])
    msg["X-Contact-Form"] = form.id
    reply = form.reply_field()
    if reply is not None and values.get(reply.name):
        name_spec = next((s for s in form.fields if s.name == "name"), None)
        display = values.get("name", "") if name_spec is not None else ""
        msg["Reply-To"] = formataddr((" ".join(display.split())[:80], values[reply.name]))

    lines = ["New message from the '%s' form, %s" % (form.id, submitted_at), ""]
    rows = []
    for spec in form.fields:
        value = values.get(spec.name)
        if value is None:
            continue
        if "\n" in value:
            lines += ["%s:" % spec.title, value, ""]
        else:
            lines.append("%s: %s" % (spec.title, value))
        rows.append(
            '<tr><th align="left" valign="top" style="padding:4px 12px 4px 0">%s</th>'
            '<td style="padding:4px 0;white-space:pre-wrap">%s</td></tr>'
            % (html.escape(spec.title), html.escape(value))
        )
    msg.set_content("\n".join(lines).rstrip() + "\n", cte="quoted-printable")
    msg.add_alternative(
        "<!doctype html><html><body>"
        "<p>New message from the <b>%s</b> form, %s</p>"
        '<table cellspacing="0" cellpadding="0">%s</table>'
        "</body></html>\n"
        % (html.escape(form.id), html.escape(submitted_at), "".join(rows)),
        subtype="html",
        cte="quoted-printable",
    )
    return msg


@functools.lru_cache(maxsize=1)
def _local_hostname() -> str:
    # smtplib looks this up on every connection; it can take a second on some hosts.
    return socket.getfqdn()


def send_message(msg: EmailMessage, settings: MailSettings, recipients: Sequence[str]) -> None:
    """Send over SMTP with STARTTLS, implicit TLS or plain, logging in when a user is set."""
    context = ssl.create_default_context()
    if settings.security == "ssl":
        conn: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.host, settings.port, local_hostname=_local_hostname(),
            timeout=settings.timeout, context=context,
        )
    else:
        conn = smtplib.SMTP(
            settings.host, settings.port, local_hostname=_local_hostname(),
            timeout=settings.timeout,
        )
    with conn:
        conn.ehlo()
        if settings.security == "starttls":
            conn.starttls(context=context)
            conn.ehlo()
        if settings.user:
            conn.login(settings.user, settings.password)
        conn.send_message(msg, from_addr=settings.sender_address, to_addrs=list(recipients))
