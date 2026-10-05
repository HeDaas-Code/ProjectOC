"""Short-lived signed tickets for the self-hosted canvas sync service.

The sync service must not receive Django's session cookie.  The API issues a
scoped, expiring HMAC token instead.  The same compact format is implemented
in ``sync-service/src/ticket.ts`` so the websocket process can validate it
without making every frame a round trip to Django.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from django.conf import settings


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def ticket_secret() -> bytes:
    # A dedicated secret is recommended for self-hosted deployments. Falling
    # back to SECRET_KEY keeps local development usable without another env var.
    return os.getenv("SYNC_TICKET_SECRET", getattr(settings, "SECRET_KEY", "")).encode()


def issue_sync_ticket(*, user, workspace_id, canvas_id, role: str, branch_id: str | None = None, ttl: int = 300, client_id: str | None = None) -> tuple[str, int]:
    now = int(time.time())
    expires = now + max(30, min(int(ttl), 900))
    payload = {
        "v": 1,
        "sub": str(user.pk),
        "username": user.get_username(),
        "workspace": str(workspace_id),
        "canvas": str(canvas_id),
        # Branch is part of the room identity. Keep it explicit in new tickets
        # so a client can never accidentally join main when it meant to join a
        # canvas work branch. Legacy tickets without it are still accepted by
        # the sync daemon and treated as main for backwards compatibility.
        "branch": str(branch_id) if branch_id else "main",
        "role": role,
        "client_id": client_id or f"user:{user.pk}",
        "iat": now,
        "exp": expires,
    }
    encoded = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    signature = _b64(hmac.new(ticket_secret(), encoded.encode(), hashlib.sha256).digest())
    return f"v1.{encoded}.{signature}", expires


def verify_sync_ticket(token: str, *, now: int | None = None) -> dict[str, Any]:
    try:
        version, encoded, signature = token.split(".", 2)
        if version != "v1":
            raise ValueError("unsupported ticket version")
        expected = _b64(hmac.new(ticket_secret(), encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid ticket signature")
        payload = json.loads(_unb64(encoded))
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid sync ticket") from exc
    current = int(time.time() if now is None else now)
    if payload.get("v") != 1 or int(payload.get("exp", 0)) <= current:
        raise ValueError("expired sync ticket")
    required = ("sub", "workspace", "canvas", "role", "client_id")
    if any(not payload.get(field) for field in required) or payload["role"] not in {"owner", "editor", "reader"}:
        raise ValueError("invalid sync ticket claims")
    return payload
