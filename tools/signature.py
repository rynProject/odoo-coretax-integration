"""Verifikasi tanda tangan webhook AksaTax.

Header ``AksaTax-Signature: t=<unix>,v1=<hex>[,v1=<hex>…]`` - HMAC-SHA256
dengan kunci = secret ``whsec_…`` utuh, atas ``"<t>.<isi mentah>"``.
Selama rotasi secret, header membawa beberapa ``v1`` (cukup satu yang cocok).
Lihat aksatax-be/apps/api/src/integrasi/webhook-signature.ts.
"""
import hashlib
import hmac
import time

TOLERANCE_SECONDS = 300


def parse_signature_header(header):
    timestamp, signatures = None, []
    for part in (header or "").split(","):
        key, _sep, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1" and value:
            signatures.append(value)
    return timestamp, signatures


def compute_signature(secret, timestamp, raw_body):
    if isinstance(raw_body, str):
        raw_body = raw_body.encode()
    message = f"{timestamp}.".encode() + raw_body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_signature(secret, header, raw_body, now=None, tolerance=TOLERANCE_SECONDS):
    """True bila salah satu ``v1`` cocok dan stempel waktunya masih dalam
    toleransi (mencegah kiriman lama diputar ulang)."""
    if not secret:
        return False
    timestamp, signatures = parse_signature_header(header)
    if not timestamp or not signatures:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    now = int(now if now is not None else time.time())
    if abs(now - ts) > tolerance:
        return False
    expected = compute_signature(secret, timestamp, raw_body)
    return any(hmac.compare_digest(expected, sig) for sig in signatures)
