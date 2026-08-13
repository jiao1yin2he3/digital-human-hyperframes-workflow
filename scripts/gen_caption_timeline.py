#!/usr/bin/env python3
"""基于 Whisper 时间戳生成字幕时间轴（真·时间戳对齐版）。

用法:
  python3 scripts/gen_caption_timeline.py \
    --audio projects/<topic>/audio/voiceover_130.wav \
    --script projects/<topic>/口播稿.txt \
    --output projects/<topic>/captions.json
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

def whisper_transcribe(audio_path: str, model_size="base", language="zh"):
    """使用 faster-whisper 转录音频，返回带时间戳的片段列表。"""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(audio_path, language=language, beam_size=5)

    result = []
    for seg in segments:
        result.append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": seg.text.strip()
        })
    return result

def split_script(script: str) -> list[str]:
    """按中文标点拆分脚本为句子列表。"""
    sentences = re.split(r'([。！？；])', script)
    merged = []
    i = 0
    while i < len(sentences):
        if sentences[i].strip():
            if i + 1 < len(sentences) and sentences[i + 1].strip() in '。！？；':
                merged.append(sentences[i].strip() + sentences[i + 1].strip())
                i += 2
            else:
                merged.append(sentences[i].strip())
                i += 1
        else:
            i += 1
    return [s for s in merged if s.strip()]

def normalize(s: str) -> str:
    """去掉所有非中文字符用于模糊匹配（数字也保留，因为口播常用阿拉伯数字）。"""
    return re.sub(r'[^\u4e00-\u9fff0-9a-zA-Z]', '', s)

def map_captions(whisper_data: list[dict], sentences: list[str]) -> list[dict]:
    """将脚本句子映射到 Whisper 时间戳（真对齐）。

    策略：
    1. 优先用文本前缀匹配 Whisper 全文，匹配上的句子直接采用 Whisper 段时间。
    2. 匹配失败（因 ASR 错字导致口播稿与转写文本差异大）时，按脚本文本累计位置
       映射到 Whisper 全文，并在对应 Whisper 段内插值，避免把整段音频均分给句子。

    核心铁律：字幕时间优先来自 Whisper 实际语音时间戳；兜底也必须落在 Whisper 段内。
    """
    if not whisper_data or not sentences:
        return []

    total_audio = whisper_data[-1]["end"]

    # 把 whisper 转写拼接成全文，记录每段的边界
    full_text = ""
    seg_bounds = []  # (start, end, text)
    for seg in whisper_data:
        t = seg["text"]
        full_text += t
        seg_bounds.append((seg["start"], seg["end"], t))

    # 归一化全文，便于匹配
    full_norm = normalize(full_text)
    if not full_norm:
        return []
    script_norm_lengths = [len(normalize(sentence)) for sentence in sentences]
    script_total = sum(script_norm_lengths)

    captions = []
    pos = 0  # 在 full_norm 中的位置
    prev_end = 0.0

    script_cursor = 0
    def time_at(pos_abs, edge='start'):
        cum = 0
        for i, (s, e, t) in enumerate(seg_bounds):
            n = len(normalize(t))
            if n > 0:
                is_last = (i == len(seg_bounds) - 1)
                if edge == 'end' and pos_abs == cum + n:
                    return e
                if (cum <= pos_abs < cum + n) or (is_last and pos_abs == cum + n):
                    char_offset = pos_abs - cum
                    return s + (e - s) * (char_offset / n)
                cum += n
        return whisper_data[-1]["end"]

    for sent, script_length in zip(sentences, script_norm_lengths):
        sent_norm = normalize(sent)
        if not sent_norm:
            continue

        # 在 full_norm 中从 pos 开始找 sent_norm 的匹配
        # 优先整句包含匹配；失败则退化为"子串 + 错字容忍"匹配：
        # 把口播稿句子按 2-gram 拆分，在 Whisper 全文里找连续命中率最高的窗口
        found = False
        start = end = pos
        window = full_norm[pos:pos + 200]
        idx = window.find(sent_norm)
        if idx >= 0:
            start = pos + idx
            end = start + len(sent_norm)
            found = True
        else:
            # 错字容忍：用句子前 6 个中文字符做"锚点"前缀匹配（ASR 常错同音字，
            # 但句首实体词往往保留）。若找到锚点，认为该句大致位于此位置。
            cn_chars = normalize(sent_norm)  # 仅留中文+数字字母
            if len(cn_chars) >= 4:
                idx2 = window.find(cn_chars[:6])
                if idx2 >= 0:
                    start = pos + idx2
                    end = start + len(sent_norm)
                    found = True

        if found:
            cap_start = time_at(start, 'start')
            cap_end = time_at(end, 'end')
        else:
            # 兜底仍映射到 Whisper 字符位置，再在对应段内插值；不按句子数量均分音频。
            norm_start = int(round((script_cursor / max(1, script_total)) * len(full_norm)))
            norm_end = int(round(((script_cursor + script_length) / max(1, script_total)) * len(full_norm)))
            cap_start = time_at(norm_start, "start")
            cap_end = time_at(max(norm_start + 1, norm_end), "end")

        # 钳制 + 最小时长
        cap_start = max(cap_start, prev_end)
        if cap_end - cap_start < 0.5:
            cap_end = min(cap_start + 1.2, total_audio)
        cap_end = min(cap_end, total_audio)

        captions.append({
            "start": round(cap_start, 2),
            "end": round(cap_end, 2),
            "text": sent
        })

        prev_end = cap_end
        if found:
            pos = end
        else:
            pos = max(pos, int(round(((script_cursor + script_length) / max(1, script_total)) * len(full_norm))))
        script_cursor += script_length

    return captions

def main():
    parser = argparse.ArgumentParser(description="生成字幕时间轴")
    parser.add_argument("--audio", required=True, help="定稿音频路径")
    parser.add_argument("--script", required=True, help="口播稿路径")
    parser.add_argument("--output", required=True, help="输出 JSON 路径")
    parser.add_argument("--model", default="base", help="Whisper 模型大小")
    args = parser.parse_args()

    # 转录
    print(f"📝 Whisper 转录: {args.audio}")
    whisper_data = whisper_transcribe(args.audio, args.model)
    print(f"   → {len(whisper_data)} 段，总时长 {whisper_data[-1]['end']:.2f}s")

    # 拆分脚本
    script = Path(args.script).read_text(encoding="utf-8").strip()
    sentences = split_script(script)
    print(f"📝 脚本拆分: {len(sentences)} 句，共 {len(script)} 字符")

    # 映射
    captions = map_captions(whisper_data, sentences)
    print(f"📝 生成字幕: {len(captions)} 条")
    for cap in captions:
        print(f"   [{cap['start']:.2f}-{cap['end']:.2f}s] {cap['text'][:40]}")

    # 保存
    output = {
        "captions": captions,
        "duration": whisper_data[-1]["end"]
    }
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 已保存到 {args.output}")

if __name__ == "__main__":
    main()
