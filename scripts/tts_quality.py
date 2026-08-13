"""TTS quality and IndexTTS2 control helpers.

The helpers in this module are dependency-free so they can be tested in the
workflow Python 3.9 environment. Model inference remains in the IndexTTS2
virtualenv; this module only validates and assembles its arguments.
"""

import difflib
import re


# IndexTTS2's emotion vector order from the upstream infer_v2.py implementation:
# [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm].
DEFAULT_NEWS_EMOTION_TEXT = (
    "平静、专业、克制的科技新闻播报。重点信息略微强调，遇到结论时有一点自然的强调，"
    "但不要夸张、不要激动、不要悲伤，保持清晰和可信。"
)

# Conservative sampling values for a stable cloned news voice. IndexTTS2's upstream
# infer_v2 defaults are temperature=0.8, top_p=0.8, top_k=30,
# repetition_penalty=10.0. The production profile makes only a small, bounded
# adjustment: slightly lower temperature and slightly wider top-p for smoother
# prosodic variation while retaining deterministic emotion selection.
DEFAULT_NEWS_GENERATION = {
    "temperature": 0.75,
    "top_p": 0.85,
    "top_k": 30,
    "repetition_penalty": 10.0,
}


def analyze_silences(ffmpeg_output: str) -> dict:
    """Parse ``ffmpeg silencedetect`` stderr into aggregate stats."""
    durations = [
        float(value)
        for value in re.findall(r"silence_duration:\s*([0-9]*\.[0-9]+)", ffmpeg_output)
    ]
    if not durations:
        return {
            "segments": 0,
            "sum_silence": 0.0,
            "max_silence": 0.0,
            "over_0_3": 0,
            "over_0_4": 0,
        }
    return {
        "segments": len(durations),
        "sum_silence": round(sum(durations), 4),
        "max_silence": round(max(durations), 4),
        "over_0_3": sum(1 for d in durations if d > 0.3),
        "over_0_4": sum(1 for d in durations if d > 0.4),
    }


def interval_silence_to_pad(seconds: float) -> int:
    """Convert IndexTTS2 interval silence seconds to milliseconds."""
    if seconds < 0:
        raise ValueError("interval_silence must be non-negative")
    return int(round(seconds * 1000))


def validate_generation_params(temperature, top_p, top_k, repetition_penalty):
    """Validate IndexTTS2 sampling parameters and return normalized values."""
    temperature = float(temperature)
    top_p = float(top_p)
    top_k = int(top_k)
    repetition_penalty = float(repetition_penalty)
    if temperature < 0.0:
        raise ValueError("temperature must be >= 0")
    if not 0.0 < top_p <= 1.0:
        raise ValueError("top_p must be in (0, 1]")
    if top_k < 0:
        raise ValueError("top_k must be >= 0")
    if repetition_penalty <= 0.0:
        raise ValueError("repetition_penalty must be > 0")
    return {
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "repetition_penalty": repetition_penalty,
    }


def build_index_tts_emotion_kwargs(
    use_emo_text=True,
    emo_text=None,
    emo_alpha=0.4,
    use_random=False,
    temperature=DEFAULT_NEWS_GENERATION["temperature"],
    top_p=DEFAULT_NEWS_GENERATION["top_p"],
    top_k=DEFAULT_NEWS_GENERATION["top_k"],
    repetition_penalty=DEFAULT_NEWS_GENERATION["repetition_penalty"],
    emo_audio_prompt=None,
):
    """Build safe ``IndexTTS2.infer`` kwargs for a news narration.

    With ``use_emo_text=True`` and no explicit ``emo_text``, IndexTTS2 analyzes
    each actual text segment. Upstream recommends alpha around 0.6 or lower; this
    production profile uses 0.4 after an isolated A/B. ``use_random=False``
    protects speaker fidelity. No emotion reference audio is supplied by default.
    """
    if not 0.0 <= float(emo_alpha) <= 1.0:
        raise ValueError("emo_alpha must be in [0, 1]")
    params = validate_generation_params(temperature, top_p, top_k, repetition_penalty)
    result = {
        "use_emo_text": bool(use_emo_text),
        "emo_alpha": float(emo_alpha),
        "use_random": bool(use_random),
        **params,
    }
    if emo_audio_prompt:
        if not isinstance(emo_audio_prompt, str):
            raise ValueError("emo_audio_prompt must be a path string")
        result["emo_audio_prompt"] = emo_audio_prompt
    if use_emo_text:
        if emo_text is not None:
            if not isinstance(emo_text, str) or not emo_text.strip():
                raise ValueError("emo_text must be non-empty when explicitly provided")
            result["emo_text"] = emo_text.strip()
    return result


def audit_segment_f0s(segments, male_min=80.0, male_max=180.0, target=130.0) -> dict:
    """Audit per-segment ``(text, f0, attempt)`` tuples."""
    if not segments:
        return {
            "segments": 0,
            "fallback_count": 0,
            "male_ratio": 0.0,
            "male_f0s": [],
            "drifted": [],
            "passed": False,
        }
    male_f0s = []
    drifted = []
    fallback_count = 0
    for text, f0, attempt in segments:
        if attempt == -1:
            fallback_count += 1
            drifted.append({"text": text, "f0": f0, "attempt": attempt})
        elif male_min <= f0 <= male_max:
            male_f0s.append(f0)
        else:
            drifted.append({"text": text, "f0": f0, "attempt": attempt})
    male_ratio = len(male_f0s) / len(segments)
    passed = fallback_count == 0 and male_ratio >= 0.9
    return {
        "segments": len(segments),
        "fallback_count": fallback_count,
        "male_ratio": round(male_ratio, 6),
        "male_f0s": male_f0s,
        "drifted": drifted,
        "passed": passed,
    }


