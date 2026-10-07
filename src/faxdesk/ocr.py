"""ocr.py - search the words inside incoming faxes. A fax is a picture; Tesseract (free, local) reads each new one once
and FaxDesk keeps the words in <state>/ocr/<id>.txt (pages separated by form feeds). Nothing leaves the PC.

Needs: Tesseract on this PC (https://github.com/UB-Mannheim/tesseract/wiki on Windows; brew install tesseract on Mac)
and PyMuPDF to turn PDF pages into images (bundled in the EXE; from source: pip install pymupdf).
Without either, search is simply off and the page says so. ASCII only."""
import datetime as _dt
import os
import re
import shutil
import subprocess
import sys
import time
import uuid

OCR_DPI = 200
PER_PASS = 6                       # faxes per pass, newest first
KEEP_DAYS = 120
MAX_PAGES = 60

if os.name == "nt":
    _si = subprocess.STARTUPINFO(); _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW; _si.wShowWindow = 0
    NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000), "startupinfo": _si, "stdin": subprocess.DEVNULL}
else:
    NO_WINDOW = {"stdin": subprocess.DEVNULL}


def tesseract(cfg=None):
    """Path of the Tesseract executable, or ''."""
    cands = [os.environ.get("FAXDESK_TESSERACT") or "", (cfg or {}).get("tesseract_path") or "", shutil.which("tesseract") or ""]
    if os.name == "nt":
        cands += [r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                  os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe")]
    elif sys.platform == "darwin":
        cands += ["/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


def have_renderer():
    try:
        import fitz  # noqa: F401
        return True
    except Exception:
        return False


def status(cfg=None):
    return {"engine": bool(tesseract(cfg)), "renderer": have_renderer()}


def render_pages(pdf_path, work, dpi=OCR_DPI):
    """Yield PNG file paths, one per page (caller deletes). Uses PyMuPDF."""
    import fitz
    doc = fitz.open(str(pdf_path))
    try:
        for i, pg in enumerate(doc):
            if i >= MAX_PAGES:
                break
            png = os.path.join(str(work), "ocr-" + uuid.uuid4().hex[:10] + ".png")
            pg.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY).save(png)
            yield png
    finally:
        doc.close()


def read_pdf(pdf_path, exe, work):
    """-> list of page texts (ASCII, cleaned)."""
    out = []
    for png in render_pages(pdf_path, work):
        try:
            r = subprocess.run([exe, png, "stdout", "-l", "eng", "--psm", "3"], capture_output=True, timeout=120, **NO_WINDOW)
            txt = r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""
        finally:
            try:
                os.unlink(png)
            except OSError:
                pass
        txt = "".join(ch if 32 <= ord(ch) < 127 or ch == "\n" else " " for ch in txt)
        out.append(re.sub(r"[ \t]+", " ", txt).strip())
    return out


class Ocr:
    def __init__(self, store):
        self.store = store
        self.dir = store.root / "ocr"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.work = store.root / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        self.stat = {"done": 0, "pending": 0, "failed": 0, "last": ""}
        self._cache = {}

    def once(self):
        """One pass: read up to PER_PASS unread inbox faxes, newest first. Prune text of faxes long gone."""
        cfg = self.store.config()
        exe = tesseract(cfg)
        if not exe or not have_renderer():
            return 0
        inbox = self.store.root / "inbox"
        pdfs = sorted(inbox.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        have = {p.stem for p in self.dir.glob("*.txt")}
        failed = {p.stem for p in self.dir.glob("*.fail")}
        todo = [p for p in pdfs if p.stem not in have and p.stem not in failed]
        self.stat.update(done=len(have), pending=len(todo), failed=len(failed))
        n = 0
        for p in todo[:PER_PASS]:
            try:
                pages = read_pdf(p, exe, self.work)
                tmp = self.dir / (p.stem + ".part")
                tmp.write_text("\f".join(pages), encoding="ascii", errors="replace")
                os.replace(str(tmp), str(self.dir / (p.stem + ".txt")))
                n += 1
            except Exception as e:
                (self.dir / (p.stem + ".fail")).write_text(type(e).__name__, encoding="ascii")
                self.store.audit("ocr", "", "failed_" + type(e).__name__, p.stem)
        if n:
            self.stat["last"] = _dt.datetime.now().isoformat(timespec="seconds")
            self.stat["done"] += n; self.stat["pending"] -= n
            self.store.audit("ocr", "", "read_%d" % n, "")
        cutoff = time.time() - KEEP_DAYS * 86400
        live = {p.stem for p in pdfs}
        for f in list(self.dir.glob("*.txt")) + list(self.dir.glob("*.fail")):
            try:
                if f.stem not in live and f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
        return n

    def text(self, fid):
        f = self.dir / (fid + ".txt")
        try:
            mt = f.stat().st_mtime
        except OSError:
            self._cache.pop(fid, None)
            return None
        c = self._cache.get(fid)
        if c and c[0] == mt:
            return c[1]
        t = f.read_text(encoding="ascii", errors="replace")
        self._cache[fid] = (mt, t)
        return t

    def search(self, q):
        """Case-insensitive 'contains' (or all words) over every inbox fax -> {id: {page, line}}."""
        q = re.sub(r"\s+", " ", str(q or "")).strip().lower()[:80]
        hits = {}
        if len(q) < 2:
            return hits
        words = q.split()
        inbox = self.store.root / "inbox"
        for f in self.dir.glob("*.txt"):
            fid = f.stem
            if not (inbox / (fid + ".pdf")).is_file():
                continue
            t = self.text(fid)
            if t is None:
                continue
            low = t.lower()
            if q in low:
                pos = low.index(q)
            elif all(w in low for w in words):
                pos = low.index(words[0])
            else:
                continue
            page = low.count("\f", 0, pos) + 1
            lines = low.replace("\f", "\n")
            ls, le = lines.rfind("\n", 0, pos) + 1, lines.find("\n", pos)
            line = t[ls:(le if le > 0 else len(t))].strip()
            hits[fid] = {"page": page, "line": line[:160]}
        return hits
