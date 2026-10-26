"""Stage 1: English storyboard (SB) -> target-language storyboard (Hindi, Tamil, Malayalam, Telugu,
Kannada, Punjabi, Urdu or Arabic), same file format and layout.

    extract  reads every piece of text in the SB (Word, PowerPoint or Excel), groups identical
             texts, and writes batches for Claude to translate (batches/bNN.json).
    check    validates one batch answer Claude wrote (batches/bNN.out.json) and moves the progress bar.
    apply    writes the translation back into a copy of the SB, keeping tables, images, styles and
             run formatting (bold/italic/colour), sets the language's font and proofing language so
             Word/PowerPoint show it correctly, and turns translated paragraphs right-to-left for
             Urdu and Arabic. Also builds the bilingual English–target review workbook.

Text with mixed formatting inside one paragraph is handed to Claude with numbered tags,
e.g. "Click <1>Next</1> to continue", so bold or coloured words stay bold or coloured.
"""
import copy
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from lxml import etree

from . import langs

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
w = lambda t: f"{{{W}}}{t}"
a = lambda t: f"{{{A}}}{t}"
p_ = lambda t: f"{{{P}}}{t}"
s_ = lambda t: f"{{{S}}}{t}"

LATIN = re.compile(r"[A-Za-z]")
TAG = re.compile(r"<(\d{1,2})>(.*?)</\1>", re.S)
BATCH_CHARS = 9000          # English characters per batch handed to Claude
BATCH_MAX = 120             # texts per batch
DEFAULT_FONT = "Nirmala UI"


def _font_for(options):
    L = langs.get(langs.code_of(options))
    return ((options or {}).get("font") or L["fonts"][0]).strip() or L["fonts"][0]


class TranslateError(Exception):
    pass


# ============================================================ helpers
def _convert(path: Path, ext: str, out_dir: Path) -> Path:
    """Old binary Office formats (.doc .ppt .xls) -> modern format with LibreOffice."""
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["soffice", "--headless", "--convert-to", ext, "--outdir", str(out_dir), str(path)],
                   capture_output=True, timeout=600)
    out = out_dir / (path.stem + "." + ext)
    if not out.exists():
        raise TranslateError(f"The {path.suffix} file could not be opened. Save it as .{ext} and upload again.")
    return out


def source_file(path: Path, work: Path):
    """Returns (file to read, format). Old formats are converted first."""
    ext = path.suffix.lower()
    if ext in (".docx", ".pptx", ".xlsx"):
        return path, ext[1:]
    conv = {".doc": "docx", ".rtf": "docx", ".odt": "docx", ".ppt": "pptx", ".odp": "pptx", ".xls": "xlsx", ".ods": "xlsx"}
    if ext in conv:
        return _convert(path, conv[ext], work / "converted"), conv[ext]
    if ext == ".pdf":
        raise TranslateError("A PDF storyboard cannot be translated in place. Upload the Word, PowerPoint or Excel SB.")
    raise TranslateError(f"{ext} files are not supported. Upload the SB as Word, PowerPoint or Excel.")


def _needs_translation(text: str, target=None) -> bool:
    text = KEEP_TOKENS.sub(" ", text)
    if not LATIN.search(text):
        return False                      # numbers, symbols, already translated
    letters = LATIN.findall(text)
    tr = target or langs.script_re("hi")
    if tr.search(text) and len(tr.findall(text)) > len(letters):
        return False                      # already (mostly) in the target language
    return True


def _clean_ctx(t, n=60):
    t = re.sub(r"\s+", " ", t or "").strip()
    return t if len(t) <= n else t[:n - 1] + "…"


# ============================================================ Word
WORD_PARTS = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")
RPR_ORDER = ["rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "outline", "shadow",
             "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden", "color", "spacing", "w", "kern",
             "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign", "rtl", "cs",
             "em", "lang", "eastAsianLayout", "specVanish", "oMath"]
IGNORE_RPR = {"rFonts", "lang", "noProof", "bCs", "iCs", "szCs", "rtl", "cs", "webHidden", "spacing", "kern"}
TEXT_CHILDREN = {"rPr", "t", "tab", "br", "cr", "noBreakHyphen", "softHyphen", "lastRenderedPageBreak"}


def _w_paragraph_runs(p):
    """Text runs that belong to paragraph p (not to a text box nested inside it), skipping field codes."""
    runs, in_code = [], False
    for r in p.iter(w("r")):
        anc = r.getparent()
        while anc is not None and anc.tag != w("p"):
            if anc.tag in (w("del"), w("moveFrom")):
                break
            anc = anc.getparent()
        if anc is not p:
            continue
        for fc in r.iter(w("fldChar")):
            t = fc.get(w("fldCharType"))
            if t == "begin":
                in_code = True
            elif t in ("separate", "end"):
                in_code = False
        if in_code or r.find(w("instrText")) is not None:
            continue
        txt = _w_run_text(r)
        if txt:
            runs.append((r, txt))
    return runs


def _w_run_text(r):
    out = []
    for ch in r:
        tag = etree.QName(ch).localname
        if tag == "t":
            out.append(ch.text or "")
        elif tag == "tab":
            out.append("\t")
        elif tag in ("br", "cr"):
            out.append("\n")
        elif tag == "noBreakHyphen":
            out.append("-")
    return "".join(out)


