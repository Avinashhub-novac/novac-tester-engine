"""Runs a full test for one upload and writes results.json."""
import json
import re
import shutil
import time
import traceback
import zipfile
from pathlib import Path

from . import config, media, claude, textcheck
from .findings import Collector, CHECKS
from .storyline import load_course


class Run:
    def __init__(self, run_dir: Path):
        self.dir = run_dir
        self.meta = json.loads((run_dir / "run.json").read_text())
        # stage rules: Alpha never tests mobile; Beta always runs the transcript check
        opts = self.meta.setdefault("options", {})
        if opts.get("stage") == "alpha":
            opts["swipe"] = False
        elif opts.get("stage") == "beta":
            opts["transcript"] = True
        self.col = Collector()
        # work handed to the Claude session that runs this engine (LANGUAGE_MODE=export)
        self.shot_boxes = {}
        self.tasks = {"proofread": [], "script_compare": [], "transcript_review": [], "sb_compare": {}}
        self.logf = open(run_dir / "log.txt", "a", encoding="utf-8")
        from .progress import Progress
        self.prog = Progress(run_dir)

    # --------------------------------------------------- status/log
    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        self.logf.write(line + "\n")
        self.logf.flush()
        self.meta["progress"] = msg
        self.save_meta()

    def save_meta(self):
        tmp = self.dir / "run.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=1))
        tmp.replace(self.dir / "run.json")

    def finish(self, slides, extra=None):
        f = [x.to_dict() for x in self.col.findings]
        from .release import stamp
        result = {"made_with": stamp(), "summary": summarize(f, self.col.checks_run, self.col.checks_skipped), "findings": f,
                  "slides": slides, "checks_run": sorted(self.col.checks_run),
                  "checks_skipped": self.col.checks_skipped, **(extra or {})}
        (self.dir / "results.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
        if any(self.tasks.values()):
            glossary = [g.strip() for g in self.meta.get("glossary", "").replace("\n", ",").split(",") if g.strip()]
            (self.dir / "claude_tasks.json").write_text(json.dumps({"glossary": glossary, **self.tasks},
                                                                    indent=1, ensure_ascii=False))
        self.prog.phase("review")   # the engine is done; Claude reviews the text next
        self.meta.update(status="done", finished=time.time(), progress="Finished",
                         totals={"errors": sum(x["severity"] == "error" for x in f),
                                 "warnings": sum(x["severity"] == "warning" for x in f)})
        self.save_meta()

    def execute(self):
        self.meta.update(status="running", started=time.time())
        self.save_meta()
        try:
            if self.meta["kind"] == "scorm":
                self.run_scorm()
            elif self.meta["kind"] == "sb":
                self.run_sb()
            else:
                self.run_video()
        except Exception as e:
            self.log("FAILED: " + repr(e))
            self.logf.write(traceback.format_exc())
            self.meta.update(status="failed", error=str(e), finished=time.time())
            self.save_meta()
        finally:
            self.logf.close()

    # --------------------------------------------------- shared text checks
    def proofread(self, items, label_of):
        glossary = [g.strip() for g in self.meta.get("glossary", "").replace("\n", ",").split(",") if g.strip()]
        if config.LANGUAGE_MODE == "export":
            self.col.ran("grammar"); self.col.ran("spelling")
            self.tasks["proofread"].extend(
                {"id": i["id"], "slide_id": i.get("slide_id", ""), "label": label_of(i), "source": i.get("source", ""),
                 "time": i.get("time", ""), "text": i["text"], "shot": i.get("shot", ""),
                 "cap_shots": i.get("cap_shots", [])} for i in items if i["text"].strip())
            return
        if not claude.available():
            for c in ("grammar", "spelling"):
                self.col.skip(c, "ANTHROPIC_API_KEY is not set on the server")
            return
        self.log(f"Claude is proofreading {len(items)} text items")
        try:
            issues = claude.proofread(items, glossary)
        except claude.ClaudeUnavailable as e:
            for c in ("grammar", "spelling"):
                self.col.skip(c, str(e))
            return
        self.col.ran("grammar"); self.col.ran("spelling")
        by_id = {i["id"]: i for i in items}
        for iss in issues:
            item = by_id.get(iss.get("item_id"), {})
            f = issue_to_finding(iss, {**item, "label": label_of(item)})
            self.col.add(f.pop("check"), f.pop("severity"), f.pop("message"), **f)

    # --------------------------------------------------- SCORM
    def run_scorm(self):
        from .crawler import Crawler
        upload = self.dir / self.meta["file"]
        content = self.dir / "content"
        self.prog.phase("extract")
        self.log("Extracting package")
        with zipfile.ZipFile(upload) as z:
            for m in z.infolist():
                target = (content / m.filename).resolve()
                if not str(target).startswith(str(content.resolve())):
                    raise ValueError(f"Unsafe path in zip: {m.filename}")
            z.extractall(content)
        course = load_course(content)
        # course videos in MP4 (H.264 / AAC) do not play in the open-source test browser: play a converted copy
        todo = media.media_to_prepare(course.root)
        self.media_estimate = media.prepare_estimate(todo)
        if todo:
            from .progress import plan_scorm as _plan
            self.prog.set_plan(_plan(course.slides, ["desktop"], True, bool(self.meta.get("storyboard")), True,
                                     self.media_estimate))
            self.prog.phase("media")
        self.media_prepared = media.make_playable(course.root, self.log, todo=todo, tick=self.prog.tick)
        for w in course.warnings:
            self.col.add("navigation", "warning", w)
        self.log(f"Course model: {len(course.slides)} slides" + (f" – {course.title}" if course.title else ""))
        rel = course.root.relative_to(self.dir).as_posix()
        url = f"http://127.0.0.1:{config.PORT}/runs/{self.meta['id']}/{rel}/{course.launch_file}"
        opts = self.meta.get("options", {})
        stage = opts.get("stage", "beta")
        gold = stage == "gold"
        passes = [p for p in ("desktop", "restrict", "prev", "menu", "buttons", "assessment", "mobile") if opts.get(p, True)]
        if stage != "beta" or not any(v in ("restricted", "free") for v in (opts.get("expect") or {}).values()):
            passes.remove("restrict")
        if gold:   # Gold / Showstopper: functionality only (navigation and the assessment)
            passes = ["desktop", "prev", "assessment"]
        from .quiz import assessment_parts as _ap
        if not any(s.is_quiz for s in course.slides) and not _ap(course)[1]:
            passes = [p for p in passes if p != "assessment"]
        # time limit: twice the course's running time plus 10 minutes (at least 20), so a test never hangs
        play_min = sum(min(s.duration_ms, 600000) for s in course.slides) / 60000.0
        limit = min(config.MAX_CRAWL_MINUTES, max(20.0, 2 * play_min + 10))
        self.log(f"Course running time about {play_min:.0f} min; the browser checks stop after {limit:.0f} min at the latest")
        crawler = Crawler(course, url, self.dir, self.col, play_full=opts.get("play_full", True),
                          max_minutes=limit, log=self.log, max_buttons=config.MAX_BUTTONS_PER_SLIDE)
        crawler.expect = opts.get("expect") or {}
        beta = opts.get("stage", "beta") == "beta"
        crawler.check_completion = beta or gold   # Beta: completion; Gold: the assessment result sent to the LMS
        if not opts.get("swipe", True) and "mobile" in passes:
            passes.remove("mobile")
        from .progress import plan_scorm
        self.prog.set_plan(plan_scorm(course.slides, passes, opts.get("play_full", True),
                                      bool(self.meta.get("storyboard")), opts.get("transcript", True),
                                      getattr(self, "media_estimate", 0.0)))
        crawler.on_progress = self.prog.tick
        crawler.run_menu = "menu" in passes
        visits = crawler.run(passes)
        self.prog.phase("analyse")
        self.crawler, self.passes = crawler, passes
        if crawler.blocked:
            stop = crawler.label(crawler.blocked["sid"])
            for c in ("menu", "buttons", "swipe", "restrictions", "completion", "interactivity"):
                if c not in self.col.checks_run:
                    self.col.skip(c, f"Testing stopped at {stop}: the learner cannot go past this screen")
            if "assessment" in passes:
                self.col.skip("assessment", f"Testing stopped at {stop}, so the assessment was not tested")
        tables = self.test_tables(course, visits, crawler, passes)
        for v in visits.values():   # every picture taken of a screen, with where its text sits
            for key in ("cap_shots", "layer_shots", "text_shots"):
                for c in v.get(key, []) or []:
                    if c.get("shot"):
                        self.shot_boxes[c["shot"]] = c.get("boxes") or {}
            if v.get("transcript_shot"):
                self.shot_boxes[v["transcript_shot"]] = v.get("transcript_boxes") or {}
        if crawler.blocked:   # the rest of the review covers only the screens the learner can reach
            reach = set(crawler.order)
            course.slides = [x for x in course.slides if x.id in reach]
        if gold:
            for c in ("menu", "buttons", "swipe", "seekbar", "screen_number", "transcript", "content", "restrictions", "completion"):
                self.col.skip(c, "Gold / Showstopper: only major language errors, navigation and the assessment are checked")
            self.assessment_check(course, crawler, visits)
            self.gold_language(course, visits)
            slides = self.slide_list(course, visits, crawler)
            self.finish(slides, {"course_title": course.title, "slide_count": len(course.slides),
                                 "completion": crawler.completion, "shot_boxes": self.shot_boxes, "stage": "gold",
                                 **tables})
            return
        if "mobile" not in passes:
            self.col.skip("swipe", "Alpha review: mobile is not tested" if opts.get("stage") == "alpha"
                          else "Mobile pass turned off for this run")
        if "buttons" not in passes:
            self.col.skip("buttons", "Buttons pass turned off for this run")

        # ---- restrictions (Beta): compare what the course does with what the RCD says
        names = {"menu": "Menu", "seekbar": "Seekbar", "next": "Next button"}
        where = {"menu": "Menu", "seekbar": "Seek Bar", "next": "Navigation"}
        if "restrict" in passes:
            self.col.ran("restrictions")
            for k, o in crawler.restrictions.items():
                if o.get("found") is None:
                    continue
                if o["found"] != o["expected"]:
                    want = "restricted" if o["expected"] == "restricted" else "free (not restricted)"
                    self.col.add("restrictions", "error",
                                 f"{names[k]} is {'not restricted' if o['found'] == 'free' else 'restricted'}, but it should be {want} as per RCD",
                                 slide_id=o.get("sid", ""), slide_label=self.label_of(course, o.get("sid", "")),
                                 detail=o.get("note", ""), screenshot=o.get("shot", ""), box=o.get("box"), vw=1366,
                                 found_in=where[k])
        else:
            self.col.skip("restrictions", "Alpha review: restrictions are not checked" if opts.get("stage") == "alpha"
                          else "No restrictions selected for this test")

        # ---- completion reported to the LMS
        comp = crawler.completion
        if not beta:
            self.col.skip("completion", "Alpha review: completion status is checked in Beta only")
        elif comp and "error" not in comp:
            self.col.ran("completion")
            shot = comp.get("panel_shot", "")
            if not comp["connected"]:
                self.col.add("completion", "warning" if course.launch_file == "story.html" else "error",
                             "The course never connected to the LMS (no SCORM calls)", screenshot=shot,
                             box=comp.get("panel_box"), vw=1060,
                             detail=f"Launched {course.launch_file}. Upload the LMS-published zip (Publish → LMS) to test tracking.")
            elif not comp["completed"] and self._passed_by_quiz(crawler):
                # the walk-through answers questions without knowing the answers, so it can fail the quiz;
                # the assessment test passed it and the LMS then showed the course as passed / completed
                self.col.add("completion", "info", "Course is completed by passing the assessment",
                             detail="Answering every question correctly sent “passed” / “completed” to the LMS.")
            elif not comp["completed"]:
                st = comp["completion_status"] or comp["lesson_status"] or "no status sent"
                self.col.add("completion", "error", f"Course did not report completion after the last slide (status: {st})",
                             screenshot=shot, box=comp.get("panel_box"), vw=1060, detail="Check the tracking settings in Publish → LMS and the completion trigger.")
        elif "desktop" in passes:
            self.col.skip("completion", "Completion could not be read" + (f": {comp['error']}" if comp else ""))

        # ---- seekbar (audio itself is not checked)
        shot_boxes = self.shot_boxes
        for s in course.slides:
            v = visits.get(s.id)
            if not v:
                continue
            heard = any(str(m).lower().endswith((".mp3", ".m4a", ".wav", ".ogg", ".aac", ".mp4", ".webm", ".m4v"))
                        for m in v.get("media_requests") or ())
            if v.get("seek_visible") and not s.audio and not s.video and not heard:
                self.col.add("seekbar", "error", "Empty seekbar: the seekbar is shown but the slide has nothing to play",
                             slide_id=s.id, slide_label=s.label, screenshot=v["shot"],
                             box=v.get("seek_box"), vw=v.get("vw", 1366))
            elif v.get("seek_visible") and crawler.play_full and not v.get("via_menu"):
                vals = {(x[1], round(x[2] or 0, 2)) for x in v["seek"]}
                if len(v["seek"]) > 4 and len(vals) <= 1:
                    self.col.add("seekbar", "warning", "Seekbar did not move while the slide played",
                                 slide_id=s.id, slide_label=s.label, screenshot=v["shot"],
                                 box=v.get("seek_box"), vw=v.get("vw", 1366))
        self.col.ran("seekbar")

        # ---- grammar / spelling (Claude): on-screen text, transcript and captions
        items = []
        for s in course.slides:
            v = visits.get(s.id, {})
            caps = s.captions or v.get("captions", [])
            cap_shots = [{"text": c["text"], "shot": c["shot"]} for c in v.get("cap_shots", []) if c.get("shot")]
            for c in v.get("cap_shots", []):
                if c.get("shot"):
                    shot_boxes[c["shot"]] = c.get("boxes") or {}
            for c in v.get("layer_shots", []):
                if c.get("shot"):
                    shot_boxes[c["shot"]] = c.get("boxes") or {}
            for c in v.get("text_shots", []):   # pictures taken as text appeared while the screen played
                if c.get("shot"):
                    shot_boxes[c["shot"]] = c.get("boxes") or {}
            if v.get("transcript_shot"):
                shot_boxes[v["transcript_shot"]] = v.get("transcript_boxes") or {}
            tr_text = v.get("transcript_text") or s.notes
            tr_shot = v.get("transcript_shot", "") if v.get("transcript_text") else ""
            if tr_shot:
                shot_boxes[tr_shot] = v.get("transcript_boxes") or {}
            parts = [("slide title", s.title, {}), ("on-screen text", "\n".join(s.texts), {}),
                     ("transcript", tr_text, {"shot": tr_shot}),
                     ("captions", " ".join(caps), {"cap_shots": cap_shots})]
            for src, txt, extra in parts:
                if txt and txt.strip():
                    items.append({"id": f"{s.id}|{src}", "slide_id": s.id, "slide": s.label, "source": src,
                                  "text": txt, **extra})
        if not opts.get("transcript", True):
            items = [i for i in items if i["source"] not in ("captions", "transcript")]
        self.proofread(items, lambda it: it.get("slide", ""))
        self.english_check(course, visits, opts.get("english", "auto"))
        if opts.get("stage") == "alpha":   # Alpha: logic and facts are reviewed too
            self.col.ran("logic")
            self.tasks["logic_check"] = [{"slide_id": s.id, "label": s.label, "title": s.title,
                                          "text": "\n".join(s.texts), "transcript": visits.get(s.id, {}).get("transcript_text") or s.notes}
                                         for s in course.slides if s.texts or s.notes]

        # ---- transcript (captions present, and captions vs the transcript script)
        self.transcript_scorm(course, visits)

        # ---- content mapping against the storyboard (Claude compares)
        sb_slides = []
        for s in course.slides:
            v = visits.get(s.id, {})
            sb_slides.append({"id": s.id, "label": s.label, "screen": s.index, "title": s.title,
                              "on_screen_text": "\n".join(s.texts), "captions": " ".join(s.captions or v.get("captions", [])),
                              "transcript": v.get("transcript_text") or s.notes, "reached": s.id in crawler.order,
                              "shot": v.get("shot", ""),
                              "cap_shots": [{"text": x["text"], "shot": x["shot"]} for x in v.get("cap_shots", []) if x.get("shot")]})
        self.storyboard_task(sb_slides)

        # ---- screen numbers
        # screen numbers follow story order; slides missing from the model fall back to the Next path
        order = course.ids() or [sid for sid in crawler.order if not sid.startswith("unknown")]
        seq = []
        for sid in order:
            s = course.by_id(sid)
            dom = visits.get(sid, {}).get("text", "")
            seq.append((sid, s.label if s else sid, dom + "\n" + (s.all_text() if s else ""), s.title if s else ""))
        if crawler.blocked:   # only the screens the learner can reach
            reach = set(crawler.order)
            seq = [x for x in seq if x[0] in reach]
        from .crawler import is_welcome, is_end
        n_all = len(course.slides)
        textcheck.check_screen_numbers(seq, self.col, assess=crawler.assess_ids, results=crawler.results_ids,
                                       welcome={s.id for s in course.slides if is_welcome(s) or is_end(s, n_all)},
                                       partial=bool(crawler.blocked))

        slides = self.slide_list(course, visits, crawler)
        self.finish(slides, {"course_title": course.title, "slide_count": len(course.slides),
                             "completion": crawler.completion, "shot_boxes": shot_boxes,
                             "restrictions": crawler.restrictions, "stage": opts.get("stage", "beta"),
                             "media_prepared": self.media_prepared,
                             **tables})

    @staticmethod
    def _passed_by_quiz(crawler):
        A = getattr(crawler, "assessment", None) or {}
        for s in A.get("scenarios") or []:
            if s.get("name") == "All answers correct":
                lms = s.get("lms") or {}
                st = " ".join(str(lms.get(k) or "") for k in ("status", "success", "completion")).lower()
                return "passed" in st or "completed" in st
        return False

    def test_tables(self, course, visits, crawler, passes):
        """Navigation test (every screen: Next, Back, menu, clickables) and the assessment test, for the report tabs."""
        from .crawler import is_welcome
        from .wording import plain
        ids = course.ids()
        stop_at = ids.index(crawler.blocked["sid"]) if crawler.blocked and crawler.blocked["sid"] in ids else None
        rows = []
        for k, s in enumerate(course.slides):
            v = visits.get(s.id, {})
            fs = [f for f in self.col.findings if f.slide_id == s.id and not f.mobile and f.severity in ("error", "warning")
                  and f.check in ("navigation", "menu", "buttons", "interactivity")]
            back_f = [f for f in fs if f.check == "navigation" and re.search(r"\b(prev|back)\b", f.message, re.I)
                      and "goes back to an earlier" not in f.message]
            next_f = [f for f in fs if f.check == "navigation" and f not in back_f]
            menu_f = [f for f in fs if f.check == "menu"]
            click_f = [f for f in fs if f.check in ("buttons", "interactivity")]
            after_stop = stop_at is not None and k > stop_at
            no_nav = is_welcome(s) or s.id in crawler.quiz_ids or s.id in crawler.results_ids
            nt = "Not tested (testing stopped)" if after_stop else "Not tested"

            def res(found, ran, na=False):
                if after_stop:
                    return nt
                if found:
                    return "Fail"
                if na:
                    return "N/A"
                return "Pass" if ran else "Not tested"
            last = k == len(course.slides) - 1
            reached = s.id in crawler.order
            c = crawler.clicks.get(s.id)
            if after_stop:
                clicks = nt
            elif s.id in crawler.quiz_ids or s.id in crawler.results_ids:
                clicks = "See Assessment tab" if crawler.assessment else ("Fail" if click_f else "Not tested")
            elif c is not None:
                n = len(c["items"])
                dead = sum(1 for i in c["items"] if not i["opened"])
                stuck = sum(1 for i in c["items"] if i["closed"] is False)
                if not click_f:
                    clicks = f"Pass ({n} item{'s' if n != 1 else ''} opened" + (" and closed)" if any(i["closed"] for i in c["items"]) else ")") if n else "No clickable present"
                else:
                    bits = [f"{dead} of {n} did nothing" if dead else "", f"{stuck} pop-up{'s' if stuck != 1 else ''} did not close" if stuck else "",
                            c.get("rule") or ""]
                    clicks = "Fail" + (" – " + "; ".join(b for b in bits if b) if any(bits) else "")
            elif s.id in crawler.quiz_ids:
                clicks = "See Assessment tab"
            else:
                clicks = "Fail" if click_f else ("Not tested" if "buttons" not in passes or crawler.blocked else "No clickable present")
            row = {"sid": s.id, "index": s.index, "title": s.title, "label": s.label,
                   "reached": "Not tested (testing stopped)" if after_stop else ("Yes" if reached else (
                       "Question bank – not drawn in this attempt" if s.draw else ("No – opened directly" if v.get("via_jump") else "No"))),
                   "next": res(next_f, reached or k == 0, na=last and not next_f),
                   "back": res(back_f, "prev" in passes and not crawler.blocked, na=(no_nav or k == 0) and not back_f),
                   "menu": res(menu_f, "menu" in passes and not crawler.blocked) if not (
                       "menu" in self.col.checks_skipped and not menu_f and not after_stop) else
                       ("No menu found" if self.col.checks_skipped["menu"].startswith("No course menu") else "Not tested"),
                   "swipe_next": nt if after_stop else (getattr(crawler, "swipes", {}).get(s.id, {}).get("next")
                                  or ("Not tested" if "mobile" in passes else "Not in this stage")),
                   "swipe_back": nt if after_stop else (getattr(crawler, "swipes", {}).get(s.id, {}).get("back")
                                  or ("Not tested" if "mobile" in passes else "Not in this stage")),
                   "clicks": clicks,
                   "notes": "; ".join(dict.fromkeys(plain(f.message)[0] for f in fs))[:600] + (("; " if fs else "") + v["note"] if v.get("note") else "")}
            if is_welcome(s):
                row["notes"] = (row["notes"] + "; " if row["notes"] else "") + "Welcome screen: no Back/Next expected (START moves on)"
            if (s.id in crawler.quiz_ids or s.id in crawler.results_ids) and not after_stop:
                # the assessment has no Next / Previous (it moves on as questions are answered); results need no menu
                for k_ in ("next", "back", "swipe_next", "swipe_back"):
                    row[k_] = "N/A (assessment)"
                if s.id in crawler.results_ids:
                    row["menu"] = "N/A (not needed)"
                row["notes"] = (row["notes"] + "; " if row["notes"] else "") + "Assessment screen: no Back/Next expected"
            vals = [row["next"], row["back"], row["menu"], row["clicks"], row["swipe_next"], row["swipe_back"]]
            row["result"] = ("Not tested" if after_stop else "Fail" if any(str(x).startswith("Fail") for x in vals)
                             else "Pass")
            if crawler.blocked and crawler.blocked["sid"] == s.id:
                row["result"] = "Fail – testing stopped here"
            rows.append(row)
        return {"nav_table": rows, "assessment": crawler.assessment, "blocked": crawler.blocked, "passes": list(passes)}

    def slide_list(self, course, visits, crawler):
        slides = []
        for s in course.slides:
            v = visits.get(s.id, {})
            slides.append({"id": s.id, "label": s.label, "index": s.index, "title": s.title, "shot": v.get("shot", ""),
                           "vw": v.get("vw", 1366), "boxes": v.get("boxes", {}), "reached": s.id in crawler.order,
                           "audio": s.audio, "duration": round(s.duration_ms / 1000, 1),
                           "captions": (s.captions or v.get("captions", []))[:50]})
        return slides

    def english_check(self, course, visits, variant):
        """US / UK spelling must follow the course's choice (or, when not chosen, the spelling the course mostly uses)."""
        from . import sbreview
        pages = {s.index: "\n".join([s.title] + s.texts) for s in course.slides}
        if variant not in ("us", "uk"):
            variant = sbreview.majority_variant(pages)
        self.meta.setdefault("options", {})["english_used"] = variant
        self.tasks["english"] = variant
        for s in course.slides:
            words = set(re.findall(r"[a-z]+", "\n".join([s.title] + s.texts).lower()))
            for us, uk in sbreview.US_UK:
                if us in sbreview.AMBIGUOUS:
                    continue
                wrong, right = (uk, us) if variant == "us" else (us, uk)
                for w in words:
                    if (w == wrong or (len(wrong) > 4 and w.startswith(wrong) and w[len(wrong):] in ("s", "d", "ed", "ing", "es", "al", "ally"))) and not w.startswith(right):
                        self.col.add("spelling", "warning", f"Spelling in on-screen text: “{w}”", slide_id=s.id, slide_label=s.label,
                                     detail=f"{'UK' if variant == 'us' else 'US'} spelling in a {'US' if variant == 'us' else 'UK'} English course",
                                     suggestion=right + w[len(wrong):], screenshot=visits.get(s.id, {}).get("shot", ""))

    def assessment_check(self, course, crawler, visits):
        """Gold: the assessment works – every question can be reached, there is a result, and the result reaches the LMS."""
        self.col.ran("assessment")
        quiz = [s for s in course.slides if s.is_quiz]
        rules = [s for s in course.slides if re.search(r"\b(rules?|instructions?|assessment)\b", s.title, re.I) and not s.is_quiz]
        results = [s for s in course.slides if re.search(r"\b(results?|score|summary)\b", s.title, re.I)]
        shot = lambda sid: visits.get(sid, {}).get("shot", "")
        if not quiz:
            self.col.add("assessment", "info", "No question screens found in this course")
            return
        for s in quiz:
            if s.id not in crawler.order:
                self.col.add("assessment", "error", "Question screen cannot be reached", slide_id=s.id, slide_label=s.label,
                             screenshot=shot(s.id))
        if not results:
            self.col.add("assessment", "warning", "No results screen found after the questions", slide_id=quiz[-1].id,
                         slide_label=quiz[-1].label, screenshot=shot(quiz[-1].id))
        elif results[-1].id not in crawler.order:
            self.col.add("assessment", "error", "Results screen cannot be reached", slide_id=results[-1].id,
                         slide_label=results[-1].label, screenshot=shot(results[-1].id))
        comp = crawler.completion or {}
        if comp and "error" not in comp:
            if comp.get("connected") and not comp.get("score"):
                self.col.add("assessment", "error", "The quiz score is not sent to the LMS", screenshot=comp.get("panel_shot", ""),
                             box=comp.get("panel_box"), vw=1060)
            if comp.get("connected") and not (comp.get("success_status") or comp.get("lesson_status") in ("passed", "failed", "completed")) \
                    and not self._passed_by_quiz(crawler):
                self.col.add("assessment", "error", "Pass / fail result is not sent to the LMS", screenshot=comp.get("panel_shot", ""),
                             box=comp.get("panel_box"), vw=1060)
        quiz_scoring = [x for x in course.scorings if x.get("type") != "view"]
        self.tasks["assessment_review"] = {
            "question_screens": [{"slide_id": s.id, "label": s.label, "text": "\n".join(s.texts)[:3000]} for s in quiz],
            "rules_screens": [{"slide_id": s.id, "label": s.label, "text": "\n".join(s.texts)[:3000]} for s in rules],
            "results_screens": [{"slide_id": s.id, "label": s.label, "text": "\n".join(s.texts)[:2000]} for s in results],
            "settings": quiz_scoring, "lms": {k: comp.get(k) for k in ("score", "score_max", "lesson_status", "success_status")},
            "already_found": [f"{f.slide_label}: {f.message}" for f in self.col.findings if f.check == "assessment"]}

    def gold_language(self, course, visits):
        """Gold: on-screen text and titles only, and only major spelling and grammar mistakes."""
        items = []
        for s in course.slides:
            for src, txt in (("slide title", s.title), ("on-screen text", "\n".join(s.texts))):
                if txt and txt.strip():
                    items.append({"id": f"{s.id}|{src}", "slide_id": s.id, "slide": s.label, "source": src, "text": txt,
                                  "shot": visits.get(s.id, {}).get("shot", "")})
        self.tasks["mode"] = "gold"
        self.proofread(items, lambda it: it.get("slide", ""))

    def transcript_scorm(self, course, visits):
        if not self.meta.get("options", {}).get("transcript", True):
            self.col.skip("transcript", "Transcript check turned off for this run")
            return
        self.col.ran("transcript")
        for s in course.slides:
            v = visits.get(s.id, {})
            caps = s.captions or v.get("captions", [])
            if (s.audio or s.video) and not caps:
                self.col.add("transcript", "warning", "Slide has narration but no closed captions were found",
                             slide_id=s.id, slide_label=s.label, screenshot=v.get("shot", ""))
            script = v.get("transcript_text") or s.notes
            if caps and script and len(script.split()) >= 5:
                self.tasks["script_compare"].append({"id": s.id, "slide_id": s.id, "label": s.label,
                                                     "captions": " ".join(caps), "script": script,
                                                     "shot": v.get("transcript_shot") or v.get("shot", "")})

    @staticmethod
    def label_of(course, sid):
        s = course.by_id(sid) if sid else None
        return s.label if s else ""

    def storyboard_task(self, slides):
        """Hands the storyboard text and the course content to Claude for screen-by-screen mapping."""
        sb = self.meta.get("storyboard")
        if not sb:
            if self.meta.get("options", {}).get("stage") == "alpha":  # matching with the SB is a must for Alpha
                self.col.add("content", "error", "Storyboard not uploaded: content could not be matched with the SB",
                             detail="Upload the course again together with its storyboard.")
            else:
                self.col.skip("content", "No storyboard uploaded with this test")
            return
        from . import storyboard
        self.log("Reading the storyboard")
        try:
            text = storyboard.read_text(self.dir / sb)
        except Exception as e:
            self.col.add("content", "warning", f"The storyboard could not be read: {e}")
            return
        if len(text.strip()) < 50:
            self.col.add("content", "warning", "The storyboard has no readable text (is it a scanned PDF?)")
            return
        out = self.dir / "storyboard.txt"
        out.write_text(text)
        try:   # pictures of the SB pages, so every "Not as per SB" issue can show the SB too
            self.log("Making pictures of the storyboard pages")
            storyboard.render_pages(self.dir / sb, self.dir / "sb")
        except Exception as e:
            self.log(f"Storyboard pages could not be pictured: {e}"[:200])
        self.col.ran("content")
        self.tasks["sb_compare"] = {"storyboard_text_file": str(out), "storyboard_chars": len(text), "slides": slides}

    def report_transcripts(self, pending, slide_based):
        if not pending:
            return
        verdicts = {}
        if config.LANGUAGE_MODE == "export":
            for p in pending:
                self.tasks["transcript_review"].append({**p, "slide_id": p["id"] if slide_based else ""})
            return
        if claude.available():
            self.log(f"Claude is reviewing {len(pending)} caption/narration differences")
            try:
                verdicts = claude.review_transcripts([{"item_id": p["id"], "captions": p["captions"],
                                                       "narration": p["narration"]} for p in pending])
            except claude.ClaudeUnavailable as e:
                self.log(f"Claude review unavailable: {e}")
        for p in pending:
            v = verdicts.get(p["id"])
            kw = {"slide_id": p["id"] if slide_based else "", "slide_label": p["label"],
                  "time": p.get("time", "")}
            pct = f"{p['score'] * 100:.0f}% word match"
            if v:
                if v.get("verdict") == "match":
                    continue
                sev = "error" if v.get("verdict") == "mismatch" else "warning"
                self.col.add("transcript", sev, f"Captions differ from narration ({pct})",
                             detail="\n".join(v.get("differences") or p["diffs"]), **kw)
            else:
                sev = "error" if p["score"] < 0.75 else "warning"
                self.col.add("transcript", sev, f"Captions differ from narration ({pct})",
                             detail="\n".join(p["diffs"]), **kw)

    # --------------------------------------------------- Video
    # --------------------------------------------------- SB stage (storyboard only)
    def run_sb(self):
        from . import sbreview, storyboard
        from .progress import plan_sb
        path = self.dir / self.meta["file"]
        opts = self.meta.get("options", {})
        variant = opts.get("english", "auto")
        if path.suffix.lower() not in (".doc", ".docx", ".ppt", ".pptx"):
            raise ValueError("The SB stage takes a Word or PowerPoint storyboard (.doc, .docx, .ppt, .pptx).")
        self.prog.phase("extract")
        self.log("Making pictures of the storyboard pages")
        n = storyboard.render_pages(path, self.dir / "sb")
        pages, with_images = sbreview.page_texts(path, self.dir / "sb")
        self.prog.set_plan(plan_sb(len(pages), len(with_images)))
        self.prog.phase("analyse")
        self.log(f"Storyboard: {n} pages, {len(with_images)} with pictures")
        marked = sbreview.marked_text(path)
        (self.dir / "sb_pages.txt").write_text("\n".join(f"=== SB page {k} ===\n{v}" for k, v in pages.items()))
        (self.dir / "sb_formatting.txt").write_text(marked)
        titles = sbreview.page_titles(pages)
        if variant not in ("us", "uk"):
            variant = sbreview.majority_variant(pages)
            self.log(f"English variant not chosen: the storyboard mostly uses {variant.upper()} English")
        auto = sbreview.auto_checks(pages, marked, variant)
        for g in sbreview.GROUPS:
            self.col.ran(g)
        for i in auto:
            self.col.add(i["group"], i["severity"], f"SB: {i['issue']}", slide_id=f"p{i['page']}",
                         slide_label=f"Page {i['page']}", detail=f"“{i['quote']}”", suggestion=i.get("suggestion", ""),
                         found_in=sbreview.FOUND_IN.get(i.get("found_in"), "SB-OST"), sb_says=i["quote"],
                         sb_page=i["page"], rule=sbreview.RULES[i["rule"] - 1][1])
        self.tasks["sb_review"] = {
            "pages_file": str(self.dir / "sb_pages.txt"), "formatting_file": str(self.dir / "sb_formatting.txt"),
            "page_pictures": str(self.dir / "sb"), "pages_with_pictures": with_images, "page_count": len(pages),
            "english": variant, "requirements": opts.get("requirements", ""),
            "rules": [{"rule": i + 1, "text": t} for i, (_, t) in enumerate(sbreview.RULES)],
            "already_found": [{"page": i["page"], "rule": i["rule"], "quote": i["quote"]} for i in auto]}
        def screen_no(t):
            m = re.search(r"\b(?:screen|slide)\s*(?:no\.?|number|#)?\s*[:\-]?\s*(\d{1,3})\b", t or "", re.I)
            return m.group(1) if m else ""
        slides = [{"id": f"p{k}", "index": k, "title": re.sub(r"^\s*(?:screen|slide)\s*\d{1,3}\s*[:\-–]\s*", "", titles.get(k, ""), flags=re.I),
                   "screen": screen_no(pages[k]), "label": f"Page {k}",
                   "reached": True, "shot": f"sb/page{k}.png", "vw": 0} for k in sorted(pages)]
        self.finish(slides, {"stage": "sb", "slide_count": len(pages), "english": variant,
                             "course_title": Path(self.meta.get("name", "")).stem})

    def run_video(self):
        path = self.dir / self.meta["file"]
        info = media.probe(path)
        if not info:
            raise ValueError("The file could not be read as a video")
        dur = media.duration(info)
        from .progress import plan_video
        self.prog.set_plan(plan_video(dur or 0, 0, bool(self.meta.get("storyboard"))))
        self.prog.phase("video")
        self.log(f"Video: {media.ts(dur)} long")
        shots = self.dir / "shots"
        shots.mkdir(exist_ok=True)

        def frame(t, name):
            out = shots / f"{name}.jpg"
            media.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                       "-vf", "scale=1366:-2", "-q:v", "3", str(out)], 60)
            return f"shots/{out.name}" if out.exists() else ""

        self.col.ran("video")
        if media.has_stream(info, "video"):
            self.log("Checking picture (black and frozen frames)")
            blacks, freezes = media.video_picture_checks(path)
            for i, (b0, b1) in enumerate(blacks):
                if b0 > 1 and b1 < dur - 1:
                    self.col.add("video", "warning", f"Black screen for {b1 - b0:.1f}s", time=media.ts(b0),
                                 slide_label=f"{media.ts(b0)}–{media.ts(b1)}", screenshot=frame(b0 + 0.5, f"black{i}"))
            for i, (f0, f1) in enumerate(freezes):
                end = f1 if f1 is not None else dur
                self.col.add("video", "info" if end - f0 < 10 else "warning", f"Picture frozen for {end - f0:.1f}s",
                             time=media.ts(f0), slide_label=f"{media.ts(f0)}–{media.ts(end)}",
                             screenshot=frame(f0 + 0.5, f"freeze{i}"))
        else:
            self.col.add("video", "error", "The file has no video stream")

        # captions
        cap_file = self.dir / self.meta["captions"] if self.meta.get("captions") else None
        if not cap_file:
            cap_file = media.extract_embedded_subs(path, self.dir)
        cues = media.parse_subtitles(cap_file) if cap_file else []
        if not self.meta.get("options", {}).get("transcript", True):
            cues = []
            for c in ("transcript", "grammar", "spelling"):
                self.col.skip(c, "Transcript check turned off for this Alpha review")
        elif not cues:
            self.col.add("transcript", "warning", "No captions found (upload an .srt/.vtt or embed subtitles)")
            for c in ("grammar", "spelling"):
                self.col.skip(c, "No captions to check")
        else:
            self.log(f"{len(cues)} caption cues")
            from .progress import plan_video
            self.prog.set_plan(plan_video(dur or 0, len(cues), bool(self.meta.get("storyboard"))))
            # group cues into ~45 s chunks
            chunks, cur = [], []
            for c in cues:
                if cur and c[1] - cur[0][0] > 45:
                    chunks.append(cur); cur = []
                cur.append(c)
            if cur:
                chunks.append(cur)
            # one frame per caption cue with the caption drawn on it, so caption issues have a screenshot
            self.log("Capturing a frame for each caption")
            items, n_frames = [], 0
            for i, ch in enumerate(chunks):
                lab = f"{media.ts(ch[0][0])}–{media.ts(ch[-1][1])}"
                cap_shots = []
                for c0, c1, ctext in ch:
                    if n_frames >= 300:
                        break
                    name = f"cc{n_frames:03d}.png"
                    boxes = media.caption_frame(path, (c0 + c1) / 2, ctext, shots / name)
                    self.shot_boxes[f"shots/{name}"] = boxes
                    cap_shots.append({"text": ctext, "shot": f"shots/{name}"})
                    n_frames += 1
                items.append({"id": f"c{i}", "slide": lab, "source": "captions", "time": media.ts(ch[0][0]),
                              "text": " ".join(c[2] for c in ch), "cap_shots": cap_shots,
                              "shot": cap_shots[0]["shot"] if cap_shots else ""})
            self.proofread(items, lambda it: it.get("slide", ""))
            self.storyboard_task([{"id": it["id"], "label": it["slide"], "time": it["time"], "captions": it["text"],
                                   "shot": it["shot"]} for it in items])
            if cue_end := cues[-1][1]:
                if cue_end > dur + 1:
                    self.col.add("transcript", "error", "Captions run past the end of the video",
                                 detail=f"Last cue ends {media.ts(cue_end)}, video ends {media.ts(dur)}")
            if (self.meta.get("options", {}).get("transcript", True) and media.has_stream(info, "audio")
                    and not media.stt_available()):
                self.video_caption_coverage(path, cues, dur)
            elif self.meta.get("options", {}).get("transcript", True) and media.has_stream(info, "audio"):
                self.log("Speech-to-text on the narration")
                wav = self.dir / "audio.wav"
                media.to_wav(path, wav)
                try:
                    segs = media.transcribe(wav)
                except media.STTUnavailable as e:
                    self.col.skip("transcript", str(e))
                    segs = None
                if segs is not None:
                    self.col.ran("transcript")
                    pending = []
                    for i, ch in enumerate(chunks):
                        t0, t1 = ch[0][0] - 0.75, ch[-1][1] + 0.75
                        spoken = " ".join(t for s0, s1, t in segs if s1 > t0 and s0 < t1)
                        cap = " ".join(c[2] for c in ch)
                        score, diffs = textcheck.compare(cap, spoken)
                        if score < config.TRANSCRIPT_PASS_SCORE:
                            pending.append({"id": f"c{i}", "label": f"{media.ts(ch[0][0])}–{media.ts(ch[-1][1])}",
                                            "time": media.ts(ch[0][0]), "score": score, "diffs": diffs,
                                            "captions": cap, "narration": spoken})
                    # narration with no captions at all
                    covered = [(c[0] - 1, c[1] + 1) for c in cues]
                    for s0, s1, t in segs:
                        if len(t.split()) >= 4 and not any(a < s1 and s0 < b for a, b in covered):
                            self.col.add("transcript", "error", "Narration has no caption", time=media.ts(s0),
                                         slide_label=f"{media.ts(s0)}–{media.ts(s1)}", detail=t)
                    self.report_transcripts(pending, slide_based=False)
        if not cues:
            self.col.skip("content", "No captions to compare with the storyboard")
        # every issue needs a screenshot: a frame at its time (or the start of the video)
        for i, f in enumerate(self.col.findings):
            if not f.screenshot:
                t = f.time or (f.slide_label.split("–")[0] if f.slide_label else "")
                sec = sum(float(x) * m for x, m in zip(reversed(t.split(":")), (1, 60, 3600))) if t else 1.0
                f.screenshot = frame(sec + 0.3, f"issue{i}")
        for k in ("navigation", "swipe", "menu", "buttons", "screen_number", "seekbar", "completion"):
            self.col.skip(k, "Not applicable to videos")
        self.finish([], {"duration": round(dur, 1), "shot_boxes": self.shot_boxes})


