"""Translation quality steps around the batches Claude translates.

    terms   the agreed term list for one SB: key terms and recurring words, each with one translation,
            agreed BEFORE translating so the whole SB uses the same words (merged with the client glossary).
    review  after the first draft: keeps a copy of the draft, and writes review.txt, the whole translation
            in reading order with automatic flags (agreed term not used, English words left, much longer or
            shorter than the English, missing sentence end mark) for the editing pass.
    edits   which lines the editing pass changed (first draft -> final), shown on the site and in Excel.
"""
import json
import re
import shutil
import unicodedata
from pathlib import Path

from . import langs

TAG = re.compile(r"<(\d{1,2})>(.*?)</\1>", re.S)
LATIN_WORD = re.compile(r"\b[A-Za-z][A-Za-z'’-]{2,}\b")
TOKENS = re.compile(r"%[\w.]+%|https?://\S+|www\.\S+|\b[\w.-]+\.(?:png|jpe?g|gif|mp3|mp4|wav|svg|pdf|docx?|xlsx?|pptx?)\b|\{[^}]+\}", re.I)
END_MARK = {"hi": "।", "pa": "।", "ur": "۔"}


class QualityError(Exception):
    pass


def _plain(s):
    return TAG.sub(lambda m: m.group(2), s or "")


def answers(work: Path):
    out = {}
    for f in sorted(work.glob("batches/b??.out.json")):
        out.update(json.loads(f.read_text()))
    return out


def _tr(ans):
    if not isinstance(ans, dict) or ans.get("keep"):
        return ""
    v = ans.get("tr", ans.get("hi", ""))
    return v if isinstance(v, str) else ""


def _vlen(s):
    return sum(1 for ch in s if unicodedata.category(ch) not in ("Mn", "Cf"))


def _glossary_lines(text):
    out = []
    for line in (text or "").splitlines():
        if "=" in line:
            en, tr = line.split("=", 1)
            if en.strip() and tr.strip():
                out.append({"en": en.strip(), "tr": tr.strip(), "note": "Client glossary", "source": "client"})
    return out


# ------------------------------------------------------------------ term list
def check_terms(run_dir: Path, options):
    """Validates tr/terms.json (written by Claude) and merges the client glossary over it."""
    work = run_dir / "tr"
    code = langs.code_of(options)
    L = langs.get(code)
    f = work / "terms.json"
    if not f.exists():
        raise QualityError("tr/terms.json was not written")
    try:
        raw = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        raise QualityError(f"terms.json is not valid JSON: {e}")
    if isinstance(raw, dict):
        raw = raw.get("terms", [])
    problems, terms = [], []
    script = langs.script_re(code)
    for i, t in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(t, dict) or not str(t.get("en", "")).strip():
            problems.append(f"Entry {i + 1}: needs \"en\"")
            continue
        en, tr = str(t["en"]).strip(), str(t.get("tr", "")).strip()
        keep = bool(t.get("keep"))
        if not keep and not script.search(tr):
            problems.append(f"“{en}”: \"tr\" must be in {L['script']} script, or set \"keep\": true for a term that stays in English")
            continue
        terms.append({"en": en, "tr": en if keep else tr, "keep": keep, "note": str(t.get("note", ""))[:200], "source": "agreed"})
    client = _glossary_lines(options.get("glossary", ""))
    by_en = {t["en"].lower(): t for t in terms}
    for c in client:                          # the client's own glossary always wins
        by_en[c["en"].lower()] = {**c, "keep": False}
    for k in [x.strip() for x in (options.get("keep_terms") or "").split(",") if x.strip()]:
        by_en.setdefault(k.lower(), {"en": k, "tr": k, "keep": True, "note": "Keep in English", "source": "client"})
    final = sorted(by_en.values(), key=lambda t: t["en"].lower())
    (work / "terms_final.json").write_text(json.dumps(final, ensure_ascii=False, indent=1))
    return {"ok": not problems, "problems": problems, "terms": len(final), "client_terms": len(client),
            "terms_file": str(work / "terms_final.json")}


def load_terms(work: Path):
    f = work / "terms_final.json"
    return json.loads(f.read_text()) if f.exists() else []


