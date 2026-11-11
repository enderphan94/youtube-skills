#!/usr/bin/env python3
"""Unit tests for the youtube-skills scripts. Offline: no network, no API keys.

    python3 -m unittest discover -s tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SUB = ROOT / "skills" / "youtube-sub" / "scripts"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ft = load("fetch_transcript", SUB / "fetch_transcript.py")
ac = load("apply_corrections", SUB / "apply_corrections.py")


def cues(*texts):
    return [{"start": float(i), "duration": 1.0, "text": t} for i, t in enumerate(texts)]


class Track:
    """Stand-in for youtube_transcript_api.Transcript."""

    def __init__(self, code, generated=False, translatable=True, name=None):
        self.language_code = code
        self.is_generated = generated
        self.is_translatable = translatable
        self.language = name or code


class VideoId(unittest.TestCase):
    def test_url_shapes(self):
        vid = "dQw4w9WgXcQ"
        for url in [
            vid,
            f"https://www.youtube.com/watch?v={vid}",
            f"https://youtube.com/watch?v={vid}&t=42s&list=PL123",
            f"https://m.youtube.com/watch?v={vid}",
            f"https://music.youtube.com/watch?v={vid}&si=x",
            f"https://youtu.be/{vid}?si=abc",
            f"youtu.be/{vid}",
            f"https://www.youtube.com/shorts/{vid}",
            f"https://www.youtube.com/live/{vid}?feature=share",
            f"https://www.youtube.com/embed/{vid}",
            f"https://www.youtube-nocookie.com/embed/{vid}",
            f"<https://www.youtube.com/watch?v={vid}>",
        ]:
            self.assertEqual(ft.extract_video_id(url), vid, url)

    def test_rejects_non_videos(self):
        for url in ["", "https://vimeo.com/123", "https://www.youtube.com/playlist?list=PL1",
                    "https://www.youtube.com/@channel", "not a url", "short"]:
            self.assertEqual(ft.extract_video_id(url), "", url)


class Cleaning(unittest.TestCase):
    """Behaviour ported from YouGist's test_transcript_clean.py."""

    def test_newlines_and_join(self):
        out = ft.clean_snippets(cues("in front of the\nelephants", "the cool thing"))
        self.assertEqual(out, "in front of the elephants the cool thing")

    def test_annotations(self):
        self.assertEqual(ft.clean_snippets(cues("[Music]", "Hello there", "[ __ ]", "world", "[Applause]")),
                         "Hello there world")
        self.assertEqual(ft.clean_snippets(cues("[MUSIC] a [music] b [Laughter] c")), "a b c")

    def test_speaker_labels_and_stutters_kept(self):
        self.assertIn("[John]", ft.clean_snippets(cues("[John] I disagree")))
        self.assertIn("if if I I spent", ft.clean_snippets(cues("says if if I", "I spent")))
        self.assertEqual(ft.clean_snippets(cues("same line", "same line")), "same line same line")

    def test_music_glyphs(self):
        self.assertEqual(ft.clean_snippets(cues("♪ a sung line ♪")), "a sung line")
        self.assertEqual(ft.clean_snippets(cues("[♪♪♪]", "first words")), "first words")
        self.assertEqual(ft.clean_snippets(cues("[ ♪ ♪ ] alpha [♪] beta")), "alpha beta")

    def test_entities_untouched(self):
        self.assertEqual(ft.clean_snippets(cues("write &amp; in R&D")), "write &amp; in R&D")

    def test_empty(self):
        self.assertEqual(ft.clean_snippets(cues("", None, "   ", "real")), "real")
        self.assertEqual(ft.clean_snippets([]), "")

    def test_fillers(self):
        self.assertEqual(ft.clean_snippets(cues("ừ ừ, có một cách khác")), "có một cách khác")
        self.assertEqual(ft.clean_snippets(cues("um, so anyway")), "so anyway")
        self.assertEqual(ft.clean_snippets(cues("ừ", "ừ, ở chương 17")), "ở chương 17")

    def test_real_vietnamese_words_kept(self):
        self.assertEqual(ft.clean_snippets(cues("ngài ở đó và ở đây")), "ngài ở đó và ở đây")
        self.assertEqual(ft.clean_snippets(cues("con ạ, thưa thầy ạ")), "con ạ, thưa thầy ạ")
        self.assertEqual(ft.clean_snippets(cues("Anh à, à ừ, ừ.")), "Anh à, à.")
        self.assertEqual(ft.clean_snippets(cues("hôm nay a lô a")), "hôm nay a lô a")