def _w_fmt_key(r):
    rpr = r.find(w("rPr"))
    if rpr is None:
        return ""
    parts = []
    for ch in rpr:
        name = etree.QName(ch).localname
        if name in IGNORE_RPR:
            continue
        parts.append(name + str(sorted((etree.QName(k).localname, v) for k, v in ch.attrib.items())))
    return "|".join(sorted(parts))


def _w_where(p):
    """'Table 3 · row 2 · column “Audio”' for paragraphs in tables, else 'Paragraph'."""
    tc = next((x for x in p.iterancestors(w("tc"))), None)
    if tc is None:
        tx = next((x for x in p.iterancestors(w("txbxContent"))), None)
        return ("Text box", "", "") if tx is not None else ("Paragraph", "", "")
    tr = tc.getparent()
    tbl = tr.getparent()
    rows = tbl.findall(w("tr"))
    ri = rows.index(tr) if tr in rows else 0
    cells = tr.findall(w("tc"))
    ci = cells.index(tc) if tc in cells else 0
    head = ""
    if rows and ri > 0:
        hc = rows[0].findall(w("tc"))
        if ci < len(hc):
            head = _clean_ctx("".join(t.text or "" for t in hc[ci].iter(w("t"))), 40)
    label = ""
    if ci > 0 and cells:
        label = _clean_ctx("".join(t.text or "" for t in cells[0].iter(w("t"))), 40)
    return (f"Table row {ri + 1}, column {ci + 1}", head, label)


def _w_paragraphs(root):
    for p in root.iter(w("p")):
        yield p


def _target_rpr_w(rpr, font, lang="hi"):
    """Indic and Arabic scripts are complex scripts in Word: bold/italic/size live in the twins (bCs, iCs,
    szCs), the font in rFonts@cs and the proofing language in lang@bidi. Urdu/Arabic runs also get <w:rtl/>."""
    if rpr is None:
        rpr = etree.Element(w("rPr"))
    have = {etree.QName(c).localname: c for c in rpr}
    rf = have.get("rFonts")
    if rf is None:
        rf = etree.SubElement(rpr, w("rFonts"))
    rf.set(w("cs"), font)
    for k in ("cstheme",):
        rf.attrib.pop(w(k), None)
    for base, twin in (("b", "bCs"), ("i", "iCs")):
        if base in have and twin not in have:
            el = etree.SubElement(rpr, w(twin))
            v = have[base].get(w("val"))
            if v is not None:
                el.set(w("val"), v)
    if "sz" in have and "szCs" not in have:
        etree.SubElement(rpr, w("szCs")).set(w("val"), have["sz"].get(w("val")))
    L = langs.get(lang)
    lg = have.get("lang")
    if lg is None:
        lg = etree.SubElement(rpr, w("lang"))
    lg.set(w("bidi"), L["tag"])
    if L["rtl"] and "rtl" not in have:
        etree.SubElement(rpr, w("rtl"))
    # schema order (Word refuses to open some files with rPr children out of order)
    kids = list(rpr)
    kids.sort(key=lambda c: RPR_ORDER.index(etree.QName(c).localname) if etree.QName(c).localname in RPR_ORDER else 99)
    for c in kids:
        rpr.remove(c)
    for c in kids:
        rpr.append(c)
    return rpr


def _w_new_run(template_run, text, font, lang="hi"):
    r = etree.Element(w("r"))
    rpr = template_run.find(w("rPr"))
    r.append(_target_rpr_w(copy.deepcopy(rpr) if rpr is not None else None, font, lang))
    lines = text.split("\n")
    for li, line in enumerate(lines):
        if li:
            etree.SubElement(r, w("br"))
        for ti, chunk in enumerate(line.split("\t")):
            if ti:
                etree.SubElement(r, w("tab"))
            if chunk:
                t = etree.SubElement(r, w("t"))
                t.text = chunk
                t.set(XML_SPACE, "preserve")
    return r


# ============================================================ PowerPoint
PPT_PARTS = re.compile(r"^ppt/(slides/slide\d+|notesSlides/notesSlide\d+)\.xml$")
ARPR_AFTER_CS = ["sym", "hlinkClick", "hlinkMouseOver", "rtl", "extLst"]
IGNORE_ARPR_ATTR = {"lang", "altLang", "dirty", "err", "noProof", "smtClean", "kumimoji", "bmk", "spc", "kern"}
IGNORE_ARPR_EL = {"latin", "ea", "cs", "sym", "extLst"}


def _a_runs(p):
    runs = []
    for ch in p:
        tag = etree.QName(ch).localname
        if tag == "r":
            t = ch.find(a("t"))
            if t is not None and t.text:
                runs.append((ch, t.text))
        elif tag == "br":
            runs.append((ch, "\n"))
    return runs


def _a_fmt_key(r):
    if r.tag == a("br"):
        return None
    rpr = r.find(a("rPr"))
    if rpr is None:
        return ""
    parts = [f"{k}={v}" for k, v in sorted(rpr.attrib.items()) if etree.QName(k).localname not in IGNORE_ARPR_ATTR]
    for ch in rpr:
        if etree.QName(ch).localname in IGNORE_ARPR_EL:
            continue
        parts.append(etree.tostring(ch, method="c14n").decode())
    return "|".join(parts)


def _target_rpr_a(rpr, font, lang="hi"):
    if rpr is None:
        rpr = etree.Element(a("rPr"))
    rpr.set("lang", langs.get(lang)["tag"])
    rpr.set("altLang", "en-US")
    cs = rpr.find(a("cs"))
    if cs is None:
        cs = etree.Element(a("cs"))
        after = [c for c in rpr if etree.QName(c).localname in ARPR_AFTER_CS]
        if after:
            after[0].addprevious(cs)
        else:
            rpr.append(cs)
    cs.set("typeface", font)
    return rpr


