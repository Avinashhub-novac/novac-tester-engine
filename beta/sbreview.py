"""SB stage: review of a storyboard (Word or PowerPoint) before the course is built.

The engine prepares everything Claude needs to review the storyboard against the team's SB checklist:
  * a picture of every SB page (for the screenshots in the report and for checking images),
  * the text page by page (so every issue can name its page),
  * the text with its formatting marked (**bold**, _italic_, ^superscript^) for formatting checks,
and runs the mechanical checks itself (spacing, superscripts, US/UK spelling, repeated sentences).
"""
import re
import subprocess
from pathlib import Path

from . import storyboard

# The SB checklist, grouped into the report's checks. Each rule is shown in the report next to its issue.
GROUPS = {
    "sb_language": "SB: Language",
    "sb_consistency": "SB: Consistency",
    "sb_structure": "SB: Structure & assessment",
    "sb_alignment": "SB: OST, VO, titles & images",
    "sb_standards": "SB: Standards & style",
    "sb_logic": "SB: Logic & facts",
}
RULES = [
    ("sb_language", "VO and OST content must be free of grammar, punctuation, spelling, casing, and spacing errors."),
    ("sb_structure", "Learning agenda and recap points must be included in the module."),
    ("sb_consistency", "Casing must be consistent, with uppercase used where required."),
    ("sb_consistency", "OSTs must follow consistent end punctuation."),
    ("sb_structure", "Questions and feedback must be included in the module."),
    ("sb_alignment", "Titles and subtitles must be relevant to the VO."),
    ("sb_consistency", "US/UK English usage must be consistent, as per course requirements."),
    ("sb_consistency", "Symbols, years, numbers, and units must be used consistently."),
    ("sb_consistency", "Italics and bold formatting must be applied consistently."),
    ("sb_alignment", "OST and VO should convey the same meaning."),
    ("sb_structure", "Check Your Understanding / Knowledge check logic must align with the rules page (number of questions, attempts) and be relevant to the content."),
    ("sb_consistency", "Superscripts must be formatted correctly."),
    ("sb_standards", "Content must not have duplication."),
    ("sb_alignment", "Images must be relevant to the content."),
    ("sb_alignment", "Menu titles must match screen titles."),
    ("sb_consistency", "Button casing must be consistent."),
    ("sb_standards", "Standards for the module must be followed (acronym spelled out at first use, consistent lead-ins, CYU intros, recap lead-ins)."),
    ("sb_standards", "Pronoun usage must be consistent throughout the module."),
    ("sb_standards", "Voice and style must remain consistent across the module."),
    ("sb_standards", "Grammatical parallelism must be maintained."),
    ("sb_logic", "Content must be logical and factually correct (facts, figures, calculations, dates, cause and effect, steps in the right order)."),
]
RULE_GROUP = {i + 1: g for i, (g, _) in enumerate(RULES)}

# "Issue found in" values of the QC template
FOUND_IN = {"spelling": "SB-Spelling", "grammar": "SB-Grammar", "casing": "SB-Casing", "vo": "SB-Audio Script",
            "ost": "SB-OST", "ost_vo": "SB-OST/Audio Script", "assessment": "Assessment", "menu": "Menu",
            "image": "Graphic", "title": "Slide Title", "flow": "Content Flow", "buttons": "Buttons",
            "objectives": "Objectives"}

# US / UK spelling pairs (US, UK). Only clear, common pairs, so a mix is a real inconsistency.
US_UK = [("color", "colour"), ("behavior", "behaviour"), ("favorite", "favourite"), ("honor", "honour"),
         ("labor", "labour"), ("neighbor", "neighbour"), ("center", "centre"), ("meter", "metre"),
         ("liter", "litre"), ("theater", "theatre"), ("organize", "organise"), ("organization", "organisation"),
         ("realize", "realise"), ("recognize", "recognise"), ("analyze", "analyse"), ("prioritize", "prioritise"),
         ("utilize", "utilise"), ("summarize", "summarise"), ("customize", "customise"), ("optimize", "optimise"),
         ("minimize", "minimise"), ("maximize", "maximise"), ("emphasize", "emphasise"), ("authorize", "authorise"),
         ("standardize", "standardise"), ("defense", "defence"), ("license", "licence"), ("catalog", "catalogue"),
         ("dialog", "dialogue"), ("fulfill", "fulfil"), ("enroll", "enrol"), ("traveled", "travelled"),
         ("traveling", "travelling"), ("canceled", "cancelled"), ("modeling", "modelling"), ("labeled", "labelled"),
         ("program", "programme"), ("gray", "grey"), ("check", "cheque"), ("aluminum", "aluminium"),
         ("judgment", "judgement"), ("acknowledgment", "acknowledgement"), ("percent", "per cent")]
