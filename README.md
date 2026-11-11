<p align="center">
  <img src="assets/icon.svg" width="120" height="120" alt="youtube-sub icon: a video frame with two subtitle lines, the second one checked">
</p>

# youtube-skills

A [Claude skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview)
that gets the subtitles of any YouTube video.

- **The video has subtitles:** you get them as they are.
- **The video has only auto-generated captions:** Claude fixes the mis-heard words
  and adds punctuation, then gives you the corrected version.
- **The video has no captions at all:** the audio is transcribed on your own
  computer with [Whisper](https://github.com/SYSTRAN/faster-whisper), then
  corrected by Claude the same way.

Paste a link and ask:

> Get the subtitles of https://youtu.be/8jPQjjsBbIc

No API key is needed. The Claude you are already chatting with does the correcting,
and audio is transcribed on your own computer.

## What you can ask for

| You say | You get |
|---|---|
| *"Get the transcript of …"* | A `.txt` file with clean paragraphs |
| *"…with timestamps"* | `[mm:ss]` lines, about one every 30 seconds |
| *"Give me the .srt for …"* | A subtitle file with the original timing |
| *"…as JSON"* | Every caption cue with its start time and duration |
| *"English subtitles for …"* | The English track, or YouTube's translation if there is none |
| *"Which subtitle languages does it have?"* | The list of tracks |
| *"…the raw auto-captions"* | YouTube's captions exactly as they are, uncorrected |
| *"Transcribe this file"* (an mp4, mp3, m4a, wav…) | The same outputs, from a video or audio file on your computer |

After that, keep talking to Claude: *"summarize it in 5 points"*, *"translate it to
Vietnamese"*, *"what did he say about passports?"*. Claude answers from the
transcript, so there is no separate summary command.

## Install

You need Python 3.9 or newer. Everything else installs itself on first use.

Videos without captions need a little more, only the first time one comes up: a
speech model is downloaded (1.6 GB on a computer with 8 or more CPU cores, 0.5 GB
otherwise), and YouTube downloads need [Node.js 22+](https://nodejs.org) or
[Deno](https://deno.com) installed.

### Claude Code (terminal, IDE, and the desktop app's Code tab)

```
/plugin marketplace add enderphan94/youtube-skills
/plugin install youtube-skills@youtube-skills
```

Start a new session and paste a YouTube link.

<details>
<summary>Without the plugin system</summary>

```bash
git clone https://github.com/enderphan94/youtube-skills.git ~/youtube-skills
```

```bash
mkdir -p ~/.claude/skills && ln -s ~/youtube-skills/skills/youtube-sub ~/.claude/skills/
```

</details>

### Claude desktop app (Chat) and claude.ai

1. Build the zip file:
   ```bash
   git clone https://github.com/enderphan94/youtube-skills.git && cd youtube-skills && ./tools/build.sh
   ```
2. Open **Settings → Capabilities**. Turn on **Code execution and file creation**.
3. In the same place, allow network access to these domains (or to all domains):
   `youtube.com`, `www.youtube.com`, `pypi.org`, `files.pythonhosted.org`,
   `yougist.kuberscan.com`.
4. Go to **Settings → Capabilities → Skills**, click **Upload skill**, and choose
   `dist/youtube-sub.zip`.
5. Start a new chat and paste a YouTube link.

The chat sandbox runs on a cloud server, and YouTube usually refuses to send
audio to those. So for a video with **no captions**, the link alone is not enough
there: download the video yourself, attach the file to the chat, and ask Claude
to transcribe it. For that, also allow `huggingface.co` in step 3 (the speech
model is downloaded from it), or allow all domains. In Claude Code on your own
computer the link alone works.

## How it works

```mermaid
flowchart TD
    U([A YouTube link]) --> F[fetch_transcript.py<br/>lists the video's caption tracks.<br/>If YouTube blocks this machine, it asks<br/>the hosted YouGist backend instead]
    F --> Q{What captions<br/>does the video have?}

    Q -- "human-made subtitles" --> H[Download them.<br/>Final as they are]
    Q -- "only auto-generated" --> A[Download the auto-captions]
    Q -- "none at all" --> G{GEMINI_API_KEY set?}

    G -- "yes" --> GM[Gemini fetches the video itself<br/>and transcribes the audio.<br/>No per-line timing]
    G -- "no" --> D{Can yt-dlp download<br/>the audio here?}
    D -- "yes: your own computer" --> W[faster-whisper transcribes<br/>the audio locally, with timing]
    D -- "no: chat sandbox,<br/>blocked network" --> X([You download the video<br/>and attach the file])
    L([An audio or video file]) --> W
    X --> W

    W --> S[Speech check: a voice detector maps<br/>where someone speaks. Suspicious lines and<br/>speech with no text are transcribed again]
    A --> P[Split into parts<br/>of about 8,000 characters]
    S --> P
    P --> C[Claude corrects each part:<br/>mis-heard words, punctuation, capitals.<br/>It may propose removing an invented line]
    C --> V{apply_corrections.py<br/>checks each part}
    V -- "pass" --> M[Corrected text goes into the file.<br/>For .srt, timing is never touched.<br/>A line is removed only where<br/>no voice was detected]
    V -- "fail: paraphrased, summarized,<br/>wrong language, lost a timestamp" --> K[That part keeps<br/>the original text]

    H --> O([Your file: .txt, timestamped .txt, .srt or .json])
    GM --> O
    M --> O
    K --> O
    O --> N([Ask Claude anything about it:<br/>summary, notes, translation, quotes])
```

The pieces:

| File | Job |
|---|---|
| [`skills/youtube-sub/SKILL.md`](skills/youtube-sub/SKILL.md) | Tells Claude when to use the skill and which steps to follow |
| [`scripts/fetch_transcript.py`](skills/youtube-sub/scripts/fetch_transcript.py) | Picks the best caption track, downloads and cleans it (`[Music]`, `um`, `uh` removed), or transcribes the audio when there are no captions. Writes the file and reports whether it needs correcting |
| [`references/correction-guide.md`](skills/youtube-sub/references/correction-guide.md) | The rules Claude follows when correcting: fix errors, never rewrite, summarize or translate |
| [`scripts/apply_corrections.py`](skills/youtube-sub/scripts/apply_corrections.py) | Verifies Claude's corrections and writes the ones that pass into the file |

The audio path uses two open-source tools, installed on first use: [yt-dlp](https://github.com/yt-dlp/yt-dlp) to download the audio track and [faster-whisper](https://github.com/SYSTRAN/faster-whisper) to transcribe it.

Design choices worth knowing:

- **Order of preference.** Human-made subtitles in the language actually spoken
  come first, then the auto-generated track, then other languages, and only
  then the audio.
- **Corrections are verified, not trusted.** A corrected part is thrown away if
  more than 30% of its words changed, its length changed by more than 2x, it
  switched writing system, or a timestamp or cue number moved. The original
  text is used for that part instead, so nothing is lost.
- **Subtitle timing is safe.** For `.srt` files Claude only ever sees
  `[cue number] text` lines. The timing stays in the file and is never rewritten.
- **When YouTube blocks your network** (common on cloud and VPN addresses), the
  script fetches the same captions through the hosted
  [YouGist](https://yougist.kuberscan.com) backend, which receives only the
  video URL. Set `YOUGIST_BACKEND=off` to disable this.
- **Nothing that was said should go missing.** Whisper sometimes invents a line
  over music ("thanks for watching", a subtitle credit), stamps it across a
  whole 30-second window, and skips real speech inside that window. So after
  transcribing, a voice detector (Silero VAD, which ships with faster-whisper)
  maps where someone speaks. A suspicious line (a known invented phrase, a line
  stretched over more than 12 seconds, or one with odd word timing) is
  transcribed again from where its speech starts. Speech with no text over it
  is transcribed on its own. The detector only triggers a second look and never
  cuts audio, because it misses singing and speech over music.
- **Claude cannot delete speech.** While correcting, Claude can only propose
  removing a line it thinks was invented. The script removes it only where no
  voice was detected, and otherwise puts the text back and says so. Every
  removed or recovered line, and every stretch where a voice was heard but no
  words were recognized, is listed in the report and passed on to you.
- **Audio transcription is local and never automatic.** It is slow (about a
  minute per 10 minutes of audio on a fast laptop) and downloads a large model
  once, so Claude starts it only after the normal fetch reports that the video
  has no captions. No API key is involved and the audio is deleted afterwards.
  No ffmpeg is needed either.
- **Gemini is an optional alternative for caption-less videos.** With
  `GEMINI_API_KEY` set, Google fetches and transcribes the video itself, so it
  works from the link alone even where YouTube is blocked. Its output has no
  per-line timing, so it cannot produce a real `.srt`.
- **Machine-translated and Gemini transcripts are not corrected.** Their errors
  are not mis-hearings, so the correction rules do not apply to them.

## Optional settings

All are environment variables. None is required.

| Variable | What it does |
|---|---|
| `WHISPER_MODEL` | Local speech model: `small`, `medium`, `large-v3-turbo` or `large-v3`. Bigger is more accurate and slower. Default: `large-v3-turbo` with 8 or more CPU cores, otherwise `small` |
| `WHISPER_DEVICE` | `cuda` to use an NVIDIA GPU (default `cpu`) |
| `GEMINI_API_KEY` | Lets Gemini transcribe caption-less videos instead of the local model. Free key at <https://aistudio.google.com/apikey>. Put it in your shell profile, never in the chat |
| `GEMINI_MODEL` | Gemini model to use (default `gemini-2.5-flash`) |
| `YT_PROXY_URL` | `http://user:pass@host:port` proxy for YouTube requests, for when YouTube blocks your IP |
| `YOUGIST_BACKEND` | Hosted fallback URL, or `off` to never use it |
| `YT_SKILLS_VENV` | Where the script keeps its private Python environment (default `~/.cache/youtube-skills/venv`) |
| `YT_SKILLS_NO_BOOTSTRAP` | Set to `1` to stop the script creating that environment. You then install `youtube-transcript-api` yourself |

## Troubleshooting

| Problem | Fix |
|---|---|
| "YouTube is blocking requests from your IP" | Wait and retry, set `YT_PROXY_URL`, or set `GEMINI_API_KEY` |
| The audio download fails with "Sign in to confirm you're not a bot" | YouTube is blocking this network. Download the video yourself and give Claude the file, or set `YT_PROXY_URL` |
| The audio download fails for another reason | Install Node.js 22+ or Deno, which YouTube downloads need |
| Part of the speech is missing from a transcribed video | Tell Claude roughly when ("there is dialogue from 0:20"). It will transcribe that stretch again |
| Audio transcription is too slow or inaccurate | Set `WHISPER_MODEL` to a smaller (`small`) or larger (`large-v3`) model |
| Wrong language | Ask for the one you want: *"the English subtitles"* |
| "could not set up the private Python environment" | `python3 -m pip install 'youtube-transcript-api>=1.2'` |
| `CERTIFICATE_VERIFY_FAILED` on macOS | Run `/Applications/Python\ 3.x/Install\ Certificates.command` |
| The skill does not start | Start a new session, mention "YouTube" and paste the link |

## Development

```bash
python3 -m unittest discover -s tests
```

```bash
./tools/build.sh
```

The fetcher also runs on its own:

```bash
python3 skills/youtube-sub/scripts/fetch_transcript.py "https://youtu.be/8jPQjjsBbIc" --out-dir .
```

---

Transcripts come from YouTube's own caption tracks, or from speech recognition, and are meant
for personal use: reading, studying, accessibility, notes. Respect creators'
rights when sharing them.

Questions or ideas: open an issue, or email <enderlocphan@gmail.com>.
