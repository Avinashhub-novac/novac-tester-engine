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
AUDIO_EXT = re.compile(r"\.(?:mp3|m4a|wav|ogg|aac)\b", re.I)
VIDEO_EXT = re.compile(r"\.(?:mp4|webm|m4v)\b", re.I)
_PATH_CH = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-./")


def _media_refs(raw, ext_re):
    """File paths ending in one of the extensions. Found from each extension backwards, so the time grows only
    with the file size (a pattern search over long embedded data could take hours)."""
    out = set()
    for m in ext_re.finditer(raw):
        i = m.start()
        j = i
        while j > 0 and raw[j - 1] in _PATH_CH and i - j < 300:
            j -= 1
        name = raw[j:m.end()].lstrip("./")
        stem = name[: -len(m.group(0))].rsplit("/", 1)[-1]
        if stem and any(c.isalnum() for c in stem):
            out.add(name)
    return sorted(out)
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
    dead_click_objects: list = field(default_factory=list)  # [{id, label}] objects with click triggers but no actions
    nav_objects: list = field(default_factory=list)  # [{id, label, target}] slide buttons that jump to a slide
    click_objects: list = field(default_factory=list)  # [{id, label}] every object with a click trigger
    close_objects: list = field(default_factory=list)  # ids of buttons that close a pop-up layer
    raw_ok: bool = True
    object_ids: set = field(default_factory=set)   # ids of every object on the slide (shown as data-model-id)
    scene_id: str = ""
    has_interaction: bool = False   # drag-drop, quiz or other activity the learner must finish
    draw: str = ""   # id of the question bank draw this question comes from ("" = a normal screen)
    choices: list = field(default_factory=list)  # [{obj, text, correct}] answer options of a one-answer question (from the course data)

    @property
    def abs_id(self):
        """The player's own address for the slide (used to open it directly)."""
        if self.scene_id and self.draw:
            return f"_player.{self.scene_id}.{self.draw}.{self.id}"
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
    menu_ids: list = field(default_factory=list)       # ids of course "Menu" buttons found in the published data
    scorings: list = field(default_factory=list)      # quiz scoring settings (pass mark, quiz type)
    draws: dict = field(default_factory=dict)  # draw id -> {ids, count, exit, abs, scene}: questions picked from a bank
    quiz_refs: list = field(default_factory=list)  # slide ids the graded quiz scores (draws expanded), story order

    def draw_of(self, sid):
        s = self.by_id(sid)
        return s.draw if s else ""

    def same_draw(self, a, b):
        da = self.draw_of(a)
        return bool(da) and da == self.draw_of(b)

    def asked(self, quiz):
        """How many questions the learner answers in one attempt (a draw asks only its pick count)."""
        n, seen = 0, set()
        for q in quiz:
            if q.draw:
                if q.draw not in seen:
                    seen.add(q.draw)
                    n += self.draws.get(q.draw, {}).get("count") or 0
            else:
                n += 1
        return n

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


MENU_LABEL = re.compile(r"^\s*(menu|course menu|main menu|contents|table of contents|toc)\s*$", re.I)


