"""What each stage covers – and nothing else.

Every stage has a fixed list of scope items. Each item names the checks it is made of. A finding whose check is not
in the stage's list is out of scope and is never reported (no deviation from the scope), and the result page shows
the list as a checklist with the outcome of every item.
"""

SCOPE = {
    "sb": [
        ("Grammar, spelling, punctuation, casing and spacing in VO and OST", ["sb_language"]),
        ("Learning agenda, recap, questions and feedback; knowledge check matches its rules page", ["sb_structure"]),
        ("OST and VO say the same thing; titles fit the VO; images and menu titles", ["sb_alignment"]),
        ("Consistency: casing, end punctuation, US/UK English, numbers and units, bold and italics, superscripts, buttons",
         ["sb_consistency"]),
        ("Standards: acronyms, lead-ins, pronouns, voice and style, parallelism, no duplicated content", ["sb_standards"]),
        ("Logic and facts", ["sb_logic"]),
        ("Style guide followed (when a style guide is uploaded)", ["style_guide"]),
    ],
    "alpha": [
        ("Navigation of every screen: Next, Back, START and menu", ["navigation", "menu"]),
        ("Every visible clickable opened and closed; overlapping text; Next only after all items", ["buttons", "interactivity"]),
        ("Assessment: every option, feedback, scores 100 / 80 / 60 / 0%, every attempt (Retry, Retake); rules match the results; numbering; right answers; on-topic questions", ["assessment"]),
        ("Screen numbers", ["screen_number"]),
        ("Spelling and grammar in on-screen text and titles; US/UK English", ["spelling", "grammar"]),
        ("Content matched with the SB (on-screen text, titles, VO, screen order)", ["content"]),
        ("Logic, facts and flow: steps and screens in the right order, recap matches the content, right course name", ["logic"]),
        ("Consistency: casing, end punctuation, parallel lists, instructions and buttons; titles and OST fit the VO", ["consistency"]),
        ("Images fit their screen; text fits its box and does not run into pictures", ["visual"]),
        ("Transcript and captions", ["transcript"]),
        ("Style guide followed (when a style guide is uploaded)", ["style_guide"]),
    ],
    "beta": [
        ("Navigation of every screen: Next, Back and menu", ["navigation", "menu"]),
        ("Every visible clickable opened and closed; overlapping text; Next only after all items", ["buttons", "interactivity"]),
        ("Assessment: every option, feedback, scores 100 / 80 / 60 / 0%, every attempt (Retry, Retake)", ["assessment"]),
        ("Screen numbers and seekbar", ["screen_number", "seekbar"]),
        ("Spelling and grammar in on-screen text, titles, transcript and captions", ["spelling", "grammar"]),
        ("Transcript and captions", ["transcript"]),
        ("Restrictions as per SOW: menu, seekbar, Next", ["restrictions"]),
        ("Mobile: swipe next and previous on an iPhone (landscape)", ["swipe"]),
        ("LMS course completion", ["completion"]),
        ("Content matched with the SB VO (when the SB is uploaded)", ["content"]),
    ],
    "gold": [
        ("Major spelling and grammar mistakes", ["spelling", "grammar"]),
        ("Navigation of every screen: Next and Back", ["navigation"]),
        ("Assessment: options, scores 100 / 80 / 60 / 0%, every attempt (Retry, Retake), result sent to the LMS", ["assessment"]),
        ("LMS course completion", ["completion"]),
        ("Restrictions as per SOW: menu, seekbar, Next (when set)", ["restrictions"]),
    ],
}

SCOPE["localized"] = [
    ("Content matched with the translated SB: on-screen text, titles, captions, missing / extra / reordered screens", ["content"]),
    ("English left untranslated on screen", ["translation"]),
    ("Spelling and grammar in the course language: on-screen text, titles, captions and transcript", ["spelling", "grammar"]),
    ("Navigation of every screen: Next, Back and menu", ["navigation", "menu"]),
    ("Screen numbers", ["screen_number"]),
    ("Transcript and captions", ["transcript"]),
]

LOCALIZED_VIDEO_SCOPE = [
    ("Video plays; picture and sound", ["video"]),
    ("Captions present, in time with the narration", ["transcript"]),
    ("Captions matched with the translated SB", ["content"]),
    ("English left untranslated in the captions", ["translation"]),
    ("Spelling and grammar in the course language", ["spelling", "grammar"]),
]

