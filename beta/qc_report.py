"""Builds the team's QC Review workbook from a finished run.

One row per issue in the 'Review Report' sheet. The Feedback description cell holds the
issue text with its screenshot (problem area outlined in red) placed directly under it.
"""
import base64
import datetime as dt
import io
import re
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.drawing.spreadsheet_drawing import OneCellAnchor, AnchorMarker
from openpyxl.drawing.xdr import XDRPositiveSize2D
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils.units import pixels_to_EMU
from openpyxl.worksheet.datavalidation import DataValidation
from copy import copy

from .qc_template import TEMPLATE_B64

REVIEWER = "QC360 AI"
STATUS = "In-progress"
ISSUE_LIST = ["Transcript", "Audio", "OST", "Animation", "ARD", "Graphic", "Header", "Buttons", "Menu", "Objectives",
              "Assessment", "Assessment Strategy", "Resource", "Seek Bar", "Slide Title", "Skin", "Video", "Navigation",
              "Content Flow", "SB-Spelling", "SB-Grammar", "SB-Casing", "SB-Audio Script", "SB-OST",
              "SB-OST/Audio Script", "VO INS", "Animation INS"]
IMG_W = 560          # display width of a screenshot in the cell, px (column F is ~770 px wide)
IMG_MAX_H = 330      # screenshots are shrunk to at most this height so rows stay compact
MAX_ROW_PX = 540     # Excel's row-height ceiling is 409 pt (~545 px)
LABELS = re.compile(r"^(Issue identified|Action required|Issue Found in|What is given|What needs to be done|"
                    r"Provided in SB|Provided in Output|Checklist):")
CAT_FILL = {"High": "FDE2E1", "Medium": "FFF1D6", "Low": "E5F4E3", "Suggestion": "E3EEFB", "Update": "EFEFEF"}
LINE_PX = 18
CHARS_PER_LINE = 105


def category(f):
    """Maps a finding to the template's issue categories (Description sheet)."""
    c, sev, msg = f["check"], f["severity"], f["message"].lower()
    if f.get("advice"):   # a suggestion, not a defect (e.g. the storyboard has no knowledge check)
        return "Suggestion"
    if f.get("blocker"):
        return "High"
    if c in ("assessment", "logic", "interactivity", "visual"):
        return "High" if sev == "error" else "Medium"
    if c == "consistency":
        return "Medium" if sev == "error" else "Low"
    if c.startswith("sb_"):
        if f.get("found_in") == "SB-Spelling":
            return "High"
        return "High" if sev == "error" else ("Low" if c in ("sb_consistency", "sb_standards") else "Medium")
    if c == "grammar":
        return "Low" if msg.startswith("punctuation") else "Medium"
    if c == "spelling":
        return "High"
    if c in ("content", "restrictions"):
        return "High" if sev == "error" else "Medium"
    if c == "transcript" and msg.startswith("spelling"):
        return "High"
    if c == "transcript" and msg.startswith("grammar"):
        return "Medium"
    if c == "transcript" and msg.startswith("punctuation"):
        return "Low"
    if c == "audio":
        if "gap" in msg or "clipping" in msg:
            return "Low"
        if "longer than" in msg:
            return "Medium"
        return "High"
    if c in ("seekbar", "video"):
        return "Medium"
    if c in ("navigation", "swipe", "menu", "screen_number", "transcript", "buttons"):
        return "High" if sev == "error" else "Medium"
    return "Medium" if sev == "error" else "Low"


