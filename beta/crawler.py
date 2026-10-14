"""Drives the published course in headless Chromium like a learner would.

Passes: desktop Next walk -> Prev walk -> Menu -> Buttons -> Mobile swipe.
Every finding goes into the shared Collector.
"""
import hashlib
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from .player import (SEL, PLAYER_BUTTONS, VISITED_CLASSES, CURRENT_CLASSES, INIT_SCRIPT,
                     STATE_JS, FINGERPRINT_JS, CLICKABLES_JS, MENU_JS, TEXTBOX_JS, LMS_JS, TRANSCRIPT_JS)

WORD = re.compile(r"[a-z0-9']+")


def _norm(s):
    return " ".join(WORD.findall((s or "").lower()))


WELCOME_RE = re.compile(r"\b(welcome|start|begin|intro|introduction|title|splash|launch|home|cover)\b", re.I)


def is_welcome(slide):
    """The opening screen (welcome / title / START page). It is often left out of the menu and uses a START
    button instead of Next – both are normal and never reported."""
    if not slide:
        return False
    return slide.index == 1 or (slide.index <= 2 and bool(WELCOME_RE.search(slide.title or "")))


class Deadline(Exception):
    pass


class Crawler:
    def __init__(self, course, url, run_dir: Path, col, play_full=True, max_minutes=120, log=print,
                 max_buttons=25):
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

    # ------------------------------------------------------------ utilities
    def tick(self, name, done, total):
        if self.on_progress:
            try:
                self.on_progress(name, done, total)
            except Exception:
                pass

    def check_time(self):
        if time.time() > self.deadline:
            raise Deadline()

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
        self.learned = []   # (words on screen, slide id) for screens recognised during this run
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

    def settle(self, page, ms=1200):
        page.wait_for_timeout(ms)
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
        for txt in ("No", "Start", "Start Course", "Begin", "Click to start", "Play"):
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
    def play_slide(self, page, sid, record=None):
        """Wait for the slide to play (or fast-forward it). Collects captions and seekbar samples."""
        s = self.course.by_id(sid)
        dur = (s.duration_ms / 1000.0) if s and s.duration_ms else 0
        t0 = time.time()
        if self.play_full:
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
            if not st["nextDisabled"] and elapsed >= (dur if self.play_full else 0.5) and not playing:
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
        return page.evaluate(MENU_JS, [SEL, VISITED_CLASSES, CURRENT_CLASSES])

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
            first_visit = sid not in self.visits
            rec = self.visits.setdefault(sid, {"captions": [], "seek": [], "media_requests": set(), "shot": "",
                                               "seek_visible": False, "text": st["text"], "title": st["title"]})
            if not first_visit and step > 0:
                self.col.add("navigation", "error", "Next loops back to a slide that was already visited",
                             slide_id=prev_sid or "", slide_label=self.label(prev_sid), detail=f"Went to {self.label(sid)}")
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
            try:
                rec["boxes"] = page.evaluate(TEXTBOX_JS)
            except Exception:
                rec["boxes"] = []
            self.capture_transcript(page, sid, rec)
            slide = self.course.by_id(sid)
            welcome = is_welcome(slide)
            start_nav = self.slide_nav(page, slide, "forward") if welcome and st["nextExists"] and st["nextDisabled"] else None

            if not st["nextExists"] or start_nav:
                # player without Next (custom navigation), or a welcome screen with its own START button
                nav = start_nav or self.slide_nav(page, slide, "forward")
                if nav is None and self._submit_box(page):
                    # a survey / text-entry screen: answer it and press Submit, like a learner
                    self._fill_inputs(page)
                    b = self._submit_box(page)
                    nav = {"box": b, "label": "Submit", "target": slide.next_target if slide else "NEXT"}
                if nav is None:
                    if slide and slide.index < n:
                        msg = None if slide.has_interaction else "No way forward on this slide: no Next button and no button that goes to another slide"
                        if not self._jump_on(page, sid, slide, msg, "error"):
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
                lab = nav["label"] or "the slide's forward button"
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
                if new == sid:
                    if not (slide and slide.has_interaction):   # an activity must be finished first: not a fault
                        self.col.add("navigation", "error", f"“{lab}” did not move to another slide", slide_id=sid,
                                     slide_label=self.label(sid), screenshot=nshot, box=nav["box"], vw=1366)
                    if not self._jump_on(page, sid, slide, None, None):
                        break
                elif expected and new != expected and not new.startswith("unknown"):
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
                if st["nextDisabled"] and not welcome:
                    # the learner may have to click the screen's tabs or answer first: do that, then look again
                    self._unstick(page, sid, slide, None)
                    st = self.state(page)
                if st["nextDisabled"]:
                    if slide and slide.index >= n:
                        break
                    if welcome or (slide and slide.has_interaction):   # welcome screen: moving on with START (or a click) is normal
                        ok = self._jump_on(page, sid, slide, None, None)
                    else:
                        ok = self._jump_on(page, sid, slide, "Next stays locked after the slide finished", "warning",
                                           "The tester clicked the items on the screen and waited, but Next stayed locked.")
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
            if new == sid:
                if not (slide and slide.has_interaction):
                    self.col.add("navigation", "error", "Next button did not move to another slide",
                                 slide_id=sid, slide_label=self.label(sid), screenshot=rec["shot"],
                                 box=st.get("nextBox"), vw=1366)
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
        if self.check_completion:
            try:
                self.capture_completion(page, ctx)
            except Exception as e:
                self.completion = {"error": repr(e)[:300]}
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
            got = self.goto_slide(page, s.id)
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
        page.mouse.click(box[0] + box[2] / 2, box[1] + box[3] / 2)

    def _box_of(self, page, model_id):
        try:
            return page.evaluate("""(id) => { for (const el of document.querySelectorAll('[data-model-id="' + id + '"]')) {
                const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                if (r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none') return [r.x, r.y, r.width, r.height]; }
                return null; }""", model_id)
        except Exception:
            return None

    def slide_nav(self, page, slide, direction="forward"):
        """The slide's own navigation button, for players without Next/Prev: {box, label, target} or None."""
        ids = self.course.ids()
        here = ids.index(slide.id) if slide and slide.id in ids else -1
        cands = []
        for o in (slide.nav_objects if slide else []):
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
        return None

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
            for word in ("Close", "Continue", "OK", "X"):   # close a pop-up layer, if one opened
                try:
                    b = page.get_by_role("button", name=re.compile(rf"^{word}$", re.I))
                    if b.count() and b.first.is_visible():
                        b.first.click(timeout=800)
                        page.wait_for_timeout(400)
                        break
                except Exception:
                    pass
        self.seek_to_end(page)
        page.wait_for_timeout(600)
        if retry is None:
            return sid
        return retry()

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
                    if not self.custom_nav:
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
                                 screenshot=pshot, box=back["box"], vw=1366)
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
                self.col.add("navigation", "error", "Prev button did not move back", slide_id=sid, slide_label=self.label(sid))
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
                    if not self.custom_nav and not is_welcome(slide):
                        self.col.add("navigation", "warning", "No Prev button on this slide", slide_id=b, slide_label=self.label(b))
                    continue
                pshot, box, lab = self.shot(page, f"{b}_back"), back["box"], back["label"] or "the slide's back button"
                before = self._sig(st)
                self.click_box(page, back["box"])
            st2 = self.settle(page)
            new = self.identify(st2, expected=a if self._moved(before, self._sig(st2)) else None)
            self.log(f"Back: {self.label(b)} -> {self.label(new)}")
            name = f"“{lab}”" if lab else "Prev button"
            if new == b:
                self.col.add("navigation", "error", f"{name} did not move back" if lab else "Prev button did not move back",
                             slide_id=b, slide_label=self.label(b), screenshot=pshot, box=box, vw=1366)
            elif new != a and not new.startswith("unknown") and not (slide and slide.prev_target == new):
                self.col.add("navigation", "warning", "Prev went to a different slide than the one before it",
                             slide_id=b, slide_label=self.label(b), screenshot=pshot, box=box, vw=1366,
                             detail=f"Expected {self.label(a)}, got {self.label(new)}")
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
        return self._as_obj(n["t"], n["b"]) if n else None

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
        return {"button": btn, "items": items, "close": close}

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

    def custom_menu_pass(self, page):
        if not self._find_custom_menu(page):
            self.col.skip("menu", "No course menu found: the player has no menu and no screen shows a Menu button")
            return
        home = self.identify(self.state(page))
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
                locked.append((sid, text, here))
            else:
                self.col.add("menu", "error", "Menu item opened the wrong screen", slide_id=sid, slide_label=self.label(sid),
                             detail=f"Menu item “{text}” opened {self.label(got)}", screenshot=self.shot(page, f"{sid}_menu"), vw=1366)
        for sid, text, here in locked:
            if expect == "free":
                self.col.add("menu", "error", "Menu item does not open its screen", slide_id=sid, slide_label=self.label(sid),
                             detail=f"Clicked “{text}” in the menu on {self.label(here)}; nothing opened")
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

        # slide objects
        targets = [sid for sid in self.order if not sid.startswith("unknown")] or self.course.ids()
        found_objs = 0
        for k_t, sid in enumerate(targets):
            self.check_time()
            self.tick("buttons", k_t, len(targets))
            s = self.course.by_id(sid)
            for name in (s.dead_click_objects if s else []):
                self.col.add("buttons", "error", "Object has a click trigger with no action", slide_id=sid,
                             slide_label=self.label(sid), detail=name)
            if self.identify(self.state(page)) != sid:
                got = self.goto_slide(page, sid)
                if got != sid:
                    continue
            self.current = sid
            self.settle_timeline(page)
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
                    b = self._box_of(page, co["id"])
                    if b and not any(inside({"x": b[0] + b[2] / 2, "y": b[1] + b[3] / 2}, nb) for nb in nav_boxes):
                        objs.append(self._as_obj(co["label"] or "", b))
                objs = objs[: self.max_buttons]
            found_objs += len(objs)
            self.log(f"Buttons: {self.label(sid)} – {len(objs)} clickable objects")
            for k, o in enumerate(objs):
                self.check_time()
                if o.get("label"):   # earlier clicks may have moved things (accordions, tabs): find it again
                    try:
                        now = [c for c in page.evaluate(CLICKABLES_JS, SEL) if c.get("label") == o["label"]]
                        if now:
                            o = min(now, key=lambda c: abs(c["y"] - o["y"]) + abs(c["x"] - o["x"]))
                    except Exception:
                        pass
                base, noise = self.fp_baseline(page)
                oshot = self.shot(page, f"{sid}_btn{k}")
                n_pages = len(ctx.pages)
                try:
                    page.mouse.click(o["x"], o["y"])
                except Exception:
                    continue
                page.wait_for_timeout(1000)
                changed = self.fp_changed(page, base, noise) or len(ctx.pages) != n_pages
                if not changed:   # pictures and animations do not always show in the page structure: compare the screen
                    page.wait_for_timeout(800)
                    changed = self._pixels_changed(oshot, page)
                if changed and len(ctx.pages) == n_pages and self.identify(self.state(page)) == sid:
                    # what the click revealed (a layer, a tab): keep it, so text shown only there can be pictured
                    try:
                        lay = self.shot(page, f"{sid}_layer{k}")
                        self.visits.setdefault(sid, {}).setdefault("layer_shots", []).append(
                            {"shot": lay, "boxes": page.evaluate(TEXTBOX_JS)})
                    except Exception:
                        pass
                if not changed:
                    self.col.add("buttons", "warning", "Clickable object does nothing when clicked", slide_id=sid,
                                 slide_label=self.label(sid), detail=o["label"] or f"object at {int(o['x'])},{int(o['y'])}",
                                 screenshot=oshot, box=[o["x"] - o["w"] / 2, o["y"] - o["h"] / 2, o["w"], o["h"]], vw=1366)
                else:
                    self.col.ran("buttons")
                    for p in ctx.pages[n_pages:]:
                        p.close()
                    if self.identify(self.state(page)) != sid:
                        if self.goto_slide(page, sid) != sid:
                            break
                        self.settle_timeline(page)
                    elif k < len(objs) - 1 and self.has_menu:
                        if self.goto_via_menu(page, sid) == sid:
                            self.settle_timeline(page)
            # on to the next slide without the menu: go forward like a learner
            if not self.has_menu:
                self.advance(page, sid)
        if targets and found_objs == 0:
            # say so plainly: a "pass" here would only mean the player's own buttons were tested
            self.col.add("buttons", "warning", "Buttons inside the screens could not be detected, so they were not clicked",
                         detail="Only the player's own buttons (play, volume, replay and so on) were tested. "
                                "Check the screens' own buttons by hand for this course.")
        ctx.close()

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
            stuck.append((sid, why))
            start = ids.index(sid) + 1 if sid in ids else 0
            for nid in ids[start:]:
                if nid in seen:
                    continue
                if self.goto_slide(page, nid, tap=True) == nid:
                    self.log(f"Mobile: moved on to {self.label(nid)} (could not go forward from {self.label(sid)})")
                    return True
            return False

        for _ in range(max(n * 2, 10)):
            self.check_time()
            st = self.state(page)
            sid = self.identify(st)
            if sid in seen:
                nxt = [i for i in ids if i not in seen]
                if not nxt or self.goto_slide(page, nxt[0], tap=True) != nxt[0]:
                    break
                continue
            seen.add(sid)
            self.current = sid
            lab = self.label(sid)
            self.log(f"Mobile: {lab}")
            self.tick("mobile", len(seen) - 1, n)
            slide = self.course.by_id(sid)
            if is_welcome(slide) and (not st["nextExists"] or st["nextDisabled"]):
                # welcome screen moved on with START: nothing to swipe-test here
                self.play_slide(page, sid)
                if not self.advance(page, sid, tap=True):
                    if not onward(sid, "START did not open the next screen"):
                        break
                continue
            # 1. while Next is locked, swipe left must not move
            if st["nextExists"] and st["nextDisabled"]:
                before = self.shot(page, f"{sid}_swipe_locked")  # the screen as it was, Next still locked
                new, _ = self.touch_swipe(page, cdp, "left", retry=False)
                if new != sid:
                    self.col.add("swipe", "error", "Swipe left moved to the next slide while Next was locked",
                                 slide_id=sid, slide_label=lab, detail=f"Moved to {self.label(new)}",
                                 screenshot=before, box=st.get("nextBox"), vw=page.viewport_size["width"])
                    self.goto_slide(page, sid, tap=True)
                else:
                    self.col.ran("swipe")
            # 2. while Prev is locked, swipe right must not move
            if st["prevExists"] and st["prevDisabled"]:
                new, _ = self.touch_swipe(page, cdp, "right", retry=False)
                if new != sid:
                    self.col.add("swipe", "error", "Swipe right moved back while Prev was locked",
                                 slide_id=sid, slide_label=lab, detail=f"Moved to {self.label(new)}")
                    self.goto_slide(page, sid, tap=True)
            st = self.play_slide(page, sid)
            if slide and slide.is_quiz and st["nextDisabled"]:
                self.try_quiz(page)
                st = self.state(page)
            if not st["nextExists"]:
                # the player shows no Next: the slide has its own buttons, so swiping should not skip ahead
                new, _ = self.touch_swipe(page, cdp, "left", retry=False)
                if new != sid and not new.startswith("unknown"):
                    self.col.add("swipe", "warning", "Swipe moved to another slide although the player shows no Next button",
                                 slide_id=sid, slide_label=lab, detail=f"Moved to {self.label(new)}",
                                 screenshot=self.shot(page, f"{sid}_swipe_custom"), vw=page.viewport_size["width"])
                    continue
                self.col.ran("swipe")
                if not self.advance(page, sid, tap=True):
                    if slide and slide.next_target and self.goto_slide(page, slide.next_target, tap=True) == slide.next_target:
                        continue
                    if not onward(sid, "the screen's own button did not open the next screen"):
                        break
                continue
            if st["nextDisabled"]:
                if slide and slide.index < n and slide.next_target:
                    if self.goto_slide(page, slide.next_target, tap=True) == slide.next_target:
                        continue
                if not onward(sid, "NEXT stayed locked after the screen finished"):
                    break
                continue
            # 3. Next enabled: swipe left must go forward
            new, st2 = self.touch_swipe(page, cdp, "left")
            if new == sid:
                swipe_shot = self.shot(page, f"{sid}_swipe_next")
                before = self._sig(self.state(page))
                self.click_first(page, SEL["next"])
                st2 = self.settle(page)
                new = self.identify(st2, expected=(slide.next_target if slide else None) if self._moved(before, self._sig(st2)) else None)
                if new != sid:   # Next works here, so swiping should too
                    self.col.add("swipe", "error", "Swipe left did not go to the next slide (Next is enabled)",
                                 slide_id=sid, slide_label=lab, screenshot=swipe_shot, vw=page.viewport_size["width"])
                    continue
                # Next does not move on either (a branching or activity screen): not a swipe problem, carry on
                if slide and slide.next_target and self.goto_slide(page, slide.next_target, tap=True) == slide.next_target:
                    continue
                if not onward(sid, "NEXT did not open the next screen"):
                    break
                continue
            self.col.ran("swipe")
            # 4. on the new slide, swipe right must go back when Prev is enabled
            if st2["prevExists"] and not st2["prevDisabled"]:
                back, _ = self.touch_swipe(page, cdp, "right")
                if back == new:
                    self.col.add("swipe", "error", "Swipe right did not go back (Prev is enabled)",
                                 slide_id=new, slide_label=self.label(new), screenshot=self.shot(page, f"{new}_swipe_prev"), vw=page.viewport_size["width"])
                else:
                    if back != sid:
                        self.col.add("swipe", "warning", "Swipe right went to an unexpected slide",
                                     slide_id=new, slide_label=self.label(new), detail=f"Went to {self.label(back)}")
                    # return to the new slide
                    if self.goto_slide(page, new, tap=True) != new:
                        self.click_first(page, SEL["next"])
                        self.settle(page)
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
        if "swipe" not in self.col.checks_run and "swipe" not in self.col.checks_skipped:
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
        """Beta: are menu, seekbar and Next restricted the way the RCD says?"""
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
                if "next" in exp and sid != ids[0]:  # the title slide's START is meant to be available at once
                    if st["nextExists"]:
                        usable, box, lab = not st["nextDisabled"], st.get("nextBox"), "Next"
                    else:
                        nav = self.slide_nav(page, slide, "forward")
                        usable, box, lab = nav is not None, (nav or {}).get("box"), (nav or {}).get("label", "")
                    if not re.match(r"^\W*(start|begin|let'?s (go|begin|start)|get started)\b", lab or "", re.I):
                        next_seen.append({"sid": sid, "usable": usable, "box": box,
                                          "shot": self.shot(page, f"{sid}_restrict_next")})
                if "seekbar" in exp and st["seekVisible"] and st.get("seekBox"):
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
            if not self.advance(page, sid):
                break
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
        self.restrictions = {k: {"expected": exp[k], **v} for k, v in obs.items()}
        ctx.close()

    # ------------------------------------------------------------ main
    def run(self, passes=("desktop", "prev", "menu", "buttons", "mobile")):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required", "--no-sandbox"])
            try:
                for name in passes:
                    self.log(f"Starting {name} pass")
                    self.tick(name, 0, 1)
                    try:
                        if name == "mobile":
                            self.col.mobile_mode = True
                            try:
                                self.mobile_pass(pw, browser)
                            finally:
                                self.col.mobile_mode = False
                        else:
                            getattr(self, f"{name}_pass")(browser)
                    except Deadline:
                        self.col.add("navigation", "warning", f"Time limit reached during the {name} pass; later passes skipped")
                        break
                    except PWTimeout as e:
                        self.col.add("navigation", "warning", f"The {name} pass timed out", detail=str(e)[:300])
                    except Exception as e:  # keep going with the other passes
                        self.col.add("navigation", "warning", f"The {name} pass stopped early", detail=repr(e)[:300])
            finally:
                browser.close()
        return self.visits
