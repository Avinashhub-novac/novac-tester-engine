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

REVIEWER = "Novac Tester (Claude)"
ISSUE_LIST = ["Transcript", "Audio", "OST", "Animation", "ARD", "Graphic", "Header", "Buttons", "Menu", "Objectives",
              "Assessment", "Assessment Strategy", "Resource", "Seek Bar", "Slide Title", "Skin", "Video", "Navigation",
              "Content Flow", "SB-Spelling", "SB-Grammar", "SB-Casing", "SB-Audio Script", "SB-OST",
              "SB-OST/Audio Script", "VO INS", "Animation INS"]
IMG_W = 700          # display width of a screenshot in the cell, px (column F is ~770 px wide)
MAX_ROW_PX = 540     # Excel's row-height ceiling is 409 pt (~545 px)
LINE_PX = 18
CHARS_PER_LINE = 105


def category(f):
    """Maps a finding to the template's issue categories (Description sheet)."""
    c, sev, msg = f["check"], f["severity"], f["message"].lower()
    if c in ("assessment", "logic"):
        return "High" if sev == "error" else "Medium"
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
    if c.startswith("sb_"):
        return f.get("found_in") if f.get("found_in") in ISSUE_LIST else "SB-OST"
    if c in ("content", "restrictions"):
        return f.get("found_in") if f.get("found_in") in ISSUE_LIST else ("Content Flow" if c == "content" else "Navigation")
    if c in ("grammar", "spelling"):
        if " in slide title" in msg:
            return "Slide Title"
        if " in captions" in msg or " in notes" in msg:
            return "Transcript"
        return "OST"
    return {"navigation": "Navigation", "swipe": "Navigation", "menu": "Menu", "buttons": "Buttons",
            "screen_number": "Header", "transcript": "Transcript", "audio": "Audio", "seekbar": "Seek Bar",
            "video": "Video", "assessment": "Assessment", "logic": "Content Flow"}.get(c, "Navigation")


def describe(f):
    msg = f["message"]
    if msg.startswith("SB: "):
        msg = msg[4:]
    parts = [msg.rstrip(".") + "."]
    if f.get("detail"):
        parts.append(f["detail"])
    if f.get("suggestion"):
        parts.append("Suggested fix: " + f["suggestion"])
    if f.get("rule"):
        parts.append("Checklist: " + f["rule"])
    return "\n".join(parts)


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
        s = slide_index.get(f.get("slide_id"))
        return (s["index"] if s and s.get("index") else (0 if not f.get("slide_id") else 9999),
                f.get("time") or "", rank[category(f)])
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
        if is_video:
            ws.cell(r, 4, f.get("slide_label") or f.get("time") or "Whole video")
            ws.cell(r, 5, "")
        elif res.get("stage") == "sb":   # storyboard review: SB screen number (and page) instead of a course slide
            pg = f.get("sb_page") or (s["index"] if s else "")
            ws.cell(r, 4, (f"{f['sb_screen']} (p. {pg})" if f.get("sb_screen") else f"Page {pg}") if pg else "Whole SB")
            ws.cell(r, 5, f.get("screen_title") or "")
        else:
            ws.cell(r, 4, s["index"] if s and s.get("index") else "Course-wide")
            ws.cell(r, 5, (s.get("title") or "") if s else "")
        text = ("MOBILE ISSUE: " if f.get("mobile") else "") + describe(f)
        ws.cell(r, 6, text)
        if f.get("mobile"):
            ws.cell(r, 6).font = Font(name=ws.cell(r, 6).font.name, size=ws.cell(r, 6).font.sz)
        ws.cell(r, 7, category(f))
        ws.cell(r, 8, issue_found_in(f))
        ws.cell(r, 12, "Open")
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
            room = MAX_ROW_PX - text_px - 12
            if disp_h > room:
                disp_h = max(120, room)
                disp_w = round(w * disp_h / h)
            xl = XLImage(str(img_path))
            xl.width, xl.height = disp_w, disp_h
            marker = AnchorMarker(col=5, colOff=pixels_to_EMU(6), row=r - 1, rowOff=pixels_to_EMU(text_px))
            xl.anchor = OneCellAnchor(_from=marker, ext=XDRPositiveSize2D(pixels_to_EMU(disp_w), pixels_to_EMU(disp_h)))
            ws.add_image(xl)
            img_px = disp_h + 12
        ws.row_dimensions[r].height = min(409, max(31.5, (text_px + img_px) * 0.75))

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
    _completion_sheet(wb, res, run_dir)
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
    if not comp and not res.get("restrictions"):
        return
    ws = wb.create_sheet("Completion Status", 1)
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 80
    ws["A1"] = "Completion Status"
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
        ws.cell(row, 1, "Restrictions (Beta, as per RCD)").font = Font(bold=True, size=12)
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