def _a_new_run(template_run, text, font, lang="hi"):
    r = etree.Element(a("r"))
    rpr = template_run.find(a("rPr")) if template_run.tag == a("r") else None
    r.append(_target_rpr_a(copy.deepcopy(rpr) if rpr is not None else None, font, lang))
    t = etree.SubElement(r, a("t"))
    t.text = text
    return r


def _a_where(p, slide_label):
    """'Slide 4 · Title 1' / 'Slide 4 · table row 2, column 3' / 'Slide 4 notes'."""
    if "notes" in slide_label:
        return (slide_label, "", "")
    tc = next((x for x in p.iterancestors(a("tc"))), None)
    sp = next((x for x in p.iterancestors(p_("sp"), p_("graphicFrame"))), None)
    name = ""
    if sp is not None:
        c = next(sp.iter(p_("cNvPr")), None)
        name = c.get("name", "") if c is not None else ""
    if tc is not None:
        tr = tc.getparent()
        tbl = tr.getparent()
        rows = tbl.findall(a("tr"))
        ri = rows.index(tr)
        cells = tr.findall(a("tc"))
        ci = cells.index(tc)
        head = ""
        if ri > 0:
            hc = rows[0].findall(a("tc"))
            if ci < len(hc):
                head = _clean_ctx("".join(t.text or "" for t in hc[ci].iter(a("t"))), 40)
        label = _clean_ctx("".join(t.text or "" for t in cells[0].iter(a("t"))), 40) if ci > 0 else ""
        return (f"{slide_label} · table row {ri + 1}, column {ci + 1}", head, label)
    return (f"{slide_label} · {name}" if name else slide_label, "", "")


# ============================================================ Excel (shared strings)
def _x_runs(si):
    """An Excel shared string: plain <t>, or rich text runs <r><rPr/><t/></r>."""
    rs = si.findall(s_("r"))
    if not rs:
        t = si.find(s_("t"))
        return [(si, t.text or "")] if t is not None and t.text else []
    out = []
    for r in rs:
        t = r.find(s_("t"))
        if t is not None and t.text:
            out.append((r, t.text))
    return out


def _x_fmt_key(r):
    rpr = r.find(s_("rPr"))
    if rpr is None:
        return ""
    return "|".join(sorted(etree.tostring(c, method="c14n").decode() for c in rpr
                           if etree.QName(c).localname not in ("rFont", "family", "scheme", "charset")))


def _x_sheets(z: zipfile.ZipFile):
    """[(sheet name, part path)] in workbook order."""
    try:
        wbx = etree.fromstring(z.read("xl/workbook.xml"))
        rels = etree.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    except KeyError:
        return []
    R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    rid = {r.get("Id"): r.get("Target") for r in rels}
    out = []
    for sh in wbx.iter(s_("sheet")):
        t = rid.get(sh.get(R), "")
        path = t.lstrip("/") if t.startswith("/") else "xl/" + t
        if path in z.namelist():
            out.append((sh.get("name"), path))
    return out


def _x_cells(z, shared):
    """{sheet part: {ref: (text, shared index or None, inline <is> element or None)}} plus parsed roots."""
    cells, roots = {}, {}
    for name, path in _x_sheets(z):
        root = etree.fromstring(z.read(path))
        roots[path] = (name, root)
        m = {}
        for c in root.iter(s_("c")):
            t = c.get("t")
            if t == "s" and c.find(s_("v")) is not None:
                i = int(c.find(s_("v")).text)
                m[c.get("r")] = (shared[i] if i < len(shared) else "", i, None)
            elif t == "inlineStr" and c.find(s_("is")) is not None:
                isel = c.find(s_("is"))
                m[c.get("r")] = ("".join(x.text or "" for x in isel.iter(s_("t"))), None, isel)
        cells[path] = m
    return cells, roots


def _x_where(sheet, ref, m, extra=0):
    mm = re.match(r"([A-Z]+)(\d+)", ref)
    col, row = mm.group(1), int(mm.group(2))
    head = m.get(f"{col}1", ("",))[0] if row > 1 else ""
    label = m.get(f"A{row}", ("",))[0] if col != "A" else ""
    return (f"Sheet “{sheet}” · cell {ref}" + (f" (+{extra} more)" if extra else ""), _clean_ctx(head, 40), _clean_ctx(label, 40))


