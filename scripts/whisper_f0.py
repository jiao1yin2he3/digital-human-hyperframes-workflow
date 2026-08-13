#!/usr/bin/env python3
"""Run Whisper transcription + F0 extraction using the workflow Python env.
Output: JSON with 'text' and 'f0' keys."""
import argparse
import json
import subprocess
import sys
import wave
import numpy as np

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    args = ap.parse_args()

    from faster_whisper import WhisperModel
    m = WhisperModel("base", device="cpu", compute_type="int8")
    segs, _ = m.transcribe(args.audio, language="zh")
    text = "".join(s.text for s in segs)

    # F0
    tmp = f"/tmp/_f0_{__import__('os').getpid()}.wav"
    try:
        subprocess.run(["ffmpeg", "-y", "-i", args.audio, "-ar", "16000", "-ac", "1", tmp],
                       capture_output=True)
        with wave.open(tmp) as w:
            sr = w.getframerate()
            x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)/32768.0
    finally:
        if __import__('os').path.exists(tmp):
            __import__('os').remove(tmp)
    vals = []
    frame = int(.04*sr); hop = int(.02*sr)
    for s in range(0, len(x)-frame, hop):
        z = x[s:s+frame] - np.mean(x[s:s+frame])
        if np.sqrt(np.mean(z*z)) < .01: continue
        ac = np.correlate(z, z, mode='full')[frame-1:]
        lo, hi = int(sr/300), int(sr/60)
        if hi >= len(ac): hi = len(ac)-1
        k = lo + np.argmax(ac[lo:hi])
        f = sr/k
        if 60 < f < 300: vals.append(f)
    f0 = np.median(vals) if vals else 0

    print(json.dumps({"text": text, "f0": float(f0), "text_len": len(text)}))

if __name__ == "__main__":
    main()
