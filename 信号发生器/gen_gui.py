# -*- coding: utf-8 -*-
"""
信号发生器控制 —— 中文通用版（USB / 网络）

支持的仪器（自动识别，见 drivers.py）：
  Keysight/Agilent 33500B、33600A（已实测）；33120A/33210A/33220A/33250A；
  Rigol DG 系列；鼎阳 SDG 系列；泰克 AFG 系列；其他品牌走“兼容模式”。

运行：双击“启动信号发生器控制.bat”，或命令行 python gen_gui.py
依赖：Keysight IO Libraries Suite（VISA 驱动）、pyvisa、numpy
"""
import os
import sys
import queue
import threading

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

import drivers as drv_mod
from drivers import WAVE_NAMES
import remote_api

# 本机控制接口（给脚本等外部程序用）：方法名 -> 说明
EXT_METHODS = {
    "status": "连接状态和仪器能力",
    "scan": "扫描信号发生器",
    "connect": "连接 [addr=地址，省略则连第一台]",
    "disconnect": "断开（输出保持不变）",
    "read": "读取通道设置 ch=1",
    "apply": "设置波形 ch func freq amp offset phase duty highz（没给的参数保持现值）",
    "output": "开关输出 ch=1 on=true",
    "sum": "叠加干扰 ch enable shape freq percent",
    "sync_phase": "两通道相位对齐",
    "arb": "上传任意波 ch data=[...] srate keep_volts",
    "scpi_write": "直接发 SCPI 命令 cmd=...",
    "scpi_query": "直接发 SCPI 查询 cmd=...",
}

SUM_SHAPES = [("正弦", "SIN"), ("方波", "SQU"), ("三角", "TRI"), ("锯齿", "RAMP"), ("噪声", "NOIS")]
FREQ_UNITS = {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6}
ALL_WAVES = ("SIN", "SQU", "RAMP", "TRI", "PULS", "NOIS", "DC")