def _video_coverage(self, path, cues, dur):
    """Without speech-to-text: every stretch of speech needs a caption, and captions should sit on speech."""
    self.col.ran("transcript")
    regions = media.speech_regions(path, dur)
    for a, b in regions:
        if b - a < 1.5:
            continue
        covered = sum(max(0, min(b, c1 + 0.5) - max(a, c0 - 0.5)) for c0, c1, _ in cues)
        if covered < 0.3 * (b - a):
            self.col.add("transcript", "error", "Speech with no caption", time=media.ts(a),
                         slide_label=f"{media.ts(a)}–{media.ts(b)}")
    for c0, c1, text in cues:
        if c1 - c0 < 1.5:
            continue
        overlap = sum(max(0, min(c1, b) - max(c0, a)) for a, b in regions)
        if overlap < 0.1 * (c1 - c0):
            self.col.add("transcript", "warning", "Caption is shown while nothing is being said",
                         time=media.ts(c0), slide_label=f"{media.ts(c0)}–{media.ts(c1)}", detail=text)
    self.col.add("transcript", "info", "Word-for-word narration check not run (no speech-to-text here). "
                 "Caption timing was checked against the speech in the audio.")


Run.video_caption_coverage = _video_coverage


def execute(run_dir: Path):
    Run(run_dir).execute()


