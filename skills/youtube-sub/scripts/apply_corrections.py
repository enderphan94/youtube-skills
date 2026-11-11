#!/usr/bin/env python3
"""
apply_corrections.py - verify Claude's corrected parts and write them into the
transcript file.

fetch_transcript.py splits a machine-made transcript (YouTube auto-captions or
a local Whisper transcription) into part-NN.txt files.
Claude writes a corrected copy of each into the "corrected" subfolder. This
script checks every corrected part against its original and rewrites the
transcript file (text, timestamped text or .srt) with the parts that pass.

Published work on ASR post-correction finds that an UNVERIFIED correction pass
is worse on average than doing nothing, so every check fails closed: a part
that looks wrong keeps its original text, and nothing is ever lost.
The checks are a port of YouGist's polishLooksSane() (background.js).

Deleting is checked separately, because a text-only editor cannot hear the
audio. Claude may empty a "[N]" or "[mm:ss]" line it believes the recognizer
invented, but the line is removed only when the speech map that
fetch_transcript.py saved (speech.json) shows no voice in that line's stretch
of audio. Otherwise the original text is put back and the refusal is reported:
a line that reads like an invented credit can still sit on top of real speech.

A corrected part is rejected when:
  1. it is missing or empty;
  2. its [mm:ss] timestamps or [N] cue numbers differ from the original's;
  3. it contains a writing system the original did not (language drift);
  4. its length changed by more than 2x either way (summarized or padded);
  5. more than 30% of its words differ from the original once punctuation and
     case are ignored (paraphrased instead of fixing mis-heard words).

USAGE
  python3 apply_corrections.py --parts-dir DIR --out TRANSCRIPT_FILE

  DIR is "parts_dir" from the fetch report: part-NN.txt plus
  corrected/part-NN.txt. TRANSCRIPT_FILE is the report's "out"; it is rewritten
  in place. Prints a JSON report.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

MAX_EDIT_RATE = 0.3

# Scripts a model might wander into that the source almost certainly wasn't in:
# kana, CJK, Hangul, Cyrillic, Hebrew, Arabic.
FOREIGN_SCRIPT_RE = re.compile(
    "[぀-ヿ㐀-䶿一-鿿가-힯"
    "Ѐ-ӿ֐-׿؀-ۿ]"
)
# Scripts written without spaces between words: compare these per character.
_UNSPACED_RE = re.compile("([぀-ヿ㐀-䶿一-鿿฀-๿])")
# A line-leading [mm:ss] / [h:mm:ss] timestamp or [N] cue number.
MARKER_RE = re.compile(r"^\[(\d+(?::\d\d){0,2})\]", re.MULTILINE)
_LINE_RE = re.compile(r"^\[(\d+(?::\d\d){0,2})\][ \t]*(.*)$")
_HEADER_LINE_RE = re.compile(r"^[A-Z][A-Za-z ]{1,15}:\s")
CORRECTED_NOTE = (
    "Corrected: punctuation and mis-heard words repaired by Claude; parts that "
    "failed verification keep the original text"
)


def script_ratio(text: str) -> float:
    if not text:
        return 0.0
    return len(FOREIGN_SCRIPT_RE.findall(text)) / len(text)


def content_tokens(text: str) -> list:
    """The words of a part, ignoring what correction is allowed to change:
    punctuation, case, markers and Unicode composition (captions are often
    NFD, Claude writes NFC)."""
    text = unicodedata.normalize("NFC", MARKER_RE.sub(" ", text or "")).lower()
    text = "".join(" " if unicodedata.category(c)[0] in "PS" else c for c in text)
    return _UNSPACED_RE.sub(r" \1 ", text).split()


def edit_rate(a: list, b: list) -> float:
    """Word-level edit rate, (max(n, m) - matched) / n.

    difflib's matching blocks are a lower bound on the LCS, so this can only
    overestimate the edit rate, which errs toward rejecting: the safe side.
    """
    n, m = len(a), len(b)
    if not n:
        return 1.0 if m else 0.0
    matched = sum(block.size for block in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
    return (max(n, m) - matched) / n


def without_emptied_lines(original: str, corrected: str) -> str:
    """The original minus the marker lines the correction left empty.

    A proposed deletion is judged by gate_deletions() against the audio, so it
    must not count against the edit budget, or removing one invented line from
    a short clip would reject the whole part.
    """
    emptied = [not m.group(2).strip() for m in map(_LINE_RE.match, map(str.strip, corrected.splitlines())) if m]
    out, i = [], 0
    for line in original.splitlines():
        if _LINE_RE.match(line.strip()):
            i += 1
            if i <= len(emptied) and emptied[i - 1]:
                continue
        out.append(line)
    return "\n".join(out)


def check(original: str, corrected: str):
    """Return (accepted, reason)."""
    if not corrected or not corrected.strip():
        return False, "missing or empty"
    if MARKER_RE.findall(corrected) != MARKER_RE.findall(original):
        return False, "timestamps or cue numbers were changed, added or dropped"
    original = without_emptied_lines(original, corrected)
    before, after = script_ratio(original), script_ratio(corrected)
    if after > 0.02 and after > before * 3 + 0.01:
        return False, "language drift (a writing system the original does not use)"
    ratio = len(corrected) / max(1, len(original))
    if ratio < 0.5 or ratio > 2.0:
        return False, f"length changed {ratio:.2f}x (summarized or padded)"
    rate = edit_rate(content_tokens(original), content_tokens(corrected))
    if rate > MAX_EDIT_RATE:
        return False, f"{rate:.0%} of words changed (paraphrased, limit {MAX_EDIT_RATE:.0%})"
    return True, f"{rate:.0%} of words changed"


MIN_SPEECH_IN_LINE = 0.5  # seconds of detected voice that make a line undeletable


def load_speech_map(parts_dir: Path):
    try:
        data = json.loads((parts_dir / "speech.json").read_text(encoding="utf-8"))
        return data if isinstance(data.get("markers"), dict) else None
    except (OSError, ValueError, AttributeError):
        return None


def gate_deletions(original: str, corrected: str, speech):
    """Allow an emptied marker line only where no voice was detected.

    Returns (text, removed, refused). A refused line gets its original text
    back. With no speech map (YouTube captions, or the check could not run)
    there is no evidence, so every deletion is refused.
    """
    before = [m for m in (_LINE_RE.match(line.strip()) for line in original.splitlines()) if m]
    out, removed, refused, i = [], [], [], 0
    for line in corrected.splitlines():
        m = _LINE_RE.match(line.strip())
        if not m or i >= len(before):
            out.append(line)
            continue
        old, i = before[i], i + 1
        if m.group(2).strip() or not old.group(2).strip():
            out.append(line)
            continue
        marker, span = m.group(1), None
        if speech:
            span = speech["markers"].get(marker)
        if span is None:
            reason = "there is no record of the audio behind this line, so nothing shows it is safe to delete"
        else:
            voice = sum(max(0.0, min(span[1], b) - max(span[0], a)) for a, b in speech.get("speech") or [])
            reason = (None if voice < MIN_SPEECH_IN_LINE else
                      f"a voice is heard for {voice:.1f} s of this line's audio, so real speech may be under it")
        entry = {"line": f"[{marker}]", "text": old.group(2).strip()}
        if reason:
            refused.append({**entry, "reason": reason})
            out.append(old.group(0))
        else:
            removed.append(entry)
            out.append(line)
    return "\n".join(out), removed, refused


def split_header(document: str):
    """Split a transcript file into its Title/URL/... header lines and the rest."""
    lines = document.splitlines()
    n = 0
    while n < len(lines) and _HEADER_LINE_RE.match(lines[n]):
        n += 1
    return lines[:n], "\n".join(lines[n:]).strip()


def rebuild_srt(srt: str, sections: list) -> str:
    """Put corrected "[N] text" lines back into cue N; timing is untouched.

    A marker left with no text means "nobody said this" (a line the speech
    recognizer invented over music): that cue is dropped and the rest renumbered.
    """
    texts = {}
    for section in sections:
        for line in section.splitlines():
            m = re.match(r"^\[(\d+)\]\s*(.*)$", line.strip())
            if m:
                texts[m.group(1)] = m.group(2).strip()
    blocks = []
    for block in re.split(r"\n\s*\n", srt.strip()):
        lines = block.splitlines()
        if len(lines) >= 3:
            text = texts.get(lines[0].strip(), "\n".join(lines[2:]))
            if not text:
                continue
            lines = [str(len(blocks) + 1), lines[1], text]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verify corrected parts and rewrite the transcript file.")
    ap.add_argument("--parts-dir", required=True,
                    help="'parts_dir' from the fetch report (part-NN.txt and corrected/part-NN.txt)")
    ap.add_argument("--out", required=True, help="the transcript file to rewrite ('out' from the fetch report)")
    args = ap.parse_args(argv)

    parts_dir, out = Path(args.parts_dir).expanduser(), Path(args.out).expanduser()
    parts = sorted(parts_dir.glob("part-*.txt"))
    if not parts:
        print(json.dumps({"ok": False, "error": f"no part-*.txt files in {parts_dir}"}))
        return 2
    if not out.exists():
        print(json.dumps({"ok": False, "error": f"transcript file not found: {out}"}))
        return 2

    speech = load_speech_map(parts_dir)
    sections, rejected, removed, refused = [], [], [], []
    for part in parts:
        original = part.read_text(encoding="utf-8").strip()
        fixed_path = parts_dir / "corrected" / part.name
        fixed = fixed_path.read_text(encoding="utf-8").strip() if fixed_path.exists() else ""
        ok, reason = check(original, fixed)
        if ok:
            fixed, gone, kept = gate_deletions(original, fixed, speech)
            removed += gone
            refused += kept
        sections.append(fixed if ok else original)
        if not ok:
            rejected.append({"part": part.name, "reason": reason})

    accepted = len(parts) - len(rejected)
    if accepted:
        document = out.read_text(encoding="utf-8")
        if out.suffix.lower() == ".srt":
            document = rebuild_srt(document, sections)
        else:
            header, _ = split_header(document)
            if header and CORRECTED_NOTE not in header:
                header.append(CORRECTED_NOTE)
            # Timestamped parts are lines, plain-text parts are paragraphs.
            joiner = "\n" if MARKER_RE.search(sections[0]) else "\n\n"
            body = joiner.join(sections).strip()
            # A [mm:ss] marker left with no text is a deleted (invented) line.
            body = re.sub(r"^\[[\d:]+\][ \t]*\n?", "", body, flags=re.MULTILINE)
            document = ("\n".join(header) + "\n\n" if header else "") + body + "\n"
        out.write_text(document, encoding="utf-8")

    print(json.dumps({
        "ok": True,
        # "review" means something needs a person's attention: tell the user.
        "status": "review" if rejected or refused else "pass",
        "out": str(out),
        "parts": len(parts),
        "accepted": accepted,
        "kept_original": rejected or None,
        "removed_lines": removed or None,
        "deletions_refused": refused or None,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