class Paragraphs(unittest.TestCase):
    def test_only_whitespace_changes(self):
        text = " ".join(f"Sentence number {i} is here." for i in range(300))
        out = ft.to_paragraphs(text)
        self.assertEqual(out.split(), text.split())
        self.assertGreater(out.count("\n\n"), 5)
        self.assertTrue(all(len(p) <= 1000 for p in out.split("\n\n")))

    def test_unpunctuated_asr(self):
        text = " ".join(["word"] * 3000)
        out = ft.to_paragraphs(text)
        self.assertEqual(out.split(), text.split())
        self.assertTrue(all(len(p) <= 1000 for p in out.split("\n\n")))

    def test_keeps_existing_paragraphs(self):
        self.assertEqual(ft.to_paragraphs("One.\n\nTwo."), "One.\n\nTwo.")


class Chunking(unittest.TestCase):
    def test_content_preserved_and_bounded(self):
        text = ft.to_paragraphs(" ".join(f"This is sentence {i}." for i in range(5000)))
        parts = ft.chunk_text(text, 8000)
        self.assertGreater(len(parts), 5)
        self.assertEqual(" ".join(parts).split(), text.split())
        self.assertTrue(all(len(p) <= 8800 for p in parts))

    def test_short_text_single_chunk(self):
        self.assertEqual(ft.chunk_text("hello world", 100), ["hello world"])
        self.assertEqual(ft.chunk_text("", 100), [])

    def test_sliver_absorbed(self):
        # 1,050 chars with a 1,000 window: the 50-char leftover (< 10%) is
        # folded into the first piece instead of becoming a runt chunk.
        text = ("x " * 520) + "tail"
        parts = ft.chunk_text(text, 1000)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0], text.strip())

    def test_no_spaces_hard_cut(self):
        parts = ft.chunk_text("z" * 2500, 1000)
        self.assertEqual("".join(parts), "z" * 2500)


class TrackOrder(unittest.TestCase):
    def test_ted_case_prefers_spoken_language(self):
        tracks = [Track("ar"), Track("bg"), Track("en"), Track("vi"), Track("en", generated=True)]
        order = [ft.track_label(t) for t in ft.order_tracks(tracks)]
        self.assertEqual(order[:2], ["en", "en (auto)"])

    def test_user_preference_wins(self):
        tracks = [Track("ar"), Track("en"), Track("vi"), Track("en", generated=True)]
        order = [ft.track_label(t) for t in ft.order_tracks(tracks, ["vi"])]
        self.assertEqual(order[0], "vi")

    def test_regional_variants_match(self):
        tracks = [Track("pt-BR"), Track("en-GB"), Track("en", generated=True)]
        order = [ft.track_label(t) for t in ft.order_tracks(tracks)]
        self.assertEqual(order[0], "en-GB")
        order = [ft.track_label(t) for t in ft.order_tracks(tracks, ["pt"])]
        self.assertEqual(order[0], "pt-BR")

    def test_auto_only(self):
        tracks = [Track("vi", generated=True)]
        self.assertEqual([ft.track_label(t) for t in ft.order_tracks(tracks)], ["vi (auto)"])

    def test_every_track_listed_once(self):
        tracks = [Track("de"), Track("en"), Track("en", generated=True), Track("fr")]
        self.assertEqual(len(ft.order_tracks(tracks, ["en", "en", "de"])), 4)


class FetchableTrack(Track):
    def __init__(self, code, generated=False, block_translate=False):
        super().__init__(code, generated, translatable=True, name=f"lang-{code}")
        self.block_translate = block_translate

    def translate(self, target):
        if self.block_translate:
            raise type("RequestBlocked", (Exception,), {})("YouTube is blocking requests")
        return FetchableTrack(target, self.is_generated)

    def fetch(self):
        snip = type("S", (), {"text": f"spoken in {self.language_code}", "start": 0.0, "duration": 2.0})
        return type("F", (), {"snippets": [snip]})


class Translation(unittest.TestCase):
    def test_native_track_preferred_over_translation(self):
        tracks = [FetchableTrack("vi", generated=True), FetchableTrack("en")]
        with mock.patch.object(ft, "list_tracks", return_value=tracks):
            res = ft.fetch_captions("abcdefghijk", translate_to="en")
        self.assertEqual(res["language_code"], "en")
        self.assertIsNone(res["translated_to"])

    def test_translation(self):
        tracks = [FetchableTrack("vi", generated=True)]
        with mock.patch.object(ft, "list_tracks", return_value=tracks):
            res = ft.fetch_captions("abcdefghijk", translate_to="en")
        self.assertEqual(res["translated_to"], "en")
        self.assertEqual(res["segments"][0]["text"], "spoken in en")

    def test_blocked_translation_returns_original(self):
        tracks = [FetchableTrack("vi", generated=True, block_translate=True)]
        with mock.patch.object(ft, "list_tracks", return_value=tracks):
            res = ft.fetch_captions("abcdefghijk", translate_to="en")
        self.assertTrue(res["translation_failed"])
        self.assertIsNone(res["translated_to"])
        self.assertEqual(res["segments"][0]["text"], "spoken in vi")
        self.assertIn("refused to machine-translate", res["warnings"][0])


