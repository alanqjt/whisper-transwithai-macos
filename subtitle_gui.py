#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频自动加中文字幕 — 图形界面。

跑在系统自带 Python(含 tkinter)上, 后台调用 ~/auto-subtitle/sub(venv)完成
识别/翻译/合成。支持多视频批量, 每个视频输出: 中文 .srt + 内嵌软字幕的视频
(原视频留在原处, 三者同目录)。
"""
import os
import queue
import subprocess
import threading
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _HAS_DND = True
except Exception:
    _HAS_DND = False

HERE = Path(__file__).resolve().parent
SUB = HERE / "sub"
CHICKEN = "chickenrice0721/whisper-large-v2-translate-zh-v0.2-st-ct2"

# 预设模式 -> {args: 传给 sub 的参数(不含 model), model: 该模式推荐的默认模型}
PRESETS = {
    "日语 → 中文(语音翻译, 推荐)":
        {"args": ["--engine", "faster", "--language", "日语", "--task", "translate"], "model": CHICKEN},
    "中文视频 → 中文字幕":
        {"args": ["--engine", "faster", "--language", "中文"], "model": "large-v3-turbo"},
    "英语 → 中文":
        {"args": ["--engine", "faster", "--language", "英语", "--translator", "argos"], "model": "large-v3-turbo"},
    "其他外语 → 中文(自动检测)":
        {"args": ["--engine", "faster", "--language", "auto", "--translator", "argos"], "model": "large-v3-turbo"},
}

# 模型下拉候选(可手动粘贴任意 HuggingFace 模型名)
MODEL_CHOICES = [
    "large-v3-turbo", "large-v3", "large-v2", "medium", "small", "base",
    CHICKEN,                              # 日→中 语音翻译
    "TransWithAI/whisper-ja-1.5B-ct2",    # 日语原文转写
]

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".ts",
              ".mpg", ".mpeg", ".wmv", ".rmvb", ".rm", ".m4a", ".mp3", ".wav", ".flac"}

# 让子进程一定能找到 homebrew 的 ffmpeg(从 Finder 双击启动时 PATH 很干净)
CHILD_ENV = dict(os.environ)
CHILD_ENV["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + CHILD_ENV.get("PATH", "")


class App:
    def __init__(self, root):
        self.root = root
        root.title("Whisper TransWithAI")
        root.geometry("860x640")
        self.files = []
        self.q = queue.Queue()
        self.proc = None
        self.stop_flag = False
        self.last_outdir = None
        self._build()
        self.root.after(120, self._drain)

    # ---------------------------------------------------------------- UI
    def _build(self):
        pad = dict(padx=8, pady=4)

        # 文件列表
        hint = "视频文件(可多选 / 多次添加" + (" / 拖拽文件或文件夹到此)" if _HAS_DND else ")")
        top = ttk.LabelFrame(self.root, text=hint)
        top.pack(fill="both", expand=False, **pad)
        self.lb = tk.Listbox(top, height=7, selectmode="extended")
        self.lb.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        sb = ttk.Scrollbar(top, command=self.lb.yview)
        sb.pack(side="left", fill="y")
        self.lb.config(yscrollcommand=sb.set)
        if _HAS_DND:  # 注册拖放(列表与外框都可接收)
            for w in (self.lb, top):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)
        btns = ttk.Frame(top)
        btns.pack(side="left", fill="y", padx=6)
        ttk.Button(btns, text="添加视频…", command=self.add_files).pack(fill="x", pady=2)
        ttk.Button(btns, text="移除选中", command=self.remove_sel).pack(fill="x", pady=2)
        ttk.Button(btns, text="清空", command=self.clear_files).pack(fill="x", pady=2)

        # 选项
        opt = ttk.LabelFrame(self.root, text="选项")
        opt.pack(fill="x", **pad)
        ttk.Label(opt, text="模式:").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.preset = ttk.Combobox(opt, values=list(PRESETS), state="readonly", width=34)
        self.preset.current(0)
        self.preset.grid(row=0, column=1, sticky="w", padx=6)
        self.preset.bind("<<ComboboxSelected>>", self._on_preset)

        ttk.Label(opt, text="模型:").grid(row=1, column=0, sticky="w", padx=6, pady=6)
        mframe = ttk.Frame(opt)
        mframe.grid(row=1, column=1, sticky="w", padx=6)
        self.model = ttk.Combobox(mframe, values=MODEL_CHOICES, width=38)  # 可编辑: 可粘贴 HF 名
        self.model.set(PRESETS[self.preset.get()]["model"])
        self.model.pack(side="left")
        ttk.Button(mframe, text="浏览…", width=6, command=self.pick_model)\
            .pack(side="left", padx=4)

        self.var_soft = tk.BooleanVar(value=True)
        self.var_bi = tk.BooleanVar(value=False)
        self.var_keep = tk.BooleanVar(value=False)
        self.var_burn = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="输出软字幕视频(内嵌可开关)", variable=self.var_soft)\
            .grid(row=0, column=2, sticky="w", padx=12)
        ttk.Checkbutton(opt, text="双语字幕(原文+中文)", variable=self.var_bi)\
            .grid(row=1, column=2, sticky="w", padx=12)
        ttk.Checkbutton(opt, text="同时保留原文 .srt", variable=self.var_keep)\
            .grid(row=2, column=2, sticky="w", padx=12)
        ttk.Checkbutton(opt, text="烧录硬字幕视频(需 libass 的 ffmpeg)", variable=self.var_burn)\
            .grid(row=3, column=2, sticky="w", padx=12)
        self.var_del = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="完成后删除原视频(移到废纸篓, 仅成功时)", variable=self.var_del)\
            .grid(row=4, column=2, sticky="w", padx=12)

        ttk.Label(opt, text="单条字幕最长(秒):").grid(row=2, column=0, sticky="w", padx=6)
        self.maxdur = ttk.Entry(opt, width=8)
        self.maxdur.insert(0, "8")
        self.maxdur.grid(row=2, column=1, sticky="w", padx=6)

        ttk.Label(opt, text="输出目录:").grid(row=3, column=0, sticky="w", padx=6)
        self.outdir = ttk.Entry(opt, width=34)
        self.outdir.grid(row=3, column=1, sticky="w", padx=6)
        ttk.Button(opt, text="选择…", width=6, command=self.pick_outdir)\
            .grid(row=3, column=1, sticky="e", padx=6)
        ttk.Label(opt, text="(留空=与原视频同目录)").grid(row=4, column=0, columnspan=2, sticky="w", padx=6)

        # 操作 + 进度
        act = ttk.Frame(self.root)
        act.pack(fill="x", **pad)
        self.btn_start = ttk.Button(act, text="开始处理", command=self.start)
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(act, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.btn_open = ttk.Button(act, text="打开输出目录", command=self.open_outdir, state="disabled")
        self.btn_open.pack(side="left", padx=4)
        self.progress = ttk.Label(act, text="就绪")
        self.progress.pack(side="left", padx=12)

        # 日志
        logf = ttk.LabelFrame(self.root, text="日志")
        logf.pack(fill="both", expand=True, **pad)
        self.log = scrolledtext.ScrolledText(logf, height=14, wrap="word")
        self.log.pack(fill="both", expand=True, padx=6, pady=6)

    # ------------------------------------------------------------ 文件操作
    def _add_one(self, f):
        if f not in self.files:
            self.files.append(f)
            self.lb.insert("end", f)

    def _add_path(self, p):
        p = Path(p)
        if p.is_dir():
            for f in sorted(p.iterdir()):
                if f.suffix.lower() in VIDEO_EXTS:
                    self._add_one(str(f))
        elif p.suffix.lower() in VIDEO_EXTS:
            self._add_one(str(p))

    def add_files(self):
        for f in filedialog.askopenfilenames(title="选择视频"):
            self._add_path(f)

    def on_drop(self, event):
        for p in self.root.tk.splitlist(event.data):
            self._add_path(p)

    def remove_sel(self):
        for i in reversed(self.lb.curselection()):
            self.lb.delete(i)
            del self.files[i]

    def clear_files(self):
        self.lb.delete(0, "end")
        self.files.clear()

    def pick_outdir(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.outdir.delete(0, "end")
            self.outdir.insert(0, d)

    def pick_model(self):
        d = filedialog.askdirectory(title="选择本地模型文件夹(CTranslate2 格式)")
        if d:
            self.model.set(d)

    def open_outdir(self):
        if self.last_outdir:
            subprocess.Popen(["open", str(self.last_outdir)])

    def _on_preset(self, _=None):
        # 切换模式时, 把模型框填成该模式推荐的默认模型(用户仍可改)
        self.model.set(PRESETS[self.preset.get()]["model"])

    # ------------------------------------------------------------ 处理流程
    def start(self):
        if not self.files:
            messagebox.showwarning("提示", "请先添加视频文件")
            return
        try:
            float(self.maxdur.get())
        except ValueError:
            messagebox.showwarning("提示", "单条字幕最长(秒)必须是数字")
            return
        if self.var_del.get() and not messagebox.askyesno(
                "确认删除原视频",
                f"处理成功后,将把 {len(self.files)} 个原视频移到废纸篓"
                "(可从废纸篓恢复)。\n\n失败或无字幕的文件会自动保留。确定继续?"):
            return
        self.stop_flag = False
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.btn_open.config(state="disabled")
        self.log.delete("1.0", "end")
        files = list(self.files)
        preset = PRESETS[self.preset.get()]
        model = self.model.get().strip() or preset["model"]
        args = list(preset["args"]) + ["--model", model, "--max-dur", self.maxdur.get()]
        if self.var_soft.get():
            args.append("--softsub")
        if self.var_burn.get():
            args.append("--burn")
        if self.var_bi.get():
            args.append("--bilingual")
        if self.var_keep.get():
            args.append("--keep-src")
        if self.var_del.get():
            args.append("--delete-source")
        outdir = self.outdir.get().strip()
        if outdir:
            args += ["--outdir", outdir]
        self.last_outdir = outdir or str(Path(files[0]).parent)
        threading.Thread(target=self._worker, args=(files, args), daemon=True).start()

    def _worker(self, files, args):
        n = len(files)
        for i, f in enumerate(files, 1):
            if self.stop_flag:
                break
            self.q.put(("progress", f"处理中 {i}/{n}: {Path(f).name}"))
            self.q.put(("log", f"\n{'='*60}\n[{i}/{n}] {f}\n{'='*60}\n"))
            cmd = [str(SUB), f] + args
            try:
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, env=CHILD_ENV)
            except Exception as e:
                self.q.put(("log", f"启动失败: {e}\n"))
                continue
            # 按 \n 和 \r 切分: \n 行进日志; \r 进度(下载/转写)进状态栏, 不刷屏
            buf = ""
            while True:
                if self.stop_flag:
                    self.proc.terminate()
                    break
                ch = self.proc.stdout.read(1)
                if ch == "":
                    break
                if ch == "\n":
                    if buf.strip() and "frames/s" not in buf:
                        self.q.put(("log", buf + "\n"))
                    buf = ""
                elif ch == "\r":
                    if buf.strip():
                        self.q.put(("status", buf.strip()[:90]))
                    buf = ""
                else:
                    buf += ch
            if buf.strip():
                self.q.put(("log", buf + "\n"))
            self.proc.wait()
            self.proc = None
        self.q.put(("progress", "已停止" if self.stop_flag else f"完成 ({n} 个)"))
        self.q.put(("done", None))

    def stop(self):
        self.stop_flag = True
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass

    # ------------------------------------------------------------ 队列刷新
    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log.insert("end", payload)
                    self.log.see("end")
                elif kind == "progress":
                    self.progress.config(text=payload)
                elif kind == "status":  # 下载/转写进度条 -> 状态栏(实时, 不刷屏)
                    self.progress.config(text=payload)
                elif kind == "done":
                    self.btn_start.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self.btn_open.config(state="normal")
        except queue.Empty:
            pass
        self.root.after(150, self._drain)


def main():
    root = TkinterDnD.Tk() if _HAS_DND else tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