# ============================================================ walk any format
def _units(z: zipfile.ZipFile, fmt: str):
    """Yields (part name, xml root, [(unit element, runs, fmt_key_fn, where tuple)]) for every part with text."""
    names = z.namelist()
    if fmt == "docx":
        parts = sorted((n for n in names if WORD_PARTS.match(n)), key=lambda n: (not n.startswith("word/document"), n))
        for n in parts:
            root = etree.fromstring(z.read(n))
            kind = re.match(r"word/([a-z]+)", n).group(1)
            units = []
            for p in _w_paragraphs(root):
                runs = _w_paragraph_runs(p)
                if runs:
                    where = _w_where(p)
                    if kind != "document":
                        where = (kind.capitalize(), "", "")
                    units.append((p, runs, _w_fmt_key, where))
            yield n, root, units
    elif fmt == "pptx":
        def num(n):
            return int(re.search(r"(\d+)\.xml$", n).group(1))
        slides = sorted((n for n in names if PPT_PARTS.match(n)), key=lambda n: ("notes" in n, num(n)))
        # notes slides are numbered by creation, not by slide: map them through the slide relationships
        notes_of = {}
        for n in names:
            m = re.match(r"^ppt/slides/_rels/slide(\d+)\.xml\.rels$", n)
            if m:
                for rel in etree.fromstring(z.read(n)):
                    t = rel.get("Target", "")
                    if "notesSlide" in t:
                        notes_of["ppt/notesSlides/" + t.rsplit("/", 1)[-1]] = int(m.group(1))
        order = _ppt_order(z)
        for n in slides:
            root = etree.fromstring(z.read(n))
            if "notes" in n:
                sn = notes_of.get(n, num(n))
                label = f"Slide {order.get(sn, sn)} notes"
            else:
                label = f"Slide {order.get(num(n), num(n))}"
            units = []
            for p in root.iter(a("p")):
                if "notes" in n:   # skip the slide-number and header placeholders of the notes page
                    sp = next(p.iterancestors(p_("sp")), None)
                    ph = next(sp.iter(p_("ph")), None) if sp is not None else None
                    if ph is not None and ph.get("type") not in (None, "body"):
                        continue
                runs = _a_runs(p)
                if any(t.strip() for _, t in runs):
                    units.append((p, runs, _a_fmt_key, _a_where(p, label)))
            yield n, root, units
    elif fmt == "xlsx":
        sst = etree.fromstring(z.read("xl/sharedStrings.xml")) if "xl/sharedStrings.xml" in names else None
        sis = sst.findall(s_("si")) if sst is not None else []
        shared = ["".join(t.text or "" for t in si.iter(s_("t"))) for si in sis]
        cells, roots = _x_cells(z, shared)
        if sst is not None:
            where_of = {}
            for path, m in cells.items():
                for ref, (_, i, _) in m.items():
                    if i is not None:
                        where_of.setdefault(i, []).append((roots[path][0], ref, m))
            units = []
            for i, si in enumerate(sis):
                runs = _x_runs(si)
                if runs:
                    ws = where_of.get(i)
                    where = _x_where(ws[0][0], ws[0][1], ws[0][2], len(ws) - 1) if ws else ("Unused cell text", "", "")
                    units.append((si, runs, _x_fmt_key, where))
            yield "xl/sharedStrings.xml", sst, units
        for path, (sheet, root) in roots.items():
            m = cells[path]
            units = []
            for ref, (_, i, isel) in m.items():
                if isel is not None:
                    runs = _x_runs(isel)
                    if runs:
                        units.append((isel, runs, _x_fmt_key, _x_where(sheet, ref, m)))
            if units:
                yield path, root, units


def _ppt_order(z):
    """slideN.xml number -> position in the deck (the order a reader sees)."""
    try:
        pres = etree.fromstring(z.read("ppt/presentation.xml"))
        rels = etree.fromstring(z.read("ppt/_rels/presentation.xml.rels"))
    except KeyError:
        return {}
    R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    rid = {r.get("Id"): r.get("Target") for r in rels}
    out = {}
    for pos, sid in enumerate(pres.iter(p_("sldId")), 1):
        m = re.search(r"slide(\d+)\.xml", rid.get(sid.get(R), ""))
        if m:
            out[int(m.group(1))] = pos
    return out


def _source_text(runs, key_fn):
    """Plain text, or tagged text when the runs are formatted differently. Returns (text, groups)."""
    groups = []   # [(fmt key, template run, text)]
    for r, t in runs:
        k = key_fn(r)
        if k is None:            # a line break element (PowerPoint)
            if groups:
                groups[-1] = (groups[-1][0], groups[-1][1], groups[-1][2] + t)
            continue
        if groups and groups[-1][0] == k:
            groups[-1] = (k, groups[-1][1], groups[-1][2] + t)
        else:
            groups.append((k, r, t))
    plain = "".join(g[2] for g in groups)
    if len({g[0] for g in groups}) <= 1:
        return plain, groups, False
    # only tag groups that carry words; whitespace-only groups fold into their neighbour
    merged = []
    for g in groups:
        if merged and (not g[2].strip() or not merged[-1][2].strip()):
            prev = merged[-1]
            keep = prev if prev[2].strip() else g
            merged[-1] = (keep[0], keep[1], prev[2] + g[2])
        else:
            merged.append(g)
    if len({g[0] for g in merged}) <= 1:
        return plain, merged, False
    tagged = "".join(f"<{i + 1}>{g[2]}</{i + 1}>" for i, g in enumerate(merged))
    return tagged, merged, True