class Classify(unittest.TestCase):
    def test_names(self):
        def exc(name):
            return type(name, (Exception,), {})()

        self.assertEqual(ft.classify(exc("TranscriptsDisabled")), "no_captions")
        self.assertEqual(ft.classify(exc("NoTranscriptFound")), "no_captions")
        self.assertEqual(ft.classify(exc("RequestBlocked")), "blocked")
        self.assertEqual(ft.classify(exc("IpBlocked")), "blocked")
        self.assertEqual(ft.classify(exc("VideoUnavailable")), "unavailable")
        self.assertEqual(ft.classify(exc("AgeRestricted")), "age_restricted")
        self.assertEqual(ft.classify(ConnectionError("x")), "network")
        self.assertEqual(ft.classify(ValueError("x")), "failed")

    def test_backend_messages(self):
        self.assertEqual(ft._classify_message("No transcript available for this video."), "no_captions")
        self.assertEqual(ft._classify_message("Subtitles are disabled for this video"), "no_captions")
        self.assertEqual(ft._classify_message("YouTube is blocking requests from your IP"), "blocked")


class Rendering(unittest.TestCase):
    def test_srt(self):
        segs = [
            {"start": 0.0, "duration": 4.0, "text": "[Music]"},
            {"start": 1.5, "duration": 3.0, "text": "hello\nthere"},
            {"start": 3.0, "duration": 2.5, "text": "um, world"},
        ]
        srt = ft.render_srt(segs)
        self.assertEqual(
            srt,
            "1\n00:00:01,500 --> 00:00:03,000\nhello there\n\n"
            "2\n00:00:03,000 --> 00:00:05,500\nworld\n",
        )

    def test_times(self):
        self.assertEqual(ft.fmt_srt_time(3725.5), "01:02:05,500")
        self.assertEqual(ft.fmt_ts(65), "01:05")
        self.assertEqual(ft.fmt_ts(3725), "1:02:05")
        self.assertEqual(ft.fmt_ts(65, force_hours=True), "0:01:05")

    def test_group_cues(self):
        segs = [{"start": float(i * 5), "duration": 5.0, "text": f"w{i}"} for i in range(13)]
        groups = ft.group_cues(segs, seconds=30)
        self.assertEqual([g["start"] for g in groups], [0.0, 30.0, 60.0])
        self.assertEqual(groups[0]["text"], "w0 w1 w2 w3 w4 w5")

    def test_slugify(self):
        self.assertEqual(ft.slugify("Càng Tu càng Tệ - Ajahn Chah"), "cang-tu-cang-te-ajahn-chah")
        self.assertEqual(ft.slugify("Đức Phật"), "duc-phat")
        self.assertEqual(ft.slugify("日本語"), "")
        long = ft.slugify("How to stay calm when you know you'll be stressed | Daniel Levitin | TED")
        self.assertEqual(long, "how-to-stay-calm-when-you-know-you-ll-be-stressed-daniel")


class GeminiClips(unittest.TestCase):
    def test_plan(self):
        self.assertEqual(ft.plan_clips(None, 1800), [(None, None)])
        self.assertEqual(ft.plan_clips(600, 1800), [(0, 600)])
        self.assertEqual(ft.plan_clips(3700, 1800), [(0, 1800), (1800, 3700)])  # 100 s sliver folded
        self.assertEqual(ft.plan_clips(4000, 1800), [(0, 1800), (1800, 3600), (3600, 4000)])

    def test_request_and_parse(self):
        sent = {}

        def fake_http_json(url, payload=None, headers=None, timeout=60, proxy=""):
            sent.update(url=url, payload=payload, headers=headers)
            return {"candidates": [{"content": {"parts": [{"text": "Hello "}, {"text": "world."}]},
                                    "finishReason": "STOP"}]}

        with mock.patch.object(ft, "http_json", fake_http_json):
            text, warn = ft._gemini_clip("KEY", "gemini-2.5-flash", "abcdefghijk", 1800, 3600, "T", "vi")
        self.assertEqual(text, "Hello world.")
        self.assertIn("gemini-2.5-flash:generateContent", sent["url"])
        self.assertNotIn("KEY", sent["url"])  # key travels in a header, not the URL
        self.assertEqual(sent["headers"]["x-goog-api-key"], "KEY")
        part = sent["payload"]["contents"][0]["parts"][0]
        self.assertEqual(part["fileData"]["fileUri"], "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(part["videoMetadata"]["startOffset"], "1800s")
        self.assertEqual(part["videoMetadata"]["endOffset"], "3600s")
        self.assertEqual(sent["payload"]["generationConfig"]["thinkingConfig"], {"thinkingBudget": 0})
        self.assertIn("possibly truncated", warn)  # 2 words for 30 minutes

    def test_finish_reason_warning(self):
        resp = {"candidates": [{"content": {"parts": [{"text": "x " * 5000}]}, "finishReason": "MAX_TOKENS"}]}
        with mock.patch.object(ft, "http_json", lambda *a, **k: resp):
            _, warn = ft._gemini_clip("K", "gemini-2.5-pro", "abcdefghijk", None, None, None, "")
        self.assertIn("MAX_TOKENS", warn)

    def test_no_key(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}):
            with self.assertRaises(ft.SourceError) as ctx:
                ft.fetch_gemini("abcdefghijk", 100, None, "")
        self.assertEqual(ctx.exception.kind, "disabled")


