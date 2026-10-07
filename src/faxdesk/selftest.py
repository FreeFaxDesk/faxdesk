"""selftest.py - Free Fax Desk end to end against a FAKE Phone.com on loopback. Temp state, no network, no real numbers."""
import base64
import json
import os
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import pdfmini


def _j(base, method, path, obj=None, name="Pat"):
    req = urllib.request.Request(base + path, data=(json.dumps(obj).encode() if obj is not None else None), method=method,
                                 headers={"Content-Type": "application/json", "Cookie": "fd_name=" + name})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def run():
    calls = {"send": 0, "fail_next": 0}
    pdf_in = pdfmini.build([["## incoming test fax", "hello"]], "in")

    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _out(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/road/"):
                return self._road({})
            if self.headers.get("Authorization") != "Bearer TESTTOKEN":
                return self._out(401, {"error": "bad token"})
            if self.path.startswith("/v4/accounts?"):
                return self._out(200, {"items": [{"id": "999", "name": "Test Office"}]})
            if "/phone-numbers" in self.path:
                return self._out(200, {"items": [{"phone_number": "+15555550100", "name": "Fax"}]})
            if "/fax?" in self.path:
                if "offset=0" in self.path:
                    return self._out(200, {"items": [{"id": 501, "direction": "in", "from": {"number": "+18185550123", "name": "Dr Test"}, "to": "+15555550100", "pages": 1, "created_at": "2026-09-26T10:00:00", "download_url": "http://127.0.0.1:%d/dl/501" % self.server.server_address[1]},
                                                     {"id": 500, "direction": "out", "from": "+15555550100", "to": "+18185550123", "pages": 1, "created_at": "2026-09-25T10:00:00"}], "total": 2})
                return self._out(200, {"items": [], "total": 2})
            if "/fax/" in self.path and self.path.rsplit("/", 1)[1].isdigit():     # v1.1 delivery watch: one fax by id
                fid = int(self.path.rsplit("/", 1)[1])
                calls["status"] = calls.get("status", 0) + 1
                if fid == calls.get("undeliver_id"):
                    return self._out(200, {"id": fid, "status": "failed", "error": {"message": "No answer from the receiving fax"}})
                return self._out(200, {"id": fid, "status": "delivered"})
            if self.path.startswith("/dl/"):
                return self._out(200, pdf_in, "application/pdf")
            return self._out(404, {})

        def _road(self, body):                                   # a tiny stand-in for relay.js (same contract, no crypto of its own)
            R = calls.setdefault("relay", {"offices": {}, "devices": {}, "inv": {}, "env": {}, "tok": {}})
            p = self.path.split("?")[0]; tok = (self.headers.get("Authorization") or "")[7:]; me = R["tok"].get(tok)
            def out(code, o): return self._out(code, o)
            if p == "/road/office/register":
                oid = "off_%d" % (len(R["offices"]) + 1); t = "OT" + oid; R["offices"][oid] = {"pub": body["pub"], "name": body["name"]}; R["tok"][t] = ("office", oid); return out(200, {"ok": True, "office_id": oid, "office_token": t})
            if p == "/road/device/enroll":
                inv = R["inv"].pop(body.get("code"), None)
                if not inv: return out(400, {"ok": False, "error": "bad code"})
                did = "dev_%d" % (len(R["devices"]) + 1); t = "DT" + did; R["devices"][did] = {"device_id": did, "office_id": inv, "name": body.get("name"), "pub": body["pub"], "status": "pending"}; R["tok"][t] = ("device", did)
                return out(200, {"ok": True, "device_id": did, "device_token": t, "office_id": inv, "office_pub": R["offices"][inv]["pub"], "status": "pending"})
            if not me: return out(401, {"ok": False, "error": "no"})
            kind, mid = me; oid = mid if kind == "office" else R["devices"][mid]["office_id"]
            if p == "/road/office" and self.command == "GET":
                devs = [d for d in R["devices"].values() if d["office_id"] == oid]; return out(200, {"ok": True, "office_id": oid, "name": R["offices"][oid]["name"], "devices": devs, "pending": sum(1 for d in devs if d["status"] == "pending")})
            if p == "/road/office/people":
                R["offices"][oid]["people"] = list(body.get("people") or []); return out(200, {"ok": True, "people": R["offices"][oid]["people"]})
            if p == "/road/office/invite":
                code = "CODE%04d" % len(R["inv"]); R["inv"][code] = oid; return out(200, {"ok": True, "code": code, "expires": "soon"})
            if p == "/road/office/device":
                d = R["devices"].get(body.get("device_id"))
                if not d or d["office_id"] != oid: return out(404, {"ok": False, "error": "no"})
                d["status"] = "allowed" if body.get("status") == "allow" else "blocked"; return out(200, {"ok": True, "device": d})
            if p == "/road/env" and self.command == "POST":
                if kind == "device" and R["devices"][mid]["status"] != "allowed": return out(403, {"ok": False, "error": "pending"})
                eid = "env_%d" % (len(R["env"]) + 1); to = "office" if kind == "device" else body["to"]
                R["env"][eid] = {"head": {"id": eid, "office_id": oid, "from": "office" if kind == "office" else mid, "from_name": "office" if kind == "office" else R["devices"][mid]["name"], "to": to, "kind": body.get("kind"), "size": body.get("size"), "at": "2026-10-03T10:00:00"}, "body": body["body"]}
                return out(200, {"ok": True, "id": eid})
            if p == "/road/env" and self.command == "GET":
                to = "office" if kind == "office" else mid; return out(200, {"ok": True, "rows": [e["head"] for e in R["env"].values() if e["head"]["office_id"] == oid and e["head"]["to"] == to]})
            if p.startswith("/road/env/"):
                eid = p.rsplit("/", 1)[1]; e = R["env"].get(eid)
                if self.command == "DELETE": R["env"].pop(eid, None); return out(200, {"ok": True})
                return out(200, {"ok": True, "head": e["head"], "body": e["body"]}) if e else out(404, {"ok": False})
            return out(404, {"ok": False, "error": "nf"})

        def do_DELETE(self):
            return self._road({})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
            if self.path.startswith("/road/"):
                return self._road(body)
            calls["send"] += 1; calls["last"] = body
            if calls["fail_next"]:
                calls["fail_next"] -= 1
                return self._out(500, {"error": "line busy"})
            return self._out(201, {"id": 7000 + calls["send"], "pages": len(body.get("media") or [])})

    fake = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    api = "http://127.0.0.1:%d" % fake.server_address[1]
    tmp = Path(tempfile.mkdtemp(prefix="faxdesk_st_"))
    os.environ["FAXDESK_STATE"] = str(tmp)
    from .store import Store
    from .server import make
    st = Store(tmp)
    app, srv = make(st, host="127.0.0.1", port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    ok = []
    try:
        code, r = _j(base, "GET", "/api/state", name="")
        assert r["ok"] and not r["setup_done"] and r["people"] == []
        code, r = _j(base, "POST", "/api/people", {"people": ["Pat", "Sam", "pat"]}, name="")
        assert r["people"] == ["Pat", "Sam"]
        code, r = _j(base, "POST", "/api/setup", {"office": "Test Office", "token": "TESTTOKEN", "voip_id": "999", "extension": "12", "fax_number": "555-555-0100", "api": api})
        assert r["ok"] and r["setup_done"], r
        cfg = json.loads((tmp / "config.json").read_text())
        assert "TESTTOKEN" not in json.dumps(cfg) or os.name != "nt" or False        # sealed (dpapi on Windows; base64 elsewhere)
        assert cfg["token"].startswith(("dpapi:", "plain:")) and st.token() == "TESTTOKEN"
        for f in tmp.rglob("*"):
            if f.is_file() and f.name != "config.json":
                assert b"TESTTOKEN" not in f.read_bytes(), f
        code, r = _j(base, "POST", "/api/test", {})
        assert r["ok"] and r["sees_configured"] and r["numbers"][0]["number"] == "+15555550100", r
        ok.append("setup: people, sealed token (only in config.json), Phone.com test")

        doc = pdfmini.build([["## page one"], ["## page two"]], "doc")
        code, r = _j(base, "POST", "/api/send", {"to": "(818) 555-0123", "to_name": "Dr Test", "subject": "Records", "note": "see attached", "pdf": base64.b64encode(doc).decode(), "filename": "records.pdf", "save_to_book": True})
        assert r["ok"] and r["pages"] == 3, r                                       # 2 pages + cover
        code, o = _j(base, "GET", "/api/outbox")
        assert o["rows"][0]["status"] == "queued" and calls["send"] == 0           # the page never transmits
        app.worker.transmit_once()
        code, o = _j(base, "GET", "/api/outbox")
        assert o["rows"][0]["status"] == "sent" and calls["send"] == 1 and len(calls["last"]["media"]) == 2 and calls["last"]["media"][0]["filename"] == "cover.pdf"
        code, L = _j(base, "GET", "/api/log")
        assert L["rows"][0]["status"] == "sent" and L["rows"][0]["to"] == "+18185550123"
        code, s2 = _j(base, "GET", "/api/state")
        assert any(b["fax"] == "+18185550123" and b["name"] == "Dr Test" for b in s2["book"])
        req = urllib.request.Request(base + "/api/confirm?id=" + o["rows"][0]["id"])
        with urllib.request.urlopen(req, timeout=10) as rr:
            assert rr.headers.get("Content-Type") == "application/pdf" and rr.read()[:5] == b"%PDF-"
        ok.append("send: queue -> worker -> sent with cover; log; book; confirmation PDF")

        calls["fail_next"] = 3
        code, r = _j(base, "POST", "/api/send", {"to": "8185550124", "pdf": base64.b64encode(doc).decode(), "cover": False})
        fid = r["id"]
        for _ in range(3):
            m = st.read("outbox/%s.json" % fid, {}); m["not_before"] = ""; st.write("outbox/%s.json" % fid, m)
            app.worker.transmit_once()
        m = st.read("outbox/%s.json" % fid, {})
        assert m["status"] == "failed" and m["tries"] == 3 and "busy" in m["error"], m
        code, r = _j(base, "POST", "/api/resend", {"id": fid})
        app.worker.transmit_once()
        assert st.read("outbox/%s.json" % fid, {})["status"] == "sent"
        ok.append("send: 3 tries then failed with the reason; resend works")

        # v1.1: Phone.com accepted both; the far machine refused one later -> failed with the reason, Resend; the other says delivered
        code, r = _j(base, "POST", "/api/send", {"to": "8185550125", "pdf": base64.b64encode(doc).decode(), "cover": False}); fid2 = r["id"]
        code, r = _j(base, "POST", "/api/send", {"to": "8185550126", "pdf": base64.b64encode(doc).decode(), "cover": False}); fid3 = r["id"]
        app.worker.transmit_once()
        m2 = st.read("outbox/%s.json" % fid2, {}); m3 = st.read("outbox/%s.json" % fid3, {})
        assert m2["status"] == "sent" and m2["fax_id"] and m3["status"] == "sent" and m3["fax_id"], (m2, m3)
        calls["undeliver_id"] = int(m2["fax_id"])
        n = app.worker.watch_once()
        assert n >= 2, n
        m2 = st.read("outbox/%s.json" % fid2, {}); m3 = st.read("outbox/%s.json" % fid3, {})
        assert m2["status"] == "failed" and m2["error"].startswith("not delivered: No answer") and m2["delivery"]["final"] and m2["delivery"]["ok"] is False, m2
        assert m3["status"] == "sent" and m3["delivery"]["final"] and m3["delivery"]["ok"] is True, m3
        code, L = _j(base, "GET", "/api/log")
        assert any(x.get("undelivered") and x["id"] == fid2 for x in L["rows"]), L["rows"][:3]
        assert app.worker.watch_once() == 0                                          # final ones are not asked again
        code, r = _j(base, "POST", "/api/resend", {"id": fid2})
        assert r["ok"] and st.read("outbox/%s.json" % fid2, {})["status"] == "queued"
        app.worker.transmit_once(); calls["undeliver_id"] = 0
        assert app.worker.watch_once() >= 1 and st.read("outbox/%s.json" % fid2, {})["delivery"]["ok"] is True   # the resend is watched afresh
        ok.append("delivery watch: accepted-then-undelivered -> failed + plain reason + resend; delivered -> marked; asked once")

        # ---- Road: office key, invite, phone enrols (roadcrypto plays the phone), allow, phone -> office pages land as a PDF, office -> phone
        from . import roadcrypto as rc
        from .road import jpegs_to_pdf
        app.road.relay = api + "/road"
        code, r = _j(base, "POST", "/api/road/enable", {})
        assert r["ok"] and r["enabled"] and r["fingerprint"], r
        assert "priv_sealed" in st.read("road.json", {}) and "office_id" in st.read("road.json", {})
        code, r = _j(base, "POST", "/api/road/invite", {"label": "Dr B iPhone"})
        assert r["ok"] and r["code"].startswith("CODE") and r["link"].endswith("#" + r["code"]), r
        ppriv, ppub = rc.new_keypair()                                              # the phone
        import urllib.request as _ur
        def relay(method, path, obj=None, tok=None):
            rq = _ur.Request(api + "/road" + path, data=json.dumps(obj).encode() if obj is not None else None, method=method, headers=dict({"Content-Type": "application/json"}, **({"Authorization": "Bearer " + tok} if tok else {})))
            with _ur.urlopen(rq, timeout=10) as rr: return json.loads(rr.read().decode())
        en = relay("POST", "/device/enroll", {"code": r["code"], "pub": ppub, "name": "Dr. Example"})
        assert en["status"] == "pending" and en["office_pub"]["x"] == st.read("road.json", {})["pub"]["x"]
        code, r = _j(base, "GET", "/api/road/status")
        assert r["pending"] == 1 and r["devices"][0]["name"] == "Dr. Example" and "pub" not in r["devices"][0], r
        oid = st.read("road.json", {})["office_id"]; AAD = rc.aad_for(oid, en["device_id"])
        code, r = _j(base, "POST", "/api/road/device", {"device_id": en["device_id"], "status": "allow", "fp": "zzzz"})
        assert code == 400 and "does not match" in r["error"], r                          # Allow checks the phone's code
        code, r = _j(base, "POST", "/api/road/device", {"device_id": en["device_id"], "status": "allow", "fp": rc.fingerprint(ppub)[:4]})
        assert r["ok"] and r["device"]["status"] == "allowed", r
        # phone photographs two pages and sends them to the office; the PC turns them into one PDF in the inbox
        jpg = base64.b64decode("/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/yQALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")
        jpg2 = jpg + b"\0" * 300000                      # a real phone photo is a few hundred KB: the header must take it (v1.3.1 fix)
        blob = rc.pack({"name": "Consent - J Doe", "from": "Dr. Example", "kind": "doc", "note": "signed in the car", "to": "Pat", "pages": [base64.urlsafe_b64encode(jpg).decode().rstrip("="), base64.b64encode(jpg2).decode()]}, b"")
        er = relay("POST", "/env", {"to": "office", "kind": "doc", "size": len(jpg) + len(jpg2), "body": rc.encrypt_to(en["office_pub"], blob, AAD)}, en["device_token"])
        assert er["ok"], er
        assert app.road.pull_once() == 1
        code, ib = _j(base, "GET", "/api/inbox")
        rd = [x for x in ib["rows"] if x.get("source") == "road"]
        assert len(rd) == 1 and rd[0]["from_name"] == "Dr. Example (road)" and rd[0]["pages"] == 2 and rd[0]["assigned_to"] == "Pat" and rd[0]["note"] == "signed in the car", rd
        pdfb = (st.root / "inbox" / (rd[0]["id"] + ".pdf")).read_bytes()
        assert pdfb[:5] == b"%PDF-" and pdfb.count(b"/Type /Page ") == 2 and b"DCTDecode" in pdfb
        assert relay("GET", "/env", None, "OT" + st.read("road.json", {})["office_id"])["rows"] == []        # acked
        # office sends a document (a sent fax from the outbox) to the phone; the phone (roadcrypto) opens it
        code, r = _j(base, "POST", "/api/road/send", {"id": fid3, "device_id": en["device_id"], "note": "please review"})
        assert r["ok"] and r["device"] == "Dr. Example", r
        mine = relay("GET", "/env", None, en["device_token"])["rows"]
        assert len(mine) == 1 and mine[0]["kind"] == "doc" and mine[0]["from"] == "office"
        full = relay("GET", "/env/" + mine[0]["id"], None, en["device_token"])
        meta, data = rc.unpack(rc.decrypt(ppriv, full["body"], AAD))
        try:
            rc.decrypt(ppriv, full["body"], rc.aad_for(oid, "dev_other")); raise AssertionError("aad not bound")
        except Exception as e:
            assert not isinstance(e, AssertionError)
        assert data[:5] == b"%PDF-" and meta["note"] == "please review" and meta["sent_by"] == "Pat" and meta["kind"] == "doc", meta
        # a blocked phone cannot send; a tampered envelope is dropped, not landed
        code, r = _j(base, "POST", "/api/road/device", {"device_id": en["device_id"], "status": "block"})
        try:
            relay("POST", "/env", {"to": "office", "kind": "doc", "size": 1, "body": rc.encrypt_to(en["office_pub"], blob, AAD)}, en["device_token"]); raise AssertionError("blocked phone sent")
        except Exception as e:
            assert not isinstance(e, AssertionError)
        _j(base, "POST", "/api/road/device", {"device_id": en["device_id"], "status": "allow", "fp": rc.fingerprint(ppub)[:4]})
        bad = rc.encrypt_to(en["office_pub"], blob, AAD); bad["ct"] = bad["ct"][:-6] + ("AAAAAA" if not bad["ct"].endswith("AAAAAA") else "BBBBBB")
        relay("POST", "/env", {"to": "office", "kind": "doc", "size": 1, "body": bad}, en["device_token"])
        assert app.road.pull_once() == 0 and len([x for x in _j(base, "GET", "/api/inbox")[1]["rows"] if x.get("source") == "road"]) == 1
        assert not any("signed in the car" in l or "Consent" in l for l in (st.root / "audit.jsonl").read_text().splitlines())
        ok.append("road: office key sealed; invite -> enrol -> Allow checks the phone's code; envelopes bound to office+device (AAD); phone pages land as one PDF, assigned; office -> phone sealed; blocked / tampered refused; relay and audit see no plaintext")

        n = app.worker.pull_once()
        assert n == 1 and app.worker.pull_once() == 0                                 # once
        code, ib = _j(base, "GET", "/api/inbox")
        row = [x for x in ib["rows"] if x.get("source") != "road"][0]
        assert row["from"] == "+18185550123" and row["is_new"] and row["pages"] == 1 and (tmp / "inbox" / (row["id"] + ".pdf")).exists()
        code, r = _j(base, "POST", "/api/inbox/name", {"id": row["id"], "name": "Dr Test's office"})
        code, r = _j(base, "POST", "/api/inbox/assign", {"id": row["id"], "to": "Sam"})
        code, r = _j(base, "POST", "/api/inbox/rule", {"id": row["id"], "to": "Sam"})
        code, r = _j(base, "POST", "/api/inbox/handle", {"id": row["id"], "note": ""})          # one click: Done
        code, r = _j(base, "POST", "/api/inbox/handle", {"id": row["id"], "note": "Filed"}, name="Sam")   # second click: the why
        code, ib = _j(base, "GET", "/api/inbox", name="Sam")
        row = [x for x in ib["rows"] if x.get("source") != "road"][0]
        assert row["from_name"] == "Dr Test's office" and row["assigned_to"] == "Sam" and row["rule_to"] == "Sam" and row["handled"] and not row["is_new"]
        assert row["handled_note"] == "Filed" and row["handled_by"] != "Sam"                    # the why does not change who did it
        code, r = _j(base, "POST", "/api/inbox/assign", {"id": row["id"], "to": "Nobody"})
        assert code == 400
        code, r = _j(base, "POST", "/api/inbox/spam", {"id": row["id"]}, name="")
        assert code == 400                                                             # no name, no action
        ok.append("inbox: pull once, name sender, assign, rule, Done then +why; names validated; no name = no action")
        # search (OCR): a stand-in "tesseract" script says what is on each page; the real one is only on the user's PC
        from . import ocr as _ocrmod
        tess = tmp / ("tesseract.cmd" if os.name == "nt" else "tesseract")
        if os.name == "nt":
            tess.write_text("@echo RE: claim 26-902750892 Jacqueline Example records request\r\n", encoding="ascii")
        else:
            tess.write_text("#!/bin/sh\necho 'RE: claim 26-902750892 Jacqueline Example records request'\n", encoding="ascii"); os.chmod(str(tess), 0o755)
        os.environ["FAXDESK_TESSERACT"] = str(tess)
        try:
            if _ocrmod.have_renderer():
                n = app.ocr.once()
                assert n >= 1, n
                code, r = _j(base, "GET", "/api/inbox/search?q=902750892")
                assert code == 200 and r["engine"] and row["id"] in r["hits"] and r["hits"][row["id"]]["page"] == 1 and "Jacqueline" in r["hits"][row["id"]]["line"], r
                code, r = _j(base, "GET", "/api/inbox/search?q=claim%20records")
                assert row["id"] in r["hits"], r
                code, r = _j(base, "GET", "/api/inbox/search?q=nothing%20like%20this")
                assert r["hits"] == {}, r
                assert "902750892" not in (tmp / "audit.jsonl").read_text(encoding="ascii")
                ok.append("search: OCR reads a new fax once, words found by page, all-words match, audit never records the search")
            else:
                code, r = _j(base, "GET", "/api/inbox/search?q=claim")
                assert code == 200 and r["engine"] and not r["renderer"]
                ok.append("search: renderer (PyMuPDF) not installed here - search answers 'off' plainly (OCR path untested)")
        finally:
            os.environ.pop("FAXDESK_TESSERACT", None)
        # settings re-save with blank account/extension keeps them; bulk actions; logo + disclaimer on the cover
        code, r = _j(base, "POST", "/api/setup", {"office": "Test Office", "voip_id": "", "extension": "", "office_line": "1 Main St", "disclaimer": "confidential"})
        assert r["setup_done"] and st.config()["voip_id"] == "999" and st.config()["disclaimer"] == "confidential"
        code, r = _j(base, "POST", "/api/inbox/bulk", {"what": "open", "ids": [row["id"]]})
        assert r["ok"] and r["done"] == 1 and st.read("inbox/%s.json" % row["id"], {}).get("opened_by") == "Pat"
        jpg = bytes([0xFF, 0xD8, 0xFF, 0xE0, 0, 16]) + b"JFIF" + bytes(10) + bytes([0xFF, 0xC0, 0, 17, 8, 0, 60, 0, 170, 3]) + bytes(20)
        code, r = _j(base, "POST", "/api/logo", {"jpeg": base64.b64encode(jpg).decode()})
        assert r["ok"] and (tmp / "logo.jpg").exists()
        code, r = _j(base, "POST", "/api/logo", {"jpeg": base64.b64encode(b"not a jpeg").decode()})
        assert code == 400
        with urllib.request.urlopen(base + "/api/cover_preview", timeout=10) as rr:
            pdf = rr.read()
        assert pdf[:5] == b"%PDF-" and b"/Im1" in pdf and b"CONFIDENTIAL" in pdf and b"1 Main St" in pdf
        code, r = _j(base, "POST", "/api/inbox/bulk", {"what": "delete", "ids": [row["id"]]})
        assert r["done"] == 1 and not (tmp / "inbox" / (row["id"] + ".json")).exists()
        ok.append("settings keep account on blank re-save; bulk open/delete; JPG logo + disclaimer on the cover preview")

        assert app.worker.pull_once() == 1
        m = st.read("inbox/%s.json" % row["id"], {}); m["received"] = "2020-01-01T00:00:00"; st.write("inbox/%s.json" % row["id"], m)
        assert st.prune("inbox", 60) == 1 and not (tmp / "inbox" / (row["id"] + ".pdf")).exists()
        assert st.backup_daily() >= 3 and st.backup_daily() == 0
        for line in (tmp / "audit.jsonl").read_text().splitlines():
            assert "hello" not in line and "records" not in line.lower() or "fax_queued" in line
        ok.append("retention prune; daily backup once; audit has no document text")
        for f in Path(__file__).resolve().parent.rglob("*.py"):
            f.read_bytes().decode("ascii")
        (Path(__file__).resolve().parent / "www" / "index.html").read_bytes().decode("ascii")
        from . import license as LIC
        key, em = LIC.make("Buyer@Example.com ")
        assert key.startswith("FFD-") and len(key) == 21 and LIC.valid(key) and LIC.valid(key, "buyer@example.com") and not LIC.valid(key, "other@example.com") and not LIC.valid("FFD-AAAAA-AAAAA-AAAA")
        code, r = _j(base, "POST", "/api/setup", {"lan": True})
        assert code == 400 and "licence" in r["error"]                                  # free = one PC
        code, r = _j(base, "POST", "/api/setup", {"lan": True, "license": key})
        assert r["ok"] and st.config()["lan"] is True
        ok.append("licence: free = this PC; a key opens the LAN switch")
        # Windows-only code (tray) cannot run here, so at least compile it and make sure no function shadows a module name
        import symtable, pathlib
        for f in pathlib.Path(__file__).parent.glob("*.py"):
            src = f.read_text(encoding="ascii")
            def _walk(t):
                for c in t.get_children():
                    if c.get_type() == "function":
                        for n in c.get_symbols():
                            assert not (n.get_name() in ("os", "sys", "ctypes", "json", "time") and n.is_local() and n.is_imported()), "%s: %s imports %s inside a function (shadows the module)" % (f.name, c.get_name(), n.get_name())
                    _walk(c)
            _walk(symtable.symtable(src, str(f), "exec"))
        ok.append("pure ASCII sources; all modules compile; no module shadowing inside functions")
        print("faxdesk selftest: ALL PASS")
        for o in ok:
            print("  ok  " + o)
        return 0
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print("faxdesk selftest: FAIL")
        return 1
    finally:
        srv.shutdown(); fake.shutdown()