# ============================================================ extract
def extract(src: Path, run_dir: Path, options=None):
    """Writes segments.json, outline.txt and batches/bNN.json. Returns a summary dict."""
    options = options or {}
    lang = langs.code_of(options)
    target = langs.script_re(lang)
    work = run_dir / "tr"
    work.mkdir(parents=True, exist_ok=True)
    path, fmt = source_file(src, work)
    shutil.copy(path, work / ("source." + fmt))
    segs, uniques, outline = [], {}, []
    with zipfile.ZipFile(path) as z:
        for part, root, units in _units(z, fmt):
            for ui, (el, runs, key_fn, where) in enumerate(units):
                text, groups, tagged = _source_text(runs, key_fn)
                plain = TAG.sub(lambda m: m.group(2), text)
                if not plain.strip():
                    continue
                loc, head, label = where
                outline.append(f"[{loc}]" + (f" (row label: {label})" if label else "") + (f" (top of column: {head})" if head else "") +
                               f"  {_clean_ctx(plain, 160)}")
                if not _needs_translation(plain, target):
                    continue
                key = text.strip()
                u = uniques.get(key)
                if u is None:
                    u = uniques[key] = {"id": f"u{len(uniques) + 1:04d}", "en": key, "tagged": tagged,
                                        "where": [], "count": 0}
                u["count"] += 1
                ctx = loc + (f" · row label “{label}”" if label else "") + (f" · top of this column “{head}”" if head else "")
                if len(u["where"]) < 3 and ctx not in u["where"]:
                    u["where"].append(ctx)
                segs.append({"seg": f"s{len(segs) + 1:05d}", "part": part, "unit": ui, "u": u["id"],
                             "lead": text[:len(text) - len(text.lstrip())], "trail": text[len(text.rstrip()):],
                             "where": ctx, "en": plain.strip()})
    if not uniques:
        raise TranslateError("No English text was found in this storyboard.")
    (work / "segments.json").write_text(json.dumps({"format": fmt, "lang": lang, "source": src.name, "segments": segs,
                                                    "uniques": list(uniques.values())}, ensure_ascii=False, indent=1))
    (work / "outline.txt").write_text("\n".join(outline))
    # batches of unique texts, in document order
    bdir = work / "batches"
    shutil.rmtree(bdir, ignore_errors=True)
    bdir.mkdir()
    batches, cur, size = [], [], 0
    for u in uniques.values():
        n = len(u["en"])
        if cur and (size + n > BATCH_CHARS or len(cur) >= BATCH_MAX):
            batches.append(cur)
            cur, size = [], 0
        cur.append({"id": u["id"], "en": u["en"], "tagged": u["tagged"], "where": u["where"], "times": u["count"]})
        size += n
    if cur:
        batches.append(cur)
    files = []
    for i, b in enumerate(batches, 1):
        f = bdir / f"b{i:02d}.json"
        f.write_text(json.dumps(b, ensure_ascii=False, indent=1))
        files.append(str(f))
    words = sum(len(u["en"].split()) for u in uniques.values())
    return {"format": fmt, "lang": lang, "language": langs.get(lang)["name"], "segments": len(segs), "unique_texts": len(uniques), "words": words,
            "batches": files, "outline_file": str(work / "outline.txt")}


# ============================================================ check one batch answer
NUM = re.compile(r"\d+(?:[.,:]\d+)*%?")
KEEP_TOKENS = re.compile(r"%[\w.]+%|https?://\S+|www\.\S+|\b[\w.-]+\.(?:png|jpe?g|gif|mp3|mp4|wav|svg|pdf|docx?|xlsx?|pptx?)\b|\{[^}]+\}", re.I)


def _visual_len(s):
    """Characters that take up width (vowel signs above/below a letter take none)."""
    import unicodedata
    return sum(1 for ch in s if unicodedata.category(ch) != "Mn")


def ans_text(ans):
    """The translation in one answer: {"tr": …} (older answers used {"hi": …})."""
    if not isinstance(ans, dict):
        return ""
    v = ans.get("tr", ans.get("hi", ""))
    return v if isinstance(v, str) else ""


def problems_for(item, ans, lang="hi"):
    """Plain-English problems with one answer (empty list = fine)."""
    L = langs.get(lang)
    out = []
    if not isinstance(ans, dict):
        return ["Answer must be an object {\"tr\": …} or {\"keep\": true, \"why\": …}"]
    if ans.get("keep"):
        return []
    hi = ans_text(ans)
    if not hi.strip():
        return [f"No {L['name']} text"]
    if item.get("tagged"):
        want = sorted(set(re.findall(r"<(\d{1,2})>", item["en"])))
        got_open = re.findall(r"<(\d{1,2})>", hi)
        got_close = re.findall(r"</(\d{1,2})>", hi)
        if sorted(set(got_open)) != want or sorted(got_open) != sorted(got_close) or len(got_open) != len(set(got_open)):
            out.append(f"Tags must be kept exactly once each: {' '.join('<%s>…</%s>' % (t, t) for t in want)}")
    elif re.search(r"</?\d{1,2}>", hi):
        out.append("Remove the <n> tags: this text has none")
    en_plain = TAG.sub(lambda m: m.group(2), item["en"])
    hi_plain = langs.fold_digits(TAG.sub(lambda m: m.group(2), hi)).replace("٫", ".").replace("٬", ",").replace("٪", "%")
    for n in set(NUM.findall(en_plain)):
        if lang == "ar" and n in ("1", "2"):
            continue      # Arabic says one and two with singular and dual word forms (محاولتان), not digits
        if n not in hi_plain and n.rstrip("%") not in hi_plain:
            out.append(f"Number “{n}” is missing in the {L['name']}")
    for tok in set(KEEP_TOKENS.findall(en_plain)):
        if tok not in hi:
            out.append(f"“{tok}” must stay exactly as it is")
    if not langs.script_re(lang).search(hi) and len(en_plain.split()) > 2:
        out.append(f"The {L['name']} has no {L['script']} script text: translate it, or answer {{\"keep\": true, \"why\": …}}")
    return out