class Chain(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ft.main(argv)
        return code, out.getvalue()

    def test_no_captions_skips_backend_and_reports_hint(self):
        def no_caps(*a, **k):
            raise ft.SourceError("no_captions", "Subtitles are disabled for this video")

        backend = mock.Mock()
        with mock.patch.object(ft, "ensure_library", return_value=True), \
             mock.patch.object(ft, "fetch_oembed", return_value={"title": "T"}), \
             mock.patch.object(ft, "fetch_captions", no_caps), \
             mock.patch.object(ft, "fetch_backend", backend), \
             mock.patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}):
            code, out = self.run_main(["abcdefghijk", "--quiet"])
        report = json.loads(out)
        self.assertEqual(code, ft.EXIT_NO_TRANSCRIPT)
        backend.assert_not_called()
        self.assertEqual([a["source"] for a in report["attempts"]], ["captions", "backend", "gemini"])
        self.assertEqual(report["attempts"][1]["kind"], "skipped")
        self.assertTrue(any("GEMINI_API_KEY" in h for h in report["hints"]))

    def test_blocked_falls_back_to_backend(self):
        def blocked(*a, **k):
            raise ft.SourceError("blocked", "YouTube is blocking requests from your IP")

        backend_result = {"source": "backend", "transcript_type": "auto", "language": None,
                          "language_code": None, "translated_to": None,
                          "text": "Hello from the backend. " * 3, "warnings": ["w"]}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(ft, "ensure_library", return_value=True), \
             mock.patch.object(ft, "fetch_oembed", return_value={}), \
             mock.patch.object(ft, "fetch_captions", blocked), \
             mock.patch.object(ft, "fetch_backend", return_value=backend_result):
            code, out = self.run_main(["abcdefghijk", "--quiet", "--format", "srt", "--out-dir", tmp])
            report = json.loads(out)
            self.assertEqual(code, 0)
            self.assertEqual(report["source"], "backend")
            self.assertEqual(report["format"], "text")  # SRT impossible without timing
            self.assertTrue(Path(report["out"]).read_text().startswith("URL:"))

    def test_unavailable_stops_chain(self):
        def gone(*a, **k):
            raise ft.SourceError("unavailable", "The video is no longer available")

        gem = mock.Mock()
        with mock.patch.object(ft, "ensure_library", return_value=True), \
             mock.patch.object(ft, "fetch_oembed", return_value={}), \
             mock.patch.object(ft, "fetch_captions", gone), \
             mock.patch.object(ft, "fetch_gemini", gem):
            code, out = self.run_main(["abcdefghijk", "--quiet"])
        self.assertEqual(code, ft.EXIT_UNAVAILABLE)
        gem.assert_not_called()

    def captions(self, kind, n=400):
        segs = [{"start": float(i * 3), "duration": 3.0, "text": f"this is sentence {i}"} for i in range(n)]
        return {"source": "captions", "transcript_type": kind, "language": "English",
                "language_code": "en", "translated_to": None, "segments": segs,
                "available_tracks": ["en (auto)"]}

    def fetch(self, res, *flags):
        with mock.patch.object(ft, "ensure_library", return_value=True), \
             mock.patch.object(ft, "fetch_oembed", return_value={"title": "My Talk", "channel": "C"}), \
             mock.patch.object(ft, "fetch_captions", return_value=res), \
             mock.patch.object(ft.tempfile, "gettempdir", return_value=self.tmp):
            code, out = self.run_main(["abcdefghijk", "--quiet", "--out-dir", self.tmp, *flags])
        self.assertEqual(code, 0)
        return json.loads(out)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def test_human_captions_are_final(self):
        report = self.fetch(self.captions("manual"))
        self.assertTrue(report["out"].endswith("my-talk-abcdefghijk.txt"))
        self.assertEqual(report["duration"], "20:00")
        self.assertNotIn("needs_correction", report)
        self.assertNotIn("parts", report)

    def test_auto_captions_get_parts(self):
        res = self.captions("auto", n=1000)
        report = self.fetch(res)
        self.assertTrue(report["needs_correction"])
        self.assertGreater(len(report["parts"]), 1)
        self.assertTrue((Path(report["parts_dir"]) / "corrected").is_dir())
        joined = " ".join(Path(p).read_text() for p in report["parts"])
        self.assertEqual(joined.split(), ft.render_text(res).split())

    def test_raw_and_translated_skip_correction(self):
        self.assertNotIn("needs_correction", self.fetch(self.captions("auto"), "--raw"))
        self.assertNotIn("needs_correction", self.fetch(self.captions("auto"), "--format", "json"))
        translated = dict(self.captions("auto"), translated_to="vi")
        self.assertNotIn("needs_correction", self.fetch(translated))

    def test_srt_round_trip(self):
        """Fetch an auto .srt, correct its cue lines, apply: text changes, timing doesn't."""
        report = self.fetch(self.captions("auto", n=5), "--format", "srt")
        before = Path(report["out"]).read_text()
        part = Path(report["parts"][0])
        self.assertEqual(part.read_text().splitlines()[0], "[1] this is sentence 0")
        fixed = part.read_text().replace("this is", "This is").replace("\n", ".\n")
        (part.parent / "corrected" / part.name).write_text(fixed)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ac.main(["--parts-dir", report["parts_dir"], "--out", report["out"]])
        self.assertEqual(json.loads(out.getvalue())["accepted"], 1)
        after = Path(report["out"]).read_text()
        self.assertIn("1\n00:00:00,000 --> 00:00:03,000\nThis is sentence 0.\n", after)
        self.assertEqual(re.findall(r".* --> .*", before), re.findall(r".* --> .*", after))

    def test_bad_input(self):
        code, out = self.run_main(["https://vimeo.com/1", "--quiet"])
        self.assertEqual(code, ft.EXIT_USAGE)
        self.assertFalse(json.loads(out)["ok"])