def summarize(findings, checks_run, checks_skipped):
    summary = {}
    for key, name in CHECKS:
        items = [x for x in findings if x["check"] == key]
        status = ("skipped" if key in checks_skipped and not items
                  else "fail" if any(x["severity"] == "error" for x in items)
                  else "warn" if any(x["severity"] == "warning" for x in items)
                  else "pass" if key in checks_run or items
                  else "n/a")
        summary[key] = {"name": name, "status": status,
                        "errors": sum(x["severity"] == "error" for x in items),
                        "warnings": sum(x["severity"] == "warning" for x in items),
                        "info": sum(x["severity"] == "info" for x in items),
                        "skipped_reason": checks_skipped.get(key, "")}
    return summary


def text_shot(item, original):
    """The screenshot that shows a text issue: the caption frame holding the words, the transcript panel, or the slide."""
    want = " ".join((original or "").lower().split())
    for c in item.get("cap_shots") or []:
        if want and want in " ".join(c["text"].lower().split()):
            return c["shot"]
    if item.get("cap_shots"):
        return item["cap_shots"][0]["shot"]
    return item.get("shot", "")


def issue_to_finding(iss, item):
    """Turns one proofreading answer into a finding. Transcript and caption issues go to the Transcript check."""
    cat = iss.get("category", "grammar")
    src = item.get("source", "text")
    in_transcript = src in ("captions", "transcript", "notes")
    check = "transcript" if in_transcript else ("spelling" if cat == "spelling" else "grammar")
    where = {"captions": "closed captions", "transcript": "transcript"}.get(src, src)
    return {"check": check,
            "severity": iss.get("severity") if iss.get("severity") in ("error", "warning") else "warning",
            "message": f"{cat.capitalize()} in {where}: “{iss.get('original', '')}”",
            "slide_id": item.get("slide_id", ""), "slide_label": item.get("label", ""), "time": item.get("time", ""),
            "suggestion": iss.get("suggestion", ""), "detail": iss.get("explanation", ""),
            "course_says": iss.get("original", ""),
            "fix": (f"Change “{iss.get('original', '')}” to “{iss['suggestion']}”." if iss.get("suggestion") else ""),
            "screenshot": text_shot(item, iss.get("original", ""))}
