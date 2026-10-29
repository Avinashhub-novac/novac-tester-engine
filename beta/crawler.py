"""Drives the published course in headless Chromium like a learner would.

Passes: desktop Next walk -> Prev walk -> Menu -> Buttons -> Mobile swipe.
Every finding goes into the shared Collector.
"""
import hashlib
import re
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from .player import (SEL, PLAYER_BUTTONS, VISITED_CLASSES, CURRENT_CLASSES, INIT_SCRIPT,
                     STATE_JS, FINGERPRINT_JS, CLICKABLES_JS, MENU_JS, TEXTBOX_JS, LMS_JS, TRANSCRIPT_JS, OVERLAP_JS, OVERFLOW_JS)
from .quiz import QuizMixin, assessment_parts

WORD = re.compile(r"[a-z0-9'\u0900-\u097f\u0a00-\u0a7f\u0b80-\u0bff\u0c00-\u0cff\u0d00-\u0d7f\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff]+")


def _norm(s):
    return " ".join(WORD.findall((s or "").lower()))


WELCOME_RE = re.compile(r"\b(welcome|start|begin|splash|launch|cover)\b", re.I)
END_RE = re.compile(r"\b(thank\s*you|thankyou|thanks|congratulations|the end|end of (the )?(course|module)|completion|conclusion)\b", re.I)


def is_welcome(slide):
    """The opening screen (welcome / title / START page). It is often left out of the menu and uses a START
    button instead of Next – both are normal and never reported."""
    if not slide:
        return False
    return slide.index == 1 or (slide.index == 2 and bool(WELCOME_RE.search(slide.title or "")))


def is_end(slide, n):
    """The closing screen (thank you / congratulations): like the welcome screen it carries no screen number."""
    if not slide:
        return False
    return slide.index == n or bool(END_RE.search(slide.title or ""))


class Deadline(Exception):
    pass


class Stopped(Exception):
    """Another pass found that the learner cannot go on (the walk-through stopped): this pass stops too."""


def _per_pass(name, default=None):
    """An attribute each test pass keeps for itself (passes can run side by side, one browser each)."""
    def get(self):
        return getattr(self._tl, name, default)

    def set_(self, v):
        setattr(self._tl, name, v)
    return property(get, set_)


