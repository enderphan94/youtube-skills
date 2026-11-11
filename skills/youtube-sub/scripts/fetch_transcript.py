#!/usr/bin/env python3
"""
fetch_transcript.py - fetch the full transcript of any YouTube video.

Part of the `youtube-sub` Claude skill. The transcript logic is ported from
YouGist (github.com/enderphan94/yousumchrome): caption
selection and cleanup from main.py, and the Gemini audio-transcription path
from eval/run_eval.py, used here as the fallback for videos with no captions.

SOURCE CHAIN  (--source auto, the default)
  1. captions  YouTube caption tracks via youtube-transcript-api, run on this
               machine. Every track is tried in priority order until one
               yields text:
                 a. tracks in the --lang languages you asked for
                 b. human-made tracks in the language actually spoken
                 c. the auto-generated (ASR) track, which is always in the
                    spoken language
                 d. every other human-made track (usually translations)
  2. backend   The hosted YouGist backend: same library, different IP. Only
               tried when step 1 failed for a reason another machine can fix
               (an IP block, a network error, the library being missing).
  3. gemini    Gemini listens to the video and transcribes the audio. Used
               when the video has no captions at all, or when 1 and 2 both
               failed. Needs GEMINI_API_KEY. Long videos are transcribed in
               clips (default 30 min): Gemini silently truncates and
               hallucinates on long single requests.

LOCAL AUDIO TRANSCRIPTION  (--source whisper, or a local file)
  Not part of the automatic chain, because it is slow and its first run
  downloads a speech model (0.5 to 1.6 GB). yt-dlp downloads the video's
  audio track and faster-whisper transcribes it on this machine: no API key,
  nothing leaves the computer, and the result has real cue timing. Giving a
  local audio/video file instead of a URL transcribes that file the same way,
  which is the way in when this machine cannot reach youtube.com.
  The result is then checked against a map of where a voice is heard (Silero
  VAD): suspicious cues and speech with no cue are decoded a second time, so
  an invented line cannot hide real speech. The report's "speech_check" lists
  what was recovered, what was removed, and what a person should listen to.

OUTPUT
  The transcript goes to --out FILE, into --out-dir DIR (file name built from
  the title), or to stdout. A JSON report of what happened goes to stdout when
  the transcript was written to a file, or to stderr when the transcript
  itself is on stdout.

CORRECTION HAND-OFF
  Human-made captions are final. Auto-generated captions and local Whisper
  transcripts have mis-heard words and weak punctuation, so when the transcript
  written to a file came from either of them the report says
  "needs_correction": true and the
  body is also split into part-NN.txt files (in a temp folder, "parts_dir")
  for Claude to correct. apply_corrections.py then verifies the corrected
  parts and rewrites the transcript file. --raw skips all of this.

ENVIRONMENT
  GEMINI_API_KEY / GOOGLE_API_KEY   enables the Gemini fallback
  GEMINI_MODEL                      default: gemini-2.5-flash
  WHISPER_MODEL                     default: large-v3-turbo (8+ CPU cores) or small
  WHISPER_DEVICE                    default: cpu ("cuda" for an NVIDIA GPU)
  YOUGIST_BACKEND                   hosted backend base URL, or "off"
  YT_PROXY_URL                      http(s) proxy for requests to YouTube
  YT_SKILLS_VENV                    where the private venv lives
  YT_SKILLS_NO_BOOTSTRAP=1          never create a venv or pip install

EXIT CODES
  0 ok   2 bad arguments / not a YouTube URL   3 video unavailable
  4 no transcript could be obtained from any source
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path

__version__ = "2.2.0"

REQUIREMENT = "youtube-transcript-api>=1.2,<2"
# yt-dlp[default] brings yt-dlp-ejs, which YouTube downloads need since 2025.
YTDLP_REQUIREMENT = "yt-dlp[default]"
WHISPER_REQUIREMENT = "faster-whisper>=1.1,<2"
# Sources whose segments are timed cues (so SRT and [mm:ss] lines are possible).
CUE_SOURCES = ("captions", "whisper")
DEFAULT_BACKEND = "https://yougist.kuberscan.com"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

# Cloudflare in front of the YouGist backend rejects urllib's default
# "Python-urllib/3.x" agent with a 403, so every request names itself.
TOOL_UA = f"youtube-skills/{__version__} (+https://github.com/enderphan94/youtube-skills)"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Hard ceiling on transcript length, from YouGist: ~600k chars is ~10 hours of
# speech, so in practice nothing is cut. Bounds memory on pathological input.
MAX_TRANSCRIPT_CHARS = 600_000

# Size of the parts handed to Claude for correction. Small enough that a part
# is rewritten in one go without drifting, large enough to carry context.
CORRECTION_PART_CHARS = 8000

EXIT_OK, EXIT_USAGE, EXIT_UNAVAILABLE, EXIT_NO_TRANSCRIPT = 0, 2, 3, 4

QUIET = False


def log(msg: str) -> None:
    if not QUIET:
        print(f"[youtube-skills] {msg}", file=sys.stderr, flush=True)


def _short(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── dependency bootstrap ──────────────────────────────────────────────────────

def _venv_dir() -> Path:
    env = os.environ.get("YT_SKILLS_VENV", "").strip()
    if env:
        return Path(env).expanduser()
    cache = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(cache).expanduser() if cache else Path.home() / ".cache"
    return base / "youtube-skills" / "venv"


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ensure_library(force_venv: bool = False) -> bool:
    """Make youtube-transcript-api importable, bootstrapping a private venv once.

    force_venv: move into the private venv even when the library is already
    importable, because more packages (yt-dlp, faster-whisper) are about to be
    installed and they must not land in the user's own Python.

    Homebrew and Debian Pythons refuse global pip installs (PEP 668), so rather
    than asking the user to manage packages, the script creates its own venv on
    first use and re-executes itself inside it. If even that is impossible the
    caption source is skipped; the backend and Gemini sources still work
    because they only need the standard library.
    """
    settled = os.environ.get("YT_SKILLS_NO_BOOTSTRAP") or os.environ.get("_YT_SKILLS_REEXEC")
    if settled or not force_venv:
        try:
            import youtube_transcript_api  # noqa: F401
            return True
        except ImportError:
            pass
    if settled:
        return False

    venv = _venv_dir()
    py = _venv_python(venv)
    try:
        if not py.exists():
            log(f"first run: creating a private Python environment in {venv}")
            venv.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True
            )
        probe = subprocess.run(
            [str(py), "-c", "import youtube_transcript_api"], capture_output=True
        )
        if probe.returncode != 0:
            log("installing youtube-transcript-api into it (one-time)")
            subprocess.run(
                [str(py), "-m", "pip", "install", "--quiet",
                 "--disable-pip-version-check", REQUIREMENT],
                check=True, capture_output=True,
            )
    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, "stderr", b"") or b""
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        log(
            "could not set up the private Python environment "
            f"({_short(detail or e, 200)}). Caption tracks will be skipped. "
            f"Fix with:  python3 -m pip install '{REQUIREMENT}'"
        )
        return False

    env = dict(os.environ, _YT_SKILLS_REEXEC="1")
    argv = [str(py), str(Path(__file__).resolve()), *sys.argv[1:]]
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name == "nt":
        sys.exit(subprocess.call(argv, env=env))
    os.execve(str(py), argv, env)
    return False  # unreachable


# ── HTTP ──────────────────────────────────────────────────────────────────────

class HttpError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


def _ssl_context():
    # python.org macOS builds ship without a wired-up CA bundle, so HTTPS fails
    # with CERTIFICATE_VERIFY_FAILED even though curl works. Prefer certifi.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL_CTX = None


def yt_proxy() -> str:
    return os.environ.get("YT_PROXY_URL", "").strip()


def http(url: str, payload=None, headers=None, timeout: float = 60, proxy: str = "") -> str:
    global _SSL_CTX
    if _SSL_CTX is None:
        _SSL_CTX = _ssl_context()
    hdrs = {"User-Agent": TOOL_UA, "Accept": "application/json"}
    hdrs.update(headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST" if body else "GET")
    handlers = [urllib.request.HTTPSHandler(context=_SSL_CTX)]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", errors="replace")
        except Exception:
            text = ""
        raise HttpError(e.code, text) from None


def http_json(url: str, payload=None, headers=None, timeout: float = 60, proxy: str = ""):
    return json.loads(http(url, payload, headers, timeout, proxy))


# ── video id + metadata ───────────────────────────────────────────────────────

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_video_id(value: str) -> str:
    """Accept every common YouTube URL shape, or a bare 11-character id.

    watch?v=, youtu.be/, /shorts/, /live/, /embed/, /v/, /e/, m.youtube.com,
    music.youtube.com and youtube-nocookie.com. Tracking parameters (si=, t=,
    list=, ...) are ignored.
    """
    v = (value or "").strip().strip("<>\"'")
    if _ID_RE.match(v):
        return v
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", v):
        v = "https://" + v
    try:
        u = urllib.parse.urlparse(v)
    except ValueError:
        return ""
    host = (u.hostname or "").lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    cand = ""
    if host == "youtu.be":
        cand = u.path.lstrip("/").split("/")[0]
    elif host in ("youtube.com", "music.youtube.com", "youtube-nocookie.com",
                  "gaming.youtube.com"):
        q = urllib.parse.parse_qs(u.query)
        if q.get("v"):
            cand = q["v"][0]
        else:
            m = re.match(r"^/(?:shorts|live|embed|v|e)/([^/?#]+)", u.path)
            cand = m.group(1) if m else ""
    return cand if _ID_RE.match(cand) else ""


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def fetch_oembed(video_id: str) -> dict:
    """Title + channel with no API key. Fails quietly for private videos."""
    url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(
        watch_url(video_id), safe=""
    )
    try:
        d = http_json(url, timeout=10, proxy=yt_proxy())
        return {"title": d.get("title"), "channel": d.get("author_name")}
    except Exception:
        return {}


def fetch_duration(video_id: str):
    """Video length in seconds, read from the watch page. None if unknown."""
    try:
        page = http(
            watch_url(video_id),
            headers={
                "User-Agent": BROWSER_UA,
                "Accept": "text/html",
                "Accept-Language": "en-US,en;q=0.8",
                # Skips the EU cookie-consent interstitial.
                "Cookie": "SOCS=CAI; CONSENT=YES+1",
            },
            timeout=20,
            proxy=yt_proxy(),
        )
    except Exception:
        return None
    m = re.search(r'"lengthSeconds"\s*:\s*"(\d+)"', page)
    if m:
        return int(m.group(1))
    m = re.search(r'"approxDurationMs"\s*:\s*"(\d+)"', page)
    return int(m.group(1)) // 1000 if m else None


# ── transcript cleanup (ported verbatim in behaviour from YouGist main.py) ────

# Targeted allowlist of caption annotations. Deliberately NOT a blanket
# r"\[.*?\]": that would also delete real speaker labels like "[John]".
# "[ __ ]" is YouTube's profanity mask for auto-captions.
_ANNOTATION_RE = re.compile(
    r"\[\s*(?:__+|music|applause|laughter|laughs|cheering|clapping|"
    r"inaudible|silence|noise|sighs?|coughs?|sound\s+effects?)\s*\]",
    re.IGNORECASE,
)
# Music glyphs: quarter note, eighth note, beamed eighths, beamed sixteenths.
_MUSIC_GLYPH_RE = re.compile("[♩-♬]")
# A bracket group holding nothing but music glyphs, e.g. "[♪♪♪]". Must be
# removed as a unit, or stripping the glyphs first leaves a bare "[ ]".
_MUSIC_BRACKET_RE = re.compile("\\[\\s*[♩-♬\\s]+\\s*\\]")
_EMPTY_BRACKET_RE = re.compile(r"\[\s*\]")
_WHITESPACE_RE = re.compile(r"\s+")

# Vocal fillers. The list is deliberately narrow: several near-neighbours are
# REAL Vietnamese words and must never appear here:
#   ở = "at/in"   à = vocative particle   ạ = politeness particle   a = exclamation
_FILLER_WORDS = [
    "ừ", "ừm", "ừa", "ờ", "ờm", "ợ",
    "um", "uh", "uhm", "hmm", "hm", "mm", "mmm", "eh", "ehm",
]
_FILLER_RE = re.compile(
    r"(?<![\wÀ-ỹ])(?:" + "|".join(_FILLER_WORDS) + r")(?![\wÀ-ỹ])",
    re.IGNORECASE,
)
_ORPHAN_PUNCT_RE = re.compile(r"(?:\s*,)+(\s*,)")
_LEADING_PUNCT_RE = re.compile(r"^[\s,.;:]+")


def strip_fillers(text: str) -> str:
    """Remove vocal fillers, then tidy the punctuation they leave behind."""
    if not text:
        return text
    out = _FILLER_RE.sub(" ", text)
    out = _WHITESPACE_RE.sub(" ", out)
    while True:
        collapsed = _ORPHAN_PUNCT_RE.sub(r"\1", out)
        if collapsed == out:
            break
        out = collapsed
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r",+\s*([.;:!?])", r"\1", out)
    out = re.sub(r",+\s*$", "", out)
    out = _LEADING_PUNCT_RE.sub("", out)
    return _WHITESPACE_RE.sub(" ", out).strip()


def clean_cue(text: str) -> str:
    """Clean ONE caption cue: annotations, music glyphs, internal newlines."""
    text = (text or "").replace("\n", " ")
    # Order matters: bracketed glyph groups go before bare glyphs.
    text = _MUSIC_BRACKET_RE.sub(" ", text)
    text = _ANNOTATION_RE.sub(" ", text)
    text = _MUSIC_GLYPH_RE.sub(" ", text)
    text = _EMPTY_BRACKET_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def clean_snippets(snippets) -> str:
    """Join caption cues into prose, exactly like YouGist.

    Deliberately does NOT de-duplicate (repeated words in srv1 data are real
    stutters), does NOT html.unescape (the library already decoded entities),
    and does NOT try to restore punctuation with heuristics.
    """
    parts = []
    for snip in snippets:
        text = snip.get("text") if isinstance(snip, dict) else getattr(snip, "text", "")
        text = clean_cue(text)
        if text:
            parts.append(text)
    joined = _WHITESPACE_RE.sub(" ", " ".join(parts)).strip()
    # Fillers are stripped on the joined text: "ừ ừ" is often split over cues.
    return strip_fillers(joined)


def clean_prose(text: str) -> str:
    """Clean already-paragraphed text (Gemini output) paragraph by paragraph."""
    paras = []
    for para in re.split(r"\n\s*\n", text or ""):
        para = strip_fillers(clean_cue(para))
        if para:
            paras.append(para)
    return "\n\n".join(paras)


def truncate_at_sentence(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if boundary > limit * 0.8:
        return cut[: boundary + 1].strip()
    return cut.strip()


_SENTENCE_END_RE = re.compile(r"(?<=[.!?…。！？])\s+")


def to_paragraphs(text: str, target: int = 700, hard_max: int = 1000) -> str:
    """Re-flow one long line into paragraphs of roughly `target` characters.

    Only whitespace changes. Breaks land on sentence ends when there are any;
    unpunctuated ASR text is broken at word boundaries. Keeps every line short
    enough for tools that truncate very long lines.
    """
    out = []
    for block in re.split(r"\n\s*\n", text or ""):
        block = _WHITESPACE_RE.sub(" ", block).strip()
        if not block:
            continue
        cur = ""
        for sent in _SENTENCE_END_RE.split(block):
            while len(sent) > hard_max:
                cut = sent.rfind(" ", 0, target)
                if cut <= 0:
                    cut = sent.find(" ", target)
                if cut <= 0:
                    cut = hard_max
                if cur:
                    out.append(cur)
                    cur = ""
                out.append(sent[:cut].strip())
                sent = sent[cut:].strip()
            if not sent:
                continue
            if cur and len(cur) + 1 + len(sent) > hard_max:
                out.append(cur)
                cur = sent
            else:
                cur = f"{cur} {sent}" if cur else sent
            if len(cur) >= target:
                out.append(cur)
                cur = ""
        if cur:
            out.append(cur)
    return "\n\n".join(p for p in out if p)


# ── chunking (port of YouGist's chunkTranscript) ──────────────────────────────

def chunk_text(text: str, max_chars: int) -> list:
    """Slice text into pieces of <= max_chars (+10% sliver absorption).

    Prefers to cut at a paragraph break, then a line break, then a sentence
    end, within the last 20% of each window; falls back to a word boundary.
    A leftover smaller than 10% of a window is absorbed into the previous
    piece instead of becoming its own runt chunk.
    """
    text = (text or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return [text] if text else []
    chunks, pos, n = [], 0, len(text)
    while pos < n:
        if n - pos <= max_chars:
            chunks.append(text[pos:].strip())
            break
        end = pos + max_chars
        window_start = pos + int(max_chars * 0.8)
        cut = -1
        for sep in ("\n\n", "\n", ". ", "? ", "! ", "。", " "):
            i = text.rfind(sep, window_start, end)
            if i != -1:
                cut = i + len(sep.rstrip())
                break
        if cut == -1:
            space = text.rfind(" ", pos + 1, end)
            cut = space if space > pos else end
        if cut <= pos:
            cut = pos + max_chars
        remainder = n - cut
        if 0 < remainder < max_chars * 0.1:
            cut = n
        chunks.append(text[pos:cut].strip())
        pos = cut
    return [c for c in chunks if c]


# ── source 1: YouTube captions ────────────────────────────────────────────────

class SourceError(Exception):
    """kind: no_captions | blocked | network | unavailable | age_restricted |
    unplayable | missing_dep | disabled | config | failed"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


