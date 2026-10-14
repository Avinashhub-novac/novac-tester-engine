"""Common result model shared by every check."""
from dataclasses import dataclass, field, asdict

CHECKS = [
    ("navigation", "Navigation"),
    ("grammar", "Grammar"),
    ("spelling", "Spelling"),
    ("transcript", "Transcript"),
    ("swipe", "Mobile swipe"),
    ("menu", "Menu"),
    ("buttons", "Buttons"),
    ("screen_number", "Screen number"),
    ("seekbar", "Seekbar"),
    ("restrictions", "Restrictions (RCD)"),
    ("completion", "Completion"),
    ("content", "Content mapping (SB)"),
    ("video", "Video"),
    ("assessment", "Assessment"),
    ("interactivity", "Clickables & pop-ups"),
    ("logic", "Logic & facts"),
    ("sb_language", "SB: Language"),
    ("sb_consistency", "SB: Consistency"),
    ("sb_structure", "SB: Structure & assessment"),
    ("sb_alignment", "SB: OST, VO, titles & images"),
    ("sb_standards", "SB: Standards & style"),
    ("sb_logic", "SB: Logic & facts"),
]
CHECK_NAMES = dict(CHECKS)
SEVERITIES = ("error", "warning", "info")


@dataclass
class Finding:
    check: str
    severity: str
    message: str
    slide_id: str = ""
    slide_label: str = ""
    detail: str = ""
    suggestion: str = ""
    screenshot: str = ""
    time: str = ""  # for videos, e.g. "00:01:12"
    box: list = None   # [x, y, w, h] in CSS pixels of the screenshot's page, to outline the problem
    vw: int = 0        # CSS width of the page the screenshot was taken at
    mobile: bool = False  # found while testing on the phone layout
    found_in: str = ""    # QC sheet "Issue found in" when the check decides it (e.g. restrictions)
    sb_says: str = ""     # SB stage / content mapping: the SB wording the issue is about (marked on the SB page picture)
    sb_page: int = 0      # SB page that wording is on
    rule: str = ""        # SB stage: the checklist rule the issue breaks
    course_says: str = "" # content mapping: what the course shows (the SB wording is in sb_says)
    fix: str = ""         # what needs to be done (report: "What needs to be done")
    blocker: bool = False # the learner cannot go past this screen: testing stopped here

    def to_dict(self):
        return asdict(self)


@dataclass
class Collector:
    findings: list = field(default_factory=list)
    checks_run: set = field(default_factory=set)
    checks_skipped: dict = field(default_factory=dict)  # check -> reason
    mobile_mode: bool = False  # set while the mobile pass runs

    def add(self, check, severity, message, **kw):
        assert severity in SEVERITIES, severity
        self.checks_run.add(check)
        kw.setdefault("mobile", self.mobile_mode or check == "swipe")
        f = Finding(check=check, severity=severity, message=message, **kw)
        # avoid exact duplicates (same check/slide/message)
        key = (f.check, f.slide_id, f.message, f.detail, f.time)
        for g in self.findings:
            if (g.check, g.slide_id, g.message, g.detail, g.time) == key:
                return g
        self.findings.append(f)
        return f

    def ran(self, check):
        self.checks_run.add(check)

    def skip(self, check, reason):
        self.checks_skipped[check] = reason
