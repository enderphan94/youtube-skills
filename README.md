# youtube-skills

A [Claude skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview)
that gets the subtitles of any YouTube video.

- **The video has subtitles:** you get them as they are.
- **The video has only auto-generated captions:** Claude fixes the mis-heard words
  and adds punctuation, then gives you the corrected version.

Paste a link and ask:

> Get the subtitles of https://youtu.be/8jPQjjsBbIc

No API key is needed. The Claude you are already chatting with does the correcting.

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

After that, keep talking to Claude: *"summarize it in 5 points"*, *"translate it to
Vietnamese"*, *"what did he say about passports?"*. Claude answers from the
transcript, so there is no separate summary command.

## Install

You need Python 3.9 or newer. Everything else installs itself on first use.

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

## How it works

```mermaid
flowchart TD
    U([You: a YouTube link]) --> F[fetch_transcript.py<br/>lists the video's caption tracks]
    F --> Q{Human-made subtitles<br/>in the spoken language?}

    Q -- yes --> H[Download them]
    Q -- "no, only auto-generated" --> A[Download the auto-captions<br/>and split them into parts<br/>of about 8,000 characters]
    Q -- "no captions at all" --> G[Gemini transcribes the audio<br/>optional, needs GEMINI_API_KEY]

    A --> C[Claude corrects each part:<br/>mis-heard words, punctuation,<br/>capitals, paragraphs]
    C --> V{apply_corrections.py<br/>checks each part}
    V -- "pass" --> M[Corrected text goes into the file.<br/>For .srt, timing is never touched]
    V -- "fail: paraphrased, summarized,<br/>wrong language, lost a timestamp" --> K[That part keeps the<br/>original caption text]

    H --> O([Your file: .txt, timestamped .txt, .srt or .json])
    G --> O
    M --> O
    K --> O
    O --> N([Ask Claude anything about it:<br/>summary, notes, translation, quotes])
```

The pieces:

| File | Job |
|---|---|
| [`skills/youtube-sub/SKILL.md`](skills/youtube-sub/SKILL.md) | Tells Claude when to use the skill and which steps to follow |
| [`scripts/fetch_transcript.py`](skills/youtube-sub/scripts/fetch_transcript.py) | Picks the best caption track, downloads and cleans it (`[Music]`, `um`, `uh` removed), writes the file, and reports whether it needs correcting |
| [`references/correction-guide.md`](skills/youtube-sub/references/correction-guide.md) | The rules Claude follows when correcting: fix errors, never rewrite, summarize or translate |
| [`scripts/apply_corrections.py`](skills/youtube-sub/scripts/apply_corrections.py) | Verifies Claude's corrections and writes the ones that pass into the file |

Design choices worth knowing:

- **Track choice.** Human-made subtitles in the language actually spoken come
  first, then the auto-generated track, then other languages.
- **Corrections are verified, not trusted.** A corrected part is thrown away if
  more than 30% of its words changed, its length changed by more than 2x, it
  switched writing system, or a timestamp or cue number moved. The original
  caption text is used for that part instead, so nothing is lost.
- **Subtitle timing is safe.** For `.srt` files Claude only ever sees
  `[cue number] text` lines. The timing stays in the file and is never rewritten.
- **When YouTube blocks your network** (common on cloud and VPN addresses), the
  script fetches the same captions through the hosted
  [YouGist](https://yougist.kuberscan.com) backend, which receives only the
  video URL. Set `YOUGIST_BACKEND=off` to disable this.
- **Machine-translated and Gemini transcripts are not corrected.** Their errors
  are not mis-hearings, so the correction rules do not apply to them.

## Optional settings

All are environment variables. None is required.

| Variable | What it does |
|---|---|
| `GEMINI_API_KEY` | Lets Gemini transcribe videos that have no captions at all. Free key at <https://aistudio.google.com/apikey>. Put it in your shell profile, never in the chat |
| `GEMINI_MODEL` | Gemini model to use (default `gemini-2.5-flash`) |
| `YT_PROXY_URL` | `http://user:pass@host:port` proxy for YouTube requests, for when YouTube blocks your IP |
| `YOUGIST_BACKEND` | Hosted fallback URL, or `off` to never use it |
| `YT_SKILLS_VENV` | Where the script keeps its private Python environment (default `~/.cache/youtube-skills/venv`) |
| `YT_SKILLS_NO_BOOTSTRAP` | Set to `1` to stop the script creating that environment. You then install `youtube-transcript-api` yourself |

## Troubleshooting

| Problem | Fix |
|---|---|
| "YouTube is blocking requests from your IP" | Wait and retry, set `YT_PROXY_URL`, or set `GEMINI_API_KEY` |
| "This video has no usable captions" | Set `GEMINI_API_KEY` so Gemini can transcribe the audio |
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

Transcripts come from YouTube's own caption tracks, or from Gemini, and are meant
for personal use: reading, studying, accessibility, notes. Respect creators'
rights when sharing them.

Questions or ideas: open an issue, or email <enderlocphan@gmail.com>.
