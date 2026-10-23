"""QC360 AI on your own PC: every browser check runs here, offline; Claude is used only for the content review.

    python -m beta.local setup                         check that the tools are installed
    python -m beta.local test COURSE.zip [--sb SB.pptx] [--stage beta|alpha|gold|sb] [--captions FILE]
    python -m beta.local finish REPORT_FOLDER          after a manual content review (answers.json saved there)

Content review:
  * with an Anthropic API key (set ANTHROPIC_API_KEY) it runs by itself, in a few minutes;
  * without one, the tester writes REVIEW_REQUEST.md into the report folder: attach it in a Claude chat
    (claude.ai), save Claude's reply as answers.json in the same folder, then run `finish`.

Every report folder gets the QC Review Excel workbook and SITE_IMPORT_<name>.zip, which the QC360 AI website
imports with "Import PC report" so the whole team sees the result.
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

HOME = Path(os.environ.get("EQC_HOME") or (Path.home() / "eQC360"))
WORK = HOME / "work"
REPORTS = HOME / "reports"
RULES = Path(__file__).with_name("review_rules.md")
DOC_EXT = {".doc", ".docx", ".ppt", ".pptx", ".pdf"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
KEYS = ("proofread", "script_compare", "transcript_review", "sb_compare", "sb_review", "logic_check", "assessment_review",
        "style_guide")


# ------------------------------------------------------------------------------------------------ set-up
def _utf8():
    """Windows: the engine reads and writes UTF-8 text (Hindi, curly quotes, ™ …): run Python in UTF-8 mode."""
    if os.name == "nt" and not sys.flags.utf8_mode:
        sys.exit(subprocess.call([sys.executable, "-X", "utf8", "-m", "beta.local", *sys.argv[1:]]))


def _tool_dirs():
    pats = []
    if os.name == "nt":
        pf = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)") if os.environ.get(k)]
        lad = os.environ.get("LOCALAPPDATA", "")
        for p in pf:
            pats += [rf"{p}\LibreOffice\program", rf"{p}\ffmpeg\bin", rf"{p}\poppler*\Library\bin", rf"{p}\poppler*\bin"]
        pats += [r"C:\ffmpeg\bin", r"C:\ffmpeg*\bin", r"C:\poppler*\Library\bin", r"C:\poppler*\bin"]
        if lad:
            pats += [rf"{lad}\Microsoft\WinGet\Packages\Gyan.FFmpeg*\ffmpeg-*\bin",
                     rf"{lad}\Microsoft\WinGet\Packages\oschwartz10612.Poppler*\poppler-*\Library\bin",
                     rf"{lad}\Microsoft\WinGet\Links"]
    else:
        pats += ["/opt/homebrew/bin", "/usr/local/bin", "/Applications/LibreOffice.app/Contents/MacOS"]
    extra = [p for p in (os.environ.get("EQC_TOOLS") or "").split(os.pathsep) if p]
    found = []
    for pat in extra + pats:
        found += [d for d in glob.glob(pat) if os.path.isdir(d)]
    return found


def _tools():
    """Adds the usual install folders to PATH. Returns {tool: found?}."""
    dirs = _tool_dirs()
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])
    return {t: bool(shutil.which(t)) for t in ("ffmpeg", "ffprobe", "soffice", "pdftotext", "pdftoppm")}


NEEDS = {"ffmpeg": "videos and narration timing", "ffprobe": "videos", "soffice": "storyboards (Word / PowerPoint)",
         "pdftotext": "storyboards", "pdftoppm": "storyboard page pictures"}
HOW = {"ffmpeg": "winget install Gyan.FFmpeg", "ffprobe": "winget install Gyan.FFmpeg",
       "soffice": "winget install TheDocumentFoundation.LibreOffice",
       "pdftotext": "winget install oschwartz10612.Poppler", "pdftoppm": "winget install oschwartz10612.Poppler"}


def cmd_setup(a):
    have = _tools()
    from beta import release
    name, ver = release.browser()
    print(f"QC360 AI {release.RELEASE} – checking this PC\n")
    print(f"  Browser       : {'Google Chrome' if name == 'chrome' else 'Chromium (Google Chrome not found)'} {ver}")
    ok = True
    for t, got in have.items():
        print(f"  {t:<13} : {'found' if got else 'MISSING – needed for ' + NEEDS[t] + '   (install: ' + HOW[t] + ')'}")
        ok = ok and got
    key = bool(_key())
    print(f"  Review key: {'set – the content review runs by itself' if key else 'not set – the content review is finished in the review chat (see README_PC)'}")
    print(f"\n  Reports are saved in {REPORTS}")
    print("\nAll set." if ok else "\nInstall the missing tools above, close this window, open a new one and run setup again.")
    return 0


# ------------------------------------------------------------------------------------------------ test
def _options(stage, kind, a):
    """The same choices the website makes for each stage."""
    o = {"play_full": not a.quick, "swipe": not a.no_swipe, "buttons": True, "transcript": True, "stage": stage}
    if stage == "alpha":
        o["swipe"] = False
        o["english"] = a.english
    if stage == "beta" and kind == "scorm":
        o["expect"] = {"menu": a.menu, "seekbar": a.seekbar, "next": a.next}
    if stage == "gold":
        o = {"stage": "gold", "play_full": True, "swipe": False, "buttons": False, "transcript": False}
        if kind == "scorm":
            o["expect"] = {"menu": a.menu, "seekbar": a.seekbar, "next": a.next}
    if kind == "sb":
        o = {"stage": "sb", "english": a.english, "requirements": (a.requirements or "")[:2000]}
    return o


def _tail(log, stop):
    """Prints the tester's progress lines while it works."""
    pos = 0
    while not stop.is_set():
        try:
            if log.exists():
                with open(log, encoding="utf-8", errors="replace") as fh:
                    fh.seek(pos)
                    for line in fh.read().splitlines():
                        if line.strip():
                            print("  " + line.split("  ", 1)[-1][:150], flush=True)
                    pos = fh.tell()
        except Exception:
            pass
        stop.wait(2)