_NO_CAPTIONS = {"TranscriptsDisabled", "NoTranscriptFound", "NoTranscriptAvailable"}
_BLOCKED = {
    "RequestBlocked", "IpBlocked", "TooManyRequests", "PoTokenRequired",
    "YouTubeRequestFailed", "FailedToCreateConsentCookie", "YouTubeDataUnparsable",
}
_UNAVAILABLE = {"VideoUnavailable", "InvalidVideoId"}


def classify(exc: BaseException) -> str:
    name = type(exc).__name__
    if name in _NO_CAPTIONS:
        return "no_captions"
    if name in _BLOCKED:
        return "blocked"
    if name in _UNAVAILABLE:
        return "unavailable"
    if name == "AgeRestricted":
        return "age_restricted"
    if name == "VideoUnplayable":
        return "unplayable"
    module = type(exc).__module__ or ""
    if isinstance(exc, OSError) or module.startswith(("requests", "urllib3")):
        return "network"
    return "failed"


def _exc_msg(exc: BaseException) -> str:
    cause = None
    try:
        cause = getattr(exc, "cause", None)
    except Exception:
        cause = None
    return _short(cause if isinstance(cause, str) and cause.strip() else exc)


def _base_lang(code: str) -> str:
    return re.split(r"[-_]", (code or "").lower())[0]


