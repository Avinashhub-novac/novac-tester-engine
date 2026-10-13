"""Progress bar and time-left estimate for a run.

The engine plans how long each phase should take (from the number of slides and their running time),
then corrects the plan with how fast the finished phases really went. Everything is kept in
progress.json in the run folder; `python3 -m beta.cli progress` reads it for the website.

Overall phases:  preparing → engine phases (browser passes / video checks) → review (Claude reads the
text and the storyboard) → report (screenshots and Excel) → upload (to the site) → done.
"""
import json
import time
from pathlib import Path

LABELS = {
    "prepare": "Preparing", "extract": "Opening the course", "desktop": "Playing every screen",
    "restrict": "Checking restrictions", "prev": "Checking Back buttons", "menu": "Checking the menu",
    "buttons": "Clicking every button", "mobile": "Testing on mobile", "analyse": "Collecting results",
    "video": "Checking the video", "review": "Reviewing text and storyboard", "report": "Making screenshots and report",
    "upload": "Saving the report to the site", "sb": "Reviewing the storyboard",
}


def plan_scorm(slides, passes, play_full, has_sb, transcript):
    """Planned seconds for each phase of a course test."""
    n = max(1, len(slides))
    play = sum(min(s.duration_ms, 600000) for s in slides) / 1000.0
    words = sum(len(" ".join(s.texts).split()) + len((s.notes or "").split()) for s in slides)
    p = [("extract", 3 + n * 0.2)]
    est = {   # seconds, measured on sample courses
        "desktop": (play if play_full else n * 2) + n * 4 + 15,
        "restrict": 15 + min(8, n) * 4,
        "prev": n * 2.5 + 5,
        "menu": n * 3 + 5,
        "buttons": n * 7 + 10,
        "mobile": (play * 0.8 if play_full else n * 2) + n * 7 + 10,
    }
    for name in passes:
        p.append((name, est.get(name, n * 5)))
    p.append(("analyse", 10 + n * 0.5))
    p += _post(n, words, has_sb, transcript)
    return p


def plan_video(duration_s, cues, has_sb):
    p = [("video", 30 + duration_s * 0.25 + cues * 2.5), ("analyse", 10)]
    p += _post(max(1, cues // 4), cues * 12, has_sb, True)
    return p


def _post(n, words, has_sb, transcript):
    # the background Claude session: reading the text, then screenshots/report, then uploads
    review = 60 + words * 0.06 + (150 + n * 6 if has_sb else 0) + (n * 2 if transcript else 0)
    return [("review", review), ("report", 30 + n * 2), ("upload", 60 + n * 3)]


class Progress:
    def __init__(self, run_dir: Path):
        self.file = Path(run_dir) / "progress.json"
        self.state = {"plan": [], "phase": "prepare", "frac": 0.0, "done": {}, "phase_started": time.time(),
                      "started": time.time()}
        if self.file.exists():
            try:
                self.state.update(json.loads(self.file.read_text()))
            except Exception:
                pass

    def save(self):
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        tmp.replace(self.file)

    def set_plan(self, plan):
        self.state["plan"] = [[k, float(v)] for k, v in plan]
        self.save()

    def phase(self, name, frac=0.0):
        st = self.state
        if name != st["phase"]:
            if st["phase"] in dict(st["plan"]):
                st["done"][st["phase"]] = time.time() - st["phase_started"]
            st["phase"], st["phase_started"] = name, time.time()
        st["frac"] = max(0.0, min(1.0, frac))
        self.save()

    def tick(self, name, done, total):
        """Progress inside a phase (e.g. slide 4 of 17)."""
        if name != self.state["phase"]:
            self.phase(name)
        self.state["frac"] = max(self.state["frac"], min(1.0, done / max(1, total)))
        self.save()

    def finish(self):
        self.phase("done", 1.0)

    def view(self):
        """{pct, eta_s, stage} for the website."""
        st, now = self.state, time.time()
        plan = st["plan"]
        if st["phase"] == "done":
            return {"pct": 100, "eta_s": 0, "stage": "Finished"}
        if not plan:
            return {"pct": 2, "eta_s": None, "stage": LABELS.get(st["phase"], st["phase"])}
        names = [k for k, _ in plan]
        planned = dict(plan)
        # how much slower or faster than planned the finished phases ran
        p_done = sum(planned[k] for k in st["done"] if k in planned)
        a_done = sum(v for k, v in st["done"].items() if k in planned)
        scale = min(3.0, max(0.5, a_done / p_done)) if p_done > 60 else 1.0
        cur = st["phase"]
        in_cur = now - st["phase_started"]
        if cur in planned:
            idx = names.index(cur)
            exp_cur = planned[cur] * scale
            # the slide count inside a phase is the better guide once it has moved
            left_cur = exp_cur * (1 - st["frac"]) if st["frac"] > 0.05 else max(exp_cur - in_cur, exp_cur * 0.15)
            if st["frac"] > 0.15 and in_cur > 30:
                left_cur = 0.5 * left_cur + 0.5 * in_cur * (1 - st["frac"]) / st["frac"]
            later = sum(planned[k] for k in names[idx + 1:]) * scale
        else:   # preparing: nothing planned yet
            left_cur, later = 0, sum(planned.values())
        eta = max(5, left_cur + later)
        elapsed = now - st["started"]
        pct = 100 * elapsed / (elapsed + eta)
        return {"pct": int(max(2, min(99, pct))), "eta_s": int(eta), "stage": LABELS.get(cur, cur)}


def plan_sb(pages, picture_pages):
    """SB stage: almost all the time is Claude reading the storyboard."""
    return [("extract", 20 + pages * 1.5), ("analyse", 10), ("review", 120 + pages * 25 + picture_pages * 10),
            ("report", 20 + pages * 2), ("upload", 40 + pages * 2)]