def effective_voiceover_chars(text: str) -> int:
    """Count spoken-content characters, excluding whitespace and punctuation."""
    return len(re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]", "", text))


def duration_gate(audio_duration: float, end_padding: float, max_final_duration: float) -> dict:
    """Check the estimated final duration before SadTalker/HyperFrames."""
    audio_duration = float(audio_duration)
    end_padding = float(end_padding)
    max_final_duration = float(max_final_duration)
    if audio_duration < 0 or end_padding < 0 or max_final_duration <= 0:
        raise ValueError("duration values must be non-negative and max_final_duration > 0")
    estimated = audio_duration + end_padding
    passed = estimated <= max_final_duration
    return {
        "audio_duration": round(audio_duration, 3),
        "end_padding": round(end_padding, 3),
        "estimated_final_duration": round(estimated, 3),
        "max_final_duration": round(max_final_duration, 3),
        "passed": passed,
        "reason": "ok" if passed else "estimated_final_duration_exceeds_limit",
    }


_TRADITIONAL_TO_SIMPLIFIED = {
    "個": "个", "劇": "剧", "億": "亿", "這": "这", "來": "来", "確": "确",
    "後": "后", "進": "进", "測": "测", "試": "试", "現": "现", "準": "准",
    "讀": "读", "數": "数", "聲": "声", "過": "过", "說": "说", "還": "还",
    "沒": "没", "廣": "广", "決": "决", "為": "为", "與": "与", "從": "从",
    "轉": "转", "對": "对", "臺": "台", "聽": "听", "輕": "轻", "鬆": "松",
    "幾": "几", "畫": "画", "面": "面", "寫": "写", "將": "将", "兩": "两",
    "詞": "词", "週": "周", "戲": "戏", "調": "调", "每": "每", "賴": "赖",
    "錢": "钱", "萬": "万", "實": "实", "際": "际", "江": "江", "蘭": "兰",
    "產": "产", "觀": "观", "審": "审", "門": "门", "檻": "槛", "別": "别",
    "鍵": "键", "騙": "骗", "內": "内", "貴": "贵", "歲": "岁", "電": "电",
    "設": "设", "計": "计", "辭": "辞", "職": "职", "帶": "带", "順": "顺",
    "歡": "欢", "網": "网", "創": "创", "開": "开", "關": "关", "媽": "妈",
    "寶": "宝", "論": "论", "話": "话", "題": "题", "營": "营", "運": "运",
    "軍": "军", "隊": "队", "長": "长", "時": "时", "問": "问", "歷": "历",
    "經": "经", "結": "结", "構": "构", "點": "点", "鐘": "钟", "認": "认",
    "識": "识", "處": "处", "裡": "里", "東": "东", "車": "车", "紅": "红",
    "綠": "绿", "頭": "头", "價": "价", "親": "亲", "愛": "爱", "國": "国",
    "風": "风", "雲": "云", "總": "总", "節": "节", "線": "线", "張": "张",
    "強": "强", "場": "场", "階": "阶", "層": "层", "態": "态", "顏": "颜",
    "語": "语", "豐": "丰", "農": "农", "陽": "阳", "陰": "阴", "書": "书",
    "專": "专", "員": "员", "業": "业", "會": "会", "體": "体", "號": "号",
    "單": "单", "雙": "双", "絕": "绝", "當": "当", "戰": "战", "勝": "胜",
    "負": "负", "責": "责", "質": "质", "驗": "验", "顯": "显", "證": "证",
    "譽": "誉", "補": "补", "費": "费", "選": "选", "舉": "举", "議": "议",
    "園": "园", "醫": "医", "藥": "药", "監": "监", "獄": "狱", "鎖": "锁",
    "間": "间",
}


def normalize_cn(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]", "", text.lower())


def traditional_to_simplified_map() -> dict:
    return dict(_TRADITIONAL_TO_SIMPLIFIED)


def _to_simplified(text: str) -> str:
    return "".join(_TRADITIONAL_TO_SIMPLIFIED.get(ch, ch) for ch in text)


def _similarity(left: str, right: str) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    set_left, set_right = set(left), set(right)
    union = set_left | set_right
    base = len(set_left & set_right) / len(union) if union else 1.0
    ratio = difflib.SequenceMatcher(None, left, right).ratio()
    return 0.5 * base + 0.5 * ratio


def content_acceptance(script: str, whisper: str, required_ratio: float = 0.7, keywords=None) -> dict:
    norm_script = normalize_cn(_to_simplified(script))
    norm_whisper = normalize_cn(_to_simplified(whisper))
    similarity = _similarity(norm_script, norm_whisper)
    missing_keywords = []
    for keyword in keywords or []:
        if normalize_cn(_to_simplified(keyword)) not in norm_whisper:
            missing_keywords.append(keyword)
    return {
        "similarity": round(similarity, 4),
        "required_ratio": required_ratio,
        "missing_keywords": missing_keywords,
        "passed": similarity >= required_ratio and not missing_keywords,
    }


def build_speed_command(input_path, output_path, speed=1.30, loudness_i=-14.0, true_peak=-1.0, lra=8.0) -> str:
    return (
        f'ffmpeg -y -v error -i "{input_path}" '
        f'-af "atempo={speed:g},loudnorm=I={loudness_i:g}:TP={true_peak:g}:LRA={lra:g}" '
        f'-ar 48000 -ac 2 "{output_path}"'
    )
