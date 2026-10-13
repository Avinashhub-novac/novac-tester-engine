"""Reads a storyboard (SB) into plain text for content mapping.

Storyboards are usually Word documents with one table per screen (screen no, OST, VO/audio
script, developer notes); PDFs, PowerPoint and Excel storyboards are read too. Table rows
are kept as "cell | cell | cell" lines so screen numbers stay next to their content.
"""
import re
import subprocess
import tempfile
from pathlib import Path

SUPPORTED = {".docx", ".doc", ".rtf", ".odt", ".pdf", ".pptx", ".ppt", ".xlsx", ".xls"}


def _clean(t):
    return re.sub(r"[ \t]+", " ", (t or "").replace(" ", " ")).strip()


def _docx(path):
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    doc = Document(str(path))
    out = []
    for block in doc.element.body.iterchildren():
        tag = block.tag.rsplit("}", 1)[-1]
        if tag == "p":
            t = _clean(Paragraph(block, doc).text)
            if t:
                out.append(t)
        elif tag == "tbl":
            out.append("")
            for row in Table(block, doc).rows:
                cells, seen = [], set()
                for c in row.cells:
                    if id(c._tc) in seen:  # merged cells repeat
                        continue
                    seen.add(id(c._tc))
                    cells.append(_clean(c.text).replace("\n", " / "))
                if any(cells):
                    out.append(" | ".join(cells))
            out.append("")
    return "\n".join(out)


def _pdf(path):
    p = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=300)
    pages = p.stdout.split("\f")
    return "\n".join(f"=== SB page {i} ===\n{t}" for i, t in enumerate(pages, 1) if t.strip())


def _pptx(path):
    from pptx import Presentation
    out = []
    for i, slide in enumerate(Presentation(str(path)).slides, 1):
        out.append(f"=== SB slide {i} ===")
        for sh in slide.shapes:
            if sh.has_text_frame:
                t = _clean(sh.text_frame.text)
                if t:
                    out.append(t)
            if getattr(sh, "has_table", False) and sh.has_table:
                for row in sh.table.rows:
                    out.append(" | ".join(_clean(c.text) for c in row.cells))
        if slide.has_notes_slide:
            n = _clean(slide.notes_slide.notes_text_frame.text)
            if n:
                out.append("Notes: " + n)
    return "\n".join(out)


def _xlsx(path):
    from openpyxl import load_workbook
    wb = load_workbook(str(path), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"=== Sheet {ws.title} ===")
        for row in ws.iter_rows(values_only=True):
            cells = [_clean(str(v)) for v in row if v is not None and str(v).strip()]
            if cells:
                out.append(" | ".join(cells))
    return "\n".join(out)


def _convert(path, fmt):
    tmp = Path(tempfile.mkdtemp())
    subprocess.run(["soffice", "--headless", "--convert-to", fmt, "--outdir", str(tmp), str(path)],
                   capture_output=True, timeout=300)
    hits = list(tmp.glob("*." + fmt))
    if not hits:
        raise ValueError(f"Could not convert the storyboard ({path.suffix}) to {fmt}")
    return hits[0]


def read_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        raise ValueError(f"Storyboard type {ext} is not supported. Use Word, PDF, PowerPoint or Excel.")
    if ext in (".doc", ".rtf", ".odt"):
        return _docx(_convert(path, "docx"))
    if ext == ".ppt":
        return _pptx(_convert(path, "pptx"))
    if ext == ".xls":
        return _xlsx(_convert(path, "xlsx"))
    return {".docx": _docx, ".pdf": _pdf, ".pptx": _pptx, ".xlsx": _xlsx}[ext](path)


# ---------------------------------------------------------------- page pictures (for "Not as per SB" evidence)
DPI = 150
WORD_RE = re.compile(r"[\w']+")


def _norm(w):
    return re.sub(r"[^\w']+", "", w.lower())


def render_pages(path: Path, out_dir: Path, max_pages=300):
    """Saves every SB page as a picture (out_dir/p-N.png) and the position of each word on it
    (out_dir/words.json: {"1": [[word, x, y, w, h], …]} in picture pixels). Returns the page count."""
    import html
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = path if path.suffix.lower() == ".pdf" else _convert(path, "pdf")
    subprocess.run(["pdftoppm", "-r", str(DPI), "-png", "-l", str(max_pages), str(pdf), str(out_dir / "p")],
                   capture_output=True, timeout=600)
    pages = {}
    for p in out_dir.glob("p-*.png"):   # pdftoppm pads the number (p-01.png): store as p-1.png
        n = int(p.stem.split("-")[-1])
        p.rename(out_dir / f"page{n}.png")
    bbox = subprocess.run(["pdftotext", "-bbox-layout", "-l", str(max_pages), str(pdf), "-"],
                          capture_output=True, text=True, timeout=600).stdout
    k = DPI / 72.0
    for n, body in enumerate(re.findall(r"<page[^>]*>(.*?)</page>", bbox, re.S), 1):
        words = []
        for m in re.finditer(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>', body):
            x0, y0, x1, y1 = (float(m.group(i)) * k for i in range(1, 5))
            words.append([html.unescape(m.group(5)), round(x0, 1), round(y0, 1), round(x1 - x0, 1), round(y1 - y0, 1)])
        pages[str(n)] = words
    (out_dir / "words.json").write_text(__import__("json").dumps(pages))
    return len(pages)


def find_on_pages(words_by_page, text, page_hint=None):
    """The SB page and box that best match `text` (the SB wording quoted in an issue).
    Returns (page number, [x, y, w, h] or None, score 0..1)."""
    want = [_norm(w) for w in WORD_RE.findall(text or "") if _norm(w)]
    if not want:
        return (int(page_hint) if page_hint else None), None, 0.0
    want = want[:40]
    best = (None, None, 0.0)
    for pg, words in words_by_page.items():
        norm = [_norm(w[0]) for w in words]
        n = len(want)
        # longest run of the quoted words appearing in order on this page (allowing small gaps)
        for i in range(len(norm)):
            if norm[i] != want[0] and (n < 2 or norm[i] != want[1]):
                continue
            j, hit, last = i, 0, i
            for wv in want:
                for jj in range(j, min(j + 4, len(norm))):
                    if norm[jj] == wv:
                        hit += 1
                        last, j = jj, jj + 1
                        break
            score = hit / n
            if page_hint and str(page_hint) == pg:
                score += 0.05
            if score > best[2]:
                sel = words[i:last + 1]
                x0 = min(w[1] for w in sel); y0 = min(w[2] for w in sel)
                x1 = max(w[1] + w[3] for w in sel); y1 = max(w[2] + w[4] for w in sel)
                best = (int(pg), [x0, y0, x1 - x0, y1 - y0], score)
    if best[2] < 0.5:   # fall back to the page sharing the most words
        ws = set(want)
        scores = [(len(ws & {_norm(w[0]) for w in words}) / len(ws), pg) for pg, words in words_by_page.items()]
        if scores:
            sc, pg = max(scores)
            if page_hint and sc < 0.3:
                return int(page_hint), None, sc
            if sc >= 0.3:
                return int(pg), None, sc
        return (int(page_hint) if page_hint else None), None, best[2]
    return best
