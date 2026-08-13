"""TDD tests for TTS quality improvements.

These tests drive the improvements described in the 2026-08-12 TTS enhancement
work: configurable inter-segment silence, per-segment F0 auditing, and a script
vs Whisper content-acceptance gate. The functions under test live in
``scripts/tts_quality.py`` which is intentionally absent until the tests below
are written first (RED phase).
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from scripts.tts_quality import (
    analyze_silences,
    interval_silence_to_pad,
    audit_segment_f0s,
    normalize_cn,
    traditional_to_simplified_map,
    content_acceptance,
    build_speed_command,
    build_index_tts_emotion_kwargs,
    DEFAULT_NEWS_EMOTION_TEXT,
    validate_generation_params,
    effective_voiceover_chars,
    duration_gate,
)


AUDIO_SAMPLE = """
[Parsed_silencedetect_0 @ 0xabc] silence_start: 0
[Parsed_silencedetect_0 @ 0xabc] silence_end: 0.274146 | silence_duration: 0.274146
[Parsed_silencedetect_0 @ 0xabc] silence_start: 1.516104
[Parsed_silencedetect_0 @ 0xabc] silence_end: 1.893146 | silence_duration: 0.377042
[Parsed_silencedetect_0 @ 0xabc] silence_start: 66.275828
[Parsed_silencedetect_0 @ 0xabc] silence_end: 66.931202 | silence_duration: 0.655374
"""

SPEED_FFMPEG_OUTPUT = """
Input #0, wav, from 'voiceover_natural.wav':
  Duration: 00:01:14.48, bitrate: 705 kb/s
