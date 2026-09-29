"""TOML configuration: server/log settings and one table per form."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

FIELD_TYPES = ("text", "textarea", "email", "phone")
TOKEN_MODES = ("required", "lenient", "off")
DEFAULT_MAX_LENGTH = {"text": 200, "textarea": 5000, "email": 254, "phone": 40}
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


class ConfigError(ValueError):
    """The configuration file is invalid."""


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str = "text"
    required: bool = False
    max_length: int = 0
    label: str = ""

    @property
    def title(self) -> str:
        return self.label or self.name.replace("_", " ").capitalize()

    @property
    def limit(self) -> int:
        return self.max_length or DEFAULT_MAX_LENGTH[self.type]


@dataclass(frozen=True)
class FormConfig:
    id: str
    fields: Tuple[FieldSpec, ...]
    subject: str = "New message from your website"
    to: Tuple[str, ...] = ()
    email: bool = True
    reply_to_field: str = ""
    allowed_origins: Tuple[str, ...] = ()
    success_url: str = ""
    error_url: str = ""
    thank_you: str = "Thank you, your message has been sent."
    token: str = "required"
    min_age: float = 3.0
    max_age: float = 4 * 3600.0
    honeypot: str = "website"
    content_filter: bool = True
    allowed_scripts: Tuple[str, ...] = ("latin",)
    allowed_domains: Tuple[str, ...] = ()
    blocked_hosts: Tuple[str, ...] = ()
    max_links: int = 2
    max_foreign_ratio: float = 0.3
    rate_limit: int = 5
    rate_refill_minutes: float = 10.0
    webhook: str = ""

    def reply_field(self) -> Optional[FieldSpec]:
        for spec in self.fields:
            if self.reply_to_field and spec.name == self.reply_to_field:
                return spec
        if not self.reply_to_field:
            for spec in self.fields:
                if spec.type == "email":
                    return spec
        return None


@dataclass(frozen=True)
class AppConfig:
    forms: Dict[str, FormConfig]
    trust_proxy: bool = False
    max_body_bytes: int = 64 * 1024
    nonce_db: str = ""
    jsonl: str = ""
    sqlite: str = ""
    retention_days: float = 30.0
    log_ip: bool = False


def _take(table: Mapping[str, Any], allowed: Dict[str, type], where: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in table.items():
        if key not in allowed:
            raise ConfigError("%s: unknown key %r" % (where, key))
        want = allowed[key]
        if want is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if want is tuple:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError("%s: %r must be a list of strings" % (where, key))
            value = tuple(value)
        elif not isinstance(value, want) or (want is int and isinstance(value, bool)):
            raise ConfigError("%s: %r must be %s" % (where, key, want.__name__))
        out[key] = value
    return out


_FIELD_KEYS = {"name": str, "type": str, "required": bool, "max_length": int, "label": str}
_FORM_KEYS = {
    "subject": str, "to": tuple, "email": bool, "reply_to_field": str,
    "allowed_origins": tuple, "success_url": str, "error_url": str, "thank_you": str,
    "token": str, "min_age": float, "max_age": float, "honeypot": str,
    "content_filter": bool, "allowed_scripts": tuple, "allowed_domains": tuple,
    "blocked_hosts": tuple, "max_links": int, "max_foreign_ratio": float,
    "rate_limit": int, "rate_refill_minutes": float, "webhook": str,
}
_SERVER_KEYS = {"trust_proxy": bool, "max_body_bytes": int, "nonce_db": str}
_LOG_KEYS = {"jsonl": str, "sqlite": str, "retention_days": float, "log_ip": bool}


def _parse_form(form_id: str, table: Mapping[str, Any]) -> FormConfig:
    where = "forms.%s" % form_id
    if not _ID.match(form_id):
        raise ConfigError("%s: form id may only use letters, digits, '-' and '_'" % where)
    if not isinstance(table, Mapping):
        raise ConfigError("%s must be a table" % where)
    raw_fields = table.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ConfigError("%s: at least one [[%s.fields]] entry is needed" % (where, where))
    specs: List[FieldSpec] = []
    for i, item in enumerate(raw_fields):
        fwhere = "%s.fields[%d]" % (where, i)
        if not isinstance(item, Mapping):
            raise ConfigError("%s must be a table" % fwhere)
        values = _take(item, _FIELD_KEYS, fwhere)
        if "name" not in values or not _NAME.match(values["name"]):
            raise ConfigError("%s: 'name' is missing or not a valid field name" % fwhere)
        if values.get("type", "text") not in FIELD_TYPES:
            raise ConfigError("%s: type must be one of %s" % (fwhere, ", ".join(FIELD_TYPES)))
        if values.get("max_length", 0) < 0:
            raise ConfigError("%s: max_length must be >= 0" % fwhere)
        specs.append(FieldSpec(**values))
    names = [s.name for s in specs]
    if len(set(names)) != len(names):
        raise ConfigError("%s: duplicate field names" % where)

    rest = {k: v for k, v in table.items() if k != "fields"}
    values = _take(rest, _FORM_KEYS, where)
    cfg = FormConfig(id=form_id, fields=tuple(specs), **values)
    if cfg.token not in TOKEN_MODES:
        raise ConfigError("%s: token must be one of %s" % (where, ", ".join(TOKEN_MODES)))
    if not (0 <= cfg.min_age < cfg.max_age):
        raise ConfigError("%s: need 0 <= min_age < max_age" % where)
    if cfg.honeypot and cfg.honeypot in names:
        raise ConfigError("%s: honeypot %r clashes with a real field" % (where, cfg.honeypot))
    if cfg.honeypot and not _NAME.match(cfg.honeypot):
        raise ConfigError("%s: honeypot is not a valid field name" % where)
    if "fsg_token" in names:
        raise ConfigError("%s: 'fsg_token' is reserved for the spam token" % where)
    if cfg.reply_to_field:
        spec = next((s for s in specs if s.name == cfg.reply_to_field), None)
        if spec is None or spec.type != "email":
            raise ConfigError("%s: reply_to_field must name an email field" % where)
    for url in (cfg.success_url, cfg.error_url, cfg.webhook):
        if url and not url.startswith(("http://", "https://", "/")):
            raise ConfigError("%s: URL %r must start with http://, https:// or /" % (where, url))
    if cfg.webhook and not cfg.webhook.startswith(("http://", "https://")):
        raise ConfigError("%s: webhook must be an absolute http(s) URL" % where)
    if cfg.rate_limit < 0 or cfg.rate_refill_minutes <= 0:
        raise ConfigError("%s: rate_limit must be >= 0 and rate_refill_minutes > 0" % where)
    for origin in cfg.allowed_origins:
        if origin != "*" and (origin.endswith("/") or "://" not in origin):
            raise ConfigError(
                "%s: origin %r must look like https://example.ca (no path, no trailing /)"
                % (where, origin)
            )
    return cfg


def parse_config(data: Mapping[str, Any]) -> AppConfig:
    """Build an :class:`AppConfig` from already-parsed TOML data."""
    unknown = set(data) - {"server", "log", "forms"}
    if unknown:
        raise ConfigError("unknown top-level table(s): %s" % ", ".join(sorted(unknown)))
    server = _take(data.get("server", {}), _SERVER_KEYS, "server")
    logcfg = _take(data.get("log", {}), _LOG_KEYS, "log")
    forms_raw = data.get("forms")
    if not isinstance(forms_raw, Mapping) or not forms_raw:
        raise ConfigError("define at least one form, e.g. [forms.contact]")
    forms = {fid: _parse_form(fid, table) for fid, table in forms_raw.items()}
    cfg = AppConfig(forms=forms, **server, **logcfg)
    if cfg.max_body_bytes < 1024:
        raise ConfigError("server.max_body_bytes must be at least 1024")
    if cfg.retention_days <= 0:
        raise ConfigError("log.retention_days must be > 0")
    return cfg


def load_config(path: str) -> AppConfig:
    """Read and validate a TOML config file."""
    with open(path, "rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError("%s: %s" % (path, exc)) from None
    return parse_config(data)


def loads_config(text: str) -> AppConfig:
    """Parse a TOML string (handy in tests)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(str(exc)) from None
    return parse_config(data)
