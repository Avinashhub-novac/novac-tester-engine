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
        ("Assessment: every option, feedback, pass / fail / retry; rules match the results; numbering; right answers; on-topic questions", ["assessment"]),
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
        ("Assessment: every option, feedback, pass / fail / retry and attempts", ["assessment"]),
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
        ("Assessment scenarios and rules: options, pass, fail, retry, attempts, result sent to the LMS", ["assessment"]),
        ("LMS course completion", ["completion"]),
        ("Restrictions as per SOW: menu, seekbar, Next (when set)", ["restrictions"]),
    ],
}

VIDEO_SCOPE = [
    ("Video plays; picture and sound", ["video"]),
    ("Captions present, in time with the narration", ["transcript"]),
    ("Spelling and grammar in the captions", ["spelling", "grammar"]),
    ("Captions matched with the SB (when the SB is uploaded)", ["content"]),
]


def items(stage, kind=""):
    if kind == "video":
        return VIDEO_SCOPE
    return SCOPE.get(stage or "beta", SCOPE["beta"])


def allowed(stage, kind=""):
    return {c for _, cs in items(stage, kind) for c in cs}


def apply(res, stage, kind=""):
    """Drops out-of-scope findings and returns the scope checklist for the result page."""
    ok = allowed(stage, kind)
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
    for label, checks in items(stage, kind):
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
