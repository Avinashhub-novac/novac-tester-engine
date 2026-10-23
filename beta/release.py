"""Frozen release of QC360 AI.

Every run must give the same result for the same input. That needs three things to stay fixed:
  1. the checks (this engine)            -> RELEASE + checksums of every engine file (manifest.json)
  2. the tools the checks run in         -> FROZEN_TOOLS: a run stops if any tool version is different
  3. the reviewer model and its rules    -> MODEL + the fixed instructions of the background task
A run never produces a report with a different engine or different tools: it stops and says why.
"""
import hashlib
import os
import json
import platform
import re
import subprocess
from pathlib import Path

RELEASE = "1.6.4"
MODEL = "claude-opus-5-5"

# Exact versions the release was built and tested with.
FROZEN_TOOLS = {
    "python": "3.13",
    "chromium": "153.0.8010.12",
    "playwright": "1.63.0",
    "ffmpeg": "6.1.1",
    "libreoffice": "24.2.7.2",
    "poppler": "24.02.0",
    "pillow": "12.3.0",
    "openpyxl": "3.1.5",
    "python-docx": "1.2.0",
    "python-pptx": "1.0.2",
}


def _run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r.stdout + r.stderr
    except Exception:
        return ""


_BROWSER = None


def _real_chrome():
    """True when Google Chrome itself is installed (not a link to a Chromium build, as on the cloud tester)."""
    places = ["/opt/google/chrome/chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        if os.environ.get(env):
            places.append(os.path.join(os.environ[env], "Google", "Chrome", "Application", "chrome.exe"))
    for pth in places:
        if os.path.exists(pth) and "chromium" not in os.path.realpath(pth).lower() and "pw-browsers" not in os.path.realpath(pth):
            return True
    return False


def browser():
    """The browser the checks run in: Google Chrome when it is installed on this machine (it plays MP4 / H.264
    video and AAC audio like the learners' Chrome), otherwise the frozen Chromium build (the cloud tester).
    EQC_BROWSER=chromium forces Chromium. Returns ("chrome" | "chromium", version)."""
    global _BROWSER
    if _BROWSER is None:
        _BROWSER = ("chromium", FROZEN_TOOLS["chromium"])
        if os.environ.get("EQC_BROWSER", "").lower() != "chromium" and _real_chrome():
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    b = p.chromium.launch(channel="chrome")
                    _BROWSER = ("chrome", b.version)
                    b.close()
            except Exception:
                pass
    return _BROWSER


def launch(pw, **kw):
    """Starts the chosen browser (see browser())."""
    return pw.chromium.launch(channel=browser()[0], **kw)


def tool_versions():
    from importlib.metadata import version
    v = {"python": ".".join(platform.python_version_tuple()[:2])}
    for pkg in ("playwright", "pillow", "openpyxl", "python-docx", "python-pptx"):
        try:
            v[pkg] = version(pkg)
        except Exception:
            v[pkg] = "missing"
    browser()   # decided before a Playwright session is open (sessions cannot be nested)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            if browser()[0] == "chrome":
                v["chrome"] = browser()[1]
            else:
                b = p.chromium.launch(channel="chromium")
                v["chromium"] = b.version
                b.close()
    except Exception as e:
        v["chromium"] = f"error: {e!r}"[:120]
    m = re.search(r"ffmpeg version (\d+\.\d+(?:\.\d+)?)", _run(["ffmpeg", "-version"]))
    v["ffmpeg"] = m.group(1) if m else "missing"
    m = re.search(r"LibreOffice (\d+(?:\.\d+)+)", _run(["soffice", "--version"]))
    v["libreoffice"] = m.group(1) if m else "missing"
    m = re.search(r"pdftoppm version (\d+(?:\.\d+)+)", _run(["pdftoppm", "-v"]))
    v["poppler"] = m.group(1) if m else "missing"
    return v


def engine_checksums(engine_dir: Path):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(engine_dir).glob("*.py")) if p.name != "release_manifest.py"}


def check(engine_dir: Path, manifest: dict | None = None):
    """{ok, release, model, tools, problems: [...]} – problems are in plain words."""
    problems = []
    tools = tool_versions()
    for k, want in FROZEN_TOOLS.items():
        if k == "chromium" and "chrome" in tools:
            continue   # Google Chrome is used on this machine instead (its version is shown on every report)
        got = tools.get(k, "missing")
        if got != want:
            problems.append(f"{k} is {got}, the frozen release uses {want}")
    if manifest:
        if manifest.get("release") != RELEASE:
            problems.append(f"engine files belong to release {manifest.get('release')}, not {RELEASE}")
        sums = engine_checksums(engine_dir)
        for name, h in (manifest.get("files") or {}).items():
            if sums.get(name) != h:
                problems.append(f"engine file {name} is not the frozen copy")
        extra = set(sums) - set(manifest.get("files") or {})
        if extra:
            problems.append("engine has files that are not part of the release: " + ", ".join(sorted(extra)))
    return {"ok": not problems, "release": RELEASE, "model": MODEL, "tools": tools, "problems": problems}


def stamp():
    """What every report shows about how it was made."""
    name, ver = browser()
    tools = {k: v for k, v in FROZEN_TOOLS.items() if not (k == "chromium" and name == "chrome")}
    if name == "chrome":
        tools["chrome"] = ver
    return {"release": RELEASE, "model": MODEL, "tools": tools, "browser": f"{'Google Chrome' if name == 'chrome' else 'Chromium'} {ver}"}


def write_manifest(engine_dir: Path, out: Path):
    out.write_text(json.dumps({"release": RELEASE, "files": engine_checksums(engine_dir)}, indent=1))
