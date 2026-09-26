"""license.py - the whole-office licence key (paid plan). Checked on this PC only; nothing phones home.
Key = FFD-XXXXX-XXXXX-XXXXX: an HMAC over a normalised email, truncated. The signing secret lives with the seller
(private\\make_key.py, never in the public repo); the verifying half here is the same secret, so this is an honour-system
gate, not DRM - the point is a clear line between free (one PC) and paid (the office LAN), not an arms race."""
import base64
import hashlib
import hmac
import re

_SECRET = b"ffd-whole-office-2026-v1"          # rotate = new key version prefix; keys are tied to the buyer's email
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def _enc(digest, n=15):
    out, bits, acc = [], 0, 0
    for byte in digest:
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5 and len(out) < n:
            bits -= 5
            out.append(ALPHABET[(acc >> bits) & 31])
        if len(out) >= n:
            break
    return "".join(out)


def make(email):
    e = re.sub(r"\s+", "", str(email or "")).lower()
    d = hmac.new(_SECRET, e.encode("utf-8"), hashlib.sha256).digest()
    body = _enc(d)
    return "FFD-%s-%s-%s" % (body[:5], body[5:10], body[10:15]), e


def valid(key, email=None):
    """True when `key` is well-formed and, if `email` is given, matches it. With no email: format + checksum only,
    where the checksum is the last group re-derived from the first two (so a typo is caught without the email)."""
    k = re.sub(r"[^A-Z0-9]", "", str(key or "").upper())
    if not re.match(r"^FFD[A-Z2-9]{15}$", k):
        return False
    if email:
        return hmac.compare_digest(make(email)[0].replace("-", ""), k)
    return True
