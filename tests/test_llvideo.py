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
from llvideo import craft as craft_mod  # noqa: E402
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


class TestCraftDetection(unittest.TestCase):
    """The shape of the score curve is what separates a cut from a blend."""

    def _scores(self, pairs):
        return pairs

    def test_hard_cut_is_tall_and_narrow(self):
        from llvideo import craft
        scores = [(i * 0.033, 0.002) for i in range(60)]
        scores[30] = (30 * 0.033, 0.72)
        c = craft.find_candidates(scores)
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0].kind_hint, "cut-like")

    def test_soft_blend_is_low_and_wide_and_still_found(self):
        """A 1s crossfade peaks near 0.025 — far below any spike threshold.
        Missing these is why detect_scenes alone cannot classify transitions."""
        from llvideo import craft
        scores = [(i * 0.033, 0.002) for i in range(90)]
        for i in range(30, 60):
            scores[i] = (i * 0.033, 0.02)
        c = craft.find_candidates(scores)
        self.assertTrue(c, "a soft blend must still be detected")
        self.assertEqual(c[0].kind_hint, "blend-like")
        self.assertGreater(c[0].width, 0.5)

    def test_first_frame_artifact_ignored(self):
        from llvideo import craft
        scores = [(0.0, 0.9)] + [(i * 0.033, 0.001) for i in range(1, 60)]
        self.assertEqual(craft.find_candidates(scores), [])

    def test_no_candidates_on_a_flat_curve(self):
        from llvideo import craft
        self.assertEqual(craft.find_candidates([(i * 0.033, 0.001) for i in range(200)]), [])

    def test_prominence_keeps_soft_blends_over_noise(self):
        """Ranking on raw peak would discard every dissolve. Wide blends must survive."""
        from llvideo import craft
        blend = craft.Candidate(time=8.0, peak=0.025, width=1.0, kind_hint="blend-like")
        noise = craft.Candidate(time=14.0, peak=0.040, width=0.0, kind_hint="blend-like")
        wins = craft.windows_for([blend, noise], duration=24.0, limit=1)
        self.assertEqual(len(wins), 1)
        self.assertLess(wins[0][0], 9.0, "the 1s blend should outrank a narrow noise spike")

    def test_windows_merge_when_they_overlap(self):
        from llvideo import craft
        a = craft.Candidate(time=5.0, peak=0.5, width=0.0, kind_hint="cut-like")
        b = craft.Candidate(time=5.5, peak=0.5, width=0.0, kind_hint="cut-like")
        self.assertEqual(len(craft.windows_for([a, b], duration=20.0)), 1)

    def test_pacing_stats(self):
        from llvideo import craft
        c = [craft.Candidate(t, 0.5, 0.0, "cut-like") for t in (4.0, 8.0, 13.0, 18.0)]
        st = craft.shot_stats(c, 24.0)
        self.assertEqual(st["shots"], 5)
        self.assertEqual(st["cuts_per_minute"], 10.0)

    def test_empty_input_safe(self):
        from llvideo import craft
        self.assertEqual(craft.find_candidates([]), [])
        self.assertEqual(craft.shot_stats([], 0.0)["shots"], 0)


class TestCraftSchema(unittest.TestCase):
    def test_transition_needs_type_duration_and_confidence(self):
        req = schema.CRAFT_SCHEMA["properties"]["transitions"]["items"]["required"]
        for field_name in ("kind", "duration_seconds", "confidence"):
            self.assertIn(field_name, req)

    def test_whip_pan_is_a_camera_move_not_only_a_transition(self):
        """The trap this mode exists to avoid: fast camera movement read as an edit."""
        self.assertIn("whip_pan", schema.CAMERA_MOVES)

    def test_blend_kinds_are_distinguishable(self):
        for k in ("hard_cut", "crossfade", "fade_to_black", "wipe"):
            self.assertIn(k, schema.TRANSITION_KINDS)


