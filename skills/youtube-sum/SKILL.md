---
name: youtube-sum
description: Summarize any YouTube video (talks, lectures, podcasts, interviews, tutorials, sermons, news, Shorts, live-stream replays; any length, any language) into numbered key points that keep the names, numbers, examples and reasoning. Use whenever the user shares a YouTube link or video id and asks for a summary, key points, takeaways, a TL;DR, notes, "what is this video about", or whether it is worth watching. Fetches the transcript itself (human or auto-generated captions, falling back to Gemini audio transcription when there are none), handles multi-hour videos section by section, and writes the summary in whatever language the user wants.
---

# YouTube summary (`youtube-sum`)

Summarizes a YouTube video from its full transcript, using YouGist's method:
exactly N numbered key points with high information retention. Long videos are
summarized section by section and then merged, so nothing is silently truncated.

In the commands below, `SKILL_DIR` means this skill's folder. Claude Code shows
it as "Base directory for this skill". On Claude.ai or in the desktop chat it is
usually `/mnt/skills/user/youtube-sum`. Use `python` instead of `python3` on Windows.

## 0. Read the request

| Setting | Values | Default | Cues |
|---|---|---|---|
| **count** | 1 to 20 points | 10 | "5 key points", "top 3 takeaways" |
| **detail** | `brief` · `standard` · `detailed` | `standard` | TL;DR / quick / one-liners → `brief`. In depth / detailed / thorough / study notes → `detailed` |
| **language** | any | the language the user wrote in | "in Vietnamese", "bằng tiếng Việt", "auf Deutsch" |
| **timestamps** | on / off | off | "with timestamps", "where in the video" |

The output language is never chosen by the video's language. A Vietnamese talk
summarized for an English-speaking user gets an English summary.

## 1. Fetch the transcript

```bash
python3 "SKILL_DIR/scripts/fetch_transcript.py" "<youtube url or id>" \
  --out-dir "${TMPDIR:-/tmp}/youtube-sum" --chunk-chars 40000
```

Add `--format timestamped` when timestamps are on. The transcript is a working
file, so keep it in that temp folder in every environment. Only the summary goes
to a user-visible folder, and only when the user asks for it to be saved. The
first run takes about 10 seconds while the script creates its own Python
environment; do not pip-install anything yourself.

It prints a JSON report: `ok`, `title`, `channel`, `duration`, `source_detail`,
`chars`, `words`, `out` (the full transcript), `parts` (the transcript split into
files of at most 40k characters), and `warnings`.

The transcript comes from YouTube captions (human-made first, then
auto-generated), the hosted YouGist backend if YouTube blocks this machine, or
Gemini audio transcription if the video has no captions. The script picks the
source on its own.

**If `ok` is false**, do not summarize from the title or from your own knowledge
of the topic. Explain what failed using `attempts` and `hints`, and give the fix:

