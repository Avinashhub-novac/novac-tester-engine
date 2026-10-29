"""Assessment testing: every answer option, the feedback for each option, and the full pass / fail / retry runs.

The tester works out the right answers by trying each option and reading the feedback, then plays the
assessment from the first question like a learner: once with every answer right, once with every answer
wrong (and retries when the course offers them). What the results screen says and what reached the LMS
are compared with the course's own pass mark and with what the rules screen promises.
"""
import re

from .player import OPTIONS_JS, LMS_JS, SEL

RULES_RE = re.compile(r"\b(rules?|instructions?|assessment|quiz|test|exam|knowledge check|check your understanding|cyu)\b", re.I)
RESULT_RE = re.compile(r"\b(results?|score|scored|passed|failed|you (have )?(passed|failed)|pass mark|passing score)\b|%", re.I)
CORRECT_RE = re.compile(r"\b(correct|that'?s right|well done|great job|excellent|right answer)\b", re.I)
INCORRECT_RE = re.compile(r"\b(incorrect|not correct|not quite|wrong|that'?s not right|try again|sorry)\b", re.I)
MUST_ANSWER_RE = re.compile(r"\b(must|please)\b.{0,40}\b(answer|select|choose|make a selection)\b|\bselect an? (answer|option)\b", re.I)
PASS_RE = re.compile(r"\b(passed|you pass|congratulations|well done|successfully)\b", re.I)
FAIL_RE = re.compile(r"\b(failed|did not pass|didn'?t pass|not passed|unsuccessful|try again|retry|retake)\b", re.I)
RETRY_RE = re.compile(r"^\s*(retry|retake|try again|retry (quiz|assessment)|retake (quiz|assessment))\s*$", re.I)
SCORES = (100, 80, 60, 0)   # the fixed score plan of every assessment test (never changed)
NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
             "unlimited": 0}


def assessment_parts(course):
    """(rules slides, question slides, results slides) of the course's assessment, in story order."""
    slides = course.slides
    refs = set(getattr(course, "quiz_refs", None) or [])
    # the graded quiz names its own questions (a knowledge check elsewhere in the course is not part of it)
    quiz = [s for s in slides if s.id in refs] or [s for s in slides if s.is_quiz]
    if not quiz:
        return [], [], []
    first, last = slides.index(quiz[0]), slides.index(quiz[-1])
    rules = [s for s in slides[max(0, first - 2):first] if RULES_RE.search(s.title + " " + " ".join(s.texts[:3]))]
    graded = any(x.get("type") not in (None, "view") for x in (course.scorings or []))
    after = slides[last + 1:last + 3]
    # a results screen exists only for a graded assessment (a quiz result in the course settings)
    results = [s for s in after if graded and not s.is_quiz and RESULT_RE.search(s.title + " " + " ".join(s.texts))][:1]
    return rules, quiz, results


def stated_rules(text):
    """What a rules screen promises: number of questions, attempts and the pass mark."""
    raw = re.sub(r"\^%\^|%\^", "%", text or "")   # Storyline writes % as ^%^ in its text data
    t = raw.lower()
    out = {}
    m = re.search(r"(\d+|" + "|".join(NUM_WORDS) + r")\s+(?:multiple[- ]choice\s+)?(?:questions|items)", t)
    if m:
        out["questions"] = int(m.group(1)) if m.group(1).isdigit() else NUM_WORDS[m.group(1)]
    m = re.search(r"(\d+|" + "|".join(NUM_WORDS) + r")\s+(?:attempts?|tries|chances)", t)
    if m:
        out["attempts"] = int(m.group(1)) if m.group(1).isdigit() else NUM_WORDS[m.group(1)]
    m = re.search(r"(\d{1,3})\s*(?:%|percent)", t)
    if m:
        out["pass_mark"] = int(m.group(1))
    if "attempts" not in out and re.search(r"\b(unlimited|any number of)\s+(attempts?|tries)|\bas many times\b", t):
        out["attempts"] = 0
    # the module's own instruction on retry / retake (shown in the report and compared after the last attempt)
    sents = [x.strip() for x in re.split(r"(?<=[.!?])\s+|\s{2,}|\n", raw) if x.strip()]
    ins = []
    for x in sents:
        if re.search(r"\b(attempts?|retake|retry|try again|re-?attempt)\b", x, re.I) and x.rstrip(".") not in [y.rstrip(".") for y in ins]:
            ins.append(x)
    if ins:
        out["instruction"] = " ".join(ins)[:300]
    # "you will need to retake the assessment" with no number: the assessment can be taken again (no limit stated)
    if "attempts" not in out and re.search(r"\b(retake|retry|re-?attempt|take)\b.{0,25}\b(assessment|quiz|test|exam)\b(?!.{0,15}\bcourse\b)", t):
        out["retake_allowed"] = True
    return out


