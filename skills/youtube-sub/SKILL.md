---
name: youtube-sub
description: Get the subtitles / transcript of any YouTube video as clean text, timestamped text, an .srt subtitle file or JSON. Use whenever the user shares a YouTube link (watch, youtu.be, Shorts, live, embed, music) or video id and wants its subtitles, captions, transcript, an .srt file or an exact quote, and also whenever they ask to summarize the video, take notes on it, translate it or answer questions about it, because all of that starts from the transcript this skill fetches. Uses the video's own human-made subtitles when it has them. When it only has YouTube's auto-generated captions, Claude corrects the mis-heard words and punctuation itself. When the video has no captions at all, it transcribes the audio on this machine with Whisper. Also transcribes a local audio or video file (mp4, mp3, m4a, wav, webm) the user gives or uploads. Works in any language.
---

# YouTube subtitles (`youtube-sub`)

Fetches a video's subtitles and saves them as a file. Human-made subtitles are
used as they are. Auto-generated captions are corrected by you (section 2).

In the commands below, `SKILL_DIR` means this skill's folder. Claude Code shows
it as "Base directory for this skill". On Claude.ai or in the desktop chat it is
usually `/mnt/skills/user/youtube-sub`. Use `python` instead of `python3` on Windows.

## 1. Fetch

```bash
python3 "SKILL_DIR/scripts/fetch_transcript.py" "<youtube url or id>" --out-dir "<dir>" [flags]
```

Pick `<dir>` like this:
- The user named a folder: use it. For an exact file name, use `--out <file>` instead.
- `/mnt/user-data/outputs` exists (Claude.ai / desktop chat sandbox): use it, so the user can download the file.
- Otherwise: the current working directory, `.`.

Map the request to flags:

| The user wants | Flags |
|---|---|
| the transcript (default) | `--format text`: clean paragraphs with a Title/URL/Source header |
| timestamps | `--format timestamped`: one `[mm:ss]` line per ~30 s. Add `--group-seconds 10` for finer lines |
| subtitles / an `.srt` file | `--format srt` |
| structured data | `--format json` (segments with start/duration, plus the full text) |
| the subtitles **in** a given language ("English subtitles", "in Vietnamese") | `--translate en`. Uses a real `en` track when one exists, and YouTube's machine translation only when none does |
| to prefer a language but keep the original if it's missing | `--lang en` (comma-separated, best first: `--lang vi,en`) |
| which languages exist | `--list-tracks` (prints the tracks and exits) |
| the auto-captions exactly as YouTube made them | `--raw` |
| text only, no header | `--no-header` |

Without `--lang` or `--translate`, you get the language actually spoken in the video.

The first run takes about 10 seconds because the script creates its own Python
environment in `~/.cache/youtube-skills/venv`. Do not pip-install anything yourself
unless the report says that step failed.

The script prints a JSON report. The fields that matter: `ok`, `out`, `title`,
`duration`, `source_detail`, `words`, `needs_correction`, `parts_dir`, `parts`,
`available_tracks`, `warnings`, `attempts` and `hints`.

Where the text comes from, decided by the script:

1. **Human-made subtitles** in the spoken language, when the video has them. Final as fetched.
2. **Auto-generated captions** otherwise. The report says `needs_correction: true`. Go to section 2.
3. **Hosted YouGist backend**: only when YouTube blocks this machine. No timestamps.
4. **Gemini audio transcription**: only when the video has no captions at all and
   `GEMINI_API_KEY` is set. Already punctuated, so no correction.
5. **Local Whisper transcription of the audio**: never automatic. You start it
   (section 4) when the video has no captions, or when the user gives a file.
   The report says `needs_correction: true`, so section 2 applies.

## 2. Correct auto-generated captions (`needs_correction: true`)

Auto-captions are an unpunctuated stream with mis-heard words. Correct them
before you hand over the file. Skip this section when the report has no
`needs_correction`, or when the user only wants a summary or an answer (section 6).

1. The report's `parts` lists the files to correct: `<parts_dir>/part-01.txt`,
   `part-02.txt`, … (about 8,000 characters each).
2. **More than 15,000 words** (`words` in the report): correcting rewrites every
   word, so first give the user the raw file, say how many parts there are, and
   ask whether to correct it. Otherwise just start.
3. For each part, write the corrected text to `<parts_dir>/corrected/part-NN.txt`
   (same file name), following
   [references/correction-guide.md](references/correction-guide.md) exactly. Use
   the video title as context. With more than 3 parts and an Agent/Task tool
   available, hand the parts to subagents (up to 4 at a time). Give each one the
   guide's path, the title, the input path and the output path.
4. Verify and write the result into the transcript file:
   ```bash
   python3 "SKILL_DIR/scripts/apply_corrections.py" --parts-dir "<parts_dir>" --out "<out>"
   ```
   It rewrites `<out>` in place. A part that was paraphrased, summarized, drifted
   into another language or lost a timestamp is rejected and keeps its original
   caption text. For `.srt` files only the text changes, never the timing.
5. Read the result: `accepted` of `parts`. If `kept_original` lists a part, fix
   that part once more and run the command again. Tell the user about any part
   that still keeps the raw caption text.

## 3. Tell the user