def cmd_test(a):
    have = _tools()
    # the files may come in any order (dragged together onto test.bat): course = zip / video, SB = document
    files = [Path(x).expanduser().resolve() for x in a.files]
    for x in files:
        if not x.exists():
            raise SystemExit(f"File not found: {x}")
    # a file with "style" in its name is the style guide (Word, PDF, PowerPoint or a picture)
    guides = [x for x in files if re.search(r"style", x.stem, re.I) and x.suffix.lower() in DOC_EXT | IMG_EXT]
    if guides and not a.styleguide:
        a.styleguide = str(guides[0])
    files = [x for x in files if x not in guides]
    docs = [x for x in files if x.suffix.lower() in DOC_EXT]
    others = [x for x in files if x.suffix.lower() not in DOC_EXT | IMG_EXT]
    if a.stage == "sb":
        if not docs:
            raise SystemExit("The SB stage takes a storyboard (.doc, .docx, .ppt, .pptx).")
        up = docs[0]
    else:
        if not others:
            raise SystemExit("Give the course zip (or video) to test.")
        up = others[0]
        if docs and not a.sb:
            a.sb = str(docs[0])
    kind = "sb" if a.stage == "sb" else ("scorm" if up.suffix.lower() == ".zip" else "video")
    if kind == "video" and not have["ffmpeg"]:
        raise SystemExit("Videos need ffmpeg. Install it (winget install Gyan.FFmpeg) and run again.")
    sb = Path(a.sb).expanduser().resolve() if a.sb else None
    if (sb or kind == "sb") and not (have["soffice"] and have["pdftotext"]):
        raise SystemExit("Storyboards need LibreOffice and Poppler. Run `python -m beta.local setup` to see what is missing.")
    if a.stage == "alpha" and not sb:
        raise SystemExit("Alpha review needs the storyboard: add --sb <storyboard file>.")
    if a.stage == "gold":
        sb = None
    run_id = "pc" + time.strftime("%Y%m%d%H%M%S")
    stem = re.sub(r"[^\w.-]+", "_", up.stem)[:60]
    out = REPORTS / f"{stem}_{time.strftime('%Y-%m-%d_%H%M')}"
    out.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    opts = _options(a.stage, kind, a)
    state = {"work": str(WORK), "id": run_id, "name": up.name, "kind": kind, "options": opts,
             "sb_name": sb.name if sb else "", "started": time.time()}
    (out / "state.json").write_text(json.dumps(state, indent=1))
    from beta import release
    bname, bver = release.browser()
    print(f"QC360 AI {release.RELEASE} on this PC – testing {up.name} ({a.stage.upper()})")
    print(f"Browser: {'Google Chrome' if bname == 'chrome' else 'Chromium'} {bver}.  Report folder: {out}\n")
    run_dir = WORK / "runs" / run_id
    stop = threading.Event()
    threading.Thread(target=_tail, args=(run_dir / "log.txt", stop), daemon=True).start()
    from beta import cli
    args = ["run", "--work", str(WORK), "--id", run_id, "--upload", str(up), "--kind", kind, "--name", up.name,
            "--options", json.dumps(opts), "--glossary", a.glossary or ""]
    if sb:
        args += ["--sb", str(sb)]
    if a.styleguide:
        args += ["--styleguide", str(Path(a.styleguide).expanduser().resolve())]
    if a.captions:
        args += ["--captions", str(Path(a.captions).expanduser().resolve())]
    t0 = time.time()
    try:
        code = cli.main(args)
    finally:
        stop.set()
        time.sleep(0.2)
    print(f"\nBrowser checks finished in {int((time.time() - t0) / 60)} min.")
    if code and not (run_dir / "results.json").exists():
        raise SystemExit("The test did not finish – see the lines above.")
    return _review(out, state)