class TestAuditThresholds(unittest.TestCase):
    """The limited-range bug: black in an H.264 export is luma 16, not 0.

    A threshold written for full range never fires on real footage, so this
    check silently passed everything before it was fixed.
    """

    def test_black_threshold_covers_limited_range(self):
        from llvideo import audit
        self.assertGreater(audit.BLACK_LUMA, 16.0,
                           "limited-range black is luma 16; a lower bound never fires")
        self.assertLess(audit.BLACK_LUMA, 32.0, "too loose would flag dark footage")

    def test_white_threshold_covers_limited_range(self):
        from llvideo import audit
        self.assertLessEqual(audit.WHITE_LUMA, 236.0)
        self.assertGreater(audit.WHITE_LUMA, 200.0)

    def test_severity_order_is_worst_first(self):
        from llvideo import audit
        self.assertEqual(audit.SEVERITIES[0], "blocker")
        self.assertEqual(audit.SEVERITIES[-1], "note")


class TestAuditVerdict(unittest.TestCase):
    def _f(self, sev):
        from llvideo.audit import Finding
        return Finding(sev, "x", "msg")

    def test_clean_when_empty(self):
        from llvideo import audit
        self.assertEqual(audit.summarise([])["verdict"], "clean")

    def test_blocker_dominates(self):
        from llvideo import audit
        s = audit.summarise([self._f("minor"), self._f("blocker")])
        self.assertEqual(s["verdict"], "fails")
        self.assertEqual(s["worst"], "blocker")

    def test_measured_and_judged_counted_separately(self):
        from llvideo.audit import Finding, summarise
        s = summarise([Finding("minor", "a", "m", source="measured"),
                       Finding("minor", "b", "m", source="judged")])
        self.assertEqual(s["measured_findings"], 1)
        self.assertEqual(s["judged_findings"], 1)


class TestIntentDiff(unittest.TestCase):
    def _probe(self, duration=30.0, w=1920, h=1080, audio=True):
        return probe.Probe(path="x", duration=duration, size_bytes=0,
                           width=w, height=h, has_video=True, has_audio=audio)

    def test_duration_mismatch_flagged(self):
        from llvideo import audit
        f = audit.compare_intent({"duration_seconds": 30}, self._probe(34.0), None)
        self.assertTrue(any(x.check == "duration" for x in f))

    def test_duration_within_tolerance_passes(self):
        from llvideo import audit
        f = audit.compare_intent({"duration_seconds": 30, "duration_tolerance": 1.0},
                                 self._probe(30.4), None)
        self.assertFalse(any(x.check == "duration" for x in f))

    def test_aspect_mismatch_flagged(self):
        from llvideo import audit
        f = audit.compare_intent({"aspect": "9:16"}, self._probe(w=1920, h=1080), None)
        self.assertTrue(any(x.check == "aspect" for x in f))

    def test_matching_aspect_passes(self):
        from llvideo import audit
        f = audit.compare_intent({"aspect": "16:9"}, self._probe(w=1920, h=1080), None)
        self.assertFalse(any(x.check == "aspect" for x in f))

    def test_missing_transition_flagged(self):
        from llvideo import audit
        spec = {"transitions": [{"at": "00:04", "kind": "crossfade"}]}
        f = audit.compare_intent(spec, self._probe(), {"transitions": []})
        self.assertTrue(any(x.check == "transition_missing" for x in f))

    def test_wrong_transition_kind_flagged(self):
        from llvideo import audit
        spec = {"transitions": [{"at": "00:04", "kind": "crossfade"}]}
        got = {"transitions": [{"at": "00:04", "kind": "hard_cut"}]}
        f = audit.compare_intent(spec, self._probe(), got)
        self.assertTrue(any(x.check == "transition_kind" for x in f))

    def test_correct_transition_passes(self):
        from llvideo import audit
        spec = {"transitions": [{"at": "00:04", "kind": "crossfade", "duration_seconds": 0.5}]}
        got = {"transitions": [{"at": "00:04", "kind": "crossfade", "duration_seconds": 0.55}]}
        f = audit.compare_intent(spec, self._probe(), got)
        self.assertEqual([x for x in f if x.check.startswith("transition")], [])

    def test_intent_findings_are_labelled_judged(self):
        """Transition comparison rests on a model reading, not a measurement."""
        from llvideo import audit
        spec = {"transitions": [{"at": "00:04", "kind": "crossfade"}]}
        f = audit.compare_intent(spec, self._probe(), {"transitions": []})
        self.assertTrue(all(x.source == "judged" for x in f))

    def test_music_required_but_absent(self):
        from llvideo import audit
        f = audit.compare_intent({"audio": {"must_have_music": True}},
                                 self._probe(audio=False), None)
        self.assertTrue(any(x.severity == "blocker" for x in f))

    def test_bad_spec_file_raises_clearly(self):
        from llvideo import audit
        from llvideo.errors import LLVideoError
        with self.assertRaises(LLVideoError):
            audit.load_intent("definitely_not_a_real_spec.json")


