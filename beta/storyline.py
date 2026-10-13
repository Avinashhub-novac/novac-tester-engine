"""Reads a published Articulate Storyline package (HTML5 output).

Storyline writes its course model as JavaScript files of the form
    window.globalProvideData('slide', '{...json...}');
under html5/data/js/. This module decodes them into a simple Course object.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

PROVIDE_RE = re.compile(r"globalProvideData\(\s*'(\w+)'\s*,\s*'(.*)'\s*\)\s*;?\s*$", re.S)
MEDIA_RE = re.compile(r"(?:story_content|mobile)/[\w\-./]+?\.(?:mp3|m4a|wav|ogg|aac)", re.I)
VIDEO_RE = re.compile(r"(?:story_content|mobile)/[\w\-./]+?\.(?:mp4|webm|m4v)", re.I)
CAPTION_FILE_RE = re.compile(r"[\w\-./]+?\.(?:vtt|srt)", re.I)
CLICK_EVENTS = {"onrelease", "onclick", "ontap", "onpress", "onrelease_outside"}


@dataclass
class Slide:
    id: str
    title: str
    index: int  # 1-based position in story order
    scene: str
    url: str = ""
    texts: list = field(default_factory=list)       # on-screen paragraphs
    notes: str = ""
    captions: list = field(default_factory=list)    # caption cues from the data files
    audio: list = field(default_factory=list)       # relative paths of audio files
    video: list = field(default_factory=list)
    duration_ms: int = 0
    next_target: str = ""   # slide id the Next trigger jumps to ("" = unknown)
    prev_target: str = ""
    is_quiz: bool = False
    dead_click_objects: list = field(default_factory=list)  # names of objects with click triggers but no actions
    nav_objects: list = field(default_factory=list)  # [{id, label, target}] slide buttons that jump to a slide
    raw_ok: bool = True
    object_ids: set = field(default_factory=set)   # ids of every object on the slide (shown as data-model-id)
    scene_id: str = ""
    has_interaction: bool = False   # drag-drop, quiz or other activity the learner must finish

    @property
    def abs_id(self):
        """The player's own address for the slide (used to open it directly)."""
        return f"_player.{self.scene_id}.{self.id}" if self.scene_id else self.id

    @property
    def label(self):
        return f"{self.index}. {self.title}" if self.title else f"Slide {self.index}"

    def all_text(self):
        return "\n".join([self.title] + self.texts)


@dataclass
class Course:
    root: Path
    launch_file: str
    title: str = ""
    slides: list = field(default_factory=list)
    frame: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    scene_start: dict = field(default_factory=dict)   # scene id -> its first slide id
    scorings: list = field(default_factory=list)      # quiz scoring settings (pass mark, quiz type)

    def by_id(self, sid):
        for s in self.slides:
            if s.id == sid:
                return s
        return None

    def ids(self):
        return [s.id for s in self.slides]


# ---------------------------------------------------------------- decoding
def _js_unescape(s: str) -> str:
    out, i, n = [], 0, len(s)
    simple = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
    while i < n:
        c = s[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        d = s[i + 1]
        if d in simple:
            out.append(simple[d]); i += 2
        elif d == "u" and i + 5 < n + 1:
            try:
                out.append(chr(int(s[i + 2:i + 6], 16))); i += 6
            except ValueError:
                out.append(d); i += 2
        elif d == "x":
            try:
                out.append(chr(int(s[i + 2:i + 4], 16))); i += 4
            except ValueError:
                out.append(d); i += 2
        else:  # \\ \' \" \/ and anything else
            out.append(d); i += 2
    return "".join(out)


def decode_provide_file(path: Path):
    """Return (kind, data) for a globalProvideData JS file, or (None, None)."""
    try:
        txt = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None, None
    m = PROVIDE_RE.search(txt)
    if not m:
        return None, None
    raw = _js_unescape(m.group(2))
    try:
        return m.group(1), json.loads(raw)
    except json.JSONDecodeError:
        try:
            return m.group(1), json.loads(m.group(2))
        except json.JSONDecodeError:
            return m.group(1), None


# ---------------------------------------------------------------- helpers
def _walk(o, parent_key=""):
    if isinstance(o, dict):
        yield parent_key, o
        for k, v in o.items():
            yield from _walk(v, k)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v, parent_key)


def _block_text(d):
    """Text of a Storyline vartext {'blocks': [{'spans': [{'text':..}]}]}."""
    paras = []
    for b in d.get("blocks", []) or []:
        if not isinstance(b, dict):
            continue
        t = "".join(sp.get("text", "") for sp in b.get("spans", []) if isinstance(sp, dict))
        t = re.sub(r"\s+", " ", t.replace("​", "")).strip()
        if t:
            paras.append(t)
    return paras