# ------------------------------------------------------------------------------------------------ review
def _request(state):
    """The content review as one self-contained text: rules + data (no local file paths)."""
    run_dir = Path(state["work"]) / "runs" / state["id"]
    tf = run_dir / "claude_tasks.json"
    tasks = json.loads(tf.read_text()) if tf.exists() else {}
    files = {}

    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if k not in ("shot", "cap_shots", "boxes", "start_shot")}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x
    sbc = tasks.get("sb_compare") or {}
    if sbc.get("storyboard_text_file") and Path(sbc["storyboard_text_file"]).exists():
        files["STORYBOARD TEXT"] = Path(sbc["storyboard_text_file"]).read_text(errors="replace")
        sbc.pop("storyboard_text_file", None)
    sbr = tasks.get("sb_review") or {}
    for key, title in (("pages_file", "SB PAGES"), ("formatting_file", "SB PAGES WITH FORMATTING MARKS")):
        if sbr.get(key) and Path(sbr[key]).exists():
            files[title] = Path(sbr[key]).read_text(errors="replace")
            sbr.pop(key, None)
    sg = tasks.get("style_guide") or {}
    if sg.get("text_file") and Path(sg["text_file"]).exists():
        files["STYLE GUIDE"] = Path(sg["text_file"]).read_text(errors="replace")
        sg.pop("text_file", None)
    pics = []
    for n in (sg.get("pictures") or [])[:15]:
        p = Path(sg.get("pictures_dir", "")) / n
        if p.exists():
            pics.append((f"style guide picture {n}", p))
    sg.pop("pictures_dir", None)
    if sbr.get("page_pictures"):
        for k in (sbr.get("pages_with_pictures") or [])[:60]:
            p = Path(sbr["page_pictures"]) / f"page{k}.png"
            if p.exists():
                pics.append((k, p))
        sbr.pop("page_pictures", None)
    for it in (tasks.get("logic_check") or [])[:80]:   # Alpha: the screenshot of every screen (images and layout)
        p = run_dir / it.get("shot", "") if it.get("shot") else None
        if p and p.exists():
            pics.append((f"screen {it.get('label', '')} (slide_id {it.get('slide_id', '')})", p))
    has_work = any(tasks.get(k) for k in KEYS)
    text = RULES.read_text() + "\n\n# TASKS (JSON)\n\n```json\n" + json.dumps(strip(tasks), ensure_ascii=False, indent=1) + "\n```\n"
    for title, body in files.items():
        text += f"\n\n# {title}\n\n{body}\n"
    return text, pics, has_work


def _key():
    """The review key: QC360_REVIEW_KEY (or the older ANTHROPIC_API_KEY)."""
    return os.environ.get("QC360_REVIEW_KEY") or os.environ.get("ANTHROPIC_API_KEY") or ""


