"""pdfmini.py - a tiny text-only PDF writer (stdlib): pages of Courier lines, letter size. Free Fax Desk uses it for the cover sheet and confirmations.
Pure ASCII in, PDF bytes out."""
import datetime as _dt

W, H = 612, 792                     # US letter, points
MARGIN, LEAD, SIZE = 40, 12, 9      # left/top margin, line height, font size
LINES_PER_PAGE = int((H - 2 * MARGIN) / LEAD)


def _esc(t):
    return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _ascii(t):
    return "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in t)


def jpeg_size(data):
    """(width, height) from a baseline/progressive JPEG's SOF marker, or None."""
    if not data or data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h = (data[i + 5] << 8) | data[i + 6]
            w = (data[i + 7] << 8) | data[i + 8]
            return (w, h) if w and h else None
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = (data[i + 2] << 8) | data[i + 3]
        i += 2 + seg
    return None


def build(pages, title="Report", logo=None):
    """pages = list of list-of-lines (str). Returns bytes. A line starting with '## ' is drawn bold-ish (bigger).
    logo = JPEG bytes drawn top-right of page 1 (max 170 x 60 pt), or None."""
    objs = []                       # object bodies, 1-based
    def add(body):
        objs.append(body)
        return len(objs)
    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")
    fontb = add("<< /Type /Font /Subtype /Type1 /BaseFont /Courier-Bold >>")
    img_id, img_draw = None, ""
    dims = jpeg_size(logo) if logo else None
    if dims:
        w, h = dims
        scale = min(170.0 / w, 60.0 / h, 1.0)
        dw, dh = w * scale, h * scale
        img_id = len(objs) + 1
        objs.append(None)           # placeholder: image body holds binary, written separately
        img_draw = "q %.2f 0 0 %.2f %.2f %.2f cm /Im1 Do Q\n" % (dw, dh, W - MARGIN - dw, H - MARGIN - dh + 8)
    page_ids, content_ids = [], []
    first = True
    for lines in pages:
        chunks = [lines[i:i + LINES_PER_PAGE] for i in range(0, max(1, len(lines)), LINES_PER_PAGE)] or [[]]
        for chunk in chunks:
            y = H - MARGIN
            ops = [(img_draw if (first and img_draw) else "") + "BT"]
            first = False
            for ln in chunk:
                ln = _ascii(ln)
                if ln.startswith("## "):
                    ops.append("/F2 %d Tf 1 0 0 1 %d %d Tm (%s) Tj" % (SIZE + 3, MARGIN, y, _esc(ln[3:])))
                else:
                    ops.append("/F1 %d Tf 1 0 0 1 %d %d Tm (%s) Tj" % (SIZE, MARGIN, y, _esc(ln)))
                y -= LEAD
            ops.append("ET")
            stream = "\n".join(ops)
            cid = add("<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
            content_ids.append(cid)
            page_ids.append(None)   # placeholder, filled after Pages id known
    pages_id = len(objs) + len(page_ids) + 1
    for i, cid in enumerate(content_ids):
        xo = (" /XObject << /Im1 %d 0 R >>" % img_id) if img_id else ""
        pid = add("<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Resources << /Font << /F1 %d 0 R /F2 %d 0 R >>%s >> /Contents %d 0 R >>"
                  % (pages_id, W, H, font, fontb, xo, cid))
        page_ids[i] = pid
    kids = " ".join("%d 0 R" % p for p in page_ids)
    real_pages_id = add("<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids)))
    assert real_pages_id == pages_id
    catalog = add("<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    info = add("<< /Title (%s) /Producer (deskstats pdfmini) /CreationDate (D:%s) >>" % (_esc(_ascii(title)), _dt.datetime.now().strftime("%Y%m%d%H%M%S")))
    out = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    offsets = []
    pos = len(out[0])
    for i, body in enumerate(objs, 1):
        offsets.append(pos)
        if body is None:            # the JPEG
            head = ("%d 0 obj\n<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n"
                    % (i, dims[0], dims[1], len(logo))).encode("latin-1")
            chunk = head + logo + b"\nendstream\nendobj\n"
        else:
            chunk = ("%d 0 obj\n%s\nendobj\n" % (i, body)).encode("latin-1")
        out.append(chunk)
        pos += len(chunk)
    xref = pos
    out.append(("xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + "".join("%010d 00000 n \n" % o for o in offsets)).encode("latin-1"))
    out.append(("trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, catalog, info, xref)).encode("latin-1"))
    return b"".join(out)
