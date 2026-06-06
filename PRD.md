# PRD: 视频英文→中文 AI 配音工具

> 版本: v1.0 | 日期: 2026-06-06 | 状态: MVP 已完成，迭代中

---

## 1. 产品概述

### 1.1 一句话描述
输入英文视频 → 自动识别语音、翻译为中文、合成中文配音 → 输出中文配音视频。

### 1.2 目标用户

| 用户类型 | 场景 | 痛点 |
|---------|------|------|
| 知识内容消费者 | 观看英文 TED/课程/YouTube | 英文听力不够好，边看字幕边听很累 |
| 教育/培训从业者 | 将英文教学视频本地化 | 人工配音成本高（$5-20/分钟），周期长 |
| 视频内容创作者 | 搬运/二创视频需要中配 | 没有配音资源，外包质量参差不齐 |
| 普通用户 | 想给孩子/老人看英文动画/纪录片 | 他们看不懂字幕 |

### 1.3 核心价值
**全自动**：一条命令，15 分钟视频约 30 分钟处理完毕，零人工介入。
**低成本**：ASR（本地免费）+ 翻译（Google 免费）+ TTS（Edge 免费），仅消耗算力。

---

## 2. 功能需求

### 2.1 核心流水线（P0）

```
英文视频.mp4
  ┌──────────────┐
  │ Step 1: ASR   │  faster-whisper 本地语音识别
  │  英文→文本     │  输出: 带毫秒级时间戳的文本片段 [200段/14min视频]
  └──────┬───────┘
         ▼
  ┌──────────────┐
  │ Step 2: 翻译  │  Google Translate (deep-translator)
  │  英文→中文     │  逐段翻译，带退避重试
  └──────┬───────┘
         ▼
  ┌──────────────┐
  │ Step 3: TTS   │  Edge-TTS (微软免费)
  │  中文→语音     │  每段生成独立 .mp3，5段/批并发生成
  └──────┬───────┘
         ▼
  ┌──────────────┐
  │ Step 4: 合成  │  ffmpeg 两阶段
  │  配音+视频     │  A: concat 拼接中文配音轨
  │               │  B: amix 混合原音(背景) + 中文轨 + 视频流
  └──────────────┘
输出: 原视频名_dubbed.mp4
```

### 2.2 配音模式

| 模式 | 参数 | 效果 |
|------|------|------|
| 纯中文配音 | `--no-bg` | 完全替换原音轨 |
| 中文+原音背景 | `--bg-volume 0.18`（默认） | 中文主配音 + 英文原声降为 18% 背景 |
| 自定义背景音量 | `--bg-volume 0.5` | 可调节英文背景音大小 |

### 2.3 模型与质量参数

| 参数 | 可选值 | 默认 | 说明 |
|------|--------|------|------|
| Whisper 模型 | tiny/base/small/medium/large-v3 | small | 越大越准，越慢 |
| TTS 语音 | zh-CN-XiaoxiaoNeural(女)/YunxiNeural(男) 等 7 种 | XiaoxiaoNeural | Edge-TTS 中文语音 |
| 视频语言 | en/ja/ko/... | en | 源语言代码 |
| 推理设备 | auto/cpu/cuda | auto | CPU 推理 14 分钟视频约 30 分钟 |

### 2.4 辅助功能（P1）

| 功能 | 参数 | 说明 |
|------|------|------|
| 复用 ASR 结果 | `--skip-asr --segments-file xx.json` | 跳过语音识别，适合反复调 TTS/翻译参数 |
| 自动缓存 segments | 输出旁自动生成 `.segments.json` | 失败后可重跑跳过 ASR |
| 多中文语音 | `--voice` | 7 种 Edge-TTS 中文语音可选 |

---

## 3. 技术架构

### 3.1 技术栈

| 环节 | 技术 | 许可证 | 备注 |
|------|------|--------|------|
| ASR | faster-whisper + CTranslate2 | MIT | 本地 CPU/GPU 推理 |
| 翻译 | deep-translator (Google Translate) | MIT | 免费，有频率限制 |
| TTS | edge-tts | GPLv3 | 微软免费 TTS，中文质量高 |
| 音视频 | ffmpeg | GPL | 系统级依赖，macOS `brew install ffmpeg` |
| 运行环境 | Python 3.10+, miniconda | — | 项目位于 `video_dub/` |

