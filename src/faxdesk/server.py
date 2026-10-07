"""server.py - Free Fax Desk: the page and its API on one PC. Stdlib. ASCII. No estate imports.
Identity = the name picker in the page (cookie fd_name), on trust. The page only QUEUES sends; worker.py transmits.
Free = 127.0.0.1 only. The LAN switch (0.0.0.0) is the paid 'whole office' option."""
import base64
import datetime as _dt
import json
import os
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import license, pdfmini, phonecom
from .store import Store, clean, now
from .worker import Worker
from .road import Road
from . import ocr as _ocr

VERSION = "0.6.0"
HERE = Path(getattr(sys, "_MEIPASS", "")) / "faxdesk" if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
MAX_PDF = 20 * 1024 * 1024
OUTCOMES = ["Printed", "Filed", "Given to someone", "Replied", "Junk"]
DISCLAIMERS = {
    "none": "",
    "standard": "This fax is intended only for the person or office named above. If you received it in error, please call the sender and destroy it.",
    "confidential": "CONFIDENTIAL: This transmission may contain private health or personal information protected by law. It is intended only for the "
                    "addressee. If you are not the intended recipient, any review, use or disclosure is prohibited; please notify the sender by "
                    "telephone and destroy all copies.",
    "custom": None,
}
MAX_LOGO = 400 * 1024
_ID = re.compile(r"^(in|out)-[A-Za-z0-9-]{1,48}$")


def page_count(pdf):
    n = len(re.findall(rb"/Type\s*/Page[^s]", pdf))
    return n or 1


def wrap(text, width=90):
    words, lines, cur = str(text or "").split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


