import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.gen_caption_timeline import map_captions
from scripts.pipeline_daily import (
    MAX_FINAL_DURATION,
    VOICEOVER_MAX_CHARS,
    VOICEOVER_MIN_CHARS,
    AUDIO_SPEED,
    PipelineError,
    parse_args as parse_pipeline_args,
    prepare_sadtalker_source_image,
    update_html,
    validate_voiceover,
    validate_name,
    wait_for_stable_mp4,
)
import scripts.pre_composite_check as timeline_check
from scripts.pre_composite_check import check_timeline, stage_duration
from scripts.upload_video import parse_args as parse_upload_args
from scripts.validate_style import compare_history, validate_guide, validate_html, validate_schema
from scripts.workflow_config import config_path, load_workflow_config
import scripts.doctor as doctor


DIMENSIONS = {
    "style_family": "editorial-collage",
    "palette": "ink-red-cream",
    "typography": "condensed-editorial",
    "composition": "asymmetric-split",
    "scene_grammar": "chapter-panels",
    "motion_language": "snap-and-hold",
    "transitions": "paper-tear",
    "media_treatment": "halftone-cutout",
    "pacing": "rhythmic-bursts",
    "audio_direction": "voice-only",
}


def valid_plan():
    plan = {
        "project": "demo01",
        "creative_seed": "seed-1",
        "content_goal": "测试",
        "audience": "测试受众",
        "emotion": "克制",
        "details": {"palette_colors": ["#000", "#fff"]},
        **DIMENSIONS,
    }
    plan["intentional_differences"] = [
        {"dimension": key, "from": "old-style", "to": value, "evidence": f"data-style-{key}"}
        for key, value in list(DIMENSIONS.items())[:5]
    ]
    return plan