def _label_of(d):
    label = d.get("accText") or d.get("altText") or ""
    if not label:
        texts = [t for _, x in _walk(d) if isinstance(x.get("blocks"), list) for t in _block_text(x)]
        label = " ".join(texts)[:60]
    label = _plain(label)
    words = label.split()
    for n in range(1, len(words) // 2 + 1):
        if len(words) % n == 0 and words == words[:n] * (len(words) // n):
            return " ".join(words[:n])
    return label


def _menu_objects(obj):
    """Ids of clickable objects labelled "Menu" (a course menu button designed into the screens or the master)."""
    out = []
    for _, d in _walk(obj):
        evs = d.get("events")
        if not isinstance(evs, list) or "id" not in d:
            continue
        if any(isinstance(ev, dict) and ev.get("kind") in CLICK_EVENTS and ev.get("actions") for ev in evs):
            if MENU_LABEL.match(_label_of(d)):
                out.append(str(d["id"]))
    return out


def _click_objects(slide_json):
    """Every object on the slide with a click trigger that does something: [{id, label}]."""
    out, seen = [], set()
    for _, d in _walk(slide_json):
        evs = d.get("events")
        if not isinstance(evs, list) or "id" not in d or str(d["id"]) in seen:
            continue
        if any(isinstance(ev, dict) and ev.get("kind") in CLICK_EVENTS and ev.get("actions") for ev in evs):
            seen.add(str(d["id"]))
            out.append({"id": str(d["id"]), "label": _label_of(d)})
    return out


def _close_objects(slide_json):
    """Ids of buttons whose click hides a layer (the close buttons of pop-ups, often an unnamed X icon)."""
    out = []
    for _, d in _walk(slide_json):
        evs = d.get("events")
        if not isinstance(evs, list) or "id" not in d:
            continue
        for ev in evs:
            if isinstance(ev, dict) and ev.get("kind") in CLICK_EVENTS and '"hide_slidelayer"' in json.dumps(ev.get("actions") or [], separators=(",", ":")):
                out.append(str(d["id"]))
                break
    return out


def _dead_clicks(slide_json):
    out = []
    for _, d in _walk(slide_json):
        evs = d.get("events")
        if not isinstance(evs, list) or "id" not in d:
            continue
        for ev in evs:
            # no action at all, or only the button's own hover-state reset: a click does nothing for the learner
            acts = [a for a in (ev.get("actions") or []) if isinstance(a, dict)] if isinstance(ev, dict) else []
            only_hover = acts and all(a.get("kind") == "exe_actiongroup" and "hoverstate" in str(a.get("id", "")).lower() for a in acts)
            if isinstance(ev, dict) and ev.get("kind") in CLICK_EVENTS and (not ev.get("actions") or only_hover):
                name = d.get("accText") or d.get("altText") or _label_of(d) or ""
                out.append({"id": str(d["id"]), "label": str(name)})
    return out


def _is_quiz(slide_json):
    # a question screen evaluates an answer (Storyline's eval_interaction action) or carries a question type;
    # a screen that only mentions words like "submit" or "quiz" in its text is not a question
    blob = json.dumps(slide_json, separators=(",", ":"))[:500000].lower()
    return any(k in blob for k in ('"kind":"eval_interaction"', '"kind":"multiplechoice"', '"kind":"truefalse"',
                                   '"kind":"multipleresponse"', '"kind":"freeform', '"kind":"matching"',
                                   '"kind":"sequence"', '"kind":"fillin"', '"kind":"hotspot"', '"kind":"wordbank"'))


def _choices(slide_entry):
    """Answer options of a one-answer question and which one is right, as the course data defines them.
    Returns [] when the screen is not such a question (or the right answer cannot be read)."""
    inters = [x for x in (slide_entry.get("interactions") or []) if isinstance(x, dict)]
    if len(inters) != 1 or inters[0].get("type") not in ("multiplechoice", "truefalse") or inters[0].get("issurvey"):
        return []
    it = inters[0]
    right = set()
    for a in it.get("answers") or []:
        if not isinstance(a, dict) or a.get("status") != "correct":
            continue
        sts = (a.get("evaluate") or {}).get("statements") or []
        ids = [str(s.get("choiceid") or "") for s in sts if isinstance(s, dict) and s.get("kind") == "equals"]
        if len(ids) == 1:
            right.add(ids[0].rsplit("choice_", 1)[-1])
    out = []
    for c in it.get("choices") or []:
        cid = str((c or {}).get("id") or "")
        obj = cid.split("choice_", 1)[-1]
        out.append({"obj": obj, "text": _plain(c.get("lmstext") or ""), "correct": obj in right})
    if len(right) != 1 or sum(1 for c in out if c["correct"]) != 1:
        return []
    return out


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

    bank = {str(b.get("id")): b for b in ((data.get("slideBank") or {}).get("slides") or []) if isinstance(b, dict) and b.get("id")}
    idx = 0
    for scene in data.get("scenes", []) or []:
        # the player's own Resume / Warning prompts live in a "message scene": they are not course screens
        if scene.get("isMessageScene") or scene.get("id") == "PromptScene":
            continue
        start = str(scene.get("startingSlide") or "").rsplit(".", 1)[-1]
        # question banks: a "draw" sits between the scene's slides and shows N questions picked from a bank
        draws_at = {}
        for dr in scene.get("slidedraws", []) or []:
            if not isinstance(dr, dict) or not dr.get("id"):
                continue
            refs = [str(r.get("id")) for r in dr.get("sliderefs") or [] if isinstance(r, dict) and r.get("id") in bank]
            if not refs:
                continue
            ex = dr.get("exitaction") or {}
            exit_ref = str(((ex.get("objRef") or {}).get("value")) or "").rsplit(".", 1)[-1]
            cnt = int(dr.get("shufflecount") or 0) if dr.get("shuffle") else 0
            cnt = cnt if 0 < cnt <= len(refs) else len(refs)
            course.draws[str(dr["id"])] = {"ids": refs, "count": cnt, "exit": exit_ref, "scene": str(scene.get("id") or ""),
                                           "abs": f"_player.{scene.get('id')}.{dr['id']}", "lms": dr.get("lmsId") or ""}
            pos = int(dr.get("slideNumberInScene") or 0)
            draws_at.setdefault(pos, []).append(str(dr["id"]))

        def add(sd, draw=""):
            nonlocal idx
            idx += 1
            course.slides.append(Slide(
                choices=_choices(sd),
                id=str(sd.get("id", f"slide{idx}")),
                title=_plain(sd.get("title") or ""),
                index=idx,
                scene=scene.get("title") or scene.get("id", ""),
                url=sd.get("html5url") or sd.get("url") or "",
                scene_id=str(scene.get("id") or ""),
                draw=draw,
            ))

        def add_draws(pos):
            for did in draws_at.pop(pos, []):
                for rid in course.draws[did]["ids"]:
                    add(bank[rid], did)

        item = 0   # draws and slides share one numbering in the scene
        for sd in scene.get("slides", []) or []:
            if str(sd.get("id")) in ("ResumePromptSlide", "ExternalInterfaceErrorSlide"):
                continue
            while item + 1 in draws_at:
                item += 1
                add_draws(item)
            item += 1
            add(sd)
        for pos in sorted(draws_at):
            add_draws(pos)
        if scene.get("id"):
            course.scene_start[str(scene["id"])] = start or (course.slides[-1].id if course.slides else "")
    known = set(course.ids())
    # a Menu button can sit on the slide master: look through every data file, not only the slides
    menu_ids = set(_menu_objects(data))
    for extra in js_dir.glob("*.js"):
        if extra.name in ("data.js", "frame.js") or extra.stat().st_size > 8_000_000:
            continue
        try:
            _, ej = decode_provide_file(extra)
        except Exception:
            continue
        if isinstance(ej, (dict, list)):
            menu_ids |= set(_menu_objects(ej))
    course.menu_ids = sorted(menu_ids)
    for i, s in enumerate(course.slides):
        path = root / s.url if s.url else js_dir / f"{s.id}.js"
        kind, sj = decode_provide_file(path)
        if not isinstance(sj, dict):
            s.raw_ok = False
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        s.texts, s.notes = _texts(sj)
        s.captions = _captions(sj)
        s.audio = _media_refs(raw, AUDIO_EXT)
        s.video = _media_refs(raw, VIDEO_EXT)
        s.duration_ms = _duration(sj)
        s.next_target = _event_target(sj, {"onnextslide", "next"}, known - {s.id})
        s.prev_target = _event_target(sj, {"onprevslide", "prev"}, known - {s.id})
        s.is_quiz = _is_quiz(sj)
        s.has_interaction = s.is_quiz or bool(sj.get("interactions")) or '"dragdrop' in raw.lower() or '"kind":"dragitem' in raw.lower()
        s.dead_click_objects = _dead_clicks(sj)
        s.nav_objects = _nav_objects(sj, (known | set(course.draws)) - {s.id})
        for o in s.nav_objects:   # a button that starts a question bank goes to its first question
            if o["target"] in course.draws:
                o["target"] = course.draws[o["target"]]["ids"][0]
        if not s.next_target:
            d = _event_target(sj, {"onnextslide", "next"}, set(course.draws))
            if d:
                s.next_target = course.draws[d]["ids"][0]
        s.click_objects = _click_objects(sj)
        s.close_objects = _close_objects(sj)
        s.object_ids = {str(d["id"]) for _, d in _walk(sj) if "id" in d and isinstance(d.get("id"), (str, int))} - {s.id}
    # the slides the graded quiz scores (a draw stands for all its bank questions)
    for qz in data.get("quizzes") or []:
        if not isinstance(qz, dict) or qz.get("issurvey"):
            continue
        for r in qz.get("sliderefs") or []:
            rid = str((r or {}).get("id") or "").rsplit(".", 1)[-1]
            if rid in course.draws:
                course.quiz_refs += course.draws[rid]["ids"]
            elif rid in known:
                course.quiz_refs.append(rid)
    order = {sid: i for i, sid in enumerate(course.ids())}
    course.quiz_refs = sorted(dict.fromkeys(course.quiz_refs), key=lambda x: order.get(x, 0))
    seen, shared = set(), set()
    for s in course.slides:
        shared |= seen & s.object_ids
        seen |= s.object_ids
    for s in course.slides:
        s.object_ids -= shared | known
    for s in course.slides:
        if s.draw and course.draws.get(s.draw, {}).get("exit") in known:
            s.next_target = s.next_target or ""
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
