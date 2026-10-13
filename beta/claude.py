"""Grammar, spelling and caption review done by Claude through the Anthropic API."""
import json
import time

import requests

from . import config

GRAMMAR_TOOL = {
    "name": "report_issues",
    "description": "Report the language problems found in the course text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string", "description": "The id of the text item the problem is in"},
                        "category": {"type": "string", "enum": ["spelling", "grammar", "punctuation"]},
                        "severity": {"type": "string", "enum": ["error", "warning"],
                                     "description": "error = clearly wrong and visible to learners; warning = probably wrong or inconsistent"},
                        "original": {"type": "string", "description": "The exact wrong words, copied from the text"},
                        "suggestion": {"type": "string", "description": "The corrected words"},
                        "explanation": {"type": "string", "description": "One short sentence"},
                    },
                    "required": ["item_id", "category", "severity", "original", "suggestion"],
                },
            }
        },
        "required": ["issues"],
    },
}

GRAMMAR_SYSTEM = """You are the proofreader in an eLearning QA team. You receive the text of course slides \
(on-screen text, slide titles, closed captions and notes) as JSON items.

Report:
- every genuine spelling mistake (typos, wrong words such as their/there, its/it's, doubled words);
- MAJOR grammar problems only: subject-verb agreement, wrong tense, missing or extra words, broken sentences, \
wrong articles, wrong prepositions that change meaning;
- punctuation that is clearly wrong (missing full stop at the end of a full sentence in body text, doubled \
punctuation, unmatched brackets or quotes).

Do NOT report: style preferences, passive voice, sentence length, Oxford comma, title case choices, \
short labels or button text without full stops, bullet fragments, product names, acronyms, words in the \
glossary, placeholder variables like %Results.ScorePercent%, or differences between US and UK spelling \
unless the course mixes both (then report the minority spelling as a warning).
Copy `original` exactly from the text so it can be found. If an item has no problems, report nothing for it. \
Always call the report_issues tool, with an empty list if the text is clean."""

TRANSCRIPT_TOOL = {
    "name": "report_transcript_review",
    "description": "Report whether each caption set matches its narration.",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["match", "minor_differences", "mismatch"]},
                        "differences": {"type": "array", "items": {"type": "string"},
                                        "description": "Each real difference as 'caption says X / narration says Y'"},
                    },
                    "required": ["item_id", "verdict", "differences"],
                },
            }
        },
        "required": ["results"],
    },
}

TRANSCRIPT_SYSTEM = """You compare closed captions with an automatic speech-to-text transcript of the same narration. \
The transcript was made by a machine and contains recognition errors: homophones, misheard names, numbers written \
differently, missing punctuation, filler words. Ignore those. Report only real differences a learner would notice: \
words or sentences that are in the narration but missing from the captions (or the other way round), different \
numbers or facts, a different word with a different meaning, or captions that belong to another slide.
verdict: match = no real differences; minor_differences = small wording changes that keep the meaning; \
mismatch = missing/extra sentences or changed meaning. Always call report_transcript_review."""


class ClaudeUnavailable(Exception):
    pass


def available():
    return bool(config.ANTHROPIC_API_KEY)


def _call(system, user, tool, max_tokens=8000):
    if not config.ANTHROPIC_API_KEY:
        raise ClaudeUnavailable("ANTHROPIC_API_KEY is not set")
    body = {
        "model": config.CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": tool["name"]},
        "messages": [{"role": "user", "content": user}],
    }
    headers = {"x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    delay = 5
    for attempt in range(6):
        try:
            r = requests.post(config.ANTHROPIC_API_URL, headers=headers, json=body, timeout=300)
        except requests.RequestException as e:
            if attempt == 5:
                raise ClaudeUnavailable(f"Could not reach the Anthropic API: {e}")
            time.sleep(delay); delay *= 2
            continue
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(float(r.headers.get("retry-after", delay))); delay *= 2
            continue
        if r.status_code >= 400:
            raise ClaudeUnavailable(f"Anthropic API error {r.status_code}: {r.text[:300]}")
        for block in r.json().get("content", []):
            if block.get("type") == "tool_use":
                return block.get("input", {})
        return {}
    raise ClaudeUnavailable("Anthropic API kept returning rate-limit or overload errors")


def _chunks(items, max_chars=24000):
    batch, size = [], 0
    for it in items:
        n = len(json.dumps(it))
        if batch and size + n > max_chars:
            yield batch
            batch, size = [], 0
        batch.append(it)
        size += n
    if batch:
        yield batch


def proofread(items, glossary=()):
    """items: [{'id','slide','source','text'}]. Returns list of issue dicts (with item_id)."""
    out = []
    gloss = ", ".join(sorted(set(glossary))) or "(none)"
    for batch in _chunks([i for i in items if i["text"].strip()]):
        user = (f"Glossary (always correct, never report): {gloss}\n\n"
                f"Text items:\n{json.dumps(batch, ensure_ascii=False, indent=1)}")
        res = _call(GRAMMAR_SYSTEM, user, GRAMMAR_TOOL)
        out.extend(res.get("issues", []) or [])
    return out


def review_transcripts(pairs):
    """pairs: [{'id','captions','narration'}]. Returns {id: {'verdict','differences'}}."""
    out = {}
    for batch in _chunks(pairs, 30000):
        res = _call(TRANSCRIPT_SYSTEM, json.dumps(batch, ensure_ascii=False, indent=1), TRANSCRIPT_TOOL)
        for r in res.get("results", []) or []:
            out[str(r.get("item_id"))] = r
    return out
