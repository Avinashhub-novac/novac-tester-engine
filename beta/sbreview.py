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
    ("sb_alignment", "OST must fit its text area and must not run into or cover pictures or other text."),
    ("sb_logic", "Screens must follow the learning order (agenda, menu and topic sequence); no screen may be out of place."),
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

    out, i = [], 0
    for slide in Presentation(str(path)).slides:
        if slide._element.get("show") == "0":
            continue   # hidden slides are not in the page pictures: numbering follows the pictures
        i += 1
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
# Rows of the SB template that are notes for the developer, not course content: they are only spell-checked by the
# reviewer, never checked for spacing, duplication or OST / VO match (the same note on many screens is normal).
META_LABEL = re.compile(
    r"^\W*(screen\s*objective|objective\s*of\s*the\s*screen|description|screen\s*description|developer|dev\.?\s*notes?|"
    r"notes?\s*(to|for)\s*(the\s*)?(developer|dev|graphics?|team)|graphic(s)?(\s*(notes?|instructions?|description))?|"
    r"animation|interactivity|interaction|instructions?\s*(to|for)|navigation|learner\s*action|audio\s*script|"
    r"programming|programmer|reference|references|source|image\s*(id|ref|reference|description)|visual|layout|"
    r"screen\s*type|template|asset|media|transition|timing|sync)\b", re.I)
DEV_SENTENCE = re.compile(
    r"\b(the\s+learner\s+(clicks?|selects?|drags?|hovers?|proceeds?|navigates?|is\s+taken)|learners?\s+(click|select|drag)|"
    r"clicks?\s+(on\s+)?(the\s+)?(next|submit|button|icon|tab)|screen\s+(appears|presents|shows|displays)|"
    r"(appears|is\s+displayed|is\s+shown)\s+on\s+(the\s+)?screen|added\s+in\s+the\s+notes|notes\s+section|"
    r"(animated|animation)\b|the\s+next\s+button|placeholder|on\s+click|fade[sd]?\s+in|pop[- ]?up\s+(appears|opens))", re.I)
RECAP = re.compile(r"\b(recap|summary|summari[sz]e|key\s+takeaways?|let'?s\s+review|review\s+of)\b", re.I)


def _sections(marked):
    """{slide number: text} for a PowerPoint storyboard (one slide = one page of the pictures)."""
    out, cur = {}, None
    for line in marked.splitlines():
        m = re.fullmatch(r"=== SB slide (\d+) ===", line.strip())
        if m:
            cur = int(m.group(1))
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def _content_cells(line):
    """The course-content pieces of one line of SB text: developer rows and notes are left out."""
    cells = [c.strip() for c in line.split(" | ")]
    if cells and META_LABEL.match(cells[0]):
        return []   # a row such as "Description | …", "Audio script | …", "Screen objective | …"
    out = []
    for c in cells:
        if c.lower().startswith("notes: "):
            c = c[7:]   # PowerPoint speaker notes hold the VO: they are content
        for piece in c.split(" / "):   # our own joiner between paragraphs of one cell
            piece = piece.strip()
            if piece and not META_LABEL.match(piece) and not re.fullmatch(r"=== SB slide \d+ ===", piece):
                out.append(piece)
    return out


def auto_checks(pages, marked, variant):
    """Returns issues found by rules (no judgement needed). Each: {page, rule, group, issue, quote, suggestion, severity}.
    Text checks run on the storyboard's own text (exact spacing), on course content only (VO, OST, questions and
    feedback – never the notes for the developer); the page is the slide the text is on (PowerPoint) or the page
    of the pictures that holds it (Word)."""
    issues = []
    if variant not in ("us", "uk"):
        variant = majority_variant(pages)
    norm_pages = {n: re.sub(r"\s+", " ", t) for n, t in pages.items()}
    plain = re.sub(r"\*\*|\^|(?<!\w)_|_(?!\w)", "", marked)
    sections = _sections(plain)   # PowerPoint: the text of each slide, speaker notes included

    def page_of(snippet, prefer=None):
        key = re.sub(r"\s+", " ", snippet).strip()
        if prefer:
            return prefer
        for k in (key, key[:40], key[-40:]):
            for n, t in norm_pages.items():
                if k and k in t:
                    return n
        return None

    def add(rule, issue, quote, suggestion="", severity="warning", found_in="ost", page=None, detail="", fix=""):
        pg = page or page_of(quote)
        if not pg:
            return   # the text could not be placed on a page: not reported rather than shown on the wrong page
        issues.append({"page": pg, "rule": rule, "group": RULE_GROUP[rule], "issue": issue, "detail": detail,
                       "quote": quote, "suggestion": suggestion, "severity": severity, "found_in": found_in, "fix": fix})

    def around(core, a, b):
        """The words just before and after a spot: (“recognition”, “and”)."""
        left = re.findall(r"\S+", core[:a])
        right = re.findall(r"\S+", core[b:])
        return (left[-1] if left else ""), (right[0] if right else "")

    def snip(core, a, b):
        """A few whole words around core[a:b]."""
        a, b = max(0, a - 25), min(len(core), b + 25)
        while a > 0 and core[a - 1] != " ":
            a -= 1
        while b < len(core) and core[b] != " ":
            b += 1
        return core[a:b].strip()

    # (page, content piece) in reading order
    if sections:
        pieces = [(n, c) for n in sorted(sections) for line in sections[n].splitlines() for c in _content_cells(line)]
    else:
        pieces = [(None, c) for line in plain.splitlines() for c in _content_cells(line)]
    for pg, core in pieces:
        m = re.search(r"\S( {2,})(?=\S)", core)
        if m:
            l, r = around(core, m.start(1), m.end(1))
            q = f"{l}{m.group(1)}{r}"   # just the two words around the gap: that is what gets marked
            add(1, "Extra space between words", q, f"{l} {r}", page=pg,
                fix=f"Remove the extra space between “{l}” and “{r}”.")
        m = re.search(r"\w (,|\.(?!\.)|;|:|\?|!)(\s|$)", core)
        if m:
            q = snip(core, m.start(), m.end())
            l, _ = around(core, m.start() + 1, m.start() + 1)
            add(1, "Space before a punctuation mark", q, q.replace(" " + m.group(1), m.group(1)), page=pg,
                fix=f"Remove the space between “{l}” and “{m.group(1)}”.")
        m = re.search(r"[a-z][,;][A-Za-z]", core)
        if m:
            q = snip(core, m.start(), m.end())
            w = re.search(r"\S*$", core[:m.start() + 2]).group(0) + re.match(r"\S*", core[m.start() + 2:]).group(0)
            add(1, "Missing space after a punctuation mark", q, page=pg,
                fix=f"Add a space after “{m.group(0)[1]}” in “{w}”.")
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
                        w, right + w[len(wrong):], "warning" if us in AMBIGUOUS else "error", "spelling", page=n,
                        fix=f"Change “{w}” to “{right + w[len(wrong):]}”.")
    # ordinals not set as superscript (needs the formatting-marked text)
    unsup = re.sub(r"\^[^^]*\^", "", marked)
    for sec_n, sec in (_sections(unsup).items() if sections else [(None, unsup)]):
        for m in re.finditer(r"\b(\d+)(st|nd|rd|th)\b", sec):
            add(12, "Ordinal ending is not in superscript", m.group(0), f"{m.group(1)} with “{m.group(2)}” in superscript",
                page=sec_n, fix=f"Set “{m.group(2)}” in “{m.group(0)}” as superscript.")
    # duplicated content is judged by the reviewer (rule 13): reused option lists, recaps and repeated
    # instructions are normal, so a word-for-word match alone is not a mistake
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


