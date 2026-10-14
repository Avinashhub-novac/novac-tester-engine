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
NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
             "unlimited": 0}


def assessment_parts(course):
    """(rules slides, question slides, results slides) of the course's assessment, in story order."""
    slides = course.slides
    quiz = [s for s in slides if s.is_quiz]
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
    t = (text or "").lower()
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
    return out


class QuizMixin:
    """Mixed into the Crawler."""

    # ---------------------------------------------------------------- one question
    def _options(self, page):
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
        shot = ""
        txt = new or after
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

    def answer_question(self, page, sid, i=0):
        """Desktop walk: answer with option i and go on. Returns the slide reached, or None when there are no options."""
        if not self._options(page):
            return None
        self.select_option(page, i)
        self.submit_answer(page)
        return self.leave_feedback(page, sid)

    # ---------------------------------------------------------------- the pass
    def fresh(self, page):
        """A new learner session: reload the course (the test LMS starts empty again)."""
        page.reload(wait_until="load", timeout=90000)
        self._ready(page)
        self.dismiss_prompts(page)

    def assessment_pass(self, browser):
        rules, quiz, results = assessment_parts(self.course)
        if not quiz:
            return
        ctx = browser.new_context(viewport={"width": 1366, "height": 768}, device_scale_factor=2)
        page = self.open(ctx, "assessment")
        A = self.assessment = {"questions": [], "scenarios": [], "rules": {}, "pass_mark": None,
                               "rules_ids": [s.id for s in rules], "quiz_ids": [s.id for s in quiz],
                               "results_ids": [s.id for s in results]}
        rules_text = " ".join(" ".join(s.texts) for s in rules)
        A["rules"] = stated_rules(rules_text)
        scoring = next((x for x in self.course.scorings if x.get("type") not in (None, "view")), None) or {}
        A["pass_mark"] = scoring.get("passPercent")
        total_steps = sum(4 for _ in quiz) + 4
        step = 0
        # 1. every option of every question: can it be selected, and what feedback does it give
        for q in quiz:
            self.check_time()
            qi = {"sid": q.id, "label": q.label, "options": [], "kind": "choice", "shot": ""}
            A["questions"].append(qi)
            if self.goto_slide(page, q.id) != q.id:
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
                    if self.goto_slide(page, q.id) != q.id:
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
                    self.visits.setdefault(q.id, {}).setdefault("layer_shots", []).append(
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
        if self.goto_slide(page, q0.id) == q0.id and self._options(page):
            self.settle_timeline(page)
            fb, text = self.submit_answer(page)
            here = self.identify(self.settle(page, 400))
            shot = self.shot(page, f"{q0.id}_blank")
            ok = fb == "Answer required" or (here == q0.id and fb == "No feedback")
            A["scenarios"].append({"name": "Submit without choosing an answer",
                                   "steps": f"Open {q0.label}, press Submit without selecting any option.",
                                   "expected": "The learner is asked to choose an answer and stays on the question.",
                                   "actual": {"Answer required": "A message asks to choose an answer."}.get(fb,
                                             "Stayed on the question." if here == q0.id else f"Moved on to {self.label(here)}."),
                                   "result": "Pass" if ok else "Fail", "shot": shot})
            if not ok:
                self.col.add("assessment", "error", "The question can be submitted without choosing an answer", slide_id=q0.id,
                             slide_label=q0.label, screenshot=shot, vw=1366, found_in="Assessment",
                             fix="Ask the learner to choose an answer before Submit is accepted.")
        # 3. full attempts (only for a graded assessment with a results screen)
        if not results:
            A["scenarios"].append({"name": "Full assessment (pass / fail / retry)", "steps": "—",
                                   "expected": "—", "actual": "No graded assessment with a results screen in this course: "
                                   "only the question screens were tested.", "result": "N/A", "shot": ""})
            self.col.ran("assessment")
            ctx.close()
            return
        keys = {qi["sid"]: qi.get("key") or [] for qi in A["questions"]}
        nopt = {qi["sid"]: len(qi["options"]) for qi in A["questions"]}
        can_right = all(keys.get(q.id) for q in quiz)
        can_wrong = all(nopt.get(q.id, 0) > len(keys.get(q.id) or []) for q in quiz)
        attempts = A["rules"].get("attempts")
        pm = A["pass_mark"] if A["pass_mark"] is not None else A["rules"].get("pass_mark")
        if can_right:
            r = self._attempt(page, quiz, "right", keys, nopt)
            self._judge(A, r, "All answers correct", "pass", pm, quiz)
        else:
            A["scenarios"].append({"name": "All answers correct", "steps": "Answer every question correctly.",
                                   "expected": "Pass", "actual": "Not run: the right answer of every question could not be found from the feedback.",
                                   "result": "Not tested", "shot": ""})
        if can_wrong:
            r = self._attempt(page, quiz, "wrong", keys, nopt)
            self._judge(A, r, "All answers wrong", "fail", pm, quiz)
            # retries
            if r.get("retry") is not None and r["end_sid"] in A["results_ids"]:
                tries = 1
                while r.get("retry") and tries < 6:
                    tries += 1
                    r2 = self._attempt(page, quiz, "wrong", keys, nopt, page_ready=True, retry_box=r["retry"])
                    landed_ok = r2.get("start_sid") == quiz[0].id
                    A["scenarios"].append({"name": f"Retry – attempt {tries}",
                                           "steps": "On the results screen press Retry.",
                                           "expected": f"The assessment starts again at {quiz[0].label} with no answers selected.",
                                           "actual": f"Opened {self.label(r2.get('start_sid'))}" + ("" if r2.get("cleared", True) else ", previous answers still selected") + ".",
                                           "result": "Pass" if landed_ok and r2.get("cleared", True) else "Fail", "shot": r2.get("start_shot", "")})
                    if not landed_ok:
                        self.col.add("assessment", "error", "Retry does not start the assessment again", slide_id=r["end_sid"],
                                     slide_label=self.label(r["end_sid"]), screenshot=r.get("shot", ""), vw=1366, found_in="Assessment",
                                     detail=f"Retry opened {self.label(r2.get('start_sid'))}",
                                     fix=f"Retry should open {quiz[0].label}.")
                        break
                    r = r2
                A["attempts_seen"] = tries
                if attempts:
                    ok = tries == attempts
                    A["scenarios"].append({"name": "Number of attempts",
                                           "steps": "Fail the assessment and press Retry until it is no longer offered.",
                                           "expected": f"{attempts} attempt{'s' if attempts != 1 else ''} (as the rules screen says).",
                                           "actual": f"{tries} attempt{'s' if tries != 1 else ''} allowed.",
                                           "result": "Pass" if ok else "Fail", "shot": r.get("shot", "")})
                    if not ok:
                        self.col.add("assessment", "error", "Number of attempts is different from the rules screen",
                                     slide_id=r["end_sid"], slide_label=self.label(r["end_sid"]), screenshot=r.get("shot", ""), vw=1366,
                                     detail=f"Rules screen: {attempts} attempts. Course allows: {tries}.", found_in="Assessment",
                                     fix=f"Allow exactly {attempts} attempts, or correct the rules screen.")
        else:
            A["scenarios"].append({"name": "All answers wrong", "steps": "Answer every question wrongly.",
                                   "expected": "Fail", "actual": "Not run: a wrong option could not be found for every question.",
                                   "result": "Not tested", "shot": ""})
        # 4. the rules screen against the course
        n_rules = A["rules"].get("questions")
        if n_rules and n_rules != len(quiz):
            sid = rules[0].id if rules else quiz[0].id
            self.col.add("assessment", "error", "Number of questions is different from the rules screen", slide_id=sid,
                         slide_label=self.label(sid), screenshot=self.visits.get(sid, {}).get("shot", ""), vw=1366, found_in="Assessment",
                         detail=f"Rules screen: {n_rules} questions. Assessment has: {len(quiz)}.",
                         fix="Make the number of questions match the rules screen.")
        if A["rules"].get("pass_mark") and A["pass_mark"] is not None and A["rules"]["pass_mark"] != A["pass_mark"]:
            sid = rules[0].id if rules else quiz[0].id
            self.col.add("assessment", "error", "Pass mark on the rules screen is different from the course setting", slide_id=sid,
                         slide_label=self.label(sid), screenshot=self.visits.get(sid, {}).get("shot", ""), vw=1366, found_in="Assessment",
                         detail=f"Rules screen: {A['rules']['pass_mark']}%. Course setting: {A['pass_mark']}%.",
                         fix="Use the same pass mark on the rules screen and in the course settings.")
        self.col.ran("assessment")
        ctx.close()

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
            if sid != quiz[0].id:
                return out
        else:
            self.fresh(page)
            if self.goto_slide(page, quiz[0].id) != quiz[0].id:
                out["end_sid"] = self.identify(self.state(page))
                return out
            sid = quiz[0].id
            out["start_sid"] = sid
        qids = {q.id for q in quiz}
        for _ in range(len(quiz) * 2 + 2):
            self.check_time()
            if sid not in qids:
                break
            self.settle_timeline(page)
            key = keys.get(sid) or []
            n = nopt.get(sid, 0)
            pick = key[0] if mode == "right" else next((k for k in range(n) if k not in key), 0)
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

    def _judge(self, A, r, name, want, pass_mark, quiz):
        """Compares one full attempt with what should happen."""
        results_ids = A["results_ids"]
        end = r.get("end_sid") or ""
        reached = end in results_ids if results_ids else (end and end not in {q.id for q in quiz})
        text = r.get("text", "")
        lms = r.get("lms") or {}
        status = (lms.get("success") or lms.get("status") or "").lower()
        nums = [int(x) for x in re.findall(r"(\d{1,3})\s*%", text)]
        problems = []
        if not reached:
            problems.append("The results screen did not open after the last question" + (f" (stopped on {self.label(end)})" if end else ""))
        else:
            if want == "pass" and not PASS_RE.search(text):
                problems.append("The results screen does not say the learner passed")
            if want == "fail" and not FAIL_RE.search(text):
                problems.append("The results screen does not say the learner failed")
            if want == "pass" and nums and max(nums) < 100:
                problems.append(f"Score shown is {max(nums)}%, expected 100%")
            if want == "fail" and nums and min(nums) > 0 and all(r_[2] != "Correct" for r_ in r["answers"]):
                problems.append(f"Score shown is {min(nums)}%, expected 0%")
            if lms and any(lms.values()):
                if want == "pass" and status not in ("passed", "completed"):
                    problems.append(f"LMS status is “{status or 'not sent'}”, expected “passed”")
                if want == "fail" and status in ("passed",):
                    problems.append("LMS status is “passed” although every answer was wrong")
                if want == "pass" and lms.get("score") and lms.get("score_max") and lms["score"] != lms["score_max"]:
                    problems.append(f"LMS score is {lms['score']} of {lms['score_max']}, expected full marks")
        expected = ("Results screen says the learner passed (100%)" if want == "pass" else "Results screen says the learner failed (0%)") + \
                   (f"; pass mark {pass_mark}%" if pass_mark is not None else "") + "; result sent to the LMS"
        actual = (f"Results screen: “{' '.join(text.split()[:30])}”" if reached else "Results screen not reached") + \
                 (f"; LMS: status {status or 'not sent'}, score {lms.get('score') or '—'}" + (f"/{lms['score_max']}" if lms.get("score_max") else "") if lms else "")
        A["scenarios"].append({"name": name, "steps": ("Answer every question correctly" if want == "pass" else "Answer every question wrongly") +
                               f" ({len(r['answers'])} answers given), then open the results screen.",
                               "expected": expected, "actual": actual, "result": "Fail" if problems else "Pass",
                               "shot": r.get("shot", ""), "lms": lms})
        for p in problems:
            self.col.add("assessment", "error", p, slide_id=end if reached else (quiz[-1].id if quiz else ""),
                         slide_label=self.label(end if reached else quiz[-1].id), screenshot=r.get("shot", ""), vw=1366,
                         detail=f"Scenario: {name}.", found_in="Assessment",
                         fix="Check the results screen and the quiz reporting settings (Publish → LMS) so the result is shown and reported correctly.")