def _stems(tr):
    """Each word of an agreed translation, trimmed so inflected forms (case endings, plural) still match."""
    out = []
    for w in re.findall(r"\S+", tr):
        w = re.sub(r"[^\wऀ-ൿ؀-ۿݐ-ݿ]+", "", w)
        if not w:
            continue
        n = len(w)
        out.append(w if n <= 3 else w[:max(3, (n * 2 + 2) // 3)])
    return out


def _term_hits(en, tr, terms):
    """Agreed terms present in the English whose translation is not in the line."""
    misses = []
    low = en.lower()
    for t in terms:
        if t.get("keep"):
            exact = t["en"] != t["en"].lower()          # PASS, SAP, Next: match the capitals, so "pass" the verb is not a hit
            src, word = (en, t["en"]) if exact else (low, t["en"].lower())
            if re.search(r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])", src) and word not in (tr if exact else tr.lower()):
                misses.append(f"Keep “{t['en']}” in English")
            continue
        if not re.search(r"(?<![A-Za-z])" + re.escape(t["en"].lower()) + r"(?:s|es|ed|ing)?(?![A-Za-z])", low):
            continue
        st = _stems(t["tr"])
        if st and not all(s in tr for s in st):
            misses.append(f"Agreed term “{t['en']}” = “{t['tr']}” not used")
    return misses


# ------------------------------------------------------------------ editing pass
def review(run_dir: Path, options):
    """Snapshots the first draft (once) and writes review.txt with flags for the editing pass."""
    work = run_dir / "tr"
    code = langs.code_of(options)
    L = langs.get(code)
    data = json.loads((work / "segments.json").read_text())
    ans = answers(work)
    missing = [u["id"] for u in data["uniques"] if u["id"] not in ans]
    if missing:
        raise QualityError(f"{len(missing)} texts have no translation yet (first: {missing[0]})")
    draft = work / "draft.json"
    if not draft.exists():
        draft.write_text(json.dumps(ans, ensure_ascii=False))
    terms = load_terms(work)
    keep = {k.strip().lower() for k in (options.get("keep_terms") or "").split(",") if k.strip()}
    keep |= {t["en"].lower() for t in terms if t.get("keep")}
    where = {}
    for s in data["segments"]:
        where.setdefault(s["u"], s["where"])
    lines = [f"EDITING PASS – English to {L['name']} ({L['script']} script{', right to left' if L['rtl'] else ''})",
             f"Style: {options.get('register', 'simple')}" + (f" · Learners: {options.get('context')}" if options.get("context") else ""),
             "", "AGREED TERMS"]
    lines += [f"  {t['en']} = {t['tr']}" + (" (keep in English)" if t.get("keep") else "") + (f"  – {t['note']}" if t.get("note") else "")
              for t in terms] or ["  (none)"]
    lines.append("")
    flagged = 0
    for u in data["uniques"]:
        a = ans.get(u["id"]) or {}
        if a.get("keep"):
            lines.append(f"[{u['id']}] {where.get(u['id'], '')}\n  EN: {_plain(u['en'])}\n  KEPT IN ENGLISH: {a.get('why', '')}\n")
            continue
        en, tr = _plain(u["en"]), _plain(_tr(a))
        flags = _term_hits(en, tr, terms)
        left = [w for w in LATIN_WORD.findall(TOKENS.sub(" ", tr))
                if w.lower() not in keep and not w.isupper() and not any(w.lower() in k for k in keep)]
        if left:
            flags.append("English words in the translation: " + ", ".join(dict.fromkeys(left)) + " (keep only if a learner would say them in English)")
        le, lt = _vlen(en), _vlen(tr)
        if le >= 20 and lt > le * 1.8:
            flags.append("Much longer than the English: say it more simply")
        if le >= 20 and lt < le * 0.45:
            flags.append("Much shorter than the English: check nothing is missing")
        mark = END_MARK.get(code)
        if mark and re.search(r"[.!]\s*$", en) and re.search(r"\.\s*$", tr) and not re.search(r"\.\.\.\s*$", tr):
            flags.append(f"Sentence should end with “{mark}”")
        if flags:
            flagged += 1
        lines.append(f"[{u['id']}] {where.get(u['id'], '')}" + (f"  (×{u['count']})" if u.get("count", 1) > 1 else ""))
        lines.append(f"  EN: {en}")
        lines.append(f"  {code.upper()}: {tr}")
        for fl in flags:
            lines.append(f"  ⚑ {fl}")
        lines.append("")
    out = work / "review.txt"
    out.write_text("\n".join(lines))
    return {"review_file": str(out), "texts": len(data["uniques"]), "flagged": flagged, "terms": len(terms),
            "chars": len(out.read_text())}


def edits(work: Path):
    """[(unique id, first draft, final, reason)] for lines the editing pass changed."""
    d = work / "draft.json"
    if not d.exists():
        return []
    draft, final = json.loads(d.read_text()), answers(work)
    out = []
    for k, a in final.items():
        b = draft.get(k)
        ta, tb = _plain(_tr(a)).strip(), _plain(_tr(b)).strip()
        ka, kb = bool((a or {}).get("keep")), bool((b or {}).get("keep"))
        if ta != tb or ka != kb:
            out.append((k, tb if not kb else "(kept in English)", ta if not ka else "(kept in English)",
                        str((a or {}).get("edit", ""))[:300]))
    return out