def _lang_match(code: str, want: str) -> bool:
    return (code or "").lower() == (want or "").lower() or _base_lang(code) == _base_lang(want)


def track_label(t) -> str:
    return f"{t.language_code}{' (auto)' if t.is_generated else ''}"


def order_tracks(tracks, prefs=()) -> list:
    """Priority order for caption tracks.

    YouGist took the first human-made track in listing order, which is sorted
    by language NAME: for a TED talk with 40 tracks that is Arabic. Fine when an
    LLM summarizes into a fixed language anyway, wrong for a transcript. The
    auto-generated track reveals the spoken language (ASR always runs in it),
    so human tracks in that language come first, then the ASR track itself,
    then everything else.
    """
    manual = [t for t in tracks if not t.is_generated]
    auto = [t for t in tracks if t.is_generated]
    spoken = []
    for t in auto:
        b = _base_lang(t.language_code)
        if b not in spoken:
            spoken.append(b)

    ordered = []

    def add(items):
        for t in items:
            if not any(t is o for o in ordered):
                ordered.append(t)

    for want in prefs:
        add([t for t in manual if _lang_match(t.language_code, want)])
        add([t for t in auto if _lang_match(t.language_code, want)])
    for b in spoken:
        add([t for t in manual if _base_lang(t.language_code) == b])
    add(auto)
    add(manual)
    return ordered


def _make_api():
    from youtube_transcript_api import YouTubeTranscriptApi

    proxy = yt_proxy()
    if proxy:
        from youtube_transcript_api.proxies import GenericProxyConfig

        return YouTubeTranscriptApi(
            proxy_config=GenericProxyConfig(http_url=proxy, https_url=proxy)
        )
    return YouTubeTranscriptApi()


def list_tracks(video_id: str) -> list:
    try:
        return list(_make_api().list(video_id))
    except Exception as e:
        raise SourceError(classify(e), _exc_msg(e)) from None


def fetch_captions(video_id: str, prefs=(), translate_to: str = "") -> dict:
    tracks = list_tracks(video_id)
    if not tracks:
        raise SourceError("no_captions", "This video has no caption tracks.")
    available = [track_label(t) for t in tracks]
    ordered = order_tracks(tracks, prefs)

    plan = []
    if translate_to:
        native = [t for t in ordered if _lang_match(t.language_code, translate_to)]
        plan += [(t, None) for t in native]
        plan += [
            (t, translate_to)
            for t in ordered
            if t.is_translatable and not any(t is n for n in native)
        ]
        if not plan:
            raise SourceError(
                "no_captions",
                f"No caption track is in, or can be translated to, '{translate_to}'. "
                f"Available tracks: {', '.join(available)}",
            )
    else:
        plan = [(t, None) for t in ordered]

    def attempt(track, target):
        try:
            src = track.translate(target) if target else track
            fetched = src.fetch()
        except Exception as e:  # try the next candidate, as YouGist does
            return None, e
        segments = [
            {"start": float(s.start), "duration": float(s.duration), "text": s.text or ""}
            for s in fetched.snippets
        ]
        if not clean_snippets(segments):
            return None, None
        return {
            "source": "captions",
            "transcript_type": "auto" if track.is_generated else "manual",
            "language": re.sub(r"\s*\(auto-generated\)\s*$", "", track.language or "", flags=re.I),
            "language_code": track.language_code,
            "translated_to": target,
            "segments": segments,
            "available_tracks": available,
        }, None

    last_exc = None
    for track, target in plan:
        result, err = attempt(track, target)
        if result:
            return result
        last_exc = err or last_exc

    if translate_to:
        # YouTube rate-limits its translation endpoint far harder than plain
        # captions. Rather than fail, hand back the original-language text and
        # say so; Claude can translate it.
        reason = _exc_msg(last_exc) if last_exc else "no translatable track"
        for track in ordered:
            result, err = attempt(track, None)
            if result:
                result["translation_failed"] = True
                result["warnings"] = [
                    f"YouTube refused to machine-translate the captions into "
                    f"'{translate_to}' ({_short(reason, 80)}). This is the original "
                    f"{result['language'] or result['language_code']} transcript instead."
                ]
                return result
            last_exc = err or last_exc

    if last_exc is not None:
        raise SourceError(classify(last_exc), _exc_msg(last_exc))
    raise SourceError("no_captions", "Every caption track was empty.")


# ── source 2: hosted YouGist backend ──────────────────────────────────────────

def backend_url() -> str:
    v = os.environ.get("YOUGIST_BACKEND", DEFAULT_BACKEND).strip()
    if v.lower() in ("", "off", "none", "false", "0", "no", "disabled"):
        return ""
    return v.rstrip("/")


def _classify_message(msg: str) -> str:
    m = (msg or "").lower()
    if ("no transcript" in m or "subtitles are disabled" in m
            or "transcripts were found" in m or "no caption" in m):
        return "no_captions"
    if "no longer available" in m or "unavailable" in m or "invalid video" in m:
        return "unavailable"
    if "blocking" in m or "blocked" in m or "too many requests" in m:
        return "blocked"
    return "failed"