def issue_found_in(f):
    c, msg = f["check"], f["message"].lower()
    if c == "style_guide":   # where the words are: SB VO / OST, or the course's on-screen text
        if f.get("sb_only"):
            return "SB-Audio Script" if re.search(r"\bvo\b|voice|audio|narrat", msg) else "SB-OST"
        return "Slide Title" if "title" in msg else "OST"
    if c.startswith("sb_"):
        return f.get("found_in") if f.get("found_in") in ISSUE_LIST else "SB-OST"
    if c in ("content", "restrictions", "interactivity", "assessment", "logic", "consistency", "visual") and f.get("found_in") in ISSUE_LIST:
        return f["found_in"]
    if c in ("content", "restrictions"):
        return "Content Flow" if c == "content" else "Navigation"
    if c == "interactivity":
        return "Buttons"
    if c in ("grammar", "spelling"):
        if " in slide title" in msg:
            return "Slide Title"
        if " in captions" in msg or " in notes" in msg:
            return "Transcript"
        return "OST"
    return {"navigation": "Navigation", "swipe": "Navigation", "menu": "Menu", "buttons": "Buttons",
            "screen_number": "Header", "transcript": "Transcript", "audio": "Audio", "seekbar": "Seek Bar",
            "video": "Video", "assessment": "Assessment", "logic": "Content Flow", "consistency": "OST",
            "visual": "Graphic"}.get(c, "Navigation")


def feedback_text(f):
    """The Feedback description, in the team's format:
    Issue Found in / What is given / What needs to be done / Provided in SB / Provided in Output."""
    from .wording import default_fix
    if f.get("check", "").startswith("sb_") or (f.get("check") == "style_guide" and f.get("sb_only")):
        return sb_feedback(f)
    msg = f.get("message", "")
    if msg.startswith("SB: "):
        msg = msg[4:]
    given = [msg.rstrip(".") + "."]
    for line in (f.get("detail") or "").split("\n"):
        line = line.strip()
        quoted = f"“{f.get('sb_says')}”" if f.get("sb_says") else None
        if line and not re.match(r"^(SB|Course|Suggested fix)\s*:", line) and line not in given[0] and line != quoted:
            given.append(line)
    if f.get("mobile"):
        given[0] = "On a phone: " + given[0]
    fix = f.get("fix") or (f"Change it to: “{f['suggestion']}”." if f.get("suggestion") else "") or \
        ("Correct the storyboard." if f.get("check", "").startswith("sb_") and default_fix(msg).startswith("Correct this") else default_fix(msg))
    given = [g for g in given if g.rstrip(".") != fix.rstrip(".")] or given[:1]
    out = [f"Issue Found in: {issue_found_in(f)}",
           "What is given: " + "\n".join(given),
           f"What needs to be done: {fix}"]
    sb_related = f.get("check") == "content" or f.get("check", "").startswith("sb_") or f.get("sb_only")
    if sb_related and f.get("sb_says"):
        if f.get("sb_says"):
            out.append(f"Provided in SB: “{f['sb_says']}”")
        if f.get("check") == "content":
            out.append(f"Provided in Output: “{f['course_says']}”" if f.get("course_says") else "Provided in Output: Not present")
    if f.get("rule"):
        out.append("Checklist: " + f["rule"])
    return "\n".join(out)


def _short_quote(q, n=160):
    q = re.sub(r"\s*/\s*", " ", re.sub(r"\s+", " ", (q or "").replace("\n", " "))).strip()
    return q if len(q) <= n else q[:n].rsplit(" ", 1)[0] + " …"


def sb_feedback(f):
    """SB QC: two lines – what the issue is, in short, and the action required to fix it."""
    issue = (f.get("message") or "Issue").removeprefix("SB: ").strip().rstrip(".")
    quote = _short_quote(f.get("sb_says") or "")
    what = f"{issue}: “{quote}”" if quote and quote.lower() not in issue.lower() else issue + "."
    fix = (f.get("fix") or "").strip()
    sug = (f.get("suggestion") or "").strip()
    if not fix and sug:
        fix = f"Change “{quote}” to “{_short_quote(sug)}”." if quote else f"Change it to “{_short_quote(sug)}”."
    if not fix:
        fix = "Correct this in the storyboard."
    return f"Issue identified: {what}\nAction required: {fix}"