class LocalWhisper(unittest.TestCase):
    RESULT = {"source": "whisper", "transcript_type": "auto", "language": None,
              "language_code": "zh", "translated_to": None, "model": "small", "duration": 6,
              "segments": [{"start": 0.0, "duration": 2.5, "text": "你好"},
                           {"start": 3.0, "duration": 3.0, "text": "世界"}]}

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ft.main(argv)
        return code, json.loads(out.getvalue())

    def test_local_file_is_transcribed_not_fetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "My Clip.mp4"
            media.write_bytes(b"x")
            whisper = mock.Mock(return_value=self.RESULT)
            with mock.patch.object(ft, "ensure_library", return_value=True), \
                 mock.patch.object(ft, "fetch_oembed", side_effect=AssertionError("no network for files")), \
                 mock.patch.object(ft, "fetch_whisper", whisper), \
                 mock.patch.object(ft.tempfile, "gettempdir", return_value=tmp):
                code, report = self.run_main([str(media), "--quiet", "--format", "srt", "--out-dir", tmp])
            self.assertEqual(code, 0)
            self.assertEqual(whisper.call_args.args[1], media.resolve())
            self.assertEqual(report["source"], "whisper")
            self.assertNotIn("url", report)
            self.assertTrue(report["out"].endswith("my-clip.zh.srt"))
            self.assertTrue(report["needs_correction"])
            self.assertIn("1\n00:00:00,000 --> 00:00:02,500\n你好\n", Path(report["out"]).read_text())

    def test_whisper_is_opt_in_and_hinted(self):
        def no_caps(*a, **k):
            raise ft.SourceError("no_captions", "Subtitles are disabled for this video")

        whisper = mock.Mock()
        with mock.patch.object(ft, "ensure_library", return_value=True), \
             mock.patch.object(ft, "fetch_oembed", return_value={}), \
             mock.patch.object(ft, "fetch_captions", no_caps), \
             mock.patch.object(ft, "fetch_whisper", whisper), \
             mock.patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}):
            code, report = self.run_main(["abcdefghijk", "--quiet"])
        whisper.assert_not_called()
        self.assertEqual(code, ft.EXIT_NO_TRANSCRIPT)
        self.assertIn("--source whisper", report["hints"][0])

    def test_blocked_download_suggests_a_file(self):
        def blocked(*a, **k):
            raise ft.SourceError("blocked", "yt-dlp: Sign in to confirm you're not a bot")

        with mock.patch.object(ft, "ensure_library", return_value=True), \
             mock.patch.object(ft, "fetch_oembed", return_value={}), \
             mock.patch.object(ft, "fetch_whisper", blocked):
            code, report = self.run_main(["abcdefghijk", "--quiet", "--source", "whisper"])
        self.assertEqual(code, ft.EXIT_NO_TRANSCRIPT)
        self.assertTrue(any("file's path" in h for h in report["hints"]))

    def test_ytdlp_errors(self):
        self.assertEqual(ft._classify_ytdlp("ERROR: Sign in to confirm you’re not a bot"), "blocked")
        self.assertEqual(ft._classify_ytdlp("ERROR: [youtube] x: Private video"), "unavailable")
        self.assertEqual(ft._classify_ytdlp("ERROR: Unable to download webpage: timed out"), "network")
        self.assertEqual(ft._classify_ytdlp("ERROR: something new"), "failed")

    def test_download_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)

            def fake_run(cmd, **kw):
                (folder / "audio.webm").write_bytes(b"x")
                return mock.Mock(returncode=0, stderr="")

            with mock.patch.object(ft, "pip_install"), \
                 mock.patch.object(ft.shutil, "which", side_effect=lambda n: "/bin/node" if n == "node" else None), \
                 mock.patch.object(ft.subprocess, "run", side_effect=fake_run) as run, \
                 mock.patch.dict(os.environ, {"YT_PROXY_URL": ""}):
                self.assertEqual(ft.download_audio("abcdefghijk", folder), folder / "audio.webm")
            cmd = run.call_args.args[0]
            self.assertEqual(cmd[-1], "https://www.youtube.com/watch?v=abcdefghijk")
            self.assertIn("--no-playlist", cmd)
            self.assertEqual(cmd[cmd.index("--js-runtimes") + 1], "node")
            self.assertNotIn("bun", cmd)