def fetch_backend(video_id: str) -> dict:
    base = backend_url()
    if not base:
        raise SourceError("disabled", "hosted backend disabled (YOUGIST_BACKEND=off)")
    try:
        data = http_json(
            f"{base}/api/transcript",
            {"videoUrl": watch_url(video_id), "language": "ENG"},
            timeout=180,
        )
    except HttpError as e:
        msg = ""
        try:
            msg = json.loads(e.body).get("error", "")
        except Exception:
            msg = _short(e.body, 200)
        msg = msg or f"HTTP {e.status}"
        raise SourceError(_classify_message(msg), f"backend: {_short(msg)}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SourceError("network", f"backend unreachable at {base}: {_short(e, 200)}") from None
    text = (data.get("transcript_text") or "").strip()
    if not text:
        raise SourceError("failed", "backend returned no transcript text")
    return {
        "source": "backend",
        "transcript_type": data.get("transcript_type") or "unknown",
        "language": None,
        "language_code": None,
        "translated_to": None,
        "text": text,
        "warnings": [
            "Fetched through the hosted YouGist backend: no timestamps, and it "
            "returns the first human-made track, which may not be in the spoken "
            "language."
        ],
    }


# ── source 3: Gemini audio transcription ──────────────────────────────────────

def gemini_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def fmt_ts(seconds, force_hours: bool = False) -> str:
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if (h or force_hours) else f"{m:02d}:{s:02d}"


def plan_clips(duration, clip_seconds: int) -> list:
    if not duration:
        return [(None, None)]
    clips = [(s, min(s + clip_seconds, duration)) for s in range(0, int(duration), clip_seconds)]
    # Fold a sliver (< 2 min) into the previous clip rather than paying for a call.
    if len(clips) > 1 and clips[-1][1] - clips[-1][0] < 120:
        clips[-2] = (clips[-2][0], clips[-1][1])
        clips.pop()
    return clips


def _gemini_clip(key, model, video_id, start, end, title, lang_hint):
    prompt = (
        "Transcribe the spoken audio of this video verbatim, in the language that "
        "is actually spoken. Write what is actually said, with correct spelling, "
        "punctuation and capitalization, in paragraphs. Preserve the speaker's own "
        "words and dialect: do not translate, do not standardize regional "
        "vocabulary, do not summarize, and do not add commentary, headings, "
        "speaker labels or timestamps. "
    )
    if title:
        prompt += (
            f'The video is titled "{title}"; use that only to spell names and '
            "specialist terms correctly. "
        )
    if lang_hint:
        prompt += (
            f"The speech is expected to be in the language with code '{lang_hint}'; "
            "if it is actually another language, transcribe that language. "
        )
    prompt += "If nothing is spoken, return nothing. Return only the transcript text."

    video_meta = {"fps": 0.2}  # few frames: keeps long videos inside the window
    if start is not None:
        video_meta.update({"startOffset": f"{int(start)}s", "endOffset": f"{int(end)}s"})
    gen = {"temperature": 0, "maxOutputTokens": 65536}
    if model.startswith("gemini-2.5-flash"):
        gen["thinkingConfig"] = {"thinkingBudget": 0}  # all budget to the transcript
    payload = {
        "contents": [{
            "parts": [
                {"fileData": {"fileUri": watch_url(video_id)}, "videoMetadata": video_meta},
                {"text": prompt},
            ]
        }],
        "generationConfig": gen,
    }

    label = "whole video" if start is None else f"{fmt_ts(start)}-{fmt_ts(end)}"
    data = None
    for attempt in range(3):
        try:
            data = http_json(
                GEMINI_ENDPOINT.format(model=model),
                payload,
                headers={"x-goog-api-key": key},
                timeout=900,
            )
            break
        except HttpError as e:
            try:
                msg = json.loads(e.body).get("error", {}).get("message", "")
            except Exception:
                msg = _short(e.body, 200)
            msg = msg or f"HTTP {e.status}"
            if e.status in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(4 * (attempt + 1))
                continue
            kind = "config" if e.status in (401, 403, 404) or "api key" in msg.lower() else "failed"
            if e.status == 404:
                msg += f" (model '{model}' not found; set GEMINI_MODEL to a current model)"
            raise SourceError(kind, f"gemini [{label}]: {_short(msg)}") from None
        except (urllib.error.URLError, OSError) as e:
            if attempt < 2:
                time.sleep(4 * (attempt + 1))
                continue
            raise SourceError("network", f"gemini [{label}]: {_short(e, 200)}") from None

    cands = (data or {}).get("candidates") or []
    if not cands:
        reason = ((data or {}).get("promptFeedback") or {}).get("blockReason")
        raise SourceError("failed", f"gemini [{label}]: no output" + (f" ({reason})" if reason else ""))
    cand = cands[0]
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])).strip()

    warning = None
    finish = cand.get("finishReason")
    if finish == "RECITATION":
        warning = (f"{label}: Gemini refused to reproduce this part verbatim (RECITATION), "
                   "common for music and other copyrighted recordings")
    elif finish and finish not in ("STOP", "FINISH_REASON_STOP"):
        warning = f"{label}: Gemini stopped early (finishReason={finish}); this part is probably incomplete"
    elif start is not None and text:
        minutes = (end - start) / 60
        words = len(text.split())
        if minutes >= 3 and words / minutes < 25:
            warning = (f"{label}: only {words} words for {minutes:.0f} min; possibly "
                       "truncated, or mostly music/silence")
    return text, warning


def fetch_gemini(video_id, duration, title, lang_hint, clip_minutes: float = 30) -> dict:
    key = gemini_key()
    if not key:
        raise SourceError(
            "disabled",
            "GEMINI_API_KEY is not set, so audio transcription is unavailable "
            "(free key: https://aistudio.google.com/apikey)",
        )
    model = os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL
    warnings = []
    if duration is None:
        duration = fetch_duration(video_id)
    clips = plan_clips(duration, max(60, int(clip_minutes * 60)))
    if clips == [(None, None)]:
        warnings.append(
            "Video length unknown, so Gemini transcribed the whole video in one "
            "request. Past ~1 hour that is known to truncate silently: check that "
            "the transcript reaches the end of the video."
        )
    log(f"gemini ({model}): transcribing {len(clips)} clip(s) of up to {clip_minutes:g} min")

    def run(clip):
        try:
            return _gemini_clip(key, model, video_id, clip[0], clip[1], title, lang_hint), None
        except SourceError as e:
            return ("", None), e

    with ThreadPoolExecutor(max_workers=min(3, len(clips))) as pool:
        results = list(pool.map(run, clips))

    segments, errors = [], []
    for (start, end), ((text, warn), err) in zip(clips, results):
        if err is not None:
            errors.append(err)
            warnings.append(err.message + " (this part is missing)")
            continue
        if warn:
            warnings.append(warn)
        if text:
            segments.append({
                "start": float(start or 0),
                "duration": float((end - start) if start is not None else (duration or 0)),
                "text": text,
            })
    if not segments:
        first = errors[0] if errors else SourceError("failed", "Gemini returned no transcript text")
        raise SourceError(first.kind, first.message)
    return {
        "source": "gemini",
        "transcript_type": "gemini-asr",
        "language": None,
        "language_code": lang_hint or None,
        "translated_to": None,
        "segments": segments,
        "model": model,
        "duration": duration,
        "warnings": warnings,
    }


# ── source 4: local Whisper (yt-dlp + faster-whisper) ────────────────────────

def pip_install(requirement: str, module: str, upgrade: bool = False) -> None:
    """Make `module` importable, installing `requirement` next to this Python."""
    if not upgrade:
        try:
            importlib.import_module(module)
            return
        except ImportError:
            pass
    manual = f"install it with:  {Path(sys.executable).name} -m pip install '{requirement}'"
    if os.environ.get("YT_SKILLS_NO_BOOTSTRAP"):
        raise SourceError("missing_dep", f"{module} is not installed; {manual}")
    log(f"installing {requirement} (one-time)")
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
    done = subprocess.run(cmd + (["--upgrade"] if upgrade else []) + [requirement],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise SourceError("missing_dep", f"could not install {requirement} "
                                         f"({_short(done.stderr, 200)}); {manual}")
    importlib.invalidate_caches()


def _classify_ytdlp(stderr: str) -> str:
    m = (stderr or "").lower()
    if "sign in to confirm" in m or "not a bot" in m or "http error 429" in m or "http error 403" in m:
        return "blocked"
    if "private video" in m or "video unavailable" in m or "has been removed" in m:
        return "unavailable"
    if "confirm your age" in m or "age-restricted" in m:
        return "age_restricted"
    if ("unable to download" in m or "timed out" in m or "getaddrinfo" in m
            or "connection" in m or "tunnel" in m or "name resolution" in m):
        return "network"
    return "failed"


def download_audio(video_id: str, folder: Path) -> Path:
    """Download the smallest decent audio-only stream. No ffmpeg involved:
    the file is decoded later by faster-whisper's bundled PyAV."""
    pip_install(YTDLP_REQUIREMENT, "yt_dlp")
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--quiet", "--no-warnings",
           "--no-progress", "-f", "ba[abr>=45]/ba/b", "-S", "+size,+br",
           "-o", str(folder / "audio.%(ext)s")]
    # YouTube needs a JavaScript runtime. yt-dlp looks for deno by itself;
    # node (>= 22) and bun must be switched on explicitly.
    for runtime in ("node", "bun"):
        if shutil.which(runtime):
            cmd += ["--js-runtimes", runtime]
    if yt_proxy():
        cmd += ["--proxy", yt_proxy()]
    cmd.append(watch_url(video_id))

    done = None
    for attempt in range(2):
        log("downloading the audio track with yt-dlp")
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            raise SourceError("network", "yt-dlp: the audio download timed out") from None
        files = [f for f in folder.glob("audio.*") if f.suffix != ".part"]
        if done.returncode == 0 and files:
            return files[0]
        kind = _classify_ytdlp(done.stderr)
        if kind != "failed" or attempt:
            break
        # YouTube changes often and an old yt-dlp is the usual cause: update once.
        pip_install(YTDLP_REQUIREMENT, "yt_dlp", upgrade=True)
    raise SourceError(_classify_ytdlp(done.stderr),
                      f"yt-dlp: {_short(done.stderr or 'no audio file was produced')}")


def whisper_model_name() -> str:
    name = os.environ.get("WHISPER_MODEL", "").strip()
    if name:
        return name
    # Turbo is far more accurate (small mis-hears many names and, in Chinese,
    # homophones) but needs a capable CPU to be bearable.
    return "large-v3-turbo" if (os.cpu_count() or 1) >= 8 else "small"


