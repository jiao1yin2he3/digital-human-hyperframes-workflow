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
    build_resume_contract,
    parse_args as parse_pipeline_args,
    prepare_sadtalker_source_image,
    update_html,
    validate_voiceover,
    validate_resume_contract,
    validate_name,
    wait_for_stable_mp4,
)
import scripts.pre_composite_check as timeline_check
from scripts.pre_composite_check import check_timeline, stage_duration
from scripts.upload_video import parse_args as parse_upload_args
from scripts.validate_style import compare_history, validate_guide, validate_html, validate_schema
from scripts.workflow_config import config_path, load_workflow_config
import scripts.doctor as doctor
import scripts.append_style_history as append_style
import scripts.repair_run as repair_run
import scripts.upload_video as upload_video
import scripts.validate_style as validate_style
from scripts.pipeline_daily import TTS_INTERVAL_SILENCE


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
        self.assertEqual(timeline_check.MAX_FINAL_DURATION, 59.5)

    def test_voiceover_character_policy_uses_effective_characters(self):
        validate_voiceover("中" * 260)
        validate_voiceover("中" * 290)
        with self.assertRaises(PipelineError):
            validate_voiceover("中" * 259)
        with self.assertRaises(PipelineError):
            validate_voiceover("中" * 291)

    def test_resume_contract_rejects_changed_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {}
            for name in ("ref.wav", "photo.jpg", "index.html", "text.txt", "STYLE_PLAN.yaml", "STYLE_HISTORY.md"):
                path = root / name
                path.write_text(f"{name}-v1", encoding="utf-8")
                paths[name] = path
            inputs = {
                "reference_audio": paths["ref.wav"],
                "photo": paths["photo.jpg"],
                "html": paths["index.html"],
                "text": paths["text.txt"],
                "style_plan": paths["STYLE_PLAN.yaml"],
                "style_history": paths["STYLE_HISTORY.md"],
            }
            contract = build_resume_contract(inputs, DIMENSIONS)
            validate_resume_contract({"resume_contract": contract}, contract)
            paths["text.txt"].write_text("changed", encoding="utf-8")
            changed = build_resume_contract(inputs, DIMENSIONS)
            with self.assertRaises(PipelineError):
                validate_resume_contract({"resume_contract": contract}, changed)

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
        html = f'<script src="vendor/gsap.min.js"></script><div id="stage" {attributes}><audio id="main-voiceover"></audio><section class="scene"></section><section class="scene"></section><section class="scene"></section></div><script>const captions=[]; const scenes=document.querySelectorAll(".scene");</script>'
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
        self.assertTrue(ok, f"{label}: {detail}")