### 3.2 实时性能数据（Mac Intel CPU，small 模型）

| 视频时长 | ASR 耗时 | 翻译+TTS | 合成 | 总耗时 | 处理比 |
|---------|---------|---------|------|------|--------|
| 14 分钟 | ~25 min | ~3 min | ~2 min | **~30 min** | 2.1x |
| 5 分钟 | ~8 min | ~1 min | ~30s | **~10 min** | 2x |
| 1 分钟 | ~2 min | ~15s | ~10s | **~2.5 min** | 2.5x |

> CUDA 加速下 ASR 可提速 3-5x。

### 3.3 本次实测数据（TED 演讲，13'49"）

| 指标 | 数值 |
|------|------|
| 识别片段数 | 200 段 |
| 平均片段时长 | 4.1s |
| 最短片段 | 1.0s |
| 总处理时长 | ~35 分钟 |
| 输出文件 | 18MB → 31MB（纯中文配音，`--no-bg`） |

---

## 4. 已知问题与改进方向

### 4.1 已修复的严重问题

| 问题 | 原因 | 修复 |
|------|------|------|
| ffmpeg 合成跑 >10 小时 | `apad` + 100+ 路 `amix` 每路都补齐到全长 | 改为 concat 拼接 + 2 路 amix |

### 4.2 当前限制

| 问题 | 影响 | 改进方向 |
|------|------|----------|
| 片段过多（200/14min） | 翻译+ TTS 请求多，有被限速风险 | 合并相邻短片段，降低粒度 |
| 中文语速不匹配 | TTS 时长可能偏离原文时间轴 | atempo 调速（已实现 ±35%），可增加静默裁剪 |
| CPU ASR 慢 | 长视频等待久 | 支持 CUDA/Apple Silicon GPU (MLX) |
| 翻译质量不稳定 | Google 免费翻译偶发不准确 | 支持 Claude/DeepL API 作为高质量翻译后端 |
| 无字幕输出 | 只有配音无字幕文件 | 输出 SRT 字幕，方便上传 B站/YouTube |
| 单文件处理 | 不能批量操作 | 支持目录输入，批量配音 |

### 4.3 改进路线图

```
v1.1 ─ 片段合并优化 + SRT 字幕输出 + 批量处理
v1.2 ─ 多翻译后端（Claude/DeepL API）
v1.3 ─ Apple Silicon GPU 加速
v2.0 ─ Web UI / 拖拽上传 / 在线配音
```

---

## 5. 用户使用指南

### 5.1 安装

```bash
# 1. 系统依赖
brew install ffmpeg

# 2. Python 依赖
cd video_dub
pip install -r requirements.txt
```

### 5.2 基本用法

```bash
# 默认：中文配音 + 英文原声 18% 背景
python dub_video.py ~/Movies/ted_talk.mp4

# 纯中文配音（去掉英文原声）
python dub_video.py ~/Movies/ted_talk.mp4 --no-bg

# 高质量模型 + 男声
python dub_video.py ~/Movies/ted_talk.mp4 -m medium --voice zh-CN-YunxiNeural

# 跳过 ASR（已跑过一次，只调 TTS 参数）
python dub_video.py ~/Movies/ted_talk.mp4 --skip-asr --voice zh-CN-YunyangNeural
```

### 5.3 参数速查

```
video              输入视频路径 [必填]
-o, --output       输出路径（默认 原文件名_dubbed.mp4）
-m, --model        tiny|base|small|medium|large-v3（默认 small）
--voice            中文语音（默认 zh-CN-XiaoxiaoNeural 女声）
--no-bg            纯中文配音
--bg-volume        英文背景音量 0~1（默认 0.18）
--skip-asr         跳过语音识别
--segments-file    指定已有 segments JSON
--language         源语言代码（默认 en）
--device           auto|cpu|cuda（默认 auto）
```

---

## 6. 项目文件

| 文件 | 说明 |
|------|------|
| `video_dub/dub_video.py` | 主脚本，~400 行 |
| `video_dub/requirements.txt` | faster-whisper, edge-tts, deep-translator |
| `video_subtitle/extract_subtitles.py` | 共享 ASR 能力（参考实现） |