class Corrections(unittest.TestCase):
    ORIGINAL = ("ngài đã theo đuổi lối tu khổ hạnh và hành xác quá rệt ăn uống ngủ nghỉ "
                "ngày đã tự hành hạ bản thân mình một cách khắc nghiệt nhưng điều quan trọng là")

    def test_accepts_light_repair(self):
        fixed = ("Ngài đã theo đuổi lối tu khổ hạnh và hành xác quá mức: ăn uống, ngủ nghỉ. "
                 "Ngài đã tự hành hạ bản thân mình một cách khắc nghiệt. Nhưng điều quan trọng là")
        ok, why = ac.check(self.ORIGINAL, fixed)
        self.assertTrue(ok, why)

    def test_unicode_composition_ignored(self):
        import unicodedata
        ok, why = ac.check(unicodedata.normalize("NFD", self.ORIGINAL), self.ORIGINAL.capitalize() + ".")
        self.assertTrue(ok, why)
        self.assertTrue(why.startswith("0%"), why)

    def test_unspaced_scripts_compared_per_character(self):
        ok, why = ac.check("今日は天気がいいですね 散歩に行きましょう", "今日は天気がいいですね。散歩に行きましょう。")
        self.assertTrue(ok, why)

    def test_rejects_summary(self):
        ok, why = ac.check(self.ORIGINAL, "Ngài tu khổ hạnh.")
        self.assertFalse(ok)
        self.assertIn("length", why)

    def test_rejects_language_drift(self):
        ok, why = ac.check(self.ORIGINAL, "他追求苦行，折磨自己的身体，饮食睡眠都极度克制，但重要的是" * 2)
        self.assertFalse(ok)
        self.assertIn("drift", why)

    def test_rejects_paraphrase(self):
        fixed = ("Thầy từng sống rất kham khổ, hành hạ thân xác bằng cách nhịn ăn và thức "
                 "khuya liên tục, nhưng rồi thầy nhận ra một điều quan trọng hơn hết thảy")
        ok, why = ac.check(self.ORIGINAL, fixed)
        self.assertFalse(ok)
        self.assertIn("words changed", why)

    def test_markers_must_survive(self):
        original = "[00:00] hello world how are you\n[00:30] i am fine thank you"
        ok, why = ac.check(original, "[00:00] Hello world, how are you?\n[00:30] I am fine, thank you.")
        self.assertTrue(ok, why)
        ok, why = ac.check(original, "[00:00] Hello world, how are you? I am fine, thank you.")
        self.assertFalse(ok)
        self.assertIn("timestamps", why)

    def test_emptied_cue_is_dropped_and_renumbered(self):
        srt = ("1\n00:00:00,000 --> 00:00:02,000\nhello there\n\n"
               "2\n00:00:02,000 --> 00:00:04,000\nthanks for watching\n\n"
               "3\n00:00:04,000 --> 00:00:06,000\ngoodbye now\n")
        out = ac.rebuild_srt(srt, ["[1] Hello there.\n[2]\n[3] Goodbye now."])
        self.assertEqual(out, "1\n00:00:00,000 --> 00:00:02,000\nHello there.\n\n"
                              "2\n00:00:04,000 --> 00:00:06,000\nGoodbye now.\n")

    def apply(self, t):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ac.main(["--parts-dir", str(t / "parts"), "--out", str(t / "talk.txt")])
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())

    def test_rejected_part_keeps_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "parts" / "corrected").mkdir(parents=True)
            (t / "parts" / "part-01.txt").write_text("hello world how are you today my friend")
            (t / "parts" / "part-02.txt").write_text("second part stays as it is")
            (t / "parts" / "corrected" / "part-01.txt").write_text("Hello world, how are you today, my friend?")
            (t / "talk.txt").write_text("Title:    X\nURL:      https://y\n\nraw body\n")
            report = self.apply(t)
            self.assertEqual(report["accepted"], 1)
            self.assertEqual(report["kept_original"], [{"part": "part-02.txt", "reason": "missing or empty"}])
            final = (t / "talk.txt").read_text()
            self.assertTrue(final.startswith("Title:    X\nURL:      https://y\nCorrected:"))
            self.assertIn("Hello world, how are you today, my friend?\n\nsecond part stays as it is", final)
            self.assertNotIn("raw body", final)
            self.apply(t)  # running again must not stack a second "Corrected:" line
            self.assertEqual((t / "talk.txt").read_text(), final)

    def test_nothing_accepted_leaves_file_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "parts").mkdir()
            (t / "parts" / "part-01.txt").write_text("hello world")
            (t / "talk.txt").write_text("Title:    X\n\nhello world\n")
            self.assertEqual(self.apply(t)["accepted"], 0)
            self.assertEqual((t / "talk.txt").read_text(), "Title:    X\n\nhello world\n")