AMBIGUOUS = {"program", "check", "license", "meter", "dialog"}   # valid in both in some senses: report as warning


# ---------------------------------------------------------------- formatting-marked text
def _mark(text, bold, italic, sup):
    t = text
    if not t.strip():
        return t
    if sup:
        t = f"^{t}^"
    if italic:
        t = f"_{t}_"
    if bold:
        t = f"**{t}**"
    return t


def _docx_marked(path):
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    doc = Document(str(path))

    def para(p):
        return "".join(_mark(r.text, bool(r.bold), bool(r.italic), bool(r.font.superscript)) for r in p.runs).strip()

    out = []
    for block in doc.element.body.iterchildren():
        tag = block.tag.rsplit("}", 1)[-1]
        if tag == "p":
            t = para(Paragraph(block, doc))
            if t:
                out.append(t)
        elif tag == "tbl":
            out.append("")
            for row in Table(block, doc).rows:
                cells, seen = [], set()
                for c in row.cells:
                    if id(c._tc) in seen:
                        continue
                    seen.add(id(c._tc))
                    cells.append(" / ".join(x for x in (para(p) for p in c.paragraphs) if x))
                if any(cells):
                    out.append(" | ".join(cells))
            out.append("")
    return "\n".join(out)


def _pptx_marked(path):
    from pptx import Presentation

    def runs(tf):
        lines = []
        for p in tf.paragraphs:
            parts = []
            for r in p.runs:
                rpr = r._r.find("{http://schemas.openxmlformats.org/drawingml/2006/main}rPr")
                base = int(rpr.get("baseline", "0")) if rpr is not None and rpr.get("baseline") else 0
                parts.append(_mark(r.text, bool(r.font.bold), bool(r.font.italic), base > 0))
            t = "".join(parts).strip()
            if t:
                lines.append(t)
        return lines

    out = []
    for i, slide in enumerate(Presentation(str(path)).slides, 1):
        out.append(f"=== SB slide {i} ===")
        for sh in slide.shapes:
            if sh.has_text_frame:
                out += runs(sh.text_frame)
            if getattr(sh, "has_table", False) and sh.has_table:
                for row in sh.table.rows:
                    out.append(" | ".join(" / ".join(runs(c.text_frame)) for c in row.cells))
        if slide.has_notes_slide:
            n = runs(slide.notes_slide.notes_text_frame)
            if n:
                out.append("Notes: " + " / ".join(n))
    return "\n".join(out)


def marked_text(path: Path):
    ext = path.suffix.lower()
    if ext == ".doc":
        path, ext = storyboard._convert(path, "docx"), ".docx"
    if ext == ".ppt":
        path, ext = storyboard._convert(path, "pptx"), ".pptx"
    return _docx_marked(path) if ext == ".docx" else _pptx_marked(path)


def page_texts(path: Path, out_dir: Path):
    """Text of each SB page as it appears in the page pictures: {page: text}."""
    pdf = path if path.suffix.lower() == ".pdf" else storyboard._convert(path, "pdf")
    txt = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, timeout=300).stdout
    pages = {i: t for i, t in enumerate(txt.split("\f"), 1) if t.strip()}
    imgs = subprocess.run(["pdfimages", "-list", str(pdf)], capture_output=True, text=True, timeout=300).stdout
    with_images = sorted({int(l.split()[0]) for l in imgs.splitlines()[2:] if l.split() and l.split()[0].isdigit()
                          and int(l.split()[3]) >= 60 and int(l.split()[4]) >= 60})
    return pages, with_images


def page_titles(pages):
    """A short title per page (first meaningful line), used as the screen title in the report."""
    out = {}
    for n, t in pages.items():
        lines = [re.sub(r"\s+", " ", l).strip() for l in t.splitlines() if l.strip()]
        title = ""
        for l in lines:
            m = re.search(r"(?:screen|slide)\s*title\s*[:|]?\s*(.+)", l, re.I)
            if m:
                title = m.group(1).strip(" |")
                break
        out[n] = (title or (lines[0] if lines else f"Page {n}"))[:80]
    return out


