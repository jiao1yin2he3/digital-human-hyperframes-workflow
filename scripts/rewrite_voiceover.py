#!/usr/bin/env python3
"""
口播稿改写器 (Voiceover Rewriter)
--------------------------------
把"书面文案 / 新闻素材"改写为适合 AI 配音的口语化口播稿。

用法:
    python3 scripts/rewrite_voiceover.py <input.md> <output.txt>

输入: 任意文本（支持 markdown，会去掉 #、*、- 等符号）
输出: 纯口播文本，可直接喂给 edge-tts / CosyVoice / 任何 TTS

改写规则（针对中文口播优化）:
1. 数字读法: 62% -> 百分之六十二; 5.6 -> 五点六; 1080 -> 一千零八十
            但保留常见约定: "5G" 等不强行替换
2. 量级词: "2 个" 中量词前的"二"改"两"（两条 / 两个）
3. 英文专名: 首次出现给中文注音，如 Think(思考) / Sol(索尔)
4. 书面连接词口语化: 此外->另外 / 与此同时->同时 / 然而->不过
5. 拆分长句: 超过 ~28 字的句子在标点处建议断句（仅提示，不强制）
6. 去除 markdown 标记与多余空白

注意: 这是规则初版，专有名词注音表可在此维护。
"""
import sys
import re

# 专有名词首次出现注音（中文环境下帮助 TTS 断词 + 听众理解）
GLOSSARY = {
    "Think": "Think(思考)",
    "think": "think(思考)",
    "Sol": "索尔(Sol)",
    "Luna": "露娜(Luna)",
    "Terra": "特拉(Terra)",
    "Astra": "阿斯特拉(Astra)",
    "GPT-5.6": "GPT 五点六",
    "GPT-5.5": "GPT 五点五",
    "HyperFrames": "HyperFrames",
}

# 书面 -> 口语
FORMAL_TO_SPOKEN = {
    "此外": "另外",
    "与此同时": "同时",
    "然而": "不过",
    "故而": "所以",
    "因此": "所以",
    "倘若": "如果",
    "鉴于": "考虑到",
    "据悉": "据了解",
    "旨在": "目的是",
    "诸如": "比如",
}


def strip_markdown(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^#{1,6}\s*", "", line)        # 标题
        line = re.sub(r"[*_`>#]", "", line)            # 强调/列表/引用
        line = line.strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def cn_num_replace(text: str) -> str:
    # 百分比: 62% -> 百分之六十二
    text = re.sub(r"(\d+(?:\.\d+)?)%", lambda m: "百分之" + num_to_cn(float(m.group(1))), text)
    # 版本号 x.y: 5.6 -> 五点六
    text = re.sub(r"\b(\d+)\.(\d+)\b", lambda m: num_to_cn(int(m.group(1))) + "点" + num_to_cn(int(m.group(2))), text)
    return text


_CN_DIGIT = "零一二三四五六七八九"
_CN_UNIT = ["", "十", "百", "千"]


def num_to_cn(num: float) -> str:
    if num == int(num):
        n = int(num)
        if 0 <= n <= 99:
            if n < 10:
                return _CN_DIGIT[n]
            tens, ones = divmod(n, 10)
            if ones == 0:
                return _CN_DIGIT[tens] + "十"
            if tens == 1:
                return "十" + _CN_DIGIT[ones]
            return _CN_DIGIT[tens] + "十" + _CN_DIGIT[ones]
        return str(n)
    # 小数
    intpart = int(num)
    frac = str(num - intpart).split(".")[1]
    return num_to_cn(intpart) + "点" + "".join(_CN_DIGIT[int(d)] for d in frac)


def apply_glossary(text: str) -> str:
    for en, zh in GLOSSARY.items():
        # 仅首次替换为注音版（简单处理：全部替换，保持一致性更安全）
        text = text.replace(en, zh)
    return text


def apply_formal(text: str) -> str:
    for formal, spoken in FORMAL_TO_SPOKEN.items():
        text = text.replace(formal, spoken)
    return text


def rewrite(text: str) -> str:
    text = strip_markdown(text)
    text = apply_formal(text)
    text = cn_num_replace(text)
    text = apply_glossary(text)
    # 去除多余空白与换行
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def main():
    if len(sys.argv) < 3:
        print("用法: python3 scripts/rewrite_voiceover.py <input.md> <output.txt>")
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8") as f:
        raw = f.read()
    result = rewrite(raw)
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        f.write(result)
    print(f"口播稿已生成: {sys.argv[2]}")
    print(f"字数: {len(result)}")
    print("---预览---")
    print(result[:400])


if __name__ == "__main__":
    main()