- `no_captions`: the video has no captions. The user can set `GEMINI_API_KEY`
  (free at https://aistudio.google.com/apikey) so Gemini transcribes the audio.
- `blocked` / `network`: YouTube blocks or cannot be reached from this machine.
  Options are `YT_PROXY_URL`, `GEMINI_API_KEY`, or, in the Claude desktop or web
  app, allowing network access to `youtube.com` (Settings > Capabilities).
- `unavailable`: private, deleted or not a real video.

Never ask the user to paste an API key into the chat.

## 2. Read and summarize

**Up to 120,000 characters (3 parts or fewer):** read every part file in order,
then write the summary in one pass.

**Over 120,000 characters:** map, then reduce.

1. **Map.** For every part, write section notes: up to `max(3, min(count, 8))`
   numbered points covering only that section, in the output language. Use the
   *section notes* instructions in
   [references/summary-guide.md](references/summary-guide.md). If an Agent/Task
   tool is available, give each part to a subagent (up to 4 in parallel) with
   those instructions and the part's path, and have it return the notes.
   Otherwise read the parts one at a time and write the notes yourself.
2. **Reduce.** Merge all section notes, in order, into exactly `count` points,
   using the *merge* instructions in the same guide.

Either way, the summary contains only what the transcript says. Never fill gaps
from your own knowledge, the title, or the video description.

## 3. Writing rules (from YouGist)

The transcript is automatic speech-to-text. Punctuation is unreliable, words may
be mis-heard, and phrases may repeat. Silently correct obvious recognition errors
using context, but never invent facts that are not there. Reconstruct a garbled
name or term only when the topic makes it certain (in a Buddhist talk, "fan kara"
is *saṅkhāra*). Leave out a passage too garbled to understand rather than guess at
it. If most of the transcript is unintelligible, say so instead of producing a
confident summary.

- Exactly `count` numbered points, following the video's narrative or logical order.
- With fewer points than the video has topics, follow its main line of argument.
  Fold related ideas into one point, and drop side anecdotes before you drop
  steps of the argument.
- Keep the important names, numbers, examples, reasons, warnings and conclusions.
  If the speaker explains *why* something matters, include that. If the content
  is a sequence, process or argument, preserve its logic.
- Length per point, by detail level:
  - **brief**: one sentence of about 20 to 30 words, the core claim only.
  - **standard**: usually 2 to 4 sentences with the supporting context needed to
    understand it. Not vague, not generic.
  - **detailed**: at least 4 full sentences, ideally 5 to 7 (about 100 to 170
    words; Vietnamese runs about 120 to 200). Reach that length with the
    speaker's actual material: the names and numbers, concrete examples, a
    condensed retelling of any story or analogy, the reasoning, caveats and the
    conclusion. Never pad or repeat. Each point carries its full length on its
    own; do not ration length across points. For more than 5 detailed points,
    write them in batches of five, with each batch covering material the earlier
    ones did not.
- Each point is plain prose: no headings, bold, or nested bullets inside points.

## 4. Output

Write everything in the output language, except the video title. Keep the title
as it is; if it is in another language, you may add a translation in parentheses.

```
**<Title>** · <Channel> · <duration>
_Transcript: <source in a few words>. <a caveat if auto-captions, Gemini, translated or partial>_

1. …
2. …

Full transcript: `<out>`. Ask me anything about the video.
```

For example, for a Vietnamese talk summarized in English:

```
**Càng Tu càng Tệ vì không thấy Chân đế - Ajahn Chah** (Practicing harder, getting worse: not seeing ultimate truth) · Tỉnh Thức · 26:20
_Transcript: auto-generated Vietnamese captions, so some names and Pali terms were mis-heard and have been corrected from context._

1. …
```

The caveat is one short clause:

| `source_detail` | Caveat |
|---|---|
| human-made captions | none needed |
| auto-generated | names and terms may have been mis-heard |
| Gemini | AI transcription of the audio |
| translated | machine-translated captions |
| any truncation warning | covers only part of the video |

With timestamps on, start each point with the `[mm:ss]` where that topic begins
in the timestamped transcript.

The closing line points to the transcript so the user can ask follow-up questions. Answer those from the transcript file: search or
read the relevant part, and quote with timestamps when you have them. Don't
answer from memory.

If the user wants the summary saved, write it as Markdown into the folder they
name, or `/mnt/user-data/outputs` when that exists (Claude.ai), or else the
current directory.

## Notes

- **Several videos:** summarize each one. For "compare these", summarize each and
  then add a short comparison.
- **Playlists and channels** are not videos. A `watch?v=…&list=…` link summarizes
  just that video. For a whole playlist, ask for the video links.
- **Warnings** about truncated or missing Gemini clips mean the summary covers only
  part of the video. Say so in the caveat line.
- The full transcript tool is the sister skill `youtube-sub`, which also handles
  timestamps, `.srt` files, translation and transcript clean-up.
