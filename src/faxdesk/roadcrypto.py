"""roadcrypto.py - the one place Road touches cryptography. Envelope format shared with the phone page (WebCrypto):

  ECDH P-256 (ephemeral sender key x recipient public key) -> HKDF-SHA256(salt = iv, info = "ffd-road-v1") -> AES-256-GCM.
  Envelope = {"v": 1, "ek": <sender ephemeral public key, JWK>, "iv": b64, "ct": b64}   (ct carries the GCM tag at the end)

Keys are JWK dicts (kty EC / crv P-256 / x / y [/ d]) so the phone and the PC speak the same words. The office private
key is sealed by store.py like the Phone.com token; it never leaves the PC.

Needs the 'cryptography' package (the only non-stdlib piece of FaxDesk; bundled in the EXE). If it is missing, Road is
off and the page says so - the fax desk itself keeps working."""
import base64
import json
import os

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    AVAILABLE = True
except Exception:                                   # pragma: no cover - the EXE always has it
    AVAILABLE = False

INFO = b"ffd-road-v1"


def aad_for(office_id, device_id):
    """The addresses baked into every envelope (AES-GCM additional data): an envelope opens only for the office/device pair
    it was sealed for, so a relabelled or misrouted envelope fails to open instead of landing in the wrong inbox."""
    return ("ffd-road-v1|%s|%s" % (office_id or "", device_id or "")).encode("ascii", "replace")


def _b64e(b):
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64d(s):
    s = str(s or "")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _need():
    if not AVAILABLE:
        raise RuntimeError("The 'cryptography' package is not installed, so Road is off. Install it (pip install cryptography) or use the EXE.")


def new_keypair():
    """-> (private_jwk, public_jwk). P-256, the curve every phone browser has in hardware-backed WebCrypto."""
    _need()
    k = ec.generate_private_key(ec.SECP256R1())
    nums = k.private_numbers()
    pub = {"kty": "EC", "crv": "P-256", "x": _b64e(nums.public_numbers.x.to_bytes(32, "big")), "y": _b64e(nums.public_numbers.y.to_bytes(32, "big"))}
    priv = dict(pub, d=_b64e(nums.private_value.to_bytes(32, "big")))
    return priv, pub


def _load_pub(jwk):
    if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise ValueError("Not a P-256 public key.")
    x = int.from_bytes(_b64d(jwk["x"]), "big"); y = int.from_bytes(_b64d(jwk["y"]), "big")
    return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()


def _load_priv(jwk):
    pub = _load_pub(jwk)
    d = int.from_bytes(_b64d(jwk["d"]), "big")
    return ec.EllipticCurvePrivateNumbers(d, pub.public_numbers()).private_key()


def fingerprint(pub_jwk):
    """Short human-checkable id of a public key (what the phone and the PC show each other at enrolment)."""
    import hashlib
    raw = _b64d(pub_jwk["x"]) + _b64d(pub_jwk["y"])
    h = hashlib.sha256(raw).hexdigest()[:20]
    return "-".join(h[i:i + 4] for i in range(0, 20, 4))


def _kdf(shared, iv):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=iv, info=INFO).derive(shared)


def encrypt_to(recipient_pub_jwk, plaintext, aad=b""):
    """Seal bytes to a recipient's public key. Returns the envelope dict (JSON-able)."""
    _need()
    rpub = _load_pub(recipient_pub_jwk)
    eph = ec.generate_private_key(ec.SECP256R1())
    shared = eph.exchange(ec.ECDH(), rpub)
    iv = os.urandom(12)
    key = _kdf(shared, iv)
    ct = AESGCM(key).encrypt(iv, bytes(plaintext), aad or None)
    en = eph.public_key().public_numbers()
    ek = {"kty": "EC", "crv": "P-256", "x": _b64e(en.x.to_bytes(32, "big")), "y": _b64e(en.y.to_bytes(32, "big"))}
    return {"v": 1, "ek": ek, "iv": _b64e(iv), "ct": _b64e(ct)}


def decrypt(private_jwk, envelope, aad=b""):
    """Open an envelope made for our public key. Raises on tamper or wrong key."""
    _need()
    if not isinstance(envelope, dict) or envelope.get("v") != 1:
        raise ValueError("Not a Road envelope.")
    priv = _load_priv(private_jwk)
    shared = priv.exchange(ec.ECDH(), _load_pub(envelope["ek"]))
    iv = _b64d(envelope["iv"])
    key = _kdf(shared, iv)
    return AESGCM(key).decrypt(iv, _b64d(envelope["ct"]), aad or None)


def pack(meta, data):
    """What goes INSIDE an envelope: a small JSON header (sender name, filename, kind...) + the file bytes.
    Layout: 4-byte big-endian header length, header JSON (utf-8), file bytes."""
    h = json.dumps(meta, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return len(h).to_bytes(4, "big") + h + bytes(data)


HEADER_MAX = 9 * 1024 * 1024       # the phone's camera pages travel INSIDE the header (base64 JPEGs, up to 6 MB raw)


def unpack(blob):
    n = int.from_bytes(blob[:4], "big")
    if n > HEADER_MAX or n > len(blob) - 4:
        raise ValueError("Bad Road payload.")
    return json.loads(blob[4:4 + n].decode("utf-8")), blob[4 + n:]


def _selftest():
    priv, pub = new_keypair()
    env = encrypt_to(pub, b"hello road", b"aad1")
    assert decrypt(priv, env, b"aad1") == b"hello road"
    big = pack({"pages": ["A" * 400000, "B" * 400000]}, b"")               # two real phone photos: header far past 64 KB
    assert len(unpack(big)[0]["pages"]) == 2
    try:
        decrypt(priv, env, b"aad2"); raise AssertionError("aad not checked")
    except Exception as e:
        assert not isinstance(e, AssertionError)
    priv2, _ = new_keypair()
    try:
        decrypt(priv2, env, b"aad1"); raise AssertionError("wrong key accepted")
    except Exception as e:
        assert not isinstance(e, AssertionError)
    m, d = unpack(pack({"name": "x.pdf", "from": "Dr. Example"}, b"%PDF-1.4"))
    assert m["name"] == "x.pdf" and d == b"%PDF-1.4"
    assert len(fingerprint(pub)) == 24
    print("roadcrypto: ok", fingerprint(pub))


if __name__ == "__main__":
    _selftest()