class TestSpecExtraction(unittest.TestCase):
    """Nobody should hand-write an intent spec — the project already knows."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="llvideo_spec_"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _hf(self, storyboard: str | None = None):
        (self.dir / "hyperframes.json").write_text(
            '{"width":1920,"height":1080,"fps":30}', encoding="utf-8")
        (self.dir / "index.html").write_text(
            '<div id="root" data-duration="10" data-width="1920" data-height="1080">'
            '<section id="a" class="clip" data-start="0" data-duration="4"><h1>Hello</h1></section>'
            '<section id="b" class="clip" data-start="4" data-duration="6"><h1>World</h1></section>'
            '</div>', encoding="utf-8")
        if storyboard:
            (self.dir / "STORYBOARD.md").write_text(storyboard, encoding="utf-8")
        return str(self.dir)

    def test_reads_timing_from_data_attributes(self):
        from llvideo import spec
        sp = spec.extract(self._hf())
        self.assertEqual(len(sp.scenes), 2)
        self.assertAlmostEqual(sp.scenes[0].start, 0.0)
        self.assertAlmostEqual(sp.scenes[0].end, 4.0)
        self.assertAlmostEqual(sp.scenes[1].end, 10.0)

    def test_extracts_on_screen_text(self):
        from llvideo import spec
        sp = spec.extract(self._hf())
        self.assertIn("Hello", " ".join(sp.scenes[0].text))

    def test_derives_aspect_and_duration(self):
        from llvideo import spec
        intent = spec.extract(self._hf()).to_intent()
        self.assertEqual(intent["aspect"], "16:9")
        self.assertAlmostEqual(intent["duration_seconds"], 10.0)

    def test_storyboard_transition_maps_to_previous_scene(self):
        """transition_in on frame N is the transition OUT of frame N-1."""
        from llvideo import spec
        sb = "\n".join([
            "### Frame 1 - a",
            "- transition_in: cut",
            "",
            "### Frame 2 - b",
            "- transition_in: crossfade",
            "- transition_duration: 0.6",
            "",
        ])
        sp = spec.extract(self._hf(sb))
        self.assertIsNotNone(sp.scenes[0].transition_out)
        self.assertEqual(sp.scenes[0].transition_out["kind"], "crossfade")
        self.assertAlmostEqual(sp.scenes[0].transition_out["duration_seconds"], 0.6)

    def test_no_storyboard_means_no_invented_transitions(self):
        from llvideo import spec
        sp = spec.extract(self._hf())
        self.assertTrue(all(s.transition_out is None for s in sp.scenes))
        self.assertTrue(any("not declared" in n or "no authored" in n.lower()
                            for n in sp.notes))

    def test_transition_aliases_normalise(self):
        from llvideo.spec import normalise_transition
        self.assertEqual(normalise_transition("cut"), "hard_cut")
        self.assertEqual(normalise_transition("dissolve"), "crossfade")
        self.assertEqual(normalise_transition("fade to black"), "fade_to_black")
        self.assertEqual(normalise_transition("clockWipe"), "wipe")
        self.assertIsNone(normalise_transition(None))

    def test_unknown_project_says_so(self):
        from llvideo import spec
        from llvideo.errors import LLVideoError
        with self.assertRaises(LLVideoError):
            spec.detect(str(self.dir))


class TestRemotionSpec(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="llvideo_rem_"))
        (self.dir / "src").mkdir()
        (self.dir / "remotion.config.ts").write_text("// config", encoding="utf-8")
        (self.dir / "src" / "Root.tsx").write_text(
            '<Composition id="X" durationInFrames={300} fps={30} width={1920} height={1080} />',
            encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, body):
        (self.dir / "src" / "Promo.tsx").write_text(body, encoding="utf-8")
        from llvideo import spec
        return spec.extract(str(self.dir), kind="remotion")

    def test_literal_and_fps_expressions_both_resolve(self):
        sp = self._write(
            "<Sequence from={0} durationInFrames={4 * fps}><A/></Sequence>"
            "<Sequence from={120} durationInFrames={90}><B/></Sequence>")
        self.assertEqual(len(sp.scenes), 2)
        self.assertAlmostEqual(sp.scenes[0].end, 4.0)
        self.assertAlmostEqual(sp.scenes[1].start, 4.0)
        self.assertAlmostEqual(sp.scenes[1].end, 7.0)

    def test_computed_values_are_skipped_not_guessed(self):
        sp = self._write("<Sequence from={computeIt()} durationInFrames={dyn}><A/></Sequence>")
        self.assertEqual(len(sp.scenes), 0)
        self.assertTrue(any("cannot be read statically" in n for n in sp.notes))

    def test_spring_timing_flagged_as_unreadable(self):
        sp = self._write(
            "<Sequence from={0} durationInFrames={60}><A/></Sequence>"
            "<TransitionSeries.Transition presentation={fade()} "
            "timing={springTiming({config:{damping:200}})} />")
        self.assertTrue(any("springTiming" in n for n in sp.notes))


class TestIntendedSuppression(unittest.TestCase):
    def test_declared_fade_to_black_is_not_a_defect(self):
        from llvideo.audit import Finding, suppress_intended
        f = [Finding("major", "black_gap", "0.43s of black at 00:17.", at=17.0,
                     measured={"duration": 0.43})]
        intent = {"transitions": [{"at": "00:17", "kind": "fade_to_black",
                                   "duration_seconds": 0.8}]}
        out = suppress_intended(f, intent)
        self.assertEqual(out[0].severity, "note")
        self.assertIn("Expected", out[0].message)

    def test_undeclared_black_gap_still_a_defect(self):
        from llvideo.audit import Finding, suppress_intended
        f = [Finding("major", "black_gap", "2s of black at 00:06.", at=6.0,
                     measured={"duration": 2.0})]
        intent = {"transitions": [{"at": "00:17", "kind": "fade_to_black"}]}
        self.assertEqual(suppress_intended(f, intent)[0].severity, "major")

    def test_no_intent_changes_nothing(self):
        from llvideo.audit import Finding, suppress_intended
        f = [Finding("major", "black_gap", "x", at=6.0)]
        self.assertEqual(suppress_intended(f, None), f)


class TestGrokGeneration(unittest.TestCase):
    """Grok Imagine — xAI, not Groq. Different company, different product."""

    def test_schema_matches_what_the_api_reported(self):
        """These came from the API's own 422 responses naming its variants."""
        from llvideo import generate as G
        self.assertEqual(G.RESOLUTIONS, ("480p", "720p", "1080p"))
        self.assertIn("16:9", G.ASPECTS)
        self.assertIn("9:16", G.ASPECTS)
        self.assertEqual((G.MIN_SECONDS, G.MAX_SECONDS), (1, 15))

    def test_defaults_above_grok_native_480p(self):
        """Grok returns 848x480 by default, which the auditor flags."""
        from llvideo import generate as G
        self.assertEqual(G.DEFAULT_RESOLUTION, "720p")

    def test_pricing_is_per_resolution_not_flat(self):
        """Measured: 8s@480p = $0.40, 5s@720p = $0.35. A flat rate under-quotes."""
        from llvideo.generate import estimate
        self.assertAlmostEqual(estimate(8, "480p")[0], 0.40, places=2)
        self.assertAlmostEqual(estimate(5, "720p")[0], 0.35, places=2)

    def test_unmeasured_rate_is_flagged(self):
        from llvideo.generate import estimate
        self.assertTrue(estimate(5, "720p")[1])
        self.assertFalse(estimate(5, "1080p")[1], "1080p rate is extrapolated")

    def test_tick_conversion(self):
        """cost_in_usd_ticks is 1e-10 USD, cross-checked against the image price."""
        from llvideo.generate import USD_PER_TICK
        self.assertAlmostEqual(200_000_000 * USD_PER_TICK, 0.02, places=4)
        self.assertAlmostEqual(4_000_000_000 * USD_PER_TICK, 0.40, places=4)

    def test_duration_out_of_range_rejected_before_spending(self):
        from llvideo import generate as G
        from llvideo.errors import LLVideoError
        for bad in (0, 16, 99):
            with self.assertRaises(LLVideoError):
                G.generate_video("x", "out.mp4", seconds=bad)

    def test_bad_resolution_and_aspect_rejected(self):
        from llvideo import generate as G
        from llvideo.errors import LLVideoError
        with self.assertRaises(LLVideoError):
            G.generate_video("x", "o.mp4", seconds=5, resolution="4k")
        with self.assertRaises(LLVideoError):
            G.generate_video("x", "o.mp4", seconds=5, aspect="banana")

    def test_video_to_video_needs_the_right_model(self):
        """grok-imagine-video-1.5 takes audio, not video, as input."""
        from llvideo import generate as G
        from llvideo.errors import LLVideoError
        if not G.available():
            self.skipTest("no XAI_API_KEY")
        with self.assertRaises(LLVideoError):
            G.generate_video("x", "o.mp4", seconds=5, video=__file__, with_audio=True)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class TestSinglePassDetectors(unittest.TestCase):
    """Chained filters decode once. Two separate calls decode the file twice —
    measured at 12.7s vs 5.8s on a 19.5s 4K clip."""

    @classmethod
    def setUpClass(cls):
        Fixtures.build()

    @classmethod
    def tearDownClass(cls):
        Fixtures.teardown()

    def test_video_events_match_separate_calls(self):
        combined = probe.detect_video_events(str(Fixtures.black_gap), black_dur=0.5)
        separate = probe.detect_black(str(Fixtures.black_gap), min_dur=0.5)
        self.assertEqual(len(combined["black"]), len(separate))
        if separate:
            self.assertAlmostEqual(combined["black"][0]["start"],
                                   separate[0]["start"], delta=0.05)

    def test_video_events_returns_both_keys(self):
        ev = probe.detect_video_events(str(Fixtures.three_scene))
        self.assertIn("black", ev)
        self.assertIn("freeze", ev)

    def test_audio_events_returns_loudness_and_silence(self):
        ev = probe.detect_audio_events(str(Fixtures.three_scene))
        self.assertIn("integrated_lufs", ev)
        self.assertIn("silence", ev)
        self.assertIsNotNone(ev["integrated_lufs"], "ebur128 should report a value")

    def test_luma_profile_single_pass_still_finds_black(self):
        """The optimisation must not lose the fade-to-black signal."""
        prof = craft_mod.luma_profile(str(Fixtures.black_gap), 2.5, 6.5, samples=12)
        self.assertTrue(prof)
        self.assertLessEqual(prof["min"], 20.0)
        self.assertIn("BLACK", prof["verdict"].upper())

    def test_luma_profile_thins_to_sample_count(self):
        prof = craft_mod.luma_profile(str(Fixtures.three_scene), 0.0, 12.0, samples=10)
        self.assertLessEqual(len(prof["points"]), 12)