class ExtraRegressionTests(unittest.TestCase):
    def test_doctor_allow_resume_check(self):
        ok_res, label, target, detail = doctor.check_config_allow_resume({"pipeline": {"allow_resume": False}})
        self.assertTrue(ok_res)
        ok_fail, label, target, detail = doctor.check_config_allow_resume({"pipeline": {"allow_resume": True}})
        self.assertFalse(ok_fail)

    def test_caption_map_short_audio_reproduction(self):
        whisper_data = [
            {"start": 0.0, "end": 0.3, "text": "快"},
            {"start": 0.3, "end": 0.4, "text": "语"},
        ]
        captions = map_captions(whisper_data, ["快", "语"])
        self.assertEqual(len(captions), 2)
        for cap in captions:
            self.assertLess(cap["start"], cap["end"])
            self.assertLessEqual(cap["end"], 0.4)

    def test_caption_map_empty_whisper_raises(self):
        with self.assertRaises(ValueError):
            map_captions([], ["句一", "句二"])

    def test_caption_map_multi_sentence_same_segment(self):
        whisper_data = [{"start": 0.0, "end": 2.0, "text": "第一句第二句"}]
        captions = map_captions(whisper_data, ["第一句", "第二句"])
        self.assertEqual(len(captions), 2)
        for cap in captions:
            self.assertLess(cap["start"], cap["end"])
            self.assertLessEqual(cap["end"], 2.0)

    def test_caption_map_too_short_audio_raises(self):
        whisper_data = [{"start": 0.0, "end": 0.02, "text": "短"}]
        sentences = [f"句子{i}" for i in range(10)]
        with self.assertRaises(ValueError):
            map_captions(whisper_data, sentences)

    def test_bigtext_template_static_scene_switching(self):
        template_path = Path(__file__).resolve().parents[1] / "templates" / "bigtext_template.html"
        content = template_path.read_text(encoding="utf-8")
        self.assertNotIn("sceneTimes=[0,12,24,36]", content)
        self.assertNotIn("delay:(sceneTimes[i]-12)*1000", content)
        self.assertIn("stage.dataset.duration", content)

    def test_blank_template_static_scene_switching(self):
        template_path = Path(__file__).resolve().parents[1] / "templates" / "blank_template.html"
        content = template_path.read_text(encoding="utf-8")
        self.assertIn("querySelectorAll('.scene')", content)
        self.assertIn("dataset", content)

    def test_validate_style_html_gate_runs_even_when_schema_fails(self):
        plan = valid_plan()
        plan["palette"] = "replace-me"
        with tempfile.TemporaryDirectory() as directory:
            html_file = Path(directory) / "index.html"
            html_file.write_text(
                '<script src="vendor/gsap.min.js"></script><div id="stage"><audio id="main-voiceover"></audio><section class="scene"></section><section class="scene"></section></div><script>const captions=[]; const scenes=document.querySelectorAll(".scene");</script>',
                encoding="utf-8",
            )
            args = SimpleNamespace(
                plan=str(Path(directory) / "plan.yaml"),
                history=str(Path(directory) / "history.md"),
                project="demo01",
                html=str(html_file),
                min_different=5,
                guide=str(config_path(Path(__file__).resolve().parents[1], load_workflow_config(Path(__file__).resolve().parents[1]), "paths.style_guide")),
                report=None,
            )
            Path(args.plan).write_text(json.dumps(plan), encoding="utf-8")
            Path(args.history).write_text("# History\n", encoding="utf-8")
            errors = validate_html(plan, html_file)
            self.assertIn("HTML 至少需要 3 个独立 scene", errors)
            exit_code = validate_style.execute(args)
            self.assertEqual(exit_code, 1)

    def test_validate_html_rejects_static_css_only_scenes(self):
        plan = valid_plan()
        html = '<script src="vendor/gsap.min.js"></script><div id="stage"><audio id="main-voiceover"></audio><section class="scene"></section><section class="scene"></section><section class="scene"></section></div><script>const captions=[];</script>'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.html"
            path.write_text(html, encoding="utf-8")
            errors = validate_html(plan, path)
            self.assertIn("HTML 缺少 JS 场景切换逻辑（仅依赖 CSS，后续场景无法正常显示）", errors)

    def test_upload_rejects_external_video_path(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "demo"
            project.mkdir()
            ext_video = Path(directory) / "external.mp4"
            ext_video.write_bytes(b"vid")
            args = SimpleNamespace(
                project=str(project),
                video=str(ext_video),
                name="demo",
                style_plan=str(project / "plan.yaml"),
                style_history=str(project / "history.md"),
                title="title",
                desc="",
                dynamic="",
                tag="tag",
                public=False,
                reuse_pipeline_lock=True,
            )
            with self.assertRaises(RuntimeError) as ctx:
                upload_video.execute(args)
            self.assertIn("不在项目目录", str(ctx.exception))

    def test_upload_rejects_missing_done_dir_in_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "demo"
            project.mkdir()
            video = project / "final_video.mp4"
            video.write_bytes(b"vid")
            (project / "plan.yaml").write_text("project: demo\n", encoding="utf-8")
            (project / "history.md").write_text("# History\n", encoding="utf-8")
            (project / "index.html").write_text("<html></html>", encoding="utf-8")
            (project / "voiceover_130.wav").write_bytes(b"audio")
            (project / "口播稿.txt").write_text("中" * 270, encoding="utf-8")
            vhash = upload_video.sha256_file(video)

            runs_dir = project / "runs" / "run1"
            runs_dir.mkdir(parents=True)
            manifest = {
                "schema_version": 1,
                "run_id": "run1",
                "project": "demo",
                "status": "validated",
                "outputs": {
                    "html": str(project / "index.html"),
                    "text": str(project / "口播稿.txt"),
                    "final_audio": str(project / "voiceover_130.wav"),
                    "final_video_sha256": vhash,
                    "digital_human": str(project / "digital_human.mp4"),
                    "main_video": str(project / "main_video.mp4"),
                    "duration": 50.0,
                },
            }
            (project / "digital_human.mp4").write_bytes(b"digital_human")
            (project / "main_video.mp4").write_bytes(b"main_video")
            (project / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (runs_dir / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            final_dir = project / "final"
            final_dir.mkdir()
            marker = {
                "status": "success",
                "video_sha256": vhash,
                "done_dir": str(Path(directory) / "non_existent_done"),
            }
            manifest["policy"] = {
                "audio_speed": 1.30,
                "max_final_duration": 59.5,
                "end_padding_seconds": 1.2,
                "whisper_language": "zh",
                "voiceover_min_chars": 260,
                "voiceover_max_chars": 290,
            }
            manifest["tts_synthesis_report"] = {
                "f0_audit": {"passed": True},
                "content_acceptance": {"passed": True},
            }
            (project / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (runs_dir / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (final_dir / "upload-success.json").write_text(json.dumps(marker), encoding="utf-8")

            args = SimpleNamespace(
                project=str(project),
                video=str(video),
                name="demo",
                style_plan=str(project / "plan.yaml"),
                style_history=str(project / "history.md"),
                title="title",
                desc="",
                dynamic="",
                tag="tag",
                public=False,
                reuse_pipeline_lock=True,
            )
            def fake_ffprobe(path, entries, stream=None):
                if "duration" in entries:
                    if Path(path).name == "voiceover_130.wav":
                        return "48.8"
                    return "50.0"
                if "width" in entries:
                    return "1920,1080"
                return "0"

            with patch("scripts.upload_video.ffprobe_value", side_effect=fake_ffprobe), \
                 patch("scripts.upload_video.run_checked", return_value=SimpleNamespace(stdout='{"text":"转写文本内容足够长测试123456789","f0":100}')), \
                 patch("scripts.pipeline_daily.validate_voiceover"):
                with self.assertRaises(RuntimeError) as ctx:
                    upload_video.execute(args)
                self.assertIn("done_dir 不存在", str(ctx.exception))

    def test_upload_rejects_manifest_without_current_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "demo"
            project.mkdir()
            video = project / "final_video.mp4"
            video.write_bytes(b"vid")
            (project / "plan.yaml").write_text("project: demo\n", encoding="utf-8")
            (project / "history.md").write_text("# History\n", encoding="utf-8")
            (project / "index.html").write_text("<html></html>", encoding="utf-8")
            audio = project / "voiceover_130.wav"
            audio.write_bytes(b"audio")
            run_dir = project / "runs" / "run1"
            run_dir.mkdir(parents=True)
            manifest = {
                "run_id": "run1",
                "project": "demo",
                "status": "validated",
                "outputs": {
                    "html": str(project / "index.html"),
                    "final_audio": str(audio),
                    "final_video_sha256": upload_video.sha256_file(video),
                },
            }
            (project / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (run_dir / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            args = SimpleNamespace(
                project=str(project), video=str(video), name="demo",
                style_plan=str(project / "plan.yaml"), style_history=str(project / "history.md"),
                title="title", desc="", dynamic="", tag="tag", public=False,
                reuse_pipeline_lock=True,
            )
            with self.assertRaises(RuntimeError) as ctx:
                upload_video.execute(args)
            self.assertIn("duration policy", str(ctx.exception))

    def test_upload_rejects_final_duration_mismatch_with_audio_padding(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "demo"
            project.mkdir()
            video = project / "final_video.mp4"
            video.write_bytes(b"vid")
            audio = project / "voiceover_130.wav"
            audio.write_bytes(b"audio")
            html = project / "index.html"
            html.write_text("<html></html>", encoding="utf-8")
            text = project / "口播稿.txt"
            text.write_text("中" * 270, encoding="utf-8")
            style_plan = project / "plan.yaml"
            style_history = project / "history.md"
            style_plan.write_text("project: demo\n", encoding="utf-8")
            style_history.write_text("# History\n", encoding="utf-8")
            dh = project / "digital_human.mp4"
            mv = project / "main_video.mp4"
            dh.write_bytes(b"dh")
            mv.write_bytes(b"mv")
            run_dir = project / "runs" / "run1"
            run_dir.mkdir(parents=True)
            manifest = {
                "schema_version": 1,
                "run_id": "run1",
                "project": "demo",
                "status": "validated",
                "policy": {
                    "audio_speed": 1.30,
                    "max_final_duration": 59.5,
                    "end_padding_seconds": 1.2,
                    "whisper_language": "zh",
                    "voiceover_min_chars": 260,
                    "voiceover_max_chars": 290,
                },
                "outputs": {
                    "html": str(html),
                    "text": str(text),
                    "final_audio": str(audio),
                    "final_video_sha256": upload_video.sha256_file(video),
                    "digital_human": str(dh),
                    "main_video": str(mv),
                    "duration": 50.0,
                },
                "tts_synthesis_report": {
                    "f0_audit": {"passed": True},
                    "content_acceptance": {"passed": True},
                },
            }
            (project / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (run_dir / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            args = SimpleNamespace(
                project=str(project), video=str(video), name="demo",
                style_plan=str(style_plan), style_history=str(style_history),
                title="title", desc="", dynamic="", tag="tag", public=False,
                reuse_pipeline_lock=True,
            )

            def fake_ffprobe(path, entries, stream=None):
                if "duration" in entries:
                    return "48.0" if Path(path).name == "voiceover_130.wav" else "50.0"
                if "width" in entries:
                    return "1920,1080"
                return "0"

            with patch("scripts.upload_video.ffprobe_value", side_effect=fake_ffprobe):
                with self.assertRaises(RuntimeError) as ctx:
                    upload_video.execute(args)
            self.assertIn("音频 48.00s + 尾部缓冲 1.20s", str(ctx.exception))

    def test_upload_rejects_manifest_with_different_whisper_language(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "demo"
            project.mkdir()
            video = project / "final_video.mp4"
            video.write_bytes(b"vid")
            audio = project / "voiceover_130.wav"
            audio.write_bytes(b"audio")
            html = project / "index.html"
            html.write_text("<html></html>", encoding="utf-8")
            text = project / "script.txt"
            text.write_text("中" * 270, encoding="utf-8")
            dh = project / "digital_human.mp4"
            mv = project / "main_video.mp4"
            dh.write_bytes(b"dh")
            mv.write_bytes(b"mv")
            run_dir = project / "runs" / "run1"
            run_dir.mkdir(parents=True)
            manifest = {
                "schema_version": 1,
                "run_id": "run1",
                "project": "demo",
                "status": "validated",
                "policy": {
                    "audio_speed": 1.30,
                    "max_final_duration": 59.5,
                    "end_padding_seconds": 1.2,
                    "whisper_language": "en",
                    "voiceover_min_chars": 260,
                    "voiceover_max_chars": 290,
                },
                "outputs": {
                    "html": str(html),
                    "text": str(text),
                    "final_audio": str(audio),
                    "final_video_sha256": upload_video.sha256_file(video),
                    "digital_human": str(dh),
                    "main_video": str(mv),
                    "duration": 50.0,
                },
                "tts_synthesis_report": {
                    "f0_audit": {"passed": True},
                    "content_acceptance": {"passed": True},
                },
            }
            (project / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (run_dir / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            args = SimpleNamespace(
                project=str(project), video=str(video), name="demo",
                style_plan=str(project / "plan.yaml"), style_history=str(project / "history.md"),
                title="title", desc="", dynamic="", tag="tag", public=False,
                reuse_pipeline_lock=True,
            )
            (project / "plan.yaml").write_text("project: demo\n", encoding="utf-8")
            (project / "history.md").write_text("# History\n", encoding="utf-8")
            with self.assertRaises(RuntimeError) as ctx:
                upload_video.execute(args)
            self.assertIn("whisper_language", str(ctx.exception))

    def test_append_style_history_lock_path_name(self):
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "STYLE_HISTORY.md"
            plan = Path(directory) / "STYLE_PLAN.yaml"
            plan_data = {
                "project": "testproj",
                **{dim: "test-slug" for dim in append_style.DIMENSIONS},
            }
            plan.write_text(json.dumps(plan_data), encoding="utf-8")
            append_style.append_history(plan, history)
            self.assertTrue((Path(directory) / "STYLE_HISTORY.md.lock").is_file())
            self.assertFalse((Path(directory) / "STYLE_HISTORY.md.lock.lock").is_file())


if __name__ == "__main__":
    unittest.main()
