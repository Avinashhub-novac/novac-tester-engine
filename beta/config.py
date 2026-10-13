"""Settings, read from environment variables (see README)."""
import os
from pathlib import Path


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
RUNS_DIR = DATA_DIR / "runs"
PORT = _int("PORT", 8080)
WORKERS = _int("WORKERS", 1)
MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 2048)
MAX_CRAWL_MINUTES = _int("MAX_CRAWL_MINUTES", 120)
MAX_BUTTONS_PER_SLIDE = _int("MAX_BUTTONS_PER_SLIDE", 25)

# Claude (grammar, spelling, caption review)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_URL = os.environ.get("ANTHROPIC_API_URL", "https://api.anthropic.com/v1/messages")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

# Speech-to-text (Claude cannot listen to audio, so narration is transcribed first)
STT_ENGINE = os.environ.get("STT_ENGINE", "faster-whisper")  # faster-whisper | openai | none
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base.en")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
STT_API_URL = os.environ.get("STT_API_URL", "https://api.openai.com/v1/audio/transcriptions")
STT_API_KEY = os.environ.get("STT_API_KEY", "")
STT_API_MODEL = os.environ.get("STT_API_MODEL", "whisper-1")

TRANSCRIPT_PASS_SCORE = float(os.environ.get("TRANSCRIPT_PASS_SCORE", "0.90"))
SILENCE_GAP_SECONDS = float(os.environ.get("SILENCE_GAP_SECONDS", "4"))

RUNS_DIR.mkdir(parents=True, exist_ok=True)
LANGUAGE_MODE = os.environ.get("LANGUAGE_MODE", "api")  # api = call the Anthropic API; export = hand text to the running Claude session
