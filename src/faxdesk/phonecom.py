"""
phonecom.py - the ONLY thing in Free Fax Desk that talks to Phone.com. Stdlib only. ASCII only. No estate imports.

    c = Client(token, voip_id, extension, from_number="+1..." or None)
    c.check()                         -> {"ok", "accounts": [...]} : is the token good, which accounts it sees
    c.numbers()                       -> [{"number", "name"}]     : fax-capable numbers on the account
    c.send(pdf_bytes, to, filename)   -> {"ok", "fax_id", "pages", "status", "error"}
    c.received(limit=200)             -> [{"id", "from", "from_name", "to", "pages", "created_at", "download_url"}]
    c.download(url)                   -> bytes (PDF)              : never follows a redirect off api.phone.com with the token

The token is held in memory only; nothing here logs, prints or stores it. Errors come back as plain-English strings.
Run `python -m faxdesk.phonecom --selftest` (fake server on loopback; no network).
"""
import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.phone.com"
E164 = re.compile(r"^\+1\d{10}$")


def to_e164(raw):
    d = re.sub(r"\D", "", str(raw or ""))
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    return "+1" + d if len(d) == 10 else ""


class _NoCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """Phone.com hands out download links on other hosts (storage). The bearer token must not follow them."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = urllib.request.HTTPRedirectHandler.redirect_request(self, req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


class Client:
    def __init__(self, token, voip_id, extension, from_number=None, api=API, timeout=60):
        self._token = str(token or "").strip()
        self.voip_id = str(voip_id or "").strip()
        self.extension = int(extension) if str(extension or "").strip().isdigit() else None
        self.from_number = to_e164(from_number) if from_number else ""
        self.api = api.rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoCrossHostRedirect())

    # ----------------------------------------------------------------- plumbing
    def _call(self, method, path, body=None, timeout=None):
        url = self.api + path if path.startswith("/") else path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Authorization": "Bearer " + self._token, "Accept": "application/json",
                                              **({"Content-Type": "application/json"} if data else {})})
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
                return r.getcode(), (json.loads(raw.decode("utf-8")) if raw else {}), ""
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8", "replace"))
                detail = detail.get("message") or detail.get("error") or json.dumps(detail)[:200]
            except Exception:
                detail = ""
            return e.code, {}, _plain(e.code, detail)
        except urllib.error.URLError as e:
            return 0, {}, "Could not reach Phone.com (%s). Check the internet connection." % (getattr(e, "reason", "") or "network")
        except (TimeoutError, OSError) as e:
            return 0, {}, "Phone.com did not answer in time (%s)." % type(e).__name__

    # ----------------------------------------------------------------- calls
    def check(self):
        code, j, err = self._call("GET", "/v4/accounts?limit=100")
        if err:
            return {"ok": False, "error": err, "accounts": []}
        items = j.get("items") or []
        return {"ok": True, "accounts": [{"id": str(i.get("id") or ""), "name": str(i.get("name") or "")} for i in items],
                "sees_configured": any(str(i.get("id")) == self.voip_id for i in items) if self.voip_id else False}

    def numbers(self):
        code, j, err = self._call("GET", "/v4/accounts/%s/phone-numbers?limit=500" % self.voip_id)
        if err:
            return []
        return [{"number": to_e164(i.get("phone_number") or i.get("number")), "name": str(i.get("name") or "")}
                for i in (j.get("items") or [])]

    def send(self, pdf_bytes, to, filename="document.pdf", quality="high", extra=None):
        """pdf_bytes = the document; extra = optional list of (filename, pdf_bytes) sent FIRST (a cover sheet)."""
        to = to_e164(to)
        if not to:
            return {"ok": False, "error": "That is not a 10-digit US fax number."}
        if not pdf_bytes or pdf_bytes[:5] != b"%PDF-":
            return {"ok": False, "error": "Only PDF files can be faxed."}
        if self.extension is None:
            return {"ok": False, "error": "No Phone.com extension is set up yet."}
        media = [{"filename": re.sub(r"[^A-Za-z0-9._-]", "_", fn)[:48] or "cover.pdf", "data": base64.b64encode(b).decode("ascii")}
                 for fn, b in (extra or []) if b and b[:5] == b"%PDF-"]
        media.append({"filename": re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:48] or "document.pdf",
                      "data": base64.b64encode(pdf_bytes).decode("ascii")})
        body = {"to": to, "extension": self.extension, "quality": quality, "media": media}
        if self.from_number:
            body["from"] = self.from_number
        code, j, err = self._call("POST", "/v4/accounts/%s/fax" % self.voip_id, body, timeout=120)
        if err:
            return {"ok": False, "error": err, "status": code}
        return {"ok": True, "fax_id": str(j.get("id") or ""), "pages": j.get("pages"), "status": code, "error": ""}

    def status(self, fax_id):
        """v1.1: one fax by id. Returns {"ok", "status", "reason"} - status/reason are Phone.com's words, lower-cased, or
        ok=False with a plain error when Phone.com could not be asked. Used by the delivery watch after a fax is accepted."""
        fid = re.sub(r"[^A-Za-z0-9_-]", "", str(fax_id or ""))
        if not fid:
            return {"ok": False, "error": "no fax id"}
        code, j, err = self._call("GET", "/v4/accounts/%s/fax/%s" % (self.voip_id, fid), timeout=45)
        if err:
            return {"ok": False, "error": err}
        st, why = "", ""
        for k in ("status", "state", "delivery_status", "result"):
            v = j.get(k)
            if isinstance(v, dict):
                v = v.get("status") or v.get("state") or v.get("name")
            if v:
                st = str(v); break
        for k in ("error", "error_message", "reason", "failure_reason", "status_reason", "message", "detail"):
            v = j.get(k)
            if isinstance(v, dict):
                v = v.get("message") or v.get("reason") or v.get("text")
            if v:
                why = str(v); break
        return {"ok": True, "status": st.strip().lower(), "reason": why.strip()}

    def received(self, limit=200, max_pages=25):
        """All received faxes the account knows about (Phone.com lists in/out together; ids are not time-ordered)."""
        out, offset = [], 0
        for _ in range(max_pages):
            code, j, err = self._call("GET", "/v4/accounts/%s/fax?limit=%d&offset=%d&sort[id]=desc" % (self.voip_id, min(limit, 500), offset))
            if err or not j.get("items"):
                break
            for i in j["items"]:
                if i.get("direction") != "in":
                    continue
                frm = i.get("from")
                to_ = i.get("to")
                out.append({"id": str(i.get("id") or ""), "from": to_e164(frm.get("number") if isinstance(frm, dict) else frm),
                            "from_name": str((frm.get("name") if isinstance(frm, dict) else "") or ""),
                            "to": to_e164(to_.get("number") if isinstance(to_, dict) else to_),
                            "pages": i.get("pages"), "created_at": i.get("created_at"), "download_url": str(i.get("download_url") or "")})
            if len(j["items"]) < min(limit, 500) or j.get("total") is not None and offset + len(j["items"]) >= int(j["total"]):
                break
            offset += len(j["items"])
        return out

    def download(self, url):
        if not str(url).startswith(("https://", "http://")):
            return b""
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self._token} if urllib.parse.urlsplit(url).netloc == urllib.parse.urlsplit(self.api).netloc else {})
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                data = r.read()
            return data if data[:5] == b"%PDF-" else b""
        except Exception:
            return b""


def _plain(code, detail):
    return {401: "Phone.com rejected the API token. Check it in Setup.", 403: "This token is not allowed to do that (check the account id and extension).",
            404: "Phone.com could not find that account or extension.", 422: "Phone.com refused the fax: %s" % (detail or "check the number and the PDF"),
            429: "Phone.com is rate-limiting us. It will be retried."}.get(code, "Phone.com error %s%s" % (code, (": " + detail) if detail else ""))


# --------------------------------------------------------------------- selftest (fake server, no network)
def _selftest():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen = []

    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _j(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

        def do_GET(self):
            seen.append(("GET", self.path, self.headers.get("Authorization")))
            if self.headers.get("Authorization") != "Bearer good":
                return self._j(401, {"message": "bad token"})
            if self.path.startswith("/v4/accounts?"):
                return self._j(200, {"items": [{"id": 123, "name": "Test Office"}]})
            if "/phone-numbers" in self.path:
                return self._j(200, {"items": [{"phone_number": "+15555550100", "name": "Fax"}]})
            if "/fax?" in self.path:
                off = int(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)["offset"][0])
                items = [{"id": 900 + off, "direction": "in", "from": {"number": "5555550199", "name": "Sender"}, "to": {"number": "+15555550100"}, "pages": 2, "created_at": "2026-01-01T00:00:00", "download_url": "http://127.0.0.1:%d/file.pdf" % self.server.server_address[1]},
                         {"id": 800 + off, "direction": "out", "to": "+15555550101"}] if off == 0 else []
                return self._j(200, {"items": items, "total": 2})
            if self.path == "/file.pdf":
                b = b"%PDF-1.4 fake"; self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
            self._j(404, {})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            seen.append(("POST", self.path, body))
            if body["to"] == "+15555550422":
                return self._j(422, {"message": "number not allowed"})
            self._j(201, {"id": "fx1", "pages": 3})
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    c = Client("good", "123", "7", "+15555550100", api=base)
    assert to_e164("(555) 555-0100") == "+15555550100" and to_e164("1 555 555 0100") == "+15555550100" and to_e164("555") == ""
    r = c.check(); assert r["ok"] and r["sees_configured"] and r["accounts"][0]["name"] == "Test Office"
    assert Client("bad", "123", "7", api=base).check()["error"].startswith("Phone.com rejected the API token")
    assert c.numbers() == [{"number": "+15555550100", "name": "Fax"}]
    r = c.send(b"%PDF-1.4 x", "555-555-0101", "My Report.pdf"); assert r["ok"] and r["fax_id"] == "fx1" and r["pages"] == 3
    posted = seen[-1][2]; assert posted["from"] == "+15555550100" and posted["extension"] == 7 and posted["media"][0]["filename"] == "My_Report.pdf"
    assert base64.b64decode(posted["media"][0]["data"]) == b"%PDF-1.4 x"
    assert not c.send(b"hello", "6615550101")["ok"] and not c.send(b"%PDF-", "12")["ok"]
    assert c.send(b"%PDF-1", "5555550422")["error"].startswith("Phone.com refused the fax: number not allowed")
    rx = c.received(); assert len(rx) == 1 and rx[0]["from"] == "+15555550199" and rx[0]["pages"] == 2
    assert c.download(rx[0]["download_url"]) == b"%PDF-1.4 fake"
    assert c.download("file:///etc/passwd") == b""
    assert Client("good", "123", "7", api="http://127.0.0.1:9").check()["error"].startswith("Could not reach Phone.com")
    srv.shutdown()
    open(__file__, encoding="ascii").read()                                  # pure ASCII
    print("phonecom selftest: ALL PASS")
    print("  ok  check / numbers / send (from, extension, filename, base64 PDF) / plain-English errors")
    print("  ok  received (direction in only, all pages) / download (PDF sniff, no token off-host, no file: urls)")
    print("  ok  pure ASCII, stdlib only")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