# ---------------------------------------------------------------- mechanical checks
def auto_checks(pages, marked, variant):
    """Returns issues found by rules (no judgement needed). Each: {page, rule, group, issue, quote, suggestion, severity}.
    Text checks run on the storyboard's own text (exact spacing); the page is then found in the page pictures' text."""
    issues = []
    if variant not in ("us", "uk"):
        variant = majority_variant(pages)
    norm_pages = {n: re.sub(r"\s+", " ", t) for n, t in pages.items()}

    def page_of(snippet):
        key = re.sub(r"\s+", " ", snippet).strip()
        for k in (key, key[:40], key[-40:]):
            for n, t in norm_pages.items():
                if k and k in t:
                    return n
        return min(pages) if pages else 1

    def add(rule, issue, quote, suggestion="", severity="warning", found_in="ost", page=None):
        issues.append({"page": page or page_of(quote), "rule": rule, "group": RULE_GROUP[rule], "issue": issue,
                       "quote": quote, "suggestion": suggestion, "severity": severity, "found_in": found_in})

    def snip(core, a, b):
        """A few whole words around core[a:b]."""
        a, b = max(0, a - 25), min(len(core), b + 25)
        while a > 0 and core[a - 1] != " ":
            a -= 1
        while b < len(core) and core[b] != " ":
            b += 1
        return core[a:b].strip()

    plain = re.sub(r"\*\*|\^|(?<!\w)_|_(?!\w)", "", marked)
    for line in plain.splitlines():
        for cell in line.split(" | "):
            core = cell.strip()
            if not core or re.fullmatch(r"=== SB slide \d+ ===", core):
                continue
            m = re.search(r"\S( {2,})(?=\S)", core)
            if m:
                q = snip(core, m.start(), m.end())
                add(1, "Extra space between words", q, re.sub(r" {2,}", " ", q))
            m = re.search(r"\w (,|\.(?!\.)|;|:|\?|!)(\s|$)", core)
            if m:
                q = snip(core, m.start(), m.end())
                add(1, "Space before a punctuation mark", q, q.replace(" " + m.group(1), m.group(1)))
            m = re.search(r"[a-z][,;][A-Za-z]", core)
            if m:
                q = snip(core, m.start(), m.end())
                add(1, "Missing space after a punctuation mark", q)
    # US / UK spelling
    for n, text in pages.items():
        words = set(re.findall(r"[a-z]+", text.lower()))
        for us, uk in US_UK:
            if us in AMBIGUOUS:   # correct in both variants in some senses (check, program): left to the reviewer
                continue
            wrong, right = (uk, us) if variant == "us" else (us, uk)
            for w in words:
                if w == wrong or (len(wrong) > 4 and w.startswith(wrong) and w[len(wrong):] in ("s", "d", "ed", "ing", "es", "al", "ally")):
                    if w.startswith(right):
                        continue
                    add(7, f"{'UK' if variant == 'us' else 'US'} spelling in a {'US' if variant == 'us' else 'UK'} English course",
                        w, right + w[len(wrong):], "warning" if us in AMBIGUOUS else "error", "spelling", page=n)
    # ordinals not set as superscript (needs the formatting-marked text)
    for m in re.finditer(r"\b(\d+)(st|nd|rd|th)\b", re.sub(r"\^[^^]*\^", "", marked)):
        add(12, "Ordinal ending is not in superscript", m.group(0), f"{m.group(1)} with “{m.group(2)}” in superscript")
    # the same long sentence more than once
    seen = set()
    for sent in re.split(r"(?<=[.!?])\s+|\n", plain):
        sent = sent.strip(" |-•")
        if len(sent.split()) >= 10:
            key = re.sub(r"[^\w ]", "", sent.lower())
            if key in seen:
                k = re.sub(r"\s+", " ", sent).strip()[:60]
                hits = [n for n, t in norm_pages.items() if k in t]
                add(13, "The same sentence appears more than once", sent[:200], "", "warning", "flow",
                    page=hits[1] if len(hits) > 1 else None)
            seen.add(key)
    out, keys = [], set()
    for i in issues:   # one finding per (page, rule, quote)
        k = (i["page"], i["rule"], i["quote"].lower())
        if k not in keys:
            keys.add(k)
            out.append(i)
    return out


def _page_of(pages, needle):
    for n, t in pages.items():
        if needle in t:
            return n
    return min(pages) if pages else 1


def majority_variant(pages):
    """'us' or 'uk': whichever spelling the storyboard mostly uses (US when equal)."""
    words = re.findall(r"[a-z]+", " ".join(pages.values()).lower())
    us = sum(1 for w in words for a, b in US_UK if a not in AMBIGUOUS and w.startswith(a) and not w.startswith(b))
    uk = sum(1 for w in words for a, b in US_UK if a not in AMBIGUOUS and w.startswith(b))
    return "uk" if uk > us else "us"