""".strip()


class SilenceAnalysisTests(unittest.TestCase):
    def test_parses_silence_durations(self):
        result = analyze_silences(AUDIO_SAMPLE)
        self.assertEqual(result["segments"], 3)
        self.assertAlmostEqual(result["sum_silence"], 0.274146 + 0.377042 + 0.655374, places=4)
        self.assertEqual(result["over_0_3"], 2)
        self.assertEqual(result["over_0_4"], 1)
        self.assertAlmostEqual(result["max_silence"], 0.655374, places=4)

    def test_empty_input_has_zero_segments(self):
        result = analyze_silences("no silence here")
        self.assertEqual(result["segments"], 0)
        self.assertEqual(result["sum_silence"], 0.0)
        self.assertEqual(result["over_0_3"], 0)


class IntervalSilenceMappingTests(unittest.TestCase):
    def test_100ms_maps_to_100ms_in_milliseconds(self):
        self.assertEqual(interval_silence_to_pad(0.1), 100)

    def test_default_maps_to_current_pipeline_value(self):
        # Current tts_male_pipeline uses IndexTTS2.infer default 200ms.
        self.assertEqual(interval_silence_to_pad(0.2), 200)

    def test_invalid_negative_raises(self):
        with self.assertRaises(ValueError):
            interval_silence_to_pad(-0.1)


class SegmentF0AuditTests(unittest.TestCase):
    def test_all_male_no_fallback_passes(self):
        segments = [("一句", 117.0, 1), ("两句", 121.0, 2), ("三句", 113.0, 1)]
        report = audit_segment_f0s(segments, male_min=80, male_max=180, target=130.0)
        self.assertEqual(report["fallback_count"], 0)
        self.assertEqual(report["male_ratio"], 1.0)
        self.assertTrue(report["passed"])

    def test_single_fallback_fails_audit(self):
        segments = [("一句", 117.0, 1), ("两句", 230.0, -1)]
        report = audit_segment_f0s(segments, male_min=80, male_max=180, target=130.0)
        self.assertEqual(report["fallback_count"], 1)
        self.assertAlmostEqual(report["male_ratio"], 0.5, places=6)
        self.assertFalse(report["passed"])

    def test_mostly_male_with_one_drift_fails_but_reports_ratio(self):
        segments = [("a", 117.0, 1), ("b", 119.0, 1), ("c", 188.0, 3)]
        report = audit_segment_f0s(segments, male_min=80, male_max=180, target=130.0)
        self.assertAlmostEqual(report["male_ratio"], 2 / 3, places=6)
        self.assertFalse(report["passed"])


class ChineseNormalizationTests(unittest.TestCase):
    def test_strips_punctuation_and_spaces(self):
        self.assertEqual(normalize_cn("你好，世界！"), "你好世界")

    def test_traditional_mapped_to_simplified(self):
        text = "這是一個測試"
        mapping = traditional_to_simplified_map()
        converted = "".join(mapping.get(ch, ch) for ch in text)
        self.assertEqual(converted, "这是一个测试")

    def test_mapping_covers_common_social_media_chars(self):
        mapping = traditional_to_simplified_map()
        for tra, sim in [("這", "这"), ("個", "个"), ("劇", "剧"), ("億", "亿"), ("聲", "声")]:
            self.assertEqual(mapping.get(tra), sim)


class ContentAcceptanceTests(unittest.TestCase):
    def test_exact_match_passes(self):
        script = "一个人做的AI短剧，播放破防了"
        whisper = "一个人做的AI短剧，播放破防了"
        result = content_acceptance(script, whisper, required_ratio=0.7)
        self.assertTrue(result["passed"])
        self.assertGreaterEqual(result["similarity"], 0.99)

    def test_traditional_whisper_still_matches_simplified_script(self):
        script = "一个人做的AI短剧，播放破防了"
        whisper = "一個人做的AI短劇，播放破防了"
        result = content_acceptance(script, whisper, required_ratio=0.7)
        self.assertTrue(result["passed"])

    def test_missing_keywords_fails(self):
        script = "门槛降了，天花板没降"
        whisper = "今天天气不错，我们聊点别的"
        result = content_acceptance(
            script, whisper, required_ratio=0.7, keywords=["门槛", "天花板"]
        )
        self.assertFalse(result["passed"])
        self.assertIn("门槛", result["missing_keywords"])

    def test_low_similarity_fails(self):
        script = "AI不是一件生成的魔法，真正决定成片的，是剧本"
        whisper = "今天我们来聊聊美食和旅游"
        result = content_acceptance(script, whisper, required_ratio=0.7)
        self.assertFalse(result["passed"])

    def test_partial_keyword_loss_fails(self):
        script = "归墟作者江兰是个35岁的宝妈"
        whisper = "作者江兰是个35岁的宝妈"
        result = content_acceptance(
            script, whisper, required_ratio=0.7, keywords=["归墟"]
        )
        self.assertFalse(result["passed"])
        self.assertIn("归墟", result["missing_keywords"])


class SpeedCommandTests(unittest.TestCase):
    def test_speed_command_uses_configured_speed(self):
        cmd = build_speed_command(
            input_path="natural.wav",
            output_path="sped.wav",
            speed=1.18,
            loudness_i=-14,
            true_peak=-1.0,
            lra=8,
        )
        self.assertIn("atempo=1.18", cmd)
        self.assertIn("loudnorm=I=-14:TP=-1:LRA=8", cmd)
        self.assertIn("natural.wav", cmd)
        self.assertIn("sped.wav", cmd)

    def test_speed_command_avoids_135_filename_mismatch(self):
        # The production artifact is explicitly named for the configured 1.30x speed.
        cmd = build_speed_command("natural.wav", "voiceover_130.wav")
        self.assertIn("atempo=1.3", cmd)
        self.assertNotIn("atempo=1.18", cmd)


class DurationPolicyTests(unittest.TestCase):
    def test_effective_chars_ignore_spaces_newlines_and_punctuation(self):
        self.assertEqual(effective_voiceover_chars("你好，世界！\nAI 1.30"), 9)

    def test_production_speed_default_is_1_30(self):
        self.assertIn("atempo=1.3", build_speed_command("natural.wav", "voiceover_130.wav"))

    def test_duration_gate_passes_under_59_5_seconds(self):
        result = duration_gate(audio_duration=58.0, end_padding=1.2, max_final_duration=59.5)
        self.assertTrue(result["passed"])
        self.assertAlmostEqual(result["estimated_final_duration"], 59.2)

    def test_duration_gate_fails_before_expensive_render(self):
        result = duration_gate(audio_duration=58.4, end_padding=1.2, max_final_duration=59.5)
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "estimated_final_duration_exceeds_limit")

    def test_duration_gate_rejects_negative_values(self):
        with self.assertRaises(ValueError):
            duration_gate(audio_duration=-1.0, end_padding=1.2, max_final_duration=59.5)


class IndexTTS2EmotionControlTests(unittest.TestCase):
    def test_news_emotion_kwargs_use_fixed_prompt_without_randomness(self):
        kwargs = build_index_tts_emotion_kwargs(emo_text=DEFAULT_NEWS_EMOTION_TEXT)
        self.assertTrue(kwargs["use_emo_text"])
        self.assertEqual(kwargs["emo_alpha"], 0.4)
        self.assertFalse(kwargs["use_random"])
        self.assertEqual(kwargs["emo_text"], DEFAULT_NEWS_EMOTION_TEXT)
        self.assertEqual(kwargs["temperature"], 0.75)
        self.assertEqual(kwargs["top_p"], 0.85)
        self.assertEqual(kwargs["top_k"], 30)
        self.assertEqual(kwargs["repetition_penalty"], 10.0)

    def test_default_without_explicit_prompt_uses_auto_mode(self):
        kwargs = build_index_tts_emotion_kwargs()
        self.assertTrue(kwargs["use_emo_text"])
        self.assertNotIn("emo_text", kwargs)
        self.assertNotIn("emo_audio_prompt", kwargs)

    def test_static_news_emotion_prompt_is_supported_as_override(self):
        kwargs = build_index_tts_emotion_kwargs(
            emo_text=DEFAULT_NEWS_EMOTION_TEXT,
            emo_alpha=0.4,
        )
        self.assertEqual(kwargs["emo_text"], DEFAULT_NEWS_EMOTION_TEXT)

    def test_emotion_audio_prompt_is_optional_and_separate(self):
        kwargs = build_index_tts_emotion_kwargs(
            use_emo_text=False,
            emo_audio_prompt="emotion.wav",
            emo_alpha=0.8,
        )
        self.assertEqual(kwargs["emo_audio_prompt"], "emotion.wav")
        self.assertFalse(kwargs["use_emo_text"])
        self.assertEqual(kwargs["emo_alpha"], 0.8)

    def test_generation_params_are_validated(self):
        params = validate_generation_params(0.7, 0.85, 25, 10.0)
        self.assertEqual(params, {
            "temperature": 0.7,
            "top_p": 0.85,
            "top_k": 25,
            "repetition_penalty": 10.0,
        })

    def test_invalid_sampling_parameters_fail_closed(self):
        with self.assertRaises(ValueError):
            validate_generation_params(-0.1, 0.8, 30, 10.0)
        with self.assertRaises(ValueError):
            validate_generation_params(0.8, 1.1, 30, 10.0)
        with self.assertRaises(ValueError):
            validate_generation_params(0.8, 0.8, -1, 10.0)
        with self.assertRaises(ValueError):
            validate_generation_params(0.8, 0.8, 30, 0.0)

    def test_empty_explicit_emo_text_fails_when_text_mode_enabled(self):
        with self.assertRaises(ValueError):
            build_index_tts_emotion_kwargs(use_emo_text=True, emo_text="")

    def test_emotion_alpha_range_is_enforced(self):
        with self.assertRaises(ValueError):
            build_index_tts_emotion_kwargs(emo_alpha=1.1)


if __name__ == "__main__":
    unittest.main()