class QuizMixin:
    """Mixed into the Crawler."""

    # ---------------------------------------------------------------- one question
    def _options(self, page):
        self.unscroll(page)
        try:
            return [o for o in page.evaluate(OPTIONS_JS) if not o.get("disabled")]
        except Exception:
            return []

    def _screen_text(self, page):
        st = self.state(page)
        return (st.get("text", "") + " " + st.get("acc", "")).strip()

    def _click_named(self, page, words):
        for txt in words:
            try:
                b = page.get_by_role("button", name=re.compile(rf"^\s*{txt}\s*$", re.I))
                if b.count() and b.first.is_visible():
                    b.first.click(timeout=1200)
                    page.wait_for_timeout(900)
                    return txt
            except Exception:
                continue
        for o in self._clickables(page):   # buttons drawn as pictures carry their name in the label
            if re.match(rf"^\s*({'|'.join(words)})\s*$", o.get("label") or "", re.I):
                page.mouse.click(o["x"], o["y"])
                page.wait_for_timeout(900)
                return o["label"]
        b = self._named_box(page, words)   # a button drawn as a shape on the slide
        if b:
            self.click_box(page, b)
            page.wait_for_timeout(900)
            return words[0]
        return None

    def _region_changed(self, before_png, page, box):
        try:
            from io import BytesIO
            from PIL import Image, ImageChops
            a = Image.open(BytesIO(before_png)).convert("L")
            b = Image.open(BytesIO(page.screenshot())).convert("L")
            k = a.width / page.viewport_size["width"]
            x, y, w, h = [v * k for v in box]
            pad = 30 * k
            crop = (max(0, int(x - pad)), max(0, int(y - 6 * k)), min(a.width, int(x + w + pad)), min(a.height, int(y + h + 6 * k)))
            d = ImageChops.difference(a.crop(crop), b.crop(crop)).point(lambda v: 255 if v > 30 else 0)
            return sum(1 for v in d.getdata() if v) > 0.01 * d.width * d.height
        except Exception:
            return False

    def select_option(self, page, i):
        """Clicks answer option i like a learner. Returns (option, selectable)."""
        opts = self._options(page)
        if i >= len(opts):
            return None, False
        o = opts[i]
        try:
            before = page.screenshot()
        except Exception:
            before = None
        b = o["box"]
        page.mouse.click(b[0] + min(b[2] / 2, 24), b[1] + b[3] / 2)
        page.wait_for_timeout(500)
        now = self._options(page)
        same = min(now, key=lambda p: abs(p["box"][1] - b[1]) + abs(p["box"][0] - b[0])) if now else None
        ok = bool(same and same.get("checked"))
        if not ok and before is not None:
            ok = self._region_changed(before, page, b)
        return o, ok

    def submit_answer(self, page):
        """Presses Submit and reads the feedback. Returns 'Correct', 'Incorrect', 'Answer required' or 'No feedback'."""
        before = self._screen_text(page)
        sub = self._submit_box(page)
        if sub:
            self.click_box(page, sub)
        elif not self._click_named(page, ["submit", "check", "check answer", "done"]):
            return "No Submit button", ""
        page.wait_for_timeout(1500)
        after = self._screen_text(page)
        new = " ".join(w for w in after.split() if w not in set(before.split()))
        if not new.strip():   # nothing new on screen: Submit did nothing (the question's own words, e.g.
            return "No feedback", ""   # "Select the correct option", are not feedback)
        txt = new
        if MUST_ANSWER_RE.search(txt):
            return "Answer required", txt[:200]
        if INCORRECT_RE.search(txt):
            return "Incorrect", txt[:200]
        if CORRECT_RE.search(txt):
            return "Correct", txt[:200]
        return "No feedback", txt[:200]

    def leave_feedback(self, page, sid):
        """Closes the feedback / goes on (Continue, OK, Next). Returns the slide now showing."""
        self._click_named(page, ["continue", "ok", "close", "next", "proceed"])
        st = self.settle(page, 600)
        here = self.identify(st)
        if here == sid and st["nextExists"] and not st["nextDisabled"]:
            self.click_first(page, SEL["next"])
            here = self.identify(self.settle(page))
        return here

    # ---------------------------------------------------------------- drag and drop
    def drag_to(self, page, item_id, target_id):
        """Drags one item onto a drop target like a learner (press, move, release).
        Returns {moved, placed} or None when either is not on screen."""
        a = self._visible_box(page, item_id) or self._box_of(page, item_id)
        b = self._box_of(page, target_id)
        if not a or not b:
            return None
        sx, sy = a[0] + a[2] / 2, a[1] + a[3] / 2
        tx, ty = b[0] + b[2] / 2, b[1] + b[3] / 2
        page.mouse.move(sx, sy)
        page.mouse.down()
        for k in range(1, 13):
            page.mouse.move(sx + (tx - sx) * k / 12, sy + (ty - sy) * k / 12)
            page.wait_for_timeout(25)
        page.mouse.up()
        page.wait_for_timeout(600)
        a2 = self._box_of(page, item_id)
        if not a2:
            return {"moved": True, "placed": True}   # some items disappear once dropped
        cx, cy = a2[0] + a2[2] / 2, a2[1] + a2[3] / 2
        pad = 12
        return {"moved": abs(cx - sx) > 8 or abs(cy - sy) > 8,
                "placed": b[0] - pad <= cx <= b[0] + b[2] + pad and b[1] - pad <= cy <= b[1] + b[3] + pad}

    def solve_drag(self, page, slide, mode="right", rot=0):
        """Places every drag item. mode "right": on its right target (from the course data; without it, each target is
        tried until one keeps the item). mode "wrong": every item on a wrong target. Returns [{item, target, moved, placed}]."""
        targets = [x["id"] for x in slide.drop_targets]
        names = {x["id"]: x["label"] for x in slide.drop_targets}
        key, out, used = slide.drag_key or {}, [], set()
        for k, it in enumerate(slide.drag_items):
            self.check_time()
            want = key.get(it["id"])
            if want and mode == "right":
                tries = [want]
            elif want:
                # every item on a DIFFERENT wrong target: a target usually keeps only one item, so items that all
                # aim at the same wrong target leave some unplaced and Submit stays off (seen on matching questions)
                n = len(slide.drag_items)
                order = [key.get(slide.drag_items[(k + 1 + rot + j) % n]["id"]) for j in range(n)] + targets
                others, seen = [], set()
                for x in order:
                    if x and x != want and x in targets and x not in seen:
                        seen.add(x)
                        others.append(x)
                tries = [x for x in others if x not in used] + [x for x in others if x in used] or [want]
            else:   # no answer key: try each free target until one keeps the item
                tries = [x for x in targets if x not in used] + [x for x in targets if x in used]
            res, tgt = None, None
            for tg in tries[:6]:
                res, tgt = self.drag_to(page, it["id"], tg), tg
                if res and res["placed"]:
                    break
            if res and res["placed"]:
                used.add(tgt)
            out.append({"item": it["label"] or it["id"], "target": names.get(tgt, tgt or ""), "moved": bool(res and res["moved"]),
                        "placed": bool(res and res["placed"])})
        return out

    def answer_drag(self, page, sid, slide, mode="right", rot=0):
        """A drag-and-drop screen: place the items, press Submit if there is one, read the feedback."""
        placed = self.solve_drag(page, slide, mode, rot)
        fb, text = ("No Submit button", "")
        if self._submit_box(page) or self._named_box(page, ["submit"]):
            fb, text = self.submit_answer(page)
        return placed, fb, text

    def answer_question(self, page, sid, i=0):
        """Desktop walk: answer with option i and go on. Returns the slide reached, or None when there are no options."""
        slide = self.course.by_id(sid)
        if slide and slide.drag_items and not self._options(page):
            self.settle_timeline(page)
            self.answer_drag(page, sid, slide, "right")
            return self.leave_feedback(page, sid)
        for _ in range(10):   # the options may still be flying in with the slide's entrance
            if self._options(page):
                break
            page.wait_for_timeout(1500)
        else:
            return None
        self.select_option(page, i)
        self.submit_answer(page)
        return self.leave_feedback(page, sid)

    # ---------------------------------------------------------------- the pass
    def fresh(self, page):
        """A new learner session: reload the course (the test LMS starts empty again)."""
        try:   # nothing remembered from the earlier attempt (the player keeps "resume" data in the browser)
            page.evaluate("() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }")
        except Exception:
            pass
        page.reload(wait_until="load", timeout=90000)
        self._ready(page)
        self.dismiss_prompts(page)

    def assessment_pass(self, browser):
        rules, quiz, results = assessment_parts(self.course)
        if not quiz:
            return
        if results and all(q.choices or q.drag_key for q in quiz):
            # the course data says which answer is right: test everything in a few full attempts (no reloads)
            return self._assessment_fast(browser, rules, quiz, results)
        return self._assessment_slow(browser, rules, quiz, results)

    # ---------------------------------------------------------------- fast: the answer key is in the course data
    def _dom_index(self, page, q, k):
        """Position on screen of answer k of question q (options may be shuffled)."""
        opts = self._options(page)
        obj = q.choices[k]["obj"] if k < len(q.choices) else ""
        for i, o in enumerate(opts):
            if o.get("mid") and o["mid"] == obj:
                return i, opts
        return (k if k < len(opts) else None), opts

    def _assessment_fast(self, browser, rules, quiz, results):
        ctx = browser.new_context(viewport={"width": 1366, "height": 768}, device_scale_factor=2)
        page = self.open(ctx, "assessment")
        A = self.assessment = {"questions": [], "scenarios": [], "rules": {}, "pass_mark": None,
                               "rules_ids": [s.id for s in rules], "quiz_ids": [s.id for s in quiz],
                               "results_ids": [s.id for s in results], "layer_shots": {}}
        A["rules"] = stated_rules("\n".join("\n".join(s.texts) for s in rules))
        scoring = next((x for x in self.course.scorings if x.get("type") not in (None, "view")), None) or {}
        A["pass_mark"] = scoring.get("passPercent")
        asked = self.course.asked(quiz)
        runs = 4 + self.attempts_to_test(A["rules"])
        self._aprog = {"base": 0, "total": runs * asked + 2, "n": 0}
        rules_shot = ""
        if rules and self.goto_slide(page, rules[0].id) == rules[0].id:
            self.settle_timeline(page)
            rules_shot = self.shot(page, f"{rules[0].id}_rules")
        qinfo = {}
        for q in quiz:
            if q.drag_key and not q.choices:
                names = {x["id"]: x["label"] for x in q.drop_targets}
                pairs = ", ".join(f"{i['label']} → {names.get(q.drag_key.get(i['id']), '?')}" for i in q.drag_items)
                qinfo[q.id] = {"sid": q.id, "label": q.label, "kind": "drag", "shot": "", "key": [0],
                               "options": [{"label": f"Every item on its right place ({pairs})", "selectable": None, "feedback": "",
                                            "feedback_text": "", "shot": "", "box": None},
                                           {"label": "Items on wrong places", "selectable": None, "feedback": "",
                                            "feedback_text": "", "shot": "", "box": None}]}
            else:
                key = [k for k, c in enumerate(q.choices) if c["correct"]]
                qinfo[q.id] = {"sid": q.id, "label": q.label, "kind": "choice", "shot": "", "key": key,
                               "options": [{"label": c["text"] or f"Option {k + 1}", "selectable": None, "feedback": "",
                                            "feedback_text": "", "shot": "", "box": None} for k, c in enumerate(q.choices)]}
            A["questions"].append(qinfo[q.id])
        state = {"blank_done": False}

        def first_look(sid, q):
            """First time a question shows: picture it and check every option can be selected."""
            qi = qinfo[sid]
            if qi["shot"]:
                return
            qi["shot"] = self.shot(page, f"{sid}_question")
            if qi["kind"] == "drag":
                return   # whether each item can be dragged is seen when the items are placed
            for k in range(len(q.choices)):
                i, opts = self._dom_index(page, q, k)
                rec = qi["options"][k]
                if i is None:
                    rec["selectable"] = False
                    continue
                o, ok = self.select_option(page, i)
                rec["selectable"], rec["box"] = ok, (o or {}).get("box")
                if not ok:
                    rec["shot"] = self.shot(page, f"{sid}_opt{k}")
                    self.col.add("assessment", "error", "Answer option cannot be selected", slide_id=sid, slide_label=q.label,
                                 detail=f"Option: “{rec['label']}”", screenshot=rec["shot"], box=rec["box"], vw=1366,
                                 found_in="Assessment", fix="Make this option clickable so the learner can choose it.")

        def blank_submit(sid, q):
            fb, _ = self.submit_answer(page)
            here = self.identify(self.settle(page, 400))
            shot = self.shot(page, f"{sid}_blank")
            # the blank answer was refused when the learner is asked to choose, or simply stays on the question
            # with no right / wrong feedback (Submit stays greyed out until an option is chosen)
            ok = fb == "Answer required" or (here == sid and fb not in ("Correct", "Incorrect"))
            A["scenarios"].append({"name": "Submit without choosing an answer",
                                   "steps": f"Open {q.label}, press Submit without selecting any option.",
                                   "expected": "The learner is asked to choose an answer and stays on the question.",
                                   "actual": {"Answer required": "A message asks to choose an answer.",
                                              "No Submit button": "Submit is not available until an option is chosen."}.get(
                                       fb, ("Stayed on the question; Submit does nothing until an option is chosen." if fb == "No feedback"
                                            else f"Stayed on the question but showed “{fb}” feedback.") if here == sid
                                       else f"Moved on to {self.label(here)}."),
                                   "result": "Pass" if ok else "Fail", "shot": shot})
            if not ok:
                self.col.add("assessment", "error", "The question can be submitted without choosing an answer", slide_id=sid,
                             slide_label=q.label, screenshot=shot, vw=1366, found_in="Assessment",
                             fix="Ask the learner to choose an answer before Submit is accepted.")
            self.dismiss_prompts(page)
            return ok and here == sid

        def play(mode, run_no, retry_box=None):
            # mode: the score this attempt aims at (100, 80, 60 or 0); the first questions asked are answered right
            n_right = self.n_right(mode, asked)
            out = {"answers": [], "end_sid": "", "text": "", "shot": "", "lms": {}, "retry": None, "start_sid": "", "cleared": True}
            if retry_box:
                self.click_box(page, retry_box)
                sid = self.identify(self.settle(page))
                out["start_sid"], out["start_shot"] = sid, self.shot(page, f"retry{run_no}_start_{sid}")
                out["cleared"] = not any(o.get("checked") for o in self._options(page))
                if not self.is_first_question(sid, quiz):
                    out["end_sid"] = sid
                    return out
            else:
                self.fresh(page)
                sid = self.start_quiz(page, quiz)
                if not sid:
                    out["end_sid"] = self.identify(self.state(page))
                    return out
                out["start_sid"] = sid
            qids = {x.id for x in quiz}
            for _ in range(len(quiz) * 2 + 2):
                self.check_time()
                if sid not in qids:
                    break
                q = self.course.by_id(sid)
                self.settle_timeline(page)
                for _w in range(10):   # options may still be flying in
                    if self._options(page):
                        break
                    page.wait_for_timeout(1000)
                if not state["blank_done"]:
                    state["blank_done"] = True
                    if not blank_submit(sid, q):
                        return None   # the blank answer was taken: start this attempt again
                first_look(sid, q)
                rt = len(out["answers"]) < n_right   # this question is answered right
                if qinfo[sid]["kind"] == "drag":
                    k = 0 if rt else 1
                    placed, fb, text = self.answer_drag(page, sid, q, "right" if rt else "wrong", run_no)
                    rec = qinfo[sid]["options"][k]
                    stuck = [p for p in placed if not p["moved"]]
                    if rec["selectable"] is None:
                        rec["selectable"] = not stuck
                        if stuck:
                            self.col.add("assessment", "error", "Item cannot be dragged", slide_id=sid, slide_label=q.label,
                                         detail="Items: " + ", ".join(f"“{p['item']}”" for p in stuck),
                                         screenshot=self.shot(page, f"{sid}_drag_stuck"), vw=1366, found_in="Assessment",
                                         fix="Make every item draggable so the learner can place it.")
                    if not rec["feedback"]:
                        rec["feedback"], rec["feedback_text"] = fb, text
                        rec["shot"] = self.shot(page, f"{sid}_fb{k}")
                        want = "Correct" if rt else "Incorrect"
                        if fb in ("Correct", "Incorrect") and fb != want:
                            msg = "Right placement is marked as wrong" if want == "Correct" else "Wrong placement is accepted as right"
                            self.col.add("assessment", "error", msg, slide_id=sid, slide_label=q.label,
                                         detail=f"Feedback shown: {fb}.", screenshot=rec["shot"], vw=1366, found_in="Assessment",
                                         fix="Set the right drop target for every item in the question settings.")
                    out["answers"].append((sid, k, fb))
                    new = self.leave_feedback(page, sid)
                    if new == sid:
                        placed = self.answer_drag(page, sid, q, "right" if rt else "wrong", run_no + 1)[0]
                        new = self.leave_feedback(page, sid)
                        if new == sid:
                            if not rt and not all(p["placed"] for p in placed):
                                # the tester could not lay out a full wrong answer, so Submit never opened: not a course error
                                out["blocked"] = (f"the tester could not place every item of {q.label} on a wrong target "
                                                  f"({sum(p['placed'] for p in placed)} of {len(placed)} placed), so the question could not be submitted")
                            break
                    sid = new
                    ap = self._aprog
                    ap["n"] += 1
                    self.tick("assessment", min(ap["n"], ap["total"] - 1), ap["total"])
                    continue
                right = [k for k, c in enumerate(q.choices) if c["correct"]]
                wrong = [k for k, c in enumerate(q.choices) if not c["correct"]]
                k = right[0] if rt or not wrong else wrong[run_no % len(wrong)]
                multi = rt and len(right) > 1   # select all that apply: every right option is ticked
                if any(c.get("multi") for c in q.choices):
                    self._clear_ticks(page)
                for kk in (right if multi else [k]):
                    i, _ = self._dom_index(page, q, kk)
                    self.select_option(page, i if i is not None else 0)
                fb, text = self.submit_answer(page)
                rec = qinfo[sid]["options"][k]
                if not rec["feedback"]:
                    rec["feedback"], rec["feedback_text"] = fb, text
                    rec["shot"] = self.shot(page, f"{sid}_fb{k}")
                    try:
                        from .player import TEXTBOX_JS
                        A["layer_shots"].setdefault(sid, []).append({"shot": rec["shot"], "boxes": page.evaluate(TEXTBOX_JS)})
                    except Exception:
                        pass
                    want = "Correct" if k in right else "Incorrect"
                    if fb in ("Correct", "Incorrect") and fb != want:
                        msg = "The right answer is marked as wrong" if want == "Correct" else "A wrong answer is accepted as right"
                        self.col.add("assessment", "error", msg, slide_id=sid, slide_label=q.label,
                                     detail=f"Option: “{rec['label']}”. Feedback shown: {fb}.", screenshot=rec["shot"], vw=1366,
                                     found_in="Assessment", fix="Set the right answer in the question settings and check its feedback.")
                out["answers"].append((sid, k, fb))
                new = self.leave_feedback(page, sid)
                if new == sid:   # a second try on the same question: answer the same way again
                    if any(c.get("multi") for c in q.choices):
                        self._clear_ticks(page)
                    for kk in (right if multi else [k]):
                        i, _ = self._dom_index(page, q, kk)
                        self.select_option(page, i if i is not None else 0)
                    self.submit_answer(page)
                    new = self.leave_feedback(page, sid)
                    if new == sid:
                        break
                sid = new
                ap = self._aprog
                ap["n"] += 1
                self.tick("assessment", min(ap["n"], ap["total"] - 1), ap["total"])
            page.wait_for_timeout(2500)   # the result reaches the LMS a moment after the results screen opens
            out["end_sid"] = sid
            out["text"] = self._screen_text(page)[:1500]
            out["shot"] = self.shot(page, f"result_{mode}_{run_no}_{sid}")
            lms = page.evaluate(LMS_JS) or {}
            store = lms.get("store") or {}
            g = lambda *ks: next((store[k] for k in ks if store.get(k)), "")
            out["lms"] = {"score": g("cmi.core.score.raw", "cmi.score.raw"), "score_max": g("cmi.core.score.max", "cmi.score.max"),
                          "status": g("cmi.core.lesson_status", "cmi.success_status", "cmi.completion_status"),
                          "success": g("cmi.success_status"), "completion": g("cmi.completion_status")}
            for o in self._clickables(page):
                if RETRY_RE.match(o.get("label") or ""):
                    out["retry"] = self._obox(o)
                    break
            if out["retry"] is None:
                b = self._named_box(page, ["retry", "retake", "try again", "retake assessment", "retry quiz"])
                out["retry"] = b
            return out

        pm = A["pass_mark"] if A["pass_mark"] is not None else A["rules"].get("pass_mark")

        def run(pct, run_no, retry_box=None):
            r = play(pct, run_no, retry_box)
            if r is None and not retry_box:   # the blank-answer check used up the attempt: play it again
                r = play(pct, run_no, retry_box)
            return r

        self.strict_plan(A, quiz, results, run, pm, asked, can_right=(True, ""), can_wrong=(True, ""))
        # questions a question bank did not draw in any attempt
        for qi in A["questions"]:
            if not qi["shot"]:
                qi["kind"], qi["note"], qi["options"] = "not drawn", "Not drawn in the tested attempts (question bank)", []
        n_rules = A["rules"].get("questions")
        if n_rules and n_rules != asked:
            sid = rules[0].id if rules else quiz[0].id
            self.col.add("assessment", "error", "Number of questions is different from the rules screen", slide_id=sid,
                         slide_label=self.label(sid), screenshot=rules_shot, vw=1366, found_in="Assessment",
                         detail=f"Rules screen: {n_rules} questions. Assessment asks: {asked}.",
                         fix="Make the number of questions match the rules screen.")
        if A["rules"].get("pass_mark") and A["pass_mark"] is not None and A["rules"]["pass_mark"] != A["pass_mark"]:
            sid = rules[0].id if rules else quiz[0].id
            self.col.add("assessment", "error", "Pass mark on the rules screen is different from the course setting", slide_id=sid,
                         slide_label=self.label(sid), screenshot=rules_shot, vw=1366, found_in="Assessment",
                         detail=f"Rules screen: {A['rules']['pass_mark']}%. Course setting: {A['pass_mark']}%.",
                         fix="Use the same pass mark on the rules screen and in the course settings.")
        self.col.ran("assessment")
        ctx.close()

    # ---------------------------------------------------------------- slow: right answers found by trying each option
    def _assessment_slow(self, browser, rules, quiz, results):
        ctx = browser.new_context(viewport={"width": 1366, "height": 768}, device_scale_factor=2)
        page = self.open(ctx, "assessment")
        rules_shot = ""
        if rules and self.goto_slide(page, rules[0].id) == rules[0].id:
            self.settle_timeline(page)
            rules_shot = self.shot(page, f"{rules[0].id}_rules")
        A = self.assessment = {"questions": [], "scenarios": [], "rules": {}, "pass_mark": None,
                               "rules_ids": [s.id for s in rules], "quiz_ids": [s.id for s in quiz],
                               "results_ids": [s.id for s in results]}
        rules_text = "\n".join("\n".join(s.texts) for s in rules)
        A["rules"] = stated_rules(rules_text)
        scoring = next((x for x in self.course.scorings if x.get("type") not in (None, "view")), None) or {}
        A["pass_mark"] = scoring.get("passPercent")
        # progress: ~4 options per question, then the full attempts (each answers every asked question)
        asked = self.course.asked(quiz) if hasattr(self.course, "asked") else len(quiz)
        runs = 4 + self.attempts_to_test(A["rules"])
        total_steps = sum(4 for _ in quiz) + 4 + runs * asked
        step = 0
        self._aprog = {"base": sum(4 for _ in quiz) + 4, "total": total_steps, "n": 0}
        # 1. every option of every question: can it be selected, and what feedback does it give
        for q in quiz:
            self.check_time()
            qi = {"sid": q.id, "label": q.label, "options": [], "kind": "choice", "shot": ""}
            A["questions"].append(qi)
            if not self.open_question(page, q):
                qi["kind"] = "not reached"
                continue
            self.settle_timeline(page)
            qi["shot"] = self.shot(page, f"{q.id}_question")
            opts = self._options(page)
            if not opts:
                qi["kind"] = "other"   # drag and drop, hotspot, typing: options cannot be listed automatically
                continue
            for i in range(len(opts)):
                self.check_time()
                step += 1
                self.tick("assessment", min(step, total_steps - 1), total_steps)
                if i:
                    self.fresh(page)
                    if not self.open_question(page, q):
                        break
                    self.settle_timeline(page)
                o, ok = self.select_option(page, i)
                if o is None:
                    break
                rec = {"label": o["label"] or f"Option {i + 1}", "selectable": ok, "feedback": "", "shot": "",
                       "box": o["box"]}
                qi["options"].append(rec)
                if not ok:
                    rec["shot"] = self.shot(page, f"{q.id}_opt{i}")
                    self.col.add("assessment", "error", "Answer option cannot be selected", slide_id=q.id,
                                 slide_label=q.label, detail=f"Option: “{rec['label']}”", screenshot=rec["shot"], box=o["box"], vw=1366,
                                 found_in="Assessment",
                                 fix="Make this option clickable so the learner can choose it.")
                    continue
                fb, text = self.submit_answer(page)
                rec["feedback"], rec["feedback_text"] = fb, text
                rec["shot"] = self.shot(page, f"{q.id}_fb{i}")
                try:   # feedback text, so language issues in it can be shown where it appears
                    from .player import TEXTBOX_JS
                    A.setdefault("layer_shots", {}).setdefault(q.id, []).append(
                        {"shot": rec["shot"], "boxes": page.evaluate(TEXTBOX_JS)})
                except Exception:
                    pass
            sel = [r for r in qi["options"] if r["selectable"]]
            qi["key"] = [k for k, r in enumerate(qi["options"]) if r["feedback"] == "Correct"]
            if sel and all(r["feedback"] == "No feedback" for r in sel):
                qi["note"] = "No feedback was shown after Submit"
            elif sel and not qi["key"] and any(r["feedback"] == "Incorrect" for r in sel):
                self.col.add("assessment", "error", "No answer option is accepted as correct", slide_id=q.id, slide_label=q.label,
                             detail="Every option gave the “incorrect” feedback.", screenshot=sel[0]["shot"], vw=1366,
                             found_in="Assessment", fix="Mark the right answer as correct in the question settings.")
        # 2. Submit with nothing selected
        q0 = quiz[0]
        self.fresh(page)
        if self.open_question(page, q0) and self._options(page):
            self.settle_timeline(page)
            fb, text = self.submit_answer(page)
            here = self.identify(self.settle(page, 400))
            shot = self.shot(page, f"{q0.id}_blank")
            # stays on the question with no feedback, or Submit is not offered until an option is chosen
            ok = fb == "Answer required" or (here == q0.id and fb not in ("Correct", "Incorrect"))
            A["scenarios"].append({"name": "Submit without choosing an answer",
                                   "steps": f"Open {q0.label}, press Submit without selecting any option.",
                                   "expected": "The learner is asked to choose an answer and stays on the question.",
                                   "actual": {"Answer required": "A message asks to choose an answer."}.get(fb,
                                             ("Stayed on the question; Submit does nothing until an option is chosen." if fb in ("No feedback", "No Submit button")
                                              else f"Stayed on the question but showed “{fb}” feedback.") if here == q0.id
                                             else f"Moved on to {self.label(here)}."),
                                   "result": "Pass" if ok else "Fail", "shot": shot})
            if not ok:
                self.col.add("assessment", "error", "The question can be submitted without choosing an answer", slide_id=q0.id,
                             slide_label=q0.label, screenshot=shot, vw=1366, found_in="Assessment",
                             fix="Ask the learner to choose an answer before Submit is accepted.")
        # 3. full attempts: the fixed plan (scores 100, 80, 60, 0; then every attempt the rules allow)
        keys = {qi["sid"]: qi.get("key") or [] for qi in A["questions"]}
        nopt = {qi["sid"]: len(qi["options"]) for qi in A["questions"]}
        no_key = [q.label for q in quiz if not keys.get(q.id)]
        no_wrong = [q.label for q in quiz if not nopt.get(q.id, 0) > len(keys.get(q.id) or [])]
        can_right = (not no_key, "the right answer could not be found from the feedback for " + ", ".join(no_key[:4]) +
                     (" …" if len(no_key) > 4 else ""))
        can_wrong = (not no_wrong, "a wrong option could not be found for " + ", ".join(no_wrong[:4]) + (" …" if len(no_wrong) > 4 else ""))
        pm = A["pass_mark"] if A["pass_mark"] is not None else A["rules"].get("pass_mark")
        self.strict_plan(A, quiz, results,
                         lambda pct, run_no, retry_box=None: self._attempt(page, quiz, pct, keys, nopt, page_ready=bool(retry_box),
                                                                         retry_box=retry_box),
                         pm, asked, can_right=can_right, can_wrong=can_wrong)
        # 4. the rules screen against the course
        n_rules = A["rules"].get("questions")
        n_asked = self.course.asked(quiz) if hasattr(self.course, "asked") else len(quiz)
        if n_rules and n_rules != n_asked:
            sid = rules[0].id if rules else quiz[0].id
            self.col.add("assessment", "error", "Number of questions is different from the rules screen", slide_id=sid,
                         slide_label=self.label(sid), screenshot=rules_shot, vw=1366, found_in="Assessment",
                         detail=f"Rules screen: {n_rules} questions. Assessment asks: {n_asked}.",
                         fix="Make the number of questions match the rules screen.")
        if A["rules"].get("pass_mark") and A["pass_mark"] is not None and A["rules"]["pass_mark"] != A["pass_mark"]:
            sid = rules[0].id if rules else quiz[0].id
            self.col.add("assessment", "error", "Pass mark on the rules screen is different from the course setting", slide_id=sid,
                         slide_label=self.label(sid), screenshot=rules_shot, vw=1366, found_in="Assessment",
                         detail=f"Rules screen: {A['rules']['pass_mark']}%. Course setting: {A['pass_mark']}%.",
                         fix="Use the same pass mark on the rules screen and in the course settings.")
        self.col.ran("assessment")
        ctx.close()

    def is_first_question(self, sid, quiz):
        """The attempt starts on the first question; with a question bank, on any question the bank draws."""
        if not sid or not quiz:
            return False
        if quiz[0].draw:
            q = self.course.by_id(sid)
            return bool(q and q.draw == quiz[0].draw)
        return sid == quiz[0].id

    def open_question(self, page, q):
        """Opens one question. A question bank draws its questions when the course loads, so a question
        not drawn this time can be opened only in a new session (a few tries)."""
        if self.goto_slide(page, q.id) == q.id:
            return True
        if not q.draw:
            return False
        for _ in range(5):
            self.fresh(page)
            if self.jump(page, q.id) == q.id:
                return True
        return False

    def start_quiz(self, page, quiz):
        """Opens the assessment at its start. A question bank is opened through its draw, so the player
        picks the questions and scores the attempt as it would for a learner."""
        d = self.course.draws.get(quiz[0].draw) if quiz and quiz[0].draw else None
        if d:
            try:
                page.evaluate(self.JUMP_JS, d["abs"])
            except Exception:
                return None
            sid = self.identify(self.settle(page))
            return sid if self.is_first_question(sid, quiz) else None
        return quiz[0].id if self.goto_slide(page, quiz[0].id) == quiz[0].id else None

    def _attempt(self, page, quiz, mode, keys, nopt, page_ready=False, retry_box=None):
        """Plays the assessment from the first question. mode 'right' or 'wrong'. Returns what happened at the end."""
        out = {"answers": [], "end_sid": "", "text": "", "shot": "", "lms": {}, "retry": None, "start_sid": "", "cleared": True}
        if page_ready and retry_box:
            self.click_box(page, retry_box)
            st = self.settle(page)
            sid = self.identify(st)
            out["start_sid"] = sid
            out["start_shot"] = self.shot(page, f"retry_start_{sid}")
            out["cleared"] = not any(o.get("checked") for o in self._options(page))
            if not self.is_first_question(sid, quiz):
                return out
        else:
            self.fresh(page)
            sid = self.start_quiz(page, quiz)
            if not sid:
                out["end_sid"] = self.identify(self.state(page))
                return out
            out["start_sid"] = sid
        qids = {q.id for q in quiz}
        n_right = self.n_right(mode, self.course.asked(quiz))
        for _ in range(len(quiz) * 2 + 2):
            self.check_time()
            if sid not in qids:
                break
            self.settle_timeline(page)
            key = keys.get(sid) or []
            n = nopt.get(sid, 0)
            rt = len(out["answers"]) < n_right
            pick = key[0] if rt and key else next((k for k in range(n) if k not in key), 0)
            self.select_option(page, pick)
            fb, _ = self.submit_answer(page)
            out["answers"].append((sid, pick, fb))
            new = self.leave_feedback(page, sid)
            if new == sid:   # a second try on the same question (attempts per question): answer the same way again
                self.select_option(page, pick)
                self.submit_answer(page)
                new = self.leave_feedback(page, sid)
                if new == sid:
                    break
            sid = new
            ap = getattr(self, "_aprog", None)
            if ap:
                ap["n"] += 1
                self.tick("assessment", min(ap["base"] + ap["n"], ap["total"] - 1), ap["total"])
        page.wait_for_timeout(2500)   # the result is sent to the LMS a moment after the results screen opens
        out["end_sid"] = sid
        out["text"] = self._screen_text(page)[:1500]
        out["shot"] = self.shot(page, f"result_{mode}_{len(out['answers'])}_{sid}")
        lms = page.evaluate(LMS_JS) or {}
        store = lms.get("store") or {}
        g = lambda *ks: next((store[k] for k in ks if store.get(k)), "")
        out["lms"] = {"score": g("cmi.core.score.raw", "cmi.score.raw"), "score_max": g("cmi.core.score.max", "cmi.score.max"),
                      "status": g("cmi.core.lesson_status", "cmi.success_status", "cmi.completion_status"),
                      "success": g("cmi.success_status"), "completion": g("cmi.completion_status")}
        rb = None
        for o in self._clickables(page):
            if RETRY_RE.match(o.get("label") or ""):
                rb = self._obox(o)
                break
        if rb is None:
            try:
                b = page.get_by_role("button", name=re.compile(r"^\s*(retry|retake|try again)", re.I))
                if b.count() and b.first.is_visible():
                    bb = b.first.bounding_box()
                    rb = [bb["x"], bb["y"], bb["width"], bb["height"]] if bb else None
            except Exception:
                pass
        out["retry"] = rb
        return out

    def _retake_course(self, r, r2, tries, quiz):
        """After the last allowed attempt many courses offer RETAKE to start the whole course again (not the
        assessment): that is by design. True (and the scenario is recorded as passed) when the button opened a
        screen before the assessment and either the screen says so ("retake the course") or a retry already worked."""
        ids = [s.id for s in self.course.slides]
        start = r2.get("start_sid") or ""
        if start not in ids or not quiz or ids.index(start) >= ids.index(quiz[0].id):
            return False
        says_course = re.search(r"\b(retake|restart|start)\b.{0,30}\b(course|module)\b", r.get("text", ""), re.I)
        if not (says_course or tries > 2):
            return False
        self.assessment["scenarios"].append({
            "name": "Retake after the last attempt", "steps": "Use up every attempt, then press Retake on the results screen.",
            "expected": "The attempts are used up: the course starts again.",
            "actual": f"Opened {self.label(start)} (the course starts again).", "result": "Pass", "shot": r2.get("start_shot", "")})
        return True

    # ---------------------------------------------------------------- the fixed plan (every stage, never changed)
    def n_right(self, pct, asked):
        """How many of the asked questions are answered right for a target score (60% of 10 = 6)."""
        if isinstance(pct, str):
            pct = 100 if pct == "right" else 0
        return int(pct * asked / 100 + 0.5) if asked else 0

    def attempts_to_test(self, rules):
        """Attempts played in the attempts part of the plan: every attempt the rules screen allows
        (at most 10); 3 when the rules screen states no number or says the attempts are unlimited."""
        n = rules.get("attempts")
        return min(n, 10) if n else 3

    def _clear_ticks(self, page):
        """Select-all-that-apply: untick every ticked option before answering."""
        for o in self._options(page):
            if o.get("checked"):
                b = o["box"]
                page.mouse.click(b[0] + min(b[2] / 2, 24), b[1] + b[3] / 2)
                page.wait_for_timeout(300)

    def _not_tested(self, A, name, steps, expected, why):
        A["scenarios"].append({"name": name, "steps": steps, "expected": expected, "actual": f"Not run: {why}.",
                               "result": "Not tested", "shot": ""})

    def strict_plan(self, A, quiz, results, run, pm, asked, can_right, can_wrong):
        """THE ASSESSMENT PLAN – the same rows, in the same order, in every stage and for every course:
          1. Score 100%, 2. Score 80%, 3. Score 60%, 4. Score 0%   (each in a new learner session; the first questions
             asked are answered right, the rest wrong; pass or fail as per the pass mark; score shown and sent to the LMS)
          5. Retry – attempt 2 … N   (from the 0% attempt: Retry, then 0% again, for every attempt the rules screen allows)
          6. After the last attempt   (no more Retry; Retake / restart as the rules screen says)
          7. Number of attempts       (allowed by the course vs the rules screen)
        A row that cannot be run is reported as "Not tested" with the reason; no row is ever left out."""
        graded = bool(results)
        no_res = "there is no graded assessment with a results screen in this course (only the questions were tested)"
        r0 = None
        for i, pct in enumerate(SCORES):
            k = self.n_right(pct, asked)
            exp = round(100 * k / asked) if asked else pct
            name = f"Score {pct}%"
            steps = (f"New learner session. Answer {k} of {asked} questions right and {asked - k} wrong "
                     "(right answers first, in the order the questions are asked), then open the results screen.")
            want = ("pass" if exp >= pm else "fail") if pm is not None else ("pass" if pct == 100 else "fail" if pct == 0 else None)
            expected = f"Score {exp}%" + (f" – {'Pass' if want == 'pass' else 'Fail'}" if want else "") + \
                       (f" (pass mark {pm}%)" if pm is not None else "")
            if not graded:
                self._not_tested(A, name, steps, expected, no_res)
                continue
            if k and not can_right[0]:
                self._not_tested(A, name, steps, expected, can_right[1])
                continue
            if k < asked and not can_wrong[0]:
                self._not_tested(A, name, steps, expected, can_wrong[1])
                continue
            r = run(pct, i)
            if r is None:
                self._not_tested(A, name, steps, expected, "the attempt could not be started")
                continue
            self._judge(A, r, name, want, pm, quiz, expect_pct=exp, steps=steps)
            if pct == 0:
                r0 = r
        self._attempts_plan(A, quiz, run, r0, graded)

    def _attempts_plan(self, A, quiz, run, r, graded):
        rules = A["rules"]
        n = rules.get("attempts")            # None: not stated, 0: unlimited, N: N attempts
        target = self.attempts_to_test(rules)
        instr = rules.get("instruction") or ""
        says = (f"The rules screen says {n} attempt{'s' if n != 1 else ''}" if n else
                "The rules screen says the attempts are unlimited" if n == 0 else "The rules screen does not state the number of attempts")
        if n is None and rules.get("retake_allowed"):   # the instruction promises a retake, with no limit
            n, says = 0, "The rules screen says the assessment can be taken again (no limit stated)"
        retry_steps = "On the results screen press Retry (or Retake), then answer every question wrong again."
        rows = [f"Retry – attempt {t}" for t in range(2, target + 1)]
        capped = bool(n) and n > target   # more than 10 attempts allowed: the first 10 are tested
        stop = None
        if not graded:
            stop = "there is no graded assessment with a results screen in this course"
        elif r is None or r.get("blocked") or r.get("end_sid") not in A["results_ids"]:
            stop = "the Score 0% attempt did not reach the results screen"
        seen, extra, after_shot, no_retry = 1, False, (r or {}).get("shot", ""), None
        t = 2
        while not stop and t <= target:
            name = f"Retry – attempt {t}"
            left = self._attempts_left(r.get("text", ""))
            left_note = ""
            if left is not None and n:
                ok_left = left == n - (t - 1)
                left_note = f" The results screen says {left} attempt{'s' if left != 1 else ''} left" + ("." if ok_left else f", expected {n - (t - 1)}.")
                if not ok_left:
                    self.col.add("assessment", "error", "Attempts left shown on the results screen is wrong", slide_id=r["end_sid"],
                                 slide_label=self.label(r["end_sid"]), screenshot=r.get("shot", ""), vw=1366, found_in="Assessment",
                                 detail=f"After attempt {t - 1} of {n}: shows {left} left, expected {n - (t - 1)}.",
                                 fix="Show the right number of attempts left.")
            if r.get("retry") is None:
                ok = n is None   # no number on the rules screen: nothing promised
                no_retry = t - 1
                A["scenarios"].append({"name": name, "steps": retry_steps,
                                       "expected": f"Retry is offered ({says.lower()}).",
                                       "actual": f"No Retry on the results screen after attempt {t - 1}." + left_note,
                                       "result": "N/A" if ok else "Fail", "shot": r.get("shot", "")})
                if n is not None:
                    self.col.add("assessment", "error", "Retry is not offered although attempts remain", slide_id=r["end_sid"],
                                 slide_label=self.label(r["end_sid"]), screenshot=r.get("shot", ""), vw=1366, found_in="Assessment",
                                 detail=f"{says}; no Retry after attempt {t - 1}.", fix=f"Offer Retry until all {n} attempts are used.")
                stop = f"no Retry was offered after attempt {t - 1}"
                t += 1
                break
            r2 = run(0, t - 1, retry_box=r["retry"])
            landed = self.is_first_question(r2.get("start_sid"), quiz)
            cleared = r2.get("cleared", True)
            A["scenarios"].append({"name": name, "steps": retry_steps,
                                   "expected": f"The assessment starts again at the first question with no answers selected ({says.lower()}).",
                                   "actual": f"Opened {self.label(r2.get('start_sid'))}" + ("" if cleared else ", previous answers still selected") +
                                             "." + left_note,
                                   "result": "Pass" if landed and cleared else "Fail", "shot": r2.get("start_shot", "")})
            if not landed:
                self.col.add("assessment", "error", "Retry does not start the assessment again", slide_id=r["end_sid"],
                             slide_label=self.label(r["end_sid"]), screenshot=r.get("shot", ""), vw=1366, found_in="Assessment",
                             detail=f"Retry after attempt {t - 1} opened {self.label(r2.get('start_sid'))}.",
                             fix="Retry should open the first question of the assessment.")
                stop = f"Retry after attempt {t - 1} did not start the assessment again"
                t += 1
                break
            if not cleared:
                self.col.add("assessment", "error", "Previous answers are still selected after Retry", slide_id=r2.get("start_sid"),
                             slide_label=self.label(r2.get("start_sid")), screenshot=r2.get("start_shot", ""), vw=1366,
                             found_in="Assessment", fix="Reset the questions when the learner retries the assessment.")
            self._judge(A, r2, f"Attempt {t} – score 0%", "fail", None, quiz, expect_pct=0,
                        steps=f"Attempt {t}: answer every question wrong, then open the results screen.")
            seen, r, after_shot = t, r2, r2.get("shot", "")
            if r2.get("blocked") or r2.get("end_sid") not in A["results_ids"]:
                stop = f"attempt {t} did not reach the results screen"
                t += 1
                break
            t += 1
        for name in rows[t - 2:]:
            self._not_tested(A, name, retry_steps, "Retry starts the assessment again.", stop or "—")
        # after the last attempt
        name = "After the last attempt"
        if n:
            exp_last = (f"After attempt {n} Retry is no longer offered" +
                        ("; Retake starts the course again, as the rules screen says." if self._retake_course_said(instr) else ".") +
                        (f" Rules screen: “{instr}”" if instr else ""))
        elif n == 0:
            exp_last = f"Retry is still offered after attempt {target} ({says[0].lower() + says[1:]})." + (f" Rules screen: “{instr}”" if instr else "")
        else:
            exp_last = f"{says}: what the results screen offers after attempt {target} is recorded." + (f" Rules screen: “{instr}”" if instr else "")
        if no_retry and n is None:
            A["scenarios"].append({"name": name, "steps": f"After attempt {no_retry}, look at the results screen.", "expected": exp_last,
                                   "actual": f"No Retry after attempt {no_retry}: the course allows {no_retry} attempt{'s' if no_retry != 1 else ''}.",
                                   "result": "N/A", "shot": after_shot})
        elif stop or capped:
            self._not_tested(A, name, "Use up the attempts, then look at the results screen.", exp_last,
                             stop or f"the rules screen allows {n} attempts; the first {target} were tested")
        elif n:
            if r.get("retry") is None:
                A["scenarios"].append({"name": name, "steps": f"After attempt {n}, look at the results screen.", "expected": exp_last,
                                       "actual": "No Retry is offered: the attempts are used up.", "result": "Pass", "shot": after_shot})
            else:
                r3 = run(0, n, retry_box=r["retry"])
                start = r3.get("start_sid") or ""
                ids = [x.id for x in self.course.slides]
                if self.is_first_question(start, quiz):
                    extra = True
                    A["scenarios"].append({"name": name, "steps": f"After attempt {n}, press Retry / Retake.", "expected": exp_last,
                                           "actual": f"Another attempt started ({self.label(start)}).", "result": "Fail",
                                           "shot": r3.get("start_shot", "")})
                    self.col.add("assessment", "error", "More attempts are allowed than the rules screen says", slide_id=r["end_sid"],
                                 slide_label=self.label(r["end_sid"]), screenshot=after_shot, vw=1366, found_in="Assessment",
                                 detail=f"{says}; attempt {n + 1} could be started.", fix=f"Allow exactly {n} attempts, or correct the rules screen.")
                elif start in ids and quiz and ids.index(start) < ids.index(quiz[0].id):
                    A["scenarios"].append({"name": name, "steps": f"After attempt {n}, press Retake.", "expected": exp_last,
                                           "actual": f"Retake opened {self.label(start)}: the course starts again.", "result": "Pass",
                                           "shot": r3.get("start_shot", "")})
                else:
                    A["scenarios"].append({"name": name, "steps": f"After attempt {n}, press the Retry / Retake button.", "expected": exp_last,
                                           "actual": f"The button opened {self.label(start) if start else 'nothing'}.", "result": "Fail",
                                           "shot": r3.get("start_shot", "")})
                    self.col.add("assessment", "error", "The button after the last attempt does not do what the rules screen says",
                                 slide_id=r["end_sid"], slide_label=self.label(r["end_sid"]), screenshot=after_shot, vw=1366,
                                 found_in="Assessment", detail=f"Opened {self.label(start) if start else 'nothing'}.",
                                 fix="After the last attempt, remove Retry or let Retake start the course again.")
        else:
            has = r.get("retry") is not None
            ok = has if n == 0 else None
            A["scenarios"].append({"name": name, "steps": f"After attempt {target}, look at the results screen.", "expected": exp_last,
                                   "actual": "Retry is still offered." if has else "No Retry is offered.",
                                   "result": "N/A" if ok is None else ("Pass" if ok else "Fail"), "shot": after_shot})
            if ok is False:
                self.col.add("assessment", "error", "Retry is not offered although the rules screen allows more attempts", slide_id=r["end_sid"],
                             slide_label=self.label(r["end_sid"]), screenshot=after_shot, vw=1366, found_in="Assessment",
                             detail=f"No Retry after attempt {target}.", fix="Offer Retry after every attempt, as the rules screen says.")
        # number of attempts
        A["attempts_seen"] = (f"{seen}+" if extra else seen) if not stop or seen > 1 else None
        name = "Number of attempts"
        steps = "Fail the assessment and press Retry until it is no longer offered."
        if capped and not stop:
            A["scenarios"].append({"name": name, "steps": steps, "expected": f"{n} attempts (rules screen).",
                                   "actual": f"At least {seen} attempts allowed (tested up to {target}).", "result": "Pass", "shot": after_shot})
        elif n and not stop:
            ok = seen == n and not extra
            A["scenarios"].append({"name": name, "steps": steps, "expected": f"{n} attempt{'s' if n != 1 else ''} (rules screen).",
                                   "actual": f"{seen}{' or more' if extra else ''} attempt{'s' if seen != 1 or extra else ''} allowed.",
                                   "result": "Pass" if ok else "Fail", "shot": after_shot})
            if not ok and not extra:
                self.col.add("assessment", "error", "Number of attempts is different from the rules screen", slide_id=r["end_sid"],
                             slide_label=self.label(r["end_sid"]), screenshot=after_shot, vw=1366, found_in="Assessment",
                             detail=f"Rules screen: {n} attempts. Course allows: {seen}.", fix=f"Allow exactly {n} attempts, or correct the rules screen.")
        elif no_retry and n is None:
            A["scenarios"].append({"name": name, "steps": steps, "expected": says + ".",
                                   "actual": f"{seen} attempt{'s' if seen != 1 else ''} allowed.", "result": "N/A", "shot": after_shot})
        elif stop and not (n is not None and no_retry):
            self._not_tested(A, name, steps, says + ".", stop)
        elif stop:
            A["scenarios"].append({"name": name, "steps": steps, "expected": (f"{n} attempts" if n else "Unlimited attempts") + " (rules screen).",
                                   "actual": f"Only {seen} attempt{'s' if seen != 1 else ''} allowed.", "result": "Fail", "shot": after_shot})
        else:
            A["scenarios"].append({"name": name, "steps": steps, "expected": says + ".",
                                   "actual": f"At least {seen} attempts allowed (tested up to {target}).",
                                   "result": "Pass" if n == 0 else "N/A", "shot": after_shot})

    @staticmethod
    def _attempts_left(text):
        t = (text or "").lower()
        m = re.search(r"(\d+|no|" + "|".join(k for k in NUM_WORDS if k != "unlimited") + r")\s+(?:more\s+)?(?:attempts?|tries|chances)\s+(?:remaining|left)", t) \
            or re.search(r"(?:attempts?|tries|chances)\s+(?:remaining|left)\s*:?\s*(\d+)", t)
        if not m:
            return None
        v = m.group(1)
        return 0 if v == "no" else int(v) if v.isdigit() else NUM_WORDS.get(v)

    @staticmethod
    def _retake_course_said(instr):
        return bool(re.search(r"\b(retake|restart|redo|take)\b.{0,40}\b(course|module)\b", instr or "", re.I))

    def _judge(self, A, r, name, want, pass_mark, quiz, expect_pct=None, steps=None):
        """Compares one full attempt with what should happen: results screen reached, pass / fail as the pass
        mark says (want None: not judged), the score shown and the result sent to the LMS."""
        if expect_pct is None:
            expect_pct = 100 if want == "pass" else 0
        steps = steps or "Answer the questions, then open the results screen."
        if r.get("blocked"):
            A["scenarios"].append({"name": name, "steps": steps, "expected": f"Score {expect_pct}%",
                                   "actual": f"Not run to the end: {r['blocked']}.", "result": "Not tested", "shot": r.get("shot", "")})
            return
        results_ids = A["results_ids"]
        end = r.get("end_sid") or ""
        reached = end in results_ids if results_ids else (end and end not in {q.id for q in quiz})
        text = r.get("text", "")
        lms = r.get("lms") or {}
        status = (lms.get("success") or lms.get("status") or "").lower()
        # some courses skip the results screen on a pass and open the completion / thank-you screen with the pass message
        ids = [s.id for s in self.course.slides]
        after_quiz = end in ids and quiz and quiz[-1].id in ids and ids.index(end) > ids.index(quiz[-1].id)
        if not reached and after_quiz and want == "pass" and (PASS_RE.search(text) or status in ("passed", "completed")):
            reached = True
        nums = [int(x) for x in re.findall(r"(\d{1,3})\s*%", text)]
        shown = list(nums)
        if pass_mark is not None and pass_mark in shown and expect_pct != pass_mark:
            shown.remove(pass_mark)   # the pass mark printed on the results screen is not the learner's score
        problems = []
        if not reached:
            problems.append("The results screen did not open after the last question" + (f" (stopped on {self.label(end)})" if end else ""))
        else:
            if want == "pass" and not PASS_RE.search(text):
                problems.append("The results screen does not say the learner passed")
            if want == "fail" and not FAIL_RE.search(text):
                problems.append("The results screen does not say the learner failed")
            if shown and not any(abs(x - expect_pct) <= 1 for x in shown):
                problems.append(f"Score shown is {shown[0]}%, expected {expect_pct}%")
            if lms and any(lms.values()):
                if want == "pass" and status not in ("passed", "completed"):
                    problems.append(f"LMS status is “{status or 'not sent'}”, expected “passed”")
                if want == "fail" and status in ("passed",):
                    problems.append(f"LMS status is “passed” although the score ({expect_pct}%) is below the pass mark")
                try:
                    raw, mx = float(lms.get("score") or "nan"), float(lms.get("score_max") or "nan")
                    if mx > 0 and abs(100 * raw / mx - expect_pct) > 1:
                        problems.append(f"LMS score is {lms['score']} of {lms['score_max']} ({round(100 * raw / mx)}%), expected {expect_pct}%")
                except ValueError:
                    pass
        expected = f"Results screen shows {expect_pct}%" + \
                   ({"pass": " and says the learner passed", "fail": " and says the learner failed"}.get(want, "")) + \
                   (f" (pass mark {pass_mark}%)" if pass_mark is not None else "") + "; result sent to the LMS"
        actual = ((f"Results screen: “{' '.join(text.split()[:30])}”" if end in results_ids or not results_ids
                   else f"{self.label(end)}: “{' '.join(text.split()[:30])}”") if reached else "Results screen not reached") + \
                 (f"; LMS: status {status or 'not sent'}, score {lms.get('score') or '—'}" + (f"/{lms['score_max']}" if lms.get("score_max") else "") if lms else "")
        A["scenarios"].append({"name": name, "steps": steps + f" ({len(r['answers'])} answers given)",
                               "expected": expected, "actual": actual, "result": "Fail" if problems else "Pass",
                               "shot": r.get("shot", ""), "lms": lms})
        for p in problems:
            self.col.add("assessment", "error", p, slide_id=end if reached else (quiz[-1].id if quiz else ""),
                         slide_label=self.label(end if reached else quiz[-1].id), screenshot=r.get("shot", ""), vw=1366,
                         detail=f"Scenario: {name}.", found_in="Assessment",
                         fix="Check the results screen and the quiz reporting settings (Publish → LMS) so the result is shown and reported correctly.")
