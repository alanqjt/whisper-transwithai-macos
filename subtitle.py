#!/usr/bin/env python3
"""
自动给视频生成中文字幕 (.srt)。

管线: 视频 --whisper--> 原文+时间轴 --翻译--> 中文 --> .srt

转写引擎:
  whisper  openai-whisper (默认, 已装)
  mlx      mlx-whisper (Apple Silicon 上更快, 需 pip/pipx 安装)

翻译后端:
  argos    argostranslate, 离线/免费 (默认)
  claude   Claude API, 质量最好, 需环境变量 ANTHROPIC_API_KEY
  none     不翻译, 只输出原文字幕
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

# 国内网络默认走 HuggingFace 镜像, 并关闭 xet(镜像不代理 xet 通道, 否则下载会失败)。
# 想用官方源: 运行前 export HF_ENDPOINT=https://huggingface.co
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v",
              ".ts", ".mpg", ".mpeg", ".wmv", ".m4a", ".mp3", ".wav", ".flac"}

# 源语言别名 -> whisper/argos 语言代码; 不在表里的直接当作已是代码
LANG_ALIASES = {
    "auto": "auto", "自动": "auto",
    "中文": "zh", "汉语": "zh", "普通话": "zh", "chinese": "zh", "zh-cn": "zh", "zh": "zh",
    "英语": "en", "英文": "en", "english": "en", "en": "en",
    "日语": "ja", "日文": "ja", "japanese": "ja", "jp": "ja", "ja": "ja",
    "韩语": "ko", "韩文": "ko", "korean": "ko", "kr": "ko", "ko": "ko",
    "法语": "fr", "法文": "fr", "french": "fr", "fr": "fr",
    "德语": "de", "德文": "de", "german": "de", "de": "de",
    "西班牙语": "es", "spanish": "es", "es": "es",
    "俄语": "ru", "俄文": "ru", "russian": "ru", "ru": "ru",
    "意大利语": "it", "italian": "it", "it": "it",
    "葡萄牙语": "pt", "portuguese": "pt", "pt": "pt",
    "泰语": "th", "thai": "th", "th": "th",
    "越南语": "vi", "vietnamese": "vi", "vi": "vi",
    "阿拉伯语": "ar", "arabic": "ar", "ar": "ar",
    "印地语": "hi", "hindi": "hi", "hi": "hi",
}


def normalize_lang(s: str) -> str:
    return LANG_ALIASES.get(s.strip().lower(), s.strip().lower())


# ---------------------------------------------------------------- utils
def log(*a):
    print(*a, file=sys.stderr, flush=True)


# Whisper 在静音/片头音乐上会吐出训练数据里的固定幻觉句 (尤其中文模型), 专门拦截
_HALLU_STRONG = ("请不吝", "点点栏目", "明镜与点点", "打赏支持",
                 "amara.org", "字幕志愿者", "字幕by", "字幕由", "谢谢观看")


def is_hallucination(text: str) -> bool:
    t = text.lower()
    if any(m in t for m in _HALLU_STRONG):
        return True
    # "请点赞+订阅/打赏/转发" 类组合
    if "点赞" in text and any(k in text for k in ("订阅", "打赏", "转发")):
        return True
    return False


def collapse_repeats(text: str, max_run: int = 2) -> str:
    """折叠连续重复 (尖叫/笑声/音乐导致的复读)。

    词级处理空格分隔语言 ('Oh oh oh ...' -> 'Oh oh');
    子串级处理无空格语言如中文 ('首先首先首先...' -> '首先首先', '啊啊啊' -> '啊啊')。
    """
    out, run_key, run = [], None, 0
    for w in text.split():
        key = w.lower().strip(".,!?;:'\"")
        if key == run_key:
            run += 1
        else:
            run_key, run = key, 1
        if run <= max_run:
            out.append(w)
    text = " ".join(out)
    # 把连续重复 (max_run 次以上) 的长度 1..7 子串压到 max_run 次
    for L in range(1, 8):
        text = re.sub(r"(.{%d})\1{%d,}" % (L, max_run),
                      lambda m: m.group(1) * max_run, text)
    return text


def srt_time(t: float) -> str:
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments, path: Path, bilingual: bool, max_dur: float = 0.0):
    lines = []
    i = 0
    for seg in segments:
        zh = (seg.get("zh") or "").strip()
        src = (seg.get("text") or "").strip()
        if not zh and not src:
            continue
        if bilingual and zh and src:
            body = f"{zh}\n{src}"
        else:
            body = zh or src
        # 截断超长段的显示时长, 防止一句字幕在屏幕上挂十几分钟
        start, end = seg["start"], seg["end"]
        if max_dur > 0 and end - start > max_dur:
            end = start + max_dur
        i += 1  # 连续编号, 跳过的空段不占号
        lines.append(f"{i}\n{srt_time(start)} --> {srt_time(end)}\n{body}\n")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------- 合成字幕视频
def _run_ffmpeg(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        tail = "\n".join(p.stdout.splitlines()[-12:])
        raise RuntimeError(f"ffmpeg 失败 (退出码 {p.returncode}):\n{tail}")


def mux_softsub(video: Path, srt: Path, out: Path):
    """把 srt 作为可开关的软字幕轨内嵌进 mp4 (mov_text, 不重编码, 快)。"""
    _run_ffmpeg(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(video), "-i", str(srt),
                 "-map", "0:v?", "-map", "0:a?", "-map", "1:0",
                 "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text",
                 "-metadata:s:s:0", "language=chi", str(out)])


def burn_subtitles(video: Path, srt: Path, out: Path, font: str = "Heiti SC"):
    """把字幕烧录进画面 (需 libass 编译的 ffmpeg; Mac 用 VideoToolbox 硬件编码提速)。"""
    esc = str(srt).replace("\\", "\\\\").replace("'", r"\'")
    style = (f"FontName={font},FontSize=22,PrimaryColour=&H00FFFFFF,"
             f"OutlineColour=&H80000000,BorderStyle=1,Outline=1,Shadow=0,MarginV=24")
    _run_ffmpeg(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(video),
                 "-vf", f"subtitles=filename='{esc}':force_style='{style}'",
                 "-c:v", "h264_videotoolbox", "-b:v", "5M",
                 "-c:a", "copy", str(out)])


# ------------------------------------------------------------ transcribe
def _curl(url, out, progress=False, allow_fail=False) -> int:
    """curl 下载(抗弱网)。progress=True 把进度转发到 stderr(GUI 状态栏可见)。
    allow_fail=True 用于可选小文件: 404 立即跳过(不死磕), 只重试网络抖动。"""
    if allow_fail:  # 可选文件(如不一定存在的 vocabulary.txt): 不对 http 错误重试
        return subprocess.run(["curl", "-L", "-fsS", "--retry", "5", "--retry-delay", "2",
                               "-o", str(out), url]).returncode
    # 必需文件(model.bin 等): 续传 + 激进重试(含网络抖动)
    base = ["curl", "-L", "-C", "-", "--retry", "30", "--retry-delay", "3",
            "--retry-all-errors"]
    if progress:
        cmd = base + ["--progress-bar", "-o", str(out), url]
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             text=True, bufsize=0)
        while True:
            ch = p.stderr.read(1)
            if not ch:
                break
            sys.stderr.write(ch)
            sys.stderr.flush()
        p.wait()
        return p.returncode
    return subprocess.run(base + ["-sS", "-o", str(out), url]).returncode


def robust_download(repo: str) -> str:
    """用 curl 从 HF 镜像把模型稳稳下到本地目录并返回该目录(供 faster-whisper 直接加载)。
    绕开 huggingface_hub(其弱网下 HEAD 重试不足、易 LocalEntryNotFoundError)。"""
    endpoint = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com").rstrip("/")
    dest = Path.home() / ".cache" / "whisper-transwithai" / repo.replace("/", "__")
    if (dest / ".done").exists():
        return str(dest)
    dest.mkdir(parents=True, exist_ok=True)
    log(f"[模型] 用 curl 从 {endpoint} 下载(抗弱网, 断了自动续传): {repo}")
    for f in ("config.json", "tokenizer.json", "vocabulary.json",
              "vocabulary.txt", "preprocessor_config.json"):
        _curl(f"{endpoint}/{repo}/resolve/main/{f}", dest / f, allow_fail=True)
    log("[模型] 下载 model.bin(大文件, 进度见底部状态栏)...")
    rc = _curl(f"{endpoint}/{repo}/resolve/main/model.bin", dest / "model.bin", progress=True)
    mb = dest / "model.bin"
    if not (mb.exists() and mb.stat().st_size > 1_000_000):
        raise RuntimeError(f"model.bin 下载失败 (curl rc={rc})")
    (dest / ".done").touch()
    log("[模型] 下载完成。")
    return str(dest)


def _model_cached(model: str) -> bool:
    """faster-whisper 模型是否已在本地(本地路径 或 HuggingFace 缓存)。"""
    if os.path.isdir(os.path.expanduser(model)):
        return True
    cache = os.path.expanduser("~/.cache/huggingface/hub")
    if "/" in model:  # HF 仓库名, 精确判断
        return os.path.isdir(os.path.join(cache, "models--" + model.replace("/", "--")))
    try:  # 标准尺寸名(large-v3 等): 模糊匹配缓存里的 faster-whisper 仓库
        return any(d.startswith("models--") and model in d and "faster-whisper" in d
                   for d in os.listdir(cache))
    except FileNotFoundError:
        return False


def transcribe(media: Path, engine: str, model: str, language: str,
               vad: bool = True, task: str = "transcribe"):
    """返回 (segments, detected_lang). segment: {start,end,text}"""
    lang = None if language == "auto" else language
    if engine == "faster":
        # faster-whisper: 自带 Silero VAD, 只转写有人声区间, 从源头挡掉音乐/静音幻觉
        from faster_whisper import WhisperModel
        try:
            from faster_whisper.utils import _MODELS as _FW_MODELS
        except Exception:
            _FW_MODELS = {}
        # 模型解析: 本地目录直接用; 尺寸名/仓库名 -> 用 curl 稳下到本地(抗弱网), 已下秒返回
        load = os.path.expanduser(model)
        if not os.path.isdir(load):
            repo = _FW_MODELS.get(model, model)
            if "/" in repo:
                load = robust_download(repo)
            else:
                load = model  # 兜底: 交给 faster-whisper 自行下载
        log(f"[faster-whisper] model={model} task={task} vad={vad} (CPU/int8)...")
        m = WhisperModel(load, device="cpu", compute_type="int8")
        log("[模型] 已加载就绪。")
        seg_iter, info = m.transcribe(
            str(media), language=lang, task=task,
            vad_filter=vad, condition_on_previous_text=False,
        )
        r = {"language": info.language,
             "segments": [{"start": s.start, "end": s.end, "text": s.text,
                           "no_speech_prob": s.no_speech_prob,
                           "avg_logprob": s.avg_logprob} for s in seg_iter]}
    elif engine == "mlx":
        import mlx_whisper
        repo = model if "/" in model else f"mlx-community/whisper-{model}"
        log(f"[mlx-whisper] model={repo} task={task} ...")
        r = mlx_whisper.transcribe(
            str(media), path_or_hf_repo=repo, language=lang,
            task=task, word_timestamps=False,
            # 关键: 不以上文为条件, 避免单个幻觉窗口引发整片复读
            condition_on_previous_text=False,
        )
    else:
        import whisper
        log(f"[openai-whisper] model={model} task={task} (载入中, 大模型在 CPU 上较慢)...")
        m = whisper.load_model(model)
        r = m.transcribe(str(media), language=lang, task=task, verbose=False,
                         condition_on_previous_text=False)
    segs, dropped = [], 0
    for s in r["segments"]:
        raw = (s["text"] or "").strip()
        if not raw:
            continue
        # 过滤静音/音乐窗口里的幻觉段 (高无语音概率 + 低置信度)
        if s.get("no_speech_prob", 0.0) > 0.6 and s.get("avg_logprob", 0.0) < -0.4:
            dropped += 1
            continue
        # 拦截 Whisper 固定幻觉句 (请点赞订阅... 之类)
        if is_hallucination(raw):
            dropped += 1
            continue
        text = collapse_repeats(raw)
        # 整段几乎全是复读 (折叠后 <25% 原长, 如音乐上 "首先首先..." ×200) = 非语音幻觉
        if len(raw) >= 16 and len(text) <= len(raw) * 0.25:
            dropped += 1
            continue
        segs.append({"start": float(s["start"]), "end": float(s["end"]), "text": text})
    if dropped:
        log(f"[filter] 丢弃 {dropped} 个疑似幻觉/无语音段")
    return segs, (r.get("language") or lang or "en")


# ------------------------------------------------------------- translate
def translate_argos(segments, src_lang):
    import argostranslate.package as pkg
    import argostranslate.translate as tr

    def have(frm, to):
        return any(p.from_code == frm and p.to_code == to
                   for p in pkg.get_installed_packages())

    def install(frm, to):
        if have(frm, to):
            return True
        pkg.update_package_index()
        avail = pkg.get_available_packages()
        p = next((p for p in avail if p.from_code == frm and p.to_code == to), None)
        if not p:
            return False
        log(f"[argos] 下载语言包 {frm}->{to} ...")
        p.install()
        return True

    src = src_lang.split("-")[0]
    direct = install(src, "zh")
    pivot = False
    if not direct:
        # 经英文中转
        ok = (src == "en" or install(src, "en")) and install("en", "zh")
        if not ok:
            log(f"[argos] 无 {src}->zh 语言包 (直连或经英文均失败), 跳过翻译")
            return
        pivot = src != "en"

    def t(text):
        if not text:
            return ""
        if direct:
            return tr.translate(text, src, "zh")
        mid = tr.translate(text, src, "en") if pivot else text
        return tr.translate(mid, "en", "zh")

    log(f"[argos] 翻译 {len(segments)} 段 ({'直连' if direct else src+'->en->zh'}) ...")
    for seg in segments:
        seg["zh"] = t(seg["text"])


def translate_claude(segments, src_lang, model):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log("[claude] 未设置 ANTHROPIC_API_KEY, 跳过翻译")
        return
    BATCH = 40
    log(f"[claude] model={model} 翻译 {len(segments)} 段 ...")
    for i in range(0, len(segments), BATCH):
        chunk = segments[i:i + BATCH]
        numbered = "\n".join(f"{j}\t{s['text']}" for j, s in enumerate(chunk))
        prompt = (
            "你是专业字幕翻译。把下面带编号的字幕逐条翻译成简体中文。\n"
            "要求: 口语化、简洁、贴合视频语境; 保留编号与行数完全一致; "
            "只输出 JSON 对象 {\"编号\": \"译文\"}, 不要任何解释。\n\n" + numbered
        )
        body = json.dumps({
            "model": model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"content-type": "application/json", "x-api-key": key,
                     "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.load(resp)
            text = "".join(b.get("text", "") for b in data["content"])
            text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            mapping = json.loads(text)
            for j, seg in enumerate(chunk):
                seg["zh"] = (mapping.get(str(j)) or mapping.get(j) or "").strip()
        except Exception as e:
            log(f"[claude] 第 {i}+ 批失败: {e}; 该批保留原文")
        log(f"[claude] {min(i + BATCH, len(segments))}/{len(segments)}")


# ------------------------------------------------------------------- main
def gather_inputs(paths):
    out = []
    for p in paths:
        p = Path(p).expanduser()
        if p.is_dir():
            out += sorted(f for f in p.iterdir() if f.suffix.lower() in VIDEO_EXTS)
        elif p.exists():
            out.append(p)
        else:
            # 当成 glob
            matches = sorted(Path().glob(str(p)))
            if matches:
                out += matches
            else:
                log(f"跳过 (找不到): {p}")
    return out


def main():
    ap = argparse.ArgumentParser(description="给视频自动生成中文字幕 (.srt)")
    ap.add_argument("inputs", nargs="+", help="视频文件 / 目录 / 通配符")
    ap.add_argument("--engine", choices=["whisper", "mlx", "faster"], default="whisper",
                    help="转写引擎: whisper(原版) / mlx(Apple Silicon 快) / faster(带 VAD, 抗音乐幻觉)")
    ap.add_argument("--vad", action=argparse.BooleanOptionalAction, default=True,
                    help="仅 faster 引擎: 用 VAD 只转写有人声区间 (默认开, --no-vad 关闭)")
    ap.add_argument("--task", choices=["transcribe", "translate"], default="transcribe",
                    help="transcribe=出原文(默认); translate=语音翻译, 配日→中 ST 模型"
                         "(chickenrice...-st-ct2)直接输出中文, 自动跳过二次翻译")
    ap.add_argument("--model", default="large-v3",
                    help="whisper 模型 (tiny/base/small/medium/large-v3); mlx 用 large-v3 等")
    ap.add_argument("--translator", choices=["argos", "claude", "none"], default="argos")
    ap.add_argument("--claude-model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--language", "--lang", default="auto",
                    help="源语言, 默认 auto 自动检测; 可用代码或中文名, 如 ja/日语 ko/韩语 en/英语")
    ap.add_argument("--max-dur", type=float, default=8.0,
                    help="单条字幕最长显示秒数, 超出则截断结束时间(防超长段挂屏); 0=不限")
    ap.add_argument("--bilingual", action="store_true", help="输出 中文+原文 双语字幕")
    ap.add_argument("--keep-src", action="store_true", help="额外输出一份原文 .srt")
    ap.add_argument("--softsub", action="store_true",
                    help="额外输出内嵌软字幕的视频 <名>.zh.sub.mp4 (mov_text, 不重编码)")
    ap.add_argument("--burn", action="store_true",
                    help="额外输出烧录硬字幕的视频 <名>.zh.hardsub.mp4 (需 libass 的 ffmpeg)")
    ap.add_argument("--delete-source", action="store_true",
                    help="处理成功后把原视频移到废纸篓(可恢复); 失败/无字幕则保留")
    ap.add_argument("--outdir", default=None, help="输出目录, 默认与视频同目录")
    args = ap.parse_args()
    args.language = normalize_lang(args.language)

    videos = gather_inputs(args.inputs)
    if not videos:
        log("没有找到任何输入视频")
        sys.exit(1)
    log(f"待处理 {len(videos)} 个文件\n")

    for v in videos:
        log(f"==== {v.name} ====")
        outdir = Path(args.outdir).expanduser() if args.outdir else v.parent
        outdir.mkdir(parents=True, exist_ok=True)
        try:
            segs, lang = transcribe(v, args.engine, args.model, args.language,
                                    args.vad, args.task)
        except Exception as e:
            log(f"转写失败: {e}\n")
            continue
        log(f"识别到语言: {lang}, 共 {len(segs)} 段")

        if args.keep_src:
            write_srt([dict(s) for s in segs], outdir / f"{v.stem}.{lang}.srt", False, args.max_dur)

        if args.task == "translate":
            log("[translate] 语音翻译模型已直接输出目标语言, 跳过二次翻译")
        elif lang.startswith("zh"):
            log("[translate] 源语言已是中文, 跳过翻译")
        elif args.translator == "argos":
            translate_argos(segs, lang)
        elif args.translator == "claude":
            translate_claude(segs, lang, args.claude_model)

        out = outdir / f"{v.stem}.zh.srt"
        write_srt(segs, out, args.bilingual, args.max_dur)
        log(f"已写出: {out}")

        ok = bool(segs)  # 有字幕内容才算成功
        if (args.softsub or args.burn) and not segs:
            log("(无字幕内容, 跳过合成字幕视频)")
        if args.softsub and segs:
            sv = outdir / f"{v.stem}.zh.sub.mp4"
            log(f"[softsub] 内嵌软字幕 -> {sv.name} ...")
            try:
                mux_softsub(v, out, sv)
                log(f"已写出: {sv}")
            except Exception as e:
                ok = False
                log(f"[softsub] 失败: {e}")
        if args.burn and segs:
            bv = outdir / f"{v.stem}.zh.hardsub.mp4"
            log(f"[burn] 烧录硬字幕 -> {bv.name} (重编码, 较慢) ...")
            try:
                burn_subtitles(v, out, bv)
                log(f"已写出: {bv}")
            except Exception as e:
                ok = False
                log(f"[burn] 失败: {e}")

        if args.delete_source:
            if ok:
                try:
                    from send2trash import send2trash
                    send2trash(str(v))
                    log(f"[删除] 原视频已移到废纸篓: {v.name}")
                except Exception as e:
                    log(f"[删除] 失败, 已保留原视频: {e}")
            else:
                log("[删除] 处理未成功/无字幕, 已保留原视频")
        log("")


if __name__ == "__main__":
    main()