def cover_lines(cfg, to_name, to_fax, from_name, subject, note, pages, when=None):
    office = cfg.get("office") or "Fax"
    L = ["## " + office] + (["  " + x for x in wrap(cfg.get("office_line"), 80)] if cfg.get("office_line") else []) + ["", "FAX COVER SHEET", "",
         "Date:     " + (when or _dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
         "To:       " + (to_name or ""), "Fax:      " + App.pretty(phonecom.to_e164(to_fax)), "From:     " + (from_name or office),
         "Our fax:  " + App.pretty(cfg.get("fax_number") or ""), "Pages:    %d (plus this cover)" % pages, "Subject:  " + (subject or ""), ""]
    if note:
        L += ["Note:"] + ["  " + x for x in wrap(note)]
    d = cfg.get("disclaimer") or "standard"
    text = cfg.get("disclaimer_text") if d == "custom" else DISCLAIMERS.get(d, "")
    if text:
        L += ["", ""] + wrap(text, 95)
    return L


def cover_pdf(cfg, to_name, to_fax, from_name, subject, note, pages, logo=None):
    return pdfmini.build([cover_lines(cfg, to_name, to_fax, from_name, subject, note, pages)], title="Fax cover", logo=logo)


class App:
    def __init__(self, store):
        self.store = store
        self.worker = Worker(store, self.client)
        self.road = Road(store)
        self.worker.road = self.road
        self.ocr = _ocr.Ocr(store)
        self.worker.ocr = self.ocr

    def client(self):
        c = self.store.config()
        tok = self.store.token()
        if not (c.get("setup_done") and tok and c.get("voip_id")):
            return None
        return phonecom.Client(tok, c["voip_id"], c.get("extension"), c.get("fax_number") or None, api=c.get("api") or phonecom.API)

    # ------------------------------------------------------------ views
    def state(self, me):
        c = self.store.config()
        inbox = self.inbox_rows(me)
        return {"ok": True, "version": VERSION, "me": me, "people": self.store.people(), "office": c.get("office", ""),
                "setup_done": bool(c.get("setup_done")), "lan": bool(c.get("lan")), "fax_number": c.get("fax_number", ""),
                "office_line": c.get("office_line", ""), "disclaimer": c.get("disclaimer") or "standard", "disclaimer_text": c.get("disclaimer_text", ""),
                "disclaimers": dict((k, v) for k, v in DISCLAIMERS.items() if v is not None), "has_logo": (self.store.root / "logo.jpg").exists(),
                "pull_minutes": c.get("pull_minutes"), "keep_in_days": c.get("keep_in_days"), "keep_out_days": c.get("keep_out_days"),
                "voip_id": c.get("voip_id", ""), "extension": c.get("extension", ""), "has_token": bool(self.store.token()),
                "licensed": license.valid(c.get("license") or ""), "license_hint": (c.get("license") or "")[-4:],
                "book": self.store.book(), "outcomes": OUTCOMES, "ocr": dict(_ocr.status(c), **self.ocr.stat),
                "counts": {"new": sum(1 for r in inbox if r["is_new"]), "mine": sum(1 for r in inbox if r["assigned_to"] == me and not r["handled"]),
                           "to_handle": sum(1 for r in inbox if not r["handled"] and not r["spam"]),
                           "queued": sum(1 for r in self.outbox_rows() if r["status"] in ("queued", "sending", "retry"))}}

    def inbox_rows(self, me=""):
        st = self.store
        rules, contacts = st.rules(), st.contacts()
        rows = []
        for p in (st.root / "inbox").glob("*.json"):
            m = st.read("inbox/" + p.name, None)
            if not isinstance(m, dict):
                continue
            m = dict(m)
            m["from_pretty"] = self.pretty(m.get("from"))
            m["from_name"] = (contacts.get(m.get("from") or "") or {}).get("name") or m.get("from_name") or ""
            m["rule_to"] = (rules.get(m.get("from") or "") or {}).get("to", "")
            m["is_new"] = not (m.get("opened_at") or m.get("handled") or m.get("spam") or m.get("assigned_to"))
            rows.append(m)
        rows.sort(key=lambda r: r.get("received") or "", reverse=True)
        return rows

    def outbox_rows(self):
        st = self.store
        rows = [st.read("outbox/" + p.name, None) for p in (st.root / "outbox").glob("*.json")]
        rows = [dict(r, to_pretty=self.pretty(r.get("to"))) for r in rows if isinstance(r, dict)]
        rows.sort(key=lambda r: r.get("queued_at") or "", reverse=True)
        return rows

    @staticmethod
    def pretty(e):
        d = re.sub(r"\D", "", str(e or ""))
        return "(%s) %s-%s" % (d[-10:-7], d[-7:-4], d[-4:]) if len(d) >= 10 else (e or "")

    # ------------------------------------------------------------ actions
    def setup(self, body, me):
        st = self.store
        ch = {}
        for k in ("office", "voip_id", "extension", "fax_number", "api"):
            if k in body and (k in ("office", "fax_number") or clean(body.get(k), 80)):   # an empty account/extension never wipes a saved one
                ch[k] = clean(body.get(k), 80)
        if "office_line" in body:
            ch["office_line"] = clean(body.get("office_line"), 160)
        if "disclaimer" in body and str(body["disclaimer"]) in DISCLAIMERS:
            ch["disclaimer"] = str(body["disclaimer"])
        if "disclaimer_text" in body:
            ch["disclaimer_text"] = clean(body.get("disclaimer_text"), 600)
        if body.get("token"):
            ch["token"] = str(body["token"]).strip()[:400]
        for k in ("pull_minutes", "keep_in_days", "keep_out_days"):
            if k in body:
                try:
                    ch[k] = max(1, min(3650, int(body[k])))
                except (TypeError, ValueError):
                    return 400, {"ok": False, "error": "%s must be a number." % k}
        if "license" in body:
            ch["license"] = clean(body.get("license"), 40).upper().replace(" ", "")
        if "lan" in body:
            want = bool(body["lan"])
            if want and not license.valid(ch.get("license") or st.config().get("license") or ""):
                return 400, {"ok": False, "error": "The whole-office switch needs a licence key (Settings > Whole office). Free FaxDesk is for this one PC."}
            ch["lan"] = want
        if "fax_number" in ch and ch["fax_number"] and not phonecom.to_e164(ch["fax_number"]):
            return 400, {"ok": False, "error": "The fax number needs 10 digits."}
        if "fax_number" in ch:
            ch["fax_number"] = phonecom.to_e164(ch["fax_number"])
        c = st.config()
        done = bool((ch.get("token") or st.token()) and (ch.get("voip_id") or c.get("voip_id")) and (ch.get("extension") or c.get("extension")))
        ch["setup_done"] = done
        st.save_config(ch)
        st.audit("setup", me, "done" if done else "partial")
        return 200, {"ok": True, "setup_done": done, "restart_needed": "lan" in ch and bool(ch["lan"]) != bool(c.get("lan"))}

    def test_connection(self):
        cl = self.client()
        if cl is None:
            return {"ok": False, "error": "Fill in the token, account and extension first."}
        try:
            r = cl.check()
        except Exception as e:
            return {"ok": False, "error": "could not reach Phone.com (%s)" % type(e).__name__}
        if not r.get("ok"):
            return r
        nums = []
        try:
            nums = cl.numbers()
        except Exception:
            pass
        return {"ok": True, "accounts": r.get("accounts"), "sees_configured": r.get("sees_configured"), "numbers": nums}

    def queue_send(self, body, me):
        st = self.store
        if not me:
            return 400, {"ok": False, "error": "Pick your name first."}
        to = phonecom.to_e164(body.get("to"))
        if not to:
            return 400, {"ok": False, "error": "The fax number needs 10 digits."}
        try:
            pdf = base64.b64decode(str(body.get("pdf") or ""))
        except Exception:
            pdf = b""
        if not pdf or pdf[:5] != b"%PDF-":
            return 400, {"ok": False, "error": "Pick a PDF file."}
        if len(pdf) > MAX_PDF:
            return 413, {"ok": False, "error": "That PDF is over 20 MB."}
        c = st.config()
        pages = page_count(pdf)
        fid = "out-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + base64.b32encode(pdf[-5:] + os.urandom(3)).decode("ascii").lower()[:10]   # v1.1: random tail - two sends of the same PDF in one second no longer share a name
        cover = None
        if body.get("cover", True):
            logo = None
            try:
                logo = (st.root / "logo.jpg").read_bytes() if (st.root / "logo.jpg").exists() else None
            except OSError:
                logo = None
            cover = cover_pdf(c, clean(body.get("to_name"), 60), to, me, clean(body.get("subject"), 80), clean(body.get("note"), 600), pages, logo=logo)
            # the worker sends [cover, document]; both are kept so the log can show what went
            (st.root / "outbox" / (fid + ".cover.pdf")).write_bytes(cover)
        (st.root / "outbox" / (fid + ".pdf")).write_bytes(pdf)
        meta = {"id": fid, "to": to, "to_name": clean(body.get("to_name"), 60), "subject": clean(body.get("subject"), 80), "note": clean(body.get("note"), 600),
                "by": me, "queued_at": now(), "status": "queued", "tries": 0, "pages": pages + (1 if cover else 0), "filename": clean(body.get("filename"), 60) or "document.pdf",
                "cover": bool(cover), "error": ""}
        st.write("outbox/" + fid + ".json", meta)
        st.audit("fax_queued", me, "pages_%d" % meta["pages"], fid)
        if body.get("save_to_book") and body.get("to_name"):
            book = st.book()
            if not any(b.get("fax") == to for b in book):
                book.append({"name": clean(body.get("to_name"), 60), "fax": to, "category": ""})
                st.save_book(book)
        self.worker.poke()
        return 200, {"ok": True, "id": fid, "pages": meta["pages"]}

    def resend(self, fid, me):
        m = self.store.read("outbox/%s.json" % fid, None)
        if not m:
            return 404, {"ok": False, "error": "Not found."}
        m.update(status="queued", tries=0, error="", not_before="", delivery={})   # v1.1: a resend is watched afresh
        self.store.write("outbox/%s.json" % fid, m)
        self.store.audit("fax_resend", me, "", fid)
        self.worker.poke()
        return 200, {"ok": True}

    def inbox_act(self, what, body, me):
        st = self.store
        fid = str(body.get("id") or "")
        if not _ID.match(fid) or not fid.startswith("in-"):
            return 400, {"ok": False, "error": "Bad id."}
        m = st.read("inbox/%s.json" % fid, None)
        if not m:
            return 404, {"ok": False, "error": "That fax is gone (retention)."}
        if what == "open":
            if not m.get("opened_at"):
                m.update(opened_by=me, opened_at=now())
        elif what == "assign":
            to = clean(body.get("to"), 40)
            if to and to not in st.people():
                return 400, {"ok": False, "error": "That name is not on the list."}
            m.update(assigned_to=to, assigned_by=me if to else "", assigned_at=now() if to else "")
        elif what == "handle":
            note = clean(body.get("note"), 120)
            if m.get("handled"):
                m.update(handled_note=note)                    # second click: the reason; who handled it stays
            else:
                m.update(handled=True, handled_by=me, handled_at=now(), handled_note=note)
        elif what == "unhandle":
            m.update(handled=False, handled_by="", handled_at="", handled_note="")
        elif what == "unopen":
            m.update(opened_by="", opened_at="")
        elif what == "spam":
            m["spam"] = bool(body.get("spam", True))
        elif what == "delete":
            for ext in (".json", ".pdf"):
                try:
                    (st.root / "inbox" / (fid + ext)).unlink()
                except OSError:
                    pass
            st.audit("inbox_delete", me, "", fid)
            return 200, {"ok": True}
        elif what == "rule":
            if not m.get("from"):
                return 400, {"ok": False, "error": "No sender number on this fax."}
            r = st.rules()
            to = clean(body.get("to"), 40)
            if to:
                r[m["from"]] = {"to": to, "by": me, "at": now()}
            else:
                r.pop(m["from"], None)
            st.write("rules.json", r)
        elif what == "name":
            if not m.get("from"):
                return 400, {"ok": False, "error": "No sender number on this fax."}
            cts = st.contacts()
            nm = clean(body.get("name"), 60)
            if nm:
                cts[m["from"]] = {"name": nm, "by": me, "at": now()}
            else:
                cts.pop(m["from"], None)
            st.write("contacts.json", cts)
        else:
            return 404, {"ok": False, "error": "Unknown action."}
        if what not in ("rule", "name"):
            st.write("inbox/%s.json" % fid, m)
        st.audit("inbox_" + what, me, clean(body.get("to") or body.get("note") or "", 40), fid)
        return 200, {"ok": True}

    def log_rows(self, q=""):
        q = clean(q, 60).lower()
        rows = []
        try:
            with open(str(self.store.root / "log.jsonl"), "r", encoding="ascii") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    r["to_pretty"] = self.pretty(r.get("to"))
                    if q and q not in json.dumps(r).lower():
                        continue
                    rows.append(r)
        except OSError:
            pass
        rows.reverse()
        return rows[:500]

    def confirmation(self, fid):
        """Page 1 = the proof (status, when queued / sent, to, pages, attempts, Phone.com id). Page 2 = the cover sheet as sent."""
        m = self.store.read("outbox/%s.json" % fid, None)
        if not m:
            return None
        c = self.store.config()
        stamp = lambda t: (str(t)[:16].replace("T", " ") if t else "")
        status = str(m.get("status", "")).upper()
        if status in ("QUEUED", "SENDING", "RETRY"):
            status += " - NOT SENT YET"
        L = ["## " + (c.get("office") or "Fax") + " - Fax transmission report", "",
             "Status:        " + status,
             "Queued:        " + stamp(m.get("queued_at")) + " by " + (m.get("by") or ""),
             "Sent:          " + (stamp(m.get("sent_at")) if m.get("sent_at") else "-"),
             "Failed:        " + stamp(m.get("failed_at")) if m.get("failed_at") else "",
             "To:            " + (m.get("to_name") or ""), "Fax:           " + self.pretty(m.get("to")),
             "From:          " + (m.get("by") or "") + " (" + (c.get("office") or "") + ")", "Our fax:       " + self.pretty(c.get("fax_number")),
             "Subject:       " + (m.get("subject") or ""), "Pages:         " + str(m.get("pages") or "") + (" (including cover)" if m.get("cover") else ""),
             "Attempts:      " + str(m.get("tries") or 0), "Phone.com id:  " + str(m.get("fax_id") or "-"), ""]
        L = [x for x in L if x != ""] if False else [x for x in L if not x.startswith("Failed:        ") or m.get("failed_at")]
        if m.get("error"):
            L += ["Last error: " + m["error"]]
        L += ["", "Printed " + _dt.datetime.now().strftime("%Y-%m-%d %H:%M") + " from FaxDesk"]
        pages = [L]
        if m.get("cover"):
            pages.append(["## Cover sheet as sent", ""] + cover_lines(c, m.get("to_name"), m.get("to"), m.get("by"), m.get("subject"), m.get("note"),
                                                                    max(0, int(m.get("pages") or 1) - 1), when=stamp(m.get("sent_at") or m.get("queued_at"))))
        logo = None
        try:
            logo = (self.store.root / "logo.jpg").read_bytes() if (self.store.root / "logo.jpg").exists() else None
        except OSError:
            pass
        return pdfmini.build(pages, title="Fax transmission report", logo=logo)


class Handler(BaseHTTPRequestHandler):
    app = None
    server_version = "FaxDesk/" + VERSION

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, (bytes, bytearray)) else (json.dumps(body, ensure_ascii=True) if not isinstance(body, str) else body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _me(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "fd_name":
                n = clean(urllib.parse.unquote(v), 40)
                return n if n in self.app.store.people() else ""
        return ""

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, _, q = self.path.partition("?")
        qs = urllib.parse.parse_qs(q)
        app, me = self.app, self._me()
        try:
            if path == "/health":
                w = app.worker.last
                c = app.store.config()
                return self._send(200, {"ok": True, "app": "FaxDesk", "version": VERSION, "setup_done": bool(c.get("setup_done")), "lan": bool(c.get("lan")),
                                        "state_dir": str(app.store.root), "last_transmit": w["transmit"], "last_pull": w["pull"], "pull_ok": w["pull_ok"], "pull_error": w["pull_error"],
                                        "queued": sum(1 for r in app.outbox_rows() if r["status"] in ("queued", "sending", "retry"))})
            if path in ("/", "/index.html"):
                return self._send(200, (HERE / "www" / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/help":
                return self._send(200, (HERE / "www" / "help.html").read_bytes(), "text/html; charset=utf-8")
            if path in ("/favicon.ico", "/favicon.svg"):
                return self._send(200, (HERE / "www" / path[1:]).read_bytes(), "image/x-icon" if path.endswith(".ico") else "image/svg+xml")
            if path == "/api/state":
                return self._send(200, app.state(me))
            if path == "/api/logo":
                p = app.store.root / "logo.jpg"
                return self._send(200, p.read_bytes(), "image/jpeg") if p.exists() else self._send(404, "no logo", "text/plain")
            if path == "/api/cover_preview":
                c = app.store.config()
                p = app.store.root / "logo.jpg"
                pdf = cover_pdf(c, "Sample Recipient", "8185550123", me or "Sender", "Sample subject", "This is what your cover sheet looks like.", 2, logo=p.read_bytes() if p.exists() else None)
                return self._send(200, pdf, "application/pdf", {"Content-Disposition": "inline; filename=cover_preview.pdf"})
            if path == "/api/inbox":
                return self._send(200, {"ok": True, "me": me, "rows": app.inbox_rows(me)})
            if path == "/api/inbox/search":
                q = (qs.get("q") or [""])[0]
                app.store.audit("inbox_search", me, "ok", "")                       # never the words
                return self._send(200, dict({"ok": True, "hits": app.ocr.search(q)}, **dict(_ocr.status(app.store.config()), **app.ocr.stat)))
            if path == "/api/road/status":
                st = app.road.status(); st["devices"] = [dict((k, v) for k, v in d.items() if k != "pub") for d in st.get("devices") or []]; return self._send(200, st)
            if path == "/api/outbox":
                return self._send(200, {"ok": True, "me": me, "rows": app.outbox_rows()})
            if path == "/api/log":
                return self._send(200, {"ok": True, "me": me, "rows": app.log_rows((qs.get("q") or [""])[0])})
            if path in ("/api/file", "/api/confirm"):
                fid = (qs.get("id") or [""])[0]
                if not _ID.match(fid):
                    return self._send(400, "Bad id.", "text/plain")
                if path == "/api/confirm":
                    pdf = app.confirmation(fid)
                    return self._send(200, pdf, "application/pdf", {"Content-Disposition": "inline; filename=confirmation_%s.pdf" % fid}) if pdf else self._send(404, "Not found.", "text/plain")
                folder = "inbox" if fid.startswith("in-") else "outbox"
                p = app.store.root / folder / (fid + ".pdf")
                if not p.exists():
                    return self._send(404, "That document is gone (retention).", "text/plain")
                if folder == "inbox" and me:
                    app.inbox_act("open", {"id": fid}, me)
                return self._send(200, p.read_bytes(), "application/pdf", {"Content-Disposition": "inline; filename=%s.pdf" % fid})
            return self._send(404, {"ok": False, "error": "Not found."})
        except Exception as e:
            return self._send(500, {"ok": False, "error": "Page error (%s)." % type(e).__name__})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        app, me = self.app, self._me()
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > MAX_PDF * 4 // 3 + 65536:
                return self._send(413, {"ok": False, "error": "Too large."})
            body = json.loads((self.rfile.read(n) if n else b"{}").decode("utf-8") or "{}")
            assert isinstance(body, dict)
        except Exception:
            return self._send(400, {"ok": False, "error": "Bad request."})
        try:
            if path == "/api/setup":
                code, out = app.setup(body, me); return self._send(code, out)
            if path == "/api/test":
                return self._send(200, app.test_connection())
            if path == "/api/people":
                names = body.get("people") if isinstance(body.get("people"), list) else []
                return self._send(200, {"ok": True, "people": app.store.save_people(names)})
            if path == "/api/book":
                rows = [{"name": clean(r.get("name"), 60), "fax": phonecom.to_e164(r.get("fax")), "category": clean(r.get("category"), 30)}
                        for r in (body.get("book") or []) if isinstance(r, dict)]
                rows = [r for r in rows if r["name"] and r["fax"]]
                app.store.save_book(rows); app.store.audit("book_save", me, "n_%d" % len(rows))
                return self._send(200, {"ok": True, "book": rows})
            if path == "/api/logo":
                if body.get("clear"):
                    try:
                        (app.store.root / "logo.jpg").unlink()
                    except OSError:
                        pass
                    return self._send(200, {"ok": True, "has_logo": False})
                try:
                    data = base64.b64decode(str(body.get("jpeg") or ""))
                except Exception:
                    data = b""
                if not pdfmini.jpeg_size(data):
                    return self._send(400, {"ok": False, "error": "The logo must be a JPG image."})
                if len(data) > MAX_LOGO:
                    return self._send(413, {"ok": False, "error": "Logo over 400 KB. Save it smaller."})
                (app.store.root / "logo.jpg").write_bytes(data); app.store.audit("logo_set", me, "bytes_%d" % len(data))
                return self._send(200, {"ok": True, "has_logo": True})
            if path == "/api/send":
                code, out = app.queue_send(body, me); return self._send(code, out)
            if path == "/api/road/enable":
                code, out = app.road.enable(me); return self._send(code, out)
            if path == "/api/road/disable":
                code, out = app.road.disable(me); return self._send(code, out)
            if path == "/api/road/invite":
                code, out = app.road.invite(me, body.get("label")); return self._send(code, out)
            if path == "/api/road/device":
                code, out = app.road.device(me, body.get("device_id"), str(body.get("status") or ""), str(body.get("fp") or "")); return self._send(code, out)
            if path == "/api/road/send":                       # an inbox fax or a sent document -> one phone
                fid = re.sub(r"[^A-Za-z0-9_.-]", "", str(body.get("id") or ""))
                box = "inbox" if fid.startswith(("in-", "rd-")) else "outbox"
                m = app.store.read("%s/%s.json" % (box, fid), None)
                pdfp = app.store.root / box / (fid + ".pdf")
                if not m or not pdfp.exists():
                    return self._send(404, {"ok": False, "error": "Not found."})
                meta = {"name": (m.get("subject") or m.get("filename") or fid) + ".pdf" if not str(m.get("subject") or "").endswith(".pdf") else m.get("subject"),
                        "from": (m.get("from_name") or app.pretty(m.get("from")) or m.get("to_name") or ""), "kind": "fax" if box == "inbox" and m.get("source") != "road" else "doc",
                        "pages": m.get("pages"), "note": body.get("note") or ""}
                code, out = app.road.send(me, str(body.get("device_id") or ""), pdfp.read_bytes(), meta); return self._send(code, out)
            if path == "/api/resend":
                fid = str(body.get("id") or "")
                if not _ID.match(fid):
                    return self._send(400, {"ok": False, "error": "Bad id."})
                code, out = app.resend(fid, me); return self._send(code, out)
            if path == "/api/inbox/bulk":
                if not me:
                    return self._send(400, {"ok": False, "error": "Pick your name first."})
                what = str(body.get("what") or "")
                if what not in ("open", "handle", "spam", "unspam", "delete", "assign"):
                    return self._send(400, {"ok": False, "error": "Unknown action."})
                ids = [str(i) for i in (body.get("ids") or []) if _ID.match(str(i))][:500]
                done = 0
                for fid in ids:
                    b2 = {"id": fid, "note": body.get("note"), "to": body.get("to"), "spam": what != "unspam"}
                    code, out = app.inbox_act("spam" if what == "unspam" else what, b2, me)
                    done += 1 if code == 200 else 0
                return self._send(200, {"ok": True, "done": done})
            if path.startswith("/api/inbox/"):
                if not me:
                    return self._send(400, {"ok": False, "error": "Pick your name first."})
                code, out = app.inbox_act(path.rsplit("/", 1)[1], body, me); return self._send(code, out)
            if path == "/api/pull_now":
                return self._send(200, {"ok": True, "new": app.worker.pull_once(), "error": app.worker.last["pull_error"]})
            return self._send(404, {"ok": False, "error": "Not found."})
        except Exception as e:
            return self._send(500, {"ok": False, "error": "Page error (%s)." % type(e).__name__})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def make(store=None, host=None, port=None):
    st = store or Store()
    c = st.config()
    app = App(st)
    Handler.app = app
    h = host or ("0.0.0.0" if c.get("lan") else "127.0.0.1")
    srv = Server((h, int(c.get("port") or 8750) if port is None else int(port)), Handler)
    return app, srv