def _texts(slide_json):
    paras, seen = [], set()
    notes = []
    for key, d in _walk(slide_json):
        if "blocks" in d and isinstance(d["blocks"], list):
            target = notes if "note" in key.lower() else paras
            for t in _block_text(d):
                if t not in seen:
                    seen.add(t)
                    target.append(t)
    if not paras:  # fall back to accessibility text
        for _, d in _walk(slide_json):
            for k in ("altText", "accText"):
                v = d.get(k)
                if isinstance(v, str) and v.strip() and v not in seen:
                    seen.add(v)
                    paras.append(v.strip())
    for _, d in _walk(slide_json):
        v = d.get("notes")
        if isinstance(v, str) and v.strip():
            notes.append(re.sub(r"<[^>]+>", " ", v).strip())
    return paras, " ".join(notes)


def _captions(slide_json):
    cues = []
    for key, d in _walk(slide_json):
        for k, v in d.items():
            if "caption" not in k.lower():
                continue
            if isinstance(v, str) and v.strip() and not CAPTION_FILE_RE.fullmatch(v.strip()):
                cues.append(v.strip())
            elif isinstance(v, list):
                for c in v:
                    if isinstance(c, dict):
                        t = c.get("text") or c.get("caption") or ""
                        if isinstance(t, str) and t.strip():
                            cues.append(t.strip())
                        elif isinstance(c.get("blocks"), list):
                            cues.extend(_block_text(c))
    return cues


def _duration(slide_json):
    tl = slide_json.get("timeline") if isinstance(slide_json, dict) else None
    if isinstance(tl, dict) and isinstance(tl.get("duration"), (int, float)):
        return int(tl["duration"])
    best = 0
    for key, d in _walk(slide_json):
        if key == "timeline" and isinstance(d.get("duration"), (int, float)):
            best = max(best, int(d["duration"]))
    return best


def _event_target(slide_json, kinds, known_ids):
    for _, d in _walk(slide_json):
        if d.get("kind") in kinds and "actions" in d:
            blob = json.dumps(d.get("actions"))
            for sid in re.findall(r"[\w]+", blob):
                if sid in known_ids:
                    return sid
    return ""


def _plain(t):
    """Slide titles can be stored as HTML (<p dir='ltr' ...>Title</p>)."""
    import html as _html
    t = re.sub(r"<[^>]+>", " ", str(t))
    return re.sub(r"\s+", " ", _html.unescape(t)).strip()


NEXT_TOKENS = ("_nextslide", "_next", "nextslide", "next_slide")
PREV_TOKENS = ("_prevslide", "_previousslide", "_prev", "previousslide", "prev_slide")


