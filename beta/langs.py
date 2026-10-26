"""The languages QC360 Localize AI translates into and checks courses in.

Each language: display names, BCP-47 tag (Word/PowerPoint proofing language), the Unicode
range of its script (to tell translated text from English), its native digits (folded to 0-9
when numbers are compared), text direction, the Windows fonts offered for the output file,
and the fonts this container has for drawing it on screenshots.
"""
import re

FREE = "/usr/share/fonts/truetype/freefont/"
DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
UNIFONT = ["/usr/share/fonts/opentype/unifont/unifont.otf", "/usr/share/fonts/truetype/unifont/unifont.ttf"]

LANGS = {
    "hi": {"name": "Hindi", "native": "हिन्दी", "tag": "hi-IN", "script": "Devanagari", "range": "ऀ-ॿ",
           "digits": "०१२३४५६७८९", "rtl": False, "fonts": ["Nirmala UI", "Mangal", "Noto Sans Devanagari", "Kokila"],
           "draw": [FREE + "FreeSans.ttf", FREE + "FreeSerif.ttf"]},
    "ta": {"name": "Tamil", "native": "தமிழ்", "tag": "ta-IN", "script": "Tamil", "range": "஀-௿",
           "digits": "௦௧௨௩௪௫௬௭௮௯", "rtl": False, "fonts": ["Nirmala UI", "Latha", "Vijaya", "Noto Sans Tamil"],
           "draw": [FREE + "FreeSans.ttf", FREE + "FreeSerif.ttf"]},
    "ml": {"name": "Malayalam", "native": "മലയാളം", "tag": "ml-IN", "script": "Malayalam", "range": "ഀ-ൿ",
           "digits": "൦൧൨൩൪൫൬൭൮൯", "rtl": False, "fonts": ["Nirmala UI", "Kartika", "Noto Sans Malayalam"],
           "draw": [FREE + "FreeSans.ttf", FREE + "FreeSerif.ttf"]},
    "te": {"name": "Telugu", "native": "తెలుగు", "tag": "te-IN", "script": "Telugu", "range": "ఀ-౿",
           "digits": "౦౧౨౩౪౫౬౭౮౯", "rtl": False, "fonts": ["Nirmala UI", "Gautami", "Vani", "Noto Sans Telugu"],
           "draw": UNIFONT},
    "kn": {"name": "Kannada", "native": "ಕನ್ನಡ", "tag": "kn-IN", "script": "Kannada", "range": "ಀ-೿",
           "digits": "೦೧೨೩೪೫೬೭೮೯", "rtl": False, "fonts": ["Nirmala UI", "Tunga", "Noto Sans Kannada"],
           "draw": [FREE + "FreeSerif.ttf"]},
    "pa": {"name": "Punjabi", "native": "ਪੰਜਾਬੀ", "tag": "pa-IN", "script": "Gurmukhi", "range": "਀-੿",
           "digits": "੦੧੨੩੪੫੬੭੮੯", "rtl": False, "fonts": ["Nirmala UI", "Raavi", "Noto Sans Gurmukhi"],
           "draw": [FREE + "FreeSans.ttf", FREE + "FreeSerif.ttf"]},
    "ur": {"name": "Urdu", "native": "اردو", "tag": "ur-PK", "script": "Urdu (Perso-Arabic)",
           "range": "؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿",
           "digits": "۰۱۲۳۴۵۶۷۸۹", "rtl": True,
           "fonts": ["Urdu Typesetting", "Jameel Noori Nastaleeq", "Noto Nastaliq Urdu", "Arial"],
           "draw": [DEJAVU, FREE + "FreeSerif.ttf"]},
    "ar": {"name": "Arabic", "native": "العربية", "tag": "ar-SA", "script": "Arabic",
           "range": "؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿",
           "digits": "٠١٢٣٤٥٦٧٨٩", "rtl": True,
           "fonts": ["Arial", "Sakkal Majalla", "Traditional Arabic", "Noto Naskh Arabic"],
           "draw": [DEJAVU, FREE + "FreeSerif.ttf"]},
}
ORDER = ["hi", "ta", "ml", "te", "kn", "pa", "ur", "ar"]
# every script any language uses: for matching words on screens and SB pages
ALL_RANGES = "".join(sorted({L["range"] for L in LANGS.values()}))
ALL_DIGITS = "".join(L["digits"] for L in LANGS.values()) + "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹"


def get(code):
    return LANGS.get((code or "hi").lower(), LANGS["hi"])


def code_of(options):
    """Language of a job (older Hindi jobs carry no lang)."""
    c = ((options or {}).get("lang") or "hi").lower()
    return c if c in LANGS else "hi"


def script_re(code):
    return re.compile("[" + get(code)["range"] + "]")


def fold_digits(s):
    """Native digits of every language -> 0-9."""
    table = {}
    for d in (L["digits"] for L in LANGS.values()):
        for i, ch in enumerate(d):
            table[ord(ch)] = str(i)
    for i, ch in enumerate("٠١٢٣٤٥٦٧٨٩"):
        table[ord(ch)] = str(i)
    for i, ch in enumerate("۰۱۲۳۴۵۶۷۸۹"):
        table[ord(ch)] = str(i)
    return s.translate(table)


def draw_font(text, default=DEJAVU):
    """A font file in this container that can draw `text` (for caption frames)."""
    import os
    for code in ORDER:
        if script_re(code).search(text or ""):
            for f in get(code)["draw"]:
                if os.path.exists(f):
                    return f, get(code)["rtl"]
    return default, False