# Custom stage: the team picks any of these checks (all ticked by default). key -> (label, checks)
CUSTOM = [
    ("navigation", "Navigation of every screen: Next, Back and menu", ["navigation", "menu"]),
    ("buttons", "Every visible clickable opened and closed; pop-ups; overlapping text; Next only after all items", ["buttons", "interactivity"]),
    ("assessment", "Assessment: every option, feedback, scores 100 / 80 / 60 / 0%, every attempt (Retry, Retake); rules match the results; numbering; right answers", ["assessment"]),
    ("screen_number", "Screen numbers", ["screen_number"]),
    ("seekbar", "Seekbar", ["seekbar"]),
    ("language", "Spelling and grammar in on-screen text and titles; US/UK English", ["spelling", "grammar"]),
    ("transcript", "Transcript and captions: present, spelling and grammar, captions vs transcript", ["transcript"]),
    ("content", "Content matched with the SB: on-screen text, titles, VO, screen order (needs the SB)", ["content"]),
    ("logic", "Logic, facts and flow: steps and screens in order, recap matches the content, right course name", ["logic"]),
    ("consistency", "Consistency: casing, end punctuation, parallel lists, instructions and buttons; titles and OST fit the VO", ["consistency"]),
    ("visual", "Images fit their screen; text fits its box and does not run into pictures", ["visual"]),
    ("restrictions", "Restrictions as per SOW: menu, seekbar, Next", ["restrictions"]),
    ("swipe", "Mobile: swipe next and previous on an iPhone (landscape)", ["swipe"]),
    ("completion", "LMS course completion", ["completion"]),
    ("style_guide", "Style guide followed (needs the style guide)", ["style_guide"]),
]
CUSTOM_KEYS = [k for k, _, _ in CUSTOM]


def custom_items(selected):
    """The scope of a Custom test: only the checks that were ticked (all of them when none are given)."""
    sel = set(selected or CUSTOM_KEYS)
    return [(label, checks) for k, label, checks in CUSTOM if k in sel]


VIDEO_SCOPE = [
    ("Video plays; picture and sound", ["video"]),
    ("Captions present, in time with the narration", ["transcript"]),
    ("Spelling and grammar in the captions", ["spelling", "grammar"]),
    ("Captions matched with the SB (when the SB is uploaded)", ["content"]),
]


def items(stage, kind="", selected=None):
    if stage == "custom":
        its = custom_items(selected)
        if kind == "video":   # a video has no screens: only what applies to it
            vid = {c for _, cs in VIDEO_SCOPE for c in cs}
            its = [VIDEO_SCOPE[0]] + [(l, cs) for l, cs in its if set(cs) & vid]
        return its
    if kind == "video":
        return LOCALIZED_VIDEO_SCOPE if stage in ("localized", "hindi") else VIDEO_SCOPE
    return SCOPE.get(stage or "beta", SCOPE["beta"])


def allowed(stage, kind="", selected=None):
    return {c for _, cs in items(stage, kind, selected) for c in cs}


def apply(res, stage, kind="", selected=None):
    """Drops out-of-scope findings and returns the scope checklist for the result page."""
    ok = allowed(stage, kind, selected)
    kept, dropped = [], 0
    for f in res.get("findings", []):
        if f.get("check") in ok or f.get("blocker"):
            kept.append(f)
        else:
            dropped += 1
    res["findings"] = kept
    if dropped:
        res["out_of_scope_dropped"] = res.get("out_of_scope_dropped", 0) + dropped
    summary = res.get("summary") or {}
    out = []
    for label, checks in items(stage, kind, selected):
        st = [summary.get(c, {}).get("status", "n/a") for c in checks]
        n_err = sum(1 for f in kept if f.get("check") in checks and f.get("severity") == "error")
        n_warn = sum(1 for f in kept if f.get("check") in checks and f.get("severity") == "warning")
        reason = next((summary.get(c, {}).get("skipped_reason") for c in checks if summary.get(c, {}).get("skipped_reason")), "")
        if n_err or n_warn:
            status = "issues"
        elif any(s in ("pass", "warn", "fail") for s in st):
            status = "pass"
        elif any(s == "skipped" for s in st):
            status = "not tested"
        else:
            status = "n/a"
        out.append({"item": label, "checks": checks, "status": status, "errors": n_err, "warnings": n_warn, "note": reason})
    res["scope"] = out
    return res
