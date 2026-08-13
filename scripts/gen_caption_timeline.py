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

def _redistribute_and_validate(raw_items: list[dict], total_audio: float) -> list[dict]:
    if not raw_items:
        return []

    n = len(raw_items)
    min_dur = 0.05
    min_needed = n * 0.01

    if total_audio < min_needed:
        raise ValueError(f"音频时长 ({total_audio:.3f}s) 太短，无法分配给 {n} 句字幕")

    captions = []
    prev_end = 0.0

    for i, item in enumerate(raw_items):
        rem_count = n - i
        max_allowed_end = total_audio - (rem_count - 1) * 0.01

        s = max(item["start"], prev_end)
        if s >= max_allowed_end:
            s = max(prev_end, max_allowed_end - 0.01)

        e = max(item["end"], s + min_dur)
        if e > max_allowed_end:
            e = max_allowed_end

        if s >= e or (e - s) < 0.01:
            rem_span = max_allowed_end - prev_end
            if rem_span < 0.01 * rem_count:
                raise ValueError(f"无法为第 {i+1} 句脚本分配有效字幕区间 ({s:.2f}s-{e:.2f}s)")
            alloc_dur = max(0.01, rem_span / rem_count)
            s = prev_end
            e = min(max_allowed_end, s + alloc_dur)

        r_start = round(s, 2)
        r_end = round(e, 2)
        if r_start >= r_end:
            r_end = round(r_start + 0.01, 2)
            if r_end > total_audio:
                r_start = round(total_audio - 0.01, 2)
                r_end = round(total_audio, 2)

        if r_start >= r_end or r_end > round(total_audio, 2) or r_start < 0:
            raise ValueError(f"无法生成有效正时长字幕: [{r_start}-{r_end}s] {item['text']}")

        captions.append({
            "start": r_start,
            "end": r_end,
            "text": item["text"]
        })
        prev_end = e

    return captions


def map_captions(whisper_data: list[dict], sentences: list[str]) -> list[dict]:
    """将脚本句子映射到 Whisper 时间戳（真对齐）。"""
    if not sentences:
        return []
    if not whisper_data:
        raise ValueError("Whisper 转写数据为空")

    total_audio = max((seg.get("end", 0.0) for seg in whisper_data), default=0.0)
    if total_audio <= 0:
        raise ValueError(f"Whisper 音频总时长无效: {total_audio}")

    full_text = ""
    seg_bounds = []
    for seg in whisper_data:
        t = seg.get("text", "")
        full_text += t
        seg_bounds.append((seg.get("start", 0.0), seg.get("end", 0.0), t))

    full_norm = normalize(full_text)
    script_norm_lengths = [len(normalize(sentence)) for sentence in sentences]
    script_total = sum(script_norm_lengths)
    valid_sentence_count = sum(1 for l in script_norm_lengths if l > 0)

    if valid_sentence_count == 0:
        return []

    if not full_norm or script_total == 0:
        raw_items = []
        cum_len = 0
        for sent, slen in zip(sentences, script_norm_lengths):
            if slen == 0:
                continue
            s = total_audio * (cum_len / script_total)
            cum_len += slen
            e = total_audio * (cum_len / script_total)
            raw_items.append({"start": s, "end": e, "text": sent})
        return _redistribute_and_validate(raw_items, total_audio)

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
                    return s + (e - s) * (char_offset / n) if e > s else s
                cum += n
        return total_audio

    pos = 0
    script_cursor = 0
    raw_items = []

    for sent, script_length in zip(sentences, script_norm_lengths):
        sent_norm = normalize(sent)
        if not sent_norm:
            continue

        found = False
        start = end = pos
        window = full_norm[pos:pos + 200]
        idx = window.find(sent_norm)
        if idx >= 0:
            start = pos + idx
            end = start + len(sent_norm)
            found = True
        else:
            cn_chars = normalize(sent_norm)
            if len(cn_chars) >= 4:
                idx2 = window.find(cn_chars[:6])
                if idx2 >= 0:
                    start = pos + idx2
                    end = start + len(sent_norm)
                    found = True

        if found:
            cap_start = time_at(start, 'start')
            cap_end = time_at(end, 'end')
            pos = end
        else:
            norm_start = int(round((script_cursor / max(1, script_total)) * len(full_norm)))
            norm_end = int(round(((script_cursor + script_length) / max(1, script_total)) * len(full_norm)))
            cap_start = time_at(norm_start, "start")
            cap_end = time_at(max(norm_start + 1, norm_end), "end")
            pos = max(pos, int(round(((script_cursor + script_length) / max(1, script_total)) * len(full_norm))))

        raw_items.append({
            "start": cap_start,
            "end": cap_end,
            "text": sent
        })
        script_cursor += script_length

    return _redistribute_and_validate(raw_items, total_audio)

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
