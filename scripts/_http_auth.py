"""Shared authentication headers for plugin-originated governance HTTP calls."""

from __future__ import annotations

import os
import urllib.parse
import urllib.request
from typing import Any


def _http_origin(url: str) -> tuple[str, str, int] | None:
    """Return a normalized HTTP origin, or None for malformed/non-HTTP URLs."""
    try:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, host, port


class _AuthorizationSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Preserve bearer headers only across redirects within one HTTP origin."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        source = _http_origin(req.full_url)
        target = _http_origin(redirected.full_url)
        if source is None or target is None or source != target:
            redirected.remove_header("Authorization")
        return redirected


_AUTHORIZATION_SAFE_OPENER = urllib.request.build_opener(
    _AuthorizationSafeRedirectHandler()
)


def authorization_safe_urlopen(
    request: urllib.request.Request,
    *,
    timeout: float,
) -> Any:
    """Open a request without forwarding Authorization across origins."""
    return _AUTHORIZATION_SAFE_OPENER.open(request, timeout=timeout)


def governance_json_headers() -> dict[str, str]:
    """Return JSON headers with the optional governance client bearer token."""
    headers = {"Content-Type": "application/json"}
    token = (os.environ.get("UNITARES_HTTP_API_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
