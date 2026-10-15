"""Screen-number check and word-level transcript comparison."""
import difflib
import re

WORDS = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
NUM_WORDS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
             "seven": "7", "eight": "8", "nine": "9", "ten": "10", "percent": "%", "okay": "ok"}
SCREEN_RES = [
    re.compile(r"\b(?:screen|page|slide)\s*[:#]?\s*(\d{1,3})\s*(?:of|/)\s*(\d{1,3})\b", re.I),
    re.compile(r"(?<![\d/.:])(\d{1,3})\s*/\s*(\d{1,3})(?![\d/.:])"),
    re.compile(r"\b(\d{1,3})\s+of\s+(\d{1,3})\b", re.I),
]


def find_screen_number(text):
    for rx in SCREEN_RES:
        for m in rx.finditer(text or ""):
            a, b = int(m.group(1)), int(m.group(2))
            if 0 < a <= b <= 500:
                return a, b, m.group(0)
    return None


def _same_title(a, b):
    n = lambda t: " ".join(WORDS.findall((t or "").lower()))
    return n(a) == n(b)


def check_screen_numbers(seq, col, assess=(), results=(), welcome=(), partial=False):
    """seq: [(slide_id, label, text, title)] in learner order.
    Team rules: screens may repeat a number when they carry the same title (one screen built as several slides);
    the assessment's rules, questions and results share one number, and the results screen may have none;
    the welcome screen has none. Numbers must run in order without gaps."""
    seq = [x if len(x) == 4 else (*x, "") for x in seq]
    found = [(sid, lab, find_screen_number(t), title) for sid, lab, t, title in seq]
    numbered = [f for f in found if f[2]]
    if not seq or len(numbered) < max(2, len(seq) * 0.5):
        col.skip("screen_number", "Fewer than half of the slides show a 'Screen N of M' label")
        return
    col.ran("screen_number")
    assess, results, welcome = set(assess), set(results), set(welcome)
    totals = {}
    for _, _, n, _ in numbered:
        totals[n[1]] = totals.get(n[1], 0) + 1
    common_total = max(totals, key=totals.get)
    first_with = {}           # number -> (sid, label, title) of the first screen that showed it
    prev = prev_sid = None
    started = False
    # the results screen carries the assessment's number (or none): check it on its own and leave it out of the count
    assess_nums = [n for s_, _, n, _ in found if n and s_ in assess and s_ not in results]
    for sid, lab, n, title in [x for x in found if x[0] in results and x[2]]:
        if assess_nums and n[0] != assess_nums[0][0]:
            col.add("screen_number", "error", f"Results screen number '{n[2]}' is different from the assessment's '{assess_nums[0][2]}'",
                    slide_id=sid, slide_label=lab, suggestion=assess_nums[0][2],
                    fix=f"Use the assessment's screen number ({assess_nums[0][2]}) on the results screen, or no number.")
    found = [x for x in found if x[0] not in results]
    for sid, lab, n, title in found:
        if not n:
            if started and sid not in results and sid not in welcome and sid not in assess:
                col.add("screen_number", "warning", "Slide has no screen number while the slides around it do",
                        slide_id=sid, slide_label=lab, fix="Add the screen number, in the same style as the other screens.")
            continue
        started = True
        a, b, raw = n
        if b != common_total:
            col.add("screen_number", "error", f"Wrong total in screen number: '{raw}'", slide_id=sid, slide_label=lab,
                    suggestion=f"{a} of {common_total}")
        if a in first_with:
            osid, olab, otitle = first_with[a]
            both_assess = sid in assess and osid in assess
            if not both_assess and not _same_title(title, otitle):
                col.add("screen_number", "error", f"Screen number {a} is used for screens with different titles", slide_id=sid,
                        slide_label=lab, detail=f"Also on {olab}",
                        fix="Give each screen with a different title its own number, in order.")
        else:
            first_with[a] = (sid, lab, title)
        if prev is not None:
            same_screen = a == prev
            if not same_screen and a != prev + 1:
                col.add("screen_number", "error", f"Screen number out of order: {raw}", slide_id=sid, slide_label=lab,
                        detail=f"Previous numbered slide showed {prev}", suggestion=f"{prev + 1} of {common_total}")
        elif a != 1:
            col.add("screen_number", "warning", f"First numbered slide starts at {a}, not 1", slide_id=sid, slide_label=lab)
        prev, prev_sid = a, sid
    count = len(first_with)
    if common_total != count and not partial:
        col.add("screen_number", "warning",
                f"Screen total says {common_total} but {count} slides carry a screen number on the Next path")


def words(text):
    out = []
    for w in WORDS.findall((text or "").lower().replace("’", "'")):
        out.append(NUM_WORDS.get(w, w))
    return out


def compare(caption_text, spoken_text):
    """Returns (score 0..1, list of 'caption: … / audio: …' differences)."""
    a, b = words(caption_text), words(spoken_text)
    if not a and not b:
        return 1.0, []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    diffs = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        cap, aud = " ".join(a[i1:i2]), " ".join(b[j1:j2])
        diffs.append(f"caption: “{cap or '—'}”  /  audio: “{aud or '—'}”")
    return sm.ratio(), diffs[:15]