def _norm_word(w):
    return re.sub(r"[^\w]", "", (w or "").lower())


def layout_checks(path: Path, sb_dir: Path):
    """PowerPoint storyboards: text that runs out of its text box (into a picture, or off the slide).
    Measured, not judged: the words LibreOffice draws on each page picture (sb_dir/words.json) are compared with the
    text boxes and pictures of the slide. Returns issues in the auto_checks format (rule 22)."""
    import json
    if path.suffix.lower() != ".pptx":
        return []
    try:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        from PIL import Image
        prs = Presentation(str(path))
        words_by_page = json.loads((sb_dir / "words.json").read_text())
    except Exception:
        return []
    W, H = prs.slide_width, prs.slide_height
    visible = [s for s in prs.slides if s._element.get("show") != "0"]   # hidden slides are not in the PDF
    out = []
    for n, slide in enumerate(visible, 1):
        words = words_by_page.get(str(n)) or []
        pic = sb_dir / f"page{n}.png"
        if not words or not pic.exists():
            continue
        with Image.open(pic) as im:
            pw, ph = im.size
        kx, ky = pw / W, ph / H

        def box(sh):
            return (sh.left * kx, sh.top * ky, (sh.left + sh.width) * kx, (sh.top + sh.height) * ky)
        texts, pics = [], []
        for sh in slide.shapes:
            if sh.left is None or sh.width is None:
                continue
            if sh.shape_type == MSO_SHAPE_TYPE.PICTURE:
                pics.append(box(sh))
            elif getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip():
                toks = [_norm_word(t) for t in sh.text_frame.text.split()]
                texts.append({"box": box(sh), "words": {t for t in toks if t}, "text": sh.text_frame.text.strip(), "spill": []})
            elif getattr(sh, "has_table", False):
                texts.append({"box": box(sh), "words": set(), "text": "", "spill": [], "table": True})
        inside = lambda w, b, tol=3: b[0] - tol <= w[1] + w[3] / 2 <= b[2] + tol and b[1] - tol <= w[2] + w[4] / 2 <= b[3] + tol
        for w in words:
            if any(inside(w, t["box"]) for t in texts):
                continue
            nw = _norm_word(w[0])
            if not nw:
                continue
            cx = w[1] + w[3] / 2
            # the text box this word belongs to: same column, the word is above or below it, and the box holds the word
            cand = [t for t in texts if not t.get("table") and nw in t["words"] and t["box"][0] - 4 <= cx <= t["box"][2] + 4]
            if not cand:
                continue
            t = min(cand, key=lambda t: min(abs(w[2] - t["box"][3]), abs(t["box"][1] - (w[2] + w[4]))))
            t["spill"].append(w)
        for t in texts:
            sp = t["spill"]
            if len(sp) < 2:
                continue
            spill_box = (min(w[1] for w in sp), min(w[2] for w in sp), max(w[1] + w[3] for w in sp), max(w[2] + w[4] for w in sp))
            over_pic = any(not (spill_box[2] < p[0] or spill_box[0] > p[2] or spill_box[3] < p[1] or spill_box[1] > p[3]) for p in pics)
            off = spill_box[3] > ph - 2 or spill_box[2] > pw - 2
            first = " ".join(t["text"].split()[:8])
            issue = ("OST runs out of its text box into the picture" if over_pic else
                     "OST runs off the slide" if off else "OST runs out of its text box")
            out.append({"page": n, "rule": 22, "group": "sb_alignment", "severity": "error", "issue": issue,
                        "quote": first, "found_in": "ost",
                        "detail": f"{len(sp)} words are drawn outside the text box"
                                  + (" and over a picture." if over_pic else "."),
                        "fix": "Shorten the OST or resize its box so it fits and does not cover the picture."})
    return out