def _rich(text, base_font=None):
    """The Feedback description with its labels ("Issue identified:", "Action required:" …) in bold."""
    try:
        from openpyxl.cell.rich_text import CellRichText, TextBlock
        from openpyxl.cell.text import InlineFont
    except Exception:
        return text
    name = (base_font.name if base_font is not None else None) or "Calibri"
    size = (base_font.sz if base_font is not None else None) or 10
    bold, plain = InlineFont(rFont=name, sz=size, b=True), InlineFont(rFont=name, sz=size)
    parts = []
    for k, line in enumerate(text.split("\n")):
        nl = "\n" if k else ""
        m = LABELS.match(line)
        if m:
            parts.append(TextBlock(bold, nl + m.group(0)))
            parts.append(TextBlock(plain, line[m.end():]))
        else:
            parts.append(TextBlock(plain, nl + line))
    return CellRichText(*parts)


def describe(f):
    return feedback_text(f)


def screen_cell(f, s, stage, is_video=False):
    """Slide/Screen No column: "SB - 5/ Screen 6" when the issue is tied to the storyboard, else the screen number."""
    if is_video:
        return f.get("slide_label") or f.get("time") or "Global"
    if stage == "sb":
        pg = f.get("sb_page") or (s["index"] if s else "")
        if not pg:
            return "Global"
        scr = f.get("sb_screen") or (s or {}).get("screen")
        return f"SB - {pg}/ Screen {scr}" if scr else f"SB - {pg}"
    idx = s["index"] if s and s.get("index") else None
    if f.get("sb_page"):
        return f"SB - {f['sb_page']}/ Screen {idx}" if idx else f"SB - {f['sb_page']}"
    return idx if idx else "Global"