def decode_audio(path: Path):
    """Any audio/video file -> the 16 kHz mono float32 array Whisper wants.

    Done here, with the PyAV that faster-whisper ships with, instead of letting
    faster-whisper open the file: its own decoder passes av.open() an argument
    that newer PyAV releases reject ("unexpected keyword 'metadata_errors'").
    Also means no ffmpeg install is needed.
    """
    import av
    import numpy as np

    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=16000)
    chunks = []
    with av.open(str(path), mode="r") as container:
        if not container.streams.audio:
            raise SourceError("failed", f"{path.name} has no audio track")
        for packet in container.demux(container.streams.audio[0]):
            try:
                frames = packet.decode()
            except av.error.InvalidDataError:
                continue  # a damaged packet: skip it, keep the rest
            for frame in frames:
                chunks += [out.to_ndarray().reshape(-1) for out in resampler.resample(frame)]
        chunks += [out.to_ndarray().reshape(-1) for out in resampler.resample(None)]
    if not chunks:
        raise SourceError("failed", f"no audio could be decoded from {path.name}")
    return np.concatenate(chunks).astype(np.float32) / 32768.0


# ── speech check: nothing that was said may go missing ───────────────────────
#
# Whisper can invent a stock line ("thanks for watching", "please subscribe")
# over music, stamp it across a whole 30 s window and move on, so real speech
# inside that window is never transcribed. None of Whisper's own checks fire,
# because they need the model to be unsure and a memorised line is confident.
# So an independent voice detector (the Silero VAD that faster-whisper ships)
# maps where someone speaks, and that map decides what gets a second look:
#   - a suspicious cue is transcribed again from where its speech starts
#     (openai/whisper PR #1838: removing the silence before a hallucination is
#     what gets a correct decode);
#   - speech with no cue over it is transcribed on its own;
#   - a stock line is removed only where the detector hears no voice at all.
# The detector never cuts audio out of the first pass: it also misses singing
# and quiet speech, so it may flag and trigger a re-decode, never delete.

SUSPECT_CUE_SECONDS = 12.0    # subtitle standards cap a cue at 7 s; far longer is text stretched over other audio
SPARSE_CUE_SECONDS = 7.0      # past the cap, also require a plausible amount of text
MIN_UNCOVERED_SPEECH = 1.5    # seconds of voice with no cue over it worth a re-decode
MIN_SPEECH_IN_CUE = 0.5       # less voice than this inside a cue counts as "no speech"
REVIEW_UNHEARD_SECONDS = 3.0  # unrecognized voice shorter than this is not worth reporting
SHORT_STOCK_SECONDS = 3.0    # a stock line this brief is removed without a second decode
MAX_REDECODES = 40
AGREE_RATIO = 0.6

# Lines Whisper is known to invent over music or silence: subtitle credits and
# channel outros memorised from its training data. Compared as whole cues after
# phrase_key(). A match is a reason to check the audio, never proof.
STOCK_PHRASES = (
    "thanks for watching", "thank you for watching", "thank you so much for watching",
    "please subscribe", "like and subscribe", "please like and subscribe",
    "don't forget to subscribe", "subscribe to my channel", "please subscribe to my channel",
    "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目", "請不吝點贊訂閱轉發打賞支持明鏡與點點欄目",
    "請不吝點讚訂閱轉發打賞支持明鏡與點點欄目",
    "感谢观看", "谢谢观看", "感謝觀看", "謝謝觀看", "谢谢收看", "謝謝收看",
    "ご視聴ありがとうございました", "チャンネル登録お願いします", "チャンネル登録をお願いいたします",
    "시청해주셔서 감사합니다", "구독과 좋아요 부탁드립니다",
    "Hãy subscribe cho kênh Ghiền Mì Gõ Để không bỏ lỡ những video hấp dẫn",
    "Hãy đăng ký kênh để ủng hộ kênh của mình nhé",
    "Merci d'avoir regardé cette vidéo", "Vielen Dank fürs Zuschauen",
    "Gracias por ver el video", "Субтитры сделал DimaTorzok", "Продолжение следует",
    "TV Gelderland 2021",
)
# Credit lines whose ending varies (a name, a year).
STOCK_PREFIXES = ("字幕志愿者", "字幕志願者", "中文字幕", "字幕by", "字幕提供", "优优独播剧场", "優優獨播劇場",
                  "untertitelimauftragdeszdf", "subtitlesby", "subtitledby", "captionsby")
STOCK_ANYWHERE = ("amaraorg",)