def _nav_objects(slide_json, known_ids):
    """Objects on the slide whose click trigger jumps to another slide (custom Next/Prev/Start buttons)."""
    out = []
    for _, d in _walk(slide_json):
        evs = d.get("events")
        if not isinstance(evs, list) or "id" not in d:
            continue
        for ev in evs:
            if not (isinstance(ev, dict) and ev.get("kind") in CLICK_EVENTS and ev.get("actions")):
                continue
            blob = json.dumps(ev["actions"])
            low = blob.lower()
            target = ""
            for sid in re.findall(r"[\w]+", blob):
                if sid in known_ids:
                    target = sid
                    break
            if not target:
                if any(t in low for t in NEXT_TOKENS):
                    target = "NEXT"
                elif any(t in low for t in PREV_TOKENS):
                    target = "PREV"
            if target:
                label = d.get("accText") or d.get("altText") or ""
                if not label:
                    texts = [t for _, x in _walk(d) if isinstance(x.get("blocks"), list) for t in _block_text(x)]
                    label = " ".join(texts)[:60]
                label = _plain(label)
                # a button's normal / hover / visited states all carry the same text: "NEXT NEXT NEXT" -> "NEXT"
                words = label.split()
                for n in range(1, len(words) // 2 + 1):
                    if len(words) % n == 0 and words == words[:n] * (len(words) // n):
                        label = " ".join(words[:n])
                        break
                out.append({"id": str(d["id"]), "label": label, "target": target})
                break
    return out


def _dead_clicks(slide_json):
    out = []
    for _, d in _walk(slide_json):
        evs = d.get("events")
        if not isinstance(evs, list) or "id" not in d:
            continue
        for ev in evs:
            if isinstance(ev, dict) and ev.get("kind") in CLICK_EVENTS and not ev.get("actions"):
                name = d.get("accText") or d.get("altText") or d.get("id")
                out.append(str(name))
    return out


def _is_quiz(slide_json):
    blob = json.dumps(slide_json)[:500000].lower()
    return any(k in blob for k in ('"kind":"multiplechoice"', '"kind":"truefalse"', '"kind":"multipleresponse"',
                                   '"quiz"', '"interaction"', '"kind":"freeform', '"submit'))


# ---------------------------------------------------------------- loader
def find_launch(root: Path):
    # the LMS launch file first: it is the only one that reports completion
    for name in ("index_lms.html", "index_lms_html5.html", "story.html", "index.html", "story_html5.html"):
        hits = sorted(root.rglob(name), key=lambda p: len(p.parts))
        if hits:
            return hits[0]
    return None


def load_course(extract_dir: Path) -> Course:
    launch = find_launch(extract_dir)
    if not launch:
        raise ValueError("No story.html / index_lms.html found. Upload the published Storyline output zip.")
    root = launch.parent
    launch_rel = launch.name
    if not (root / "html5" / "data" / "js" / "data.js").exists():
        # a launch page that wraps the Storyline output kept in a sub-folder
        found = sorted(extract_dir.rglob("html5/data/js/data.js"), key=lambda p: len(p.parts))
        if found:
            import os
            root = found[0].parents[3]
            launch_rel = Path(os.path.relpath(launch, root)).as_posix()
    course = Course(root=root, launch_file=launch_rel)
    js_dir = root / "html5" / "data" / "js"
    data_file = js_dir / "data.js"
    if not data_file.exists():
        course.warnings.append("html5/data/js/data.js not found – the course model could not be read; "
                               "checks will rely on what is seen in the browser only.")
        return course
    _, data = decode_provide_file(data_file)
    if not isinstance(data, dict):
        course.warnings.append("data.js could not be decoded.")
        return course
    course.title = data.get("title") or data.get("projectTitle") or ""
    _, frame = decode_provide_file(js_dir / "frame.js")
    course.frame = frame if isinstance(frame, dict) else {}
    course.scorings = [{k: x.get(k) for k in ("type", "passPercent", "viewThreshold", "passStatus", "failStatus", "lmstext")}
                       | {"questions": len(x.get("scoreRefs") or [])}
                       for x in (data.get("scorings") or []) if isinstance(x, dict)]

    idx = 0
    for scene in data.get("scenes", []) or []:
        # the player's own Resume / Warning prompts live in a "message scene": they are not course screens
        if scene.get("isMessageScene") or scene.get("id") == "PromptScene":
            continue
        start = str(scene.get("startingSlide") or "").rsplit(".", 1)[-1]
        for sd in scene.get("slides", []) or []:
            if str(sd.get("id")) in ("ResumePromptSlide", "ExternalInterfaceErrorSlide"):
                continue
            idx += 1
            course.slides.append(Slide(
                id=str(sd.get("id", f"slide{idx}")),
                title=_plain(sd.get("title") or ""),
                index=idx,
                scene=scene.get("title") or scene.get("id", ""),
                url=sd.get("html5url") or sd.get("url") or "",
                scene_id=str(scene.get("id") or ""),
            ))
        if scene.get("id"):
            course.scene_start[str(scene["id"])] = start or (course.slides[-1].id if course.slides else "")
    known = set(course.ids())
    for i, s in enumerate(course.slides):
        path = root / s.url if s.url else js_dir / f"{s.id}.js"
        kind, sj = decode_provide_file(path)
        if not isinstance(sj, dict):
            s.raw_ok = False
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        s.texts, s.notes = _texts(sj)
        s.captions = _captions(sj)
        s.audio = sorted(set(MEDIA_RE.findall(raw)))
        s.video = sorted(set(VIDEO_RE.findall(raw)))
        s.duration_ms = _duration(sj)
        s.next_target = _event_target(sj, {"onnextslide", "next"}, known - {s.id})
        s.prev_target = _event_target(sj, {"onprevslide", "prev"}, known - {s.id})
        s.is_quiz = _is_quiz(sj)
        s.has_interaction = s.is_quiz or bool(sj.get("interactions")) or '"dragdrop' in raw.lower() or '"kind":"dragitem' in raw.lower()
        s.dead_click_objects = _dead_clicks(sj)
        s.nav_objects = _nav_objects(sj, known - {s.id})
        s.object_ids = {str(d["id"]) for _, d in _walk(sj) if "id" in d and isinstance(d.get("id"), (str, int))} - {s.id}
    seen, shared = set(), set()
    for s in course.slides:
        shared |= seen & s.object_ids
        seen |= s.object_ids
    for s in course.slides:
        s.object_ids -= shared | known
    for i, s in enumerate(course.slides):
        if not s.next_target:
            # a custom "next" button on the slide decides where Next goes when the player has none
            later = {x.id for x in course.slides[i + 1:]}
            fwd = [o["target"] for o in s.nav_objects if o["target"] in later]
            if fwd:
                s.next_target = fwd[0]
            elif i + 1 < len(course.slides):
                s.next_target = course.slides[i + 1].id
        if not s.prev_target and i > 0:
            s.prev_target = course.slides[i - 1].id
    return course