def check_batch(run_dir: Path, batch_file: Path):
    """Validates bNN.out.json against bNN.json. Returns {ok, done, total, problems:[{id, en, problems}]}"""
    work = run_dir / "tr"
    items = json.loads(batch_file.read_text())
    out_file = batch_file.with_suffix(".out.json")
    if not out_file.exists():
        raise TranslateError(f"{out_file.name} was not written")
    try:
        answers = json.loads(out_file.read_text())
    except json.JSONDecodeError as e:
        raise TranslateError(f"{out_file.name} is not valid JSON: {e}")
    lang = json.loads((work / "segments.json").read_text()).get("lang", "hi")
    probs = []
    for it in items:
        ps = problems_for(it, answers.get(it["id"]), lang) if it["id"] in answers else ["Missing"]
        if ps:
            probs.append({"id": it["id"], "en": it["en"], "problems": ps})
    all_b = sorted(work.glob("batches/b??.json"))
    done = sum(1 for b in all_b if b.with_suffix(".out.json").exists())
    return {"ok": not probs, "batch": batch_file.name, "done_batches": done, "total_batches": len(all_b),
            "problems": probs}


# ============================================================ apply
def _split_tagged(hi, groups, tagged):
    """[(template run, text)] in the order the translation wants them."""
    if not tagged:
        return [(groups[0][1], hi)]
    spans, pos = [], 0
    for m in TAG.finditer(hi):
        before = hi[pos:m.start()]
        if before:
            if spans:
                spans[-1] = (spans[-1][0], spans[-1][1] + before)
            else:
                spans.append((groups[0][1], before))
        idx = int(m.group(1)) - 1
        tpl = groups[idx][1] if 0 <= idx < len(groups) else groups[0][1]
        spans.append((tpl, m.group(2)))
        pos = m.end()
    rest = hi[pos:]
    if rest:
        if spans:
            spans[-1] = (spans[-1][0], spans[-1][1] + rest)
        else:
            spans.append((groups[0][1], rest))
    return spans


def _replace_unit(fmt, el, runs, key_fn, hi_text, font, force_plain=False, lang="hi"):
    """Puts the translation into the unit (paragraph / shared string), keeping run formatting."""
    if langs.get(lang)["rtl"] and fmt in ("docx", "pptx"):
        _make_rtl(fmt, el)
    _, groups, tagged = _source_text(runs, key_fn)
    if force_plain:
        tagged = False
    spans = _split_tagged(hi_text, groups, tagged)
    if fmt == "xlsx":
        if el.find(s_("r")) is None:
            t = el.find(s_("t"))
            t.text = hi_text if not tagged else TAG.sub(lambda m: m.group(2), hi_text)
            t.set(XML_SPACE, "preserve")
            return
        rs = el.findall(s_("r"))
        first = rs[0]
        idx = list(el).index(first)
        for r in rs:
            el.remove(r)
        for k, (tpl, text) in enumerate(spans):
            nr = copy.deepcopy(tpl)
            t = nr.find(s_("t"))
            t.text = text
            t.set(XML_SPACE, "preserve")
            el.insert(idx + k, nr)
        return
    text_runs = [r for r, _ in runs if fmt == "docx" or r.tag == a("r") or r.tag == a("br")]
    parents = {id(r.getparent()) for r in text_runs}
    simple = len(parents) == 1 and all(
        etree.QName(c).localname in (TEXT_CHILDREN if fmt == "docx" else {"rPr", "t"}) for r in text_runs if r.tag != a("br") for c in r)
    if fmt == "docx":
        new = lambda tpl, t: _w_new_run(tpl, t, font, lang)
    else:
        new = lambda tpl, t: _a_new_run(tpl, t, font, lang)
    if not simple:
        # runs spread over hyperlinks/fields: write all the translation into the first run, empty the others
        flat = TAG.sub(lambda m: m.group(2), hi_text)
        first = text_runs[0]
        repl = new(first, flat)
        first.addprevious(repl)
        for r in text_runs:
            if fmt == "docx":
                for c in list(r):
                    if etree.QName(c).localname in ("t", "tab", "br", "cr", "noBreakHyphen"):
                        r.remove(c)
            else:
                r.getparent().remove(r)
        if fmt == "docx" and first.getparent() is not None and len(first) <= 1:
            first.getparent().remove(first)
        return
    parent = text_runs[0].getparent()
    idx = list(parent).index(text_runs[0])
    for r in text_runs:
        parent.remove(r)
    k = idx
    for tpl, text in spans:
        if fmt == "pptx" and "\n" in text:      # PowerPoint line breaks are <a:br/> elements
            for li, line in enumerate(text.split("\n")):
                if li:
                    br = etree.Element(a("br"))
                    rpr = tpl.find(a("rPr")) if tpl.tag == a("r") else None
                    br.append(_target_rpr_a(copy.deepcopy(rpr) if rpr is not None else None, font, lang))
                    parent.insert(k, br); k += 1
                if line:
                    parent.insert(k, new(tpl, line)); k += 1
        elif text:
            parent.insert(k, new(tpl, text)); k += 1


PPR_ORDER = ["pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr",
             "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap",
             "overflowPunct", "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd", "snapToGrid",
             "spacing", "ind", "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc", "textDirection",
             "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr", "pPrChange"]


