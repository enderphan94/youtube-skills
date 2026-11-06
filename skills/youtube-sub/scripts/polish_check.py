#!/usr/bin/env python3
"""
polish_check.py - verify an LLM-polished transcript, section by section.

Port of YouGist's polishLooksSane() (background.js). Published work on ASR
post-correction finds that an UNVERIFIED correction pass is worse on average
than doing nothing, so every check fails closed: a polished section that looks
wrong is replaced by its original text, and nothing is ever lost.

A polished section is rejected when:
  1. it is empty;
  2. it contains a writing system the original did not (language drift, e.g.
     a model answering in Chinese);
  3. its length changed by more than 2x either way (it summarized, or started
     editorializing);
  4. more than 30% of its words differ from the original once punctuation and
     case are ignored (it paraphrased instead of fixing mis-heard words).

USAGE
  python3 polish_check.py --original-dir PARTS --polished-dir POLISHED --out FINAL.txt
                          [--header-from TRANSCRIPT.txt]

  PARTS holds part-01.txt, part-02.txt, ... from fetch_transcript.py
  --chunk-chars; POLISHED holds files with the same names. A missing polished
  file counts as rejected. Prints a JSON report.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

MAX_EDIT_RATE = 0.3

# Scripts a model might wander into that the source almost certainly wasn't in:
# kana, CJK, Hangul, Cyrillic, Hebrew, Arabic.
FOREIGN_SCRIPT_RE = re.compile(
    "[぀-ヿ㐀-䶿一-鿿가-힯"
    "Ѐ-ӿ֐-׿؀-ۿ]"
)
_PUNCT_RE = re.compile("[.,!?;:—–\"“”'‘’()\\[\\]…]")
_HEADER_LINE_RE = re.compile(r"^[A-Z][A-Za-z ]{1,15}:\s")


def script_ratio(text: str) -> float:
    if not text:
        return 0.0
    return len(FOREIGN_SCRIPT_RE.findall(text)) / len(text)


def content_tokens(text: str) -> list:
    """Punctuation and case are what polishing may change, so ignore them."""
    return _PUNCT_RE.sub(" ", (text or "").lower()).split()


def edit_rate(a: list, b: list) -> float:
    """Word-level edit rate, (max(n, m) - matched) / n.

    difflib's matching blocks are a lower bound on the LCS, so this can only
    overestimate the edit rate, which errs toward rejecting: the safe side.
    """
    n, m = len(a), len(b)
    if not n:
        return 1.0 if m else 0.0
    matched = sum(block.size for block in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
    return (max(n, m) - matched) / max(n, 1)


def check(original: str, polished: str):
    """Return (accepted, reason)."""
    if not polished or not polished.strip():
        return False, "empty"
    before, after = script_ratio(original), script_ratio(polished)
    if after > 0.02 and after > before * 3 + 0.01:
        return False, "language drift (a writing system the original does not use)"
    ratio = len(polished) / max(1, len(original))
    if ratio < 0.5 or ratio > 2.0:
        return False, f"length changed {ratio:.2f}x (summarized or padded)"
    rate = edit_rate(content_tokens(original), content_tokens(polished))
    if rate > MAX_EDIT_RATE:
        return False, f"{rate:.0%} of words changed (paraphrased, limit {MAX_EDIT_RATE:.0%})"
    return True, f"{rate:.0%} of words changed"


def read_header(path: Path) -> str:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or not _HEADER_LINE_RE.match(line):
            break
        lines.append(line)
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verify and assemble a polished transcript.")
    ap.add_argument("--original-dir", required=True)
    ap.add_argument("--polished-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--header-from", help="copy the Title/URL/... header from this transcript file")
    args = ap.parse_args(argv)

    orig_dir, pol_dir = Path(args.original_dir), Path(args.polished_dir)
    parts = sorted(orig_dir.glob("part-*.txt"))
    if not parts:
        print(json.dumps({"ok": False, "error": f"no part-*.txt files in {orig_dir}"}))
        return 2

    sections, accepted, rejected = [], 0, []
    for part in parts:
        original = part.read_text(encoding="utf-8").strip()
        pol_path = pol_dir / part.name
        polished = pol_path.read_text(encoding="utf-8").strip() if pol_path.exists() else ""
        ok, reason = check(original, polished) if polished else (False, "missing")
        if ok:
            sections.append(polished)
            accepted += 1
        else:
            sections.append(original)
            rejected.append({"part": part.name, "reason": reason})

    body = "\n\n".join(sections).strip()
    header = ""
    if args.header_from and Path(args.header_from).exists():
        header = read_header(Path(args.header_from))
        if header:
            header += "\nPolished: punctuation and mis-heard words repaired by Claude; " \
                      "sections that failed verification kept as the original captions"
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text((header + "\n\n" if header else "") + body + "\n", encoding="utf-8")

    print(json.dumps({
        "ok": True,
        "out": str(out),
        "sections": len(parts),
        "accepted": accepted,
        "kept_original": rejected or None,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