class App:
    def __init__(self, root):
        self.root = root
        self.rm = None
        self.drv = None          # 当前连接的驱动对象
        self.found = []          # 扫描结果 [(地址, IDN, 驱动类)]
        self.busy = False
        self.q = queue.Queue()
        self.ext_q = queue.Queue()      # 外部控制线程交给界面线程做的事
        self.io_lock = threading.Lock() # 界面操作和外部控制轮流使用仪器，不会同时发命令
        self.arb_path = ""
        self.wave_codes = list(ALL_WAVES)

        root.title("信号发生器控制（通用版）")
        root.minsize(780, 560)
        font = ("Microsoft YaHei UI", 10)
        root.option_add("*Font", font)
        style = ttk.Style()
        style.configure(".", font=font)
        style.configure("Big.TButton", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Red.TButton", foreground="#b00020", font=("Microsoft YaHei UI", 10, "bold"))

        self._build()
        self._apply_caps()
        root.after(100, self._poll)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.ext = remote_api.ControlServer("信号发生器控制", remote_api.GEN_PORT, self._ext_dispatch, EXT_METHODS)
        if self.ext.start():
            self._log("后台控制已开启：电脑上的其他程序（例如 AI 助手）可以通过它操作仪器；"
                      "不想被后台控制时，取消勾选“允许后台控制”。", "cmd")
        else:
            self.ext_var.set(False)
            self._log("后台控制没能开启（可能已经有另一份程序在运行）。", "err")

    # ======================================================================
    # 布局
    # ======================================================================
    def _build(self):
        pad = dict(padx=6, pady=3)
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)

        # ① 连接
        f = ttk.LabelFrame(main, text="① 连接", padding=6)
        f.grid(row=0, column=0, columnspan=2, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        ttk.Button(f, text="扫描仪器", command=self.on_scan).grid(row=0, column=0, sticky="w")
        self.dev = ttk.Combobox(f, state="readonly", values=["（先点“扫描仪器”）"])
        self.dev.current(0)
        self.dev.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f, text="连接", style="Big.TButton", command=self.on_connect).grid(row=0, column=2)
        ttk.Button(f, text="断开", command=self.on_disconnect).grid(row=0, column=3, padx=(4, 0))
        self.lbl_conn = ttk.Label(f, text="未连接", foreground="#888")
        self.lbl_conn.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))
        self.ext_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="允许后台控制", variable=self.ext_var,
                        command=self._on_ext_toggle).grid(row=1, column=3, sticky="e", pady=(2, 0))

        # ② 通道
        f = ttk.LabelFrame(main, text="② 选择通道", padding=6)
        f.grid(row=1, column=0, columnspan=2, sticky="ew", **pad)
        self.ch = tk.IntVar(value=1)
        self.rb_ch = {}
        for c in (1, 2):
            self.rb_ch[c] = ttk.Radiobutton(f, text=f"通道 {c}", variable=self.ch, value=c, command=self.on_read)
            self.rb_ch[c].pack(side="left", padx=6)
        ttk.Button(f, text="读取仪器当前设置", command=self.on_read).pack(side="left", padx=12)
        ttk.Label(f, text="下方③⑤⑥的设置作用于这里选中的通道", foreground="#666").pack(side="left", padx=8)

        # ③ 基本波形
        f = ttk.LabelFrame(main, text="③ 基本波形", padding=6)
        f.grid(row=2, column=0, sticky="nsew", **pad)
        f.columnconfigure(3, weight=1)
        r = 0
        ttk.Label(f, text="波形").grid(row=r, column=0, sticky="w")
        self.wave = ttk.Combobox(f, state="readonly", width=16)
        self.wave.grid(row=r, column=1, columnspan=2, sticky="w")
        self.wave.bind("<<ComboboxSelected>>", lambda e: self._on_wave_change())
        r += 1
        self.lbl_freq = ttk.Label(f, text="频率")
        self.lbl_freq.grid(row=r, column=0, sticky="w")
        self.freq = ttk.Entry(f, width=12)
        self.freq.insert(0, "1")
        self.freq.grid(row=r, column=1, sticky="w")
        self.freq_unit = ttk.Combobox(f, values=list(FREQ_UNITS), state="readonly", width=5)
        self.freq_unit.set("kHz")
        self.freq_unit.grid(row=r, column=2, sticky="w")
        r += 1
        ttk.Label(f, text="幅度（峰峰值）").grid(row=r, column=0, sticky="w")
        self.amp = ttk.Entry(f, width=12)
        self.amp.insert(0, "1")
        self.amp.grid(row=r, column=1, sticky="w")
        ttk.Label(f, text="Vpp").grid(row=r, column=2, sticky="w")
        r += 1
        self.lbl_off = ttk.Label(f, text="直流偏置")
        self.lbl_off.grid(row=r, column=0, sticky="w")
        self.offset = ttk.Entry(f, width=12)
        self.offset.insert(0, "0")
        self.offset.grid(row=r, column=1, sticky="w")
        ttk.Label(f, text="V").grid(row=r, column=2, sticky="w")
        r += 1
        ttk.Label(f, text="相位").grid(row=r, column=0, sticky="w")
        self.phase = ttk.Entry(f, width=12)
        self.phase.insert(0, "0")
        self.phase.grid(row=r, column=1, sticky="w")
        ttk.Label(f, text="°").grid(row=r, column=2, sticky="w")
        r += 1
        self.lbl_duty = ttk.Label(f, text="占空比")
        self.lbl_duty.grid(row=r, column=0, sticky="w")
        self.duty = ttk.Entry(f, width=12)
        self.duty.insert(0, "50")
        self.duty.grid(row=r, column=1, sticky="w")
        ttk.Label(f, text="%").grid(row=r, column=2, sticky="w")
        r += 1
        ttk.Label(f, text="负载").grid(row=r, column=0, sticky="w")
        self.highz = tk.BooleanVar(value=True)
        lf = ttk.Frame(f)
        lf.grid(row=r, column=1, columnspan=3, sticky="w")
        ttk.Radiobutton(lf, text="高阻（接示波器/电路）", variable=self.highz, value=True).pack(side="left")
        ttk.Radiobutton(lf, text="50Ω", variable=self.highz, value=False).pack(side="left", padx=6)
        r += 1
        self.lbl_range = ttk.Label(f, text="", foreground="#555")
        self.lbl_range.grid(row=r, column=0, columnspan=4, sticky="w", pady=(4, 0))
        for e in (self.amp, self.offset):
            e.bind("<KeyRelease>", lambda ev: self._update_range())
        r += 1
        ttk.Button(f, text="应用设置", style="Big.TButton", command=self.on_apply).grid(
            row=r, column=0, columnspan=4, sticky="ew", pady=(8, 0))

        right = ttk.Frame(main)
        right.grid(row=2, column=1, sticky="nsew", **pad)
        right.columnconfigure(0, weight=1)

        # ④ 输出
        f = ttk.LabelFrame(right, text="④ 输出开关（两个通道可以同时输出）", padding=6)
        f.grid(row=0, column=0, sticky="ew")
        for c in (1, 2):
            f.columnconfigure(c, weight=1)
        self.lbl_st, self.out_btns = {}, {}
        for i, c in enumerate((1, 2)):
            ttk.Label(f, text=f"CH{c}", font=("Microsoft YaHei UI", 10, "bold")).grid(row=i, column=0, sticky="w")
            b1 = ttk.Button(f, text="打开", style="Big.TButton", command=lambda c=c: self.on_output(c, True))
            b1.grid(row=i, column=1, sticky="ew", padx=2, pady=2)
            b2 = ttk.Button(f, text="关闭", command=lambda c=c: self.on_output(c, False))
            b2.grid(row=i, column=2, sticky="ew", padx=2, pady=2)
            self.out_btns[c] = (b1, b2)
            self.lbl_st[c] = ttk.Label(f, text="？", width=8, foreground="#888")
            self.lbl_st[c].grid(row=i, column=3, sticky="w", padx=(6, 0))
        self.btn_all_on = ttk.Button(f, text="两路同时打开", command=self.on_all_on)
        self.btn_all_on.grid(row=2, column=0, columnspan=2, sticky="ew", padx=2, pady=(6, 2))
        ttk.Button(f, text="全部关闭", style="Red.TButton", command=self.on_all_off).grid(
            row=2, column=2, columnspan=2, sticky="ew", padx=2, pady=(6, 2))
        self.btn_sync = ttk.Button(f, text="两通道相位对齐（同频率时让相位差准确）", command=self.on_sync)
        self.btn_sync.grid(row=3, column=0, columnspan=4, sticky="ew", padx=2, pady=2)

        # ⑤ 叠加
        self.f_sum = f = ttk.LabelFrame(right, text="⑤ 叠加干扰（Sum）", padding=6)
        f.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        f.columnconfigure(3, weight=1)
        ttk.Label(f, text="叠加形状").grid(row=0, column=0, sticky="w")
        self.sum_shape = ttk.Combobox(f, values=[n for n, _ in SUM_SHAPES], state="readonly", width=8)
        self.sum_shape.current(0)
        self.sum_shape.grid(row=0, column=1, sticky="w")
        ttk.Label(f, text="叠加频率").grid(row=1, column=0, sticky="w")
        self.sum_freq = ttk.Entry(f, width=10)
        self.sum_freq.insert(0, "50")
        self.sum_freq.grid(row=1, column=1, sticky="w")
        ttk.Label(f, text="Hz").grid(row=1, column=2, sticky="w")
        ttk.Label(f, text="叠加幅度").grid(row=2, column=0, sticky="w")
        self.sum_pct = ttk.Entry(f, width=10)
        self.sum_pct.insert(0, "20")
        self.sum_pct.grid(row=2, column=1, sticky="w")
        ttk.Label(f, text="% 主信号").grid(row=2, column=2, sticky="w")
        bf = ttk.Frame(f)
        bf.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        ttk.Button(bf, text="开启叠加", command=lambda: self.on_sum(True)).pack(side="left", expand=True, fill="x")
        ttk.Button(bf, text="关闭叠加", command=lambda: self.on_sum(False)).pack(side="left", expand=True,
                                                                         fill="x", padx=(4, 0))
        self.lbl_sum = ttk.Label(f, text="例：主信号 10 Hz 正弦 + 50 Hz 正弦 20% = 模拟工频干扰", foreground="#666",
                                 wraplength=330)
        self.lbl_sum.grid(row=4, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # ⑥ 任意波
        self.f_arb = f = ttk.LabelFrame(main, text="⑥ 导入任意波（CSV / TXT，如 EEG 数据）", padding=6)
        f.grid(row=3, column=0, columnspan=2, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        ttk.Button(f, text="选择文件…", command=self.on_pick_file).grid(row=0, column=0, sticky="w")
        self.lbl_file = ttk.Label(f, text="未选择文件", foreground="#888")
        self.lbl_file.grid(row=0, column=1, sticky="w", padx=6)
        self.lbl_arb = ttk.Label(f, text="", foreground="#666")
        self.lbl_arb.grid(row=0, column=1, sticky="e", padx=6)
        ttk.Button(f, text="上传并播放", style="Big.TButton", command=self.on_arb).grid(row=0, column=2, sticky="e")
        rf = ttk.Frame(f)
        rf.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(rf, text="数据列").pack(side="left")
        self.arb_col = ttk.Entry(rf, width=5)
        self.arb_col.insert(0, "-1")
        self.arb_col.pack(side="left", padx=(4, 0))
        ttk.Label(rf, text="（1=第一列，-1=最后一列）", foreground="#666").pack(side="left")
        ttk.Label(rf, text="采样率").pack(side="left", padx=(12, 0))
        self.arb_srate = ttk.Entry(rf, width=9)
        self.arb_srate.insert(0, "1000")
        self.arb_srate.pack(side="left", padx=(4, 0))
        ttk.Label(rf, text="点/秒（填原始数据的采样率）", foreground="#666").pack(side="left")
        self.arb_keep = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="把文件里的数值直接当作电压（单位 V）；不勾选则使用 ③ 里的“幅度 / 偏置”",
                        variable=self.arb_keep).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # 信息 + 手动命令
        f = ttk.LabelFrame(main, text="信息 / 手动命令（不认识的仪器可以在这里直接发 SCPI 命令调试）", padding=4)
        f.grid(row=4, column=0, columnspan=2, sticky="nsew", **pad)
        main.rowconfigure(4, weight=1)
        cf = ttk.Frame(f)
        cf.pack(fill="x", pady=(0, 4))
        self.cmd = ttk.Entry(cf)
        self.cmd.pack(side="left", fill="x", expand=True)
        self.cmd.bind("<Return>", lambda e: self.on_cmd(self.cmd.get().strip().endswith("?")))
        ttk.Button(cf, text="发送", command=lambda: self.on_cmd(False)).pack(side="left", padx=(4, 0))
        ttk.Button(cf, text="发送并读取", command=lambda: self.on_cmd(True)).pack(side="left", padx=(4, 0))
        self.log = ScrolledText(f, height=4, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("err", foreground="#b00020")
        self.log.tag_configure("ok", foreground="#1b7f3b")
        self.log.tag_configure("cmd", foreground="#1a4fa0")
        self.log.tag_configure("ext", foreground="#7a3db8")
        self._log("提示：先点“扫描仪器”，选择仪器后点“连接”。设置好参数后点“应用设置”，再在 ④ 里打开对应通道的输出。")

    # ======================================================================
    # 根据仪器能力调整界面
    # ======================================================================
    def _set_enabled(self, widget, on):
        for w in widget.winfo_children():
            if isinstance(w, (ttk.Frame, ttk.LabelFrame)):
                self._set_enabled(w, on)
                continue
            try:
                w.state(["!disabled"] if on else ["disabled"])
            except Exception:
                pass

    def _apply_caps(self):
        d = self.drv
        waves = [w for w in (d.waves if d else ALL_WAVES)]
        self.wave_codes = waves
        cur = self._wave_code() if self.wave.get() else "SIN"
        self.wave.configure(values=[WAVE_NAMES[w] for w in waves])
        self.wave.current(waves.index(cur) if cur in waves else 0)

        two = (d.channels if d else 2) == 2
        self.rb_ch[2].state(["!disabled"] if two else ["disabled"])
        if not two:
            self.ch.set(1)
        for b in self.out_btns[2]:
            b.state(["!disabled"] if two else ["disabled"])
        self.btn_all_on.state(["!disabled"] if two else ["disabled"])
        self.btn_sync.state(["!disabled"] if (d is None or (d.has_phase_sync and two)) else ["disabled"])

        has_sum = d is None or d.has_sum
        self._set_enabled(self.f_sum, has_sum)
        self.lbl_sum.configure(text="例：主信号 10 Hz 正弦 + 50 Hz 正弦 20% = 模拟工频干扰" if has_sum
                               else "当前仪器（或驱动）不支持叠加功能")
        has_arb = d is None or d.has_arb
        self._set_enabled(self.f_arb, has_arb)
        self.lbl_arb.configure(text=(f"这台仪器最多 {d.max_arb_points} 点" if d and d.has_arb else
                                     "当前仪器（或驱动）不支持上传任意波" if d else ""))
        self._on_wave_change()

    # ======================================================================
    # 工具
    # ======================================================================
    def _log(self, msg, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _num(self, entry, name):
        txt = entry.get().strip()
        try:
            return float(txt)
        except ValueError:
            raise ValueError(f"“{name}”请输入数字（现在是：{txt or '空'}）")

    def _wave_code(self):
        i = self.wave.current()
        return self.wave_codes[i] if 0 <= i < len(self.wave_codes) else "SIN"

    def _on_wave_change(self):
        code = self._wave_code()
        d = self.drv
        noise_bw = d is None or d.has_noise_bw
        duty_waves = d.duty_waves if d else ("SQU", "PULS", "RAMP")
        self.lbl_freq.configure(text="噪声带宽" if code == "NOIS" else "频率")
        self.lbl_duty.configure(text="对称度" if code == "RAMP" else "占空比")
        self.freq.configure(state="disabled" if code == "DC" or (code == "NOIS" and not noise_bw) else "normal")
        self.amp.configure(state="disabled" if code == "DC" else "normal")
        self.duty.configure(state="normal" if code in duty_waves else "disabled")
        self.phase.configure(state="disabled" if code in ("NOIS", "DC") else "normal")
        self.lbl_off.configure(text="直流电压" if code == "DC" else "直流偏置")
        if code == "RAMP" and self.duty.get().strip() in ("", "50"):
            self.duty.delete(0, "end")
            self.duty.insert(0, "100")
        self._update_range()

    def _update_range(self):
        try:
            off = float(self.offset.get())
            amp = 0.0 if self._wave_code() == "DC" else float(self.amp.get())
            self.lbl_range.configure(text=f"输出电压范围：{off - amp / 2:+.4g} V  ～  {off + amp / 2:+.4g} V")
        except ValueError:
            self.lbl_range.configure(text="")

    def _set_fields(self, d):
        code = d.get("func")
        if code in self.wave_codes:
            self.wave.current(self.wave_codes.index(code))
        if "freq" in d:
            f = d["freq"]
            unit = "MHz" if f >= 1e6 else "kHz" if f >= 1e3 else "Hz"
            self.freq_unit.set(unit)
            d = dict(d, freq=f / FREQ_UNITS[unit])
        for key, e in (("freq", self.freq), ("amp", self.amp), ("offset", self.offset),
                       ("phase", self.phase), ("duty", self.duty)):
            if key in d and d[key] is not None:
                e.configure(state="normal")
                e.delete(0, "end")
                e.insert(0, f"{d[key]:.10g}")
        if d.get("highz") is not None:
            self.highz.set(d["highz"])
        self._on_wave_change()

    def _show_errors(self, errs):
        if errs:
            for e in errs:
                self._log(f"仪器报错：{e}", "err")
            self._log("（常见原因：数值超出范围；如果是未实测的驱动，也可能是命令写法和这台仪器不一致，"
                      "可以在“手动命令”框里试正确写法后反馈修改）", "err")
            return False
        return True

    def _show_out(self, on1, on2):
        for c, on in ((1, on1), (2, on2)):
            if on is None:
                self.lbl_st[c].configure(text="？", foreground="#888")
            else:
                self.lbl_st[c].configure(text="● 输出中" if on else "○ 已关闭",
                                         foreground="#1b7f3b" if on else "#888")

    def _out_states(self):
        s1 = self.drv.output_state(1)
        s2 = self.drv.output_state(2) if self.drv.channels == 2 else None
        return s1, s2

    # ======================================================================
    # 后台执行（避免界面卡住）
    # ======================================================================
    def _run(self, desc, work, done=None, need_conn=True):
        if self.busy:
            self._log("上一个操作还没完成，请稍候……")
            return
        if need_conn and self.drv is None:
            self._log("还没连接仪器，请先“扫描仪器”并“连接”。", "err")
            return
        self.busy = True
        self.root.configure(cursor="watch")
        self._log(desc + "……")

        def task():
            res, err = None, None
            try:
                with self.io_lock:
                    res = work()
            except Exception as e:  # noqa: BLE001
                err = e
            # 每次操作完都把面板交还给仪器：即使保持连接，也能直接在仪器上手动操作，不用按 Local
            drv = res if isinstance(res, drv_mod.Driver) else self.drv
            if drv is not None:
                drv.go_local()
            self.q.put((done, res, err))

        threading.Thread(target=task, daemon=True).start()

    def _poll(self):
        try:
            while True:
                done, res, err = self.q.get_nowait()
                self.busy = False
                self.root.configure(cursor="")
                if err is not None:
                    self._log(f"出错：{err}", "err")
                elif done:
                    done(res)
        except queue.Empty:
            pass
        try:
            while True:
                self.ext_q.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _refresh_out(self):
        self._run("刷新输出状态", self._out_states, lambda st: self._show_out(*st))

    # ======================================================================
    # 按钮
    # ======================================================================
    def on_scan(self):
        def work():
            if self.rm is None:
                self.rm = drv_mod.open_rm()
            return drv_mod.scan(self.rm)

        def done(found):
            self.found = found
            if not found:
                self.dev.configure(values=["（没有找到信号发生器）"])
                self.dev.current(0)
                self._log("没有找到信号发生器。检查：USB 线是否插在仪器【背面】、仪器是否开机、"
                          "Keysight Connection Expert 里能否看到它。", "err")
                if drv_mod.visa_stuck():
                    self._log("原因：Keysight 仪器通信软件卡住了（VI_ERROR_ALLOC）。", "err")
                    messagebox.showerror(drv_mod.ALLOC_TITLE, drv_mod.ALLOC_HELP)
                return
            items = []
            for addr, idn, cls in found:
                model = idn.split(",")[1].strip() if "," in idn else idn
                items.append(f"{model}  [{cls.family}{'，已实测' if cls.is_tested(idn) else '，未实测'}]  {addr}")
            self.dev.configure(values=items)
            self.dev.current(0)
            self._log(f"找到 {len(found)} 台信号发生器。选好后点“连接”。", "ok")
        self._run("正在扫描 USB / 网络上的仪器", work, done, need_conn=False)

    def on_connect(self):
        if not self.found:
            self._log("请先点“扫描仪器”。", "err")
            return
        addr, idn, cls = self.found[self.dev.current()]
        if self.drv is not None:
            self.drv.close()
            self.drv = None

        def done(d):
            self.drv = d
            self.lbl_conn.configure(text=f"已连接：{d.describe()}",
                                    foreground="#1b7f3b" if d.is_tested(idn) else "#a15c00")
            self._log(f"连接成功：{idn}", "ok")
            if not d.is_tested(idn):
                self._log("注意：这台仪器的驱动还没有用真机测试过。第一次使用时，请先只接示波器、幅度设小一点，"
                          "确认输出和设置一致后再接电路。", "err")
            self._apply_caps()
            self.root.after(50, self.on_read)
        self._run(f"正在连接 {addr}", lambda: drv_mod.connect(self.rm, addr, idn, cls), done, need_conn=False)

    def on_disconnect(self):
        if self.drv is None:
            self._log("当前没有连接。")
            return
        self.drv.close()
        self.drv = None
        self.lbl_conn.configure(text="未连接", foreground="#888")
        self._show_out(None, None)
        self._apply_caps()
        self._log("已断开，仪器前面板可以手动操作了（输出状态保持不变）。")

    def on_read(self):
        if self.drv is None:
            return
        ch = self.ch.get()

        def work():
            d = self.drv.read_settings(ch)
            return d, self._out_states()

        def done(res):
            d, st = res
            self._set_fields(d)
            self._show_out(*st)
            parts = [WAVE_NAMES.get(d.get("func"), str(d.get("func", "？")))]
            if "freq" in d:
                parts.append(f"{d['freq']:.6g} Hz")
            if "amp" in d:
                parts.append(f"{d['amp']:.4g} Vpp")
            if "offset" in d:
                parts.append(f"偏置 {d['offset']:.4g} V")
            if d.get("highz") is not None:
                parts.append(f"负载 {'高阻' if d['highz'] else '50Ω'}")
            if d.get("out") is not None:
                parts.append(f"输出 {'开' if d['out'] else '关'}")
            self._log(f"CH{ch} 当前：" + "，".join(parts), "ok")
        self._run(f"读取通道 {ch} 设置", work, done)

    def on_apply(self):
        try:
            code = self._wave_code()
            p = dict(func=code, highz=self.highz.get(), offset=self._num(self.offset, "偏置"))
            if code != "DC":
                p["amp"] = self._num(self.amp, "幅度")
                if str(self.freq.cget("state")) != "disabled":
                    p["freq"] = self._num(self.freq, "频率") * FREQ_UNITS[self.freq_unit.get()]
            if code not in ("NOIS", "DC"):
                p["phase"] = self._num(self.phase, "相位")
            if str(self.duty.cget("state")) != "disabled":
                p["duty"] = self._num(self.duty, "占空比 / 对称度")
        except ValueError as e:
            self._log(str(e), "err")
            return
        ch = self.ch.get()

        def done(errs):
            if self._show_errors(errs):
                a = p.get("amp", 0.0)
                self._log(f"CH{ch} 设置完成。输出电压范围：{p['offset'] - a / 2:+.4g} V ～ "
                          f"{p['offset'] + a / 2:+.4g} V", "ok")
                self._log("（如果该通道输出还没打开，点 ④ 里对应通道的“打开”）")
            self._update_range()
        self._run(f"设置通道 {ch}", lambda: self.drv.apply(ch, **p), done)

    def on_output(self, ch, on):
        def done(errs):
            if self._show_errors(errs):
                self._log(f"CH{ch} 输出已{'打开' if on else '关闭'}。", "ok")
                self._refresh_out()
        self._run(f"{'打开' if on else '关闭'}通道 {ch} 输出", lambda: self.drv.output(ch, on), done)

    def on_all_on(self):
        def done(errs):
            if self._show_errors(errs):
                self._log("两个通道都在输出。", "ok")
                self._refresh_out()
        self._run("打开两路输出", lambda: self.drv.output(1, True) + self.drv.output(2, True), done)

    def on_all_off(self):
        def work():
            errs = self.drv.output(1, False)
            if self.drv.channels == 2:
                errs += self.drv.output(2, False)
            return errs

        def done(errs):
            if self._show_errors(errs):
                self._log("所有通道的输出都已关闭。", "ok")
                self._refresh_out()
        self._run("关闭全部输出", work, done)

    def on_sync(self):
        def done(errs):
            if self._show_errors(errs):
                self._log("两通道相位已对齐。两路频率相同时，它们的相位差 = CH2 相位 − CH1 相位。", "ok")
        self._run("对齐两通道相位", self.drv.sync_phase, done)

    def on_sum(self, enable):
        ch = self.ch.get()
        try:
            shape = SUM_SHAPES[self.sum_shape.current()][1]
            freq = self._num(self.sum_freq, "叠加频率")
            pct = self._num(self.sum_pct, "叠加幅度")
        except ValueError as e:
            self._log(str(e), "err")
            return

        def done(errs):
            if self._show_errors(errs):
                self._log(f"CH{ch} 叠加已{'开启' if enable else '关闭'}。", "ok")
        self._run(f"{'开启' if enable else '关闭'}通道 {ch} 叠加",
                  lambda: self.drv.set_sum(ch, enable, shape, freq, pct), done)

    def on_pick_file(self):
        path = filedialog.askopenfilename(title="选择波形数据文件",
                                          filetypes=[("数据文件", "*.csv *.txt *.dat"), ("所有文件", "*.*")])
        if path:
            self.arb_path = path
            self.lbl_file.configure(text=path, foreground="#000")

    def on_arb(self):
        if not self.arb_path:
            self._log("请先点“选择文件…”。", "err")
            return
        try:
            col = int(float(self.arb_col.get()))
            srate = self._num(self.arb_srate, "采样率")
            amp = self._num(self.amp, "幅度")
            off = self._num(self.offset, "偏置")
            data = drv_mod.load_numbers(self.arb_path, col)
        except (ValueError, OSError) as e:
            self._log(str(e), "err")
            return
        keep = self.arb_keep.get()
        ch = self.ch.get()
        self._log(f"读到 {data.size} 个点，最小 {data.min():.6g}，最大 {data.max():.6g}")

        def done(res):
            errs, info = res
            if self._show_errors(errs):
                self._log(f"CH{ch} 任意波上传完成：{info['points']} 点，播放一遍 {info['duration']:.6g} 秒（循环播放），"
                          f"幅度 {info['amp']:.4g} Vpp，偏置 {info['offset']:.4g} V。", "ok")
                self._log("（如果该通道输出还没打开，点 ④ 里对应通道的“打开”）")
        self._run(f"上传任意波到通道 {ch}（点数多时需要几秒）",
                  lambda: self.drv.upload_arb(ch, data, srate, keep, amp, off), done)

    def on_cmd(self, read):
        cmd = self.cmd.get().strip()
        if not cmd:
            return

        def work():
            if read:
                return self.drv.query(cmd), self.drv.errors()
            self.drv.write(cmd)
            return None, self.drv.errors()

        def done(res):
            resp, errs = res
            self._log(f"→ {cmd}", "cmd")
            if resp is not None:
                self._log(f"← {resp}", "cmd")
            if self._show_errors(errs) and resp is None:
                self._log("（已发送，仪器没有报错）", "ok")
        self._run("发送命令", work, done)

    # ======================================================================
    # 外部控制（脚本等外部程序通过本机接口调用；在接口线程里执行，用 io_lock 和界面操作轮流使用仪器）
    # ======================================================================
    def _on_ext_toggle(self):
        self.ext.enabled = self.ext_var.get()
        self._log("已允许后台控制。" if self.ext.enabled else "已关闭后台控制：后台发来的操作会被拒绝。", "ext")

    def _ext_ui(self, fn):
        self.ext_q.put(fn)

    def _ext_need(self):
        if self.drv is None:
            raise RuntimeError("信号发生器还没连接（可先调用 connect）")
        return self.drv

    def _ext_after_change(self, msg):
        """外部改了设置：界面日志记一笔，并刷新界面上的数值和输出状态"""
        def ui():
            self._log(f"【后台】{msg}", "ext")
            if not self.busy and self.drv is not None:
                self.on_read()
        self._ext_ui(ui)

    def _ext_dispatch(self, method, a):
        if method == "status":
            d = self.drv
            if d is None:
                return {"connected": False}
            return {"connected": True, "idn": d.idn, "describe": d.describe(), "tested": d.is_tested(d.idn),
                    "channels": d.channels, "waves": d.waves, "duty_waves": d.duty_waves, "has_sum": d.has_sum,
                    "has_arb": d.has_arb, "max_arb_points": d.max_arb_points, "has_phase_sync": d.has_phase_sync}
        with self.io_lock:
            if method == "scan":
                if self.rm is None:
                    self.rm = drv_mod.open_rm()
                found = drv_mod.scan(self.rm)
                self.found = found

                def ui():
                    items = [f"{i.split(',')[1].strip() if ',' in i else i}  [{c.family}]  {a_}" for a_, i, c in found]
                    self.dev.configure(values=items or ["（没有找到信号发生器）"])
                    self.dev.current(0)
                    self._log(f"【后台】扫描：找到 {len(found)} 台", "ext")
                self._ext_ui(ui)
                return [{"addr": a_, "idn": i, "driver": c.__name__, "tested": c.is_tested(i)} for a_, i, c in found]

            if method == "connect":
                if self.rm is None:
                    self.rm = drv_mod.open_rm()
                if not self.found or a.get("addr"):
                    self.found = drv_mod.scan(self.rm)
                pick = [f for f in self.found if not a.get("addr") or f[0] == a["addr"]]
                if not pick:
                    raise RuntimeError("没有找到信号发生器" + (f"：{a['addr']}" if a.get("addr") else ""))
                if self.drv is not None:
                    self.drv.close()
                d = drv_mod.connect(self.rm, *pick[0])
                d.go_local()
                self.drv = d

                def ui():
                    self.lbl_conn.configure(text=f"已连接：{d.describe()}",
                                            foreground="#1b7f3b" if d.is_tested(d.idn) else "#a15c00")
                    self._apply_caps()
                    self._log(f"【后台】连接：{d.idn}", "ext")
                    self.on_read()
                self._ext_ui(ui)
                return {"connected": True, "idn": d.idn, "describe": d.describe()}

            if method == "disconnect":
                if self.drv is not None:
                    self.drv.close()
                self.drv = None

                def ui():
                    self.lbl_conn.configure(text="未连接", foreground="#888")
                    self._show_out(None, None)
                    self._apply_caps()
                    self._log("【后台】断开", "ext")
                self._ext_ui(ui)
                return {"connected": False}

            d = self._ext_need()
            try:
                ch = int(a.get("ch", 1))
                if method == "read":
                    s = d.read_settings(ch)
                    s["out"] = d.output_state(ch)
                    return s
                if method == "apply":
                    cur = d.read_settings(ch)
                    p = {"func": cur.get("func", "SIN"), "freq": cur.get("freq", 1000.0), "amp": cur.get("amp", 1.0),
                         "offset": cur.get("offset", 0.0), "phase": cur.get("phase", 0.0),
                         "duty": cur.get("duty", 50.0), "highz": cur.get("highz", True)}
                    p.update({k: a[k] for k in p if k in a})
                    p["func"] = str(p["func"]).upper()
                    if p["func"] not in d.waves:
                        raise ValueError(f"这台仪器支持的波形：{d.waves}")
                    errs = d.apply(ch, p["func"], float(p["freq"]), float(p["amp"]), float(p["offset"]),
                                   float(p["phase"]), float(p["duty"]), bool(p["highz"]))
                    self._ext_after_change(f"CH{ch} 设为 {WAVE_NAMES.get(p['func'], p['func'])} {p['freq']:g} Hz "
                                           f"{p['amp']:g} Vpp 偏置 {p['offset']:g} V" + (f"  仪器报错：{errs}" if errs else ""))
                    return {"errors": errs, "settings": d.read_settings(ch)}
                if method == "output":
                    on = bool(a.get("on", True))
                    errs = d.output(ch, on)
                    self._ext_after_change(f"CH{ch} 输出{'打开' if on else '关闭'}")
                    return {"errors": errs, "out": d.output_state(ch)}
                if method == "sum":
                    en = bool(a.get("enable", True))
                    errs = d.set_sum(ch, en, str(a.get("shape", "SIN")).upper(), float(a.get("freq", 50.0)),
                                     float(a.get("percent", 20.0))) if en else d.set_sum(ch, False)
                    self._ext_after_change(f"CH{ch} 叠加{'打开' if en else '关闭'}")
                    return {"errors": errs}
                if method == "sync_phase":
                    errs = d.sync_phase()
                    self._ext_after_change("两通道相位对齐")
                    return {"errors": errs}
                if method == "arb":
                    import numpy as np
                    data = np.asarray(a["data"], dtype=float)
                    res = d.upload_arb(ch, data, float(a.get("srate", 1e5)), bool(a.get("keep_volts", True)))
                    self._ext_after_change(f"CH{ch} 上传任意波 {len(data)} 点")
                    return res
                if method == "scpi_write":
                    d.write(a["cmd"])
                    self._ext_after_change(f"SCPI 写：{a['cmd']}")
                    return {"errors": d.errors()}
                if method == "scpi_query":
                    r = d.query(a["cmd"])
                    self._ext_ui(lambda: self._log(f"【后台】SCPI 查询：{a['cmd']} → {r}", "ext"))
                    return r
                raise RuntimeError(f"未实现的方法：{method}")
            finally:
                d.go_local()          # 每次都把面板交还，可以随时手动操作

    def _on_close(self):
        try:
            self.ext.stop()
        except Exception:
            pass
        try:
            if self.drv is not None:
                self.drv.close()
        finally:
            self.root.destroy()


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)   # 高分屏下字体清晰（须在创建窗口前调用）
    except Exception:
        pass
    if not drv_mod.single_instance("gen_gui"):
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo("已经打开了", "信号发生器控制程序已经在运行（可能被其他窗口挡住了，或最小化在任务栏）。\n"
                                       "同时打开两份会互相干扰，所以这次不再打开新的。")
        r.destroy()
        return
    drv_mod.set_app_id("gen")
    root = tk.Tk()
    drv_mod.set_icon(root, "gen.ico", os.path.dirname(os.path.abspath(__file__)))
    App(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # 用 pythonw 启动时没有控制台，用弹窗显示错误
        try:
            messagebox.showerror("程序出错", str(exc))
        except Exception:
            print(exc)
        sys.exit(1)
