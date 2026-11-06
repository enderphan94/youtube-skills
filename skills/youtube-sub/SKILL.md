---
name: youtube-sub
description: Get the full transcript (captions / subtitles) of any YouTube video as clean text, timestamped text, an .srt subtitle file or JSON. Use whenever the user wants a YouTube video's transcript, captions, subtitles, "what exactly did they say", an exact quote from a video, or an .srt file, given a YouTube link (watch, youtu.be, Shorts, live, embed, music) or a video id. Works with human-made or auto-generated captions in any language, can have YouTube translate them, and falls back to Gemini audio transcription when a video has no captions at all. On request it also repairs punctuation and mis-heard words in an auto-caption transcript.
---

# YouTube transcript (`youtube-sub`)

Fetches the complete transcript of a YouTube video and saves it as a file.

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
| timestamps | `--format timestamped`: one `[mm:ss]` line per ~30 s. Add `--group-seconds 10` for finer lines, or `0` for one line per caption cue |
| subtitles / an `.srt` file | `--format srt` |
| structured data | `--format json` (segments with start/duration, plus the full text) |
| the transcript **in** a given language ("English subtitles", "in Vietnamese") | `--translate en`. Uses a real `en` track when one exists, and YouTube's machine translation only when none does. The report's `translated_to` says which happened |
| to prefer a language but keep the original if it's missing | `--lang en` (comma-separated, best first: `--lang vi,en`) |
| which languages exist | `--list-tracks` (prints the tracks and exits) |
| text only, no header | `--no-header` |

Without `--lang` or `--translate`, you get the language actually spoken in the video.

The first run takes about 10 seconds because the script creates its own Python
environment in `~/.cache/youtube-skills/venv`. Do not pip-install anything yourself
unless the report says that step failed.

The script prints a JSON report. The fields that matter: `ok`, `out`, `title`,
`duration`, `source_detail`, `words`, `available_tracks`, `warnings`, `attempts`
and `hints`.

### How the transcript is obtained (automatic)

1. **YouTube captions**, fetched locally. Human-made tracks in the spoken language
   come first, then the auto-generated track, then other tracks (usually
   translations). Each track is tried in turn until one works.
2. **Hosted YouGist backend**: same library, different IP. Used only when YouTube
   blocks this machine or the network fails. It returns no timestamps.
3. **Gemini audio transcription**: used when the video has no captions at all.
   Needs `GEMINI_API_KEY` in the environment. Long videos are done in 30-minute clips.

## 2. Tell the user

- The saved file's full path. In Claude Code, a Markdown link to the file is
  clickable, e.g. `[talk.en.srt](/full/path/talk.en.srt)`.
- Title, duration, word count, and where the text came from (`source_detail`). Say
  it plainly when the text is **auto-generated** (expect mis-heard words and weak
  punctuation, and mention that you can polish it, see section 4), **Gemini**
  (an AI transcription of the audio), or **machine-translated**.
- Every entry in `warnings`, in plain words, especially possible truncation.
- **Short text transcripts** (up to about 1,500 words): you may paste the full text
  in the reply when the user wants to read it here. **Longer ones**: quote the first
  sentence or two as a preview and point to the file. Do not paste a long
  transcript into the chat; the file is the deliverable.
- **SRT and JSON files**: never paste them. Give the path, plus the cue count
  (`cues` in the report) for SRT.
- Files are named `<title-slug>-<video id>` plus `.txt`, `.timestamped.txt`,
  `.<lang>.srt` or `.json`. Use `--out <file>` when the user wants a specific name.
- If the track language is not what the user wanted, run again with `--lang`.
  `available_tracks` lists what exists, and auto-generated tracks are marked `(auto)`.
- If the report has `translation_failed: true`, YouTube refused the machine
  translation (it rate-limits that endpoint hard) and the file holds the
  original-language transcript. Say so, and offer to translate it yourself. If
  the user accepts, fetch with `--chunk-chars 8000` and translate part by part
  into a new file, keeping the meaning exact and adding nothing.

## 3. When it fails (`ok: false`)

Read `attempts` and `hints`, then tell the user in one or two sentences what
failed and the concrete fix:

| `kind` | Meaning | What to tell the user |
|---|---|---|
| `no_captions` | The video has no captions | Gemini can transcribe the audio. The user sets `GEMINI_API_KEY` (free key at https://aistudio.google.com/apikey) in their environment, then you run the command again |
| `blocked`, `network` | YouTube blocks or cannot be reached from this machine (common on cloud, VPN and sandbox IPs) | Set `YT_PROXY_URL`, or set `GEMINI_API_KEY`. In the Claude desktop or web app, allow network access to `youtube.com` (Settings > Capabilities) |
| `unavailable` | Private, deleted, or not a real video | Nothing can fetch it. Ask for another link |
| `age_restricted` | YouTube serves captions to signed-in users only | Gemini may still work |
| `missing_dep` | The caption library could not be installed | `python3 -m pip install 'youtube-transcript-api>=1.2'` |

Never ask the user to paste an API key into the chat. Keys go in their shell
profile (`export GEMINI_API_KEY=...`), or in Claude Code's `~/.claude/settings.json`
under `"env"`.

## 4. Optional: polish the transcript

Do this only when the user asks: "clean it up", "add punctuation", "fix the
mistakes", "make it readable". Auto-captions are often one unpunctuated stream
with mis-heard words. Polishing repairs that without rewriting the talk.

1. Fetch the plain text in 8,000-character parts:
   ```bash
   python3 "SKILL_DIR/scripts/fetch_transcript.py" "<url>" --out-dir "<dir>" --chunk-chars 8000
   ```
   The report's `parts` lists `…/<name>.parts/part-01.txt`, `part-02.txt`, …
2. Polishing rewrites every word of the transcript. Above about 15,000 words, tell
   the user how many parts there are and confirm before you start.
3. For each part, write `<name>.parts/polished/part-NN.txt`, following
   [references/polish-guide.md](references/polish-guide.md) exactly. Pass the
   video title along as context. With many parts and an Agent/Task tool
   available, hand out parts to subagents (up to 4 at a time). Give each one the
   guide's rules, the title, the input path and the output path.
4. Verify and assemble. Any section that drifted (wrong language, summarized,
   paraphrased) falls back to the original text:
   ```bash
   python3 "SKILL_DIR/scripts/polish_check.py" \
     --original-dir "<name>.parts" --polished-dir "<name>.parts/polished" \
     --out "<dir>/<name>.polished.txt" --header-from "<the transcript file>"
   ```
5. Report the polished file's path and how many sections were accepted. Any
   section that `kept_original` keeps the raw caption text; say so.

## Notes

- Playlist and channel links are not videos. A `watch?v=…&list=…` link fetches
  just that video. For a whole playlist, ask the user for the video links and
  process them one by one.
- Live streams have captions only once the stream has ended and YouTube has
  processed it.
- Environment switches: `YOUGIST_BACKEND=off` disables the hosted backend,
  `GEMINI_MODEL` overrides the model (default `gemini-2.5-flash`), and
  `YT_PROXY_URL=http://user:pass@host:port` routes YouTube requests through a proxy.
