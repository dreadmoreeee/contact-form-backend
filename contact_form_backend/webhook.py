"""Optional webhook: POST the accepted submission as JSON."""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.request
from typing import Any, Mapping, Optional


def post_json(
    url: str,
    payload: Mapping[str, Any],
    secret: Optional[str] = None,
    timeout: float = 10.0,
) -> int:
    """POST ``payload``; return the HTTP status. Raises on network errors and non-2xx.

    With ``secret`` (``CFB_WEBHOOK_SECRET``) the body is signed:
    ``X-Contact-Form-Signature: sha256=<hex HMAC of the raw body>``.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "contact-form-backend"}
    if secret:
        sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Contact-Form-Signature"] = "sha256=" + sig
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (URL from config)
        status = resp.status
    if not 200 <= status < 300:
        raise OSError("webhook answered HTTP %d" % status)
    return status
