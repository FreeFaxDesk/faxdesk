"""check_repo.py - run before every push: refuses if anything from the office, a token, or state slipped into the repo.
    python check_repo.py        (from the repo folder; exit 0 = clean)"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BAD_WORDS = [r"frye", r"lancaster", r"fryec", r"Z:\\", r"cowork", r"172\.16\.", r"Harry", r"chiro", r"bearer [a-z0-9]{20,}",
             r"\+1661\d{7}", r"pythoncore-3\.14", r"Administration\\"]
BAD_FILES = [r"^private/", r"config\.json$", r"logo\.jpg$", r"\.jsonl$", r"^dist/", r"^build/", r"\.spec$", r"__pycache__", r"\.pyc$"]
ALLOW_EXT = {".mjs", ".py", ".html", ".md", ".txt", ".bat", ".command", ".yml", ".png", ".ico", ".svg", ".jpg", ".css", ".js", ".json", ".xml", ".mp4", ""}


def main():
    bad = []
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d != ".git"]
        for f in fn:
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, ROOT).replace("\\", "/")
            if rel == "check_repo.py":
                continue
            if any(re.search(b, rel) for b in BAD_FILES):
                bad.append((rel, "file must not be in the repo"))
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext not in ALLOW_EXT:
                bad.append((rel, "unexpected file type " + ext))
            if ext in (".png", ".ico", ".jpg", ".svg"):
                continue
            try:
                text = open(p, "r", encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            if any(ord(ch) > 127 for ch in text) and ext in (".py", ".bat", ".command", ".txt"):
                bad.append((rel, "non-ASCII characters"))
            # Harry 2026-09-27: the clinic is named publicly in the press release, so the public-facing site text
            # (docs/index.html and docs/reviews.html) may say "Frye Chiropractic" / "Lancaster, California". Never in src/.
            public_ok = rel.replace("\\", "/") in ("docs/index.html", "docs/reviews.html")
            for w in BAD_WORDS:
                if public_ok and w in (r"frye", r"lancaster", r"chiro", r"Harry"):
                    continue
                m = re.search(w, text, re.I)
                if m:
                    line = text[:m.start()].count("\n") + 1
                    bad.append((rel, "line %d matches '%s'" % (line, w)))
    if bad:
        print("REFUSE - fix these before pushing:")
        for rel, why in bad:
            print("  %s: %s" % (rel, why))
        return 1
    print("clean - safe to push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
