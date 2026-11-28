"""Restricted HTTPS transport for DGSM's trusted public APIs.

Requests are HTTPS-only and hostname-allowlisted. Redirects are allowed only
for idempotent GET/HEAD requests that remain on the same HTTPS origin. This
prevents an API token or other request headers from being forwarded to a
different host or to plaintext HTTP.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from urllib.parse import urlsplit
from typing import Iterable


def _https_origin(url: str) -> tuple[str, int]:
    """Parse and validate an HTTPS origin, returning its canonical host/port."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid HTTPS URL: {exc}") from exc

    if parsed.scheme.casefold() != "https" or not hostname:
        raise ValueError("Only absolute HTTPS URLs are permitted.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Embedded URL credentials are not permitted.")
    if parsed.fragment:
        raise ValueError("URL fragments are not permitted.")
    if port not in (None, 443):
        raise ValueError("Only the standard HTTPS port 443 is permitted.")

    return hostname.rstrip(".").casefold(), 443


class SameOriginHTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow safe GET redirects only when scheme, host and port stay identical."""

    def __init__(self, origin: tuple[str, int]) -> None:
        super().__init__()
        self._origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            new_origin = _https_origin(newurl)
        except ValueError as exc:
            try:
                if fp is not None:
                    fp.close()
            except Exception:
                pass
            raise urllib.error.HTTPError(
                newurl,
                code,
                f"Unsafe redirect blocked: {exc}",
                headers,
                None,
            ) from exc

        if new_origin != self._origin or req.get_method().upper() not in {"GET", "HEAD"}:
            try:
                if fp is not None:
                    fp.close()
            except Exception:
                pass
            raise urllib.error.HTTPError(
                newurl,
                code,
                "Unsafe redirect blocked: only same-origin HTTPS GET/HEAD redirects are allowed.",
                headers,
                None,
            )

        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_urlopen(
    request,
    *,
    timeout: float,
    context,
    allowed_hosts: Iterable[str],
):
    """Open an allowlisted HTTPS URL with same-origin redirect enforcement."""
    if isinstance(request, str):
        url = request
    else:
        url = getattr(request, "full_url", None)
    if not isinstance(url, str):
        raise ValueError("A string URL or urllib Request is required.")

    origin = _https_origin(url)
    allowed = {str(host).rstrip(".").casefold() for host in allowed_hosts if host}
    if not allowed or origin[0] not in allowed:
        raise ValueError(f"HTTPS host {origin[0]!r} is not in the allowed host list.")

    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        SameOriginHTTPSRedirectHandler(origin),
    )
    return opener.open(request, timeout=timeout)
