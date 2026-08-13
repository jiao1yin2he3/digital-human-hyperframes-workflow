#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IndexTTS-2 Long Text Synthesis Pipeline with Male Voice Guarantee
Created for hyperframes-workflow.
"""

import os
import sys
import re
import wave
import argparse
import subprocess
import shutil
import tempfile
from pathlib import Path
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_TTS_ROOT = Path(os.environ.get("INDEX_TTS_ROOT", PROJECT_ROOT / "index-tts")).expanduser().resolve()
INDEX_TTS_CHECKPOINTS = INDEX_TTS_ROOT / "checkpoints"
sys.path.insert(0, str(INDEX_TTS_ROOT))
from indextts.infer_v2 import IndexTTS2

def f0_estim(sig, sr):
    fs = []
    for s in range(0, len(sig) - sr // 2, sr // 2):
        seg = sig[s:s + sr // 2]
        if np.max(np.abs(seg)) < 0.02:
            continue
        seg = seg * np.hanning(len(seg))
        ac = np.correlate(seg, seg, mode="full")[len(seg) - 1:]
        lo = int(sr / 300)
        hi = int(sr / 60)
        if hi >= len(ac):
            hi = len(ac) - 1
        if hi <= lo:
            continue
        p = np.argmax(ac[lo:hi]) + lo
        if ac[p] > 0:
            f = sr / p
            if 60 < f < 300:
                fs.append(f)
    if len(fs) == 0:
        return 0.0
    return float(np.median(fs))

def check_wav_f0(path, temp_dir):
    tmp = os.path.join(temp_dir, "_temp_f0_check.wav")
    # Resample to 16000Hz mono using ffmpeg for standard F0 estimation
    subprocess.run(["ffmpeg", "-y", "-i", path, "-ar", "16000", "-ac", "1", tmp], capture_output=True)
    try:
        if not os.path.exists(tmp):
            return 0.0
        with wave.open(tmp, 'rb') as w:
            sr = w.getframerate()
            x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        f0 = f0_estim(x, sr)
    except Exception as e:
        print(f"      [F0 Check Error] {e}")
        f0 = 0.0
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return f0

def split_text_to_segments(text, max_len=25):
    delimiters = "！!。？?；;，,:：\n"
    clauses = []
    current_clause = ""
    for char in text:
        current_clause += char
        if char in delimiters:
            clauses.append(current_clause)
            current_clause = ""
    if current_clause:
        clauses.append(current_clause)
        
    segments = []
    current_segment = ""
    for clause in clauses:
        clause_stripped = clause.strip()
        if not clause_stripped:
            continue
        if len(current_segment) + len(clause_stripped) <= max_len:
            current_segment += clause_stripped
        else:
            if current_segment:
                segments.append(current_segment)
            if len(clause_stripped) > max_len:
                for i in range(0, len(clause_stripped), max_len):
                    sub_clause = clause_stripped[i:i+max_len]
                    if len(sub_clause) < max_len:
                        current_segment = sub_clause
                    else:
                        segments.append(sub_clause)
            else:
                current_segment = clause_stripped
    if current_segment:
        segments.append(current_segment)
        
    return segments

def main():
    parser = argparse.ArgumentParser(description="IndexTTS-2 Stable Male Voice synthesis pipeline for long text.")
    parser.add_argument("-t", "--text", type=str, default=None, help="Text to synthesize")
    parser.add_argument("-r", "--ref_audio", type=str, default=str(PROJECT_ROOT / "cosyvoice" / "ref_user3.wav"), help="Reference audio path")
    parser.add_argument("-o", "--output", type=str, default=str(PROJECT_ROOT / "output" / "final_voiceover_male.wav"), help="Output audio path")
    parser.add_argument("--max_len", type=int, default=25, help="Max characters per segment")
    parser.add_argument(
        "--interval_silence",
        type=float,
        default=0.2,
        help="Inter-segment silence seconds passed to IndexTTS2.infer(interval_silence=...). "
        "Lower (0.08-0.1) tightens the stitched voiceover pauses.",
    )
    parser.add_argument(
        "--json-report",
        type=str,
        default=None,
        help="Optional path to write the per-segment synthesis report as JSON.",
    )
    parser.add_argument(
        "--emo-text",
        type=str,
        default=None,
        help="Optional IndexTTS2 text emotion prompt. Default: conservative news-anchor style.",
    )
    parser.add_argument(
        "--emo-audio-prompt",
        type=str,
        default=None,
        help="Optional separate emotion reference audio; not used by the default production config.",
    )
    parser.add_argument(
        "--emo-alpha",
        type=float,
        default=0.4,
        help="IndexTTS2 text-emotion strength, 0..1; 0.4 is the conservative news default.",
    )
    parser.add_argument(
        "--use-emo-text",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable IndexTTS2 text-emotion guidance (default: enabled).",
    )
    parser.add_argument(
        "--use-random",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Allow random emotion sampling (default: disabled to protect voice fidelity).",
    )
    parser.add_argument("--temperature", type=float, default=0.75)
    parser.add_argument("--top-p", type=float, default=0.85)
    parser.add_argument("--top-k", type=int, default=30)
    parser.add_argument("--repetition-penalty", type=float, default=10.0)
    parser.add_argument(
        "--required-keywords",
        type=str,
        default="",
        help="Comma-separated keywords that must survive Whisper content acceptance.",
    )
    parser.add_argument(
        "--whisper-python",
        type=str,
        default=os.environ.get("HFW_SYS_PY", str(PROJECT_ROOT / "venv" / "bin" / "python")),
        help="Python interpreter used for faster-whisper verification.",
    )
    args = parser.parse_args()

    default_text = (
        "重磅官宣！Open AI今天宣布：Chat GPT免费和Go用户，默认模型升级为GPT五点六露娜！"
        "下周起，免费用户文本对话无限次使用！作为GPT五点六家族最小一档，露娜主打快和便宜。"
        "免费档还首次新增思考按钮，遇到难题点一下，就能让模型深度推理。在金融、医疗、法律三大领域评测中，"
        "它的错误率大幅降低百分之六十二！注意，无限量仅限文本，图片生成、语音与文件上传额度照旧。"
        "另外，Plus和Pro用户同步升级索尔旗舰模型，新增思考深度滑块。"
        "圈内还爆料，下周Open AI可能发布下一代主力模型阿斯特拉！"
    )
    
    text = args.text if args.text else default_text
    ref_audio = args.ref_audio
    output_path = args.output
    max_len = args.max_len
    interval_silence = args.interval_silence
    json_report = args.json_report
    emo_text = args.emo_text
    emo_audio_prompt = args.emo_audio_prompt
    emo_alpha = args.emo_alpha
    use_emo_text = args.use_emo_text
    use_random = args.use_random
    temperature = args.temperature
    top_p = args.top_p
    top_k = args.top_k
    repetition_penalty = args.repetition_penalty
    required_keywords = [item.strip() for item in args.required_keywords.split(",") if item.strip()]
    whisper_python = args.whisper_python

    try:
        from scripts.tts_quality import (
            DEFAULT_NEWS_EMOTION_TEXT,
            build_index_tts_emotion_kwargs,
        )
    except ModuleNotFoundError:
        from tts_quality import DEFAULT_NEWS_EMOTION_TEXT, build_index_tts_emotion_kwargs

    emotion_kwargs = build_index_tts_emotion_kwargs(
        use_emo_text=use_emo_text,
        emo_text=emo_text,
        emo_alpha=emo_alpha,
        use_random=use_random,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        repetition_penalty=repetition_penalty,
        emo_audio_prompt=emo_audio_prompt,
    )

    print("==================================================")
    print("   IndexTTS-2 Long Text Stable Male Voice Pipeline")
    print("==================================================")
    print(f"Ref Audio: {ref_audio}")
    print(f"Output Path: {output_path}")
    print(f"Max segment length: {max_len}")
    print(f"Inter-segment silence: {interval_silence:.3f}s")
    print(f"Emotion guidance: use_emo_text={emotion_kwargs['use_emo_text']} alpha={emotion_kwargs['emo_alpha']:.2f} random={emotion_kwargs['use_random']} static_emo_text={'yes' if 'emo_text' in emotion_kwargs else 'no'} emo_audio_prompt={'yes' if 'emo_audio_prompt' in emotion_kwargs else 'no'}")
    print(f"Generation sampling: temperature={emotion_kwargs['temperature']:.3f} top_p={emotion_kwargs['top_p']:.3f} top_k={emotion_kwargs['top_k']} repetition_penalty={emotion_kwargs['repetition_penalty']:.3f}")
    print(f"Required content keywords: {required_keywords}")
    print("--------------------------------------------------")

    # 验证参考音频存在
    if not os.path.exists(ref_audio):
        print(f"❌ Error: Reference audio not found at {ref_audio}")
        sys.exit(1)

    ffprobe_bin = _resolve_ffprobe()

    # 1. 分句
    segments = split_text_to_segments(text, max_len)
    print(f"Text split into {len(segments)} segments:")
    for idx, seg in enumerate(segments):
        print(f"  {idx+1}: {seg} (len={len(seg)})")
    print("--------------------------------------------------")

    # 2. 准备临时目录
    output_root = PROJECT_ROOT / "output"
    output_root.mkdir(parents=True, exist_ok=True)
    temp_dir = tempfile.mkdtemp(prefix="tts-segments-", dir=output_root)

    # 3. 加载模型
    print("Loading IndexTTS-2 model...")
    tts = IndexTTS2(
        cfg_path=str(INDEX_TTS_CHECKPOINTS / "config.yaml"),
        model_dir=str(INDEX_TTS_CHECKPOINTS),
        use_fp16=False,
        use_cuda_kernel=False,
        use_deepspeed=False
    )
    print("✅ Model loaded successfully")
    print("--------------------------------------------------")

    segment_files = []
    segment_f0s = []

    # 4. 逐段生成（含重试逻辑）
    for idx, seg in enumerate(segments):
        print(f"\n[Segment {idx+1}/{len(segments)}] Synthesizing: \"{seg}\"")
        seg_file = None
        best_attempt_file = None
        best_attempt_f0 = 999.0
        best_attempt_diff = 999.0

        for attempt in range(1, 4):
            temp_seg_file = os.path.join(temp_dir, f"seg_{idx}_attempt_{attempt}.wav")
            print(f"  Attempt {attempt}/3...")
            
            tts.infer(
                spk_audio_prompt=ref_audio,
                text=seg,
                output_path=temp_seg_file,
                verbose=False,
                interval_silence=int(round(interval_silence * 1000)),
                **emotion_kwargs,
            )
            
            if os.path.exists(temp_seg_file):
                f0 = check_wav_f0(temp_seg_file, temp_dir)
                print(f"    -> F0: {f0:.1f} Hz")
                
                # Check if in male range 80-180Hz (we also support 60-185Hz as in check_gender3.py)
                is_male = 80 <= f0 <= 180
                
                # Track best attempt in case all 3 fail (closest to male range 130Hz)
                diff = abs(f0 - 130.0)
                if diff < best_attempt_diff:
                    best_attempt_diff = diff
                    best_attempt_f0 = f0
                    best_attempt_file = os.path.join(temp_dir, f"seg_{idx}_best_fallback.wav")
                    shutil.copy(temp_seg_file, best_attempt_file)
                
                if is_male:
                    seg_file = os.path.join(temp_dir, f"seg_{idx}.wav")
                    os.rename(temp_seg_file, seg_file)
                    segment_f0s.append((seg, f0, attempt))
                    print(f"    ✅ Success! (Attempt {attempt}, F0={f0:.1f}Hz is in male range)")
                    break
                else:
                    print(f"    ⚠️ Warning: F0={f0:.1f}Hz is outside male range (80-180Hz).")
                    if attempt < 3:
                        os.remove(temp_seg_file)
            else:
                print("    ❌ Error: Audio file not generated on this attempt.")

        # Fallback if all attempts failed
        if seg_file is None:
            if best_attempt_file and os.path.exists(best_attempt_file):
                seg_file = os.path.join(temp_dir, f"seg_{idx}.wav")
                os.rename(best_attempt_file, seg_file)
                segment_f0s.append((seg, best_attempt_f0, -1)) # -1 signifies fallback
                print(f"    ⚠️ All attempts failed to hit male range. Falling back to best attempt: F0={best_attempt_f0:.1f}Hz")
            else:
                print(f"    ❌ Critical Error: Failed to generate audio for segment {idx+1}")
                sys.exit(1)
        
        segment_files.append(seg_file)

    print("\n--------------------------------------------------")
    print("All segments synthesized successfully. Starting concatenation...")

    # 5. 拼接
    # Create input file list for ffmpeg
    inputs_txt_path = os.path.join(temp_dir, "inputs.txt")
    with open(inputs_txt_path, "w", encoding="utf-8") as f:
        for file in segment_files:
            escaped_file = file.replace("'", "'\\''")
            f.write(f"file '{escaped_file}'\n")

    # Run ffmpeg concat
    concat_cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", inputs_txt_path, "-c", "copy", output_path]
    print(f"Running ffmpeg: {' '.join(concat_cmd)}")
    res = subprocess.run(concat_cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print("❌ Ffmpeg concat failed:")
        print(res.stderr)
        sys.exit(1)

    print(f"✅ Audio concatenated and saved to: {output_path}")

    # 6. 验证最终音频
    print("\n--------------------------------------------------")
    print("Verifying final concatenated audio...")

    # 时长
    dur_res = subprocess.run([ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", output_path], capture_output=True, text=True)
    if dur_res.returncode != 0 or not dur_res.stdout.strip():
        print(f"❌ ffprobe failed: {dur_res.stderr}")
        sys.exit(1)
    duration = float(dur_res.stdout.strip())
    
    # 最终 F0
    final_f0 = check_wav_f0(output_path, temp_dir)

    
    # 转写 (faster-whisper)
    print("Running faster-whisper transcription...")
    whisper_cmd = [
        str(whisper_python), "-c",
        "from faster_whisper import WhisperModel; "
        "model = WhisperModel('base', device='cpu', compute_type='int8'); "
        "segments, info = model.transcribe('{}', language='zh'); "
        "print(''.join(seg.text for seg in segments))".format(output_path)
    ]
    trans_res = subprocess.run(whisper_cmd, capture_output=True, text=True)
    if trans_res.returncode != 0:
        print(f"❌ Whisper transcription failed: {trans_res.stderr}")
        sys.exit(1)
    transcription = trans_res.stdout.strip()

    # 6b. 逐段 F0 审计 + 内容验收（写入 JSON 供流水线门禁使用）
    try:
        from scripts.tts_quality import audit_segment_f0s, content_acceptance
    except ModuleNotFoundError:
        from tts_quality import audit_segment_f0s, content_acceptance

    f0_audit = audit_segment_f0s(segment_f0s)
    content = content_acceptance(text, transcription, required_ratio=0.7, keywords=required_keywords)

    report = {
        "output_path": str(output_path),
        "duration": duration,
        "final_f0": final_f0,
        "emotion_control": emotion_kwargs,
        "f0_audit": f0_audit,
        "content_acceptance": content,
        "segments": [
            {"index": idx + 1, "text": seg, "f0": f0, "attempt": att}
            for idx, (seg, f0, att) in enumerate(segment_f0s)
        ],
        "transcription": transcription,
    }
    if json_report:
        import json as _json

        with open(json_report, "w", encoding="utf-8") as handle:
            _json.dump(report, handle, ensure_ascii=False, indent=2)
        print(f"✅ Synthesis report written to: {json_report}")

    print("\n================== SYNTHESIS REPORT ==================")
    print(f"1. Script Path: {os.path.abspath(__file__)}")
    print(f"2. Reference Audio: {ref_audio}")
    print(f"3. Output Audio Path: {output_path}")
    print(f"4. Total Duration: {duration:.2f}s")
    print(f"5. Final F0: {final_f0:.1f} Hz ({'Male' if 80 <= final_f0 <= 180 else 'Drifted/Female/Unknown'})")
    print(f"6. Per-segment F0 audit: fallback={f0_audit['fallback_count']} "
          f"male_ratio={f0_audit['male_ratio']:.3f} passed={f0_audit['passed']}")
    print(f"7. Content acceptance: similarity={content['similarity']:.3f} "
          f"missing_keywords={content['missing_keywords']} passed={content['passed']}")
    print("8. Segment details:")
    for idx, (seg, f0, att) in enumerate(segment_f0s):
        att_str = f"Attempt {att}" if att != -1 else "Fallback (Best attempt)"
        print(f"   - Seg {idx+1}: F0={f0:.1f}Hz | {att_str} | \"{seg}\"")
    print("9. Transcription:")
    print(transcription)
    print("======================================================")

    # 7. 清理
    shutil.rmtree(temp_dir)
    print("\nTemporary files cleaned up successfully.")


def _resolve_ffprobe():
    for candidate in ("ffprobe", "/usr/local/bin/ffprobe", "/opt/homebrew/bin/ffprobe"):
        from shutil import which

        if which(candidate):
            return candidate
    return "ffprobe"

if __name__ == '__main__':
    main()