class Crawler(QuizMixin):
    current = _per_pass("current")
    _slide_t0 = _per_pass("_slide_t0")
    _jumped = _per_pass("_jumped")
    _objs_before = _per_pass("_objs_before")
    _no_before = _per_pass("_no_before")
    _shot_words = _per_pass("_shot_words")
    _aprog = _per_pass("_aprog")
    pass_name = _per_pass("pass_name", "")
    _lane_pos = _per_pass("_lane_pos")
    _clicked_n = _per_pass("_clicked_n", 0)

    @property
    def learned(self):
        """(words on screen, slide id) for screens recognised so far. Each pass starts from what the
        walk-through learned (never from what a pass running beside it learned), so results do not depend on timing."""
        if getattr(self._tl, "learned", None) is None:
            self._tl.learned = list(self._learned_base)
        return self._tl.learned

    @learned.setter
    def learned(self, v):
        self._tl.learned = v

    def __init__(self, course, url, run_dir: Path, col, play_full=True, max_minutes=120, log=print,
                 max_buttons=25):
        import threading
        self._tl = threading.local()
        self._learned_base = []
        self.course, self.url, self.dir, self.col = course, url, run_dir, col
        self.play_full, self.log, self.max_buttons = play_full, log, max_buttons
        self.deadline = time.time() + max_minutes * 60
        self.shots = run_dir / "shots"
        self.shots.mkdir(parents=True, exist_ok=True)
        self.visits = {}          # slide id -> info gathered in the desktop pass
        self.order = []           # slide ids in the order Next reached them
        self.current = None
        self.bad_files = set()
        self.slide_tokens = {s.id: set(WORD.findall(s.all_text().lower())) for s in course.slides}
        self.menu_map = None      # menu item index -> slide id
        self.completion = None    # what the course reported to the LMS by the end of the desktop pass
        self.custom_nav = False   # the player has no Next button; slides carry their own navigation
        self.has_menu = None      # None = not checked yet
        self.expect = {}          # Beta: expected restrictions {'menu'|'seekbar'|'next': 'restricted'|'free'}
        self.restrictions = {}    # what the restrict pass observed
        self.on_progress = None   # callback(pass name, done, total) for the progress bar
        self.check_completion = True   # Beta only: read what the course reports to the LMS at the end
        self.blocked = None       # {sid, reason}: the learner cannot go past this screen, so testing stopped there
        self.dead_back = set()    # screens whose Back arrow does nothing (already reported as a Back failure)
        self.run_menu = True      # the menu is part of this test (set from the passes)
        self.menu_done = False    # the course's own menu was already tested at the end of the Next walk
        self.assessment = None    # what the assessment pass found (questions, options, scenarios)
        self.clicks = {}          # slide id -> clickables opened / closed and the Next state before and after
        rules, quiz, results = assessment_parts(course)
        self.quiz_ids = {s.id for s in quiz}
        self.assess_ids = {s.id for s in rules + quiz + results}
        self.results_ids = {s.id for s in results}
        self._build_index()

    # ------------------------------------------------------------ utilities
    def tick(self, name, done, total):
        pos = self._lane_pos
        if pos is not None and getattr(self, "_lane_frac", None) is not None:
            k, j, n = pos
            self._lane_frac[k] = max(self._lane_frac.get(k, 0.0), (j + min(1.0, done / max(1, total))) / n)
            self._report_wave()
            return
        if self.on_progress:
            try:
                self.on_progress(name, done, total)
            except Exception:
                pass

    def check_time(self):
        name = getattr(getattr(self, "_tl", None), "name", None) or self.pass_name
        if name == "assessment":
            # the assessment plan always runs to the end: it opens the questions directly, so a screen the
            # walk-through cannot pass does not stop it, and it has its own time allowance (at least 60 minutes)
            if time.time() > max(self.deadline, getattr(self, "assess_deadline", 0)):
                raise Deadline()
            return
        if time.time() > self.deadline:
            raise Deadline()
        if self.blocked and self.pass_name not in ("", "desktop"):
            raise Stopped()

    def label(self, sid):
        s = self.course.by_id(sid)
        return s.label if s else (sid or "Unknown slide")

    def state(self, page):
        try:
            return page.evaluate(STATE_JS, SEL)
        except Exception:
            return {"slideIds": [], "text": "", "title": "", "nextExists": False, "nextDisabled": True,
                    "prevExists": False, "prevDisabled": True, "seekVisible": False, "captions": [],
                    "media": [], "errors": [], "seekVal": None, "seekMax": None, "seekFill": None}

    def _build_index(self):
        import math
        n = max(1, len(self.course.slides))
        df = {}
        for toks in self.slide_tokens.values():
            for t in toks:
                df[t] = df.get(t, 0) + 1
        self.idf = {t: math.log(1 + n / c) for t, c in df.items()}
        self.obj2slide = {}
        for s in self.course.slides:
            for o in getattr(s, "object_ids", ()) or ():
                self.obj2slide[o] = s.id

    def identify(self, st, expected=None):
        """Which slide is on screen. Tries, in order: slide id in the page, ids of the slide's objects,
        the player address, the player's own state, the on-screen text, and finally the slide we expected
        to land on (after a Next/START click that visibly changed the screen)."""
        if not hasattr(self, "idf"):
            self._build_index()
        known = set(self.course.ids())
        if st.get("gid") in known:          # Storyline 360 reports the slide itself
            return st["gid"]
        ids = st.get("slideIds", []) or []
        hits = [i for i in ids if i in known]
        if hits:
            return hits[-1]
        votes = {}
        for i in ids:
            sid = self.obj2slide.get(i)
            if sid:
                votes[sid] = votes.get(sid, 0) + 1
        if votes:
            return max(votes.items(), key=lambda kv: kv[1])[0]
        for src in (st.get("gid", ""), st.get("hash", ""), st.get("url", "")):
            for tok in re.findall(r"[\w-]+", src or "")[::-1]:
                if tok in known:
                    return tok
        dom = (st.get("text", "") + " " + st.get("acc", "") + " " + st.get("title", "")).lower()
        toks = set(WORD.findall(dom))
        sid = self._identify_text(toks, st, expected, known)
        if sid and toks and len(self.learned) < 500 and (toks, sid) not in self.learned:
            self.learned.append((toks, sid))
        if sid:
            return sid
        best, sim = None, 0.0
        for lt, lsid in self.learned:
            j = len(lt & toks) / max(1, len(lt | toks))
            if j > sim:
                best, sim = lsid, j
        if best and sim >= 0.6:
            return best
        h = hashlib.md5((st.get("text", "") + st.get("acc", "") or st.get("url", "")).encode()).hexdigest()[:8]
        return f"unknown-{h}"

    def _identify_text(self, toks, st, expected, known):
        # a screen already recognised earlier in this run (same words on screen)
        best, sim = None, 0.0
        for lt, lsid in self.learned:
            j = len(lt & toks) / max(1, len(lt | toks))
            if j > sim:
                best, sim = lsid, j
        if best and sim >= 0.85 and not expected:
            return best
        title = _norm(st.get("title"))
        scores = []
        for s in self.course.slides:
            st_toks = self.slide_tokens[s.id]
            if not st_toks:
                continue
            w_all = sum(self.idf.get(t, 1) for t in st_toks)
            w_hit = sum(self.idf.get(t, 1) for t in st_toks & toks)
            score = w_hit / w_all                     # share of this slide's (distinctive) words on screen
            if title and title == _norm(s.title):
                score += 0.3
            scores.append((score, s.id))
        scores.sort(reverse=True)
        if scores:
            top, tsid = scores[0]
            second = scores[1][0] if len(scores) > 1 else 0
            if top >= 0.45 and top - second >= 0.08:
                return tsid
            if expected and expected in known and any(x[1] == expected and x[0] >= top - 0.05 for x in scores[:3]):
                return expected
        if expected and expected in known:
            return expected
        if best and sim >= 0.85:
            return best
        if scores and scores[0][0] >= 0.6:
            return scores[0][1]
        return None

    @staticmethod
    def _sig(st):
        toks = frozenset(WORD.findall((st.get("text", "") + " " + st.get("acc", "")).lower()))
        return (st.get("hash", ""), toks, frozenset(st.get("slideIds", [])))

    @staticmethod
    def _moved(a, b):
        """True when the screen clearly changed to another slide (not just more text appearing on the same one)."""
        if a[0] != b[0]:
            return True
        def jac(x, y):
            return len(x & y) / max(1, len(x | y)) if (x or y) else 1.0
        if a[2] or b[2]:
            if jac(a[2], b[2]) < 0.5:
                return True
        return jac(a[1], b[1]) < 0.6 and len(a[1] - b[1]) > 0.3 * max(1, len(a[1]))

    # The player's slide area can be left scrolled sideways after a click or focus change (the screen then
    # shows cut off on the left and its options seem missing). A learner never sees that: put it back.
    UNSCROLL_JS = r"""() => { let n = 0; try {
        document.querySelectorAll('.window-slide, #slide.window-slide, .primary-slide')
          .forEach(e => { if (e.scrollLeft || e.scrollTop) { e.scrollLeft = 0; e.scrollTop = 0; n++; } });
      } catch (e) {} return n; }"""

    def unscroll(self, page):
        try:
            return page.evaluate(self.UNSCROLL_JS)
        except Exception:
            return 0

    def settle(self, page, ms=1200):
        page.wait_for_timeout(ms)
        self.unscroll(page)
        last = None
        for _ in range(10):
            st = self.state(page)
            sig = (tuple(st["slideIds"]), st["text"][:300])
            if sig == last:
                return st
            last = sig
            page.wait_for_timeout(400)
        return st

    def click_first(self, page, selectors, timeout=1500):
        for q in selectors:
            loc = page.locator(q)
            try:
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=timeout)
                    return True
            except Exception:
                continue
        return False

    def fp_baseline(self, page):
        """Two fingerprints 600 ms apart; lines that change on their own (timeline, captions) are noise."""
        a = set(page.evaluate(FINGERPRINT_JS).split("\n"))
        page.wait_for_timeout(600)
        b = set(page.evaluate(FINGERPRINT_JS).split("\n"))
        return b, a ^ b

    def fp_changed(self, page, base, noise):
        after = set(page.evaluate(FINGERPRINT_JS).split("\n"))
        return bool((after ^ base) - noise)

    def shot(self, page, name):
        safe = re.sub(r"[^\w-]", "_", name)
        p = self.shots / f"{safe}.png"
        self.unscroll(page)
        try:
            page.screenshot(path=str(p))
            return f"shots/{p.name}"
        except Exception:
            return ""

    def wire(self, page, tag):
        def on_resp(r):
            if r.status >= 400 and r.url.startswith(self.url.rsplit("/", 1)[0]):
                key = (r.url, r.status)
                if key not in self.bad_files:
                    self.bad_files.add(key)
                    rel = r.url.split("/content/", 1)[-1].split("/", 1)[-1]
                    self.col.add("navigation", "error", f"Missing file ({r.status})", slide_id=self.current or "",
                                 slide_label=self.label(self.current), detail=rel)

        def on_req(r):
            if r.resource_type == "media" or re.search(r"\.(mp3|m4a|wav|mp4|webm|ogg)(\?|$)", r.url, re.I):
                v = self.visits.get(self.current)
                if v is not None:
                    v["media_requests"].add(r.url.rsplit("/", 1)[-1])

        def on_err(e):
            self.col.add("navigation", "warning", f"Script error ({tag})", slide_id=self.current or "",
                         slide_label=self.label(self.current), detail=str(e)[:300])
        page.on("response", on_resp)
        page.on("request", on_req)
        page.on("pageerror", on_err)

    def open(self, ctx, tag):
        page = ctx.new_page()
        page.add_init_script(INIT_SCRIPT)
        self.wire(page, tag)
        page.goto(self.url, wait_until="load", timeout=90000)
        self._enter_frame(page)
        self._ready(page)
        return page

    def _enter_frame(self, page):
        """Some packages wrap the player in an iframe (a launch page around story.html). Open the player page itself
        so every check sees the slides."""
        js = """() => {
          const own = document.querySelectorAll('[data-model-id], #slide, .slide-container, #next, .cs-next').length;
          if (own) return null;
          let best = null, area = 0;
          for (const f of document.querySelectorAll('iframe, frame')) {
            const r = f.getBoundingClientRect();
            if (r.width * r.height > area && f.src) { area = r.width * r.height; best = f.src; }
          }
          return area > 0.3 * innerWidth * innerHeight ? best : null;
        }"""
        for _ in range(3):
            page.wait_for_timeout(1500)
            try:
                src = page.evaluate(js)
            except Exception:
                return
            from urllib.parse import urlparse
            if not src or urlparse(src).netloc != urlparse(self.url).netloc:
                return
            self.log(f"Course runs inside a frame – opening {src.rsplit('/', 1)[-1]}")
            page.goto(src, wait_until="load", timeout=90000)

    def _ready(self, page):
        # wait for the player, then get past a "Resume?" prompt or a start screen
        for _ in range(60):
            st = self.state(page)
            if st["nextExists"] or st["slideIds"] or len(st["text"]) > 20:
                break
            page.wait_for_timeout(500)
        # the phone player shows a big "Start Course" button first
        for q in ("#mobile-start-button", "button[aria-label='Start Course' i]"):
            try:
                loc = page.locator(q)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=2000)
                    page.wait_for_timeout(1500)
                    break
            except Exception:
                pass
        # "Resume / Restart" splash of a course opened before in this browser: a new learner starts again
        for txt in ("Restart", "No", "Start", "Start Course", "Begin", "Click to start", "Play"):
            try:
                b = page.get_by_role("button", name=re.compile(rf"^{txt}$", re.I))
                if b.count() and b.first.is_visible():
                    b.first.click(timeout=1000)
                    page.wait_for_timeout(800)
            except Exception:
                pass
        self.settle(page, 1500)

    # ------------------------------------------------------------ getting to a slide
    def advance(self, page, sid, tap=False):
        """Move forward one slide the way a learner would (player Next or the slide's own button)."""
        slide = self.course.by_id(sid)
        dur = (slide.duration_ms / 1000.0) if slide and slide.duration_ms else 0
        t0 = time.time()
        self.seek_to_end(page)
        expected = slide.next_target if slide else None
        while time.time() - t0 < max(6, dur + 4):
            self.check_time()
            st = self.state(page)
            before = self._sig(st)
            if st["nextExists"] and not st["nextDisabled"]:
                self.click_first(page, SEL["next"])
            elif not st["nextExists"]:
                nav = self.slide_nav(page, slide, "forward")
                if not nav:
                    page.wait_for_timeout(700)
                    continue
                if nav.get("target") in set(self.course.ids()):
                    expected = nav["target"]
                if tap:
                    b = nav["box"]
                    page.touchscreen.tap(b[0] + b[2] / 2, b[1] + b[3] / 2)
                else:
                    self.click_box(page, nav["box"])
            else:
                start = self.slide_nav(page, slide, "forward") if is_welcome(slide) else None
                if start:   # welcome screen: START moves on, the player's Next stays locked
                    if start.get("target") in set(self.course.ids()):
                        expected = start["target"]
                    self.click_box(page, start["box"])
                else:
                    if slide and slide.is_quiz:
                        self.try_quiz(page)
                    page.wait_for_timeout(700)
                    continue
            st2 = self.settle(page)
            new = self.identify(st2, expected=expected if self._moved(before, self._sig(st2)) else None)
            return new if new != sid else None
        return None

    JUMP_JS = r"""
    (abs) => {
      try {
        const wm = window.DS && window.DS.windowManager;
        if (!wm || !wm.onRequestingSlide) return false;
        wm.onRequestingSlide(abs, undefined, 'push');
        return true;
      } catch (e) { return false; }
    }"""

    def jump(self, page, sid):
        """Open a slide directly through the Storyline player (works even when Next or the menu is locked).
        Used only to reach a screen for testing and screenshots, never to judge navigation."""
        s = self.course.by_id(sid)
        if not s:
            return None
        try:
            if not page.evaluate(self.JUMP_JS, s.abs_id):
                return None
        except Exception:
            return None
        st = self.settle(page)
        got = self.identify(st)
        return got

    def goto_slide(self, page, sid, tap=False):
        """Open a slide: directly through the player when possible, else the menu, else by replaying the course."""
        if not sid or sid.startswith("unknown"):
            return None
        if self.jump(page, sid) == sid:
            return sid
        if self.has_menu is not False:
            got = self.goto_via_menu(page, sid)
            if got == sid:
                return got
        ids = self.course.ids()
        if sid not in ids:
            return None
        page.reload(wait_until="load", timeout=90000)
        self._ready(page)
        cur = self.identify(self.settle(page, 300), expected=ids[0])
        for _ in range(len(ids) + 3):
            if cur == sid:
                return sid
            new = self.advance(page, cur, tap=tap)
            if not new:
                return cur
            cur = new
        return None

    # ------------------------------------------------------------ timeline
    def play_slide(self, page, sid, record=None, fast=False):
        """Wait for the slide to play (or fast-forward it). Collects captions and seekbar samples.
        fast: jump to the end when the seekbar allows it; otherwise wait until the screen has finished playing
        (the way forward is often locked until then), so swipes are tested in the same state as before."""
        s = self.course.by_id(sid)
        dur = (s.duration_ms / 1000.0) if s and s.duration_ms else 0
        t0 = time.time()
        full = self.play_full and not fast
        if fast:
            limit = min(max(dur + 6, 6), 20 * 60)
            page.wait_for_timeout(800)
            self.seek_to_end(page)
        elif self.play_full:
            limit = min(max(dur + 6, 6), 20 * 60)
        else:
            limit = 4
            page.wait_for_timeout(800)
            self.seek_to_end(page)
        st = self.state(page)
        seen_words, pending = set(), 0
        while time.time() - t0 < limit:
            self.check_time()
            st = self.state(page)
            if record is not None:
                # text on many screens animates in and out (speech bubbles, callouts): take a picture each time
                # new words appear, once they have settled, so language issues can be shown where they are visible
                words = set(re.findall(r"[a-z0-9']{3,}", (st.get("text", "") + " " + st.get("acc", "")).lower()))
                fresh = words - seen_words
                if len(fresh) >= 3:
                    seen_words |= fresh
                    pending = 2
                elif pending:
                    pending -= 1
                    if pending == 0:
                        self._text_shot(page, sid, record)
                for cap in st["captions"]:
                    if cap not in record["captions"]:
                        record["captions"].append(cap)
                        # screenshot each caption while it is on screen, so caption issues can be shown
                        if len(record.setdefault("cap_shots", [])) < 20:
                            try:
                                n = len(record["cap_shots"])
                                record["cap_shots"].append({"text": cap, "shot": self.shot(page, f"{sid}_cc{n}"),
                                                            "boxes": page.evaluate(TEXTBOX_JS)})
                            except Exception:
                                pass
                record["seek"].append((round(time.time() - t0, 1), st.get("seekVal"), st.get("seekFill")))
                for e in st["errors"]:
                    self.col.add("navigation", "warning", "Script error", slide_id=sid, slide_label=self.label(sid), detail=e[:300])
            elapsed = time.time() - t0
            playing = any(not m["paused"] for m in st["media"])
            if not st["nextDisabled"] and elapsed >= (dur if full else 0.5) and not playing:
                break
            if not self.play_full and st["nextDisabled"] and elapsed > 2:
                self.seek_to_end(page)
            page.wait_for_timeout(500)
        if record is not None and pending:
            self._text_shot(page, sid, record)
        return self.state(page)

    def _text_shot(self, page, sid, record):
        shots = record.setdefault("text_shots", [])
        if len(shots) >= 25:
            return
        try:
            shots.append({"shot": self.shot(page, f"{sid}_txt{len(shots)}"), "boxes": page.evaluate(TEXTBOX_JS)})
        except Exception:
            pass

    def seek_to_end(self, page):
        for q in SEL["seekbar"]:
            loc = page.locator(q)
            try:
                if loc.count() and loc.first.is_visible():
                    box = loc.first.bounding_box()
                    if box:
                        page.mouse.click(box["x"] + box["width"] * 0.995, box["y"] + box["height"] / 2)
                        # move off the seekbar: its time tooltip ("0:28 / 0:29") would otherwise sit over the
                        # screen's own PREV / NEXT buttons and take the next click
                        page.mouse.move(2, 2)
                        page.wait_for_timeout(350)
                        return True
            except Exception:
                continue
        return False

    def settle_timeline(self, page, limit=6):
        """Jump the timeline to the end so animations stop before buttons are clicked."""
        self.seek_to_end(page)
        t0 = time.time()
        while time.time() - t0 < limit:
            st = self.state(page)
            if not st["nextDisabled"] and not any(not m["paused"] for m in st["media"]):
                break
            page.wait_for_timeout(400)
        page.wait_for_timeout(400)

    def try_quiz(self, page):
        """Answer a quiz slide with the first option and submit, then dismiss feedback."""
        for q in ["[role='radio']", "[role='checkbox']", "[data-acc-type='radiobutton']", "[data-acc-type='checkbox']",
                  "input[type=radio]", "input[type=checkbox]"]:
            loc = page.locator(q)
            try:
                if loc.count():
                    loc.first.click(timeout=1500, force=True)
                    break
            except Exception:
                continue
        if not self.click_first(page, SEL["submit"]):
            try:
                page.get_by_role("button", name=re.compile("submit", re.I)).first.click(timeout=1500)
            except Exception:
                pass
        page.wait_for_timeout(1500)
        for txt in ("Continue", "OK", "Close", "Next", "Try Again"):
            try:
                b = page.get_by_role("button", name=re.compile(rf"^{txt}$", re.I))
                if b.count() and b.first.is_visible():
                    b.first.click(timeout=1000)
                    page.wait_for_timeout(1200)
                    break
            except Exception:
                pass

    # ------------------------------------------------------------ menu
    def ensure_menu(self, page):
        items = page.evaluate(MENU_JS, [SEL, VISITED_CLASSES, CURRENT_CLASSES])
        if items and any(i["visible"] for i in items):
            return items
        self.click_first(page, SEL["menu_toggle"])
        page.wait_for_timeout(700)
        items = page.evaluate(MENU_JS, [SEL, VISITED_CLASSES, CURRENT_CLASSES])
        # a player menu switched off in the player settings is still in the page, but nobody can see or use it
        return items if items and any(i["visible"] for i in items) else []

    def menu_target(self, it):
        """Slide a menu item opens, from the player's own link (data-ref), else None."""
        ref = (it.get("ref") or "").strip()
        if not ref:
            return None
        last = ref.rsplit(".", 1)[-1]
        if self.course.by_id(last):
            return last
        return self.course.scene_start.get(last)   # a scene heading opens the scene's first slide

    def build_menu_map(self, items):
        mapping, last = {}, -1
        slides = self.course.slides
        if any(it.get("ref") for it in items):
            for it in items:
                t = self.menu_target(it)
                if t and not it.get("scene"):
                    mapping[it["i"]] = t
            if mapping:
                return mapping
        for it in items:
            t = _norm(it["text"])
            hit = None
            for j in range(last + 1, len(slides)):
                if t and _norm(slides[j].title) == t:
                    hit = j
                    break
            if hit is None:
                for j, s in enumerate(slides):
                    if t and (_norm(s.title) == t or (len(t) > 8 and t in _norm(s.title))):
                        hit = j
                        break
            if hit is not None:
                mapping[it["i"]] = slides[hit].id
                last = hit
        return mapping

    def menu_items(self, page):
        try:
            items = page.evaluate(MENU_JS, [SEL, VISITED_CLASSES, CURRENT_CLASSES])
        except Exception:
            return []
        if self.menu_map is None and items:
            self.menu_map = self.build_menu_map(items)
        return items

    def goto_via_menu(self, page, sid):
        items = self.ensure_menu(page)
        if self.has_menu is None:
            self.has_menu = bool(items)
        if not items:
            return None
        if self.menu_map is None:
            self.menu_map = self.build_menu_map(items)
        idx = next((i for i, v in self.menu_map.items() if v == sid), None)
        if idx is None:
            return None
        try:
            before = self._sig(self.state(page))
            if not self.click_menu_item(page, idx):
                return None
            st = self.settle(page)
            self.close_menu_if_overlay(page)
            return self.identify(st, expected=sid if self._moved(before, self._sig(st)) else None)
        except Exception:
            return None

    MENU_BOX_JS = r"""
    (args) => {
      const [sel, i] = args;
      const notMenu = el => el.closest('#speed-scroll-wrapper, [id*="speed" i], [class*="speed" i], [id*="settings" i], [class*="settings" i], [role="menu"]');
      for (const q of sel.menu_items) {
        const items = [...document.querySelectorAll(q)].filter(el => !notMenu(el));
        if (!items.length) continue;
        const el = items[i]; if (!el) return null;
        const target = el.querySelector('a, button, [role="treeitem"], .cs-listitem-content, div') || el;
        (target.scrollIntoViewIfNeeded || target.scrollIntoView).call(target);
        const r = (target.getBoundingClientRect().height > 0 ? target : el).getBoundingClientRect();
        return r.width > 0 ? [r.x, r.y, r.width, Math.min(r.height, 40)] : null;
      }
      return null;
    }"""

    def click_menu_item(self, page, i):
        """Clicks course-menu item i (same list as MENU_JS, so the speed/settings lists never shift it)."""
        box = page.evaluate(self.MENU_BOX_JS, [SEL, i])
        if not box:
            return False
        page.mouse.click(box[0] + min(box[2] / 2, 60), box[1] + box[3] / 2)
        return True

    def close_menu_if_overlay(self, page):
        # the modern player's menu is an overlay panel on small screens: close it
        try:
            panel = page.locator("#outline-panel, .cs-outline-panel, .menu-panel")
            if panel.count() and panel.first.is_visible():
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
        except Exception:
            pass

    def menu_evidence(self, page, idx, name):
        """Opens the menu, screenshots it and returns (shot, box of menu item idx); leaves the menu as it was."""
        was_open = False
        try:
            items = page.evaluate(MENU_JS, [SEL, VISITED_CLASSES, CURRENT_CLASSES])
            was_open = any(i["visible"] for i in items)
        except Exception:
            pass
        if not was_open:
            self.click_first(page, SEL["menu_toggle"])
            page.wait_for_timeout(600)
        try:
            box = page.evaluate(self.MENU_BOX_JS, [SEL, idx])
        except Exception:
            box = None
        shot = self.shot(page, name)
        if not was_open:
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            try:
                if any(i["visible"] for i in page.evaluate(MENU_JS, [SEL, VISITED_CLASSES, CURRENT_CLASSES])):
                    self.click_first(page, SEL["menu_toggle"])
                    page.wait_for_timeout(300)
            except Exception:
                pass
        return shot, box

    def check_menu_state(self, page, current_sid, previous_sid):
        items = self.menu_items(page)
        if not items or not self.menu_map:
            return
        by_sid = {v: k for k, v in self.menu_map.items()}
        if previous_sid in by_sid:
            it = next((x for x in items if x["i"] == by_sid[previous_sid]), None)
            if it and not it["visited"]:
                shot, box = self.menu_evidence(page, it["i"], f"{previous_sid}_menu_visited")
                self.col.add("menu", "error", "Menu item not marked visited after the slide was viewed",
                             slide_id=previous_sid, slide_label=self.label(previous_sid), detail=it["text"],
                             screenshot=shot, box=box, vw=1366)
        if current_sid in by_sid:
            it = next((x for x in items if x["i"] == by_sid[current_sid]), None)
            if it and not it["current"]:
                shot, box = self.menu_evidence(page, it["i"], f"{current_sid}_menu_current")
                self.col.add("menu", "warning", "Current slide is not highlighted in the menu",
                             slide_id=current_sid, slide_label=self.label(current_sid), detail=it["text"],
                             screenshot=shot, box=box, vw=1366)
        self.col.ran("menu")

    # ------------------------------------------------------------ passes
    def desktop_pass(self, browser):
        ctx = browser.new_context(viewport={"width": 1366, "height": 768}, device_scale_factor=2)
        page = self.open(ctx, "desktop")
        n = len(self.course.slides)
        prev_sid = None
        carry = self.course.ids()[0] if self.course.slides else None   # slide we expect to be on
        for step in range(max(n * 2, 10) + 5):
            self.check_time()
            st = self.settle(page, 300)
            sid = self.identify(st, expected=carry or getattr(self, "_jumped", None))
            carry = self._jumped = None
            self.current = sid
            self._slide_t0 = time.time()
            first_visit = sid not in self.visits
            rec = self.visits.setdefault(sid, {"captions": [], "seek": [], "media_requests": set(), "shot": "",
                                               "seek_visible": False, "text": st["text"], "title": st["title"]})
            if not first_visit and step > 0:
                self._block(prev_sid or sid, f"Next goes back to an earlier screen ({self.label(sid)}) instead of moving on")
                break
            self.order.append(sid)
            self.log(f"Desktop: slide {len(self.order)}/{n} – {self.label(sid)}")
            self.tick("desktop", len(self.order) - 1, n)
            self.check_menu_state(page, sid, prev_sid)
            st = self.play_slide(page, sid, rec)
            rec["seek_visible"] = st["seekVisible"] or rec["seek_visible"]
            rec["text"] = st["text"] or rec["text"]
            rec["shot"] = self.shot(page, f"{sid}_desktop")
            rec["vw"] = 1366
            rec["seek_box"] = st.get("seekBox")
            rec["next_box"] = st.get("nextBox")
            rec["next_open_after_play"] = bool(st["nextExists"] and not st["nextDisabled"])
            try:
                rec["boxes"] = page.evaluate(TEXTBOX_JS)
            except Exception:
                rec["boxes"] = []
            self.capture_transcript(page, sid, rec)
            slide = self.course.by_id(sid)
            welcome = is_welcome(slide)
            if slide and (slide.id in self.quiz_ids or slide.is_quiz):
                # a question: answer it and go on like a learner (the assessment pass tests every option)
                new = self.answer_question(page, sid, 0)
                if new and new != sid:
                    carry = new
                    self.col.ran("navigation")
                    prev_sid = sid
                    continue
            if slide and slide.id in self.results_ids:
                # the results screen: after a failed attempt it may offer only Retry, which is not a way forward
                st_r = self.state(page)
                if not (st_r["nextExists"] and not st_r["nextDisabled"]):
                    from .quiz import RETRY_RE
                    nav = self.slide_nav(page, slide, "forward")
                    moved = None
                    if nav and not RETRY_RE.match(nav.get("label") or ""):
                        b0 = self._sig(st_r)
                        self.click_box(page, nav["box"])
                        s2 = self.settle(page)
                        moved = self.identify(s2, expected=nav["target"] if self._moved(b0, self._sig(s2)) else None)
                    if moved and moved != sid:
                        carry = moved
                        prev_sid = sid
                        continue
                    rec["note"] = "Results screen: no way forward after the tester's attempt; the next screen was opened directly"
                    if not self._jump_on(page, sid, slide, None, None):
                        break
                    prev_sid = sid
                    continue
            if slide and slide.drag_items and not slide.is_quiz and slide.id not in self.quiz_ids:
                # a drag-and-drop activity: place every item like a learner, then go on
                self.settle_timeline(page)
                placed, fb, _ = self.answer_drag(page, sid, slide, "right")
                rec["drag"] = {"placed": placed, "feedback": fb}
                stuck = [p_ for p_ in placed if not p_["moved"]]
                if stuck:
                    self.col.add("interactivity", "error", "Item cannot be dragged", slide_id=sid, slide_label=self.label(sid),
                                 detail="Items: " + ", ".join(f"“{p_['item']}”" for p_ in stuck), found_in="Interactivity",
                                 screenshot=self.shot(page, f"{sid}_drag_stuck"), vw=1366,
                                 fix="Make every item draggable so the learner can place it.")
                if fb != "No Submit button":
                    new = self.leave_feedback(page, sid)
                    if new and new != sid and not new.startswith("unknown"):
                        carry, prev_sid = new, sid
                        self.col.ran("navigation")
                        continue
                st = self.state(page)
            start_nav = self.slide_nav(page, slide, "forward") if welcome and st["nextExists"] and st["nextDisabled"] else None

            if not st["nextExists"] or start_nav:
                # player without Next (custom navigation), or a welcome screen with its own START button
                nav = start_nav or self.slide_nav(page, slide, "forward")
                if nav is None and self._submit_box(page):
                    # a survey / text-entry screen: answer it and press Submit, like a learner
                    self._fill_inputs(page)
                    b = self._submit_box(page)
                    nav = {"box": b, "label": "Submit", "target": slide.next_target if slide else "NEXT"}
                if nav is None and slide and slide.index < n and not slide.has_interaction:
                    # the way forward may open only after every item on the screen was clicked: do that first (quick)
                    got = self._unstick(page, sid, slide, None)
                    if got and got != sid and not got.startswith("unknown"):
                        carry = got
                        self.col.ran("navigation")
                        prev_sid = sid
                        continue
                    nav = self.slide_nav(page, slide, "forward")
                if nav is None and slide and slide.index < n and not slide.has_interaction:
                    self._wait_full(page, slide)
                    nav = self.slide_nav(page, slide, "forward")
                if nav is None:
                    if slide and slide.index < n:
                        if not slide.has_interaction:
                            if self._locked_by_items(sid, slide) and self._jump_on(page, sid, slide, None, None):
                                prev_sid = sid
                                continue
                            self._block(sid, "No way to go to the next screen (no Next button and no button that moves on)")
                            break
                        self._activity_unfinished(sid)
                        if not self._jump_on(page, sid, slide, None, None):
                            break
                        prev_sid = sid
                        continue
                    break  # last slide
                if not welcome:
                    self.custom_nav = True
                expected = nav["target"] if nav["target"] in set(self.course.ids()) else (slide.next_target if slide else "")
                nshot = rec["shot"]
                before = self._sig(self.state(page))
                self.click_box(page, nav["box"])
                st2 = self.settle(page)
                new = self.identify(st2, expected=expected if self._moved(before, self._sig(st2)) else None)
                carry = new
                lab = nav["label"] or "Next arrow on the screen"
                if new == sid:
                    def again(nav=nav, expected=expected):
                        here = self.identify(self.state(page))
                        if here != sid:
                            return here   # already moved on (a late page change): do not press anything more
                        b = self._sig(self.state(page))
                        self.click_box(page, nav["box"])
                        s2 = self.settle(page)
                        return self.identify(s2, expected=expected if self._moved(b, self._sig(s2)) else None)
                    new = self._unstick(page, sid, slide, again)
                    carry = new
                if new == sid and not (slide and slide.has_interaction):
                    self._wait_full(page, slide)   # the button may unlock only when the audio ends
                    nav2 = self.slide_nav(page, slide, "forward") or nav
                    b2 = self._sig(self.state(page))
                    self.click_box(page, nav2["box"])
                    s2 = self.settle(page)
                    new = self.identify(s2, expected=expected if self._moved(b2, self._sig(s2)) else None)
                    carry = new
                if new == sid:
                    if not (slide and slide.has_interaction) and self._locked_by_items(sid, slide):
                        if self._jump_on(page, sid, slide, None, None):
                            carry, prev_sid = None, sid
                            continue
                    if not (slide and slide.has_interaction):   # an activity must be finished first: not a fault
                        self._block(sid, f"“{lab}” does not go to the next screen", nshot, nav["box"])
                        break
                    if not self._jump_on(page, sid, slide, None, None):
                        break
                elif expected and new != expected and not new.startswith("unknown") and not self.course.same_draw(new, expected):
                    self.col.add("navigation", "error", f"“{lab}” went to the wrong slide", slide_id=sid,
                                 slide_label=self.label(sid), detail=f"Expected {self.label(expected)}, got {self.label(new)}",
                                 screenshot=nshot, box=nav["box"], vw=1366)
                else:
                    self.col.ran("navigation")
                prev_sid = sid
                continue
            if st["nextDisabled"]:
                if slide and slide.is_quiz:
                    self.try_quiz(page)
                    st = self.state(page)
                if st["nextDisabled"]:
                    # the learner may have to click the screen's tabs or answer first: do that, then look again
                    got = self._unstick(page, sid, slide, None)
                    if got and got != sid and not got.startswith("unknown"):
                        carry = got   # one of the items was itself the way forward (e.g. START)
                        self.col.ran("navigation")
                        prev_sid = sid
                        continue
                    st = self.state(page)
                if st["nextDisabled"] and slide and slide.index < n and not slide.has_interaction:
                    self._wait_full(page, slide)
                    st = self.state(page)
                if st["nextDisabled"]:
                    if slide and slide.index >= n:
                        break
                    if slide and slide.has_interaction:   # an activity the tester cannot finish (drag and drop): go on
                        rec["note"] = "Activity could not be completed by the tester; the next screen was opened directly"
                        self._activity_unfinished(sid)
                        ok = self._jump_on(page, sid, slide, None, None)
                    elif self._locked_by_items(sid, slide):
                        ok = self._jump_on(page, sid, slide, None, None)
                    else:
                        self._block(sid, "Next stays disabled after the screen finished and every item on it was clicked",
                                    rec["shot"], st.get("nextBox"))
                        break
                    if not ok:
                        break
                    prev_sid = sid
                    continue
            before = self._sig(self.state(page))
            self.click_first(page, SEL["next"])
            st2 = self.settle(page)
            new = self.identify(st2, expected=(slide.next_target if slide else None) if self._moved(before, self._sig(st2)) else None)
            carry = new
            if new == sid:
                def again(slide=slide):
                    here = self.identify(self.state(page))
                    if here != sid:
                        return here
                    b = self._sig(self.state(page))
                    self.click_first(page, SEL["next"])
                    s2 = self.settle(page)
                    return self.identify(s2, expected=(slide.next_target if slide else None) if self._moved(b, self._sig(s2)) else None)
                new = self._unstick(page, sid, slide, again)
                carry = new
            if new == sid and slide:
                nav = self.slide_nav(page, slide, "forward")
                if nav and nav.get("target") != "NEXT":
                    # Next does nothing, but the screen has its own choices that move on (a branching screen)
                    b = self._sig(self.state(page))
                    self.click_box(page, nav["box"])
                    s2 = self.settle(page)
                    moved = self.identify(s2, expected=nav["target"] if self._moved(b, self._sig(s2)) else None)
                    if moved != sid:
                        self.col.add("navigation", "warning", "Next button does nothing on this screen; the learner has to pick one of the choices",
                                     slide_id=sid, slide_label=self.label(sid), screenshot=rec["shot"], box=st.get("nextBox"), vw=1366)
                        carry = moved
                        prev_sid = sid
                        continue
            if new == sid and not (slide and slide.has_interaction):
                self._wait_full(page, slide)
                b2 = self._sig(self.state(page))
                self.click_first(page, SEL["next"])
                s2 = self.settle(page)
                new = self.identify(s2, expected=(slide.next_target if slide else None) if self._moved(b2, self._sig(s2)) else None)
                carry = new
            if new == sid:
                if not (slide and slide.has_interaction):
                    self._block(sid, "Next button does not go to the next screen", rec["shot"], st.get("nextBox"))
                    break
                rec["note"] = "Activity could not be completed by the tester; the next screen was opened directly"
                if not self._jump_on(page, sid, slide, None, None):
                    break
            elif slide and slide.next_target and new != slide.next_target and not new.startswith("unknown"):
                self.col.add("navigation", "error", "Next went to the wrong slide", slide_id=sid,
                             slide_label=self.label(sid),
                             detail=f"Expected {self.label(slide.next_target)}, got {self.label(new)}",
                             screenshot=rec["shot"], box=st.get("nextBox"), vw=1366)
            else:
                self.col.ran("navigation")
            prev_sid = sid
        if self.blocked:
            ctx.close()
            return
        if self.check_completion:
            try:
                self.capture_completion(page, ctx)
            except Exception as e:
                self.completion = {"error": repr(e)[:300]}
        if self.run_menu and not self.ensure_menu(page):
            # a menu built into the screens often opens topics only once they are completed: test it now, in the
            # session that has just played the whole course
            try:
                self.log("Menu: testing the course's own menu after playing the course")
                if self.custom_menu_pass(page, after_walk=True) is not False:
                    self.menu_done = True
            except Deadline:
                raise
            except Exception as e:
                self.log(f"Menu test after the walk did not finish: {e!r}"[:200])
        try:
            self.visit_rest(page)
        except Deadline:
            raise
        except Exception as e:
            self.log(f"Could not open every screen directly: {e!r}"[:200])
        ctx.close()
        reached = set(self.order)
        ids = self.course.ids()
        steps = list(zip(self.order, self.order[1:]))
        for s in self.course.slides:
            if s.id not in reached and s.draw:
                continue   # a question bank shows only some of its questions in one attempt
            if s.id not in reached:
                k = ids.index(s.id)
                jump = next(((a, b) for a, b in steps if a in ids and b in ids and ids.index(a) < k < ids.index(b)), None)
                if jump:
                    self.col.add("navigation", "warning", f"Slide is skipped: {self.label(jump[0])} goes straight to {self.label(jump[1])}",
                                 slide_id=s.id, slide_label=s.label, detail="Check that this is an intended branch.")
                else:
                    self.col.add("navigation", "warning", "Slide not reached by Next (check by hand or it is a branch)",
                                 slide_id=s.id, slide_label=s.label)

    def visit_rest(self, page):
        """Opens every screen Next did not reach, so each one has its own screenshot and text positions.
        (Navigation problems are still reported; this only gathers evidence.)"""
        todo = [s for s in self.course.slides if s.id not in self.visits]
        if not todo:
            return
        self.log(f"Opening {len(todo)} screen{'s' if len(todo) != 1 else ''} that Next did not reach")
        for k, s in enumerate(todo):
            self.check_time()
            self.tick("desktop", len(self.order) + k, len(self.order) + len(todo))
            got = s.id if (s.draw and self.open_question(page, s)) else self.goto_slide(page, s.id)
            if got != s.id:
                self.log(f"Could not open {s.label}")
                continue
            self.current = s.id
            self.seek_to_end(page)
            page.wait_for_timeout(1200)
            st = self.state(page)
            rec = {"captions": list(st["captions"]), "seek": [], "media_requests": set(), "via_jump": True,
                   "shot": self.shot(page, f"{s.id}_desktop"), "seek_visible": st["seekVisible"], "text": st["text"],
                   "title": st["title"], "vw": 1366, "seek_box": st.get("seekBox"), "next_box": st.get("nextBox")}
            try:
                rec["boxes"] = page.evaluate(TEXTBOX_JS)
            except Exception:
                rec["boxes"] = []
            self.capture_transcript(page, s.id, rec)
            self.visits[s.id] = rec
            self.log(f"Opened {s.label} directly")

    def capture_transcript(self, page, sid, rec):
        """Open the player's Transcript / Notes panel, keep its text, positions and a screenshot, then close it."""
        btn = None
        for q in SEL["transcript"]:
            loc = page.locator(q)
            try:
                if loc.count() and loc.first.is_visible():
                    btn = loc.first
                    break
            except Exception:
                continue
        if btn is None:
            return
        try:
            btn.click(timeout=2000)
            page.wait_for_timeout(800)
            text = page.evaluate(TRANSCRIPT_JS, SEL)
            if text:
                rec["transcript_text"] = text
                rec["transcript_shot"] = self.shot(page, f"{sid}_transcript")
                rec["transcript_boxes"] = page.evaluate(TEXTBOX_JS)
        except Exception:
            pass
        finally:
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
                if page.evaluate(TRANSCRIPT_JS, SEL):
                    btn.click(timeout=1500)
                    page.wait_for_timeout(300)
            except Exception:
                pass

    def capture_completion(self, page, ctx):
        """After the last slide: what did the course report to the LMS? Screenshot the end slide and a status panel."""
        page.wait_for_timeout(3000)  # Storyline commits completion a moment after the last slide loads
        lms = page.evaluate(LMS_JS) or {}
        store, calls = lms.get("store", {}) or {}, lms.get("calls", []) or []
        g = lambda *keys: next((store[k] for k in keys if store.get(k)), "")
        info = {
            "launch_file": self.course.launch_file.rsplit("/", 1)[-1], "version": lms.get("version") or "",
            "connected": any(c["fn"] == "Initialize" for c in calls),
            "lesson_status": g("cmi.core.lesson_status"), "completion_status": g("cmi.completion_status"),
            "success_status": g("cmi.success_status"),
            "score": g("cmi.core.score.raw", "cmi.score.raw"), "score_max": g("cmi.core.score.max", "cmi.score.max"),
            "score_scaled": g("cmi.score.scaled"), "location": g("cmi.core.lesson_location", "cmi.location"),
            "session_time": g("cmi.core.session_time", "cmi.session_time"),
            "exit": g("cmi.core.exit", "cmi.exit"), "calls": len(calls),
            "last_slide": self.label(self.order[-1]) if self.order else "",
            "set_values": [f"{c['k']} = {c['v']}" for c in calls if c["fn"] == "SetValue" and "suspend_data" not in c["k"]][-10:],
        }
        status = (info["completion_status"] or info["lesson_status"] or "").lower()
        info["completed"] = status in ("completed", "passed") or info["success_status"].lower() == "passed"
        info["final_shot"] = self.shot(page, "completion_final_slide")
        info["panel_shot"] = self._completion_panel(ctx, info)
        self.completion = info

    def _completion_panel(self, ctx, info):
        import html as _h
        e = lambda v: _h.escape(str(v or "—"))
        ok = info["completed"]
        rows = [("Course", self.course.title or ""), ("Launch file", info["launch_file"]),
                ("Connected to LMS", "Yes" if info["connected"] else "No – the course made no SCORM calls"),
                ("SCORM version", info["version"]),
                ("Lesson status (1.2)", info["lesson_status"]), ("Completion status (2004)", info["completion_status"]),
                ("Success status (2004)", info["success_status"]),
                ("Score", f"{info['score']}" + (f" / {info['score_max']}" if info["score_max"] else "")),
                ("Bookmark (last location)", info["location"]), ("Session time", info["session_time"]),
                ("Last slide reached", info["last_slide"]), ("SCORM calls recorded", info["calls"])]
        trs = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in rows)
        calls = "".join(f"<li>{e(c)}</li>" for c in info["set_values"]) or "<li>None</li>"
        doc = f"""<!doctype html><meta charset=utf-8><style>
body{{font:15px/1.5 'Segoe UI',Arial,sans-serif;margin:0;padding:28px;background:#f3f4f6;color:#17202b}}
.card{{background:#fff;border:1px solid #dde1e7;border-radius:12px;padding:24px 28px;max-width:980px}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#5d6877;font-size:13px;margin-bottom:16px}}
.badge{{display:inline-block;padding:6px 14px;border-radius:999px;font-weight:700;font-size:15px;
background:{'#e3f4ea' if ok else '#fdecea'};color:{'#1d7a46' if ok else '#b42318'};margin-bottom:16px}}
table{{border-collapse:collapse;width:100%}} th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid #eceff3;vertical-align:top}}
th{{width:260px;color:#5d6877;font-weight:600}} ul{{margin:6px 0 0;padding-left:18px;font:12.5px Consolas,monospace;color:#3d4048}}
h2{{font-size:14px;margin:18px 0 0}}</style>
<div class=card><h1>LMS completion status</h1>
<div class=sub>Recorded by Novac Tester's test LMS after playing the course to the last slide </div>
<div class=badge>{'COMPLETED' if ok else 'NOT COMPLETED'}</div> <span class=sub>Status sent to the LMS: <b>{e(info['completion_status'] or info['lesson_status'] or 'no status sent')}</b></span>
<table>{trs}</table><h2>Last values the course sent</h2><ul>{calls}</ul></div>"""
        page = ctx.new_page()
        page.set_viewport_size({"width": 1060, "height": 700})
        page.set_content(doc)
        try:
            bb = page.locator(".badge").bounding_box()
            info["panel_box"] = [bb["x"], bb["y"], bb["width"], bb["height"]] if bb else None
        except Exception:
            info["panel_box"] = None
        p = self.shots / "completion_status.png"
        page.screenshot(path=str(p), full_page=True)
        page.close()
        return f"shots/{p.name}"

    NAV_WORDS = re.compile(r"^(next|continue|start|begin|proceed|go|let'?s (go|begin|start)|get started|click to (start|begin)|"
                           r"start (the )?course|next slide|forward|[›»>→▶►])$", re.I)
    BACK_WORDS = re.compile(r"^(back|previous|prev|go back|[‹«<←◀◄])$", re.I)

    def click_box(self, page, box):
        page.mouse.move(2, 2)            # leave any hover pop-up (e.g. the seekbar time tooltip) first
        page.wait_for_timeout(150)
        page.mouse.click(box[0] + box[2] / 2, box[1] + box[3] / 2)

    def _box_of(self, page, model_id):
        try:
            return page.evaluate("""(id) => { for (const el of document.querySelectorAll('[data-model-id="' + id + '"]')) {
                const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                if (r.width <= 2 || r.height <= 2 || s.visibility === 'hidden' || s.display === 'none') continue;
                let op = 1; for (let p = el; p && p !== document.body; p = p.parentElement) {
                  const ps = getComputedStyle(p); if (ps.display === 'none' || ps.visibility === 'hidden') { op = 0; break; }
                  op *= parseFloat(ps.opacity || '1'); }
                if (op >= 0.1) return [r.x, r.y, r.width, r.height]; }   // a faded-out or hidden copy is not where the object is
                return null; }""", model_id)
        except Exception:
            return None

    def _visible_box(self, page, model_id):
        """Box of an object only when a learner can see and click it: shown, not see-through, and not covered
        by a layer or another object at its centre."""
        try:
            return page.evaluate("""(id) => { for (const el of document.querySelectorAll('[data-model-id="' + id + '"]')) {
                const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                if (r.width <= 2 || r.height <= 2 || s.visibility === 'hidden' || s.display === 'none') continue;
                let op = 1; for (let p = el; p && p !== document.body; p = p.parentElement) {
                  const ps = getComputedStyle(p); if (ps.display === 'none' || ps.visibility === 'hidden') { op = 0; break; }
                  op *= parseFloat(ps.opacity || '1'); }
                if (op < 0.2) continue;
                const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
                if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) continue;
                const top = document.elementFromPoint(cx, cy);
                if (!top) continue;
                const owner = top.closest('[data-model-id]');
                if (top === el || el.contains(top) || (owner && (owner === el || el.contains(owner)))) return [r.x, r.y, r.width, r.height];
              }
              return null; }""", model_id)
        except Exception:
            return None

    def _arrow_name(self, page, slide, box):
        """Names an unnamed button by where it sits: just left of the screen's Next arrow it is the Back arrow."""
        if not box or not slide:
            return None
        fwd = self.slide_nav(page, slide, "forward")
        if fwd:
            fb = fwd["box"]
            cy, fy = box[1] + box[3] / 2, fb[1] + fb[3] / 2
            if abs(cy - fy) < 14:
                if 0 < fb[0] - box[0] < 140:
                    return "Back arrow"
                if 0 < box[0] - fb[0] < 140:
                    return "Next arrow"
            return None
        # no working Next on this screen: a pair of small unnamed buttons side by side in the lower part of the
        # screen is the Back / Next pair (left = Back, right = Next)
        if box[2] > 70 or box[3] > 70 or box[1] < 768 * 0.55:
            return None
        cy = box[1] + box[3] / 2
        for co in slide.click_objects:
            b = self._visible_box(page, co["id"])
            if not b or b == box or b[2] > 70 or b[3] > 70 or abs(b[1] + b[3] / 2 - cy) >= 14:
                continue
            if 0 < b[0] - box[0] < 140:
                return "Back arrow"
            if 0 < box[0] - b[0] < 140:
                return "Next arrow"
        return None

    def _dead_back(self, page, slide):
        """The screen's Back arrow when it has no working action: (box) or None."""
        if not slide:
            return None
        nav_ids = {o["id"] for o in slide.nav_objects}
        for d in slide.dead_click_objects:
            if not isinstance(d, dict) or d["id"] in nav_ids:
                continue
            b = self._visible_box(page, d["id"])
            if b and self._arrow_name(page, slide, b) == "Back arrow":
                return b
        return None

    NOT_NAV = re.compile(r"^\W*(retake|retry|restart|start again|try again|exit|home|menu|replay|review|"
                         r"retake (course|assessment|quiz)|restart (course|module))\b", re.I)

    def slide_nav(self, page, slide, direction="forward"):
        """The slide's own navigation button, for players without Next/Prev: {box, label, target} or None."""
        ids = self.course.ids()
        here = ids.index(slide.id) if slide and slide.id in ids else -1
        cands = []
        for o in (slide.nav_objects if slide else []):
            if self.NOT_NAV.match(o.get("label") or ""):
                continue   # Retake / Restart / Exit / Home / Menu jump elsewhere by design: they are not Back or Next
            t = o["target"]
            fwd = t == "NEXT" or (t in ids and ids.index(t) > here)
            back = t == "PREV" or (t in ids and 0 <= ids.index(t) < here)
            if (direction == "forward" and fwd) or (direction == "back" and back):
                box = self._box_of(page, o["id"])
                if box:
                    cands.append({"box": box, "label": o["label"], "target": t,
                                  "rank": 0 if slide and t == slide.next_target else 1})
        if cands:
            return sorted(cands, key=lambda c: c["rank"])[0]
        words = self.NAV_WORDS if direction == "forward" else self.BACK_WORDS
        try:
            objs = page.evaluate(CLICKABLES_JS, SEL)
        except Exception:
            objs = []
        for o in objs:
            lab = (o.get("label") or "").strip().strip("▶►›»").strip()
            if words.match(o.get("label", "").strip()) or (lab and words.match(lab)):
                return {"box": [o["x"] - o["w"] / 2, o["y"] - o["h"] / 2, o["w"], o["h"]], "label": o["label"], "target": "NEXT" if direction == "forward" else "PREV"}
        return None

    # A slide button drawn as a shape (SUBMIT, CONTINUE) carries its name only in data-acc-text.
    NAMED_JS = r"""(words) => {
      const re = new RegExp('^\\s*(' + words.join('|') + ')\\s*$', 'i');
      for (const el of document.querySelectorAll('.slide-object[data-acc-text]')) {
        if (!re.test(el.getAttribute('data-acc-text') || '')) continue;
        if (el.classList.contains('hidden')) continue;
        let ok = true;
        for (let p = el; p && p !== document.body; p = p.parentElement) {
          const s = getComputedStyle(p);
          if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0 || p.classList.contains('hidden')) { ok = false; break; }
        }
        if (!ok) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 8 || r.height < 8 || r.right < 0 || r.bottom < 0 || r.left > innerWidth || r.top > innerHeight) continue;
        const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
        if (top && !(top === el || el.contains(top) || top.contains(el))) continue;
        return [r.x, r.y, r.width, r.height];
      }
      return null; }"""

    def _named_box(self, page, words):
        try:
            return page.evaluate(self.NAMED_JS, [re.escape(w) for w in words])
        except Exception:
            return None

    def _submit_box(self, page):
        for q in SEL["submit"]:
            try:
                loc = page.locator(q)
                if loc.count() and loc.first.is_visible():
                    bb = loc.first.bounding_box()
                    if bb:
                        return [bb["x"], bb["y"], bb["width"], bb["height"]]
            except Exception:
                continue
        return self._named_box(page, ["submit", "submit answer", "check", "check answer"])

    INPUTS_JS = r"""
    () => [...document.querySelectorAll('textarea, input[type="text"], input:not([type])')].map(el => {
        const r = el.getBoundingClientRect();
        if (r.width < 8 || r.height < 8 || el.value) return null;
        const x = r.x + Math.min(30, r.width / 2), y = r.y + r.height / 2;
        const top = document.elementFromPoint(x, y);
        return (top === el || el.contains(top) || (top && top.contains(el))) ? [x, y] : null;
    }).filter(Boolean)"""

    def dismiss_prompts(self, page):
        """Closes a message box the player left open (e.g. "Invalid answer – OK")."""
        for word in ("OK", "Close", "Continue"):
            try:
                b = page.get_by_role("button", name=re.compile(rf"^{word}$", re.I))
                if b.count() and b.first.is_visible():
                    b.first.click(timeout=800)
                    page.wait_for_timeout(400)
                    return True
            except Exception:
                pass
        return False

    def _fill_inputs(self, page):
        """Types a short answer into the empty text boxes a learner can see (survey / reflection screens)."""
        self.dismiss_prompts(page)
        try:
            spots = page.evaluate(self.INPUTS_JS)[:10]
        except Exception:
            return
        for x, y in spots:
            try:
                page.mouse.click(x, y)       # type like a person so the course notices the answer
                page.keyboard.type("Test answer", delay=15)
                page.keyboard.press("Tab")
                page.wait_for_timeout(150)
            except Exception:
                continue

    def _unstick(self, page, sid, slide, retry):
        """The way forward did nothing. A learner may first have to click the screen's tabs, hotspots or answers,
        or close a pop-up. Try that, then try going forward again (retry). Returns the slide we end up on."""
        nav_boxes = [b for b in (self._box_of(page, o["id"]) for o in (slide.nav_objects if slide else [])) if b]
        inside = lambda o, b: b[0] <= o["x"] <= b[0] + b[2] and b[1] <= o["y"] <= b[1] + b[3]
        try:
            objs = [o for o in page.evaluate(CLICKABLES_JS, SEL) if not any(inside(o, b) for b in nav_boxes)][:14]
        except Exception:
            objs = []
        if slide:   # newer Storyline output gives no clickable hints: use the course's own list of clickable items
            skip = {o["id"] for o in slide.nav_objects} | set(getattr(self.course, "menu_ids", []) or [])
            for co in slide.click_objects:
                if co["id"] in skip or len(objs) >= 14:
                    continue
                b = self._visible_box(page, co["id"])
                if b:
                    c = {"x": b[0] + b[2] / 2, "y": b[1] + b[3] / 2}
                    if not any(inside(c, nb) for nb in nav_boxes) and not any(abs(c["x"] - o["x"]) < 8 and abs(c["y"] - o["y"]) < 8 for o in objs):
                        objs.append(self._as_obj(co["label"] or "", b))
        objs = [o for o in objs if not re.match(r"^\s*(menu|help|notes|close|exit|resources|glossary)\b", o.get("label") or "", re.I)]
        self._clicked_n = len(objs)
        self._fill_inputs(page)
        if objs:
            self.log(f"{self.label(sid)}: clicking {len(objs)} items on the screen before going on")
        for o in objs:
            self.check_time()
            before = self._sig(self.state(page))
            try:
                page.mouse.click(o["x"], o["y"])
            except Exception:
                continue
            page.wait_for_timeout(900)
            st = self.state(page)
            if self._moved(before, self._sig(st)):
                got = self.identify(self.settle(page, 300))
                if got != sid and not got.startswith("unknown"):
                    return got   # that item was itself a way forward
                # a pop-up opened: let its timeline play to the end (the item counts as visited only then)
                self._play_popup(page)
            self._close_any(page, slide)   # close the pop-up layer the item opened, if any
        self.seek_to_end(page)
        page.wait_for_timeout(600)
        if retry is None:
            return sid
        return retry()

    @staticmethod
    def _seek_frac(st):
        if st.get("seekMax") and st.get("seekVal") is not None and st["seekMax"] > 0:
            return st["seekVal"] / st["seekMax"]
        return st.get("seekFill")

    def _play_popup(self, page, limit=180):
        """Standard for every clickable: after it opens a pop-up / layer, wait until the seekbar inside it reaches
        the end (and its narration stops) before closing it – many courses enable Next only then.
        Stops early when the timeline stands still (it waits for a click) or there is nothing playing."""
        t0, last, still = time.time(), None, 0
        page.wait_for_timeout(800)
        while time.time() - t0 < limit:
            self.check_time()
            st = self.state(page)
            f = self._seek_frac(st)
            playing = any(not m["paused"] for m in st["media"])
            if not playing and (f is None or f >= 0.985):
                break
            if f is not None and last is not None and abs(f - last) < 0.002 and not playing:
                still += 1
                if still >= 4:
                    break
            else:
                still = 0
            last = f
            page.wait_for_timeout(1000)
        page.wait_for_timeout(500)

    def _close_any(self, page, slide):
        """Closes a pop-up layer: its Close / × button (by name, drawn as a shape, or as the course data says)."""
        for word in ("Close", "Continue", "OK", "X", "×"):
            try:
                b = page.get_by_role("button", name=re.compile(rf"^{re.escape(word)}$", re.I))
                if b.count() and b.first.is_visible():
                    b.first.click(timeout=800)
                    page.wait_for_timeout(400)
                    return True
            except Exception:
                pass
        nb = self._named_box(page, ["close", "x", "×", "✕", "continue", "ok"])
        if nb is None and slide:
            for cid in (getattr(slide, "close_objects", None) or []):
                nb = self._visible_box(page, cid)
                if nb:
                    break
        if nb:
            self.click_box(page, nb)
            page.wait_for_timeout(400)
            return True
        return False

    def _activity_unfinished(self, sid):
        """An activity the tester could not finish: say so in the report (never silently skipped)."""
        if sid in self.quiz_ids or sid in self.results_ids:
            return
        self.col.add("interactivity", "warning", "Activity could not be completed by the tester – check by hand",
                     slide_id=sid, slide_label=self.label(sid), screenshot=self.visits.get(sid, {}).get("shot", ""),
                     found_in="Interactivity", detail="The tester opened the next screen directly and carried on.",
                     fix="Check this activity by hand: complete it and confirm the way forward opens.")

    def _locked_by_items(self, sid, slide):
        """Next stays locked although every clickable item was opened: report it and carry on (never stop the test)."""
        if not slide or not (getattr(self, "_clicked_n", 0) or slide.click_objects):
            return False
        self.col.add("navigation", "error", "Next stays locked after every clickable item on the screen was opened",
                     slide_id=sid, slide_label=self.label(sid), screenshot=self.visits.get(sid, {}).get("shot", ""),
                     found_in="Navigation", detail="The tester opened every item, then opened the next screen directly to carry on.",
                     fix="Enable Next once the learner has opened every clickable item.")
        return True

    def no_nav_expected(self, slide):
        """Screens without their own Back / Next: the welcome and thank-you screens and the assessment (questions and result)."""
        graded = any(x.get("type") not in (None, "view") for x in (self.course.scorings or []))
        return bool(slide) and (is_welcome(slide) or is_end(slide, len(self.course.slides))
                                or (graded and slide.id in self.quiz_ids) or slide.id in self.results_ids)

    def _wait_full(self, page, slide):
        """Before calling a screen a dead end: play it again from the start in real time, without skipping ahead,
        for its full length (its audio ending is often what unlocks the way forward), like a learner who waits."""
        if not slide:
            return
        dur = (slide.duration_ms / 1000.0) if slide.duration_ms else 0
        wait = min(dur + 5, 600)
        self.log(f"{self.label(slide.id)}: playing the screen to its end ({int(wait)} s) before deciding it is stuck")
        if self.can_jump(page):
            self.jump(page, slide.id)
        t0 = time.time()
        while time.time() - t0 < wait:
            self.check_time()
            page.wait_for_timeout(1000)
        self._unstick(page, slide.id, slide, None)   # click the screen's items again, as a learner would

    def _block(self, sid, reason, shot="", box=None):
        """The learner cannot go past this screen: report it as a high issue and stop testing."""
        self.col.add("navigation", "error", f"{reason}. The learner cannot go past this screen – testing stopped here",
                     slide_id=sid, slide_label=self.label(sid), screenshot=shot or self.visits.get(sid, {}).get("shot", ""),
                     box=box, vw=1366, blocker=True, found_in="Navigation",
                     fix="Make the way forward work on this screen, then send the course for testing again.")
        self.blocked = {"sid": sid, "reason": reason}
        self.log(f"STOPPED: {self.label(sid)} – {reason}")

    def _jump_on(self, page, sid, slide, msg, sev, detail=""):
        if msg:
            self.col.add("navigation", sev, msg, slide_id=sid, slide_label=self.label(sid), detail=detail,
                         screenshot=self.visits.get(sid, {}).get("shot", ""))
        target = slide.next_target if slide else ""
        if not target:
            return False
        got = self.goto_slide(page, target)
        self._jumped = target if got == target else None
        return got == target

    def prev_pass(self, browser):
        if len(self.order) < 2:
            return
        ctx = browser.new_context(viewport={"width": 1366, "height": 768}, device_scale_factor=2)
        page = self.open(ctx, "prev")
        if self.can_jump(page):
            try:
                self.prev_pairs(page)
            finally:
                ctx.close()
            return
        last = self.order[-1]
        got = self.goto_slide(page, last)
        if got != last:
            self.col.skip("prev", "Could not reach the last slide again, Prev walk skipped")
            ctx.close()
            return
        history = list(self.order)
        sid = last
        while len(history) > 1:
            self.check_time()
            history.pop()
            expected = history[-1]
            self.current = sid
            if not self.play_full:
                self.seek_to_end(page)
            st = self.state(page)
            if not st["prevExists"]:
                back = self.slide_nav(page, self.course.by_id(sid), "back")
                if back is None:
                    if not self.custom_nav and not self.no_nav_expected(self.course.by_id(sid)):
                        self.col.add("navigation", "warning", "No Prev button on this slide", slide_id=sid, slide_label=self.label(sid))
                    got = self.goto_slide(page, expected)
                    sid = got or expected
                    continue
                pshot = self.shot(page, f"{sid}_back")
                before = self._sig(self.state(page))
                self.click_box(page, back["box"])
                st_b = self.settle(page)
                new = self.identify(st_b, expected=(back["target"] if back.get("target") in set(self.course.ids()) else expected)
                                    if self._moved(before, self._sig(st_b)) else None)
                self.log(f"Back: {self.label(sid)} -> {self.label(new)}")
                self.tick("prev", len(self.order) - len(history), len(self.order))
                lab = back["label"] or "the slide's back button"
                if new == sid:
                    self.col.add("navigation", "error", f"“{lab}” did not move back", slide_id=sid, slide_label=self.label(sid),
                                 screenshot=pshot, box=back["box"], vw=1366,
                                 detail=f"Clicked the “{lab}” (Back) button on {self.label(sid)}.\nExpected: {self.label(expected)} opens.\nResult: the screen did not change.")
                    new = self.goto_slide(page, expected) or expected
                elif back["target"] in self.course.ids() and new != back["target"] and not new.startswith("unknown"):
                    self.col.add("navigation", "error", f"“{lab}” went to the wrong slide", slide_id=sid, slide_label=self.label(sid),
                                 detail=f"Expected {self.label(back['target'])}, got {self.label(new)}",
                                 screenshot=pshot, box=back["box"], vw=1366)
                sid = new
                continue
            if st["prevDisabled"]:
                page.wait_for_timeout(1500)
                st = self.state(page)
            before = self._sig(self.state(page))
            self.click_first(page, SEL["prev"])
            st = self.settle(page)
            new = self.identify(st, expected=expected if self._moved(before, self._sig(st)) else None)
            self.log(f"Prev: {self.label(sid)} -> {self.label(new)}")
            self.tick("prev", len(self.order) - len(history), len(self.order))
            if new == sid:
                self.col.add("navigation", "error", "Prev button did not move back", slide_id=sid, slide_label=self.label(sid),
                             detail=f"Clicked the player Back (PREV) button on {self.label(sid)}.\nExpected: {self.label(expected)} opens.\nResult: the screen did not change.")
                new = self.goto_slide(page, expected) or expected
            elif new != expected and not new.startswith("unknown"):
                s = self.course.by_id(sid)
                if not (s and s.prev_target == new):
                    self.col.add("navigation", "warning", "Prev went to a different slide than the one before it",
                                 slide_id=sid, slide_label=self.label(sid),
                                 detail=f"Expected {self.label(expected)}, got {self.label(new)}")
            sid = new
        ctx.close()

    def can_jump(self, page):
        try:
            return bool(page.evaluate("() => !!(window.DS && DS.windowManager && DS.windowManager.onRequestingSlide)"))
        except Exception:
            return False

    def prev_pairs(self, page):
        """Back/Prev check for each pair of screens in the order Next showed them: open the earlier screen, then the
        later one (so the player's history is as a learner's would be), press Back and expect the earlier screen."""
        ids = set(self.course.ids())
        pairs = [(a, b) for a, b in zip(self.order, self.order[1:]) if a in ids and b in ids]
        for k, (a, b) in enumerate(reversed(pairs)):
            self.check_time()
            self.tick("prev", k, len(pairs))
            if self.jump(page, a) != a or self.jump(page, b) != b:
                continue
            self.current = b
            slide = self.course.by_id(b)
            if self.no_nav_expected(slide):
                continue   # welcome screen and assessment: no Back is expected there
            self.seek_to_end(page)
            page.wait_for_timeout(500)
            st = self.state(page)
            if st["prevExists"]:
                if st["prevDisabled"]:
                    page.wait_for_timeout(1500)
                    st = self.state(page)
                if st["prevDisabled"]:
                    continue   # Back is switched off on purpose here
                pshot, box, lab = self.shot(page, f"{b}_prev"), st.get("prevBox"), None
                before = self._sig(st)
                self.click_first(page, SEL["prev"])
            else:
                back = self.slide_nav(page, slide, "back")
                if back is None:
                    dead = self._dead_back(page, slide)
                    if dead:
                        # a Back arrow is on the screen but does nothing: click it to be sure, then report it
                        pshot = self.shot(page, f"{b}_back")
                        before = self._sig(st)
                        self.click_box(page, dead)
                        st2 = self.settle(page)
                        if not self._moved(before, self._sig(st2)):
                            self.col.add("navigation", "error", "“Back arrow” did not move back", slide_id=b,
                                         slide_label=self.label(b), screenshot=pshot, box=dead, vw=1366, found_in="Navigation",
                                         detail=f"Clicked the Back arrow on {self.label(b)}.\nExpected: {self.label(a)} opens.\n"
                                                f"Result: nothing happens (the arrow has no action).",
                                         fix=f"Add the action to the Back arrow so it opens {self.label(a)}.")
                            self.dead_back.add(b)
                        continue
                    if not self.custom_nav:
                        self.col.add("navigation", "warning", "No Prev button on this slide", slide_id=b, slide_label=self.label(b))
                    continue
                pshot, box, lab = self.shot(page, f"{b}_back"), back["box"], back["label"] or "Back arrow on the screen"
                before = self._sig(st)
                self.click_box(page, back["box"])
            st2 = self.settle(page)
            new = self.identify(st2, expected=a if self._moved(before, self._sig(st2)) else None)
            if new == b:
                # check twice before reporting: a slow screen may still be changing, or the first click was too early
                page.wait_for_timeout(1500)
                new = self.identify(self.state(page))
                if new == b:
                    before = self._sig(self.state(page))
                    if box and not lab:
                        self.click_first(page, SEL["prev"])
                    elif box:
                        self.click_box(page, box)
                    else:
                        self.click_first(page, SEL["prev"])
                    st2 = self.settle(page)
                    new = self.identify(st2, expected=a if self._moved(before, self._sig(st2)) else None)
            self.log(f"Back: {self.label(b)} -> {self.label(new)}")
            name = f"“{lab}”" if lab else "Prev button"
            if new == b:
                self.col.add("navigation", "error", f"{name} did not move back" if lab else "Prev button did not move back",
                             slide_id=b, slide_label=self.label(b), screenshot=pshot, box=box, vw=1366, found_in="Navigation",
                             detail=f"Clicked the {'“' + lab + '” (Back)' if lab else 'player Back (PREV)'} button on {self.label(b)}.\n"
                                    f"Expected: {self.label(a)} opens.\nResult: the screen did not change.",
                             fix=f"Make this Back button open {self.label(a)}.")
            elif new != a and not new.startswith("unknown") and not (slide and slide.prev_target == new):
                self.col.add("navigation", "warning", "Prev went to a different slide than the one before it",
                             slide_id=b, slide_label=self.label(b), screenshot=pshot, box=box, vw=1366,
                             detail=f"Clicked the {'“' + lab + '” (Back)' if lab else 'player Back (PREV)'} button on {self.label(b)}.\n"
                                    f"Expected {self.label(a)}, got {self.label(new)}",
                             fix=f"Make this Back button open {self.label(a)}.")
            else:
                self.col.ran("navigation")

    # ---- custom menu: a "Menu" button designed into the screens, opening a list of topics
    MENU_WORD = re.compile(r"^\s*(menu|course menu|main menu|contents|table of contents|toc|index)\s*$", re.I)
    CLOSE_WORD = re.compile(r"^\s*(close|close menu|x|×|✕|✖|back|return|resume)\s*$", re.I)

    @staticmethod
    def _obox(o):
        return [o["x"] - o["w"] / 2, o["y"] - o["h"] / 2, o["w"], o["h"]]

    def _clickables(self, page):
        try:
            return page.evaluate(CLICKABLES_JS, SEL)
        except Exception:
            return []

    @staticmethod
    def _as_obj(label, b):
        return {"label": label, "x": b[0] + b[2] / 2, "y": b[1] + b[3] / 2, "w": b[2], "h": b[3]}

    def _text_nodes(self, page):
        try:
            tb = page.evaluate(TEXTBOX_JS)
        except Exception:
            return []
        return [n for n in tb.get("nodes", []) if n.get("t") and n["b"][2] > 2 and n["b"][3] > 2]

    def _custom_menu_button(self, page):
        """The course's Menu button: from the published course data (most reliable), else a clickable or a piece of
        text on the screen that reads "Menu"."""
        for mid in getattr(self.course, "menu_ids", []) or []:
            b = self._box_of(page, mid)
            if b:
                return self._as_obj("Menu", b)
        o = next((o for o in self._clickables(page) if self.MENU_WORD.match(o.get("label") or "")), None)
        if o:
            return o
        n = next((n for n in self._text_nodes(page) if self.MENU_WORD.match(n["t"])), None)
        if n:
            return self._as_obj(n["t"], n["b"])
        return self._icon_menu_button(page)

    def _menu_links(self, slide):
        """Topic links of a course menu built into the screens: buttons on the slide that each open a different
        screen and carry that screen's name (usually hidden until the menu icon is clicked)."""
        if not slide:
            return []
        links = [o for o in slide.nav_objects if o["label"].strip() and o["target"] in set(self.course.ids())]
        return links if len({o["target"] for o in links}) >= 3 else []

    def _icon_menu_button(self, page):
        """A Menu button drawn as an icon with no name: the button that, when clicked, shows the screen's topic links."""
        sid = self.identify(self.state(page))
        slide = self.course.by_id(sid)
        links = self._menu_links(slide)
        if not links:
            return None
        cached = getattr(self, "_menu_icon_id", None)
        if cached:
            b = self._box_of(page, cached)
            if b:
                return self._as_obj("Menu", b)
        if sum(1 for o in links if self._box_of(page, o["id"])) >= 2:
            return None   # the topic links are already on the screen: not a pop-up menu
        link_ids = {o["id"] for o in slide.nav_objects}
        cands = []
        for co in slide.click_objects:
            if co["id"] in link_ids:
                continue
            b = self._box_of(page, co["id"])
            if b and b[2] >= 12 and b[3] >= 12:
                cands.append((co["id"], b))
        cands.sort(key=lambda c: (c[1][1], c[1][0]))   # menu icons usually sit at the top
        for cid, b in cands[:6]:
            self.click_box(page, b)
            page.wait_for_timeout(1000)
            shown = sum(1 for o in links if self._box_of(page, o["id"]))
            self.jump(page, sid)   # put the screen back as it was
            self.settle_timeline(page)
            if shown >= 2:
                self._menu_icon_id = cid
                self.log(f"Menu: the course menu opens from an icon on {self.label(sid)}")
                return self._as_obj("Menu", b)
        return None

    def open_custom_menu(self, page):
        """Clicks the screen's Menu button. Returns {"button", "items", "close"} or None if there is no Menu button.
        Menu entries are the pieces of text (and clickables) that appear when the menu opens."""
        btn = self._custom_menu_button(page)
        if not btn:
            return None
        key = lambda o: (round(o["x"] / 6), round(o["y"] / 6), (o.get("label") or "").lower())
        before_c = {key(o) for o in self._clickables(page)}
        before_t = {(_norm(n["t"]), round(n["b"][0] / 6), round(n["b"][1] / 6)) for n in self._text_nodes(page)}
        self.click_box(page, self._obox(btn))
        page.wait_for_timeout(1500)
        new = [o for o in self._clickables(page) if key(o) not in before_c]
        for n in self._text_nodes(page):
            if (_norm(n["t"]), round(n["b"][0] / 6), round(n["b"][1] / 6)) in before_t:
                continue
            o = self._as_obj(n["t"], n["b"])
            if not any(abs(o["x"] - x["x"]) < 8 and abs(o["y"] - x["y"]) < 8 for x in new):
                new.append(o)
        close = next((o for o in new if self.CLOSE_WORD.match(o.get("label") or "")), None)
        items = [o for o in new if o is not close and not self.MENU_WORD.match(o.get("label") or "")
                 and len((o.get("label") or "").strip()) >= 3]
        # keep only entries that read like screen titles when the menu also shows other text (headings, hints)
        titled = [o for o in items if any(_norm(o["label"]) == _norm(s.title) or
                                          (len(_norm(o["label"])) > 8 and _norm(o["label"]) in _norm(s.title))
                                          for s in self.course.slides if s.title)]
        if len(titled) >= 2:
            items = titled
        seen_l, uniq = set(), []
        for o in items:   # the same entry can be read twice (its text and its screen-reader copy)
            k = _norm(o.get("label"))
            if k not in seen_l:
                seen_l.add(k)
                uniq.append(o)
        return {"button": btn, "items": uniq, "close": close}

    def _find_custom_menu(self, page):
        """Moves through the first screens until one shows a Menu button. True if found."""
        ids = self.course.ids()
        for k in range(min(5, len(ids))):
            if self._custom_menu_button(page):
                return True
            if k + 1 < len(ids) and self.jump(page, ids[k + 1]) != ids[k + 1]:
                if not self.advance(page, self.identify(self.state(page))):
                    break
            self.settle_timeline(page)
        return bool(self._custom_menu_button(page))

    def custom_menu_pass(self, page, after_walk=False):
        """after_walk: called in the same session right after the whole course was played with Next, so a menu that
        unlocks topics only as the learner completes them is tested as a learner would see it."""
        if after_walk and not any(self._menu_links(x) for x in self.course.slides):
            return False
        if not self._find_custom_menu(page):
            self.col.skip("menu", "No course menu found: the player has no menu and no screen shows a Menu button")
            return
        home = self.identify(self.state(page))
        if self.can_jump(page):
            # test the menu from the last screen that has it, where every earlier topic should be open
            n_all = len(self.course.slides)
            # a content screen: on the assessment, its results and the end screen the menu is often switched off on purpose
            with_menu = [x.id for x in self.course.slides if self._menu_links(x) and x.id not in self.assess_ids
                         and not is_end(x, n_all) and not is_welcome(x)] or \
                        [x.id for x in self.course.slides if self._menu_links(x)]
            if with_menu:
                home = with_menu[-1]
            self.jump(page, home)
            self.settle_timeline(page)
        opened = self.open_custom_menu(page)
        shot = self.shot(page, f"{home}_custom_menu")
        btn_box = self._obox(opened["button"])
        if not opened["items"]:
            self.col.add("menu", "error", "Menu button does not open the menu", slide_id=home, slide_label=self.label(home),
                         screenshot=shot, box=btn_box, vw=1366)
            self.col.ran("menu")
            return
        self.log(f"Menu: course menu button found on {self.label(home)} with {len(opened['items'])} items")
        items = [{"i": k, "text": o["label"]} for k, o in enumerate(opened["items"])]
        mapping = self.build_menu_map(items)
        for it, o in zip(items, opened["items"]):
            if it["i"] not in mapping:
                self.col.add("menu", "warning", "Menu item does not match any screen title", slide_id=home,
                             slide_label=self.label(home), detail=it["text"], screenshot=shot, box=self._obox(o), vw=1366)
        listed = set(mapping.values())
        if len(listed) >= 0.5 * max(1, len(self.course.slides)):
            for s in self.course.slides:
                if s.id not in listed and not is_welcome(s):
                    self.col.add("menu", "info", "Screen is not listed in the menu", slide_id=s.id, slide_label=s.label)
        # the close button must close the menu
        if opened["close"]:
            self.click_box(page, self._obox(opened["close"]))
            page.wait_for_timeout(900)
            labels = {(o.get("label") or "") for o in self._clickables(page)} | {n["t"] for n in self._text_nodes(page)}
            if sum(1 for it in items if it["text"] in labels) >= max(1, len(items) // 2):
                self.col.add("menu", "error", "Close button does not close the menu", slide_id=home,
                             slide_label=self.label(home), screenshot=self.shot(page, f"{home}_menu_close"),
                             box=self._obox(opened["close"]), vw=1366)
        else:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        # every item must open its own screen
        expect = (self.expect or {}).get("menu")
        locked, moved = [], 0
        for idx, sid in sorted(mapping.items()):
            self.check_time()
            self.current = sid
            text = items[idx]["text"]
            here = self.identify(self.state(page))
            o = None   # the menu closes after each jump: always open it again from the current screen
            if not o:
                if self.can_jump(page) and self.jump(page, home) == home:
                    here = home   # every entry is tried from the same screen (a menu left open is closed this way too)
                again = self.open_custom_menu(page)
                if not again:
                    if self.goto_slide(page, home) == home:
                        again = self.open_custom_menu(page)
                if not again:
                    self.col.add("menu", "warning", "Menu button is not available on this screen", slide_id=here,
                                 slide_label=self.label(here), screenshot=self.shot(page, f"{here}_no_menu"), vw=1366)
                    break
                o = next((x for x in again["items"] if (x.get("label") or "") == text), None)
                if not o:
                    continue
            before = self._sig(self.state(page))
            ishot, ibox = self.shot(page, f"{here}_menuitem{idx}"), self._obox(o)   # the open menu, before the click
            self.click_box(page, self._obox(o))
            st = self.settle(page)
            got = self.identify(st, expected=sid if self._moved(before, self._sig(st)) else None)
            if got in (here, None) or (got or "").startswith("unknown"):
                # nothing opened (a locked entry): close the menu so the next round starts from a closed menu
                if again.get("close"):
                    self.click_box(page, self._obox(again["close"]))
                else:
                    page.keyboard.press("Escape")
                page.wait_for_timeout(700)
            self.log(f"Menu: “{text}” -> {self.label(got)}")
            self.tick("menu", idx + 1, max(1, len(mapping)))
            if got == sid:
                moved += 1
                if sid not in self.visits:
                    self.visits[sid] = {"captions": list(st["captions"]), "seek": [], "media_requests": set(),
                                        "shot": self.shot(page, f"{sid}_menu"), "seek_visible": st["seekVisible"],
                                        "text": st["text"], "title": st["title"], "via_menu": True,
                                        "vw": 1366, "seek_box": st.get("seekBox"), "boxes": page.evaluate(TEXTBOX_JS)}
            elif got in (here, None) or (got or "").startswith("unknown"):
                locked.append((sid, text, here, ishot, ibox))
            else:
                self.col.add("menu", "error", "Menu item opened the wrong screen", slide_id=sid, slide_label=self.label(sid),
                             detail=f"Menu item “{text}” opened {self.label(got)}", screenshot=self.shot(page, f"{sid}_menu"), vw=1366)
        for sid, text, here, ishot, ibox in locked:
            if expect == "free" or after_walk:   # after the whole course was played, every topic must open
                self.col.add("menu", "error", "Menu item does not open its screen", slide_id=here, slide_label=self.label(here),
                             detail=f"Clicked “{text}” in the menu on {self.label(here)} after playing the whole course; nothing opened",
                             screenshot=ishot, box=ibox, vw=1366, found_in="Menu",
                             fix=f"Make the “{text}” menu item open its screen.")
            else:
                self.col.add("menu", "info", "Menu item did not open (menu is locked until the screen is visited)",
                             slide_id=sid, slide_label=self.label(sid), detail=f"“{text}”")
        if moved == 0 and not locked:
            self.col.add("menu", "warning", "No menu item could be tested", slide_id=home, slide_label=self.label(home),
                         screenshot=shot, vw=1366)
        self.col.ran("menu")

    def menu_pass(self, browser):
        ctx = browser.new_context(viewport={"width": 1366, "height": 768}, device_scale_factor=2)
        page = self.open(ctx, "menu")
        items = self.ensure_menu(page)
        self.has_menu = bool(items)
        if not items and self.menu_done:
            ctx.close()
            return
        if not items:
            # no player menu: the course may have its own Menu button on the screens
            try:
                self.custom_menu_pass(page)
            finally:
                ctx.close()
            return
        self.menu_map = self.build_menu_map(items)
        by_ref = any(it.get("ref") for it in items)
        for it in items:
            if it.get("scene"):
                continue   # scene / topic headings group the screens; they are not screens themselves
            sid = self.menu_map.get(it["i"])
            if sid is None:
                shot, box = self.menu_evidence(page, it["i"], f"menu_item{it['i']}")
                self.col.add("menu", "warning", "Menu item does not match any slide title", detail=it["text"],
                             screenshot=shot, box=box, vw=1366)
            elif by_ref:
                s = self.course.by_id(sid)
                if s and s.title and _norm(it["text"]) != _norm(s.title) and _norm(s.title) not in _norm(it["text"]):
                    shot, box = self.menu_evidence(page, it["i"], f"{sid}_menu_title")
                    self.col.add("menu", "warning", "Menu title differs from the screen title", slide_id=sid,
                                 slide_label=s.label, detail=f"Menu: “{it['text']}”\nScreen: “{s.title}”",
                                 screenshot=shot, box=box, vw=1366)
        listed = set(self.menu_map.values())
        if len(listed) >= 0.5 * max(1, len(self.course.slides)):   # a screen-by-screen menu, not a topic list
            for s in self.course.slides:
                if s.id not in listed and not is_welcome(s):
                    self.col.add("menu", "info", "Slide is not listed in the menu", slide_id=s.id, slide_label=s.label)
        moved = 0
        for idx, sid in sorted(self.menu_map.items()):
            self.check_time()
            self.current = sid
            got = self.goto_via_menu(page, sid)
            self.log(f"Menu: item {idx + 1} -> {self.label(got)}")
            self.tick("menu", idx + 1, max(1, len(self.menu_map or {}) or idx + 1))
            if got is None or got.startswith("unknown"):
                self.col.add("menu", "error", "Menu item did not open a recognisable slide", slide_id=sid,
                             slide_label=self.label(sid), screenshot=self.shot(page, f"{sid}_menu"))
            elif got != sid:
                self.col.add("menu", "error", "Menu item opened the wrong slide", slide_id=sid, slide_label=self.label(sid),
                             detail=f"Opened {self.label(got)}", screenshot=self.shot(page, f"{sid}_menu"))
            else:
                moved += 1
                if sid not in self.visits:  # not reached by Next: record what we can for audio/seekbar checks
                    st = self.state(page)
                    self.visits[sid] = {"captions": list(st["captions"]), "seek": [], "media_requests": set(),
                                        "shot": self.shot(page, f"{sid}_menu"), "seek_visible": st["seekVisible"],
                                        "text": st["text"], "title": st["title"], "via_menu": True,
                                        "vw": 1366, "seek_box": st.get("seekBox"), "boxes": page.evaluate(TEXTBOX_JS)}
                items = self.menu_items(page)
                it = next((x for x in items if x["i"] == idx), None)
                if it and not it["current"]:
                    self.col.add("menu", "warning", "Current slide is not highlighted in the menu",
                                 slide_id=sid, slide_label=self.label(sid), detail=it["text"])
        if moved == 0 and self.menu_map:
            self.col.add("menu", "info", "Menu navigation looks restricted (no item opened a slide)")
        self.col.ran("menu")
        ctx.close()

    def buttons_pass(self, browser):
        ctx = browser.new_context(viewport={"width": 1366, "height": 768}, device_scale_factor=2)
        page = self.open(ctx, "buttons")
        # player buttons, on the first slide
        self.current = self.identify(self.state(page))
        for name, sels in PLAYER_BUTTONS.items():
            loc = None
            for q in sels:
                l = page.locator(q)
                try:
                    if l.count() and l.first.is_visible():
                        loc = l.first
                        break
                except Exception:
                    continue
            if loc is None:
                continue
            base, noise = self.fp_baseline(page)
            try:
                bb = loc.bounding_box()
            except Exception:
                bb = None
            pshot = self.shot(page, f"player_{name}")
            try:
                loc.click(timeout=2000)
            except Exception as e:
                self.col.add("buttons", "error", f"Player button '{name}' could not be clicked", detail=str(e)[:200])
                continue
            page.wait_for_timeout(900)
            if not self.fp_changed(page, base, noise) and len(ctx.pages) == 1:
                self.col.add("buttons", "error", f"Player button '{name}' does nothing when clicked",
                             slide_id=self.current or "", slide_label=self.label(self.current), screenshot=pshot,
                             box=[bb["x"], bb["y"], bb["width"], bb["height"]] if bb else None, vw=1366)
            else:
                self.col.ran("buttons")
            # restore: close panels / toggle back
            page.keyboard.press("Escape")
            if name in ("Volume", "Play/Pause", "Closed captions"):
                try:
                    loc.click(timeout=1500)
                except Exception:
                    pass
            for p in ctx.pages[1:]:
                p.close()
            page.wait_for_timeout(300)

        # slide objects: open every clickable a learner can see, close what it opened, and check Next before and after
        targets = [sid for sid in self.order if not sid.startswith("unknown")] or self.course.ids()
        found_objs = 0
        btn_seen = {}   # (label, position) -> (first screen, result) of buttons repeated on many screens
        for k_t, sid in enumerate(targets):
            self.check_time()
            self.tick("buttons", k_t, len(targets))
            s = self.course.by_id(sid)
            if self.identify(self.state(page)) != sid:
                got = self.goto_slide(page, sid)
                if got != sid:
                    continue
            self.current = sid
            self.settle_timeline(page)
            quizlike = sid in self.quiz_ids or sid in self.results_ids
            for d in (s.dead_click_objects if s and not quizlike else []):
                # a click trigger with no action matters only on something the learner can see
                # (assessment questions and results have no Back / Next by design: not checked there)
                b = self._visible_box(page, d["id"]) if isinstance(d, dict) else None
                arrow = self._arrow_name(page, s, b) if b else None
                last = s and s.index >= len(self.course.slides)
                if b and not (sid in self.dead_back and arrow == "Back arrow") and not (last and arrow == "Next arrow"):
                    self.col.add("buttons", "error", "Object has a click trigger with no action", slide_id=sid,
                                 slide_label=self.label(sid), detail=arrow or d.get("label") or "", box=b, vw=1366,
                                 screenshot=self.shot(page, f"{sid}_dead{d.get('id', '')}"))
            seen_overlap = set()
            self._overlap_check(page, sid, "", seen_overlap)
            nav_boxes = [b for b in (self._box_of(page, o["id"]) for o in (s.nav_objects if s else [])) if b]
            inside = lambda o, b: b[0] <= o["x"] <= b[0] + b[2] and b[1] <= o["y"] <= b[1] + b[3]
            objs = [o for o in page.evaluate(CLICKABLES_JS, SEL) if not any(inside(o, b) for b in nav_boxes)][: self.max_buttons]
            if not objs and s:
                # the page gives no clickable hints (common in newer Storyline output): use the course's own list of
                # objects with click triggers, found on screen by their id
                nav_ids = {o["id"] for o in s.nav_objects} | set(getattr(self.course, "menu_ids", []) or [])
                for co in s.click_objects:
                    if co["id"] in nav_ids:
                        continue
                    b = self._visible_box(page, co["id"])   # only what a learner can see (not under a layer)
                    if b and not any(inside({"x": b[0] + b[2] / 2, "y": b[1] + b[3] / 2}, nb) for nb in nav_boxes):
                        objs.append(self._as_obj(co["label"] or "", b))
                objs = objs[: self.max_buttons]
            if s and s.drag_items:   # drag items are dragged (walk-through / assessment), not clicked
                dboxes = [b for b in (self._box_of(page, x["id"]) for x in s.drag_items + s.drop_targets) if b]
                objs = [o for o in objs if not any(inside(o, b) for b in dboxes)]
            if sid in self.quiz_ids or sid in self.results_ids or (s and s.is_quiz):
                objs = []   # questions (also knowledge checks), results and Retry are tested with the questions
            found_objs += len(objs)
            self.log(f"Buttons: {self.label(sid)} – {len(objs)} clickable objects")
            items = []
            for k, o in enumerate(objs):
                self.check_time()
                if o.get("label"):   # earlier clicks may have moved things (accordions, tabs): find it again
                    try:
                        now = [c for c in page.evaluate(CLICKABLES_JS, SEL) if c.get("label") == o["label"]]
                        if now:
                            o = min(now, key=lambda c: abs(c["y"] - o["y"]) + abs(c["x"] - o["x"]))
                    except Exception:
                        pass
                lab = o.get("label") or ""
                if not lab or re.match(r"^(DIV|SPAN|SVG|G|IMG|PATH|CANVAS|BUTTON|A)$", lab) or re.match(r"^(?=.*\d)(?=.*[A-Za-z])[0-9A-Za-z]{11}$", lab):
                    lab = self._arrow_name(page, s, [o["x"] - o["w"] / 2, o["y"] - o["h"] / 2, o["w"], o["h"]]) or f"item {k + 1} (no name)"
                if lab == "Back arrow" and sid in self.dead_back:
                    continue   # already reported as "Back button not working"
                # a button repeated on every screen (Menu, Help, Notes, Close on the slide master) is tested in full on
                # the first screen and the last screen; in between it is reused when it worked
                rkey = (o.get("label") or lab, round(o["x"] / 10), round(o["y"] / 10))
                prev = btn_seen.get(rkey)
                if prev and prev[1]["opened"] and prev[1]["closed"] is not False and not prev[1].get("new_window") \
                        and k_t != len(targets) - 1:
                    items.append(dict(prev[1], same_as=prev[0]))
                    continue
                if not self._on_top(page, o) and self.can_jump(page):
                    # something opened earlier still covers this item: open the screen again so it can be clicked
                    self.jump(page, sid)
                    self.settle_timeline(page)
                    if o.get("label"):
                        now = [c for c in self._clickables(page) if c.get("label") == o["label"]]
                        if now:
                            o = min(now, key=lambda c: abs(c["y"] - o["y"]) + abs(c["x"] - o["x"]))
                    if not self._on_top(page, o):
                        continue   # not visible to a learner here: not tested
                base, noise = self.fp_baseline(page)
                oshot = self.shot(page, f"{sid}_btn{k}")
                words0 = self._words(page)
                self._shot_words = {oshot: self._seen_words(page)}
                n_pages = len(ctx.pages)
                try:
                    self._objs_before = self._clickables(page)
                except Exception:
                    self._objs_before = []
                self._no_before = self._named_box(page, ["no", "cancel", "not now"])
                try:
                    page.mouse.click(o["x"], o["y"])
                except Exception:
                    continue
                page.wait_for_timeout(1000)
                changed = self.fp_changed(page, base, noise) or len(ctx.pages) != n_pages
                if not changed:   # pictures and animations do not always show in the page structure: compare the screen
                    page.wait_for_timeout(800)
                    changed = self._pixels_changed(oshot, page)
                if not changed:   # check twice before reporting: wait for the screen to settle and click once more
                    page.wait_for_timeout(1500)
                    base, noise = self.fp_baseline(page)
                    try:
                        page.mouse.click(o["x"], o["y"])
                    except Exception:
                        pass
                    page.wait_for_timeout(1200)
                    changed = self.fp_changed(page, base, noise) or len(ctx.pages) != n_pages or self._pixels_changed(oshot, page)
                box = [o["x"] - o["w"] / 2, o["y"] - o["h"] / 2, o["w"], o["h"]]
                if changed and lab.startswith("item ") and len(ctx.pages) == n_pages:
                    title = self._popup_title(page, words0)
                    if title:
                        lab = f"{title} icon"
                item = {"label": lab, "opened": bool(changed), "closed": None,
                        "shot": oshot, "box": box, "new_window": len(ctx.pages) != n_pages}
                items.append(item)
                btn_seen.setdefault(rkey, (sid, item))
                if changed and len(ctx.pages) == n_pages and self.identify(self.state(page)) == sid:
                    # let what the click opened play to its end (seekbar complete) before judging Next or closing it
                    self._play_popup(page)
                    # what the click revealed (a layer, a tab): keep it, so text shown only there can be pictured
                    try:
                        lay = self.shot(page, f"{sid}_layer{k}")
                        item["shot"] = lay
                        self.visits.setdefault(sid, {}).setdefault("layer_shots", []).append(
                            {"shot": lay, "boxes": page.evaluate(TEXTBOX_JS)})
                        self._overlap_check(page, sid, item["label"], seen_overlap, lay)
                    except Exception:
                        pass
                    item["closed"] = self._close_popup(page, oshot, words0)
                    if item["closed"] is False:
                        self.col.add("interactivity", "error", "Pop-up does not close", slide_id=sid,
                                     slide_label=self.label(sid), detail=f"Opened from: {item['label']}",
                                     screenshot=self.shot(page, f"{sid}_noclose{k}"), vw=1366, found_in="Buttons",
                                     fix="Make the Close button close the pop-up and show the screen again.")
                if not changed:
                    try:   # the item may have moved since the list was made (it was still animating in): mark where it is now
                        now = [c for c in page.evaluate(CLICKABLES_JS, SEL) if o.get("label") and c.get("label") == o["label"]]
                        if now:
                            c = min(now, key=lambda c: abs(c["y"] - o["y"]) + abs(c["x"] - o["x"]))
                            box = [c["x"] - c["w"] / 2, c["y"] - c["h"] / 2, c["w"], c["h"]]
                            oshot = self.shot(page, f"{sid}_btn{k}_now")
                    except Exception:
                        pass
                    self.col.add("buttons", "warning", "Clickable object does nothing when clicked", slide_id=sid,
                                 slide_label=self.label(sid), detail=lab,
                                 screenshot=oshot, box=box, vw=1366)
                else:
                    self.col.ran("buttons")
                    for pg in ctx.pages[n_pages:]:
                        pg.close()
                    if self.identify(self.state(page)) != sid:
                        if self.goto_slide(page, sid) != sid:
                            break
                        self.settle_timeline(page)
                    elif item["closed"] is False and k < len(objs) - 1:
                        # the pop-up stays open and would hide the other items: open the screen again
                        if self.goto_slide(page, sid) == sid:
                            self.settle_timeline(page)
            self._gating(page, sid, s, items)
            # on to the next slide: when screens cannot be opened directly, go forward like a learner
            if not self.can_jump(page):
                self.advance(page, sid)
        self.col.ran("interactivity")
        if targets and found_objs == 0:
            # say so plainly: a "pass" here would only mean the player's own buttons were tested
            self.col.add("buttons", "warning", "Buttons inside the screens could not be detected, so they were not clicked",
                         detail="Only the player's own buttons (play, volume, replay and so on) were tested. "
                                "Check the screens' own buttons by hand for this course.")
        ctx.close()

    CLOSE_RE = re.compile(r"^\s*(close|close (window|pop-?up|layer)|x|×|✕|✖|╳|done)\s*$", re.I)

    def _popup_title(self, page, before_words):
        """The heading of what a click opened (e.g. "Menu", "Help", "Exit"): the top-most new piece of text."""
        try:
            new = [n for n in self._text_nodes(page)
                   if set(re.findall(r"[a-z0-9']{3,}", n["t"].lower())) - (before_words or set()) and len(n["t"]) <= 40]
        except Exception:
            return None
        if not new:
            return None
        n = min(new, key=lambda n: (round(n["b"][1] / 10), n["b"][0]))
        return n["t"].strip()[:30]

    def _on_top(self, page, o):
        """True when nothing covers the item at its centre (what a learner would click)."""
        try:
            return bool(page.evaluate("""([x, y, w, h]) => {
                const t = document.elementFromPoint(x, y); if (!t) return false;
                const r = t.getBoundingClientRect();
                // the element under the pointer must lie around the item, not be a big cover over it
                return r.width <= Math.max(w * 3, 60) + 40 || r.height <= Math.max(h * 3, 60) + 40 ||
                       !!t.closest('[data-acc-type], [role="button"], button, a'); }""", [o["x"], o["y"], o["w"], o["h"]]))
        except Exception:
            return True

    def _words(self, page):
        """Words a learner can see now (hidden screen-reader copies of closed layers are left out)."""
        return set(re.findall(r"[a-z0-9']{3,}", " ".join(n["t"] for n in self._text_nodes(page)).lower()))

    def _seen_words(self, page, shot_rel=None):
        """Words a learner can see right now (visible text boxes only, never hidden layers' copies).
        With shot_rel: the words recorded with that screenshot instead (the screen before the click)."""
        if shot_rel:
            tb = getattr(self, "_shot_words", {}).get(shot_rel)
            if tb is not None:
                return tb
        try:
            tb = page.evaluate(TEXTBOX_JS)
        except Exception:
            return set()
        return set(re.findall(r"[a-z0-9']{3,}", " ".join(n["t"] for n in tb.get("nodes", [])).lower()))

    def _close_popup(self, page, before_rel, before_words=None):
        """Closes what a click opened. True = the screen is as before again, False = it stays open,
        None = nothing to close (a tab or a reveal that stays on the screen by design)."""
        target = None
        # a "Do you want to …?" box is closed with No / Cancel (never Yes)
        nb = self._named_box(page, ["no", "cancel", "not now"])
        nb0 = getattr(self, "_no_before", None)
        if nb and nb0 and abs(nb[0] - nb0[0]) < 6 and abs(nb[1] - nb0[1]) < 6:
            nb = None   # a "No" that was on the screen before the click is content (e.g. a Yes / No question)
        if nb:
            target = self._as_obj("No", nb)
        before = getattr(self, "_objs_before", None) or []
        was_there = lambda o: any(abs(o["x"] - b["x"]) < 6 and abs(o["y"] - b["y"]) < 6 for b in before)
        if target is None:
            cands = [o for o in self._clickables(page) if self.CLOSE_RE.match(o.get("label") or "")]
            # prefer a close button the pop-up brought (the course's own "Close" exit button was there before)
            cands.sort(key=lambda o: was_there(o))
            target = cands[0] if cands else None
        if target is None:
            try:
                b = page.get_by_role("button", name=re.compile(r"^\s*(close|×|x)\s*$", re.I))
                if b.count() and b.first.is_visible():
                    bb = b.first.bounding_box()
                    if bb:
                        target = self._as_obj("Close", [bb["x"], bb["y"], bb["width"], bb["height"]])
            except Exception:
                pass
        if target is None:   # an unnamed close icon: the course data says which buttons close a pop-up
            slide = self.course.by_id(self.current) if self.current else None
            for cid in (getattr(slide, "close_objects", None) or []):
                b = self._visible_box(page, cid)
                if b:
                    target = self._as_obj("Close", b)
                    break
        if target is None:
            return None
        seen_before = self._seen_words(page, before_rel)
        opened = self._seen_words(page) - seen_before

        def gone():
            if opened:   # the words the pop-up showed must no longer be visible
                left = opened & self._seen_words(page)
                return len(left) <= 0.3 * len(opened)
            return not self._pixels_changed(before_rel, page, min_share=0.03)
        page.mouse.click(target["x"], target["y"])
        page.wait_for_timeout(900)
        if gone():
            return True
        page.wait_for_timeout(1200)   # a closing animation
        return gone()

    def _overlap_check(self, page, sid, where, seen, shot=None):
        try:
            spills = page.evaluate(OVERFLOW_JS)
        except Exception:
            spills = []
        for h in spills[:3]:
            key = ("spill", h["t"])
            if key in seen:
                continue
            seen.add(key)
            msg = ("Text runs out of its box into the picture" if h["overPic"] else
                   "Text runs off the slide" if h["off"] else "Text runs out of its box")
            self.col.add("visual", "error", msg + (f" (after opening “{where}”)" if where else ""),
                         slide_id=sid, slide_label=self.label(sid), detail=f"“{h['t'][:80]}”",
                         screenshot=shot or self.shot(page, f"{sid}_spill{len(seen)}"), box=h["box"], vw=1366,
                         found_in="OST", fix="Shorten the text or resize its box so it fits and does not cover the picture.")
        try:
            hits = page.evaluate(OVERLAP_JS)
        except Exception:
            return
        for h in hits[:3]:
            key = tuple(sorted((h["a"], h["b"])))
            if key in seen:
                continue
            seen.add(key)
            self.col.add("interactivity", "warning", "Text overlaps other text" + (f" (after opening “{where}”)" if where else ""),
                         slide_id=sid, slide_label=self.label(sid), detail=f"“{h['a'][:60]}” overlaps “{h['b'][:60]}”",
                         screenshot=shot or self.shot(page, f"{sid}_overlap{len(seen)}"), box=h["box"], vw=1366,
                         found_in="OST", fix="Move or resize the text boxes so they do not cover each other.")

    def _gating(self, page, sid, slide, items):
        """Next must open only after every clickable item on the screen has been opened."""
        st = self.state(page)
        if st["nextDisabled"] and st["nextExists"]:
            page.wait_for_timeout(1500)
            st = self.state(page)
        v = self.visits.get(sid, {})
        # the player / master buttons (Menu, Help, Notes, Close …) are not part of the screen's activity
        frame_btn = re.compile(r"^\s*(menu|course menu|help|notes|close|exit|exit course|resources|glossary|transcript|"
                               r"settings|volume|replay|play|pause)\b", re.I)
        real = [i for i in items if i["opened"] and not i.get("new_window") and not i.get("same_as")
                and not frame_btn.match(i.get("label") or "")]
        rec = {"items": items, "next_before": v.get("next_open_after_play"), "next_after": bool(st["nextExists"] and not st["nextDisabled"]),
               "player_next": bool(st["nextExists"]), "rule": ""}
        self.clicks[sid] = rec
        last = slide and slide.index >= len(self.course.slides)
        if not real or not st["nextExists"] or last or self.no_nav_expected(slide):
            return
        if v.get("next_open_after_play") is True and not v.get("via_jump"):
            rec["rule"] = "Next was enabled before the items were opened"
            self.col.add("interactivity", "warning", "Next is enabled before all clickable items are opened", slide_id=sid,
                         slide_label=self.label(sid), screenshot=v.get("shot", ""), box=v.get("next_box"), vw=1366,
                         detail=f"{len(real)} clickable item{'s' if len(real) != 1 else ''} on this screen: "
                                + ", ".join(i["label"] for i in real[:8]), found_in="Navigation",
                         fix="Keep Next disabled until the learner has opened every clickable item.")
        elif not rec["next_after"]:
            rec["rule"] = "Next stayed disabled after all items were opened"
            self.col.add("interactivity", "error", "Next stays disabled after all clickable items were opened", slide_id=sid,
                         slide_label=self.label(sid), screenshot=self.shot(page, f"{sid}_gate"), box=st.get("nextBox"), vw=1366,
                         found_in="Navigation", fix="Enable Next once every clickable item has been opened.")

    def _pixels_changed(self, before_rel, page, min_share=0.004):
        """True when the screen now looks different from the screenshot taken before the click."""
        try:
            from io import BytesIO
            from PIL import Image, ImageChops
            a = Image.open(self.dir / before_rel).convert("L")
            b = Image.open(BytesIO(page.screenshot())).convert("L")
            if a.size != b.size:
                b = b.resize(a.size)
            a, b = a.resize((a.width // 4, a.height // 4)), b.resize((a.width // 4, a.height // 4))
            diff = ImageChops.difference(a, b).point(lambda v: 255 if v > 24 else 0)
            share = sum(1 for v in diff.getdata() if v) / (diff.width * diff.height)
            return share >= min_share
        except Exception:
            return False

    def touch_swipe(self, page, cdp, direction, retry=True):
        """Swipes across the screen like a finger. If the first swipe lands on something clickable and nothing
        moves, it tries again higher and lower on the screen before deciding swiping does not work."""
        before = self._sig(self.state(page))
        sid = self.identify(self.state(page))
        for frac in ((0.5, 0.22, 0.78) if retry else (0.5,)):
            got, st = self._swipe_once(page, cdp, direction, frac)
            if got != sid or self._moved(before, self._sig(st)):
                return got, st
        return got, st

    def _swipe_once(self, page, cdp, direction, frac):
        vp = page.viewport_size
        y = vp["height"] * frac
        xs = (vp["width"] * 0.8, vp["width"] * 0.2) if direction == "left" else (vp["width"] * 0.2, vp["width"] * 0.8)
        steps = 8
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": xs[0], "y": y}]})
        for i in range(1, steps + 1):
            x = xs[0] + (xs[1] - xs[0]) * i / steps
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y}]})
            page.wait_for_timeout(20)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        st = self.settle(page, 1300)
        return self.identify(st), st

    def mobile_pass(self, pw, browser):
        dev = dict(pw.devices.get("iPhone 13 landscape") or {"viewport": {"width": 750, "height": 342},
                                                               "is_mobile": True, "has_touch": True})
        dev.pop("default_browser_type", None)
        ctx = browser.new_context(**dev)
        page = self.open(ctx, "mobile")
        cdp = ctx.new_cdp_session(page)
        n = len(self.course.slides)
        ids = self.course.ids()
        seen = set()
        stuck = []   # screens where the phone pass could not move on by itself

        def onward(sid, why):
            """Stuck on `sid`: note it and open the next screen not tested yet, so one broken
            screen does not end mobile testing for the rest of the course."""
            if sid not in self.results_ids:   # after a failed attempt the results screen offers only Retry: expected
                stuck.append((sid, why))
            start = ids.index(sid) + 1 if sid in ids else 0
            for nid in ids[start:]:
                if nid in seen or nid in self.quiz_ids:
                    continue
                if self.goto_slide(page, nid, tap=True) == nid:
                    self.log(f"Mobile: moved on to {self.label(nid)} (could not go forward from {self.label(sid)})")
                    return True
            return False

        vw = page.viewport_size["width"]
        sw = self.swipes = {}

        # where Next / Back should go, from the course itself (the phone test runs beside the walk-through)
        def fwd_target(sid, slide):
            return slide.next_target if slide else ""

        def back_target(sid, slide):
            return slide.prev_target if slide else ""

        for _ in range(max(n * 2, 10)):
            self.check_time()
            st = self.state(page)
            sid = self.identify(st)
            if sid in seen:
                nxt = [i for i in ids if i not in seen]
                if not nxt or self.goto_slide(page, nxt[0], tap=True) != nxt[0]:
                    break
                continue
            if sid in self.quiz_ids:
                # assessment questions have no Back / Next and are tested in full on the desktop:
                # on the phone go straight on to the screen after the assessment
                for q in self.quiz_ids:
                    seen.add(q)
                    sw[q] = {"next": "N/A", "back": "N/A"}
                after = [i for i in self.results_ids if i not in seen] + [
                    i for i in ids[max(ids.index(q) for q in self.quiz_ids if q in ids) + 1:] if i not in seen]
                self.log("Mobile: assessment questions are tested on the desktop; going on after the assessment")
                if not after or self.goto_slide(page, after[0], tap=True) != after[0]:
                    break
                continue
            seen.add(sid)
            self.current = sid
            lab = self.label(sid)
            self.log(f"Mobile: {lab}")
            self.tick("mobile", len(seen) - 1, n)
            slide = self.course.by_id(sid)
            rec = sw.setdefault(sid, {"next": "Not tested", "back": "Not tested"})
            last = bool(slide) and slide.index >= n
            if self.no_nav_expected(slide):
                # welcome (START) and assessment screens: no Back / Next, so no swipe test
                rec["next"] = rec["back"] = "N/A"
                self.play_slide(page, sid, fast=True)
                if slide and slide.id in self.quiz_ids:
                    new = self.answer_question(page, sid, 0)
                    if new and new != sid:
                        continue
                if not self.advance(page, sid, tap=True):
                    # the START / Continue button may appear only when the screen has finished playing
                    if not last and slide and slide.id not in self.results_ids:
                        self._wait_full(page, slide)
                        if self.advance(page, sid, tap=True):
                            continue
                    if last or not onward(sid, "the screen's own button did not open the next screen"):
                        break
                continue
            # 1. before the screen has finished, the way forward is locked: swipe next must not move
            locked = (st["nextExists"] and st["nextDisabled"]) or (not st["nextExists"] and not self.slide_nav(page, slide, "forward"))
            if locked and not last:
                before = self.shot(page, f"{sid}_swipe_locked")
                new, _ = self.touch_swipe(page, cdp, "left", retry=False)
                if new != sid and not new.startswith("unknown"):
                    rec["next"] = "Fail – moves on while Next is locked"
                    self.col.add("swipe", "error", "Swipe next (left) moves on while Next is locked",
                                 slide_id=sid, slide_label=lab, detail=f"Moved to {self.label(new)} before the screen finished",
                                 screenshot=before, box=st.get("nextBox"), vw=vw)
                    self.goto_slide(page, sid, tap=True)
            st = self.play_slide(page, sid, fast=True)
            # 2. swipe previous (right): goes back when Back is available, stays when it is not
            prev_nav = st["prevExists"] and not st["prevDisabled"]
            # the screen's own Back button – also a Back arrow that is there but broken (that is reported as a Back issue)
            custom_back = None if st["prevExists"] else (self.slide_nav(page, slide, "back") or self._dead_back(page, slide))
            want_back = back_target(sid, slide)
            if want_back and not (slide and slide.index == 1):
                shot_b = self.shot(page, f"{sid}_swipe_prev")
                back, _ = self.touch_swipe(page, cdp, "right", retry=True)
                moved = back != sid and not back.startswith("unknown")
                if prev_nav:
                    if not moved:
                        rec["back"] = "Fail – does not go back"
                        self.col.add("swipe", "error", "Swipe previous (right) does not go back (the Back button works)",
                                     slide_id=sid, slide_label=lab, screenshot=shot_b, vw=vw,
                                     detail=f"Swiped right on {lab}. Expected: {self.label(want_back)}. Result: stayed on the same screen.")
                    elif back != want_back:
                        rec["back"] = "Fail – opens the wrong screen"
                        self.col.add("swipe", "error", "Swipe previous (right) opens the wrong screen", slide_id=sid, slide_label=lab,
                                     screenshot=shot_b, vw=vw, detail=f"Expected {self.label(want_back)}, got {self.label(back)}")
                    else:
                        rec["back"] = "Pass – goes back"
                        self.col.ran("swipe")
                elif custom_back:
                    # the screen's own Back button: swiping is a player feature, so it may or may not move
                    rec["back"] = ("Pass – goes back" if back == want_back else
                                   "Not available (custom Back button)" if not moved else "Fail – opens the wrong screen")
                    if moved and back != want_back:
                        self.col.add("swipe", "error", "Swipe previous (right) opens the wrong screen", slide_id=sid, slide_label=lab,
                                     screenshot=shot_b, vw=vw, detail=f"Expected {self.label(want_back)}, got {self.label(back)}")
                else:
                    if moved and st["prevExists"]:   # the player's Back is there but locked
                        rec["back"] = "Fail – goes back although Back is locked"
                        self.col.add("swipe", "error", "Swipe previous (right) goes back although Back is locked", slide_id=sid,
                                     slide_label=lab, screenshot=shot_b, vw=vw, detail=f"Moved to {self.label(back)}")
                    elif moved:
                        rec["back"] = "Goes back (the screen has no Back button)"
                    else:
                        rec["back"] = "Pass – stays (Back is locked)"
                if moved and self.goto_slide(page, sid, tap=True) != sid:
                    if not onward(sid, "could not come back after swiping back"):
                        break
                    continue
                st = self.state(page)
            else:
                rec["back"] = "N/A"
            # 3. swipe next (left) after the screen finished
            want = fwd_target(sid, slide)
            if last or not want:
                rec["next"] = "N/A" if rec["next"] == "Not tested" else rec["next"]
                break
            player_next = st["nextExists"] and not st["nextDisabled"]
            custom_next = None if st["nextExists"] else self.slide_nav(page, slide, "forward")
            shot_n = self.shot(page, f"{sid}_swipe_next")
            new, _ = self.touch_swipe(page, cdp, "left", retry=True)
            moved = new != sid and not new.startswith("unknown")
            if moved and new != want and not (slide and slide.has_interaction):
                if rec["next"] == "Not tested":
                    rec["next"] = "Fail – opens the wrong screen"
                self.col.add("swipe", "error", "Swipe next (left) opens the wrong screen", slide_id=sid, slide_label=lab,
                             screenshot=shot_n, vw=vw, detail=f"Expected {self.label(want)}, got {self.label(new)}")
            elif moved:
                if rec["next"] == "Not tested":
                    rec["next"] = "Pass – goes to the next screen"
                self.col.ran("swipe")
            if moved:
                continue
            if player_next:
                # Next works here, so swiping should too (unless Next itself does not move: then it is not a swipe issue)
                before = self._sig(self.state(page))
                self.click_first(page, SEL["next"])
                st2 = self.settle(page)
                new = self.identify(st2, expected=want if self._moved(before, self._sig(st2)) else None)
                if new != sid:
                    if rec["next"] == "Not tested":
                        rec["next"] = "Fail – does not go to the next screen"
                    self.col.add("swipe", "error", "Swipe next (left) does not go to the next screen (the Next button works)",
                                 slide_id=sid, slide_label=lab, screenshot=shot_n, vw=vw,
                                 detail=f"Swiped left on {lab}. Expected: {self.label(want)}. Result: stayed on the same screen.")
                    continue
            elif rec["next"] == "Not tested":
                rec["next"] = "Not available (custom Next button)" if custom_next else "Pass – stays (no way forward yet)"
            if not self.advance(page, sid, tap=True):
                if slide and slide.next_target and self.goto_slide(page, slide.next_target, tap=True) == slide.next_target:
                    continue
                if not onward(sid, "the way forward did not open the next screen"):
                    break
        # say plainly what the phone pass could not cover, instead of leaving the check blank
        missed = [i for i in ids if i not in seen]
        if missed:
            self.col.add("swipe", "warning",
                         f"Mobile testing could not reach {len(missed)} of {n} screens on the phone",
                         detail="Not tested on mobile: " + ", ".join(self.label(i) for i in missed[:15])
                                + (" …" if len(missed) > 15 else ""))
        for sid, why in stuck:
            self.col.add("swipe", "info", f"Mobile testing could not go forward from this screen: {why}",
                         slide_id=sid, slide_label=self.label(sid),
                         detail="The tester opened the next screen directly and carried on.")
        if not self.col.has_run("swipe") and not self.col.has_skip("swipe"):
            self.col.skip("swipe", "No screen could be swipe-tested on the phone")
        ctx.close()

    def _custom_menu_restriction(self, page):
        """Fresh session: open the screen's own Menu and click a screen not visited yet."""
        ids = self.course.ids()
        if not self._find_custom_menu(page):
            return None
        here = self.identify(self.state(page))
        opened = self.open_custom_menu(page)
        if not opened or not opened["items"]:
            return {"found": None, "note": "The Menu button did not open a menu"}
        items = [{"i": k, "text": o["label"]} for k, o in enumerate(opened["items"])]
        mapping = self.build_menu_map(items)
        later = [(i, sid) for i, sid in sorted(mapping.items())
                 if sid in ids and here in ids and ids.index(sid) > ids.index(here) + 1]
        if not later:
            return {"found": None, "note": "Too few menu items after this screen to test"}
        idx, target = later[min(1, len(later) - 1)]
        o = opened["items"][idx]
        shot = self.shot(page, "restrict_menu")
        before = self._sig(self.state(page))
        self.click_box(page, self._obox(o))
        st = self.settle(page)
        got = self.identify(st, expected=target if self._moved(before, self._sig(st)) else None)
        moved = got not in (here, None) and not (got or "").startswith("unknown")
        return {"found": "free" if moved else "restricted", "shot": shot, "box": self._obox(o),
                "note": f"Clicked “{o['label']}” in the course's Menu before visiting it: "
                        + (f"it opened {self.label(got)}." if moved else "it did not open.")}

    def restrict_pass(self, browser):
        """Beta: are menu, seekbar and Next restricted the way the SOW says?"""
        exp = {k: v for k, v in (self.expect or {}).items() if v in ("restricted", "free")}
        if not exp:
            return
        ctx = browser.new_context(viewport={"width": 1366, "height": 768}, device_scale_factor=2)
        page = self.open(ctx, "restrictions")
        obs = {}
        ids = self.course.ids()

        # Menu: in a fresh session only the first slide has been seen; try to open a later slide
        if "menu" in exp:
            items = self.ensure_menu(page)
            mm = self.build_menu_map(items) if items else {}
            start = self.identify(self.state(page), expected=ids[0] if ids else None)
            later = [(i, sid) for i, sid in sorted(mm.items()) if sid in ids and start in ids and ids.index(sid) > ids.index(start) + 1]
            if not items:
                obs["menu"] = self._custom_menu_restriction(page) or {"found": None, "note": "No course menu found"}
            elif not later:
                obs["menu"] = {"found": None, "note": "No course menu, or too few menu items to test"}
            else:
                idx, target = later[min(1, len(later) - 1)]
                shot = self.shot(page, "restrict_menu")
                try:
                    box = page.evaluate(self.MENU_BOX_JS, [SEL, idx])
                    self.click_menu_item(page, idx)
                except Exception:
                    box = None
                got = self.identify(self.settle(page))
                moved = got != start
                obs["menu"] = {"found": "free" if moved else "restricted", "shot": shot, "box": box,
                               "note": f"Clicked “{self.label(target)}” in the menu before visiting it: "
                                       + (f"it opened {self.label(got)}." if moved else "it did not open.")}
                self.close_menu_if_overlay(page)
                if moved:
                    page.reload(wait_until="load", timeout=90000)
                    self._ready(page)

        # start again from a clean session: the menu test may have left the menu open or a screen visited
        if "menu" in exp:
            try:
                self.fresh(page)
            except Exception:
                pass
        # Next and seekbar: walk the first slides; test each on arrival, while the slide is still playing
        next_seen, seek_seen = [], []
        for k_r in range(min(8, len(ids))):
            self.check_time()
            self.tick("restrict", k_r + 1, min(8, len(ids)) + 1)
            sid = self.identify(self.settle(page, 300))
            slide = self.course.by_id(sid)
            dur = (slide.duration_ms / 1000.0) if slide and slide.duration_ms else 0
            if dur >= 3:
                page.wait_for_timeout(600)
                st = self.state(page)
                moved_on = None
                if "next" in exp and sid != ids[0]:  # the title slide's START is meant to be available at once
                    if st["nextExists"]:
                        usable, box, lab = not st["nextDisabled"], st.get("nextBox"), "Next"
                        shot_n = self.shot(page, f"{sid}_restrict_next")
                    else:
                        # the screen's own NEXT can be on screen and still locked: press it and see if it moves
                        nav = self.slide_nav(page, slide, "forward")
                        box, lab = (nav or {}).get("box"), (nav or {}).get("label", "")
                        shot_n = self.shot(page, f"{sid}_restrict_next")
                        usable = False
                        if nav and not re.match(r"^\W*(start|begin|let'?s (go|begin|start)|get started)\b", lab or "", re.I):
                            b0 = self._sig(self.state(page))
                            self.click_box(page, box)
                            s2 = self.settle(page)
                            got = self.identify(s2)
                            if self._moved(b0, self._sig(s2)) and got != sid and not got.startswith("unknown"):
                                usable, moved_on = True, got
                    if not re.match(r"^\W*(start|begin|let'?s (go|begin|start)|get started)\b", lab or "", re.I):
                        next_seen.append({"sid": sid, "usable": usable, "box": box, "shot": shot_n})
                if "seekbar" in exp and not moved_on and st["seekVisible"] and st.get("seekBox"):
                    def frac(x):
                        if x.get("seekMax") and x.get("seekVal") is not None and x["seekMax"] > 0:
                            return x["seekVal"] / x["seekMax"]
                        return x.get("seekFill")
                    before = frac(st)
                    shot = self.shot(page, f"{sid}_restrict_seek")
                    b = st["seekBox"]
                    page.mouse.click(b[0] + b[2] * 0.85, b[1] + b[3] / 2)
                    page.wait_for_timeout(900)
                    after = frac(self.state(page))
                    if before is not None and after is not None:
                        seek_seen.append({"sid": sid, "jumped": after - before > 0.3, "box": b, "shot": shot})
                if moved_on:
                    continue   # pressing NEXT already opened the next screen
            if not self.advance(page, sid):
                break
        # Next "restricted" also means: once a screen is finished, coming back to it the learner can go on at once
        if exp.get("next") == "restricted" and next_seen and not any(x["usable"] for x in next_seen):
            try:
                obs["next_revisit"] = self._revisit_next(page, [x["sid"] for x in next_seen])
            except Stopped:
                raise
            except Exception as e:
                self.log(f"Revisit check skipped: {e}"[:160])
        if "next" in exp:
            early = [x for x in next_seen if x["usable"]]
            obs["next"] = ({"found": "free", **early[0], "note": f"Next could be used as soon as {self.label(early[0]['sid'])} opened, before it finished."}
                           if early else {"found": "restricted", "note": f"Next stayed locked until the slide finished on {len(next_seen)} slide{'s' if len(next_seen) != 1 else ''} tested."}
                           if next_seen else {"found": None, "note": "No slide long enough to test (3 s or more)."})
        if "seekbar" in exp:
            jumped = [x for x in seek_seen if x["jumped"]]
            obs["seekbar"] = ({"found": "free", **jumped[0], "note": f"Dragging the seekbar ahead on {self.label(jumped[0]['sid'])} skipped the slide forward."}
                              if jumped else {"found": "restricted", "note": f"Dragging the seekbar ahead did not skip forward on {len(seek_seen)} slide{'s' if len(seek_seen) != 1 else ''} tested."}
                              if seek_seen else {"found": "restricted", "note": "No seekbar is shown, so the learner cannot seek."})
        self.restrictions = {k: {"expected": exp.get(k, "free"), **v} for k, v in obs.items() if v}
        ctx.close()

    def _revisit_next(self, page, finished):
        """Goes back one screen to a screen already finished and checks Next can be used straight away."""
        cur = self.identify(self.state(page))
        st = self.state(page)
        b0 = self._sig(st)
        if st["prevExists"] and not st["prevDisabled"]:
            self.click_first(page, SEL["prev"])
        else:
            back = self.slide_nav(page, self.course.by_id(cur), "back")
            if not back:
                return None
            self.click_box(page, back["box"])
        s2 = self.settle(page, 300)
        got = self.identify(s2, expected=None)
        if not self._moved(b0, self._sig(s2)) or got == cur or got not in finished:
            return None
        page.wait_for_timeout(800)   # the screen has just opened again: its narration is far from finished
        st = self.state(page)
        shot = self.shot(page, f"{got}_revisit_next")
        if st["nextExists"]:
            usable, box = not st["nextDisabled"], st.get("nextBox")
        else:
            nav = self.slide_nav(page, self.course.by_id(got), "forward")
            if not nav:
                return None
            box = nav["box"]
            b1 = self._sig(self.state(page))
            self.click_box(page, box)
            s3 = self.settle(page)
            usable = self._moved(b1, self._sig(s3)) and self.identify(s3) != got
        return {"found": "free" if usable else "restricted", "sid": got, "shot": shot, "box": box,
                "note": f"Came back to {self.label(got)} after finishing it: Next "
                        + ("could be used at once." if usable else "was locked again until the screen played to the end.")}

    # ------------------------------------------------------------ main
    def run(self, passes=("desktop", "prev", "menu", "buttons", "mobile")):
        """Runs the browser checks: two waves, two browsers side by side in each (see progress.WAVES)."""
        import threading
        from .progress import WAVES, REPORT_ORDER
        passes = list(passes)
        bufs, self._lane_frac = {}, {}
        for wave, lanes in WAVES:
            lanes = [[x for x in lane if x in passes] for lane in lanes]
            lanes = [lane for lane in lanes if lane]
            if not lanes:
                continue
            if self.blocked:
                for lane in lanes:
                    for name in lane:
                        self.log(f"Skipping the {name} pass: testing stopped at {self.label(self.blocked['sid'])}")
                continue
            self._wave, self._wave_lanes = wave, lanes
            self._lane_frac = {k: 0.0 for k in range(len(lanes))}
            from .release import browser
            name, ver = browser()   # chosen once, before any browser session is open
            self.log(f"Browser: {'Google Chrome' if name == 'chrome' else 'Chromium'} {ver}")
            ths = [threading.Thread(target=self._lane, args=(k, lane, bufs), daemon=True) for k, lane in enumerate(lanes)]
            for th in ths:
                th.start()
            for th in ths:
                th.join()
            if wave == "checks1" and "_learned" in bufs:
                self._learned_base = bufs.pop("_learned")
        if self.blocked:
            # as if testing had stopped there: what ran beside the walk-through is not reported
            for name in ("mobile",):   # the assessment plan is kept: it does not depend on the walk-through
                if name in bufs:
                    bufs.pop(name)
                    self.log(f"Skipping the {name} pass: testing stopped at {self.label(self.blocked['sid'])}")
            self.swipes = {}
        for name in REPORT_ORDER:
            self.col.replay(bufs.pop(name, []))
        for name in sorted(bufs):
            self.col.replay(bufs[name])
        if self.assessment:
            for q in self.assessment.get("questions") or []:
                for ls in (self.assessment.get("layer_shots") or {}).get(q["sid"], []):
                    self.visits.setdefault(q["sid"], {}).setdefault("layer_shots", []).append(ls)
            self.assessment.pop("layer_shots", None)
        return self.visits

    def _lane(self, k, lane, bufs):
        with sync_playwright() as pw:
            from .release import launch   # Google Chrome when installed, else the frozen Chromium build
            browser = launch(pw, args=["--autoplay-policy=no-user-gesture-required", "--no-sandbox"])
            try:
                for j, name in enumerate(lane):
                    if not hasattr(self, "_tl"):
                        self._tl = threading.local()
                    self._tl.name = name
                    if name == "assessment":
                        self.assess_deadline = time.time() + 60 * 60
                    if self.blocked and name not in ("desktop", "assessment"):
                        self.log(f"Skipping the {name} pass: testing stopped at {self.label(self.blocked['sid'])}")
                        continue
                    self.pass_name = name
                    self._lane_pos = (k, j, len(lane))
                    self.col.start_buffer()
                    self.log(f"Starting {name} pass")
                    try:
                        if name == "mobile":
                            self.col.mobile_mode = True
                            self.mobile_pass(pw, browser)
                        else:
                            getattr(self, f"{name}_pass")(browser)
                    except Stopped:
                        pass
                    except Deadline:
                        self.col.add("navigation", "warning", f"Time limit reached during the {name} pass; later checks skipped",
                                     detail="The test stops at twice the course's running time so it never hangs. "
                                            "What was tested up to then is in this report.")
                        bufs[name] = self.col.take_buffer()
                        break
                    except PWTimeout as e:
                        self.col.add("navigation", "warning", f"The {name} pass timed out", detail=str(e)[:300])
                    except Exception as e:  # keep going with the other passes
                        self.col.add("navigation", "warning", f"The {name} pass stopped early", detail=repr(e)[:300])
                    finally:
                        self.col.mobile_mode = False
                        if name not in bufs:
                            bufs[name] = self.col.take_buffer()
                        if name == "desktop":
                            bufs["_learned"] = list(self.learned)
                        self._lane_frac[k] = (j + 1) / len(lane)
                        self._report_wave()
            finally:
                browser.close()

    def _report_wave(self):
        if self.on_progress and self._lane_frac:
            try:
                # the wave ends when its slowest lane does
                self.on_progress(self._wave, min(self._lane_frac.values()) * 1000, 1000)
            except Exception:
                pass
