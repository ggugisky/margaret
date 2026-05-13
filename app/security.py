from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode())


def create_signed_token(
    *,
    secret: str,
    app: str = "margaret-voice",
    ttl_seconds: int = 23 * 60 * 60,
) -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"app": app, "iat": now, "exp": now + ttl_seconds}
    signing_input = ".".join(
        [
            _b64url_encode(json.dumps(header, separators=(",", ":")).encode()),
            _b64url_encode(json.dumps(payload, separators=(",", ":")).encode()),
        ]
    )
    signature = hmac.new(
        secret.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_b64url_encode(signature)}"


def verify_signed_token(token: str, *, secret: str, app: str = "margaret-voice") -> bool:
    if not token or not secret:
        return False
    try:
        header_b64, payload_b64, signature_b64 = token.split(".", 2)
        signing_input = f"{header_b64}.{payload_b64}"
        expected = hmac.new(
            secret.encode(), signing_input.encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_b64url_encode(expected), signature_b64):
            return False
        payload: dict[str, Any] = json.loads(_b64url_decode(payload_b64))
        if payload.get("app") != app:
            return False
        return int(payload.get("exp", 0)) >= int(time.time())
    except Exception:
        return False


def sign_message(payload: str, *, secret: str) -> str:
    if not secret:
        return ""
    ts = str(int(time.time()))
    nonce = _b64url_encode(os.urandom(8))
    data = f"{ts}.{nonce}.{payload}"
    sig = hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{nonce}.{sig}"


def verify_message_signature(payload: str, signature: str, *, secret: str) -> bool:
    if not secret:
        return True
    if not signature:
        return False
    try:
        ts_str, nonce, sig = signature.split(".", 2)
        if abs(int(time.time()) - int(ts_str)) > 60:
            return False
        data = f"{ts_str}.{nonce}.{payload}"
        expected = hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()
        legacy_expected = hashlib.sha256((secret + data).encode()).hexdigest()
        return hmac.compare_digest(expected, sig) or hmac.compare_digest(
            legacy_expected, sig
        )
    except Exception:
        return False