def _pictures_pdf(pics, path):
    """For a review in a Claude chat: every picture of the review on its own labelled page of one PDF."""
    if not pics:
        return None
    from PIL import Image, ImageDraw
    pages = []
    for k, p in pics[:100]:
        try:
            im = Image.open(p).convert("RGB")
        except Exception:
            continue
        im.thumbnail((1600, 1600))
        page = Image.new("RGB", (im.width, im.height + 40), "white")
        page.paste(im, (0, 40))
        ImageDraw.Draw(page).text((10, 12), f"Picture of SB page {k}" if isinstance(k, int) else f"Picture: {k}", fill="black")
        pages.append(page)
    if not pages:
        return None
    pages[0].save(path, save_all=True, append_images=pages[1:])
    return path


def _extract_json(txt):
    txt = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", txt.strip())
    a, b = txt.find("{"), txt.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no JSON object found")
    got = json.loads(txt[a:b + 1])
    if not isinstance(got, dict):
        raise ValueError("the reply is not a JSON object")
    return {k: (got.get(k) if isinstance(got.get(k), list) else []) for k in KEYS}


def _api_review(text, pics):
    import requests
    from beta import config, release
    model = os.environ.get("EQC_REVIEW_MODEL") or release.MODEL
    content = [{"type": "text", "text": text}]
    import base64
    for k, p in pics:
        content += [{"type": "text", "text": (f"Picture of SB page {k}:" if isinstance(k, int) else f"Picture: {k}:")},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": base64.b64encode(p.read_bytes()).decode()}}]
    body = {"model": model, "max_tokens": 32000, "messages": [{"role": "user", "content": content}],
            "system": "You are the content reviewer of an eLearning QA team. Reply with the JSON object only."}
    headers = {"x-api-key": _key(), "anthropic-version": "2023-06-01", "content-type": "application/json"}
    last = None
    for attempt in range(3):
        try:
            r = requests.post(config.ANTHROPIC_API_URL, headers=headers, json=body, timeout=900)
            if r.status_code in (429, 500, 502, 503, 529):
                last = f"HTTP {r.status_code}"
                time.sleep(20 * (attempt + 1))
                continue
            r.raise_for_status()
            reply = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
            return _extract_json(reply), model
        except (requests.RequestException, ValueError) as e:
            last = str(e)[:200]
            time.sleep(10)
    raise RuntimeError(f"The automatic review did not work ({last}).")


def _review(out, state):
    text, pics, has_work = _request(state)
    ans = out / "answers.json"
    if not has_work:
        ans.write_text(json.dumps({k: [] for k in KEYS}))
        return _finish(out, state)
    if _key():
        print("Content review (automatic)…")
        try:
            answers, model = _api_review(text, pics)
            ans.write_text(json.dumps(answers, ensure_ascii=False, indent=1))
            state["reviewer"] = "AI review (automatic)"
            (out / "state.json").write_text(json.dumps(state, indent=1))
            return _finish(out, state)
        except Exception as e:
            print(f"  {e} – finish the review in the review chat instead.")
    req = out / "REVIEW_REQUEST.md"
    req.write_text(text)
    pdf = _pictures_pdf(pics, out / "REVIEW_PICTURES.pdf")
    also = f"\n     and also attach:  {pdf}  (the pictures the review looks at)" if pdf else ""
    print(f"""
Content review – one step for you:
  1. Open the review chat (claude.ai) and start a new chat.
  2. Attach this file:  {req}{also}
     and type:  Follow the instructions in this file and give me the JSON as a downloadable answers.json file.
  3. Save the answers.json it gives you into the same folder:  {out}
  4. Run:  python -m beta.local finish "{out}"
     (or double-click finish.bat and drop the folder on it)
""")
    return 0


# ------------------------------------------------------------------------------------------------ finish
def cmd_finish(a):
    _tools()
    if a.folder:
        out = Path(a.folder).expanduser().resolve()
        if out.is_file():
            out = out.parent
    else:
        waiting = sorted((p for p in REPORTS.glob("*/state.json") if not list(p.parent.glob("SITE_IMPORT_*.zip"))),
                         key=lambda p: p.stat().st_mtime)
        if not waiting:
            raise SystemExit(f"No report folder in {REPORTS} is waiting for its content review.")
        out = waiting[-1].parent
    sf = out / "state.json"
    if not sf.exists():
        raise SystemExit(f"{out} is not a QC360 AI report folder (no state.json).")
    state = json.loads(sf.read_text())
    ans = out / "answers.json"
    if not ans.exists():
        cands = sorted(out.glob("answers*.json")) + sorted(out.glob("*.json.txt"))
        if not cands:
            raise SystemExit(f"Save the review reply as answers.json in {out} first.")
        ans = cands[0]
    try:
        clean = _extract_json(ans.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        raise SystemExit(f"answers.json could not be read ({e}). Save the JSON reply exactly as it was given.")
    (out / "answers.json").write_text(json.dumps(clean, ensure_ascii=False, indent=1))
    state.setdefault("reviewer", "AI review (chat)")
    sf.write_text(json.dumps(state, indent=1))
    return _finish(out, state)


def _finish(out, state):
    from beta import cli, release
    work, rid = state["work"], state["id"]
    run_dir = Path(work) / "runs" / rid
    print("Building screenshots and the QC report…")
    cli.main(["merge", "--work", work, "--id", rid, "--answers", str(out / "answers.json")])
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        cli.main(["bundle", "--work", work, "--id", rid])
        cli.main(["report", "--work", work, "--id", rid])
    res = json.loads((run_dir / "results.json").read_text())
    xlsx = next(run_dir.glob("QC_Review_*.xlsx"))
    shutil.copy(xlsx, out / xlsx.name)
    s = res.get("summary") or {}
    totals = {"errors": sum(v["errors"] for v in s.values()), "warnings": sum(v["warnings"] for v in s.values())}
    bname, bver = release.browser()
    meta = {"format": "eqc360-pc-report/1", "name": state["name"], "kind": state["kind"], "options": state["options"],
            "sb_name": state.get("sb_name", ""), "release": release.RELEASE, "browser": f"{bname} {bver}",
            "reviewer": state.get("reviewer", ""), "report_name": xlsx.name, "totals": totals,
            "summary": {k: v["status"] for k, v in s.items()}, "course_title": res.get("course_title", ""),
            "slide_count": res.get("slide_count", 0), "started": state.get("started"), "finished": time.time()}
    pkg = out / f"SITE_IMPORT_{Path(state['name']).stem[:60]}.zip"
    with zipfile.ZipFile(pkg, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=1))
        z.writestr("results.json", json.dumps(res, ensure_ascii=False))
        for p in sorted((run_dir / "bundle").glob("*.jpg")):
            z.write(p, f"bundle/{p.name}")
        z.write(xlsx, xlsx.name)
    print(f"""
Done: {totals['errors']} errors, {totals['warnings']} warnings.
  QC report : {out / xlsx.name}
  For the website (Import PC report): {pkg}
""")
    if os.name == "nt" and not os.environ.get("EQC_NO_OPEN"):
        try:
            os.startfile(str(out))
        except Exception:
            pass
    return 0


def main(argv=None):
    _utf8()
    p = argparse.ArgumentParser(prog="python -m beta.local", description="QC360 AI on this PC")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    t = sub.add_parser("test")
    t.add_argument("files", nargs="+", help="SCORM zip or video, and optionally its storyboard (or, with --stage sb, a storyboard)")
    t.add_argument("--sb", help="storyboard (Word / PowerPoint / PDF)")
    t.add_argument("--styleguide", help="style guide for SB / Alpha (Word, PDF, PowerPoint or a picture)")
    t.add_argument("--captions", help="captions file for a video (.srt / .vtt)")
    t.add_argument("--stage", default="beta", choices=["beta", "alpha", "gold", "sb"])
    t.add_argument("--english", default="us", choices=["us", "uk"])
    t.add_argument("--menu", default="restricted", choices=["restricted", "free", "skip"])
    t.add_argument("--next", default="restricted", choices=["restricted", "free", "skip"])
    t.add_argument("--seekbar", default="restricted", choices=["restricted", "free", "skip"])
    t.add_argument("--no-swipe", action="store_true", help="skip the phone swipe checks")
    t.add_argument("--quick", action="store_true", help="do not play every screen to its end")
    t.add_argument("--glossary", default="", help="words that are always right, separated by commas")
    t.add_argument("--requirements", default="", help="SB stage: the course's style notes")
    f = sub.add_parser("finish")
    f.add_argument("folder", nargs="?", help="the report folder (default: the newest one waiting for its review)")
    a = p.parse_args(argv)
    return {"setup": cmd_setup, "test": cmd_test, "finish": cmd_finish}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())