def _lines(text):
    return sum(max(1, -(-len(line) // CHARS_PER_LINE)) for line in text.split("\n"))


def _copy_row_style(ws, src_row, dst_row, ncols):
    for col in range(1, ncols + 1):
        s, d = ws.cell(src_row, col), ws.cell(dst_row, col)
        if s.has_style:
            d.font, d.border, d.fill = copy(s.font), copy(s.border), copy(s.fill)
            d.number_format, d.protection = s.number_format, copy(s.protection)
        d.alignment = Alignment(wrap_text=True, vertical="top", horizontal="center" if col in (1, 2, 4, 7, 12, 13) else None)


def build(res, meta, run_dir: Path, out_path: Path, review_date=None):
    wb = load_workbook(io.BytesIO(base64.b64decode(TEMPLATE_B64)))
    ws = wb.worksheets[0]
    review_date = review_date or dt.date.today()

    # header block
    is_video = meta.get("kind") == "video"
    ws["C2"] = res.get("course_title") or Path(meta.get("name", "")).stem
    ws["C3"] = meta.get("name", "")
    _stage = (res.get("stage") or (meta.get("options") or {}).get("stage") or "beta")
    ws["C4"] = {"sb": "SB", "gold": "Gold / Showstopper"}.get(_stage, _stage.capitalize())
    ws["C5"] = "QC review (automated)"
    ws["C6"] = meta.get("name", "")
    ws["G6"] = "Change set number:"

    # move the 'Issue found in' list to a hidden sheet so issue rows can grow freely
    lists = wb.create_sheet("Lists")
    for i, v in enumerate(ISSUE_LIST, 1):
        lists.cell(i, 1, v)
    lists.sheet_state = "hidden"
    for r in range(205, 245):
        ws.cell(r, 1).value = None

    findings = [f for f in res["findings"] if f["severity"] in ("error", "warning")]
    slide_index = {s["id"]: s for s in res.get("slides", [])}
    rank = {"High": 0, "Medium": 1, "Low": 2, "Update": 3, "Suggestion": 4}

    def key(f):
        """Sequential: screen by screen (SB: slide / page by page), top to bottom within a screen; issues about
        the whole course come after the screens."""
        s = slide_index.get(f.get("slide_id"))
        idx = s["index"] if s and s.get("index") else (f.get("sb_page") or 99999)
        b = f.get("box") or []
        pos = f.get("pos_y")
        if pos is None:
            pos = (b[1] * 10000 + b[0]) if len(b) == 4 else 10 ** 9
        return (int(idx), f.get("time") or "", pos, rank[category(f)])
    findings.sort(key=key)
    # desktop issues first; everything found on the phone layout goes to the end under its own heading
    desktop = [f for f in findings if not f.get("mobile")]
    mobile = [f for f in findings if f.get("mobile")]
    rows = [("issue", f) for f in desktop]
    if mobile:
        rows.append(("heading", None))
        rows += [("issue", f) for f in mobile]

    first = 8
    last = first + max(len(rows), 1) - 1
    n = 0
    thin = Side(style="thin", color="000000")
    for i, (kind, f) in enumerate(rows):
        r = first + i
        if kind == "heading":
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=19)
            h = ws.cell(r, 1, f"MOBILE ISSUES – found on a phone (iPhone 13, landscape, touch) · {len(mobile)} issue{'s' if len(mobile) != 1 else ''}")
            h.font = Font(bold=True, size=12, color="FFFFFF")
            h.fill = PatternFill("solid", fgColor="1F4E79")
            h.alignment = Alignment(horizontal="left", vertical="center", indent=1)
            for col in range(1, 20):
                ws.cell(r, col).border = Border(top=thin, bottom=thin, left=thin if col == 1 else None, right=thin if col == 19 else None)
            ws.row_dimensions[r].height = 26
            continue
        n += 1
        _copy_row_style(ws, 8, r, 19)
        s = slide_index.get(f.get("slide_id"))
        ws.cell(r, 1, n)
        c = ws.cell(r, 2, review_date)
        c.number_format = "dd-mmm-yy"
        ws.cell(r, 3, REVIEWER)
        ws.cell(r, 4, screen_cell(f, s, res.get("stage"), is_video))
        if is_video:
            ws.cell(r, 5, "")
        elif res.get("stage") == "sb":
            ws.cell(r, 5, f.get("screen_title") or ((s or {}).get("title") or ""))
        else:
            ws.cell(r, 5, (s.get("title") or "") if s else "")
        text = describe(f)
        ws.cell(r, 6, _rich(text, ws.cell(8, 6).font))
        if f.get("mobile"):
            ws.cell(r, 6).font = Font(name=ws.cell(r, 6).font.name, size=ws.cell(r, 6).font.sz)
        ws.cell(r, 7, category(f))
        if CAT_FILL.get(category(f)):
            ws.cell(r, 7).fill = PatternFill("solid", fgColor=CAT_FILL[category(f)])
        ws.cell(r, 8, issue_found_in(f))
        if f.get("assigned_to") or res.get("stage") == "sb":   # storyboard issues go to the ID
            ws.cell(r, 9, f.get("assigned_to") or "ID")
        ws.cell(r, 12, STATUS)
        text_px = _lines(text) * LINE_PX + 8
        shot_rel = f.get("xl_screenshot") or f.get("screenshot")   # SB issues: course screen and SB side by side
        img_path = run_dir / shot_rel if shot_rel else None
        img_px = 0
        if img_path and img_path.exists():
            from PIL import Image as PILImage
            with PILImage.open(img_path) as im:
                w, h = im.size
            disp_w = IMG_W
            disp_h = round(h * disp_w / w)
            room = min(IMG_MAX_H, MAX_ROW_PX - text_px - 12)
            if disp_h > room:   # tall pictures (an SB page) are shrunk to the same height as the others
                disp_h = max(120, room)
                disp_w = round(w * disp_h / h)
            xl = XLImage(str(img_path))
            xl.width, xl.height = disp_w, disp_h
            marker = AnchorMarker(col=5, colOff=pixels_to_EMU(6), row=r - 1, rowOff=pixels_to_EMU(text_px))
            xl.anchor = OneCellAnchor(_from=marker, ext=XDRPositiveSize2D(pixels_to_EMU(disp_w), pixels_to_EMU(disp_h)))
            ws.add_image(xl)
            img_px = disp_h + 12
        ws.row_dimensions[r].height = min(409, max(31.5, (text_px + img_px) * 0.75))

    # tidy: the template's empty bordered rows below the last issue are cleared, and the sheet prints neatly
    from openpyxl.styles import Border as _B
    for rr in range(last + 1, 205):
        for col in range(1, 20):
            cell = ws.cell(rr, col)
            if cell.value is None:
                cell.border = _B()
                cell.fill = PatternFill(fill_type=None)
        ws.row_dimensions[rr].height = None
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.print_title_rows = "7:7"
    ws.page_margins.left = ws.page_margins.right = 0.3
    ws.page_margins.top = ws.page_margins.bottom = 0.5
    ws.print_area = f"A1:M{last}"
    # dropdowns over every issue row (template validations stop at row 120)
    end = max(last, 120) + 50
    keep = []
    for dv in ws.data_validations.dataValidation:
        ref = str(dv.sqref)
        if any(ref.startswith(p) for p in ("G8", "H8", "I8")):
            continue
        keep.append(dv)
    ws.data_validations.dataValidation = keep
    for col, formula in (("G", '"Missed in Previous Review,Suggestion,Update,Low,Medium,High,New PDN issue, New GD issue"'),
                         ("H", f"Lists!$A$1:$A${len(ISSUE_LIST)}"),
                         ("I", '"ID,Production,GD,SME,LMS, ID/PDN, ID/GD, GD/PDN,ID/PDN/GD"')):
        dv = DataValidation(type="list", formula1=formula, allow_blank=True)
        dv.add(f"{col}8:{col}{end}")
        ws.add_data_validation(dv)
    ws.freeze_panes = "A8"
    stage = res.get("stage") or "beta"
    if res.get("nav_table"):
        _nav_sheet(wb, res, run_dir)
    if stage in ("alpha", "beta", "gold") and not is_video and res.get("nav_table") is not None:
        _assessment_sheet(wb, res, run_dir)
    if stage in ("beta", "gold") and not is_video:
        _completion_sheet(wb, res, run_dir)
    for i, name in enumerate(("Navigation Test", "Assessment Test", "LMS Completion")):   # right after the issues
        if name in wb.sheetnames:
            wb.move_sheet(name, offset=wb.sheetnames.index(ws.title) + 1 + i - wb.sheetnames.index(name))
    wb.active = 0
    wb.save(out_path)
    return out_path


def _place(ws, path, cell_row, width_px):
    from PIL import Image as PILImage
    with PILImage.open(path) as im:
        w, h = im.size
    xl = XLImage(str(path))
    xl.width, xl.height = width_px, round(h * width_px / w)
    ws.add_image(xl, f"A{cell_row}")
    return xl.height


def _completion_sheet(wb, res, run_dir):
    """'Completion Status' tab: what the course reported to the LMS, with screenshots."""
    comp = res.get("completion") or {}
    ws = wb.create_sheet("LMS Completion")
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 80
    ws["A1"] = "LMS Course Completion"
    if not comp:
        ws["A3"] = ("Not checked: testing stopped before the last screen." if res.get("blocked")
                    else "Not checked in this run.")
        return
    ws["A1"].font = Font(bold=True, size=16)
    if "error" in comp:
        ws["A3"] = "Completion could not be read: " + comp["error"]
        return
    ok = comp.get("completed")
    ws["A3"] = "Result"
    ws["A3"].font = Font(bold=True)
    ws["B3"] = "COMPLETED" if ok else "NOT COMPLETED"
    ws["B3"].font = Font(bold=True, size=13, color="1D7A46" if ok else "B42318")
    ws["B3"].fill = PatternFill("solid", fgColor="E3F4EA" if ok else "FDECEA")
    rows = [("Status sent to LMS", comp.get("completion_status") or comp.get("lesson_status") or "No status sent"),
            ("Success status", comp.get("success_status") or "—"),
            ("Score", (comp.get("score") or "—") + (f" / {comp['score_max']}" if comp.get("score_max") else "")),
            ("SCORM version", comp.get("version") or "—"),
            ("Connected to LMS", "Yes" if comp.get("connected") else "No"),
            ("Launch file", comp.get("launch_file") or ""),
            ("Last slide reached", comp.get("last_slide") or ""),
            ("How it was tested", "Played with Next to the last slide in a test LMS; values read 3 seconds after the last slide loaded.")]
    thin = Side(style="thin", color="D0D4DA")
    for i, (k, v) in enumerate(rows, start=4):
        ws.cell(i, 1, k).font = Font(bold=True)
        ws.cell(i, 1).alignment = Alignment(vertical="top")
        ws.cell(i, 2, v).alignment = Alignment(wrap_text=True, vertical="top")
        for col in (1, 2):
            ws.cell(i, col).border = Border(bottom=thin)
    for col in (1, 2):
        ws.cell(3, col).border = Border(bottom=thin)
    row = 4 + len(rows) + 1
    restr = res.get("restrictions") or {}
    if restr:
        ws.cell(row, 1, "Restrictions (Beta, as per SOW)").font = Font(bold=True, size=12)
        row += 1
        hdr = ["Item", "Expected", "Found", "Result", "How it was checked"]
        ws.column_dimensions["C"].width = 14
        ws.column_dimensions["D"].width = 12
        ws.column_dimensions["E"].width = 70
        for j, h in enumerate(hdr, 1):
            c = ws.cell(row, j, h)
            c.font = Font(bold=True)
            c.border = Border(bottom=thin)
        names = {"menu": "Menu", "seekbar": "Seekbar", "next": "Next button"}
        for k in ("menu", "seekbar", "next"):
            o = restr.get(k)
            if not o:
                continue
            row += 1
            found = o.get("found")
            ok = found == o.get("expected")
            vals = [names[k], o.get("expected", "").capitalize(), (found or "Not checked").capitalize(),
                    "PASS" if ok else ("—" if found is None else "FAIL"), o.get("note", "")]
            for j, v in enumerate(vals, 1):
                c = ws.cell(row, j, v)
                c.alignment = Alignment(wrap_text=True, vertical="top")
                c.border = Border(bottom=thin)
            res_cell = ws.cell(row, 4)
            if found is not None:
                res_cell.font = Font(bold=True, color="1D7A46" if ok else "B42318")
                res_cell.fill = PatternFill("solid", fgColor="E3F4EA" if ok else "FDECEA")
        row += 2
    for title, key, width in (("LMS status screenshot", "panel_shot", 820), ("Last slide when the status was read", "final_shot", 820)):
        p = run_dir / comp[key] if comp.get(key) else None
        if not p or not p.exists():
            continue
        ws.cell(row, 1, title).font = Font(bold=True, size=12)
        h = _place(ws, p, row + 1, width)
        row += 2 + -(-h // 20)  # default row ≈ 20 px


GREEN, RED, GREY = ("E3F4EA", "1D7A46"), ("FDECEA", "B42318"), ("F1F3F5", "5D6877")
THIN = Side(style="thin", color="C9CED6")


def _thumb(run_dir, rel, width=520):
    """A small JPEG copy for the workbook (full-size screenshots would make the file very large)."""
    if not rel:
        return None
    src = Path(run_dir) / rel
    if not src.exists():
        return None
    from PIL import Image as PILImage
    out_dir = Path(run_dir) / "xl_thumbs"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / (re.sub(r"[^\w-]", "_", rel) + f"_{width}.jpg")
    if not out.exists():
        with PILImage.open(src) as im:
            im = im.convert("RGB")
            im = im.resize((width * 2, round(im.height * width * 2 / im.width)))
            im.save(out, "JPEG", quality=80, optimize=True)
    return out


def _put_img(ws, path, row, col, width):
    from PIL import Image as PILImage
    with PILImage.open(path) as im:
        w, h = im.size
    xl = XLImage(str(path))
    xl.width, xl.height = width, round(h * width / w)
    marker = AnchorMarker(col=col - 1, colOff=pixels_to_EMU(4), row=row - 1, rowOff=pixels_to_EMU(4))
    xl.anchor = OneCellAnchor(_from=marker, ext=XDRPositiveSize2D(pixels_to_EMU(xl.width), pixels_to_EMU(xl.height)))
    ws.add_image(xl)
    return xl.height


def _header(ws, row, names, widths):
    for j, (h, w) in enumerate(zip(names, widths), 1):
        c = ws.cell(row, j, h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E79")
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        c.border = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
        ws.column_dimensions[c.column_letter].width = w
    ws.row_dimensions[row].height = 30


def _result_cell(c):
    v = str(c.value or "")
    col = GREEN if v.startswith("Pass") or v == "Yes" else RED if v.startswith("Fail") or v.startswith("No") and not v.startswith(("No clickable", "No menu")) else GREY
    if v.startswith("Pass") or v.startswith("Fail"):
        c.font = Font(bold=True, color=col[1])
    c.fill = PatternFill("solid", fgColor=col[0])


def _title(ws, text, sub):
    ws.sheet_view.showGridLines = False
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws["A1"] = text
    ws["A1"].font = Font(bold=True, size=16)
    ws["A2"] = sub
    ws["A2"].font = Font(color="5D6877")


def _nav_sheet(wb, res, run_dir):
    """Navigation Test tab: every screen with its screenshot and the result of each navigation check."""
    ws = wb.create_sheet("Navigation Test")
    rows = res.get("nav_table") or []
    blocked = res.get("blocked")
    sub = ("Each screen was opened like a learner: Next (or START / Submit), Back, the menu, every clickable "
           "item opened and closed, and on a phone: swipe left (next) and swipe right (previous).")
    if blocked:
        sub += f" TESTING STOPPED at screen “{next((r['label'] for r in rows if r['sid'] == blocked['sid']), '')}”: {blocked['reason']}."
    _title(ws, "Navigation Test", sub)
    if blocked:
        ws["A2"].font = Font(bold=True, color=RED[1])
    passed = sum(1 for r in rows if r["result"] == "Pass")
    failed = sum(1 for r in rows if str(r["result"]).startswith("Fail"))
    ws["A3"] = f"Screens: {len(rows)}   ·   Pass: {passed}   ·   Fail: {failed}   ·   Not tested: {len(rows) - passed - failed}"
    ws["A3"].font = Font(bold=True)
    names = ["Screen No", "Screen title", "Screenshot", "Next Working Status", "Previous Working Status",
             "Mobile Swipe Next Status", "Mobile Swipe Previous Status", "Menu", "Clickable"]
    widths = [10, 26, 47, 18, 18, 20, 20, 16, 26]
    _header(ws, 5, names, widths)
    slides = {s["id"]: s for s in res.get("slides", [])}
    r = 6
    for row in rows:
        vals = [row["index"], row["title"], "", row["next"], row["back"], row.get("swipe_next", "—"),
                row.get("swipe_back", "—"), row["menu"], row["clicks"]]
        for j, v in enumerate(vals, 1):
            c = ws.cell(r, j, v)
            c.alignment = Alignment(wrap_text=True, vertical="top", horizontal="center" if j != 2 else None)
            c.border = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
            if j >= 4:
                _result_cell(c)
        img_h = 0
        th = _thumb(run_dir, (slides.get(row["sid"]) or {}).get("shot", ""), 330)
        if th:
            img_h = _put_img(ws, th, r, 3, 330)
        ws.row_dimensions[r].height = max(40, (img_h + 10) * 0.75)
        r += 1
    ws.freeze_panes = "A6"


def _assessment_sheet(wb, res, run_dir):
    """Assessment Test tab: the scenarios played (pass, fail, retry, no answer) and every answer option."""
    ws = wb.create_sheet("Assessment Test")
    A = res.get("assessment")
    _title(ws, "Assessment Test", "The assessment was played like a learner: every option of every question, Submit with no "
                                  "answer, all answers right, all answers wrong, and every Retry.")
    if not A:
        reason = (f"Not tested: testing stopped at an earlier screen ({res['blocked']['reason']})." if res.get("blocked")
                  else "No assessment (question screens) found in this course.")
        ws["A4"] = reason
        ws.column_dimensions["A"].width = 100
        return
    rules = A.get("rules") or {}
    facts = [("Question screens", len(A.get("quiz_ids") or [])),
             ("Rules screen says", ", ".join(f"{k.replace('_', ' ')}: {v}{'%' if k == 'pass_mark' else ''}" for k, v in rules.items()) or "—"),
             ("Pass mark in course settings", f"{A['pass_mark']}%" if A.get("pass_mark") is not None else "—"),
             ("Attempts allowed (tested)", A.get("attempts_seen") or "—")]
    r = 4
    for k, v in facts:
        ws.cell(r, 2, k).font = Font(bold=True)
        ws.cell(r, 3, v).alignment = Alignment(horizontal="left")
        r += 1
    r += 1
    ws.cell(r, 1, "Scenarios").font = Font(bold=True, size=13)
    r += 1
    _header(ws, r, ["Sl.no", "Scenario", "Steps", "Expected", "Actual", "Result", "Screenshot"], [7, 26, 36, 36, 42, 11, 47])
    r += 1
    for i, sc in enumerate(A.get("scenarios") or [], 1):
        vals = [i, sc["name"], sc["steps"], sc["expected"], sc["actual"], sc["result"], ""]
        for j, v in enumerate(vals, 1):
            c = ws.cell(r, j, v)
            c.alignment = Alignment(wrap_text=True, vertical="top")
            c.border = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
        _result_cell(ws.cell(r, 6))
        th = _thumb(run_dir, sc.get("shot"), 330)
        h = _put_img(ws, th, r, 7, 330) if th else 0
        ws.row_dimensions[r].height = max(60, (h + 10) * 0.75)
        r += 1
    r += 1
    ws.cell(r, 1, "Questions and answer options").font = Font(bold=True, size=13)
    r += 1
    _header(ws, r, ["Sl.no", "Question screen", "Answer option", "Can be selected", "Feedback shown", "Result", "Screenshot"],
            [7, 26, 36, 36, 42, 11, 47])
    r += 1
    n = 0
    for q in A.get("questions") or []:
        opts = q.get("options") or []
        if not opts:
            n += 1
            note = q.get("note") or {"other": "Not a choice question (drag and drop, hotspot or typing): options checked by hand",
                                     "not reached": "The question screen could not be opened",
                                     "not drawn": "Not drawn in the tested attempts (question bank)"}.get(q.get("kind"), "No answer options found")
            for j, v in enumerate([n, q["label"], note, "—", "—", "Not tested", ""], 1):
                c = ws.cell(r, j, v)
                c.alignment = Alignment(wrap_text=True, vertical="top")
                c.border = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
            _result_cell(ws.cell(r, 6))
            th = _thumb(run_dir, q.get("shot"), 330)
            h = _put_img(ws, th, r, 7, 330) if th else 0
            ws.row_dimensions[r].height = max(40, (h + 10) * 0.75)
            r += 1
            continue
        for k, o in enumerate(opts):
            n += 1
            ok = bool(o["selectable"]) and o["feedback"] not in ("No Submit button",)
            fb = o["feedback"] or ("Not submitted (selected only)" if o["selectable"] else "—")
            if k in (q.get("key") or []):
                fb += " (right answer)"
            vals = [n, q["label"], o["label"], "Yes" if o["selectable"] else "No", fb, "Pass" if ok else "Fail", ""]
            for j, v in enumerate(vals, 1):
                c = ws.cell(r, j, v)
                c.alignment = Alignment(wrap_text=True, vertical="top")
                c.border = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
            _result_cell(ws.cell(r, 4))
            _result_cell(ws.cell(r, 6))
            th = _thumb(run_dir, o.get("shot"), 330)
            h = _put_img(ws, th, r, 7, 330) if th else 0
            ws.row_dimensions[r].height = max(40, (h + 10) * 0.75)
            r += 1