def _make_rtl(fmt, p):
    """Right-to-left paragraph for Urdu/Arabic. Tables and page layout stay as in the English SB."""
    if fmt == "docx":
        ppr = p.find(w("pPr"))
        if ppr is None:
            ppr = etree.Element(w("pPr"))
            p.insert(0, ppr)
        if ppr.find(w("bidi")) is None:
            etree.SubElement(ppr, w("bidi"))
        jc = ppr.find(w("jc"))
        if jc is not None and jc.get(w("val")) in ("left", "start", "both", "distribute"):
            if jc.get(w("val")) in ("left", "start"):
                ppr.remove(jc)          # default for a right-to-left paragraph is its start: the right edge
        kids = sorted(list(ppr), key=lambda c: PPR_ORDER.index(etree.QName(c).localname) if etree.QName(c).localname in PPR_ORDER else 99)
        for c in kids:
            ppr.remove(c)
        for c in kids:
            ppr.append(c)
    elif fmt == "pptx":
        ppr = p.find(a("pPr"))
        if ppr is None:
            ppr = etree.Element(a("pPr"))
            p.insert(0, ppr)
        ppr.set("rtl", "1")
        sp = next(p.iterancestors(p_("sp")), None)
        ph = next(sp.iter(p_("ph")), None) if sp is not None else None
        titled = ph is not None and ph.get("type") in ("title", "ctrTitle", "subTitle")
        if ppr.get("algn") == "l" or (ppr.get("algn") is None and not titled):
            ppr.set("algn", "r")      # titles keep the layout's own alignment (usually centred)


def apply(run_dir: Path, out_path: Path, options=None):
    """Writes the translated SB. Returns a summary with warnings."""
    options = dict(options or {})
    work = run_dir / "tr"
    data = json.loads((work / "segments.json").read_text())
    fmt = data["format"]
    options.setdefault("lang", data.get("lang", "hi"))
    lang = langs.code_of(options)
    L = langs.get(lang)
    font = _font_for(options)
    answers = {}
    for f in sorted(work.glob("batches/b??.out.json")):
        answers.update(json.loads(f.read_text()))
    uniq = {u["id"]: u for u in data["uniques"]}
    missing = [u for u in uniq if u not in answers]
    if missing:
        raise TranslateError(f"{len(missing)} texts have no translation yet (first: {missing[0]})")
    by_part = {}
    for sgm in data["segments"]:
        by_part.setdefault(sgm["part"], {})[sgm["unit"]] = sgm
    src = work / ("source." + fmt)
    warnings, changed = [], 0
    tmp = Path(tempfile.mkdtemp())
    with zipfile.ZipFile(src) as z:
        new_parts = {}
        for part, root, units in _units(z, fmt):
            todo = by_part.get(part, {})
            if not todo:
                continue
            for ui, (el, runs, key_fn, where) in enumerate(units):
                sgm = todo.get(ui)
                if not sgm:
                    continue
                ans = answers.get(sgm["u"]) or {}
                if ans.get("keep"):
                    continue
                hi = ans_text(ans)
                ps = problems_for(uniq[sgm["u"]], ans, lang)
                plain = any(p.startswith("Tags") or p.startswith("Remove the") for p in ps)
                if plain:   # broken tags: one formatting for the whole paragraph
                    hi = re.sub(r"</?\d{1,2}>", "", hi)
                for p in ps:
                    if not (p.startswith("Tags") or p.startswith("Remove the")):
                        warnings.append({"where": sgm["where"], "en": sgm["en"], "hi": TAG.sub(lambda m: m.group(2), hi),
                                         "problem": p})
                hi_full = sgm.get("lead", "") + hi.strip() + sgm.get("trail", "")
                _replace_unit(fmt, el, runs, key_fn, hi_full, font, force_plain=plain, lang=lang)
                changed += 1
                if fmt == "pptx" and "notes" not in sgm["where"]:
                    en_len, hi_len = len(sgm["en"]), _visual_len(TAG.sub(lambda m: m.group(2), hi))
                    if en_len >= 25 and hi_len > en_len * 1.5:
                        warnings.append({"where": sgm["where"], "en": sgm["en"], "hi": TAG.sub(lambda m: m.group(2), hi),
                                         "problem": f"{L['name']} is much longer than the English: check that it fits its box"})
            new_parts[part] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zo:
            for item in z.infolist():
                zo.writestr(item, new_parts.get(item.filename, z.read(item.filename)))
    shutil.rmtree(tmp, ignore_errors=True)
    if fmt == "docx":
        _docx_language(out_path, lang)
    kept = [u for u in uniq.values() if (answers.get(u["id"]) or {}).get("keep")]
    return {"format": fmt, "changed": changed, "translated_texts": len(uniq) - len(kept), "kept_texts": len(kept),
            "warnings": warnings}


