"""store.py - Free Fax Desk state on THIS PC. JSON files, atomic writes, daily backups, a sealed token. Stdlib. ASCII.

State folder (override with env FAXDESK_STATE):
  Windows  %LOCALAPPDATA%\\FaxDesk      Mac  ~/Library/Application Support/FaxDesk      Linux  ~/.local/share/faxdesk
Files: config.json, people.json, book.json, rules.json, contacts.json, inbox/<id>.pdf + <id>.json, outbox/<id>.pdf + <id>.json,
       log.jsonl (sent/failed), audit.jsonl (metadata only), backups/YYYYMMDD/.
The Phone.com token is sealed: Windows -> DPAPI (current user); elsewhere -> stored in config.json with the file at mode 600.
"""
import base64
import ctypes
import datetime as _dt
import json
import os
import re
import shutil
import sys
import threading
from pathlib import Path

_lock = threading.RLock()


def default_state_dir():
    if os.environ.get("FAXDESK_STATE"):
        return Path(os.environ["FAXDESK_STATE"])
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "FaxDesk"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FaxDesk"
    return Path.home() / ".local" / "share" / "faxdesk"


def now():
    return _dt.datetime.now().isoformat(timespec="seconds")


def clean(s, n=200):
    """ASCII, printable, trimmed."""
    s = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in str(s or ""))
    return re.sub(r"\s+", " ", s).strip()[:n]


class Store:
    def __init__(self, root=None):
        self.root = Path(root) if root else default_state_dir()
        for d in ("inbox", "outbox", "backups"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(self.root), 0o700)
        except OSError:
            pass

    # ------------------------------------------------------------ json files
    def path(self, name):
        return self.root / name

    def read(self, name, default):
        try:
            with open(str(self.root / name), "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def write(self, name, obj, private=False):
        with _lock:
            p = self.root / name
            tmp = p.with_suffix(p.suffix + ".part")
            with open(str(tmp), "w", encoding="utf-8", newline="\n") as f:
                json.dump(obj, f, indent=1, ensure_ascii=True)
            if private:
                try:
                    os.chmod(str(tmp), 0o600)
                except OSError:
                    pass
            os.replace(str(tmp), str(p))

    def append(self, name, row):
        with _lock:
            with open(str(self.root / name), "a", encoding="ascii") as f:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

    def audit(self, event, who="", outcome="", ref=""):
        """Metadata only: never a document, never a full number (last 4 only)."""
        self.append("audit.jsonl", {"ts": now(), "event": clean(event, 40), "who": clean(who, 40), "outcome": clean(outcome, 80), "ref": clean(ref, 40)})

    # ------------------------------------------------------------ token sealing
    @staticmethod
    def _dpapi(data, protect):
        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]
        buf = ctypes.create_string_buffer(data, len(data))
        inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out = BLOB()
        fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
        if not fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
            raise OSError("DPAPI failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)

    def seal(self, token):
        token = str(token or "").encode("utf-8")
        if not token:
            return ""
        if os.name == "nt":
            return "dpapi:" + base64.b64encode(self._dpapi(token, True)).decode("ascii")
        return "plain:" + base64.b64encode(token).decode("ascii")

    def unseal(self, sealed):
        sealed = str(sealed or "")
        try:
            if sealed.startswith("dpapi:"):
                return self._dpapi(base64.b64decode(sealed[6:]), False).decode("utf-8")
            if sealed.startswith("plain:"):
                return base64.b64decode(sealed[6:]).decode("utf-8")
        except Exception:
            return ""
        return ""

    # ------------------------------------------------------------ config
    DEFAULTS = {"office": "", "token": "", "voip_id": "", "extension": "", "fax_number": "", "lan": False, "port": 8750,
                "pull_minutes": 3, "keep_in_days": 60, "keep_out_days": 60, "setup_done": False, "api": "",
                "office_line": "", "disclaimer": "standard", "disclaimer_text": "", "license": ""}

    def config(self):
        c = dict(self.DEFAULTS)
        c.update(self.read("config.json", {}) or {})
        return c

    def save_config(self, changes):
        c = self.config()
        for k, v in changes.items():
            if k == "token":
                if v:
                    c["token"] = self.seal(v)
            elif k in self.DEFAULTS:
                c[k] = v
        self.write("config.json", c, private=True)
        return c

    def token(self):
        return self.unseal(self.config().get("token"))

    # ------------------------------------------------------------ people / book / rules
    def people(self):
        p = self.read("people.json", [])
        return [clean(n, 40) for n in p if clean(n, 40)] if isinstance(p, list) else []

    def save_people(self, names):
        seen, out = set(), []
        for n in names:
            n = clean(n, 40)
            if n and n.lower() not in seen:
                seen.add(n.lower()); out.append(n)
        self.write("people.json", out)
        return out

    def book(self):
        b = self.read("book.json", [])
        return b if isinstance(b, list) else []

    def save_book(self, rows):
        self.write("book.json", rows)

    def rules(self):
        r = self.read("rules.json", {})
        return r if isinstance(r, dict) else {}

    def contacts(self):
        c = self.read("contacts.json", {})
        return c if isinstance(c, dict) else {}

    # ------------------------------------------------------------ backups + retention
    def backup_daily(self, keep=30):
        day = _dt.date.today().strftime("%Y%m%d")
        dest = self.root / "backups" / day
        if dest.exists():
            return 0
        part = self.root / "backups" / (day + ".part")
        part.mkdir(parents=True, exist_ok=True)
        n = 0
        for f in self.root.glob("*.json"):
            shutil.copy2(str(f), str(part / f.name)); n += 1
        for f in self.root.glob("*.jsonl"):
            shutil.copy2(str(f), str(part / f.name)); n += 1
        os.replace(str(part), str(dest))
        olds = sorted(p for p in (self.root / "backups").iterdir() if p.is_dir() and re.match(r"^\d{8}$", p.name))
        for old in olds[:-keep]:
            shutil.rmtree(str(old), ignore_errors=True)
        return n

    def prune(self, folder, days):
        """Delete PDFs (+ sidecars) older than `days` in inbox/ or outbox/. Returns count."""
        cutoff = _dt.datetime.now() - _dt.timedelta(days=max(1, int(days)))
        n = 0
        for f in (self.root / folder).glob("*.json"):
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
                ts = _dt.datetime.fromisoformat(str(meta.get("received") or meta.get("queued_at") or "")[:19])
            except (OSError, ValueError, TypeError):
                continue
            if ts < cutoff:
                for ext in (".json", ".pdf"):
                    try:
                        os.remove(str(f.with_suffix(ext)))
                    except OSError:
                        pass
                n += 1
        return n