class TestFixPolicy(unittest.TestCase):
    """What gets repaired and what deliberately does not."""

    def test_only_deterministic_repairs_are_attempted(self):
        from llvideo import fix as FX
        self.assertEqual(FX.REPAIRABLE, {"loudness", "clipping", "edge_frame"})

    def test_editorial_findings_are_never_silently_changed(self):
        """Removing a mid-timeline black gap would be editing, not repairing."""
        from llvideo import fix as FX
        for check in ("black_gap", "freeze", "resolution", "safe_margin", "duration"):
            self.assertNotIn(check, FX.REPAIRABLE)
            self.assertTrue(FX._why_skipped(check),
                            f"{check} must explain why it is not fixed")

    def test_targets_are_platform_standard(self):
        from llvideo import fix as FX
        self.assertEqual(FX.TARGET_LUFS, -14.0)
        self.assertEqual(FX.TARGET_TRUE_PEAK, -1.0)

    def test_default_output_does_not_overwrite_the_source(self):
        from llvideo.fix import _default_out
        self.assertNotEqual(_default_out("a/b/clip.mp4"), "a/b/clip.mp4")
        self.assertIn("_fixed", _default_out("a/b/clip.mp4"))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class TestFixEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="llvideo_fix_"))
        cls.quiet = cls.dir / "quiet.mp4"
        # black head, content, black tail, with very quiet audio
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
              "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.2",
              "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=4",
              "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.2",
              "-f", "lavfi", "-i", "sine=frequency=440:duration=4.4",
              "-filter_complex",
              "[0:v][1:v][2:v]concat=n=3:v=1:a=0,fps=25[v];[3:a]volume=0.01[a]",
              "-map", "[v]", "-map", "[a]", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-shortest", str(cls.quiet)])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_repairs_loudness_and_edges_and_proves_it(self):
        from llvideo import audit as A, fix as FX
        out = self.dir / "fixed.mp4"
        r = FX.fix(str(self.quiet), str(out))
        self.assertTrue(out.exists())
        self.assertTrue(r.changed)
        # The re-audit must show real improvement, not a claim of one.
        self.assertLess(r.after["counts"]["major"], r.before["counts"]["major"],
                        "the repair must reduce major findings, verified by re-measuring")

    def test_edge_frames_actually_removed(self):
        from llvideo import audit as A, fix as FX
        out = self.dir / "fixed2.mp4"
        FX.fix(str(self.quiet), str(out))
        after = A.check_edges(probe.probe(str(out)))
        self.assertEqual([f for f in after if f.check == "edge_frame"], [],
                         "black edge frames should be gone after the trim")

    def test_loudness_lands_on_target(self):
        from llvideo import fix as FX
        out = self.dir / "fixed3.mp4"
        FX.fix(str(self.quiet), str(out))
        stats = FX.measure_loudness(str(out))
        if stats.get("input_i"):
            self.assertAlmostEqual(float(stats["input_i"]), FX.TARGET_LUFS, delta=1.5)

    def test_clean_file_is_left_alone(self):
        """Nothing repairable means no output file and no claim of a change."""
        from llvideo import fix as FX
        clean = self.dir / "clean.mp4"
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
              "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=3",
              "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
              "-af", "volume=0.35", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-shortest", str(clean)])
        r = FX.fix(str(clean), str(self.dir / "noop.mp4"),
                   normalise_audio=False, trim_edges=False)
        self.assertFalse(r.changed)
        self.assertIsNone(r.output)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class TestPerformanceFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Fixtures.build()

    @classmethod
    def tearDownClass(cls):
        Fixtures.teardown()

    def test_parallel_frame_grabs_match_serial(self):
        times = [0.5, 2.0, 5.0, 9.0]
        serial = frames.frames_at(str(Fixtures.three_scene), times, width=160, workers=1)
        par = frames.frames_at(str(Fixtures.three_scene), times, width=160)
        self.assertEqual(serial, par, "parallelism must not change the output")

    def test_frames_at_empty_input(self):
        self.assertEqual(frames.frames_at(str(Fixtures.three_scene), []), [])

    def test_frame_scores_cache_returns_identical_data(self):
        first = craft_mod.frame_scores(str(Fixtures.three_scene))
        second = craft_mod.frame_scores(str(Fixtures.three_scene))
        self.assertEqual(first, second)
        self.assertTrue(first, "scores should not be empty")

    def test_cache_key_is_content_not_path(self):
        """A re-encode must invalidate the cache, so the key hashes content."""
        import shutil as _sh
        copy = Fixtures.dir / "copy_of_three.mp4"
        _sh.copyfile(Fixtures.three_scene, copy)
        a = craft_mod._scores_cache_path(str(Fixtures.three_scene))
        b = craft_mod._scores_cache_path(str(copy))
        self.assertEqual(a.name, b.name, "identical bytes should share a cache entry")


