#!/Users/changlin/miniconda3/bin/python3
# -*- coding: utf-8 -*-
"""
视频英文配音 → 中文配音工具

流程：
  1. faster-whisper 识别英文语音 → 带时间戳的文本分段
  2. Google 翻译 英文 → 中文
  3. Edge-TTS 生成中文语音片段
  4. ffmpeg 合成：中文配音 + 原音降为背景音（可选）

用法:
  python dub_video.py 视频.mp4                      # 输出 视频_dubbed.mp4
  python dub_video.py 视频.mp4 -o 输出.mp4           # 指定输出
  python dub_video.py 视频.mp4 --no-bg               # 纯中文配音，移除原音
  python dub_video.py 视频.mp4 -m medium --voice zh-CN-YunxiNeural  # 更高精度 + 男声
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def get_audio_duration(filepath: str) -> float:
    """用 ffprobe 获取音频时长（秒）"""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", filepath],
        capture_output=True, text=True, timeout=15,
    )
    import json
    return float(json.loads(result.stdout)["format"]["duration"])


def get_video_duration(filepath: str) -> float:
    """用 ffprobe 获取视频时长（秒）"""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", filepath],
        capture_output=True, text=True, timeout=15,
    )
    import json
    return float(json.loads(result.stdout)["format"]["duration"])


# ---------------------------------------------------------------------------
# Step 1: ASR — faster-whisper
# ---------------------------------------------------------------------------

def transcribe_video(
    video_path: str,
    model_size: str = "small",
    device: str = "auto",
    language: str = "en",
) -> list[dict]:
    """
    faster-whisper 语音识别，返回带时间戳的 segment 列表。
    每个 segment: {start, end, text}
    """
    print(f"[1/4] 加载 Whisper 模型 ({model_size}) ...")
    from faster_whisper import WhisperModel

    model = WhisperModel(
        model_size,
        device="cpu" if device == "auto" else device,
        compute_type="default",
    )

    print(f"[1/4] 正在识别语音 ...")
    segments_gen, info = model.transcribe(
        video_path,
        language=language,
        vad_filter=True,
    )

    segments = []
    for seg in segments_gen:
        text = (seg.text or "").strip()
        if not text:
            continue
        segments.append({
            "start": seg.start,
            "end": seg.end,
            "text": text,
        })

    print(f"[1/4] ✓ 识别完成，共 {len(segments)} 个语音片段")
    if info.language:
        print(f"      检测语言: {info.language} (概率: {info.language_probability:.2%})")
    return segments


# ---------------------------------------------------------------------------
# Step 2: 翻译 — Google Translate (via deep-translator)
# ---------------------------------------------------------------------------

def translate_segments(segments: list[dict]) -> list[dict]:
    """将每个 segment 的英文文本翻译为中文"""
    print(f"[2/4] 正在翻译 {len(segments)} 个片段 ...")
    from deep_translator import GoogleTranslator

    for i, seg in enumerate(segments):
        text = seg["text"]
        # 跳过纯数字或太短的文本
        if len(text) <= 1 and text.isascii() and not text.isalpha():
            seg["chinese"] = text
            continue

        for attempt in range(3):
            try:
                result = GoogleTranslator(source="en", target="zh-CN").translate(text)
                seg["chinese"] = result
                break
            except Exception as e:
                if attempt == 2:
                    print(f"      警告: 片段 {i} 翻译失败，保留原文: {e}")
                    seg["chinese"] = text
                else:
                    time.sleep(1.5 * (attempt + 1))  # 退避重试

        if (i + 1) % 10 == 0:
            print(f"      进度: {i + 1}/{len(segments)}")

    print(f"[2/4] ✓ 翻译完成")
    return segments


# ---------------------------------------------------------------------------
# Step 3: TTS — Edge-TTS 中文语音合成
# ---------------------------------------------------------------------------

async def _tts_one(text: str, out_path: str, voice: str) -> None:
    """生成单段 TTS 音频"""
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_path)


async def _tts_all(segments: list[dict], tmpdir: str, voice: str) -> list[dict]:
    """并发生成所有 TTS 音频片段"""
    tasks = []
    for i, seg in enumerate(segments):
        out_path = os.path.join(tmpdir, f"tts_{i:04d}.mp3")
        seg["tts_path"] = out_path
        tasks.append(_tts_one(seg["chinese"], out_path, voice))

    # 分批并发，避免同时太多请求
    batch_size = 5
    for batch_start in range(0, len(tasks), batch_size):
        batch = tasks[batch_start:batch_start + batch_size]
        await asyncio.gather(*batch)
        done = min(batch_start + batch_size, len(tasks))
        print(f"      进度: {done}/{len(tasks)}")

    return segments


def generate_tts(segments: list[dict], tmpdir: str, voice: str = "zh-CN-XiaoxiaoNeural") -> list[dict]:
    """生成中文 TTS 音频片段"""
    print(f"[3/4] 正在生成中文语音 ({voice}) ...")
    asyncio.run(_tts_all(segments, tmpdir, voice))
    print(f"[3/4] ✓ 语音合成完成")
    return segments


# ---------------------------------------------------------------------------
# Step 4: 音频合成 — ffmpeg
# ---------------------------------------------------------------------------

def _generate_silence(filepath: str, duration: float, sample_rate: int = 22050) -> None:
    """用 ffmpeg 生成静音 WAV 文件"""
    if duration <= 0.02:  # 忽略极短间隙
        return
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=mono",
         "-t", f"{duration:.6f}", filepath],
        check=True, timeout=30,
    )


def mix_audio(
    segments: list[dict],
    video_path: str,
    output_path: str,
    tmpdir: str,
    bg_volume: float = 0.18,
) -> None:
    """
    用 ffmpeg 合成最终视频（两阶段）：
      Stage A: 将 TTS 片段 + 静音间隙拼接成一条完整的中文配音轨
      Stage B: 将配音轨与原音（降音量）混合，合成最终视频
    """
    print(f"[4/4] 正在合成音频（ffmpeg）...")

    video_dur = get_video_duration(video_path)
    concat_entries = []  # ffmpeg concat 文件列表
    prev_end = 0.0

    # ---- Stage A: 构建配音轨 ----
    print(f"      构建中文配音轨 ({len(segments)} 个片段)...")
    for i, seg in enumerate(segments):
        # 前导静音（上一段结束 → 这一段开始之间的间隙）
        gap_dur = seg["start"] - prev_end
        if gap_dur > 0.05:
            gap_path = os.path.join(tmpdir, f"gap_{i:04d}.wav")
            _generate_silence(gap_path, gap_dur)
            concat_entries.append(f"file '{gap_path}'")

        orig_dur = seg["end"] - seg["start"]

        # 获取 TTS 实际时长，计算调速比例
        try:
            tts_dur = get_audio_duration(seg["tts_path"])
        except Exception:
            tts_dur = orig_dur

        atempo = tts_dur / orig_dur if orig_dur > 0.5 else 1.0
        atempo = max(0.65, min(1.5, atempo))

        # 需要调速则先生成调速后的片段
        if abs(atempo - 1.0) > 0.03:
            adjusted_path = os.path.join(tmpdir, f"adj_{i:04d}.mp3")
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-i", seg["tts_path"],
                 "-filter:a", f"atempo={atempo:.3f}",
                 "-c:a", "libmp3lame", "-q:a", "2",
                 adjusted_path],
                check=True, timeout=30,
            )
            concat_entries.append(f"file '{adjusted_path}'")
        else:
            concat_entries.append(f"file '{seg['tts_path']}'")

        prev_end = seg["end"]

    # 尾部如有剩余也补静音
    tail_gap = video_dur - prev_end
    if tail_gap > 0.05:
        tail_path = os.path.join(tmpdir, "tail_gap.wav")
        _generate_silence(tail_path, tail_gap)
        concat_entries.append(f"file '{tail_path}'")

    # 写 concat 列表文件
    concat_list_path = os.path.join(tmpdir, "concat_list.txt")
    with open(concat_list_path, "w") as f:
        f.write("\n".join(concat_entries))

    # 用 concat demuxer 拼接成完整配音轨
    dub_track_path = os.path.join(tmpdir, "dub_track.wav")
    print(f"      拼接配音轨...")
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", concat_list_path,
         "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2",
         dub_track_path],
        check=True, timeout=300,
    )

    # ---- Stage B: 混合配音轨与原音 ----
    print(f"      混合配音轨与原视频...")
    if bg_volume > 0:
        # 中文配音 + 原音背景
        filter_complex = (
            f"[0:a]volume={bg_volume}[bg];"
            f"[1:a]volume=1.0[dub];"
            f"[dub][bg]amix=inputs=2:duration=first:dropout_transition=2[out]"
        )
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats",
            "-i", video_path,
            "-i", dub_track_path,
            "-filter_complex", filter_complex,
            "-map", "[out]", "-map", "0:v",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            output_path,
        ]
    else:
        # 纯中文配音，直接替换音轨
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats",
            "-i", video_path,
            "-i", dub_track_path,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            output_path,
        ]

    try:
        subprocess.run(cmd, check=True, timeout=7200)
    except subprocess.CalledProcessError as e:
        print(f"\n[4/4] ✗ ffmpeg 合成失败: {e}", file=sys.stderr)
        raise

    print(f"[4/4] ✓ 合成完成 → {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="视频英文配音 → 中文配音",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python dub_video.py lesson.mp4
  python dub_video.py lesson.mp4 -o dubbed.mp4 --no-bg
  python dub_video.py lesson.mp4 -m medium --voice zh-CN-YunxiNeural

可用中文语音:
  zh-CN-XiaoxiaoNeural (女声, 默认)  zh-CN-YunxiNeural (男声)
  zh-CN-XiaoyiNeural  (女声)         zh-CN-YunjianNeural (男声)
  zh-CN-XiaochenNeural(女声)         zh-CN-YunyangNeural (男声, 新闻风格)
  zh-CN-XiaohanNeural (女声)
        """,
    )
    parser.add_argument("video", type=Path, help="输入视频路径")
    parser.add_argument("-o", "--output", type=Path, default=None, help="输出路径（默认: 原文件名_dubbed.mp4）")
    parser.add_argument("-m", "--model", default="small", help="Whisper 模型: tiny/base/small/medium/large-v3")
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--language", default="en", help="视频语言代码（默认 en）")
    parser.add_argument("--voice", default="zh-CN-XiaoxiaoNeural", help="Edge-TTS 中文语音")
    parser.add_argument("--bg-volume", type=float, default=0.18, help="原音背景音量 (0=移除, 1=不变, 默认 0.18)")
    parser.add_argument("--no-bg", action="store_true", help="移除原英语配音，仅保留中文")
    parser.add_argument("--skip-asr", action="store_true", help="跳过 ASR，使用已有的 segments.json")
    parser.add_argument("--segments-file", type=Path, default=None, help="segments JSON 文件路径")
    args = parser.parse_args()

    video_path = args.video.expanduser().resolve()
    if not video_path.is_file():
        print(f"错误: 找不到文件 {video_path}", file=sys.stderr)
        return 1

    out_path = args.output
    if out_path is None:
        out_path = video_path.parent / f"{video_path.stem}_dubbed.mp4"
    else:
        out_path = out_path.expanduser().resolve()

    bg_volume = 0.0 if args.no_bg else args.bg_volume

    # 持久化 segments 文件路径（用于 debug 和 --skip-asr）
    segments_cache = out_path.with_suffix(".segments.json")

    with tempfile.TemporaryDirectory(prefix="video_dub_") as tmpdir:
        # Step 1: ASR
        if args.skip_asr:
            seg_file = args.segments_file or segments_cache
            if seg_file.is_file():
                import json
                segments = json.loads(seg_file.read_text(encoding="utf-8"))
                print(f"[1/4] 跳过 ASR，加载已有分段: {len(segments)} 段")
            else:
                print(f"错误: segments 文件不存在 {seg_file}", file=sys.stderr)
                return 1
        else:
            segments = transcribe_video(str(video_path), args.model, args.device, args.language)

        if not segments:
            print("错误: 未识别到任何语音", file=sys.stderr)
            return 1

        # 持久化保存 segments
        import json
        segments_cache.write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"      segments 已缓存到 {segments_cache}")

        # Step 2: 翻译
        segments = translate_segments(segments)

        # Step 3: TTS
        segments = generate_tts(segments, tmpdir, args.voice)

        # Step 4: 合成
        mix_audio(segments, str(video_path), str(out_path), tmpdir, bg_volume)

    # 打印结果
    orig_size = video_path.stat().st_size / (1024 * 1024)
    out_size = out_path.stat().st_size / (1024 * 1024)
    print(f"\n{'='*50}")
    print(f"原视频: {video_path.name} ({orig_size:.1f} MB)")
    print(f"输出:   {out_path.name} ({out_size:.1f} MB)")
    print(f"配音:   {'纯中文' if bg_volume == 0 else f'中文配音 + 原音背景({bg_volume:.0%}音量)'}")
    print(f"{'='*50}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
