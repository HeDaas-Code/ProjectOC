"""Encrypted storage for credentials scoped to a private workspace.

The plaintext API key is only held in memory for the duration of a provider
request.  It is never included in serializers, Git content, canvas snapshots,
or application logs.
"""
from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet() -> Fernet:
    configured = str(getattr(settings, "WORKSPACE_CREDENTIALS_KEY", "") or "").strip()
    if configured:
        key = configured.encode("utf-8")
    else:
        # Development fallback keeps local installs usable, while deployments
        # can (and should) provide a dedicated stable key through env.
        seed = str(getattr(settings, "SECRET_KEY", "projectoc-development-secret-key")).encode("utf-8")
        key = __import__("base64").urlsafe_b64encode(hashlib.sha256(seed).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii") if value else ""


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError, UnicodeDecodeError):
        return ""


def set_workspace_api_key(workspace, value: str) -> None:
    from apps.core.models import WorkspaceAISecret

    secret, _ = WorkspaceAISecret.objects.get_or_create(workspace=workspace)
    secret.api_key_encrypted = encrypt_secret(value)
    secret.api_key_last4 = value[-4:] if value else ""
    secret.save(update_fields=["api_key_encrypted", "api_key_last4", "updated_at"])


def workspace_api_key(workspace) -> str:
    try:
        secret = workspace.ai_secret
    except Exception:
        return ""
    return decrypt_secret(secret.api_key_encrypted)


def workspace_api_key_status(workspace) -> dict[str, object]:
    try:
        secret = workspace.ai_secret
    except Exception:
        return {"configured": False, "last4": ""}
    return {
        "configured": bool(secret.api_key_encrypted),
        "last4": secret.api_key_last4 if secret.api_key_encrypted else "",
    }