def _docx_language(path: Path, code="hi"):
    """Marks the target language as the document's complex-script proofing language in styles.xml (Word's spell check)."""
    with zipfile.ZipFile(path) as z:
        if "word/styles.xml" not in z.namelist():
            return
        st = etree.fromstring(z.read("word/styles.xml"))
        items = {i.filename: z.read(i.filename) for i in z.infolist()}
        infos = z.infolist()
    rpr = st.find(f"{w('docDefaults')}/{w('rPrDefault')}/{w('rPr')}")
    if rpr is None:
        return
    lang = rpr.find(w("lang"))
    if lang is None:
        lang = etree.SubElement(rpr, w("lang"))
        kids = sorted(list(rpr), key=lambda c: RPR_ORDER.index(etree.QName(c).localname) if etree.QName(c).localname in RPR_ORDER else 99)
        for c in kids:
            rpr.remove(c)
        for c in kids:
            rpr.append(c)
    lang.set(w("bidi"), langs.get(code)["tag"])
    items["word/styles.xml"] = etree.tostring(st, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zo:
        for i in infos:
            zo.writestr(i, items[i.filename])


# ============================================================ review workbook and site data
def review_rows(run_dir: Path):
    work = run_dir / "tr"
    data = json.loads((work / "segments.json").read_text())
    answers = {}
    for f in sorted(work.glob("batches/b??.out.json")):
        answers.update(json.loads(f.read_text()))
    from .quality import edits
    first = {k: (d, why) for k, d, _, why in edits(work)}
    rows = []
    for sgm in data["segments"]:
        ans = answers.get(sgm["u"]) or {}
        hi = "" if ans.get("keep") else TAG.sub(lambda m: m.group(2), ans_text(ans)).strip()
        row = {"n": len(rows) + 1, "u": sgm["u"], "where": sgm["where"], "en": sgm["en"], "hi": hi,
               "kept": bool(ans.get("keep")), "note": ans.get("why", "") if ans.get("keep") else ans.get("note", "")}
        if sgm["u"] in first:
            row["draft"], row["why"] = first[sgm["u"]]
        rows.append(row)
    return rows


def build_review_xlsx(rows, meta, warnings, out_path: Path, font=DEFAULT_FONT, lang="hi", terms=None):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
    L = langs.get(lang)
    name = L["name"]
    tr_align = Alignment(wrap_text=True, vertical="top", horizontal="right", readingOrder=2) if L["rtl"] \
        else Alignment(wrap_text=True, vertical="top")
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    head_fill = PatternFill("solid", fgColor="1F2937")
    thin = Side(style="thin", color="D0D5DD")
    ws.append([f"English → {name} storyboard translation"])
    ws["A1"].font = Font(bold=True, size=14)
    for k, v in (("Storyboard", meta.get("name", "")), (f"{name} file", meta.get("output_name", "")),
                 ("Translated by", "QC360 AI"), ("Date", meta.get("date", "")),
                 ("Texts translated", sum(not r["kept"] for r in rows)), ("Texts kept in English", sum(r["kept"] for r in rows)),
                 ("Agreed terms", len(terms or [])), ("Lines improved in the editing pass", len({r["u"] for r in rows if r.get("draft") is not None})),
                 ("Items to check", len(warnings))):
        ws.append([k, v])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 70

    ws2 = wb.create_sheet(f"English - {name}")
    heads = ["#", "Where in the SB", "English", name, "Kept in English", "Note", "Reviewer comment"]
    ws2.append(heads)
    for c in ws2[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = head_fill
        c.alignment = Alignment(vertical="center", wrap_text=True)
    keep_fill = PatternFill("solid", fgColor="F2F4F7")
    for r in rows:
        ws2.append([r["n"], r["where"], r["en"], r["hi"], "Yes" if r["kept"] else "", r["note"], ""])
        row = ws2.max_row
        for col in range(1, 8):
            c = ws2.cell(row, col)
            c.alignment = Alignment(wrap_text=True, vertical="top")
            c.border = Border(bottom=thin)
            if r["kept"]:
                c.fill = keep_fill
        ws2.cell(row, 4).font = Font(name=font, size=12)
        ws2.cell(row, 4).alignment = tr_align
    for col, wdt in zip("ABCDEFG", (6, 34, 55, 55, 10, 30, 30)):
        ws2.column_dimensions[col].width = wdt
    ws2.freeze_panes = "C2"
    ws2.auto_filter.ref = ws2.dimensions

    if terms:
        wt = wb.create_sheet("Terms")
        wt.append(["English term", name, "Note"])
        for c in wt[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = head_fill
        for t in terms:
            wt.append([t["en"], "(keep in English)" if t.get("keep") else t["tr"], t.get("note", "")])
            wt.cell(wt.max_row, 2).font = Font(name=font, size=12)
            wt.cell(wt.max_row, 2).alignment = tr_align
        for col, wdt in zip("ABC", (36, 40, 40)):
            wt.column_dimensions[col].width = wdt
        wt.freeze_panes = "A2"
    changed = []
    seen = set()
    for r in rows:
        if r.get("draft") is not None and r["u"] not in seen:
            seen.add(r["u"])
            changed.append(r)
    if changed:
        we = wb.create_sheet("Editing pass")
        we.append(["Where in the SB", "English", "First draft", "After editing", "Why it changed"])
        for c in we[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = head_fill
        for r in changed:
            we.append([r["where"], r["en"], r["draft"], r["hi"] or "(kept in English)", r.get("why", "")])
            for col in range(1, 6):
                we.cell(we.max_row, col).alignment = Alignment(wrap_text=True, vertical="top")
            for col in (3, 4):
                we.cell(we.max_row, col).font = Font(name=font, size=12)
                we.cell(we.max_row, col).alignment = tr_align
        for col, wdt in zip("ABCDE", (28, 42, 42, 42, 34)):
            we.column_dimensions[col].width = wdt
        we.freeze_panes = "A2"
    if warnings:
        ws3 = wb.create_sheet("To check")
        ws3.append(["Where in the SB", "English", name, "What to check"])
        for c in ws3[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = head_fill
        for wv in warnings:
            ws3.append([wv["where"], wv["en"], wv["hi"], wv["problem"]])
            for col in range(1, 5):
                ws3.cell(ws3.max_row, col).alignment = Alignment(wrap_text=True, vertical="top")
            ws3.cell(ws3.max_row, 3).font = Font(name=font, size=12)
            ws3.cell(ws3.max_row, 3).alignment = tr_align
        for col, wdt in zip("ABCD", (34, 50, 50, 45)):
            ws3.column_dimensions[col].width = wdt
        ws3.freeze_panes = "A2"
    wb.save(out_path)
