"""Authentication for the bridge -> backend hop."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from .config import settings


def require_bridge_token(x_bridge_token: str = Header(default="")) -> None:
    """Shared-secret check for every ingest/heartbeat call.

    `compare_digest` keeps the comparison constant-time; the bridge is on the
    same LAN as the server, so a shared secret over loopback/HTTPS is the right
    weight of control for this hop (clinician auth is a separate concern).
    """
    if not hmac.compare_digest(x_bridge_token, settings.bridge_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Bridge-Token",
        )