def seg(start, end, text):
    return {"start": float(start), "duration": float(end - start), "text": text}


class SpeechCheck(unittest.TestCase):
    """The reported incident: an invented line stamped over 0:00-0:30 hid the
    dialogue at 0:20-0:30, and correction then deleted it without a trace."""

    STOCK = "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目"
    FIRST = [seg(0, 29.98, STOCK), seg(30, 33.12, "哥前前后后耽误了多少好日子"), seg(33.12, 34.38, "这下半辈子")]
    SPEECH = [(20.5, 34.4)]

    def test_stock_phrases(self):
        self.assertTrue(ft.is_stock_phrase(self.STOCK))
        self.assertTrue(ft.is_stock_phrase("請不吝點贊訂閱轉發打賞支持明鏡與點點欄目"))
        self.assertTrue(ft.is_stock_phrase("Thanks for watching!"))
        self.assertTrue(ft.is_stock_phrase("Subtitles by the Amara.org community"))
        self.assertTrue(ft.is_stock_phrase("中文字幕——YK"))
        self.assertFalse(ft.is_stock_phrase("Thanks for watching the kids yesterday"))
        self.assertFalse(ft.is_stock_phrase("哥前前后后耽误了多少好日子"))
        self.assertFalse(ft.is_stock_phrase("..."))

    def test_uncovered_speech(self):
        self.assertEqual(ft.uncovered_speech([(0, 10)], [(0, 4), (7, 10)], 1.5), [(4, 7)])
        self.assertEqual(ft.uncovered_speech([(0, 10)], [(0, 4), (5, 10)], 1.5), [])
        self.assertEqual(ft.uncovered_speech([(0, 3), (20, 30)], [], 1.5), [(0, 3), (20, 30)])

    def test_swallowed_speech_is_recovered(self):
        calls = []

        def redecode(lo, hi):
            calls.append((lo, hi))
            return [seg(20.56, 24.2, "你看啥呀?傻了?没傻。"), seg(24.66, 30.22, "老刘,你看这两棵树")]

        out, check = ft.guard_segments([dict(s) for s in self.FIRST], self.SPEECH, redecode)
        self.assertEqual(calls, [(19.5, 30.98)])  # from the speech onset, not from 0:00
        self.assertEqual([s["text"] for s in out][:2], ["你看啥呀?傻了?没傻。", "老刘,你看这两棵树"])
        self.assertNotIn(self.STOCK, [s["text"] for s in out])
        self.assertEqual(len(out), 4)
        self.assertEqual(check["status"], "pass")
        self.assertEqual(check["recovered"][0]["replaced"], self.STOCK)

    def test_stock_line_over_silence_is_removed_and_reported(self):
        first = [seg(0, 2, "Thanks for watching!"), seg(20.5, 24, "hello there my friend")]
        out, check = ft.guard_segments(first, [(20.5, 24)], lambda lo, hi: self.fail("no re-decode needed"))
        self.assertEqual([s["text"] for s in out], ["hello there my friend"])
        self.assertEqual(check["removed"][0]["text"], "Thanks for watching!")
        self.assertEqual(check["status"], "pass")

    def test_long_stock_line_is_decoded_again_even_without_a_voice(self):
        # The detector misses speech over music, so "no voice" is not trusted for a long line.
        first = [seg(0, 20, "Thanks for watching!"), seg(20.5, 24, "hello there my friend")]
        out, check = ft.guard_segments([dict(s) for s in first], [(20.5, 24)],
                                       lambda lo, hi: [seg(12, 18, "words spoken over the music")])
        self.assertEqual([s["text"] for s in out], ["words spoken over the music", "hello there my friend"])
        # Text that changes with every decode is invented too, not recovered speech.
        takes = iter([[seg(3, 16, "some other invented credit")], [seg(3, 16, "yet another different thing")]])
        out, check = ft.guard_segments([dict(s) for s in first], [(20.5, 24)], lambda lo, hi: next(takes))
        self.assertEqual([s["text"] for s in out], ["hello there my friend"])
        self.assertNotIn("recovered", check)
        out, check = ft.guard_segments([dict(s) for s in first], [(20.5, 24)],
                                       lambda lo, hi: [seg(0, 19, "Thank you for watching.")])
        self.assertEqual([s["text"] for s in out], ["hello there my friend"])
        self.assertEqual(len(check["removed"]), 1)

    def test_stock_line_that_is_really_said_stays(self):
        first = [seg(0, 4, "hello there my friend"), seg(4, 6, "Thanks for watching!")]
        out, check = ft.guard_segments(first, [(0, 6)], lambda lo, hi: [seg(4.1, 5.8, "Thanks for watching.")])
        self.assertEqual(len(out), 2)
        self.assertNotIn("removed", check)

    def test_unrecognized_speech_asks_for_review(self):
        first = [seg(0, 29.98, self.STOCK)]
        out, check = ft.guard_segments(first, [(20.5, 30.4)], lambda lo, hi: [])
        self.assertEqual(out, [])
        self.assertEqual(check["status"], "review")
        self.assertEqual(check["review"][0]["at"], "00:20-00:30")

    def test_skipped_speech_gets_its_own_pass(self):
        first = [seg(0, 4, "hello there my friend"), seg(20, 24, "and goodbye for now")]
        speech = [(0, 4), (8, 14), (20, 24)]
        out, check = ft.guard_segments(first, speech, lambda lo, hi: [seg(8.2, 13.5, "this part was skipped")])
        self.assertEqual([s["text"] for s in out][1], "this part was skipped")
        self.assertEqual(check["recovered"][0]["reason"], "speech that the first pass skipped")
        # ...but words the neighbours already have are not added twice.
        out, check = ft.guard_segments(first, speech, lambda lo, hi: [seg(8.2, 13.5, "Hello there, my friend.")])
        self.assertEqual(len(out), 2)
        self.assertEqual(check["status"], "review")

    def test_normal_transcript_is_untouched(self):
        first = [seg(0, 4, "hello there my friend"), seg(4, 8, "how are you doing today")]
        out, check = ft.guard_segments(first, [(0, 8)], lambda lo, hi: self.fail("nothing is suspicious"))
        self.assertEqual(out, first)
        self.assertEqual(check, {"status": "pass", "speech_seconds": 8.0})

    def test_cue_without_voice_is_listed(self):
        first = [seg(0, 4, "hello there my friend"), seg(60, 63, "Zither Harp")]
        out, check = ft.guard_segments(first, [(0, 4)], lambda lo, hi: [])
        self.assertEqual(len(out), 2)  # kept: only the correction step may propose removing it
        self.assertEqual(check["no_voice_lines"], [{"at": "01:00-01:03", "text": "Zither Harp"}])

    def test_marker_spans(self):
        segs = [seg(0, 29.98, "a"), seg(30, 33, "b")]
        self.assertEqual(ft.marker_spans(segs, "srt", 30, False), {"1": [0.0, 29.98], "2": [30.0, 33.0]})
        self.assertEqual(ft.marker_spans(segs, "timestamped", 30, False), {"00:00": [0.0, 30.0], "00:30": [30.0, 33.0]})
        self.assertEqual(ft.marker_spans(segs, "text", 30, False), {})

    def deletion(self, speech):
        """Claude empties cue 1 of the incident's first-pass SRT."""
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "parts" / "corrected").mkdir(parents=True)
            (t / "parts" / "part-01.txt").write_text(f"[1] {self.STOCK}\n[2] 哥前前后后耽误了多少好日子\n[3] 这下半辈子\n")
            (t / "parts" / "corrected" / "part-01.txt").write_text("[1]\n[2] 哥前前后后耽误了多少好日子\n[3] 这下半辈子\n")
            if speech is not None:
                ft.write_speech_map(t / "parts", speech, ft.marker_spans(self.FIRST, "srt", 30, False))
            (t / "a.srt").write_text(ft.render_srt(self.FIRST))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ac.main(["--parts-dir", str(t / "parts"), "--out", str(t / "a.srt")])
            return json.loads(out.getvalue()), (t / "a.srt").read_text()

    def test_deleting_a_line_over_speech_is_refused(self):
        report, srt = self.deletion(self.SPEECH)
        self.assertEqual(report["status"], "review")
        self.assertEqual(report["deletions_refused"][0]["line"], "[1]")
        self.assertIn("9.5 s", report["deletions_refused"][0]["reason"])
        self.assertIn(self.STOCK, srt)  # the line stays, so the hole stays visible
        self.assertEqual(srt.count(" --> "), 3)

    def test_deleting_a_line_over_silence_is_allowed(self):
        report, srt = self.deletion([(30.0, 34.4)])
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["removed_lines"], [{"line": "[1]", "text": self.STOCK}])
        self.assertNotIn(self.STOCK, srt)
        self.assertTrue(srt.startswith("1\n00:00:30,000"))

    def test_deleting_without_a_speech_map_is_refused(self):
        report, srt = self.deletion(None)
        self.assertEqual(report["status"], "review")
        self.assertIn(self.STOCK, srt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
