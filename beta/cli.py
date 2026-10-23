"""Command-line entry used when Claude itself runs the tests in the background.

    python3 -m beta.cli run      --work W --id ID --upload FILE --kind scorm|video [--captions F] [--options JSON] [--glossary TXT] [--name N]
    python3 -m beta.cli merge    --work W --id ID --answers claude_answers.json
    python3 -m beta.cli bundle   --work W --id ID
    python3 -m beta.cli report   --work W --id ID
    python3 -m beta.cli finalize --work W --id ID --map shots_map.json

`run` does every browser/audio check and writes claude_tasks.json with the text Claude must
review (proofreading, captions vs. narration script). `merge` folds Claude's answers back
into results.json. `bundle` shrinks screenshots to JPEG for upload, and `finalize` swaps
local screenshot names for the uploaded asset ids.
"""
import argparse
import re
import functools
import json
import os
import shutil
import socket
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass   # the browser often stops a video download early (a "broken pipe"): not an error


def cmd_run(a):
    work = Path(a.work).resolve()
    port = _free_port()
    os.environ.update(DATA_DIR=str(work), PORT=str(port), LANGUAGE_MODE="export")
    from beta import config, runner  # noqa: imported after env is set
    run_dir = config.RUNS_DIR / a.id
    run_dir.mkdir(parents=True, exist_ok=True)
    up = Path(a.upload)
    dest = run_dir / up.name
    if up.resolve() != dest.resolve():
        shutil.copy(up, dest)
    cap = ""
    if a.captions:
        cap = "captions" + Path(a.captions).suffix.lower()
        shutil.copy(a.captions, run_dir / cap)
    sg = ""
    if getattr(a, "styleguide", None):
        sg = "styleguide_src" + Path(a.styleguide).suffix.lower()
        shutil.copy(a.styleguide, run_dir / sg)
    sb = ""
    if a.sb:
        sb = "storyboard" + Path(a.sb).suffix.lower()
        shutil.copy(a.sb, run_dir / sb)
    meta = {"id": a.id, "kind": a.kind, "name": a.name or up.name, "file": up.name, "captions": cap, "storyboard": sb,
            "styleguide": sg,
            "created": time.time(), "status": "queued", "glossary": a.glossary or "",
            "options": json.loads(a.options or "{}")}
    (run_dir / "run.json").write_text(json.dumps(meta, indent=1))
    srv = _Server(("127.0.0.1", port), functools.partial(_Quiet, directory=str(work)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        runner.execute(run_dir)
    finally:
        srv.shutdown()
    m = json.loads((run_dir / "run.json").read_text())
    tasks = run_dir / "claude_tasks.json"
    print(json.dumps({"status": m["status"], "error": m.get("error", ""), "totals": m.get("totals"),
                      "claude_tasks": str(tasks) if tasks.exists() else None}, indent=1))
    return 0 if m["status"] == "done" else 1


def _paths(a):
    work = Path(a.work).resolve()
    run_dir = work / "runs" / a.id
    return run_dir, json.loads((run_dir / "results.json").read_text())


def _save(run_dir, res):
    from beta.runner import summarize
    from beta import scope
    try:
        meta = json.loads((run_dir / "run.json").read_text())
    except Exception:
        meta = {}
    stage = res.get("stage") or (meta.get("options") or {}).get("stage") or "beta"
    kind = meta.get("kind", "")
    scope.apply(res, stage, kind)   # nothing outside the stage's scope is ever reported
    res["summary"] = summarize(res["findings"], set(res.get("checks_run", [])), res.get("checks_skipped", {}))
    scope.apply(res, stage, kind)   # the checklist for the result page, from the final summary
    (run_dir / "results.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))


def _shot_for(slide, text):
    """The caption frame that shows `text`, else the slide's own screenshot."""
    want = " ".join((text or "").lower().split())[:60]
    for c in slide.get("cap_shots") or []:
        if want and want in " ".join(c["text"].lower().split()):
            return c["shot"]
    return slide.get("shot", "")


def cmd_merge(a):
    os.environ.setdefault("DATA_DIR", a.work)
    run_dir, res = _paths(a)
    tasks = json.loads((run_dir / "claude_tasks.json").read_text())
    ans = json.loads(Path(a.answers).read_text())
    added = 0

    def item(kind, iid):
        return next((t for t in tasks.get(kind, []) if str(t["id"]) == str(iid)), None)

    def add(**f):
        nonlocal added
        base = {"check": "", "severity": "warning", "message": "", "slide_id": "", "slide_label": "", "detail": "",
                "suggestion": "", "screenshot": "", "time": ""}
        base.update(f)
        res["findings"].append(base)
        added += 1

    from beta.runner import issue_to_finding
    for iss in ans.get("proofread", []):
        it = item("proofread", iss.get("item_id")) or {}
        if "^" not in str(it.get("text", "")):   # formatting marks (^superscript^) belong to storyboards only
            iss = {k: (v.replace("^", "") if isinstance(v, str) else v) for k, v in iss.items()}
        add(**issue_to_finding(iss, it))
    for v in ans.get("script_compare", []):
        it = item("script_compare", v.get("item_id")) or {}
        if v.get("verdict") in ("mismatch", "minor_differences"):
            for d in (v.get("differences") or [""]):   # one issue per row
                add(check="transcript", severity="error" if v["verdict"] == "mismatch" else "warning",
                    message="Closed captions differ from the transcript",
                    slide_id=it.get("slide_id", ""), slide_label=it.get("label", ""), screenshot=it.get("shot", ""),
                    locate=" ".join((it.get("script") or "").split()[:6]), detail=d,
                    fix="Make the captions match the transcript (VO script).")
    vo_only = (tasks.get("sb_compare") or {}).get("mode") == "vo_only"
    for v in ans.get("sb_compare", []):
        if vo_only:   # Beta: only VO (SB) vs captions / transcript; never issues in the SB itself
            if str(v.get("error_in") or "").lower() == "sb":
                continue
            v = {**v, "found_in": "Transcript"}
        sid = v.get("slide_id") or ""
        sl = next((x for x in tasks.get("sb_compare", {}).get("slides", []) if x["id"] == sid), {})
        details = [v["explanation"]] if v.get("explanation") else []
        in_sb = str(v.get("error_in") or "").lower() == "sb"
        add(check="content", course_says=v.get("course_says", ""), sb_only=in_sb, assigned_to="ID" if in_sb else "",
            fix=v.get("suggestion") or ("Correct the storyboard." if in_sb else
                ("Change the course to match the SB." if v.get("sb_says") else "Check this with the SB and correct it.")), severity=v.get("severity") if v.get("severity") in ("error", "warning") else "error",
            message=f"Not as per SB – {v.get('issue', 'content differs')}", slide_id=sid,
            slide_label=sl.get("label", v.get("time", "")), time=v.get("time", ""), detail="\n".join(details),
            suggestion=v.get("sb_says", "") if v.get("found_in") in ("OST", "Slide Title") else "",
            found_in=v.get("found_in", "Content Flow"), locate=(v.get("course_says") or "")[:80],
            sb_says=v.get("sb_says", ""), sb_page=v.get("sb_page") or "",
            screenshot=_shot_for(sl, v.get("course_says", "")))
    slide_label = lambda sid: next((x.get("label") for x in res.get("slides", []) if x.get("id") == sid), "")
    for kind, check in (("logic_check", "logic"), ("assessment_review", "assessment")):
        for v in ans.get(kind, []):
            sid = v.get("slide_id") or ""
            details = [f"“{v['quote']}”"] if v.get("quote") else []
            if v.get("explanation"):
                details.append(v["explanation"])
            add(check=check, severity=v.get("severity") if v.get("severity") in ("error", "warning") else "warning",
                message=v.get("issue", "Check this"), slide_id=sid, slide_label=slide_label(sid), detail="\n".join(details),
                suggestion=v.get("suggestion", ""), fix=v.get("suggestion", ""), course_says=v.get("quote", ""), locate=(v.get("quote") or "")[:80],
                screenshot=next((x.get("shot", "") for x in res.get("slides", []) if x.get("id") == sid), ""))
    rules = {r["rule"]: r["text"] for r in (tasks.get("sb_review") or {}).get("rules", [])}
    from beta.sbreview import RULE_GROUP, FOUND_IN
    from beta.sbreview import META_LABEL, DEV_SENTENCE
    for v in ans.get("sb_review", []):
        try:
            rule = int(v.get("rule") or 1)
        except (TypeError, ValueError):
            rule = 1
        q = str(v.get("quote") or "")
        if rule in (6, 10, 13) and (META_LABEL.match(q) or DEV_SENTENCE.search(q)):
            continue   # notes for the developer are not course content (team rule): never duplication / OST-VO / title issues
        page = int(v.get("page") or 0) or None
        details = []
        if v.get("quote"):
            details.append(f"“{v['quote']}”")
        if v.get("explanation"):
            details.append(v["explanation"])
        advice = str(v.get("severity") or "").lower() == "suggestion"
        add(check=RULE_GROUP.get(rule, "sb_language"), severity=v.get("severity") if v.get("severity") in ("error", "warning") else "warning",
            advice=advice,
            message=f"SB: {v.get('issue', 'issue')}", slide_id=f"p{page}" if page else "", slide_label=f"Page {page}" if page else "",
            detail="\n".join(details), suggestion=v.get("suggestion", ""), sb_says=v.get("quote", ""), sb_page=page or 0,
            rule=rules.get(rule, ""), found_in=FOUND_IN.get(v.get("found_in"), v.get("found_in") or "SB-OST"),
            sb_screen=str(v.get("screen") or ""), screen_title=v.get("screen_title", ""), sb_only=True, assigned_to="ID",
            fix=v.get("suggestion", ""))
    for v in ans.get("style_guide", []):   # issues against the team's style guide (SB and Alpha)
        page = int(v.get("page") or 0) or None
        sid = v.get("slide_id") or (f"p{page}" if page else "")
        q = str(v.get("quote") or "")
        rule = str(v.get("guide_rule") or "").strip()
        details = [f"“{q}”"] if q else []
        if v.get("explanation"):
            details.append(v["explanation"])
        if rule:
            details.append(f"Style guide: {rule}")
        sb_stage = (tasks.get("style_guide") or {}).get("applies_to") == "sb"
        add(check="style_guide", severity=v.get("severity") if v.get("severity") in ("error", "warning") else "warning",
            message=(f"SB: {v.get('issue', 'Not as per the style guide')}" if sb_stage else v.get("issue", "Not as per the style guide")),
            slide_id=sid, slide_label=(f"Page {page}" if sb_stage and page else slide_label(sid)), detail="\n".join(details),
            suggestion=v.get("suggestion", ""), fix=v.get("suggestion", ""), course_says=q, locate=q[:80],
            sb_says=q if sb_stage else "", sb_page=page or 0, sb_only=sb_stage, assigned_to="ID" if sb_stage else "",
            found_in="Style guide", rule=rule,
            screenshot="" if sb_stage else next((x.get("shot", "") for x in res.get("slides", []) if x.get("id") == sid), ""))
    for v in ans.get("transcript_review", []):
        it = item("transcript_review", v.get("item_id")) or {}
        if v.get("verdict") in ("mismatch", "minor_differences"):
            for d in (v.get("differences") or it.get("diffs") or [""]):   # one issue per row
                add(check="transcript", severity="error" if v["verdict"] == "mismatch" else "warning",
                    message=f"Captions differ from narration ({it.get('score', 0) * 100:.0f}% word match)",
                    slide_id=it.get("slide_id", ""), slide_label=it.get("label", ""), time=it.get("time", ""),
                    detail=d, fix="Make the captions match what is spoken.")
    _save(run_dir, res)
    print(f"merged {added} findings")


def _norm_word(w):
    return re.sub(r"[^\w']+", "", w.lower())


def _find_text_box(tb, needle):
    """Tight box around `needle` (one or more words) on the slide; falls back to the text block."""
    if isinstance(tb, list):  # older format: blocks only
        tb = {"nodes": tb, "words": []}
    want = [_norm_word(w) for w in (needle or "").split() if _norm_word(w)]
    if not want:
        return None
    words = tb.get("words", [])
    for i in range(len(words) - len(want) + 1):
        seq = words[i:i + len(want)]
        if [_norm_word(x["w"]) for x in seq] == want:
            xs = [x["b"] for x in seq]
            x0, y0 = min(b[0] for b in xs), min(b[1] for b in xs)
            x1, y1 = max(b[0] + b[2] for b in xs), max(b[1] + b[3] for b in xs)
            return [x0, y0, x1 - x0, y1 - y0]
    flat = " ".join(want)
    hits = [b for b in tb.get("nodes", []) if flat in " ".join(_norm_word(w) for w in b["t"].split())]
    return min(hits, key=lambda b: b["b"][2] * b["b"][3])["b"] if hits else None


def _line_boxes(tb, needle):
    """The words of `needle` on the screen, one tight box per line of text (so a quote that wraps is marked on
    each line instead of with one big box over both lines)."""
    if isinstance(tb, list):
        tb = {"nodes": tb, "words": []}
    want = [_norm_word(w) for w in (needle or "").split() if _norm_word(w)]
    words = (tb or {}).get("words", [])
    for i in range(len(words) - len(want) + 1):
        seq = words[i:i + len(want)]
        if want and [_norm_word(x["w"]) for x in seq] == want:
            lines = []
            for x in seq:
                b = x["b"]
                if lines and abs((b[1] + b[3] / 2) - (lines[-1][1] + lines[-1][3] / 2)) < max(4, b[3] * 0.5):
                    l = lines[-1]
                    x0, y0 = min(l[0], b[0]), min(l[1], b[1])
                    lines[-1] = [x0, y0, max(l[0] + l[2], b[0] + b[2]) - x0, max(l[1] + l[3], b[1] + b[3]) - y0]
                else:
                    lines.append(list(b))
            return lines
    return []


def _locate(f, slide, shot_boxes=None, src_rel=""):
    """Best guess at where on the screenshot a finding is, in CSS pixels."""
    if f.get("box"):
        return f["box"]
    # text positions recorded with this exact screenshot (caption frames, transcript panel) win over the slide's
    tb = (shot_boxes or {}).get(src_rel) or ((slide or {}).get("boxes") or {})
    needle = f.get("locate")
    if not needle and f["check"] == "buttons" and f.get("detail"):
        needle = f["detail"].split("\n")[0]
    if not needle and f["check"] in ("grammar", "spelling", "transcript"):
        m = re.search(r"“(.+?)”", f["message"])
        needle = m.group(1) if m else None
    if needle:
        return _find_text_box(tb, needle)
    if not slide:
        return None
    if f["check"] == "screen_number":
        from beta.textcheck import find_screen_number
        nodes = tb if isinstance(tb, list) else tb.get("nodes", [])
        for b in nodes:
            hit = find_screen_number(b["t"])
            if hit:
                return _find_text_box(tb, hit[2]) or b["b"]
    return None


def _font(size, bold=True):
    from PIL import ImageFont
    for name in (("DejaVuSans-Bold.ttf",) if bold else ()) + ("DejaVuSans.ttf",):
        try:
            return ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/{name}", size)
        except OSError:
            continue
    return ImageFont.load_default()


def _render(src, out, box, vw, tag="", mark=True, more=None):
    """Saves a sharp JPEG with the issue's exact spot highlighted in yellow and underlined in red (or a thin red
    frame round the whole screen when the issue is about the screen as a whole). Nothing is written on the
    picture: the issue is explained in the Feedback description only. `more` = further spots of the same issue
    (for example the lines of a quote that wraps). `tag` is kept for callers but no longer drawn."""
    from PIL import Image, ImageDraw
    im = Image.open(src).convert("RGB")
    k = im.width / vw if vw else 1.0
    red = (220, 20, 30)
    if not mark:
        box = None

    def rect(bx):
        x, y, w, h = [v * k for v in bx]
        pad = 3 * k
        return [max(1, x - pad), max(1, y - pad), min(im.width - 2, x + w + pad), min(im.height - 2, y + h + pad)]
    boxes = [b for b in ([box] + list(more or [])) if b] if (mark and box) else []
    # never mark an empty spot: the area must show something (text, a button, a picture) in this very screenshot –
    # a flat, single-colour area means the object is not visible there (hidden, faded out, not drawn yet)
    if boxes:
        from PIL import ImageStat
        gray = im.convert("L")

        def drawn(bx):
            x0_, y0_, x1_, y1_ = [int(v) for v in rect(bx)]
            if x1_ - x0_ < 3 or y1_ - y0_ < 3:
                return False
            return ImageStat.Stat(gray.crop((x0_, y0_, x1_, y1_))).stddev[0] >= 4.0
        boxes = [b for b in boxes if drawn(b)]
    if mark and boxes:
        glow = Image.new("RGBA", im.size, (0, 0, 0, 0))
        g = ImageDraw.Draw(glow)
        for bx in boxes:   # a highlighter stroke over the words / the object
            g.rectangle(rect(bx), fill=(255, 230, 0, 95))
        im = Image.alpha_composite(im.convert("RGBA"), glow).convert("RGB")
        d = ImageDraw.Draw(im)
        lw = max(3, round(2.5 * k))
        for bx in boxes:   # and a red underline just below it
            x0, y0, x1, y1 = rect(bx)
            yl = min(im.height - lw, y1 + max(1, round(1 * k)))
            d.rectangle([x0, yl, x1, yl + lw], fill=red)
    elif mark:   # no single spot: a thin frame shows the issue is about this screen as a whole
        d = ImageDraw.Draw(im)
        w = max(3, round(2.5 * k))
        d.rectangle([w // 2, w // 2, im.width - 1 - w // 2, im.height - 1 - w // 2], outline=red, width=w)
    if im.width > 1800:
        im = im.resize((1800, round(im.height * 1800 / im.width)), Image.LANCZOS)
    im.save(out, "JPEG", quality=90, optimize=True, progressive=True)


def _placeholder(out, label):
    """A clearly marked card used only when the tester could not open a screen at all."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (1366, 768), (245, 246, 248))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([12, 12, 1353, 755], radius=14, outline=(225, 29, 29), width=5)
    d.text((70, 300), f"No screenshot: {label}"[:70], font=_font(40), fill=(30, 30, 30))
    d.text((70, 370), "The tester could not open this screen. Please check it by hand.", font=_font(28, bold=False), fill=(80, 80, 80))
    im.save(out, "JPEG", quality=90)


def _sb_evidence(run_dir, words, f, out_rel, tag=""):
    """Picture of the storyboard page with the SB words the issue quotes highlighted and underlined. When the
    words are in the speaker notes of a PowerPoint storyboard (the VO), the notes page of that slide is shown."""
    import json
    from PIL import Image, ImageDraw
    from beta.storyboard import find_on_pages, quote_lines
    quote = f.get("sb_says") or ""
    page, box, score = find_on_pages(words, quote, f.get("sb_page") or None)
    kind = "page"
    nf = run_dir / "sb" / "words_notes.json"
    if nf.exists() and (not box or score < 0.999):
        notes = json.loads(nf.read_text())
        hint = str(f.get("sb_page") or page or "")
        cands = {hint: notes[hint]} if hint in notes else notes
        n_page, n_box, n_score = find_on_pages(cands, quote, hint or None)
        if n_box and n_score >= 0.6 and (n_score > score or not box):
            page, box, score, kind = n_page, n_box, n_score, "notes"
    if not box and nf.exists() and (f.get("found_in") == "SB-Audio Script" or re.search(r"\bVO\b|voice|narrat|audio",
                                                                              f.get("message", ""), re.I)):
        hint = str(f.get("sb_page") or page or "")
        if hint and (run_dir / "sb" / f"notes{hint}.png").exists():
            page, kind = int(hint), "notes"   # a VO issue always shows the VO (the notes page), even unmarked
    f["pos_y"] = (box[1] + (10 ** 6 if kind == "notes" else 0)) if box else 10 ** 8   # order within the page
    lines = []
    if box:
        allw = json.loads((run_dir / "sb" / ("words_notes.json" if kind == "notes" else "words.json")).read_text())
        lines = quote_lines(allw.get(str(page), []), quote)
    if not page:
        return None
    src = run_dir / "sb" / f"{kind}{page}.png"
    if not src.exists():
        return None
    im = Image.open(src).convert("RGB")
    # trim the empty margins (a notes page is mostly white): the picture is then big enough to read in the report
    from PIL import ImageOps
    probe = im.crop((0, 0, im.width, int(im.height * (0.93 if kind == "notes" else 1.0))))   # not the page number
    bb = ImageOps.invert(probe.convert("L")).point(lambda v: 255 if v > 18 else 0).getbbox()
    if bb:
        pad = 24
        x0c, y0c = max(0, bb[0] - pad), max(0, bb[1] - pad)
        x1c, y1c = min(im.width, bb[2] + pad), min(im.height, bb[3] + pad)
        if (x1c - x0c) * (y1c - y0c) < im.width * im.height * 0.92:
            im = im.crop((x0c, y0c, x1c, y1c))
            if box:
                box = [box[0] - x0c, box[1] - y0c, box[2], box[3]]
            lines = [[b[0] - x0c, b[1] - y0c, b[2], b[3]] for b in lines]
    portrait = im.height > im.width * 1.1
    if box and portrait and kind == "page":   # a band of a long page around the quoted words
        top = max(0, int(box[1] - 300))
        bottom = min(im.height, int(box[1] + box[3] + 300))
        im = im.crop((0, top, im.width, bottom))
        box = [box[0], box[1] - top, box[2], box[3]]
        lines = [[b[0], b[1] - top, b[2], b[3]] for b in lines]
    if len(lines) > 1:
        box, more = lines[0], lines[1:]
    else:
        more = None
    tmp = run_dir / (out_rel + ".png")
    im.save(tmp)
    _render(tmp, run_dir / out_rel, box, im.width, mark=bool(box), more=more)
    tmp.unlink()
    # a slim band above names the page (nothing about the issue: that is in the Feedback description)
    im2 = Image.open(run_dir / out_rel).convert("RGB")
    fs = max(22, im2.width // 55)   # the same size on every picture once it is shrunk into the report
    band = int(fs * 2)
    out_im = Image.new("RGB", (im2.width, im2.height + band), (255, 255, 255))
    out_im.paste(im2, (0, band))
    d = ImageDraw.Draw(out_im)
    d.rectangle([0, 0, im2.width, band - 4], fill=(45, 55, 72))
    label = f"SB page {page}" + (" · speaker notes (VO)" if kind == "notes" else "")
    if kind == "notes" and f.get("found_in") in ("SB-OST", "", None):
        f["found_in"] = "SB-Audio Script"   # the words are in the VO (speaker notes), not on screen
    d.text((int(fs * 0.6), int(fs * 0.4)), label, font=_font(fs), fill=(255, 255, 255))
    out_im.save(run_dir / out_rel, "JPEG", quality=90, optimize=True)
    return out_rel


def _side_by_side(a, b, out, cap_a, cap_b):
    """Course screen and SB page next to each other, each with a caption (used in the Excel report)."""
    from PIL import Image, ImageDraw
    A, B = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
    W = 1100
    A = A.resize((W, round(A.height * W / A.width)))
    B = B.resize((W, round(B.height * W / B.width)))
    cap = 52
    H = max(A.height, B.height) + cap
    im = Image.new("RGB", (W * 2 + 24, H), (255, 255, 255))
    d = ImageDraw.Draw(im)
    for x, img, text, col in ((0, A, cap_a, (31, 78, 121)), (W + 24, B, cap_b, (120, 63, 4))):
        d.rectangle([x, 0, x + W, cap - 8], fill=col)
        d.text((x + 14, 8), text[:70], font=_font(30), fill=(255, 255, 255))
        im.paste(img, (x, cap))
    im.save(out, "JPEG", quality=88, optimize=True)


def _needle(f):
    if f.get("locate"):
        return f["locate"]
    if f.get("check") in ("grammar", "spelling", "transcript"):
        m = re.search(r"“(.+?)”", f.get("message", ""))
        return m.group(1) if m else None
    return None


def _nice_label(label):
    """Image file names are what Storyline uses when a picture has no alt text: say "picture" instead."""
    if re.search(r"\.(png|jpe?g|gif|svg)$", label or "", re.I) or re.match(r"^Asset \d+", label or ""):
        return "picture"
    return label


def _group(findings, slides):
    """Report each problem once, and one problem per row.
    1. The same button failing on three or more screens (a button on the slide master) -> one row naming the screens.
    2. The same problem on three or more screens (e.g. the seekbar shown on every screen) -> one row naming the screens.
    Different problems on one screen always stay separate rows, each with its own outlined spot."""
    order = {s["id"]: s.get("index", 0) for s in slides.values()} if isinstance(slides, dict) else {}
    # 0. the same button on many screens (usually a button on the master slide): one row for it
    by_label = {}
    for f in findings:
        if f.get("check") == "buttons" and f.get("slide_id") and (f.get("detail") or "").strip():
            by_label.setdefault((f["detail"].split("\n")[0], f.get("message"), f.get("mobile")), []).append(f)
    drop = set()
    for (label, msg, mob), group in by_label.items():
        if len({g["slide_id"] for g in group}) >= 3:
            group.sort(key=lambda f: order.get(f.get("slide_id"), 0))
            first = group[0]
            first["detail"] = (label + f"\nSame button on {len(group)} screens: "
                               + ", ".join((g.get("slide_label") or g["slide_id"]) for g in group[:30])).strip()
            first["message"] = f"“{_nice_label(label)}” button not working (on every screen it appears)"
            first["tag"] = "Button not working"
            first["_lab"] = True
            drop.update(id(g) for g in group[1:])
    findings = [f for f in findings if id(f) not in drop]
    # one issue per row: several problems on one screen (e.g. two buttons that do nothing) stay separate rows,
    # each with its own outlined spot
    out = []
    for f in findings:
        f.pop("_lab", None)
        out.append(f)
    # the same problem across many screens
    across, final = {}, []
    for f in out:
        if not f.get("slide_id") or f.get("check") == "content" or f.get("check", "").startswith("sb_"):
            final.append(f)
            continue
        # the same wording matters for language issues; for navigation and the like the same message is the same problem
        key = (f.get("check"), f.get("message"), f.get("mobile"),
               (f.get("detail") or "").split("\n")[0] if f.get("check") in ("grammar", "spelling", "transcript", "logic") else "")
        across.setdefault(key, []).append(f)
    for key, group in across.items():
        need = 2 if key[0] in ("grammar", "spelling", "transcript") else 3   # the same mistake in the same words
        if len(group) < need or key[0] == "buttons":
            final.extend(group)
            continue
        group.sort(key=lambda f: order.get(f.get("slide_id"), 0))
        first = group[0]
        labels = [g.get("slide_label") or g.get("slide_id") for g in group]
        first["detail"] = ((first.get("detail") or "") + f"\nSame problem on {len(group)} screens: " + ", ".join(labels[:30])
                           + ("…" if len(labels) > 30 else "")).strip()
        first["screens"] = [g.get("slide_id") for g in group]
        final.append(first)
    # keep the original order of the report
    pos = {id(f): i for i, f in enumerate(findings)}
    final.sort(key=lambda f: pos.get(id(f), 0))
    return final


def cmd_bundle(a):
    _phase(a, "report")
    run_dir, res = _paths(a)
    out = run_dir / "bundle"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    slides = {s["id"]: s for s in res.get("slides", [])}
    plain = {}

    def plain_copy(rel, vw):
        if rel not in plain:
            src = run_dir / rel
            if not src.exists():
                return ""
            name = "bundle/" + Path(rel).stem + ".jpg"
            _render(src, run_dir / name, None, vw, mark=False)
            plain[rel] = name
        return plain[rel]

    for s in slides.values():
        if s.get("shot"):
            s["shot"] = plain_copy(s["shot"], s.get("vw"))
    comp = res.get("completion") or {}
    for k in ("panel_shot", "final_shot"):
        if comp.get(k):
            comp[k] = plain_copy(comp[k], 1366)
    from beta import wording
    wording.apply(res["findings"])
    # one row per problem: the same message on the same screen about the same thing is reported once
    seen, kept = {}, []
    for f in res["findings"]:
        key = (f.get("check"), f.get("slide_id"), f.get("message"), (f.get("detail") or "").split("\n")[0], f.get("mobile"))
        if key in seen:
            g = seen[key]
            if not g.get("box") and f.get("box"):
                g.update(box=f["box"], screenshot=f.get("screenshot") or g.get("screenshot"), vw=f.get("vw") or g.get("vw"))
            if f.get("severity") == "error":
                g["severity"] = "error"
            continue
        seen[key] = f
        kept.append(f)
    res["findings"] = _group(kept, slides)
    shot_boxes = res.get("shot_boxes") or {}
    ids = sorted(slides, key=len, reverse=True)

    def owner(rel):
        """Which slide a screenshot shows (screenshots are named <slide id>_…)."""
        stem = Path(rel or "").stem
        return next((sid for sid in ids if stem.startswith(sid + "_")), None)

    # STRICT: an issue about a screen only ever shows that screen. Never another screen's picture.
    course_wide = next((s["shot"] for s in res.get("slides", []) if s.get("shot")), "") or comp.get("final_shot", "")
    sb_dir = run_dir / "sb"
    sb_words = json.loads((sb_dir / "words.json").read_text()) if (sb_dir / "words.json").exists() else None
    xl_dir = run_dir / "bundle_xl"
    shutil.rmtree(xl_dir, ignore_errors=True)
    xl_dir.mkdir()
    # text is called "not visible" only when every screen was watched to its end (with "play every slide to the
    # end" switched off, text that shows for a moment mid-screen may not have been pictured)
    try:
        watched = (json.loads((run_dir / "run.json").read_text()).get("options") or {}).get("play_full", True)
    except Exception:
        watched = True
    for i, f in enumerate(res["findings"]):
        sid = f.get("slide_id") or ""
        slide = slides.get(sid)
        if f.get("advice") and not sid:   # a suggestion about the whole course / storyboard: no picture to show
            f["screenshot"], f["box"] = "", None
            continue
        src_rel = f.get("screenshot") or ""
        if src_rel and not (run_dir / src_rel).exists():
            src_rel = ""
        if src_rel and sid and owner(src_rel) not in (None, sid):
            src_rel = ""   # a picture of a different screen: never use it
        if not src_rel and slide and slide.get("shot"):
            src_rel = slide["shot"]
        needle = _needle(f)
        if needle and sid and not f.get("box"):
            # a text issue: use the picture of this screen where the words are really visible
            cands = [src_rel, (slide or {}).get("shot", "")] + [r for r in shot_boxes if owner(r) == sid]
            hit = None
            for c in [c for c in cands if c]:
                tb = shot_boxes.get(c) or ((slide or {}).get("boxes") if c == (slide or {}).get("shot") or c == f.get("screenshot") else None)
                if tb and _find_text_box(tb, needle):
                    hit = c
                    break
            if hit:
                if hit != src_rel:
                    f["screenshot"] = src_rel = hit
            elif f.get("check") in ("grammar", "spelling") and slide and slide.get("shot") and res.get("stage") != "sb" and watched:
                # the words are only in the course files, never visible to a learner: not reported (team rule)
                f["_hidden"] = True
                continue
            elif f.get("check") in ("transcript",) and "not seen on screen" not in (f.get("detail") or ""):
                f["detail"] = ((f.get("detail") or "") + "\nThis text was not seen on screen during testing: it is in a part of the screen that opens on a click (layer), or it is hidden.").strip()
        name = f"bundle/f{i:03d}.jpg"
        sb_img = None
        if res.get("stage") == "sb" and sb_words:
            # SB stage: the evidence is the SB page itself, with the words the issue is about outlined
            got = _sb_evidence(run_dir, sb_words, f, name, tag=f.get("tag", ""))
            if got:
                f["screenshot"], f["box"] = got, None
                continue
        if f.get("sb_only") and sb_words:
            # the mistake is in the storyboard itself: only the SB page is shown, no course screenshot
            got = _sb_evidence(run_dir, sb_words, f, name, tag=f.get("tag", ""))
            if got:
                f["screenshot"], f["box"] = got, None
                continue
        if f.get("check") == "content" and sb_words:
            sb_img = _sb_evidence(run_dir, sb_words, f, f"bundle/f{i:03d}_sb.jpg")
        if not src_rel and sid:
            # the tester could not open this screen: say so plainly instead of borrowing a picture
            _placeholder(run_dir / name, (slide or {}).get("label") or f.get("slide_label") or "This screen")
            f["screenshot"], f["box"] = name, None
        elif not src_rel and not sid and sb_img:
            f["screenshot"], f["box"] = sb_img, None   # e.g. an SB screen missing from the course
            sb_img = None
        else:
            src_rel = src_rel or course_wide
            if not src_rel:
                continue
            src_is_bundle = src_rel.startswith("bundle/")
            vw = f.get("vw") or (slide or {}).get("vw") or 1366
            if src_is_bundle and src_rel != (f.get("screenshot") or ""):
                vw = 1366  # slide images are already saved at the desktop layout
            box = _locate(f, slide, shot_boxes, f.get("screenshot") or "")
            more = f.get("boxes")
            nd = _needle(f)
            if nd and not f.get("box"):
                tb = shot_boxes.get(f.get("screenshot") or "") or ((slide or {}).get("boxes") or {})
                lines = _line_boxes(tb, nd)
                if len(lines) > 1:   # the quote wraps: mark it line by line
                    box, more = lines[0], lines[1:]
            _render(run_dir / src_rel, run_dir / name, box, vw, f.get("tag", ""), more=more)
            f["screenshot"], f["box"] = name, box
        if sb_img:
            f["sb_screenshot"] = sb_img
            xl = f"bundle_xl/f{i:03d}.jpg"
            _side_by_side(run_dir / f["screenshot"], run_dir / sb_img, run_dir / xl,
                          "COURSE – " + ((slide or {}).get("label") or f.get("slide_label") or ""), "STORYBOARD (SB)")
            f["xl_screenshot"] = xl
    hidden = [f for f in res["findings"] if f.pop("_hidden", False)]
    if hidden:
        res["findings"] = [f for f in res["findings"] if f not in hidden]
        res["hidden_text_dropped"] = len(hidden)
    from beta.qc_report import feedback_text
    for f in res["findings"]:
        f["feedback"] = feedback_text(f)
    _save(run_dir, res)
    names = {f"bundle/{p.name}": p.name for p in sorted(out.glob("*.jpg"))}
    (out / "names.json").write_text(json.dumps(names, indent=1))
    print(json.dumps({"count": len(names), "files": [str(out / n) for n in names.values()]}, indent=1))


def cmd_report(a):
    """Builds the QC Review workbook and splits it into base64 parts for upload."""
    import base64
    from beta import qc_report
    run_dir, res = _paths(a)
    meta = json.loads((run_dir / "run.json").read_text())
    stem = re.sub(r"[^\w.-]+", "_", Path(meta.get("name", "course")).stem)
    out = run_dir / f"QC_Review_{stem}.xlsx"
    qc_report.build(res, meta, run_dir, out)
    data = out.read_bytes()
    parts = []
    for i in range(0, max(len(data), 1), 14 * 1024 * 1024):
        pp = run_dir / f"report_part{len(parts) + 1}.txt"
        pp.write_text(base64.b64encode(data[i:i + 14 * 1024 * 1024]).decode())
        parts.append(str(pp))
    _phase(a, "upload")
    print(json.dumps({"xlsx": str(out), "report_name": out.name, "size": len(data), "parts": parts}, indent=1))


def _read_upload_map(path, bundle_dir, needed):
    """Which asset id each uploaded picture got. Accepts the upload tool's own result text, pasted as it is
    (lines like  "…/f015.jpg" (143366 bytes, image/jpeg) → "/_blob/…" (id 9ff4…)), or a JSON map {"f015.jpg": id}.
    The byte counts are checked against the files here, so a picture can never be attached to the wrong issue."""
    txt = path.read_text(encoding="utf-8", errors="replace")
    ids, sizes, problems = {}, {}, []
    for m in re.finditer(r'"([^"\n]+?\.(?:jpe?g|png))"\s*\((\d+) bytes[^)]*\)\s*(?:→|->)\s*"[^"]*"\s*\(id ([0-9a-f]{16,64})\)', txt):
        p, size, aid = m.groups()
        name = Path(p).name
        if name in ids and ids[name] != aid:
            problems.append(f"{name} has two different asset ids")
        ids[name], sizes[name] = aid, int(size)
    if not ids:
        try:
            got = json.loads(txt)
            ids = {str(k): str(v) for k, v in got.items()} if isinstance(got, dict) else {}
        except Exception:
            ids = {}
    for name, size in sizes.items():
        local = bundle_dir / name
        if local.exists() and local.stat().st_size != size:
            problems.append(f"{name}: uploaded {size} bytes but the file here has {local.stat().st_size} bytes")
    seen = {}
    for name, aid in ids.items():
        if aid in seen and seen[aid] != name:
            problems.append(f"{name} and {seen[aid]} point to the same asset id")
        seen[aid] = name
    missing = sorted(n for n in needed if n not in ids)
    if missing:
        problems.append("no asset id for " + ", ".join(missing[:10]) + (" …" if len(missing) > 10 else ""))
    if problems:
        raise SystemExit("Screenshot upload list does not match the pictures: " + "; ".join(problems[:8])
                         + ". Upload the pictures again and pass the upload results exactly as the tool printed them.")
    return ids


def cmd_finalize(a):
    run_dir, res = _paths(a)
    names = json.loads((run_dir / "bundle" / "names.json").read_text())
    ids = _read_upload_map(Path(a.map), run_dir / "bundle", set(names.values()))

    def conv(r):
        n = names.get(r)
        return ids.get(n, "") if n else ""
    for f in res["findings"]:
        if f.get("screenshot"):
            f["screenshot"] = conv(f["screenshot"])
        if f.get("sb_screenshot"):
            f["sb_screenshot"] = conv(f["sb_screenshot"])
        f.pop("xl_screenshot", None)
    for s in res.get("slides", []):
        if s.get("shot"):
            s["shot"] = conv(s["shot"])
        s.pop("boxes", None)
    for f in res["findings"]:
        f.pop("box", None)
        f.pop("locate", None)
    res.pop("shot_boxes", None)
    comp = res.get("completion") or {}
    for k in ("panel_shot", "final_shot"):
        if comp.get(k):
            comp[k] = conv(comp[k])
    res["finished_at"] = time.time()
    _save(run_dir, res)
    out = run_dir / "results_upload.json"
    out.write_text(json.dumps(res, ensure_ascii=False))
    s = res["summary"]
    print(json.dumps({"results_file": str(out),
                      "totals": {"errors": sum(v["errors"] for v in s.values()),
                                 "warnings": sum(v["warnings"] for v in s.values())},
                      "summary": {k: v["status"] for k, v in s.items()},
                      "course_title": res.get("course_title", ""), "slide_count": res.get("slide_count", 0)},
                     indent=1))


CKPT_SKIP_DIRS = {"content", "bundle", "bundle_xl", "xl_thumbs", "parts", "__pycache__"}


def cmd_checkpoint_save(a):
    """Packs the finished work of a run (browser checks, tasks, answers so far, pictures) so a later session can
    continue from here. Screenshots are stored as compact JPEGs (same names). Prints the base64 part files."""
    import base64
    import io
    import zipfile
    from PIL import Image
    run_dir = Path(a.work).resolve() / "runs" / a.id
    meta = json.loads((run_dir / "run.json").read_text())
    skip_files = {meta.get("file", ""), "QC_Review.xlsx"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(run_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(run_dir)
            if rel.parts[0] in CKPT_SKIP_DIRS or p.name in skip_files or p.name.startswith(("report_part", "ckpt_part", "QC_Review_")):
                continue
            if rel.parts[0] == "shots" and p.suffix.lower() == ".png":
                try:   # same file name, JPEG inside: the pictures are only read with PIL, which goes by content
                    im = Image.open(p).convert("RGB")
                    b = io.BytesIO()
                    im.save(b, "JPEG", quality=82)
                    z.writestr(rel.as_posix(), b.getvalue())
                    continue
                except Exception:
                    pass
            if p.stat().st_size > 60 * 1024 * 1024:
                continue   # never the course or storyboard itself
            z.write(p, rel.as_posix())
    data = buf.getvalue()
    for old in run_dir.glob("ckpt_part*.txt"):
        old.unlink()
    parts = []
    for i in range(0, max(len(data), 1), 14 * 1024 * 1024):
        pp = run_dir / f"ckpt_part{len(parts) + 1}.txt"
        pp.write_text(base64.b64encode(data[i:i + 14 * 1024 * 1024]).decode())
        parts.append(str(pp))
    print(json.dumps({"size": len(data), "parts": parts}, indent=1))


def cmd_checkpoint_restore(a):
    """Unpacks a checkpoint (its base64 part files, in order) into the run folder."""
    import base64
    import io
    import zipfile
    run_dir = Path(a.work).resolve() / "runs" / a.id
    run_dir.mkdir(parents=True, exist_ok=True)
    data = b"".join(base64.b64decode(Path(p).read_text()) for p in a.parts)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            if n.startswith("/") or ".." in Path(n).parts:
                continue
            z.extract(n, run_dir)
    tasks = run_dir / "claude_tasks.json"
    partial = run_dir / "answers_partial.json"
    print(json.dumps({"restored": True, "files": len(list(run_dir.rglob("*"))), "claude_tasks": str(tasks) if tasks.exists() else None,
                      "answers_partial": str(partial) if partial.exists() else None}, indent=1))


def _phase(a, name):
    try:
        from beta.progress import Progress
        run_dir = Path(a.work).resolve() / "runs" / a.id
        if run_dir.exists():
            Progress(run_dir).phase(name)
    except Exception:
        pass


def cmd_progress(a):
    """Progress for the website: {pct, eta_s, stage, progress, log}. --step moves to a phase first."""
    from beta.progress import Progress
    run_dir = Path(a.work).resolve() / "runs" / a.id
    run_dir.mkdir(parents=True, exist_ok=True)
    pr = Progress(run_dir)
    if a.step:
        if a.step == "done":
            pr.finish()
        else:
            pr.phase(a.step)
    v = pr.view()
    lines = []
    lf = run_dir / "log.txt"
    if lf.exists():
        lines = [l.rstrip() for l in lf.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()][-12:]
    v["progress"] = v["stage"] if a.step or not lines else lines[-1].split("  ", 1)[-1]
    v["log"] = lines
    print(json.dumps(v, ensure_ascii=False))
    return 0


def cmd_check_env(a):
    """Stops a run before it starts if any tool differs from the frozen release."""
    from beta import release
    here = Path(__file__).resolve().parent
    manifest = json.loads(Path(a.manifest).read_text()) if a.manifest else None
    r = release.check(here, manifest)
    print(json.dumps(r, indent=1))
    return 0 if r["ok"] else 3


def main(argv=None):
    p = argparse.ArgumentParser(prog="beta.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    for k in ("--work", "--id", "--upload", "--kind"):
        r.add_argument(k, required=True)
    for k in ("--captions", "--options", "--glossary", "--name", "--sb", "--styleguide"):
        r.add_argument(k)
    for name, extra in (("merge", "--answers"), ("bundle", None), ("report", None), ("finalize", "--map")):
        q = sub.add_parser(name)
        q.add_argument("--work", required=True)
        q.add_argument("--id", required=True)
        if extra:
            q.add_argument(extra, required=True)
    q = sub.add_parser("progress")
    q.add_argument("--work", required=True)
    q.add_argument("--id", required=True)
    q.add_argument("--step", choices=["prepare", "review", "report", "upload", "done"])
    q = sub.add_parser("check-env")
    q.add_argument("--manifest")
    q = sub.add_parser("checkpoint-save")
    q.add_argument("--work", required=True)
    q.add_argument("--id", required=True)
    q = sub.add_parser("checkpoint-restore")
    q.add_argument("--work", required=True)
    q.add_argument("--id", required=True)
    q.add_argument("--parts", nargs="+", required=True)
    a = p.parse_args(argv)
    return {"run": cmd_run, "progress": cmd_progress, "check-env": cmd_check_env, "merge": cmd_merge, "bundle": cmd_bundle,
            "report": cmd_report, "finalize": cmd_finalize, "checkpoint-save": cmd_checkpoint_save,
            "checkpoint-restore": cmd_checkpoint_restore}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())
