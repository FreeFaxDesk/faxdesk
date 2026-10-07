"""worker.py - Free Fax Desk background work: transmit the outbox (once a minute), pull incoming (every N minutes), daily
backup + retention. One thread, never raises out. The page only ever QUEUES; this is the only thing that calls send()."""
import datetime as _dt
import json
import os
import threading
import time

from . import phonecom
from .store import Store, clean, now

MAX_TRIES = 3
WATCH_MINUTES = 90                      # how long after Phone.com accepts a fax we keep asking whether it was delivered
WATCH_EVERY = 120                       # seconds between checks
ROAD_EVERY = 45                         # seconds between Road pulls (phones -> office)
OCR_EVERY = 45                          # seconds between OCR passes over new incoming faxes (ocr.py)
FINAL_OK = ("sent", "delivered", "success", "succeeded", "completed", "complete", "ok")
FINAL_BAD = ("failed", "failure", "error", "busy", "no answer", "no_answer", "noanswer", "unreachable", "rejected",
             "cancelled", "canceled", "undeliver", "not delivered", "timeout", "timed out", "invalid")


class Worker(threading.Thread):
    def __init__(self, store, client_factory, tick=5):
        super().__init__(daemon=True)
        self.store, self.client_factory, self.tick = store, client_factory, tick
        self.last = {"transmit": "", "pull": "", "pull_ok": None, "pull_error": "", "backup": ""}
        self.road = None                                   # set by the App when Road is wired (road.py)
        self.ocr = None                                    # set by the App (ocr.py)
        self._stop = threading.Event()
        self._wake = threading.Event()

    def stop(self):
        self._stop.set(); self._wake.set()

    def poke(self):
        self._wake.set()

    # ------------------------------------------------------------ outbox
    def transmit_once(self):
        st = self.store
        client = self.client_factory()
        n = 0
        for meta_path in sorted((st.root / "outbox").glob("*.json")):
            try:
                m = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if m.get("status") not in ("queued", "retry"):
                continue
            if m.get("not_before") and m["not_before"] > now():
                continue
            pdf = meta_path.with_suffix(".pdf")
            m["status"] = "sending"; m["tries"] = int(m.get("tries") or 0) + 1; m["last_try"] = now()
            st.write("outbox/" + meta_path.name, m)
            if client is None:
                res = {"ok": False, "error": "Phone.com is not set up yet (Settings)."}
            else:
                try:
                    cov = meta_path.with_name(meta_path.stem + ".cover.pdf")
                    extra = [("cover.pdf", cov.read_bytes())] if m.get("cover") and cov.exists() else None
                    res = client.send(pdf.read_bytes(), m["to"], filename=m.get("filename") or "document.pdf", extra=extra)
                except Exception as e:
                    res = {"ok": False, "error": "send failed (%s)" % type(e).__name__}
            if res.get("ok"):
                m["status"] = "sent"; m["sent_at"] = now(); m["fax_id"] = res.get("fax_id") or ""; m["pages_sent"] = res.get("pages")
                m["error"] = ""
            elif m["tries"] < MAX_TRIES:
                m["status"] = "retry"; m["error"] = clean(res.get("error"), 160)
                m["not_before"] = (_dt.datetime.now() + _dt.timedelta(minutes=2 * m["tries"])).isoformat(timespec="seconds")
            else:
                m["status"] = "failed"; m["failed_at"] = now(); m["error"] = clean(res.get("error"), 160)
            st.write("outbox/" + meta_path.name, m)
            if m["status"] in ("sent", "failed"):
                st.append("log.jsonl", {"ts": now(), "id": m["id"], "to": m["to"], "to_name": m.get("to_name", ""), "from": m.get("by", ""),
                                        "subject": m.get("subject", ""), "pages": m.get("pages"), "status": m["status"], "error": m.get("error", ""),
                                        "fax_id": m.get("fax_id", ""), "tries": m["tries"]})
                st.audit("fax_" + m["status"], m.get("by", ""), m.get("error", "") or "ok", m["id"])
            n += 1
        self.last["transmit"] = now()
        return n

    # ------------------------------------------------------------ delivery watch (v1.1)
    def watch_once(self):
        """Phone.com says "accepted" the moment it takes a fax; the far machine can still refuse it minutes later. For every
        fax sent in the last WATCH_MINUTES we ask Phone.com how it went. Not delivered -> the row turns failed with the reason
        in plain words and Resend appears; delivered -> the row says so. Returns how many were checked."""
        st = self.store
        client = self.client_factory()
        if client is None:
            return 0
        n = 0
        cutoff = (_dt.datetime.now() - _dt.timedelta(minutes=WATCH_MINUTES * 2)).isoformat(timespec="seconds")
        for meta_path in sorted((st.root / "outbox").glob("*.json"))[-300:]:
            try:
                m = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if m.get("status") != "sent" or not m.get("fax_id") or (m.get("delivery") or {}).get("final"):
                continue
            sent_at = str(m.get("sent_at") or "")
            if not sent_at or sent_at < cutoff:
                continue
            d = dict(m.get("delivery") or {})
            try:
                age_min = (_dt.datetime.now() - _dt.datetime.fromisoformat(sent_at)).total_seconds() / 60.0
            except ValueError:
                continue
            try:
                r = client.status(m["fax_id"])
            except Exception as e:
                r = {"ok": False, "error": "status failed (%s)" % type(e).__name__}
            n += 1
            d.update(checks=int(d.get("checks") or 0) + 1, checked_at=now(), final=False)
            if not r.get("ok"):
                d["note"] = clean(r.get("error"), 120)
            else:
                low = r.get("status") or ""
                why = clean(r.get("reason"), 160)
                d.update(status=low, reason=why, note="")
                if any(x in low for x in FINAL_BAD) or (why and not any(x in low for x in FINAL_OK)):
                    d.update(final=True, ok=False)
                elif any(x in low for x in FINAL_OK):
                    d.update(final=True, ok=True)
            if not d["final"] and age_min > WATCH_MINUTES:
                d.update(final=True, ok=None, note="no final word from Phone.com in %d minutes" % WATCH_MINUTES)
            m["delivery"] = d
            if d["final"] and d.get("ok") is False:
                m["status"] = "failed"; m["failed_at"] = now()
                m["error"] = "not delivered: " + (d.get("reason") or d.get("status") or "the other fax machine did not take it")
                st.append("log.jsonl", {"ts": now(), "id": m["id"], "to": m["to"], "to_name": m.get("to_name", ""), "from": m.get("by", ""),
                                        "subject": m.get("subject", ""), "pages": m.get("pages"), "status": "failed", "error": m["error"],
                                        "fax_id": m.get("fax_id", ""), "tries": m.get("tries", 0), "undelivered": True})
                st.audit("fax_undelivered", m.get("by", ""), m["error"], m["id"])
            st.write("outbox/" + meta_path.name, m)
        self.last["watch"] = now()
        return n

    # ------------------------------------------------------------ inbox
    def pull_once(self):
        st = self.store
        client = self.client_factory()
        if client is None:
            self.last.update(pull=now(), pull_ok=False, pull_error="not set up")
            return 0
        try:
            items = client.received()
        except Exception as e:
            self.last.update(pull=now(), pull_ok=False, pull_error="pull failed (%s)" % type(e).__name__)
            return 0
        have = set(p.stem for p in (st.root / "inbox").glob("*.json"))
        mine = st.config().get("fax_number") or ""
        n = 0
        for it in items:
            if mine and it.get("to") and it.get("to") != mine:      # other numbers on the same account are not this desk's
                continue
            fid = "in-" + "".join(ch for ch in str(it.get("id") or "") if ch.isalnum())[:40]
            if not fid or fid in have:
                continue
            pdf = client.download(it.get("download_url") or "") if it.get("download_url") else b""
            if not pdf:
                continue
            (st.root / "inbox" / (fid + ".pdf")).write_bytes(pdf)
            meta = {"id": fid, "received": str(it.get("created_at") or now())[:19], "from": it.get("from") or "", "from_name": clean(it.get("from_name"), 80),
                    "to": it.get("to") or "", "pages": it.get("pages"), "opened_by": "", "opened_at": "", "assigned_to": "", "assigned_by": "", "assigned_at": "",
                    "handled": False, "handled_by": "", "handled_at": "", "handled_note": "", "spam": False, "pulled_at": now()}
            rules = st.rules()
            if meta["from"] in rules and rules[meta["from"]].get("to"):
                meta.update(assigned_to=rules[meta["from"]]["to"], assigned_by="rule", assigned_at=now())
            st.write("inbox/" + fid + ".json", meta)
            st.audit("fax_in", "", "pages_%s" % meta["pages"], fid)
            n += 1
        self.last.update(pull=now(), pull_ok=True, pull_error="")
        return n

    # ------------------------------------------------------------ loop
    def housekeeping(self):
        st = self.store
        c = st.config()
        day = _dt.date.today().isoformat()
        if self.last["backup"] != day:
            try:
                st.backup_daily()
                st.prune("inbox", c.get("keep_in_days") or 60)
                st.prune("outbox", c.get("keep_out_days") or 60)
            except Exception:
                pass
            self.last["backup"] = day

    def run(self):
        next_tx, next_pull, next_watch, next_road, next_ocr = 0, 0, 0, 0, time.time() + 20
        while not self._stop.is_set():
            t = time.time()
            try:
                if t >= next_tx or self._wake.is_set():
                    self._wake.clear()
                    self.transmit_once(); next_tx = time.time() + 60
                if t >= next_watch:
                    self.watch_once(); next_watch = time.time() + WATCH_EVERY
                if self.road is not None and t >= next_road:
                    try:
                        if self.road.state().get("office_id"):
                            self.road.pull_once()
                    except Exception:
                        pass
                    next_road = time.time() + ROAD_EVERY
                if self.ocr is not None and t >= next_ocr:
                    try:
                        self.ocr.once()
                    except Exception:
                        pass
                    next_ocr = time.time() + OCR_EVERY
                if t >= next_pull:
                    c = self.store.config()
                    if c.get("setup_done"):
                        self.pull_once()
                    next_pull = time.time() + 60 * max(1, int(c.get("pull_minutes") or 3))
                self.housekeeping()
            except Exception:
                pass
            self._wake.wait(self.tick)