- The saved file's full path. In Claude Code, a Markdown link to the file is
  clickable, e.g. `[talk.en.srt](/full/path/talk.en.srt)`.
- Title, duration, word count, and where the text came from in plain words:
  the video's own subtitles; auto-generated captions that you corrected (names
  and rare terms may still be wrong); Gemini's AI transcription of the audio;
  or YouTube's machine translation.
- Every entry in `warnings`, in plain words, especially possible truncation.
- **Short text transcripts** (up to about 1,500 words): you may paste the full text
  in the reply when the user wants to read it here. **Longer ones**: quote the first
  sentence or two as a preview and point to the file. The file is the deliverable.
- **SRT and JSON files**: never paste them. Give the path, plus the cue count
  (`cues` in the report) for SRT.
- If the track language is not what the user wanted, run again with `--lang`.
  `available_tracks` lists what exists, and auto-generated tracks are marked `(auto)`.
- If the report has `translation_failed: true`, YouTube refused the machine
  translation (it rate-limits that endpoint hard) and the file holds the
  original-language transcript. Say so, and offer to translate it yourself into
  a new file, keeping the meaning exact and adding nothing.

## 4. No captions: transcribe the audio

When the fetch fails with `no_captions` (or `age_restricted`), or the user gives
a local audio or video file, transcribe the audio on this machine. It needs no
API key and nothing is uploaded anywhere.

```bash
python3 "SKILL_DIR/scripts/fetch_transcript.py" "<youtube url>" --source whisper --out-dir "<dir>" [--format …]
```

```bash
python3 "SKILL_DIR/scripts/fetch_transcript.py" "<path to the audio or video file>" --out-dir "<dir>" [--format …]
```

- Before you start, tell the user in one sentence that the video has no captions,
  so you are transcribing its audio locally, and that this takes a few minutes.
  The very first run also downloads a speech model (0.5 to 1.6 GB). Do not ask
  for permission; just say it and start.
- It is slow: roughly 1 minute per 10 minutes of audio on a fast laptop, far
  longer on a small machine. Run the command in the background, or with the
  longest timeout you have, and do not start it twice.
- All formats work, including real `.srt` timing. Add `--lang zh` (or any code)
  when the user told you the spoken language; otherwise it is detected.
- The result is machine transcription, so the report says `needs_correction: true`.
  Do section 2. Also delete lines Whisper invented over music or silence, such as
  credits, "thanks for watching" or "subscribe" lines that nobody says: in a
  marker part, leave that line's text empty after its marker.
- If the audio download is `blocked` or fails with `network` (normal in the
  Claude.ai / desktop chat sandbox, which cannot reach YouTube), ask the user to
  download the video or its audio themselves and attach the file here, then run
  the second command on that file. If `GEMINI_API_KEY` is set, the normal fetch
  has already tried Gemini, which needs only the URL.

## 5. When it fails (`ok: false`)

Read `attempts` and `hints`, then tell the user in one or two sentences what
failed and the concrete fix:

| `kind` | Meaning | What to do |
|---|---|---|
| `no_captions` | The video has no captions at all | Section 4: transcribe the audio |
| `blocked`, `network` | YouTube blocks or cannot be reached from this machine (common on cloud, VPN and sandbox IPs) | Tell the user: set `YT_PROXY_URL`, or attach the video file (section 4). In the Claude desktop or web app, allow network access to `youtube.com` (Settings > Capabilities) |
| `unavailable` | Private, deleted, or not a real video | Nothing can fetch it. Ask for another link |
| `age_restricted` | YouTube serves this video to signed-in users only | Ask the user to attach the video file (section 4) |
| `missing_dep` | A Python package could not be installed | Give the user the `pip install` command from the error text |

Never ask the user to paste an API key into the chat. Keys go in their shell
profile (`export GEMINI_API_KEY=...`), or in Claude Code's `~/.claude/settings.json`
under `"env"`.

## 6. Summaries, notes, questions, translation

There is no separate summary command. Once you have the transcript, do whatever
the user asks with it.

- When the user wants only a summary, notes or an answer, and not the transcript
  itself, fetch with `--raw --out-dir "${TMPDIR:-/tmp}/youtube-sub"` and skip
  section 2. Read past mis-heard words using context instead of rewriting them.
- Read the whole transcript file before you answer. For a very long one, read it
  in ranges until you reach the end; do not stop at the first part.
- Use only what the transcript says. Never fill gaps from the title, the
  description or your own knowledge of the topic. If the fetch failed, say so
  instead of guessing what the video contains.
- Answer in the language the user wrote in, whatever language the video is in.
- When names or terms come from auto-generated captions, say they may be mis-heard.

## Notes

- Playlist and channel links are not videos. A `watch?v=…&list=…` link fetches
  just that video. For a whole playlist, ask the user for the video links and
  process them one by one.
- Live streams have captions only once the stream has ended and YouTube has
  processed it.
- Environment switches: `YOUGIST_BACKEND=off` disables the hosted backend,
  `GEMINI_MODEL` overrides the model (default `gemini-2.5-flash`),
  `WHISPER_MODEL` picks the local speech model (`small`, `medium`, `large-v3-turbo`, `large-v3`), and
  `YT_PROXY_URL=http://user:pass@host:port` routes YouTube requests through a proxy.