class TestTimelineFusion(unittest.TestCase):
    """Co-occurrence is the context: what was said WHILE this was on screen."""

    INDEX = {
        "summary": "s",
        "scenes": [
            {"start": "00:00", "end": "00:04", "description": "wide of a desk",
             "camera": "wide, static",
             "on_screen_text": [{"text": "Step 1", "legibility": "clear", "where": "title"}],
             "actions": ["opens laptop"]},
            {"start": "00:04", "end": "00:10", "description": "close on the screen",
             "camera": "close-up",
             "on_screen_text": [{"text": "", "legibility": "illegible",
                                 "where": "status bar"}],
             "actions": []},
        ],
        "speech": [
            {"start": "00:01", "end": "00:03", "text": "First you open it."},
            {"start": "00:05", "end": "00:08", "text": "Then it loads instantly."},
        ],
        "audio_events": [{"start": "00:00", "end": "00:10", "description": "soft music"}],
        "key_moments": [{"timestamp": "00:05", "why": "the load happens"}],
    }

    def test_speech_lands_on_the_scene_it_was_said_over(self):
        from llvideo import timeline as T
        beats = T.build(self.INDEX, duration=10.0)
        self.assertEqual(len(beats), 2)
        self.assertIn("First you open it", beats[0].spoken)
        self.assertIn("loads instantly", beats[1].spoken)

    def test_speech_straddling_a_cut_goes_to_the_bigger_overlap(self):
        """Picking the first overlap would bias every straddling line earlier."""
        from llvideo import timeline as T
        idx = dict(self.INDEX)
        idx["speech"] = [{"start": "00:03.5", "end": "00:07", "text": "straddles"}]
        beats = T.build(idx, duration=10.0)
        self.assertEqual(beats[0].spoken, "")
        self.assertIn("straddles", beats[1].spoken)

    def test_illegible_text_kept_separate_from_read_text(self):
        from llvideo import timeline as T
        beats = T.build(self.INDEX, duration=10.0)
        self.assertEqual(beats[0].on_screen_text, ["Step 1"])
        self.assertEqual(beats[1].on_screen_text, [])
        self.assertTrue(beats[1].illegible_text)

    def test_key_moment_marks_the_containing_beat(self):
        from llvideo import timeline as T
        beats = T.build(self.INDEX, duration=10.0)
        self.assertFalse(beats[0].is_key_moment)
        self.assertTrue(beats[1].is_key_moment)

    def test_local_transcript_overrides_model_speech(self):
        """The model's audio timestamps drift ~1s; a real transcript does not."""
        from llvideo import timeline as T
        tr = {"segments": [{"start": 4.5, "end": 6.0, "text": "from the transcript"}]}
        beats = T.build(self.INDEX, duration=10.0, transcript=tr)
        joined = " ".join(b.spoken for b in beats)
        self.assertIn("from the transcript", joined)
        self.assertNotIn("First you open it", joined)

    def test_coverage_reports_gaps(self):
        from llvideo import timeline as T
        idx = {"scenes": [{"start": "00:00", "end": "00:03", "description": "a"},
                          {"start": "00:08", "end": "00:10", "description": "b"}]}
        cov = T.coverage(T.build(idx, duration=10.0), 10.0)
        self.assertTrue(cov["gaps"], "a hole in the timeline must be reported")
        self.assertLess(cov["ratio"], 1.0)

    def test_coverage_always_has_every_key(self):
        """A URL has no local probe, so duration is 0 — the caller still needs
        every field. An early return with a partial dict crashed it once."""
        from llvideo import timeline as T
        beats = T.build(self.INDEX, duration=0.0)
        for d in (0.0, 10.0):
            cov = T.coverage(beats, d)
            for k in ("covered_seconds", "duration", "ratio", "gaps",
                      "with_speech", "with_text", "beats", "duration_known"):
                self.assertIn(k, cov)

    def test_empty_index_does_not_crash(self):
        from llvideo import timeline as T
        self.assertEqual(T.build({}, duration=0.0), [])
        self.assertEqual(T.coverage([], 0.0)["beats"], 0)

    def test_render_is_one_chronological_block(self):
        from llvideo import timeline as T
        text = T.render(T.build(self.INDEX, duration=10.0))
        self.assertIn("says", text)
        self.assertIn("Step 1", text)
        self.assertLess(text.index("00:00"), text.index("00:04"))


class TestIndexCache(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="llvideo_idx_"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _cache(self):
        from llvideo.analyze import IndexCache
        return IndexCache(self.dir / "indexes.json")

    def test_roundtrip(self):
        c = self._cache()
        c.put("k", {"summary": "hello"})
        self.assertEqual(self._cache().get("k"), {"summary": "hello"})

    def test_miss_returns_none(self):
        self.assertIsNone(self._cache().get("nope"))

    def test_key_separates_sampling_settings(self):
        """A 0.2fps run must not be served to a full-rate request."""
        from llvideo.analyze import IndexCache
        a = IndexCache.key("http://x/v", 1.0, True, None)
        b = IndexCache.key("http://x/v", 0.2, True, None)
        c = IndexCache.key("http://x/v", 1.0, False, None)
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)

    def test_expired_entry_is_dropped(self):
        import time as _t
        from llvideo.analyze import IndexCache
        c = self._cache()
        c.data["old"] = {"index": {"x": 1}, "at": _t.time() - IndexCache.TTL_SECONDS - 10}
        c.save()
        self.assertIsNone(self._cache().get("old"))

    def test_corrupt_file_does_not_crash(self):
        (self.dir / "indexes.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(self._cache().get("k"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