def phrase_key(text: str) -> str:
    """Text reduced to its letters, so spacing, punctuation, case and
    full-width forms don't matter when comparing."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return "".join(c for c in text if unicodedata.category(c)[0] in "LN")


_STOCK_KEYS = frozenset(phrase_key(p) for p in STOCK_PHRASES)


def is_stock_phrase(text: str) -> bool:
    key = phrase_key(text)
    if not key:
        return False
    if key in _STOCK_KEYS or any(k in key for k in STOCK_ANYWHERE):
        return True
    return len(key) <= 30 and key.startswith(STOCK_PREFIXES)


def speech_overlap(start: float, end: float, regions) -> float:
    return sum(max(0.0, min(end, r1) - max(start, r0)) for r0, r1 in regions)


def uncovered_speech(regions, covered, min_len: float) -> list:
    """The parts of the speech regions that no (start, end) in `covered` spans."""
    out, covered = [], sorted(covered)
    for r0, r1 in regions:
        cur = r0
        for c0, c1 in covered:
            if c1 <= cur:
                continue
            if c0 >= r1:
                break
            if c0 - cur >= min_len:
                out.append((cur, c0))
            cur = max(cur, c1)
        if r1 - cur >= min_len:
            out.append((cur, r1))
    return out


def _seg_end(seg) -> float:
    return seg["start"] + seg["duration"]


_UNSPACED_UNIT_RE = re.compile("([぀-ヿ㐀-䶿一-鿿฀-๿])")


def _text_units(text: str) -> int:
    """Characters for scripts written without spaces, words otherwise."""
    return len(_UNSPACED_UNIT_RE.sub(r" \1 ", text).split())


def suspect_reason(seg):
    """Why a cue deserves a second decode, or None.

    The word test is faster-whisper's own (word_anomaly_score, used by its
    hallucination_silence_threshold): a word is odd when its probability is
    under 0.15 or it lasts under 0.133 s or over 2 s.
    """
    if is_stock_phrase(seg["text"]):
        return "a stock line that speech recognition often invents"
    dur = seg["duration"]
    if dur > SUSPECT_CUE_SECONDS:
        return f"one line stretched over {dur:.0f} seconds"
    if dur > SPARSE_CUE_SECONDS and _text_units(seg["text"]) / dur < 0.7:
        return f"very little text for {dur:.0f} seconds"
    score = 0.0
    for start, end, prob in (seg.get("words") or [])[:8]:
        length = end - start
        if prob < 0.15:
            score += 1.0
        if length < 0.133:
            score += (0.133 - length) * 15
        elif length > 2.0:
            score += length - 2.0
    if score >= 3 or (seg.get("words") and score + 0.01 >= len(seg["words"][:8])):
        return "word timing that does not fit the audio"
    return None


def _span(start: float, end: float) -> str:
    return f"{fmt_ts(start)}-{fmt_ts(end)}"


def guard_segments(segments: list, regions: list, redecode) -> tuple:
    """Second-guess a Whisper transcript against the speech map.

    `redecode(lo, hi)` transcribes just that stretch of audio and returns
    segments in whole-file time. Returns (segments, check), where check lists
    everything that was recovered, removed or left for a person to review.
    """
    recovered, removed, review, budget = [], [], [], [MAX_REDECODES]

    def again(lo, hi):
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        return [s for s in redecode(max(0.0, lo), hi) if s["text"].strip()]

    kept = []
    for i, seg in enumerate(segments):
        reason = suspect_reason(seg)
        if not reason:
            kept.append(seg)
            continue
        start, end, stock = seg["start"], _seg_end(seg), is_stock_phrase(seg["text"])
        inside = [(max(start, r0), min(end, r1)) for r0, r1 in regions if r1 > start and r0 < end]
        voice = sum(b - a for a, b in inside) >= MIN_SPEECH_IN_CUE
        if not voice and not stock:
            kept.append(seg)  # probably singing, which the detector misses
            continue
        if not voice and end - start <= SHORT_STOCK_SECONDS:
            # Invented, brief, and nobody is speaking: nothing can hide under it.
            removed.append({"at": _span(start, end), "text": seg["text"],
                            "reason": "a stock line with no voice in the audio there"})
            continue
        # The detector also misses speech over music, so a longer stock line
        # is decoded again even where it hears no voice.
        new = again(inside[0][0] - 1.0, inside[-1][1] + 1.0) if voice else again(start, end + 0.5)
        if new is None:
            kept.append(seg)
            review.append({"at": _span(start, end), "reason": f"{reason}; not re-checked (too many to re-check)"})
            continue
        # Stay inside this cue's time, so the neighbours' words are not repeated.
        new = [s for s in new if start - 0.5 <= (s["start"] + _seg_end(s)) / 2 <= end + 0.5]
        old_key, new_key = phrase_key(seg["text"]), phrase_key("".join(s["text"] for s in new))
        agree = new and SequenceMatcher(None, old_key, new_key, autojunk=False).ratio() >= AGREE_RATIO
        real = [s for s in new if not is_stock_phrase(s["text"])]
        if real and not agree and not voice:
            # Invented text changes from one decode to the next and real speech
            # does not. With no voice detected, new words count only if a third
            # decode, from a shifted start, hears them again.
            third = phrase_key("".join(s["text"] for s in again(start + 1.0, end + 0.5) or []))
            real_key = phrase_key("".join(s["text"] for s in real))
            if SequenceMatcher(None, real_key, third, autojunk=False).ratio() < AGREE_RATIO:
                real = []
        if agree and voice:
            kept.append(seg)  # heard the same thing twice, the second time from the speech onset
        elif real and not agree:
            kept.extend(real)
            recovered.append({"at": _span(real[0]["start"], _seg_end(real[-1])),
                              "text": " ".join(s["text"] for s in real),
                              "replaced": seg["text"], "reason": reason})
        elif stock:
            # Any voice under it is now uncovered: the pass below tries it once
            # more on its own, and reports it for review if that fails too.
            removed.append({"at": _span(start, end), "text": seg["text"],
                            "reason": "a stock line; a second pass found nothing else there"})
        else:
            kept.append(seg)
    kept.sort(key=lambda s: s["start"])

    # Speech that no cue covers at all.
    for u0, u1 in uncovered_speech(regions, [(s["start"], _seg_end(s)) for s in kept], MIN_UNCOVERED_SPEECH):
        new = again(u0 - 0.5, u1 + 0.5)
        near = phrase_key("".join(s["text"] for s in kept if _seg_end(s) > u0 - 10 and s["start"] < u1 + 10))
        fresh = []
        for s in new or []:
            key = phrase_key(s["text"])
            if key and key not in near and not is_stock_phrase(s["text"]):
                s["start"] = max(s["start"], u0 - 0.5)
                s["duration"] = max(0.2, min(_seg_end(s), u1 + 0.5) - s["start"])
                fresh.append(s)
        if fresh:
            kept.extend(fresh)
            recovered.append({"at": _span(fresh[0]["start"], _seg_end(fresh[-1])),
                              "text": " ".join(s["text"] for s in fresh),
                              "reason": "speech that the first pass skipped"})
        elif u1 - u0 >= REVIEW_UNHEARD_SECONDS:
            review.append({"at": _span(u0, u1),
                           "reason": "a voice was detected but no words were recognized "
                                     "(often singing, laughter or noise)"
                                     + ("" if new is not None else "; not re-checked")})
    kept.sort(key=lambda s: s["start"])

    no_speech = [{"at": _span(s["start"], _seg_end(s)), "text": s["text"]} for s in kept
                 if speech_overlap(s["start"], _seg_end(s), regions) < MIN_SPEECH_IN_CUE]
    check = {
        "status": "review" if review else "pass",
        "speech_seconds": round(sum(r1 - r0 for r0, r1 in regions), 1),
        "recovered": recovered or None,
        "removed": removed or None,
        "review": review or None,
        "no_voice_lines": no_speech or None,
    }
    return kept, {k: v for k, v in check.items() if v is not None}


def speech_regions(audio) -> list:
    """Where someone speaks, as (start, end) seconds, from Silero VAD.

    Tighter than faster-whisper's decode defaults, which pad and merge so much
    that they over-report speech; 0.35 is the threshold stable-ts audits with.
    """
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    opts = VadOptions(threshold=0.35, min_silence_duration_ms=400, speech_pad_ms=100,
                      min_speech_duration_ms=250)
    return [(t["start"] / 16000.0, t["end"] / 16000.0) for t in get_speech_timestamps(audio, opts)]


def _whisper_pass(model, audio, lang, offset: float = 0.0, progress=None):
    # vad_filter off: it drops singing and can collapse a music-heavy video
    # into one cue. condition_on_previous_text off: one mis-heard segment
    # otherwise cascades into repetition loops. word_timestamps on: tighter
    # cue timing, and the per-word evidence suspect_reason() reads.
    cues, info = model.transcribe(
        audio, language=lang or None, beam_size=5, vad_filter=False,
        condition_on_previous_text=False, word_timestamps=True,
    )
    segments = []
    for cue in cues:  # a generator: the work happens here
        text = (cue.text or "").strip()
        if text:
            segments.append({
                "start": offset + float(cue.start),
                "duration": max(0.0, float(cue.end - cue.start)),
                "text": text,
                "words": [(float(w.start), float(w.end), float(w.probability)) for w in (cue.words or [])],
            })
        if progress:
            progress(float(cue.end))
    return segments, info


def transcribe_audio(path: Path, lang_hint: str = "") -> dict:
    pip_install(WHISPER_REQUIREMENT, "faster_whisper")
    from faster_whisper import WhisperModel

    name = whisper_model_name()
    device = os.environ.get("WHISPER_DEVICE", "").strip() or "cpu"
    warnings, check, regions = [], None, None
    try:
        audio = decode_audio(path)
        log(f"whisper: loading model '{name}' (the first use downloads it)")
        model = WhisperModel(name, device=device,
                             compute_type="int8" if device == "cpu" else "float16",
                             cpu_threads=min(8, os.cpu_count() or 4))
        total, mark = len(audio) / 16000.0, [10]

        def progress(done):
            while total and done / total * 100 >= mark[0]:
                log(f"whisper: {mark[0]}%")
                mark[0] += 10

        segments, info = _whisper_pass(model, audio, _base_lang(lang_hint), progress=progress)
        log(f"whisper: {fmt_ts(total)} of audio, language '{info.language}'")

        def redecode(lo, hi):
            clip = audio[int(lo * 16000):int(hi * 16000)]
            if len(clip) < 8000:
                return []
            return _whisper_pass(model, clip, info.language, offset=lo)[0]

        try:
            regions = speech_regions(audio)
        except Exception as e:  # a check that cannot run is reported, never a silent pass
            warnings.append("The speech check could not run, so missing speech would not "
                            f"have been noticed ({_short(e, 120)}).")
            check = {"status": "not_run"}
        else:
            log("whisper: checking the transcript against where speech was detected")
            segments, check = guard_segments(segments, regions, redecode)
    except SourceError:
        raise
    except Exception as e:
        kind = "network" if "huggingface" in str(e).lower() or isinstance(e, OSError) else "failed"
        raise SourceError(kind, f"whisper: {_short(e)}") from None
    if not segments:
        raise SourceError("failed", "whisper: no speech was recognized in the audio")
    for seg in segments:
        seg.pop("words", None)
    return {
        "source": "whisper",
        "transcript_type": "auto",
        "language": None,
        "language_code": info.language,
        "translated_to": None,
        "segments": segments,
        "model": name,
        "duration": int(total) or None,
        "warnings": warnings,
        "speech_check": check,
        "speech_regions": regions,
    }


def fetch_whisper(video_id: str, local_file=None, lang_hint: str = "") -> dict:
    if local_file:
        return transcribe_audio(local_file, lang_hint)
    folder = Path(tempfile.mkdtemp(prefix=f"youtube-sub-audio-{video_id}-"))
    try:
        return transcribe_audio(download_audio(video_id, folder), lang_hint)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# ── rendering ─────────────────────────────────────────────────────────────────

def fmt_srt_time(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def group_cues(segments, seconds: float = 30, max_chars: int = 600) -> list:
    groups, cur = [], None
    for seg in segments:
        text = clean_cue(seg["text"])
        if not text:
            continue
        if (cur is None or seconds <= 0 or seg["start"] - cur["start"] >= seconds
                or len(cur["text"]) >= max_chars):
            if cur:
                groups.append(cur)
            cur = {"start": seg["start"], "text": text}
        else:
            cur["text"] += " " + text
    if cur:
        groups.append(cur)
    for g in groups:
        g["text"] = strip_fillers(g["text"])
    return [g for g in groups if g["text"]]


def render_text(result) -> str:
    if result["source"] in CUE_SOURCES:
        text = clean_snippets(result["segments"])
    elif result["source"] == "gemini":
        text = "\n\n".join(clean_prose(s["text"]) for s in result["segments"])
    else:
        text = result["text"]
    return to_paragraphs(truncate_at_sentence(text, MAX_TRANSCRIPT_CHARS))


def render_timestamped(result, group_seconds: float, long_ts: bool) -> str:
    lines = []
    if result["source"] in CUE_SOURCES:
        for g in group_cues(result["segments"], group_seconds):
            lines.append(f"[{fmt_ts(g['start'], long_ts)}] {g['text']}")
        return "\n".join(lines)
    # Gemini: timing is per clip only.
    blocks = []
    for s in result["segments"]:
        body = to_paragraphs(clean_prose(s["text"]))
        blocks.append(f"[{fmt_ts(s['start'], long_ts)}]\n{body}")
    return "\n\n".join(blocks)


def srt_cues(segments) -> list:
    """Cleaned, non-empty cues as (start, end, text). Cue N is list item N-1."""
    cues = [(s["start"], s["duration"], strip_fillers(clean_cue(s["text"]))) for s in segments]
    cues = [c for c in cues if c[2]]
    out = []
    for i, (start, dur, text) in enumerate(cues):
        end = start + (dur if dur > 0 else 2.0)
        if i + 1 < len(cues) and cues[i + 1][0] > start:
            end = min(end, cues[i + 1][0])  # ASR cues overlap; don't stack them
        out.append((start, end, text))
    return out


def render_srt(segments) -> str:
    return "\n".join(
        f"{i}\n{fmt_srt_time(start)} --> {fmt_srt_time(end)}\n{text}\n"
        for i, (start, end, text) in enumerate(srt_cues(segments), 1)
    )


def render_cue_lines(segments) -> str:
    """The SRT's text only, one "[N] text" line per cue, for correction.

    Timing never goes to the model: apply_corrections.py puts the corrected
    text back into cue N of the .srt file.
    """
    return "\n".join(f"[{i}] {text}" for i, (_, _, text) in enumerate(srt_cues(segments), 1))


def correction_dir(video_id: str, fmt: str) -> Path:
    # A fresh private (0700) folder per run. A predictable name in a shared
    # /tmp would let another local user pre-plant symlinks or "corrected" parts.
    return Path(tempfile.mkdtemp(prefix=f"youtube-sub-{video_id}-{fmt}-"))


def write_parts(folder: Path, text: str) -> list:
    """Split text into fresh part-NN.txt files, clearing any earlier run."""
    (folder / "corrected").mkdir(parents=True, exist_ok=True)
    for stale in [*folder.glob("part-*.txt"), *(folder / "corrected").glob("part-*.txt")]:
        stale.unlink()
    paths = []
    for i, piece in enumerate(chunk_text(text, CORRECTION_PART_CHARS), 1):
        p = folder / f"part-{i:02d}.txt"
        p.write_text(piece + "\n", encoding="utf-8")
        paths.append(str(p))
    return paths


def marker_spans(segments, fmt: str, group_seconds: float, long_ts: bool) -> dict:
    """The audio each "[N]" or "[mm:ss]" correction line covers, as marker -> [start, end]."""
    if fmt == "srt":
        return {str(i): [round(a, 2), round(b, 2)] for i, (a, b, _) in enumerate(srt_cues(segments), 1)}
    if fmt != "timestamped" or not segments:
        return {}
    groups = group_cues(segments, group_seconds)
    last = max(s["start"] + s["duration"] for s in segments)
    spans = {}
    for g, nxt in zip(groups, groups[1:] + [{"start": last}]):
        key = fmt_ts(g["start"], long_ts)
        spans[key] = [round(spans.get(key, [g["start"]])[0], 2), round(max(nxt["start"], g["start"]), 2)]
    return spans


def write_speech_map(folder: Path, regions, spans: dict) -> None:
    """What apply_corrections.py needs to judge a deleted line without the audio."""
    (folder / "speech.json").write_text(json.dumps({
        "speech": [[round(a, 2), round(b, 2)] for a, b in regions],
        "markers": spans,
    }), encoding="utf-8")


def source_detail(result) -> str:
    src = result["source"]
    if src == "captions":
        kind = "auto-generated" if result["transcript_type"] == "auto" else "human-made"
        lang = result.get("language") or result.get("language_code") or "?"
        detail = f"YouTube captions ({kind}, {lang})"
        if result.get("translated_to"):
            detail += f", machine-translated by YouTube to '{result['translated_to']}'"
        return detail
    if src == "backend":
        return f"YouTube captions via the hosted YouGist backend ({result['transcript_type']})"
    if src == "whisper":
        return (f"Whisper transcription of the audio, run locally "
                f"({result.get('model')}, language '{result.get('language_code')}'), no captions used")
    return f"Gemini audio transcription ({result.get('model', DEFAULT_GEMINI_MODEL)}), no captions used"


def build_header(meta: dict) -> str:
    rows = []
    if meta.get("title"):
        rows.append(("Title", meta["title"]))
    if meta.get("channel"):
        rows.append(("Channel", meta["channel"]))
    if meta.get("url"):
        rows.append(("URL", meta["url"]))
    if meta.get("file"):
        rows.append(("File", meta["file"]))
    if meta.get("duration"):
        rows.append(("Duration", meta["duration"]))
    rows.append(("Source", meta["source_detail"]))
    return "\n".join(f"{k + ':':<10}{v}" for k, v in rows)


def slugify(text: str, limit: int = 60) -> str:
    text = (text or "").replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    if len(text) <= limit:
        return text
    cut = text.rfind("-", 0, limit + 1)  # end on a whole word
    return text[: cut if cut > limit // 2 else limit].rstrip("-")


# ── main ──────────────────────────────────────────────────────────────────────

def failure_hints(attempts) -> list:
    kinds = {a.get("kind") for a in attempts if not a.get("ok")}
    hints = []
    if kinds & {"blocked", "network"}:
        hints.append(
            "YouTube is blocking or unreachable from this machine. Cloud and "
            "sandbox IPs are often blocked, and heavy use gets any IP rate-limited "
            "for a while. Options: wait and retry, set YT_PROXY_URL to a "
            "residential proxy, or set GEMINI_API_KEY. In the Claude desktop/web "
            "app, allow network access to youtube.com (Settings > Capabilities)."
        )
    tried = {a.get("source") for a in attempts}
    if "whisper" not in tried and kinds & {"no_captions", "age_restricted", "failed", "unplayable"}:
        hints.append(
            "No usable captions. Run again with --source whisper to transcribe the "
            "audio on this machine: no API key, but it is slow and the first run "
            "downloads a speech model (0.5 to 1.6 GB)."
        )
    if "whisper" in tried and kinds & {"blocked", "network"}:
        hints.append(
            "The audio could not be downloaded here. Download the video or its audio "
            "on a machine that can reach YouTube, then pass that file's path instead "
            "of the URL to transcribe it locally."
        )
    if "no_captions" in kinds and not gemini_key():
        hints.append(
            "Alternatively set GEMINI_API_KEY (free at https://aistudio.google.com/apikey) "
            "to transcribe the audio with Gemini."
        )
    if "missing_dep" in kinds:
        hints.append(f"Install the caption library:  python3 -m pip install '{REQUIREMENT}'")
    if "age_restricted" in kinds:
        hints.append("The video is age-restricted; YouTube only serves its captions to signed-in users.")
    if "config" in kinds:
        hints.append("Check GEMINI_API_KEY / GEMINI_MODEL.")
    return hints


def main(argv=None) -> int:
    global QUIET
    ap = argparse.ArgumentParser(
        description="Fetch the full transcript of a YouTube video (captions, with a Gemini fallback).",
    )
    ap.add_argument("video", help="YouTube URL (watch, youtu.be, shorts, live, embed), 11-char video id, "
                                  "or the path of a local audio/video file to transcribe")
    ap.add_argument("--lang", default="",
                    help="preferred caption languages, comma-separated, best first (e.g. 'en,vi')")
    ap.add_argument("--translate", default="", metavar="LANG",
                    help="have YouTube machine-translate the captions into LANG (e.g. 'en')")
    ap.add_argument("--format", choices=["text", "timestamped", "srt", "json"], default="text")
    ap.add_argument("--group-seconds", type=float, default=30,
                    help="timestamped format: start a new line every N seconds (0 = one line per cue)")
    ap.add_argument("--out", help="write the transcript to this file")
    ap.add_argument("--out-dir", help="write into this directory, naming the file after the title")
    ap.add_argument("--no-header", action="store_true", help="omit the Title/URL/Source header")
    ap.add_argument("--raw", action="store_true",
                    help="keep auto-generated captions as they are: do not prepare parts for correction")
    ap.add_argument("--source", choices=["auto", "captions", "backend", "gemini", "whisper"], default="auto",
                    help="whisper = download the audio and transcribe it on this machine")
    ap.add_argument("--no-backend", action="store_true", help="never use the hosted YouGist backend")
    ap.add_argument("--no-gemini", action="store_true", help="never use Gemini")
    ap.add_argument("--gemini-clip-minutes", type=float, default=30)
    ap.add_argument("--list-tracks", action="store_true", help="list caption tracks and exit")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    QUIET = args.quiet

    local_file = Path(args.video).expanduser()
    local_file = local_file.resolve() if local_file.is_file() else None
    video_id = "" if local_file else extract_video_id(args.video)
    if (not local_file and not video_id) or (local_file and args.list_tracks):
        print(json.dumps({"ok": False, "error": "Not a YouTube video URL or id, nor an existing "
                                                f"audio/video file: {args.video!r}"}))
        return EXIT_USAGE
    url = None if local_file else watch_url(video_id)
    prefs = [p.strip() for p in args.lang.split(",") if p.strip()]

    order = {"auto": ["captions", "backend", "gemini"]}.get(args.source, [args.source])
    if args.source == "auto":
        if args.no_backend or args.translate:
            order.remove("backend")
        if args.no_gemini:
            order.remove("gemini")
    if args.list_tracks:
        order = ["captions"]
    if local_file:
        order = ["whisper"]

    have_lib = False
    if "captions" in order or "whisper" in order:
        have_lib = ensure_library(force_venv="whisper" in order)

    if args.list_tracks:
        if not have_lib:
            print(json.dumps({"ok": False, "error": "youtube-transcript-api unavailable"}))
            return EXIT_NO_TRANSCRIPT
        try:
            tracks = list_tracks(video_id)
        except SourceError as e:
            print(json.dumps({"ok": False, "video_id": video_id, "kind": e.kind, "error": e.message}))
            return EXIT_UNAVAILABLE if e.kind == "unavailable" else EXIT_NO_TRANSCRIPT
        print(json.dumps({
            "ok": True,
            "video_id": video_id,
            "tracks": [
                {"language_code": t.language_code, "language": t.language,
                 "auto_generated": t.is_generated, "translatable": t.is_translatable}
                for t in tracks
            ],
            "preferred_order": [track_label(t) for t in order_tracks(tracks, prefs)],
        }, ensure_ascii=False, indent=2))
        return EXIT_OK

    meta_web = {"title": local_file.stem} if local_file else fetch_oembed(video_id)
    attempts, result, captions_kind = [], None, None
    for src in order:
        if src == "backend" and captions_kind in ("no_captions", "age_restricted", "unavailable"):
            attempts.append({"source": "backend", "ok": False, "kind": "skipped",
                             "error": "skipped: the video itself has no usable captions, "
                                      "so another server would get the same answer"})
            continue
        try:
            if src == "captions":
                if not have_lib:
                    raise SourceError("missing_dep", "youtube-transcript-api is not installed")
                result = fetch_captions(video_id, prefs, args.translate)
            elif src == "backend":
                result = fetch_backend(video_id)
            elif src == "whisper":
                result = fetch_whisper(video_id, local_file, prefs[0] if prefs else "")
            else:
                result = fetch_gemini(video_id, None, meta_web.get("title"),
                                      prefs[0] if prefs else "", args.gemini_clip_minutes)
            attempts.append({"source": src, "ok": True})
            break
        except SourceError as e:
            attempts.append({"source": src, "ok": False, "kind": e.kind, "error": e.message})
            if src == "captions":
                captions_kind = e.kind
            log(f"{src} failed ({e.kind}): {e.message}")
            if e.kind == "unavailable":
                break  # nothing can transcribe a video that doesn't exist

    if result is None:
        unavailable = any(a.get("kind") == "unavailable" for a in attempts)
        print(json.dumps({
            "ok": False,
            "video_id": video_id or None,
            "url": url,
            "title": meta_web.get("title"),
            "error": "Video unavailable" if unavailable else "No transcript could be obtained",
            "attempts": attempts,
            "hints": failure_hints(attempts),
        }, ensure_ascii=False, indent=2))
        return EXIT_UNAVAILABLE if unavailable else EXIT_NO_TRANSCRIPT

    warnings = list(result.get("warnings") or [])
    segments = result.get("segments")
    duration = result.get("duration")
    if result["source"] in CUE_SOURCES and segments:
        duration = int(max(s["start"] + s["duration"] for s in segments))
    long_ts = bool(duration and duration >= 3600)

    fmt = args.format
    if fmt in ("srt", "timestamped") and result["source"] == "backend":
        warnings.append(f"'{fmt}' needs cue timing, which the hosted backend does not return; wrote plain text.")
        fmt = "text"
    if fmt == "srt" and result["source"] == "gemini":
        warnings.append("Gemini output has no per-line timing, so SRT is not possible; wrote timestamped text (per clip).")
        fmt = "timestamped"
    if fmt == "timestamped" and result["source"] == "gemini":
        warnings.append(f"Gemini timestamps mark clip starts only (every {args.gemini_clip_minutes:g} min).")

    meta = {
        "video_id": video_id or None,
        "url": url,
        "file": str(local_file) if local_file else None,
        "title": meta_web.get("title"),
        "channel": meta_web.get("channel"),
        "duration_seconds": duration,
        "duration": fmt_ts(duration) if duration else None,
        "source": result["source"],
        "source_detail": source_detail(result),
        "transcript_type": result["transcript_type"],
        "language": result.get("language"),
        "language_code": result.get("language_code"),
        "translated_to": result.get("translated_to"),
        "translation_failed": result.get("translation_failed"),
        "available_tracks": ", ".join(result["available_tracks"]) if result.get("available_tracks") else None,
    }

    if fmt == "text":
        body = render_text(result)
    elif fmt == "timestamped":
        body = render_timestamped(result, args.group_seconds, long_ts)
    elif fmt == "srt":
        body = render_srt(segments)
    else:
        body = render_text(result)
        clean_segments = None
        if result["source"] in CUE_SOURCES:
            clean_segments = [
                {"start": round(s["start"], 3), "duration": round(s["duration"], 3),
                 "text": strip_fillers(clean_cue(s["text"]))}
                for s in segments
            ]
            clean_segments = [s for s in clean_segments if s["text"]]
        elif result["source"] == "gemini":
            clean_segments = [
                {"start": s["start"], "duration": s["duration"], "text": clean_prose(s["text"])}
                for s in segments
            ]
        body_json = json.dumps({**meta, "segments": clean_segments, "text": body},
                               ensure_ascii=False, indent=2)

    if fmt in ("text", "timestamped") and not args.no_header:
        document = build_header(meta) + "\n\n" + body + "\n"
    elif fmt == "json":
        document = body_json + "\n"
    else:
        document = body + "\n"

    out_path = None
    if args.out:
        out_path = Path(args.out).expanduser()
    elif args.out_dir:
        stem = slugify(meta.get("title") or "")
        stem = "-".join(x for x in (stem, video_id) if x) or "transcript"
        ext = {"text": ".txt", "timestamped": ".timestamped.txt", "json": ".json"}.get(fmt)
        if fmt == "srt":
            code = result.get("translated_to") or result.get("language_code")
            ext = f".{code}.srt" if code else ".srt"
        out_path = Path(args.out_dir).expanduser() / (stem + ext)

    # Auto-generated captions (not machine-translated ones: their errors are
    # the translator's, not mis-hearings) are handed to Claude for correction.
    parts, parts_dir = [], None
    if (out_path and not args.raw and fmt != "json"
            and result["transcript_type"] == "auto" and not result.get("translated_to")):
        parts_dir = correction_dir(video_id or "file", fmt)
        parts = write_parts(parts_dir, render_cue_lines(segments) if fmt == "srt" else body)
        if result.get("speech_regions") is not None:
            write_speech_map(parts_dir, result["speech_regions"],
                             marker_spans(segments, fmt, args.group_seconds, long_ts))

    plain = render_text(result) if fmt != "text" else body
    report = {
        "ok": True,
        **meta,
        "format": fmt,
        "cues": body.count(" --> ") if fmt == "srt" else None,
        "chars": len(plain),
        "words": len(plain.split()),
        "out": str(out_path.resolve()) if out_path else None,
        "needs_correction": True if parts else None,
        "parts_dir": str(parts_dir) if parts else None,
        "parts": parts or None,
        "speech_check": result.get("speech_check"),
        "attempts": attempts,
        "warnings": warnings or None,
    }
    report = {k: v for k, v in report.items() if v is not None}

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(document, encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(document)
        sys.stdout.flush()
        print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
