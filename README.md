# youtube-skills

Two [Claude skills](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview) for YouTube:

| Skill | What it does | Say something like |
|---|---|---|
| **`youtube-sum`** | Summarizes any video into numbered key points that keep the names, numbers, examples and reasoning. Any length (multi-hour videos are summarized section by section) and any output language. | *"Summarize https://youtu.be/… in 5 points"* · *"TL;DR of this video in Vietnamese"* · *"Detailed notes on this lecture, with timestamps"* |
| **`youtube-sub`** | Fetches the **full transcript**: clean text, timestamped text, an `.srt` subtitle file, or JSON. Can translate it, and can repair punctuation and mis-heard words in auto-captions. | *"Get me the transcript of …"* · *"English .srt for this video"* · *"Transcript with timestamps, then clean it up"* |

The transcript logic is ported from [YouGist](https://yougist.kuberscan.com)
(`yousumchrome`), the Chrome extension, and works in every case a video can be in:

```
                ┌────────────────────────────────────────────────────────────────┐
 YouTube URL ──►│ 1. YouTube captions (local, youtube-transcript-api)            │──► transcript
 (watch, Shorts,│    human-made in the spoken language → auto-generated →        │
  live, embed,  │    other tracks; every track tried until one works             │
  youtu.be, id) └──────────────┬─────────────────────────────────────────────────┘
                               │ YouTube blocks this IP / network error / library missing
                ┌──────────────▼─────────────────────────────────────────────────┐
                │ 2. Hosted YouGist backend (same library, different IP)         │──► transcript
                └──────────────┬─────────────────────────────────────────────────┘
                               │ video has NO captions at all, or 1 and 2 failed
                ┌──────────────▼─────────────────────────────────────────────────┐
                │ 3. Gemini audio transcription (needs GEMINI_API_KEY)           │──► transcript
                │    long videos done in 30-min clips, with truncation checks    │
                └────────────────────────────────────────────────────────────────┘
```

The captions are then cleaned the way YouGist cleans them: `[Music]`/`[Applause]`/♪
removed, `um`/`uh`/`ừ`/`ờ` fillers stripped, real speaker labels and stutters kept,
with no lossy de-duplication. Then Claude does the summarizing (or polishing) with
YouGist's prompts, so **no OpenAI/Anthropic API key is needed**: Claude itself is
the model.

---

## Contents

- [Install in Claude Code (CLI, IDE, and the desktop app's Code tab)](#install-in-claude-code)
- [Install in the Claude desktop app (Chat / Cowork) and claude.ai](#install-in-the-claude-desktop-app-chat--cowork-and-claudeai)
- [Using the skills](#using-the-skills)
- [Optional: Gemini fallback for videos without captions](#optional-gemini-fallback-for-videos-without-captions)
- [Configuration](#configuration)
- [Using the script directly](#using-the-script-directly)
- [Troubleshooting](#troubleshooting)
- [How it differs from YouGist](#how-it-differs-from-yougist)
- [Repository layout and development](#repository-layout-and-development)

---

## Install in Claude Code

**Requirements:** Python 3.9 or newer (`python3 --version`). Nothing else: on first
use the script creates its own virtual environment in `~/.cache/youtube-skills/venv`
and installs `youtube-transcript-api` there, so it works even on Homebrew and
Debian Pythons that block global `pip install`.

This also covers the **Code** tab of the Claude desktop app, which runs Claude
Code and reads the same `~/.claude/skills` folder and plugins.

### Option A: as a plugin (recommended, easy to update)

In Claude Code:

```
/plugin marketplace add enderphan94/youtube-skills
/plugin install youtube-skills@youtube-skills
```

Or from a terminal:

```bash
claude plugin marketplace add enderphan94/youtube-skills
```

```bash
claude plugin install youtube-skills@youtube-skills
```

Restart Claude Code (or start a new session). Update later with
`claude plugin update youtube-skills@youtube-skills`. Plugin skills are namespaced,
so their slash commands are `/youtube-skills:youtube-sum` and `/youtube-skills:youtube-sub`.

### Option B: copy the skills into `~/.claude/skills`

```bash
git clone https://github.com/enderphan94/youtube-skills.git ~/youtube-skills
```

```bash
mkdir -p ~/.claude/skills && ln -s ~/youtube-skills/skills/youtube-sum ~/youtube-skills/skills/youtube-sub ~/.claude/skills/
```

The symlinks mean a `git pull` in `~/youtube-skills` updates them. Use `cp -R`
instead of `ln -s` if you prefer copies. To share the skills with everyone working
on one project, put them in that project's `.claude/skills/` folder instead.

Slash commands are `/youtube-sum` and `/youtube-sub`.

### Check it worked

Start a new session and ask *"What skills are available?"*, or just paste a
YouTube link and ask for a summary. Claude loads the skill on its own when the
request matches.

---

## Install in the Claude desktop app (Chat / Cowork) and claude.ai

Chat and Cowork in the desktop app, and claude.ai in the browser, run skills
inside Anthropic's code-execution sandbox. Skills are uploaded once to your
account as zip files.

**1. Build the two zip files** (one per skill, each with the skill folder at the root):

```bash
git clone https://github.com/enderphan94/youtube-skills.git && cd youtube-skills && ./tools/build.sh
```

This produces `dist/youtube-sum.zip` and `dist/youtube-sub.zip`. Without a shell,
zip each folder under `skills/` yourself. The zip must contain
`youtube-sum/SKILL.md`, not a bare `SKILL.md`. GitHub's green "Download ZIP"
button gives the whole repo, which is the wrong shape.

**2. Turn on code execution.** Open **Settings → Capabilities** and enable
**Code execution and file creation**. Skills need it.

**3. Allow network access.** The skill has to reach YouTube. In the same
Capabilities section, set network egress to allow **all domains**, or add these
to the allowed domains:

| Domain | Needed for |
|---|---|
| `www.youtube.com`, `youtube.com` | captions, title, duration |
| `pypi.org`, `files.pythonhosted.org` | the one-time install of `youtube-transcript-api` (usually allowed by default as a package manager) |
| `yougist.kuberscan.com` | the hosted-backend fallback, which matters here (see below) |
| `generativelanguage.googleapis.com` | the Gemini fallback (optional) |

On Team and Enterprise plans an owner or admin may have to enable code
execution, network access and skills for the organization first.

**4. Upload the skills.** Go to **Settings → Capabilities → Skills**, click
**Upload skill**, and choose `dist/youtube-sum.zip`. Repeat for
`dist/youtube-sub.zip`. Both appear in the list with a toggle; make sure they
are on.

**5. Use them.** In a new chat, paste a YouTube link and ask for a summary or a
transcript. Transcript files are saved to the chat's outputs, so you can
download them.

> **Why the hosted backend matters in the sandbox.** YouTube often blocks
> requests from cloud IP ranges, and the sandbox runs on one. When that happens
> the skill fetches the captions through the YouGist backend instead, which
> receives only the video URL. If your organization doesn't allow that domain,
> a proxy (`YT_PROXY_URL`) or Gemini is the alternative, but the sandbox has no
> place to keep environment variables between chats, so those two fallbacks
> are mainly practical in Claude Code.

Menu names here match the app at the time of writing; Anthropic occasionally
moves them. If something has moved, search the Settings page for "Skills" or
"Code execution".

---

## Using the skills

Just talk to Claude. The skills trigger on their own when a request involves a
YouTube link. You can also invoke them explicitly with the slash commands above.

### `youtube-sum`: summaries

| You ask for | You get |
|---|---|
| *"Summarize this: https://youtu.be/…"* | 10 points, standard depth (2 to 4 sentences each), in your language |
| *"3 key takeaways"*, *"top 7 points"* | that many points (1 to 20) |
| *"TL;DR"*, *"quick summary"* | **brief**: one sentence per point |
| *"detailed notes"*, *"in depth"* | **detailed**: 4 to 7 sentences per point, with the speaker's examples, numbers and reasoning |
| *"…in Vietnamese"*, *"…auf Deutsch"* | that output language, whatever language the video is in |
| *"…with timestamps"* | each point starts with `[mm:ss]` |

Output looks like:

```
**How to stay calm when you know you'll be stressed | Daniel Levitin | TED** · TED · 12:19
_Transcript: YouTube captions, human-made (English)._

1. …
2. …

Full transcript saved at /tmp/youtube-sum/how-to-stay-calm-…-8jPQjjsBbIc.txt; ask me anything about it.
```

Follow-up questions (*"what did he say about passports?"*) are answered from the
saved transcript, not from memory.

Long videos: up to about 120,000 characters (roughly 2 to 3 hours of speech) are
read whole. Anything longer is summarized section by section, with parallel
subagents where available, and the section notes are merged, so the end of a
4-hour podcast is covered as well as the start.

### `youtube-sub`: transcripts

| You ask for | Format |
|---|---|
| *"transcript of …"* | `text`: clean paragraphs with a Title/Channel/URL/Duration/Source header |
| *"…with timestamps"* | `timestamped`: `[mm:ss] …` lines about every 30 s (finer on request) |
| *"subtitles"*, *".srt"* | `srt`: real cue timings, named like `title-ID.en.srt` |
| *"as JSON"* | `json`: segments with start/duration, plus the full text |
| *"the Spanish captions"* | picks that caption track (`--lang es`) |
| *"English transcript"* of a non-English video | a real English track if one exists, otherwise YouTube's machine translation (`--translate en`). YouTube rate-limits translation heavily. When it refuses, you get the original-language transcript with a note, and Claude offers to translate it |
| *"which subtitle languages does it have?"* | lists the tracks |
| *"…and clean it up"* | **polish**: Claude fixes punctuation and mis-heard words section by section, and a checker rejects any section that was paraphrased, summarized or drifted into another language (the original text is kept for those) |

Files go to the folder you name, otherwise the current folder (Claude Code) or
the chat's downloadable outputs (desktop and claude.ai).

---

## Optional: Gemini fallback for videos without captions

Some videos have no captions at all: captions disabled, music-heavy content,
very new uploads. For those the skills can have Google Gemini listen to the
video and transcribe the audio. YouTube URLs are processed by Google directly,
and nothing is downloaded on your side.

1. Get a free API key at <https://aistudio.google.com/apikey>.
2. Make it visible to Claude Code, either in your shell profile:
   ```bash
   echo 'export GEMINI_API_KEY="your-key"' >> ~/.zshrc
   ```
   or in `~/.claude/settings.json`:
   ```json
   { "env": { "GEMINI_API_KEY": "your-key" } }
   ```
3. Restart Claude Code.

Never paste API keys into the chat. Gemini works with public videos only. Videos
longer than 30 minutes are transcribed in 30-minute clips, three at a time,
because Gemini is known to silently truncate or invent passages on single
requests over about an hour. Any clip that comes back suspiciously short, or
stops early, is reported as a warning rather than passed off as complete.

---

## Configuration

All optional, set as environment variables:

| Variable | Default | Effect |
|---|---|---|
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | none | Enables the Gemini audio fallback |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model used for transcription |
| `YOUGIST_BACKEND` | `https://yougist.kuberscan.com` | Hosted fallback backend. Set to `off` to never use it, or point it at your own deployment of YouGist's `main.py` |
| `YT_PROXY_URL` | none | `http://user:pass@host:port` proxy for YouTube requests. This is the standard fix when YouTube blocks your IP |
| `YT_SKILLS_VENV` | `~/.cache/youtube-skills/venv` | Where the private Python environment lives |
| `YT_SKILLS_NO_BOOTSTRAP` | unset | Set to `1` to never create a venv or pip-install (captions then need `youtube-transcript-api` installed yourself) |

---

## Using the script directly

The fetcher is a normal CLI, useful on its own or in scripts:

```bash
python3 skills/youtube-sub/scripts/fetch_transcript.py "https://youtu.be/8jPQjjsBbIc" --out-dir .
```

```text
usage: fetch_transcript.py VIDEO [--lang en,vi] [--translate LANG]
                           [--format text|timestamped|srt|json] [--group-seconds N]
                           [--out FILE | --out-dir DIR] [--no-header]
                           [--chunk-chars N] [--chunk-dir DIR]
                           [--source auto|captions|backend|gemini] [--no-backend] [--no-gemini]
                           [--gemini-clip-minutes N] [--list-tracks] [--quiet]
```

With `--out`/`--out-dir` the transcript is written to a file and a JSON report
is printed. Without them the transcript goes to stdout and the report to stderr.

```json
{
  "ok": true,
  "video_id": "8jPQjjsBbIc",
  "title": "How to stay calm when you know you'll be stressed | Daniel Levitin | TED",
  "channel": "TED",
  "duration": "12:19",
  "source": "captions",
  "source_detail": "YouTube captions (human-made, English)",
  "available_tracks": "ar, bg, …, en, …, vi, en (auto)",
  "format": "text",
  "words": 2232,
  "out": "./how-to-stay-calm-when-you-know-you-ll-be-stressed-daniel-lev-8jPQjjsBbIc.txt",
  "attempts": [{ "source": "captions", "ok": true }]
}
```

Exit codes: `0` ok · `2` not a YouTube video URL · `3` video unavailable · `4` no
transcript from any source. On failure the report includes `attempts` (what was
tried and why each failed) and `hints` (what to do about it).

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `kind: "blocked"`, "YouTube is blocking requests from your IP" | Common on cloud servers, VPNs and the claude.ai sandbox. The skill tries the hosted backend next. For a lasting fix, set `YT_PROXY_URL` to a residential proxy, or set `GEMINI_API_KEY` |
| `kind: "no_captions"` | The video has no captions. Set `GEMINI_API_KEY` to transcribe its audio |
| Transcript is in the wrong language | Ask for a language (*"the English captions"*). The report's `available_tracks` shows what exists |
| Auto-captions have no punctuation or wrong words | Normal for YouTube auto-captions, especially Vietnamese. Ask Claude to *"clean up the transcript"* (youtube-sub polish). Summaries already correct obvious errors from context |
| `could not set up the private Python environment` | Install the library yourself: `python3 -m pip install 'youtube-transcript-api>=1.2'` (in a venv if your Python refuses global installs) |
| `CERTIFICATE_VERIFY_FAILED` on macOS | A python.org-installer Python issue. Run `/Applications/Python\ 3.x/Install\ Certificates.command`, or `pip install certifi` |
| Gemini `404 model not found` | Set `GEMINI_MODEL` to a current Gemini model |
| `age_restricted` | YouTube serves captions only to signed-in users for these videos. Gemini may still work |
| Skill doesn't trigger | Check `~/.claude/skills/youtube-sum/SKILL.md` exists (manual install) or `/plugin` lists it. Start a new session. Mention "YouTube" and paste the link |

---

## How it differs from YouGist

The same transcript pipeline and prompts, adapted to run as skills:

- **The LLM is Claude itself.** No provider settings or API keys; YouGist's
  prompts (depth levels, section notes, merge, batching of detailed points)
  are instructions in the skill.
- **Spoken-language-first track choice.** YouGist takes the first human-made
  track in YouTube's listing, which is sorted by language name. For a TED talk
  with 40 caption tracks that is the **Arabic** translation of an English
  talk. The skills use the auto-generated track to detect the spoken language
  and prefer tracks in it.
- **Gemini is a real fallback.** YouGist only used it in its evaluation harness.
  Here it runs automatically for caption-less videos, in clips, with truncation
  warnings.
- **Timestamps, SRT, JSON, translation and track listing**, none of which the
  extension exposes.
- **Polish verification ported as a script** (`polish_check.py`): the same
  language-drift, length and 30% edit-rate gates as the extension's
  `polishLooksSane`.
- More URL shapes: Shorts, live, embed, music, nocookie.

---

## Repository layout and development

```
youtube-skills/
├── .claude-plugin/
│   ├── plugin.json            # Claude Code plugin manifest
│   └── marketplace.json       # lets `/plugin marketplace add enderphan94/youtube-skills` work
├── skills/
│   ├── youtube-sum/
│   │   ├── SKILL.md           # summary workflow + YouGist writing rules
│   │   ├── references/summary-guide.md   # full prompts: single pass, section notes, merge, depth
│   │   └── scripts/fetch_transcript.py   # copy of the shared fetcher (kept in sync)
│   └── youtube-sub/
│       ├── SKILL.md           # transcript workflow, formats, failure handling, polish
│       ├── references/polish-guide.md    # transcript repair rules (incl. Southern Vietnamese)
│       └── scripts/
│           ├── fetch_transcript.py       # canonical shared fetcher (stdlib + youtube-transcript-api)
│           └── polish_check.py           # verifies polished sections, falls back to the original
├── tests/test_skills.py       # 46 offline unit tests
└── tools/build.sh             # sync shared script + build dist/*.zip for claude.ai upload
```

Each skill folder is self-contained (claude.ai uploads them separately), so
`fetch_transcript.py` exists twice. Edit the copy in `youtube-sub`, then run
`./tools/build.sh` to sync it. The tests fail if the copies differ.

```bash
python3 -m unittest discover -s tests -v
```

```bash
./tools/build.sh
```

---

Transcripts come from YouTube's own caption tracks, or from Gemini, and are
intended for personal use: reading, studying, accessibility, notes. Respect
creators' rights when sharing them.

Questions or ideas: open an issue, or email <enderlocphan@gmail.com>.
