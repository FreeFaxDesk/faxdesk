"""road.py - FaxDesk Road: the office fax system on the team's phones. The PC is the hub; this module is its half.

What it does
  - keeps the OFFICE key pair (private key sealed with DPAPI like the Phone.com token) and registers with the relay
  - makes invite codes; lists / allows / blocks phones (devices)
  - PULLS envelopes from the relay (worker.py calls pull_once), decrypts them, and lands them in the ordinary inbox as
    "road" items (JPEG pages from a phone camera become one PDF), with the sender's name and note
  - SENDS a fax or document to a phone (sealed to that phone's key); the relay pokes the phone with a content-free push

Nothing readable ever sits on the relay: see roadcrypto.py. If 'cryptography' is missing, every call answers with a
plain message and the rest of FaxDesk is unaffected."""
import json
import re
import urllib.error
import urllib.request

from . import roadcrypto as rc
from .store import clean, now

RELAY_DEFAULT = "https://api.freefaxdesk.com/road"
MAX_PULL = 20
MAX_BYTES = 6 * 1024 * 1024


class Road:
    def __init__(self, store, relay=None):
        self.store = store
        self.relay = (relay or store.config().get("road_relay") or RELAY_DEFAULT).rstrip("/")
        self.last = {"pull": "", "pull_ok": None, "error": ""}

    # ------------------------------------------------------------ state
    def state(self):
        return self.store.read("road.json", {}) or {}

    def _save(self, st):
        self.store.write("road.json", st, private=True)

    def status(self, refresh=True):
        st = self.state()
        if refresh and st.get("office_id"):
            self.refresh_devices(); st = self.state()
        return {"ok": True, "available": rc.AVAILABLE, "enabled": bool(st.get("office_id")), "office_id": st.get("office_id", ""),
                "office_name": st.get("office_name", ""), "fingerprint": rc.fingerprint(st["pub"]) if st.get("pub") else "",
                "relay": self.relay, "last": self.last, "devices": st.get("devices") or [], "pending": sum(1 for d in (st.get("devices") or []) if d.get("status") == "pending")}

    # ------------------------------------------------------------ relay plumbing
    def _call(self, method, path, body=None, token=None, timeout=20):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        hdr = {"Accept": "application/json"}
        if data is not None:
            hdr["Content-Type"] = "application/json"
        if token:
            hdr["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.relay + path, data=data, method=method, headers=hdr)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.getcode(), json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                return e.code, {"ok": False, "error": "Relay answered %d." % e.code}
        except Exception as e:
            return 0, {"ok": False, "error": "Could not reach the relay (%s)." % type(e).__name__}

    def _token(self):
        st = self.state()
        return self.store.unseal(st.get("token_sealed", "")) if st.get("token_sealed") else ""

    def _priv(self):
        st = self.state()
        return json.loads(self.store.unseal(st["priv_sealed"])) if st.get("priv_sealed") else None

    # ------------------------------------------------------------ setup
    def enable(self, me):
        """Create the office key pair (once) and register with the relay. Idempotent."""
        if not rc.AVAILABLE:
            return 400, {"ok": False, "error": "Road needs the 'cryptography' package. The EXE has it; from source: pip install cryptography"}
        st = self.state()
        if st.get("office_id"):
            return 200, self.status(refresh=False)
        if not st.get("pub"):
            priv, pub = rc.new_keypair()
            st["pub"] = pub
            st["priv_sealed"] = self.store.seal(json.dumps(priv))
            st["created"] = now()
        name = self.store.config().get("office") or "FaxDesk office"
        code, r = self._call("POST", "/office/register", {"pub": st["pub"], "name": name})
        if code != 200 or not r.get("ok"):
            self._save(st)
            return 502, {"ok": False, "error": r.get("error") or "Relay refused the registration."}
        st.update(office_id=r["office_id"], office_name=name, token_sealed=self.store.seal(r["office_token"]), registered=now(), devices=[])
        self._save(st)
        self.store.audit("road_enable", me, "ok", r["office_id"])
        return 200, self.status()

    def disable(self, me):
        st = self.state()
        st.pop("office_id", None); st.pop("token_sealed", None); st["devices"] = []
        self._save(st)
        self.store.audit("road_disable", me, "ok", "")
        return 200, self.status()

    def invite(self, me, label):
        tok = self._token()
        if not tok:
            return 400, {"ok": False, "error": "Turn Road on first."}
        code, r = self._call("POST", "/office/invite", {"label": clean(label, 60)}, tok)
        if code != 200:
            return code or 502, r
        self.store.audit("road_invite", me, "ok", r.get("code", ""))
        link = (self.store.config().get("road_page") or "https://freefaxdesk.com/road.html") + "#" + r["code"]
        return 200, {"ok": True, "code": r["code"], "expires": r.get("expires", ""), "link": link}

    def sync_people(self, force=False):
        """Tell the relay the first names on the desk (names only) so a phone can address a person. Only when changed."""
        tok = self._token()
        if not tok:
            return
        people = [clean(x, 40) for x in (self.store.people() or []) if clean(x, 40)]
        st = self.state()
        if not force and st.get("people_sent") == people:
            return
        code, r = self._call("POST", "/office/people", {"people": people}, tok)
        if code == 200 and r.get("ok"):
            st["people_sent"] = people
            self._save(st)

    def refresh_devices(self):
        tok = self._token()
        if not tok:
            return []
        self.sync_people()
        code, r = self._call("GET", "/office", None, tok)
        if code == 200 and r.get("ok"):
            st = self.state(); st["devices"] = r.get("devices") or []; st["office_name"] = r.get("name") or st.get("office_name", "")
            for d in st["devices"]:
                try:
                    d["fp"] = rc.fingerprint(d["pub"]) if d.get("pub") else ""
                except Exception:
                    d["fp"] = ""
            self._save(st)
            return st["devices"]
        return self.state().get("devices") or []

    def device(self, me, device_id, status, fp=""):
        tok = self._token()
        if not tok:
            return 400, {"ok": False, "error": "Turn Road on first."}
        if status == "allow":                                                  # Admin review 2026-10-04: the office must read the phone's code
            code, r = self._call("GET", "/office", None, tok)
            dev = next((d for d in (r.get("devices") or []) if d.get("device_id") == device_id), None) if code == 200 else None
            if not dev or not dev.get("pub"):
                return 502, {"ok": False, "error": "The relay did not give that phone's key. Try again in a moment."}
            want = rc.fingerprint(dev["pub"]).replace("-", "").lower()
            got = clean(fp, 30).replace("-", "").replace(" ", "").lower()
            if len(got) < 4 or not want.startswith(got):
                self.store.audit("road_device_allow", me, "fingerprint_mismatch", device_id)
                return 400, {"ok": False, "error": "That code does not match this phone. Read the code on the phone's 'This phone' screen (first 4 characters are enough). If it never matches, press Block - someone may be in the middle."}
        code, r = self._call("POST", "/office/device", {"device_id": clean(device_id, 40), "status": status}, tok)
        if code == 200:
            self.store.audit("road_device_" + status, me, "ok", device_id)
            self.refresh_devices()
        return code or 502, r

    # ------------------------------------------------------------ pull: phone -> office
    def pull_once(self):
        """Fetch, decrypt and file every envelope waiting for the office. Returns how many landed."""
        tok = self._token(); priv = self._priv()
        if not tok or not priv:
            return 0
        code, r = self._call("GET", "/env", None, tok)
        if code != 200 or not r.get("ok"):
            self.last.update(pull=now(), pull_ok=False, error=r.get("error", "relay"))
            return 0
        n = 0
        for head in (r.get("rows") or [])[:MAX_PULL]:
            code, e = self._call("GET", "/env/" + head["id"], None, tok, timeout=60)
            if code != 200 or not e.get("ok"):
                continue
            try:
                meta, data = rc.unpack(rc.decrypt(priv, e["body"], rc.aad_for(self.state().get("office_id"), head.get("from"))))
            except Exception as ex:
                self.store.audit("road_bad_envelope", "", type(ex).__name__, head["id"])
                self._call("DELETE", "/env/" + head["id"], None, tok)          # cannot be read by anyone; drop it
                continue
            try:
                self._land(head, meta, data)
                n += 1
            except Exception as ex:
                self.store.audit("road_land_failed", "", type(ex).__name__, head["id"])
                continue
            self._call("DELETE", "/env/" + head["id"], None, tok)
        self.last.update(pull=now(), pull_ok=True, error="")
        return n

    def _land(self, head, meta, data):
        """A document from a phone becomes an inbox item like a fax: inbox/<id>.pdf + <id>.json with source 'road'."""
        st = self.store
        kind = clean(meta.get("kind"), 20) or "doc"
        pages = meta.get("pages")
        if isinstance(pages, list) and pages:                             # camera pages: list of base64 JPEGs -> one PDF
            jp = [rc._b64d(p.replace("+", "-").replace("/", "_")) for p in pages[:40] if isinstance(p, str)]   # phone sends base64url, no padding
            pdf = jpegs_to_pdf(jp)
            npages = len(jp)
        elif data[:5] == b"%PDF-":
            pdf, npages = bytes(data), None
        elif data[:3] == b"\xff\xd8\xff":
            pdf, npages = jpegs_to_pdf([bytes(data)]), 1
        else:
            raise ValueError("not a PDF or JPEG")
        if len(pdf) > MAX_BYTES:
            raise ValueError("too big")
        fid = "rd-" + head["id"].replace("env_", "")[:24]
        (st.root / "inbox" / (fid + ".pdf")).write_bytes(pdf)
        who = clean(meta.get("from") or head.get("from_name"), 60) or "phone"
        m = {"id": fid, "received": head.get("at") or now(), "from": "", "from_name": who + " (road)", "to": st.config().get("fax_number") or "",
             "pages": npages, "source": "road", "road_device": head.get("from", ""), "subject": clean(meta.get("name"), 80),
             "note": clean(meta.get("note"), 300), "to_person": clean(meta.get("to"), 40),
             "opened_by": "", "opened_at": "", "assigned_to": "", "assigned_by": "", "assigned_at": "",
             "handled": False, "handled_by": "", "handled_at": "", "handled_note": "", "spam": False, "pulled_at": now()}
        if m["to_person"] and m["to_person"] in (st.people() or []):
            m.update(assigned_to=m["to_person"], assigned_by="road", assigned_at=now())
        st.write("inbox/" + fid + ".json", m)
        st.audit("road_in", who, "pages_%s" % (npages or "pdf"), fid)

    # ------------------------------------------------------------ send: office -> phone
    def send(self, me, device_id, pdf_bytes, meta):
        """Seal a PDF to one phone. meta: name, from, kind (fax|doc), pages, note."""
        tok = self._token()
        if not tok:
            return 400, {"ok": False, "error": "Turn Road on first."}
        dev = next((d for d in (self.state().get("devices") or []) if d.get("device_id") == device_id), None)
        if not dev:
            self.refresh_devices()
            dev = next((d for d in (self.state().get("devices") or []) if d.get("device_id") == device_id), None)
        if not dev or dev.get("status") != "allowed":
            return 404, {"ok": False, "error": "That phone is not on the allowed list."}
        if not pdf_bytes or pdf_bytes[:5] != b"%PDF-" or len(pdf_bytes) > MAX_BYTES:
            return 400, {"ok": False, "error": "Only PDFs up to 6 MB can go to a phone."}
        pub = dev.get("pub")
        if not pub:                                                        # the office list carries no keys; fetch them
            code, r = self._call("GET", "/office", None, tok)
            pub = next((d.get("pub") for d in (r.get("devices") or []) if d.get("device_id") == device_id), None) if code == 200 else None
        if not pub:
            return 502, {"ok": False, "error": "The relay did not give that phone's key."}
        clean_meta = {"name": clean(meta.get("name"), 80), "from": clean(meta.get("from"), 80), "kind": clean(meta.get("kind"), 20) or "doc",
                      "pages": meta.get("pages"), "note": clean(meta.get("note"), 300), "sent_by": me, "at": now()}
        env = rc.encrypt_to(pub, rc.pack(clean_meta, pdf_bytes), rc.aad_for(self.state().get("office_id"), device_id))
        code, r = self._call("POST", "/env", {"to": device_id, "kind": clean_meta["kind"], "size": len(pdf_bytes), "body": env}, tok, timeout=90)
        if code != 200:
            return code or 502, r
        self.store.audit("road_out", me, clean_meta["kind"], device_id)
        return 200, {"ok": True, "id": r.get("id"), "device": dev.get("name")}


def jpegs_to_pdf(jpegs):
    """Phone camera pages -> one letter-size PDF, each JPEG fitted to the page (stdlib only)."""
    from .pdfmini import jpeg_size
    W, H = 612.0, 792.0
    objs = []

    def add(b):
        objs.append(b); return len(objs)
    page_ids = []
    kids_placeholder = add(None)                      # pages object, filled at the end
    for jp in jpegs:
        dims = jpeg_size(jp) or (1275, 1650)
        w, h = dims
        scale = min((W - 36) / w, (H - 36) / h)
        dw, dh = w * scale, h * scale
        x, y = (W - dw) / 2, (H - dh) / 2
        img = add(("<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n" % (w, h, len(jp))).encode("latin-1") + jp + b"\nendstream")
        content = ("q %.2f 0 0 %.2f %.2f %.2f cm /Im%d Do Q" % (dw, dh, x, y, img)).encode("latin-1")
        cid = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        pid = add(("<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Resources << /XObject << /Im%d %d 0 R >> >> /Contents %d 0 R >>" % (kids_placeholder, img, img, cid)).encode("latin-1"))
        page_ids.append(pid)
    objs[kids_placeholder - 1] = ("<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join("%d 0 R" % p for p in page_ids), len(page_ids))).encode("latin-1")
    cat = add(("<< /Type /Catalog /Pages %d 0 R >>" % kids_placeholder).encode("latin-1"))
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offs = []
    for i, body in enumerate(objs, 1):
        offs.append(len(out))
        out += ("%d 0 obj\n" % i).encode("latin-1") + body + b"\nendobj\n"
    xref = len(out)
    out += ("xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)).encode("latin-1")
    for o in offs:
        out += ("%010d 00000 n \n" % o).encode("latin-1")
    out += ("trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, cat, xref)).encode("latin-1")
    return bytes(out)