class PipelineTests(unittest.TestCase):
    def test_name_rejects_shell_characters(self):
        with self.assertRaises(PipelineError):
            validate_name("bad;rm")

    def test_global_duration_policy_defaults_are_loaded(self):
        self.assertEqual(AUDIO_SPEED, 1.30)
        self.assertEqual(MAX_FINAL_DURATION, 59.5)
        self.assertEqual((VOICEOVER_MIN_CHARS, VOICEOVER_MAX_CHARS), (260, 290))

    def test_voiceover_character_policy_uses_effective_characters(self):
        validate_voiceover("中" * 260)
        validate_voiceover("中" * 290)
        with self.assertRaises(PipelineError):
            validate_voiceover("中" * 259)
        with self.assertRaises(PipelineError):
            validate_voiceover("中" * 291)

    def test_update_html_targets_only_stage_and_audio(self):
        html = """<div id="stage" data-duration="1"><audio src="old.wav" data-duration="1" id="main-voiceover"></audio><div data-duration="9"></div></div><script>const captions=[]; gsap.to('.x', {duration: 0.25});</script>"""
        updated = update_html(html, [{"start": 0, "end": 2, "text": "测试"}], 12.34, total_duration=13.54)
        self.assertIn('id="stage" data-duration="13.54"', updated)
        self.assertIn('data-duration="12.34" id="main-voiceover"', updated)
        self.assertIn('data-duration="9"', updated)
        self.assertIn("duration: 0.25", updated)
        self.assertIn('audio/voiceover_130.wav', updated)

    def test_update_html_replaces_fixed_total_duration(self):
        html = """<div id="stage" data-duration="48"><audio id="main-voiceover" src="old.wav" data-duration="48"></audio></div><script>const captions=[]; const totalDuration = 48;</script>"""
        updated = update_html(html, [{"start": 0, "end": 2, "text": "测试"}], 27.61, total_duration=28.81)
        self.assertIn("Number(document.getElementById('stage')?.dataset.duration) || 28.81", updated)
        self.assertNotIn("const totalDuration = 48;", updated)

    def test_update_html_uses_130x_audio_filename(self):
        html = '<div id="stage" data-duration="1"><audio id="main-voiceover" src="old.wav" data-duration="1"></audio></div><script>const captions=[];</script>'
        updated = update_html(html, [{"start": 0, "end": 1, "text": "测试"}], 10.0, total_duration=11.2)
        self.assertIn('src="audio/voiceover_130.wav"', updated)
        self.assertNotIn("voiceover_135.wav", updated)

    def test_update_html_strips_scene_timed_clip_attrs(self):
        html = """<div id="stage" data-duration="48"><audio id="main-voiceover" src="old.wav" data-duration="48"></audio><section class="scene clip" id="scene-1" data-start="0" data-duration="12"></section></div><script>const captions=[];</script>"""
        updated = update_html(html, [{"start": 0, "end": 2, "text": "测试"}], 78.03, total_duration=79.23)
        self.assertIn('<div id="stage" data-duration="79.23"', updated)
        self.assertIn('<audio id="main-voiceover" src="audio/voiceover_130.wav" data-duration="78.03"', updated)
        self.assertIn('<section class="scene clip" id="scene-1"></section>', updated)

    def test_stage_duration_reads_only_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            html = Path(directory) / "index.html"
            html.write_text(
                '<div data-duration="9"></div><div id="stage" data-duration="13.54"></div>',
                encoding="utf-8",
            )
            self.assertEqual(stage_duration(str(html)), 13.54)

    def test_caption_fallback_interpolates_whisper_segments(self):
        whisper = [
            {"start": 0.0, "end": 1.0, "text": "甲"},
            {"start": 9.0, "end": 10.0, "text": "乙"},
        ]
        captions = map_captions(whisper, ["未知一", "未知二"])
        self.assertEqual(len(captions), 2)
        self.assertLess(captions[0]["end"], 2.0)
        self.assertGreaterEqual(captions[1]["end"], 9.0)

    def test_sadtalker_selection_waits_for_stable_file_in_run_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run-1" / "nested"
            run_dir.mkdir(parents=True)
            output = run_dir / "result.mp4"
            output.write_bytes(b"mp4")
            self.assertEqual(wait_for_stable_mp4(run_dir.parent, timeout=0.2, poll_interval=0), output.resolve())

    def test_prepare_sadtalker_source_image_downscales_to_configured_height(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "avatar.jpg"
            source.write_bytes(b"image")
            pipeline = SimpleNamespace(run_dir=Path(directory) / "run")
            pipeline.run_dir.mkdir()
            calls = []

            def fake_run_checked(step, command, timeout=600, cwd=None, env=None):
                calls.append((step, command, timeout))
                if step == "sadtalker-source-probe":
                    return SimpleNamespace(stdout="1440\n")

            pipeline.run_checked = fake_run_checked
            prepared = prepare_sadtalker_source_image(pipeline, source, 720)

            self.assertEqual(prepared, pipeline.run_dir / "sadtalker_source_h720.jpg")
            self.assertEqual(calls[0][0], "sadtalker-source-probe")
            self.assertEqual(calls[1][0], "sadtalker-source-image")
            self.assertIn("scale=-2:720:force_original_aspect_ratio=decrease", calls[1][1])

    def test_prepare_sadtalker_source_image_keeps_small_source(self):
        pipeline = SimpleNamespace(run_dir=Path("/tmp"))

        def fake_run_checked(step, command, timeout=600, cwd=None, env=None):
            self.assertEqual(step, "sadtalker-source-probe")
            return SimpleNamespace(stdout="640\n")

        pipeline.run_checked = fake_run_checked
        source = Path("/tmp/avatar.jpg")
        self.assertEqual(prepare_sadtalker_source_image(pipeline, source, 720), source)

    def test_prepare_sadtalker_source_image_can_be_disabled(self):
        pipeline = SimpleNamespace(run_dir=Path("/tmp"), run_checked=lambda *args, **kwargs: self.fail("unexpected resize"))
        source = Path("/tmp/avatar.jpg")
        self.assertEqual(prepare_sadtalker_source_image(pipeline, source, 0), source)

    def test_full_timeline_requires_all_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(
                project=directory,
                quick=False,
                dh_only=False,
                audio=None,
                digital_human=None,
                main_video=None,
                html=None,
            )
            self.assertFalse(check_timeline(args))

    def test_each_timeline_mode_requires_its_specific_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            base = dict(project=directory, audio=None, digital_human=None, main_video=None, html=None)
            self.assertFalse(check_timeline(SimpleNamespace(**base, quick=True, dh_only=False)))
            self.assertFalse(check_timeline(SimpleNamespace(**base, quick=False, dh_only=True)))
            self.assertFalse(check_timeline(SimpleNamespace(**base, quick=False, dh_only=False)))

    def test_timeline_rejects_unordered_caption_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "voiceover_130.wav"
            html = Path(directory) / "index.html"
            audio.write_bytes(b"audio")
            html.write_text(
                '<script>const captions=[{"start": 2, "end": 3}, {"start": 1, "end": 2.5}];</script>',
                encoding="utf-8",
            )
            args = SimpleNamespace(
                project=directory,
                quick=True,
                dh_only=False,
                audio=str(audio),
                digital_human=None,
                main_video=None,
                html=str(html),
            )
            with patch.object(timeline_check, "ffprobe_duration", return_value=3.0):
                self.assertFalse(check_timeline(args))

    def test_pipeline_resume_flag_defaults_off(self):
        argv = [
            "pipeline_daily.py",
            "--name", "demo01",
            "--ref", "cosyvoice/ref_user5_clean.wav",
            "--photo", "digital-human/user_avatar.jpg",
            "--html", "projects/demo01/index.html",
            "--text", "projects/demo01/口播稿.txt",
            "--style-plan", "projects/demo01/STYLE_PLAN.yaml",
        ]
        with patch("sys.argv", argv):
            self.assertFalse(parse_pipeline_args().resume)
        with patch("sys.argv", [*argv, "--resume"]):
            self.assertTrue(parse_pipeline_args().resume)

    def test_auto_discovery_does_not_use_historical_digital_human(self):
        with tempfile.TemporaryDirectory() as directory:
            old_output = Path(directory) / "digital_human" / "old-run" / "result.mp4"
            old_output.parent.mkdir(parents=True)
            old_output.write_bytes(b"old")
            self.assertIsNone(timeline_check.find_digital_human(directory))
            current = Path(directory) / "digital_human" / "run-current" / "nested" / "result.mp4"
            current.parent.mkdir(parents=True)
            current.write_bytes(b"current")
            (Path(directory) / "run-manifest.json").write_text(
                json.dumps({"run_id": "run-current"}), encoding="utf-8"
            )
            self.assertEqual(timeline_check.find_digital_human(directory), str(current))


class StyleTests(unittest.TestCase):
    def test_schema_rejects_mapping_dimension(self):
        plan = valid_plan()
        plan["palette"] = {"accent": "#fff"}
        self.assertTrue(validate_schema(plan, "demo01"))

    def test_history_excludes_same_project(self):
        plan = valid_plan()
        history = [{"project": "demo01", **DIMENSIONS}, {"project": "old", **{key: "old-style" for key in DIMENSIONS}}]
        comparisons, error = compare_history(plan, history, 5)
        self.assertIsNone(error)
        self.assertEqual([item["project"] for item in comparisons], ["old"])

    def test_history_uses_only_complete_recent_entries(self):
        plan = valid_plan()
        history = [
            {"project": "incomplete", "style_family": "old-style"},
            *[
                {"project": f"old{i}", **{key: "old-style" for key in DIMENSIONS}}
                for i in range(6)
            ],
        ]
        comparisons, error = compare_history(plan, history, 5)
        self.assertIsNone(error)
        self.assertEqual([item["project"] for item in comparisons], ["old1", "old2", "old3", "old4", "old5"])

    def test_schema_rejects_placeholder_values(self):
        plan = valid_plan()
        plan["palette"] = "replace-me"
        self.assertTrue(validate_schema(plan, "demo01"))

    def test_guide_must_exist_and_cover_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            guide = Path(directory) / "guide.md"
            self.assertTrue(validate_guide(guide))
            guide.write_text(
                "\n".join(
                    [
                        "视觉流派 色彩系统 字体和排版 画面构图 场景结构",
                        "动画语言 转场方式 素材处理 镜头节奏 音乐与音效",
                        "最近 5 条记录，至少有 5 个维度不同",
                    ]
                ),
                encoding="utf-8",
            )
            self.assertEqual(validate_guide(guide), [])

    def test_html_metadata_must_match_plan(self):
        plan = valid_plan()
        attributes = " ".join(
            f'data-style-{key.replace("_", "-")}="{value}"' for key, value in DIMENSIONS.items()
        )
        html = f'<script src="vendor/gsap.min.js"></script><div id="stage" {attributes}><audio id="main-voiceover"></audio><section class="scene"></section><section class="scene"></section><section class="scene"></section></div><script>const captions=[];</script>'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.html"
            path.write_text(html, encoding="utf-8")
            self.assertEqual(validate_html(plan, path), [])


class ConfigTests(unittest.TestCase):
    def test_default_style_guide_is_repo_local(self):
        config = load_workflow_config(Path(__file__).resolve().parents[1])
        guide = config_path(Path(__file__).resolve().parents[1], config, "paths.style_guide")
        self.assertTrue(guide.is_file())
        self.assertIn("docs/VIDEO_STYLE_DIVERSITY_GUIDE.md", str(guide))

    def test_doctor_hyperframes_version_matches_package(self):
        ok, label, _target, detail = doctor.check_hyperframes_version(doctor.load_workflow_config(doctor.ROOT))
        if detail == "binary missing":
            self.skipTest("HyperFrames is an optional local runtime dependency")
        self.assertTrue(ok, f"{label}: {detail}")


class UploadTests(unittest.TestCase):
    def test_upload_is_not_public_by_default(self):
        argv = [
            "upload_video.py",
            "--project", "projects/demo01",
            "--video", "projects/demo01/final_video.mp4",
            "--name", "demo01",
            "--style-plan", "projects/demo01/STYLE_PLAN.yaml",
            "--style-history", "STYLE_HISTORY.md",
            "--title", "Demo",
        ]
        with patch("sys.argv", argv):
            self.assertFalse(parse_upload_args().public)


if __name__ == "__main__":
    unittest.main()
