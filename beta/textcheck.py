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


def check_screen_numbers(seq, col):
    """seq: [(slide_id, label, text)] in learner order."""
    found = [(sid, lab, find_screen_number(t)) for sid, lab, t in seq]
    numbered = [f for f in found if f[2]]
    if not seq or len(numbered) < max(2, len(seq) * 0.5):
        col.skip("screen_number", "Fewer than half of the slides show a 'Screen N of M' label")
        return
    col.ran("screen_number")
    totals = {}
    for _, _, n in numbered:
        totals[n[1]] = totals.get(n[1], 0) + 1
    common_total = max(totals, key=totals.get)
    seen, prev = {}, None
    started = False
    for sid, lab, n in found:
        if not n:
            if started:
                col.add("screen_number", "warning", "Slide has no screen number while the slides around it do",
                        slide_id=sid, slide_label=lab)
            continue
        started = True
        a, b, raw = n
        if b != common_total:
            col.add("screen_number", "error", f"Wrong total in screen number: '{raw}'", slide_id=sid, slide_label=lab,
                     suggestion=f"{a} of {common_total}")
        if a in seen:
            col.add("screen_number", "error", f"Duplicate screen number {a}", slide_id=sid, slide_label=lab,
                     detail=f"Also on {seen[a]}")
        seen.setdefault(a, lab)
        if prev is not None and a != prev + 1 and a not in (prev,):
            col.add("screen_number", "error", f"Screen number out of order: {raw}", slide_id=sid, slide_label=lab,
                     detail=f"Previous numbered slide showed {prev}", suggestion=f"{prev + 1} of {common_total}")
        if prev is None and a != 1:
            col.add("screen_number", "warning", f"First numbered slide starts at {a}, not 1", slide_id=sid, slide_label=lab)
        prev = a
    count = len(numbered)
    if common_total != count:
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
