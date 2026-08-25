"""Tests for llvideo.

Everything here runs offline and costs nothing. Test fixtures are generated with
ffmpeg so the suite has real video to work on without shipping binaries.

    python -m pytest tests/ -v
    python tests/test_llvideo.py        # also works without pytest
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llvideo import consistency, frames, probe, schema  # noqa: E402
from llvideo.errors import ProbeFailed  # noqa: E402

FONT = "C:/Windows/Fonts/arial.ttf" if os.name == "nt" else None
HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


class Fixtures:
    dir: Path
    three_scene: Path
    silent: Path
    black_gap: Path

    @classmethod
    def build(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="llvideo_tests_"))
        cls.three_scene = cls.dir / "three_scene.mp4"
        cls.silent = cls.dir / "silent.mp4"
        cls.black_gap = cls.dir / "black_gap.mp4"

        # 3 hard-cut scenes, 4s each, with a 1 kHz tone
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
              "-f", "lavfi", "-i", "color=c=red:s=640x360:d=4",
              "-f", "lavfi", "-i", "color=c=green:s=640x360:d=4",
              "-f", "lavfi", "-i", "color=c=blue:s=640x360:d=4",
              "-f", "lavfi", "-i", "sine=frequency=1000:duration=12",
              "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
              "-map", "[v]", "-map", "3:a", "-r", "25", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-shortest", str(cls.three_scene)])
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
              "-i", str(cls.three_scene), "-an", "-c:v", "copy", str(cls.silent)])
        # 3s of black in the middle
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
              "-f", "lavfi", "-i", "color=c=white:s=320x240:d=3",
              "-f", "lavfi", "-i", "color=c=black:s=320x240:d=3",
              "-f", "lavfi", "-i", "color=c=white:s=320x240:d=3",
              "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
              "-map", "[v]", "-r", "25", "-pix_fmt", "yuv420p", str(cls.black_gap)])

    @classmethod
    def teardown(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Pure logic — no ffmpeg needed
# ---------------------------------------------------------------------------

class TestTimestamps(unittest.TestCase):
    def test_parses_every_form(self):
        self.assertAlmostEqual(schema.normalise_timestamp("83"), 83.0)
        self.assertAlmostEqual(schema.normalise_timestamp("01:23"), 83.0)
        self.assertAlmostEqual(schema.normalise_timestamp("00:01:23"), 83.0)
        self.assertAlmostEqual(schema.normalise_timestamp("1:02:03"), 3723.0)

    def test_rejects_garbage_instead_of_guessing(self):
        for bad in ("", None, "later", "??:??"):
            self.assertEqual(schema.normalise_timestamp(bad), -1.0)


class TestFrameSelection(unittest.TestCase):
    def test_uniform_floor_always_present(self):
        times = probe.select_frame_times(120.0, scene_times=[], floor_interval=12.0)
        self.assertGreaterEqual(len(times), 8)
        self.assertTrue(all(0 <= t <= 120 for t in times))

    def test_scene_hits_are_added_not_substituted(self):
        """The union is the point: scene detection alone silently drops cuts."""
        floor_only = probe.select_frame_times(60.0, [], floor_interval=12.0)
        with_scenes = probe.select_frame_times(60.0, [7.5, 31.2], floor_interval=12.0)
        self.assertGreater(len(with_scenes), len(floor_only))
        for cut in (7.5, 31.2):
            self.assertTrue(any(abs(t - cut) < 0.5 for t in with_scenes),
                            f"cut at {cut} was dropped")

    def test_respects_max_frames(self):
        times = probe.select_frame_times(7200.0, list(range(0, 7200, 30)),
                                         floor_interval=5.0, max_frames=16)
        self.assertLessEqual(len(times), 16)

    def test_zero_duration_does_not_crash(self):
        self.assertEqual(probe.select_frame_times(0.0), [0.0])


class TestTokenEstimates(unittest.TestCase):
    """The rates are measured, not documented. Guard them against drift."""

    def _p(self, duration, audio=True):
        return probe.Probe(path="x", duration=duration, size_bytes=0, has_audio=audio)

    def test_matches_measured_rates(self):
        e = self._p(600).estimate_tokens()
        self.assertEqual(e["video"], 42600)   # 71/s
        self.assertEqual(e["audio"], 19200)   # 32/s
        self.assertEqual(e["total"], 61800)

    def test_fps_scales_video_only(self):
        e = self._p(600).estimate_tokens(0.2)
        self.assertEqual(e["video"], 8520)
        self.assertEqual(e["audio"], 19200, "audio is a floor fps cannot lower")

    def test_no_audio_removes_the_floor(self):
        self.assertEqual(self._p(600, audio=False).estimate_tokens()["audio"], 0)


class TestRotation(unittest.TestCase):
    def test_portrait_phone_footage(self):
        p = probe.Probe(path="x", duration=1, size_bytes=0,
                        width=3840, height=2160, rotation=-90)
        self.assertEqual((p.display_width, p.display_height), (2160, 3840))
        self.assertTrue(p.is_portrait)
        self.assertEqual(p.orientation, "portrait")

    def test_unrotated_is_unchanged(self):
        p = probe.Probe(path="x", duration=1, size_bytes=0, width=1920, height=1080)
        self.assertEqual((p.display_width, p.display_height), (1920, 1080))
        self.assertFalse(p.is_portrait)


class TestScreenHeuristic(unittest.TestCase):
    def test_obs_capture_detected(self):
        p = probe.Probe(path="x", duration=1, size_bytes=0,
                        width=1920, height=1080, fps=60.0)
        self.assertTrue(p.is_screen_content)

    def test_cinema_4k_is_not_screen_content(self):
        """3840x2160 is a desktop size AND a camera size — fps breaks the tie."""
        p = probe.Probe(path="x", duration=1, size_bytes=0,
                        width=3840, height=2160, fps=23.976)
        self.assertFalse(p.is_screen_content)


class TestConsistency(unittest.TestCase):
    def test_unanimous(self):
        a = consistency.agree(["the cat sat", "the cat sat", "the cat sat"])
        self.assertEqual(a.verdict, "unanimous")

    def test_split_is_unreliable(self):
        a = consistency.agree(["55 F", "65 F", "23.9"])
        self.assertEqual(a.verdict, "unreliable")

    def test_formatting_differences_still_agree(self):
        a = consistency.agree(["55 °F", "55F", "55 F"])
        self.assertEqual(a.verdict, "unanimous")

    def test_flags_the_real_dashboard_case(self):
        """The measured failure: only '330 mi' held across three runs."""
        runs = [
            {"scenes": [{"on_screen_text": [
                {"text": "642", "legibility": "clear", "where": "dash"},
                {"text": "330 mi", "legibility": "clear", "where": "dash"}]}]},
            {"scenes": [{"on_screen_text": [
                {"text": "23.9", "legibility": "clear", "where": "dash"},
                {"text": "330 mi", "legibility": "clear", "where": "dash"}]}]},
            {"scenes": [{"on_screen_text": [
                {"text": "6:42", "legibility": "clear", "where": "dash"},
                {"text": "330 mi", "legibility": "clear", "where": "dash"}]}]},
        ]
        summary = consistency.summarise(consistency.check_text_claims(runs))
        self.assertIn("330 mi", summary["stable"])
        self.assertEqual(len(summary["unstable"]), 3)

    def test_prose_paraphrase_is_agreement_not_failure(self):
        """The false positive that shipped once: two answers saying the same
        thing in different words must not read as unreliable."""
        a = consistency.agree([
            "There is only one person visible. They stand under a tree at night, "
            "swaying and gesturing with their hands.",
            "Only one person is visible in the video. The person stands beneath a "
            "tree at night, gesturing with their hands and swaying to music.",
        ])
        self.assertNotEqual(a.verdict, "unreliable")
        self.assertEqual(a.variants, [])

    def test_prose_that_genuinely_differs_is_still_caught(self):
        a = consistency.agree([
            "There is one person visible, standing under a tree at night.",
            "Three people are visible, sitting inside a car during the day.",
        ])
        self.assertEqual(a.verdict, "unreliable")

    def test_short_readings_still_need_exact_match(self):
        """A single changed digit IS the failure for a dashboard reading."""
        a = consistency.agree(["55 F", "65 F"])
        self.assertEqual(a.verdict, "unreliable")

    def test_empty_input_is_safe(self):
        self.assertEqual(consistency.check_text_claims([]), [])
        self.assertEqual(consistency.agree([]).trials, 0)


class TestSchema(unittest.TestCase):
    def test_legibility_gate_is_in_the_schema(self):
        """The gate must live on the field, not only in the prompt."""
        item = (schema.VIDEO_INDEX_SCHEMA["properties"]["scenes"]["items"]
                ["properties"]["on_screen_text"]["items"])
        self.assertIn("legibility", item["properties"])
        self.assertIn("illegible", item["properties"]["legibility"]["enum"])

    def test_uncertainties_is_required(self):
        self.assertIn("uncertainties", schema.VIDEO_INDEX_SCHEMA["required"])

    def test_answers_must_cite(self):
        self.assertIn("citations", schema.ANSWER_SCHEMA["required"])

    def test_index_frame_times_dedupes(self):
        idx = {"key_moments": [{"timestamp": "00:05"}, {"timestamp": "00:05"}],
               "scenes": [{"start": "00:00"}, {"start": "00:20"}]}
        times = schema.index_frame_times(idx, duration=30.0)
        self.assertEqual(len(times), len(set(times)))
        self.assertTrue(all(0 <= t <= 30 for t in times))

    def test_index_frame_times_drops_out_of_range(self):
        idx = {"key_moments": [{"timestamp": "99:00"}], "scenes": []}
        self.assertEqual(schema.index_frame_times(idx, duration=30.0), [])


class TestOpenRouterSchemaConversion(unittest.TestCase):
    def test_google_dialect_becomes_json_schema(self):
        from llvideo.providers.openrouter import _to_json_schema
        out = _to_json_schema(schema.ANSWER_SCHEMA)
        self.assertEqual(out["type"], "object")
        self.assertFalse(out["additionalProperties"])
        self.assertEqual(out["properties"]["citations"]["type"], "array")


class TestProviderErrors(unittest.TestCase):
    def test_unknown_provider_named_clearly(self):
        from llvideo.providers import pick_provider
        from llvideo.errors import NoProvider
        with self.assertRaises(NoProvider):
            pick_provider("nope")


# ---------------------------------------------------------------------------
# Real ffmpeg work — still free, still offline
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class TestAgainstRealVideo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Fixtures.build()

    @classmethod
    def tearDownClass(cls):
        Fixtures.teardown()

    def test_probe_reads_the_file(self):
        p = probe.probe(str(Fixtures.three_scene))
        self.assertAlmostEqual(p.duration, 12.0, delta=0.3)
        self.assertEqual((p.width, p.height), (640, 360))
        self.assertTrue(p.has_video)
        self.assertTrue(p.has_audio)

    def test_missing_file_says_so(self):
        with self.assertRaises(ProbeFailed):
            probe.probe(str(Fixtures.dir / "does_not_exist.mp4"))

    def test_silent_file_detected(self):
        p = probe.probe(str(Fixtures.silent))
        self.assertFalse(p.has_audio)
        self.assertEqual(p.estimate_tokens()["audio"], 0)

    def test_blackdetect_is_exact(self):
        events = probe.detect_black(str(Fixtures.black_gap), min_dur=0.5)
        self.assertTrue(events, "the 3s black segment was not found")
        self.assertAlmostEqual(events[0]["start"], 3.0, delta=0.3)
        self.assertAlmostEqual(events[0]["end"], 6.0, delta=0.3)

    def test_scene_detect_misses_the_first_cut(self):
        """Documents the known structural failure so a regression is visible.

        Cuts are at 4s and 8s. The filter compares frame N with N-1, so the
        opening boundary can never register. This is why frame selection unions
        scene hits with a uniform floor.
        """
        cuts = probe.detect_scenes(str(Fixtures.three_scene), threshold=0.3)
        self.assertTrue(all(c > 1.0 for c in cuts),
                        "a cut at t=0 would mean the filter changed behaviour")

    def test_zero_disk_extraction_writes_nothing(self):
        before = set(Path(tempfile.gettempdir()).glob("llvideo_*"))
        jpegs = frames.frames_at(str(Fixtures.three_scene), [1.0, 5.0, 9.0], width=320)
        after = set(Path(tempfile.gettempdir()).glob("llvideo_*"))
        self.assertEqual(len(jpegs), 3)
        self.assertTrue(all(j.startswith(frames.SOI) for j in jpegs))
        self.assertEqual(before, after, "frames_at left files on disk")

    def test_contact_sheet_builds_and_is_cheaper(self):
        out = Fixtures.dir / "sheet.jpg"
        s = frames.contact_sheet(str(Fixtures.three_scene), [1.0, 5.0, 9.0], str(out),
                                 tile_width=240)
        self.assertTrue(out.exists())
        self.assertEqual(len(s.frame_times), 3)
        if s.width:
            self.assertLess(s.approx_tokens, 3 * 1100,
                            "a sheet must cost less than the frames separately")

    def test_contact_sheet_refuses_empty_input(self):
        from llvideo.errors import LLVideoError
        with self.assertRaises(LLVideoError):
            frames.contact_sheet(str(Fixtures.three_scene), [], str(Fixtures.dir / "x.jpg"))

    def test_transcode_shrinks_and_keeps_duration(self):
        p = probe.probe(str(Fixtures.three_scene))
        out = Fixtures.dir / "proxy.mp4"
        px = frames.transcode_proxy(p, str(out), height=180)
        self.assertTrue(out.exists())
        self.assertAlmostEqual(probe.probe(str(out)).duration, p.duration, delta=0.5)

    def test_plan_is_free_and_complete(self):
        from llvideo.analyze import plan
        pl = plan(str(Fixtures.three_scene))
        d = pl.to_dict()
        self.assertIn(pl.band, ("cheap", "notify", "ask", "unknown"))
        self.assertGreater(d["estimated_tokens"], 0)
        json.dumps(d)  # must be serialisable for --json

    def test_long_video_plan_lowers_sampling(self):
        from llvideo.analyze import plan, ADMISSION_LIMIT
        p = probe.Probe(path=str(Fixtures.three_scene), duration=3 * 3600,
                        size_bytes=1000, has_video=True, has_audio=True)
        import llvideo.analyze as az
        original = az.P.probe
        az.P.probe = lambda _p: p
        try:
            pl = plan("fake.mp4")
            self.assertLess(pl.fps_ratio, 1.0, "3h video must not sample at full rate")
            self.assertTrue(pl.notes)
        finally:
            az.P.probe = original


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class TestCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Fixtures.build()

    @classmethod
    def tearDownClass(cls):
        Fixtures.teardown()

    def _cli(self, *args):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, "-m", "llvideo", *args],
                              capture_output=True, text=True, timeout=300,
                              cwd=str(Path(__file__).resolve().parent.parent), env=env)

    def test_probe_json_parses(self):
        cp = self._cli("probe", str(Fixtures.three_scene), "--json")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        d = json.loads(cp.stdout)
        self.assertIn("duration", d)
        self.assertIn("estimate_default", d)

    def test_signals_json_parses(self):
        cp = self._cli("signals", str(Fixtures.three_scene), "--json")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        d = json.loads(cp.stdout)
        self.assertIn("scene_cuts", d)
        self.assertIn("note", d, "the scene-detect caveat must always be surfaced")

    def test_missing_file_exits_nonzero_with_a_message(self):
        cp = self._cli("probe", "no_such_file.mp4")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("error:", cp.stderr)

    def test_providers_runs_without_a_video(self):
        cp = self._cli("providers", "--json")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("gemini", json.loads(cp.stdout))


if __name__ == "__main__":
    unittest.main(verbosity=2)
