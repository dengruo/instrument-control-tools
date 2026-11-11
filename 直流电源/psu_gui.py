"""
直流电源电脑控制（通用版）—— 中文界面
  · 每个通道：实测电压/电流大字显示、状态（OFF/CV/CC/OV/OC）、直接输入设定电压和限流、开关输出、过压/过流保护
  · 全部打开 / 全部关闭；打开输出前弹窗确认（可关闭）
  · 电压/电流曲线实时记录，可保存 CSV 和图片
  · 后台控制接口（给 AI 助手等程序用），界面上可随时关闭
支持的电源品牌见 psu.py 开头；连接时自动识别。
"""
import bisect
import math
import os
import re
import sys
import time
import queue
import threading
import datetime

try:
    from ctypes import windll
    windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

import tkinter as tk
from tkinter import ttk, messagebox

# VISA 库必须在 matplotlib 之前加载，否则 ktvisa32.dll 初始化失败（WinError 1114）
import psu as P            # noqa: E402
import remote_api          # noqa: E402
gen = P.gen
try:
    RM, RM_ERR = gen.open_rm(), None
except Exception as _e:
    RM, RM_ERR = None, _e

import matplotlib          # noqa: E402
matplotlib.use("TkAgg")
matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False
from matplotlib.figure import Figure                                  # noqa: E402
from matplotlib.lines import Line2D                                   # noqa: E402
import numpy as np                                                    # noqa: E402
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg       # noqa: E402

SAVE_DIR = os.path.join(P.HERE, "记录")
CH_COLORS = ["#d9a400", "#2e9d3a", "#2f7fd8", "#b0459c"]          # 黄、绿、蓝（和 Keysight 面板一致）、紫
CH_BG = ["#fff6d6", "#e3f5e2", "#e0edfb", "#f5e3f1"]
STATUS_STYLE = {"OFF": ("OFF 关闭", "#888888"), "CV": ("CV 恒压", "#1b7f3b"), "CC": ("CC 恒流", "#d07000"),
                "OV": ("OV 过压保护", "#c00000"), "OC": ("OC 过流保护", "#c00000"), "?": ("？", "#888888")}
POLL_S = 0.4
FULL_MEAS_S = 5.0        # 关着的通道多久测一次实测值（正在输出的通道每轮都测）
ANIM_MS = 50             # 曲线滚动刷新间隔（约 20 帧/秒）
WINDOWS = {"最近 1 分钟": 60, "最近 5 分钟": 300, "最近 30 分钟": 1800, "全部": None}

EXT_METHODS = {
    "status": "连接状态、每个通道的设定值/实测值/状态（最新一次读取）",
    "connect": "连接电源", "disconnect": "断开电源（输出保持不变）",
    "set": "设置 ch=1 [volt=伏] [curr=安]",
    "output": "开关输出 ch=1 on=true（界面勾选了“打开输出前确认”时，会在界面弹窗等人确认）",
    "all_on": "全部通道打开输出（同上需要确认）", "all_off": "全部通道关闭输出",
    "ovp": "过压保护 ch=1 volt=伏", "ocp": "过流保护开关 ch=1 on=true", "clear": "清除保护 ch=1",
    "log_save": "保存曲线记录 → csv/png 路径", "log_clear": "清空曲线记录",
    "scpi_query": "直接发 SCPI 查询 cmd=...", "scpi_write": "直接发 SCPI 命令 cmd=...",
}

_SI = {"m": 1e-3, "u": 1e-6, "μ": 1e-6, "k": 1e3}


def parse_val(text, unit):
    """“3.3”“3.3V”“100mA”“0.5 A” → 数值（伏或安）；不合法返回 None"""
    s = str(text).strip().replace(" ", "").replace("，", ".").replace("。", ".").replace("−", "-").replace("－", "-")
    m = re.fullmatch(r"([-+]?(?:\d+\.?\d*|\.\d+))([muμk]?)(" + unit + r")?", s, re.I)
    if not m:
        return None
    return float(m.group(1)) * _SI.get(m.group(2).lower() if m.group(2) not in ("μ",) else "μ", 1.0)


def fmt_v(v):
    return "—" if v is None else f"{v:.4f} V"


def fmt_i(i):
    if i is None:
        return "—"
    return f"{i * 1000:.2f} mA" if abs(i) < 1 else f"{i:.4f} A"


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


# =============================================================================
class Engine(threading.Thread):
    """唯一和电源通信的线程：轮询读数 + 执行界面/后台交来的任务"""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.jobs = queue.Queue()
        self.drv = None
        self.quit = False
        self.lock = threading.Lock()
        self.frame = None
        self.frame_id = 0
        self.fail = 0
        # 分段读取用（驱动 split_poll=True 时）。一轮 = 设定/状态 + 本轮要测的通道（电压一次、电流逐路）
        self.step = 0             # 0 = 下一段读设定/状态；1 = 正在测本轮的实测值（见 self.parts）
        self.parts = []           # 本轮还没测的段：("v", [通道...]) / ("i", 通道)
        self.seen_drv = None
        self.sets = self.mv = self.mi = self.prev = None
        self.prev_on = None       # 上一次读到的“正在输出”的通道，变了就全部重测
        self.last_full = 0.0      # 上次把所有通道都测一遍的时间
        self.meas_ok = False      # 实测值是在最近一次操作之后测的
        self.v_fresh = False      # 最近一次操作之后已经测过电压
        self.refreshed = False    # 本次操作已经顺带读过设定/状态
        self.need_i = set()       # 操作之后还没测电流的“正在输出”通道

    def job(self, fn, desc=""):
        self.jobs.put((fn, desc))

    def say(self, msg, tag=None):
        self.app.uiq.put(lambda: self.app.say(msg, tag))

    def _publish(self, st, meas_new):
        with self.lock:
            self.frame = {"st": st, "time": time.time(), "meas": meas_new}
            self.frame_id += 1

    def _publish_split(self, meas_new=False):
        st = P.compose_state(self.sets, self.mv, self.mi)
        if not self.meas_ok:
            # 设置刚变过、新的实测值还没测到：CV/CC 先沿用上一次的判断（不会因为旧读数误显示 CC），
            # 刚打开的通道显示“？”；OFF / OV / OC 来自设定值读取，是即时准确的
            for ch, c in st.items():
                if c["status"] not in ("OFF", "OV", "OC"):
                    p = (self.prev or {}).get(ch)
                    c["status"] = p["status"] if p and p["status"] in ("CV", "CC") else "?"
        self.prev = st
        self._publish(st, meas_new)

    def after_job(self):
        """在引擎线程里、一个操作完成后立即调用：马上读设定/状态并刷新显示（后台调用返回前就是新状态）"""
        d = self.drv
        if d is None or not d.split_poll:
            return
        try:
            self._reset_if_new(d)
            self.meas_ok = self.v_fresh = False
            self.last_full = 0.0                 # 操作之后把所有通道都测一遍
            self._read_settings_and_plan()
            self.refreshed = True
        except Exception:
            d.recover()

    def _reset_if_new(self, d):
        if d is not self.seen_drv:
            self.seen_drv = d
            self.sets = self.prev = self.prev_on = None
            self.mv, self.mi = [None] * d.n, [None] * d.n
            self.meas_ok = self.v_fresh = False
            self.last_full = 0.0
            self.parts = []
            self.step = 0

    def _read_settings_and_plan(self):
        """读设定/状态，并决定本轮测哪些通道：
        正在输出的通道每轮都测；关着的通道读数就是 0，每 FULL_MEAS_S 秒才测一次（测量很慢，E36313A 每路电流约 0.35 s）；
        刚做过操作、或有通道开关状态变了（包括在电源面板上按的），立即全部测一遍，不显示过时的读数"""
        d = self.drv
        self.sets = d.read_settings()
        on = [ch for ch, s in self.sets.items() if s["on"] and not (s["ov"] or s["oc"])]
        full = on != self.prev_on or time.time() - self.last_full >= FULL_MEAS_S
        self.prev_on = on
        # 正在输出的通道排在前面先测，它们的读数和 CV/CC 状态最先更新
        chs = (on + [ch for ch in range(1, d.n + 1) if ch not in on]) if full else on
        if full:
            self.last_full = time.time()
        if not self.meas_ok:
            self.need_i = set(on)               # 这些通道的电流测完（且电压已测），状态就可以判断了
        self.parts = ([("v", chs)] + [("i", ch) for ch in chs]) if chs else []
        self._publish_split()
        self.step = 1 if self.parts else 0

    def poll_step(self):
        """分段读取的一段：step 0 读设定/状态并安排本轮；之后每次测 self.parts 里的一段"""
        d = self.drv
        if not d.split_poll:
            self._publish(d.state(), True)
            return
        self._reset_if_new(d)
        if self.step == 0 or self.sets is None or not self.parts:
            self._read_settings_and_plan()
            return
        kind, arg = self.parts.pop(0)
        if kind == "v":
            for ch, v in zip(arg, d.read_meas_v(arg)):
                self.mv[ch - 1] = v
            self.v_fresh = True
        else:
            self.mi[arg - 1] = d.read_meas_i(arg)
            self.need_i.discard(arg)
        last = not self.parts
        was_ok = self.meas_ok
        if self.v_fresh and (last or not self.need_i):
            self.meas_ok = True
        # 正在输出的通道测完就先给曲线记一个点（不用等关着的通道也测完），曲线跟得更紧
        self._publish_split(meas_new=last or (self.meas_ok and not was_ok))
        if last:
            self.step = 0

    def run(self):
        last = 0.0
        while not self.quit:
            did = False
            while True:
                try:
                    fn, desc = self.jobs.get_nowait()
                except queue.Empty:
                    break
                did = True
                self.refreshed = False
                try:
                    fn()
                except (P.NotSupported, ValueError) as e:
                    self.say(f"{desc}：{e}", "err")
                except Exception as e:
                    self.say(f"{desc} 出错：{e}", "err")
                    if self.drv:
                        self.drv.recover()
                finally:
                    if self.drv:
                        self.drv.go_local()          # 每次操作完交还面板
            if did and not self.refreshed:
                self.meas_ok = self.v_fresh = False
                self.last_full = 0.0                 # 操作之后所有通道都测一遍
                self.step = 0                        # 操作之后先读设定/状态（快）
            # 一轮开始前按 POLL_S 节拍；一轮中间的各段、以及操作之后，立刻读
            if self.drv is not None and (did or self.step != 0 or time.time() - last >= POLL_S):
                if self.step == 0:
                    last = time.time()
                try:
                    self.poll_step()
                    self.drv.go_local()
                    self.fail = 0
                except Exception as e:
                    self.fail += 1
                    self.drv.recover()
                    self.step = 0
                    if self.fail == 3:
                        self.say(f"电源连续读取失败（{e}）。请检查 USB 线和电源开关，然后点“连接电源”。", "err")
                    time.sleep(0.5)
            else:
                time.sleep(0.05)


# =============================================================================
class App:
    def __init__(self, root):
        self.root = root
        root.title("直流电源电脑控制（通用版）")
        self.uiq = queue.Queue()
        self.eng = Engine(self)
        self.vars, self.entries, self.touched = {}, {}, {}
        self.drawn_id = -1
        self.ch_widgets = {}
        self.log_t, self.log_data = [], []          # 曲线记录：时间、{通道: (V, A)}
        self.t0 = None
        os.makedirs(SAVE_DIR, exist_ok=True)

        top = ttk.Frame(root)
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Button(top, text="连接电源", command=self.on_connect).pack(side="left")
        ttk.Button(top, text="断开", width=5, command=self.on_disconnect).pack(side="left", padx=(2, 0))
        self.dev_lbl = tk.Label(top, text="电源：未连接", fg="gray", font=("Microsoft YaHei", 10))
        self.dev_lbl.pack(side="left", padx=10)
        self.ext_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="允许后台控制", variable=self.ext_var, command=self._on_ext_toggle).pack(side="right")
        self.confirm_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="打开输出前确认", variable=self.confirm_var).pack(side="right", padx=12)
        self.ch_area = ttk.Frame(root)
        self.ch_area.pack(fill="x", padx=8)

        g = ttk.Frame(root)
        g.pack(fill="x", padx=8, pady=6)
        ttk.Button(g, text="全部打开", command=self.on_all_on).pack(side="left")
        tk.Button(g, text="■ 全部关闭", bg="#c62828", fg="white", font=("Microsoft YaHei", 10, "bold"),
                  command=self.on_all_off).pack(side="left", padx=8)
        ttk.Label(g, text="（设定电压、限流：在框里输入数字后按回车，或点“应用”；可写单位，如 3.3、100mA、0.5A）",
                  foreground="gray").pack(side="left", padx=8)

        # 日志先放到底部，再放（可伸缩的）曲线区，否则日志会被挤出窗口
        self.log = tk.Text(root, height=5, font=("Microsoft YaHei", 9))
        self.log.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        self.log.tag_configure("err", foreground="#b00020")
        self.log.tag_configure("ext", foreground="#7a3db8")
        self.log.tag_configure("ok", foreground="#1b7f3b")

        lf = ttk.LabelFrame(root, text="电压 / 电流曲线")
        lf.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        bar = ttk.Frame(lf)
        bar.pack(fill="x")
        ttk.Label(bar, text="显示").pack(side="left", padx=4)
        self.win_var = tk.StringVar(value="最近 5 分钟")
        cb = ttk.Combobox(bar, textvariable=self.win_var, values=list(WINDOWS), width=11, state="readonly")
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self.draw_plot())
        ttk.Button(bar, text="保存记录（CSV + 图片）", command=self.on_save).pack(side="left", padx=8)
        ttk.Button(bar, text="清空曲线", command=self.on_clear).pack(side="left")
        ttk.Button(bar, text="重设纵轴", command=self.on_reset_y).pack(side="left", padx=(8, 0))
        ttk.Label(bar, text="（只显示正在输出的通道；保存的记录包含全部通道）", foreground="gray").pack(side="left", padx=8)
        ttk.Button(bar, text="打开记录文件夹", command=lambda: os.startfile(SAVE_DIR)).pack(side="left", padx=8)
        self.rate_lbl = ttk.Label(bar, text="", foreground="gray")
        self.rate_lbl.pack(side="right", padx=8)
        self.fig = Figure(figsize=(8, 3.2), dpi=96)
        self.ax_v = self.fig.add_subplot(211)
        self.ax_i = self.fig.add_subplot(212, sharex=self.ax_v)
        self.canvas = FigureCanvasTkAgg(self.fig, master=lf)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        # 窗口大小变了才重新排版（排版比较慢，不在每次画曲线时做）；整张画完后缓存背景，供滚动时局部重画
        self.canvas.mpl_connect("resize_event", self._on_resize)
        self.canvas.mpl_connect("draw_event", self._on_draw)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.eng.start()
        if RM_ERR:
            self.say(f"VISA 库加载失败：{RM_ERR}", "err")
        self.ext = remote_api.ControlServer("直流电源控制", remote_api.PSU_PORT, self._ext_dispatch, EXT_METHODS)
        if self.ext.start():
            self.say("后台控制已开启（不想被后台控制时，取消勾选右上角“允许后台控制”）。")
        else:
            self.ext_var.set(False)
            self.say("后台控制没能开启（可能已经有另一份程序在运行）。", "err")
        self.say("提示：本程序连接电源时不会改动任何设置；打开输出前请确认电压和限流。")
        root.after(60, self.pump)
        root.after(300, self.on_connect)
        root.after(ANIM_MS, self._anim)

    # ------------------------------------------------------------ 通用
    def say(self, msg, tag=None):
        self.log.insert("end", time.strftime("%H:%M:%S  ") + msg + "\n", tag)
        self.log.see("end")

    def job(self, fn, desc):
        if self.eng.drv is None:
            self.say("电源还没连接。", "err")
            return
        self.eng.job(fn, desc)

    def pump(self):
        try:
            while True:
                self.uiq.get_nowait()()
        except queue.Empty:
            pass
        fid, fr = self.eng.frame_id, self.eng.frame
        if fr is not None and fid != self.drawn_id:
            self.drawn_id = fid
            self.update_view(fr)
        self.root.after(60, self.pump)

    # ------------------------------------------------------------ 连接
    def on_connect(self):
        self.eng.job(self._connect, "连接")

    def _connect(self):
        e = self.eng
        if e.drv is not None:
            e.drv.close()
        e.drv, e.frame = None, None
        if RM is None:
            return
        self.eng.say("正在扫描直流电源……")
        found = P.find_psus(RM)
        if not found:
            # 实测偶尔第一次扫描失败、稍后就好：把原因写进日志，等 2 秒自动再扫一次
            errs = {a: m for a, m in P.gen.LAST_ERRORS.items() if "0x5656::0x0832" not in a}
            for a, m in errs.items():
                self.eng.say(f"  {a}：{m}", "err")
            self.eng.say("没找到，2 秒后自动再扫描一次……")
            time.sleep(2)
            found = P.find_psus(RM)
        if found:
            e.drv = P.connect(RM, *found[0])
        d = e.drv
        self.uiq.put(lambda: self._after_connect(d))

    def _after_connect(self, d):
        if d is None:
            self.dev_lbl.config(text="电源：没找到（检查 USB 线插在电源背面、电源已开机）", fg="red")
            self.say("没找到直流电源。", "err")
            if gen.visa_stuck():
                self.say("原因：Keysight 仪器通信软件卡住了（VI_ERROR_ALLOC）。", "err")
                messagebox.showerror(gen.ALLOC_TITLE, gen.ALLOC_HELP)
        else:
            tested = d.is_tested()
            self.dev_lbl.config(text=f"电源：{d.describe()}" + ("" if tested else "  —— 此型号驱动未实测，若有功能不对请告诉我"),
                                fg="green" if tested else "#d07000")
            self.say(f"已连接：{d.idn}", "ok")
        self.build_channels(d)
        self.on_clear()

    def on_disconnect(self):
        self.eng.job(self._disconnect, "断开")

    def _disconnect(self):
        e = self.eng
        if e.drv is not None:
            e.drv.close()
            self.eng.say("已断开（输出保持不变，面板可以正常操作）。")
        e.drv, e.frame = None, None
        self.uiq.put(lambda: (self.dev_lbl.config(text="电源：未连接", fg="gray"), self.build_channels(None)))

    # ------------------------------------------------------------ 通道面板
    def build_channels(self, d):
        for w in self.ch_area.winfo_children():
            w.destroy()
        self.ch_widgets.clear()
        self.vars.clear()
        self.entries.clear()
        if d is None:
            ttk.Label(self.ch_area, text="未连接电源", foreground="gray").pack(pady=20)
            return
        for k, c in enumerate(d.channels, start=1):
            col, bg = CH_COLORS[(k - 1) % 4], CH_BG[(k - 1) % 4]
            f = tk.Frame(self.ch_area, bg=bg, highlightbackground=col, highlightthickness=2)
            f.pack(side="left", fill="both", expand=True, padx=4, pady=4)
            w = {}
            tk.Label(f, text=f" {c['name']} ", bg=col, fg="white", font=("Microsoft YaHei", 11, "bold")).grid(
                row=0, column=0, sticky="w", padx=6, pady=(6, 0))
            tk.Label(f, text=f"0 ~ {c['vmax']:g} V，0 ~ {c['imax']:g} A", bg=bg, fg="#555").grid(
                row=0, column=1, columnspan=3, sticky="w", pady=(6, 0))
            w["status"] = tk.Label(f, text="—", bg="#888", fg="white", font=("Microsoft YaHei", 10, "bold"), width=11)
            w["status"].grid(row=0, column=4, sticky="e", padx=6, pady=(6, 0))
            w["mv"] = tk.Label(f, text="—", bg=bg, fg="#222", font=("Consolas", 28, "bold"))
            w["mv"].grid(row=1, column=0, columnspan=5, sticky="w", padx=10)
            w["mi"] = tk.Label(f, text="—", bg=bg, fg="#222", font=("Consolas", 22, "bold"))
            w["mi"].grid(row=2, column=0, columnspan=5, sticky="w", padx=10)

            tk.Label(f, text="设定电压", bg=bg).grid(row=3, column=0, sticky="e", padx=(8, 2), pady=2)
            self.entry(f, (k, "v"), "V").grid(row=3, column=1, sticky="w")
            tk.Label(f, text="V", bg=bg).grid(row=3, column=2, sticky="w")
            tk.Label(f, text="限流", bg=bg).grid(row=4, column=0, sticky="e", padx=(8, 2), pady=2)
            self.entry(f, (k, "i"), "A").grid(row=4, column=1, sticky="w")
            tk.Label(f, text="A", bg=bg).grid(row=4, column=2, sticky="w")
            ttk.Button(f, text="应用", width=6, command=lambda ch=k: self.apply_both(ch)).grid(
                row=3, column=3, rowspan=2, padx=4)
            w["out"] = tk.Button(f, text="打开输出", width=10, font=("Microsoft YaHei", 10, "bold"),
                                 bg="#e0e0e0", command=lambda ch=k: self.on_output_toggle(ch))
            w["out"].grid(row=3, column=4, rowspan=2, padx=6)
            if not d.per_channel_output:
                tk.Label(f, text="（这台电源只能所有通道一起开关）", bg=bg, fg="#a15c00").grid(
                    row=5, column=0, columnspan=5, sticky="w", padx=8)

            p = tk.Frame(f, bg=bg)
            p.grid(row=6, column=0, columnspan=5, sticky="w", padx=6, pady=(4, 8))
            if d.has_ovp:
                tk.Label(p, text="过压保护 OVP", bg=bg).pack(side="left")
                self.entry(p, (k, "ovp"), "V", width=7).pack(side="left", padx=2)
                tk.Label(p, text="V", bg=bg).pack(side="left")
            if d.has_ocp:
                v = tk.BooleanVar(value=False)
                self.vars[(k, "ocp")] = v
                tk.Checkbutton(p, text="过流保护 OCP", variable=v, bg=bg, activebackground=bg,
                               command=lambda ch=k, var=v: self.on_ocp(ch, var.get())).pack(side="left", padx=8)
            if d.has_ovp or d.has_ocp:
                ttk.Button(p, text="清除保护", command=lambda ch=k: self.job(lambda: self.eng.drv.clear_prot(ch),
                                                                            f"CH{ch} 清除保护")).pack(side="left", padx=4)
            self.ch_widgets[k] = w

    def entry(self, parent, key, unit, width=9):
        var = tk.StringVar()
        self.vars[key] = var
        e = ttk.Entry(parent, textvariable=var, width=width, font=("Consolas", 11))
        e.bind("<Return>", lambda _: self.apply_field(key))
        e.bind("<KP_Enter>", lambda _: self.apply_field(key))
        e.bind("<Escape>", lambda _: (self.touched.pop(key, None), self.root.focus_set()))
        e.bind("<FocusIn>", lambda _: self.touched.__setitem__(key, time.time() + 3600))   # 正在输入：不被刷新覆盖
        e.bind("<FocusOut>", lambda _: self.touched.__setitem__(key, time.time()))
        self.entries[key] = e
        return e

    def _read_entry(self, key, unit):
        txt = self.vars[key].get()
        val = parse_val(txt, unit)
        if val is None:
            self.say(f"“{txt}”不是有效数值（例：3.3、3.3V、100mA、0.5A）。", "err")
            return None
        return val

    def apply_field(self, key):
        ch, kind = key
        d = self.eng.drv
        if d is None:
            return
        unit = "A" if kind == "i" else "V"
        val = self._read_entry(key, unit)
        if val is None:
            return
        try:
            if kind == "v":
                d.check_range(ch, volt=val)
                self.job(lambda: d.set_volt(ch, val), f"CH{ch} 设定电压")
                self.say(f"CH{ch} 设定电压 → {val:g} V")
            elif kind == "i":
                d.check_range(ch, curr=val)
                self.job(lambda: d.set_curr(ch, val), f"CH{ch} 限流")
                self.say(f"CH{ch} 限流 → {val:g} A")
            elif kind == "ovp":
                self.job(lambda: d.set_ovp(ch, val), f"CH{ch} 过压保护")
                self.say(f"CH{ch} 过压保护 → {val:g} V")
        except ValueError as e:
            self.say(str(e), "err")
            messagebox.showerror("超出范围", str(e))
            return
        self.touched[key] = time.time()
        self.root.focus_set()

    def apply_both(self, ch):
        self.apply_field((ch, "v"))
        self.apply_field((ch, "i"))

    def _cur(self, ch):
        fr = self.eng.frame
        return fr["st"].get(ch) if fr else None

    def on_output_toggle(self, ch):
        c = self._cur(ch)
        on = not (c and c["on"])
        if on and not self._confirm_on([ch]):
            return
        self.job(lambda: self.eng.drv.output(ch, on), f"CH{ch} 输出")
        self.say(f"CH{ch} 输出{'打开' if on else '关闭'}")

    def _confirm_on(self, chans):
        if not self.confirm_var.get():
            return True
        fr = self.eng.frame
        lines = []
        for ch in chans:
            c = fr["st"].get(ch) if fr else None
            name = self.eng.drv.channels[ch - 1]["name"]
            lines.append(f"{name}：{fmt_v(c and c['set_v'])}，限流 {fmt_i(c and c['set_i'])}")
        return messagebox.askyesno("确认打开输出", "将打开输出：\n\n" + "\n".join(lines) +
                                   "\n\n请确认电压、限流和接线都正确。")

    def on_all_on(self):
        d = self.eng.drv
        if d is None:
            return
        if self._confirm_on(list(range(1, d.n + 1))):
            self.job(lambda: d.output_all(True), "全部打开")
            self.say("全部通道打开输出")

    def on_all_off(self):
        if self.eng.drv is None:
            return
        self.job(lambda: self.eng.drv.output_all(False), "全部关闭")
        self.say("全部通道关闭输出")

    def on_ocp(self, ch, on):
        self.touched[(ch, "ocp")] = time.time()
        self.job(lambda: self.eng.drv.set_ocp(ch, on), f"CH{ch} 过流保护")
        self.say(f"CH{ch} 过流保护{'打开' if on else '关闭'}")

    # ------------------------------------------------------------ 刷新显示
    def setv(self, key, text):
        if time.time() - self.touched.get(key, 0) < 1.5:
            return
        v = self.vars.get(key)
        if v is not None and v.get() != text:
            v.set(text)

    def update_view(self, fr):
        st = fr["st"]
        for ch, w in self.ch_widgets.items():
            c = st.get(ch)
            if not c:
                continue
            w["mv"].config(text=fmt_v(c["meas_v"]))
            w["mi"].config(text=fmt_i(c["meas_i"]))
            txt, col = STATUS_STYLE.get(c["status"], (c["status"], "#888"))
            w["status"].config(text=txt, bg=col)
            w["out"].config(text="关闭输出" if c["on"] else "打开输出", bg="#ef9a9a" if c["on"] else "#c8e6c9")
            if c["set_v"] is not None:
                self.setv((ch, "v"), f"{c['set_v']:.4g}")
            if c["set_i"] is not None:
                self.setv((ch, "i"), f"{c['set_i']:.4g}")
            if (ch, "ovp") in self.vars and c.get("ovp") is not None:
                self.setv((ch, "ovp"), f"{c['ovp']:.4g}")
            if (ch, "ocp") in self.vars and time.time() - self.touched.get((ch, "ocp"), 0) > 1.5:
                self.vars[(ch, "ocp")].set(bool(c.get("ocp_on")))
        # 曲线
        if self.t0 is None:
            self.t0 = fr["time"]
        on_now = tuple(c["on"] and not (c["ov"] or c["oc"]) for _, c in sorted(st.items()))
        if self.log_t and on_now != getattr(self, "_log_on", on_now) and not fr.get("meas", True):
            # 输出刚打开/关闭：在这一刻补一个点（取变化前的读数），曲线从开关的那一刻才开始变化，
            # 不会把“上次测量”到“开关后第一次测量”之间连成一段假的缓坡
            self.log_t.append(fr["time"] - self.t0)
            self.log_data.append(dict(self.log_data[-1]))
        self._log_on = on_now
        if fr.get("meas", True):                # 曲线只记录新的实测值（分段读取时，设定/状态段不记点）
            self.log_t.append(fr["time"] - self.t0)
            self.log_data.append({ch: (c["meas_v"], c["meas_i"]) for ch, c in st.items()})
        if len(self.log_t) > 200000:
            del self.log_t[:20000], self.log_data[:20000]
        # 曲线由 _anim 定时滚动重画（约 20 帧/秒），这里只记录数据
        if fr.get("meas", True):                # 刷新率按“新的实测值”计算
            if hasattr(self, "_t_prev"):
                dt = fr["time"] - self._t_prev
                if dt > 0:
                    self.rate_lbl.config(text=f"读数刷新 {1 / dt:.1f} 次/秒")
            self._t_prev = fr["time"]

    # ------------------------------------------------------------ 曲线（滚动模式，像示波器的 Roll）
    # 横轴是“距现在多少秒”（右端 = 现在），刻度固定不动；曲线约 20 帧/秒匀速向左滚。
    # 坐标轴、刻度、网格只在需要时整张重画一次并缓存成背景，平时每帧只贴背景 + 画曲线（局部重画，很快）。
    # 只画正在输出的通道；纵轴只在数据超出范围时扩大，不会因为小波动来回缩放（可点“重设纵轴”重新适配）。
    def _plot_setup(self, d):
        self.ax_v.clear()
        self.ax_i.clear()
        self.plot_lines = {}
        if d is not None:
            for ch in range(1, d.n + 1):
                col, name = CH_COLORS[(ch - 1) % 4], d.channels[ch - 1]["name"]
                lv, = self.ax_v.plot([], [], color=col, lw=1.6, label=name, animated=True)
                li, = self.ax_i.plot([], [], color=col, lw=1.6, label=name, animated=True)
                self.plot_lines[ch] = (lv, li)
        self.ax_v.set_ylabel("电压 (V)")
        self.ax_i.set_ylabel("电流 (A)")
        for ax in (self.ax_v, self.ax_i):
            ax.grid(True, ls=":", alpha=0.6)
        self.empty_txt = self.ax_v.text(0.5, 0.5, "没有通道在输出", transform=self.ax_v.transAxes,
                                        ha="center", va="center", color="gray", fontsize=11)
        self.plot_drv = d
        self.plot_vis = None          # 正在显示的通道（元组）
        self.plot_xkey = None         # (横轴左端, 单位)
        self.ylim = {"v": None, "i": None}
        self.plot_cache = None        # (时间数组, {通道: (电压数组, 电流数组)})
        self.plot_bg = None
        self.plot_full = True         # 下一帧需要整张重画
        self.fig.tight_layout()

    def draw_plot(self):
        """数据或显示设置变了：下一帧重新取数据、整张重画"""
        self.plot_cache = None
        self.plot_full = True

    def on_reset_y(self):
        self.ylim = {"v": None, "i": None}
        self.draw_plot()

    def _on_resize(self, event):
        self.fig.tight_layout()
        self.plot_full = True
        self.plot_bg = None

    def _plot_visible(self):
        fr = self.eng.frame
        if not fr:
            return ()
        return tuple(ch for ch, c in sorted(fr["st"].items()) if c["on"] and not (c["ov"] or c["oc"]))

    def _x_layout(self, now):
        span = WINDOWS.get(self.win_var.get())
        if span is None:                  # “全部”：横轴长度按档位增长，不每帧变化
            total = now - self.log_t[0] if self.log_t else 0
            span = next((s for s in (60, 120, 300, 600, 1200, 1800, 3600, 7200, 14400, 28800, 86400) if s >= total),
                        math.ceil(total / 86400) * 86400)
        div, unit = (60.0, "分钟") if span > 600 else (1.0, "秒")
        return -span / div, div, unit

    def _rebuild_cache(self, now):
        span = WINDOWS.get(self.win_var.get())
        idx = 0 if span is None else bisect.bisect_left(self.log_t, now - span - 5)
        ts = self.log_t[idx:]
        step = max(1, len(ts) // 3000)
        rows = self.log_data[idx::step]
        t = np.array(ts[::step], dtype=float)
        nan = float("nan")
        data = {}
        for ch in self.plot_vis:
            v = np.array([nan if r.get(ch, (None, None))[0] is None else r[ch][0] for r in rows], dtype=float)
            i = np.array([nan if r.get(ch, (None, None))[1] is None else r[ch][1] for r in rows], dtype=float)
            data[ch] = (v, i)
        self.plot_cache = (t, data)
        self.cache_n = len(self.log_t)
        self.cache_win = self.win_var.get()
        # 纵轴：只扩不缩
        for key, k in (("v", 0), ("i", 1)):
            arrs = [a[k][np.isfinite(a[k])] for a in data.values()]
            vals = np.concatenate(arrs) if arrs else np.array([])
            if not vals.size:
                continue
            lo, hi = float(vals.min()), float(vals.max())
            pad = max(0.1 * (hi - lo), 0.02 * max(abs(lo), abs(hi)), 0.01 if key == "v" else 0.0005)
            cur = self.ylim[key]
            if cur is None:
                self.ylim[key] = (lo - pad, hi + pad)
            elif lo < cur[0] or hi > cur[1]:
                self.ylim[key] = (min(cur[0], lo - pad), max(cur[1], hi + pad))
            else:
                continue
            self.plot_full = True

    def _anim(self):
        self.root.after(ANIM_MS, self._anim)
        try:
            self._anim_frame()
        except Exception as e:                       # 画图出错不能让界面停掉
            if not getattr(self, "_anim_err", False):
                self._anim_err = True
                self.say(f"曲线绘制出错：{e}", "err")

    def _anim_frame(self):
        d = self.eng.drv
        if getattr(self, "plot_drv", "未建立") is not d:
            self._plot_setup(d)
        now = time.time() - self.t0 if self.t0 is not None else 0.0
        vis = self._plot_visible() if d is not None else ()
        if vis != self.plot_vis:
            self.plot_vis = vis
            self.ylim = {"v": None, "i": None}
            self.plot_cache = None
            leg = self.ax_v.get_legend()
            if leg:
                leg.remove()
            if vis:                              # 图例用独立的样例线（曲线本身可能正处于隐藏状态）
                handles = [Line2D([], [], color=CH_COLORS[(ch - 1) % 4], lw=1.6, label=d.channels[ch - 1]["name"])
                           for ch in vis]
                self.ax_v.legend(handles=handles, loc="upper left", fontsize=8, ncol=len(vis))
            self.empty_txt.set_visible(d is not None and not vis)
            self.plot_full = True
        xmin, div, unit = self._x_layout(now)
        if (xmin, unit) != self.plot_xkey:
            self.plot_xkey = (xmin, unit)
            self.ax_v.set_xlim(xmin, 0)
            self.ax_i.set_xlabel(f"距现在（{unit}）")
            self.plot_full = True
        if self.plot_cache is None or self.cache_n != len(self.log_t) or self.cache_win != self.win_var.get():
            self._rebuild_cache(now)
        t, data = self.plot_cache
        x = (t - now) / div
        for ch, (lv, li) in self.plot_lines.items():
            if ch in data:
                lv.set_data(x, data[ch][0])
                li.set_data(x, data[ch][1])
            lv.set_visible(ch in data)
            li.set_visible(ch in data)
        if self.plot_full or self.plot_bg is None:
            for key, ax in (("v", self.ax_v), ("i", self.ax_i)):
                ax.set_ylim(*(self.ylim[key] or ((0, 1) if key == "v" else (0, 0.01))))
            self.plot_full = False
            self.canvas.draw()                       # 触发 _on_draw：缓存背景并画上曲线
            return
        self.canvas.restore_region(self.plot_bg)
        self._draw_lines()
        self.canvas.blit(self.fig.bbox)

    def _draw_lines(self):
        for lv, li in getattr(self, "plot_lines", {}).values():
            if lv.get_visible():
                self.ax_v.draw_artist(lv)
                self.ax_i.draw_artist(li)

    def _on_draw(self, event):
        """整张图画完（包括窗口缩放引起的重画）后：把不含曲线的背景存起来，再把曲线画上去"""
        self.plot_bg = self.canvas.copy_from_bbox(self.fig.bbox)
        self._draw_lines()
        self.canvas.blit(self.fig.bbox)

    def _save_png(self, path, d):
        """保存图片：完整记录、所有通道、从记录开始计时（和界面上的滚动显示分开画）"""
        fig = Figure(figsize=(12, 6), dpi=120)
        ax_v = fig.add_subplot(211)
        ax_i = fig.add_subplot(212, sharex=ax_v)
        t = np.array(self.log_t, dtype=float)
        div, unit = (60.0, "分钟") if t[-1] - t[0] > 600 else (1.0, "秒")
        nan = float("nan")
        for ch in range(1, d.n + 1):
            col, name = CH_COLORS[(ch - 1) % 4], d.channels[ch - 1]["name"]
            ax_v.plot(t / div, [nan if r.get(ch, (None, None))[0] is None else r[ch][0] for r in self.log_data],
                      color=col, lw=1.4, label=name)
            ax_i.plot(t / div, [nan if r.get(ch, (None, None))[1] is None else r[ch][1] for r in self.log_data],
                      color=col, lw=1.4, label=name)
        ax_v.set_ylabel("电压 (V)")
        ax_i.set_ylabel("电流 (A)")
        ax_i.set_xlabel(f"时间（{unit}，从记录开始）")
        ax_v.legend(loc="upper left", fontsize=8, ncol=d.n)
        for ax in (ax_v, ax_i):
            ax.grid(True, ls=":", alpha=0.6)
        fig.tight_layout()
        fig.savefig(path)

    def on_clear(self):
        self.log_t, self.log_data, self.t0 = [], [], None
        self.draw_plot()

    def on_save(self):
        d = self.eng.drv
        if not self.log_t or d is None:
            messagebox.showwarning("提示", "还没有记录。")
            return None
        base = os.path.join(SAVE_DIR, "电源记录_" + stamp())
        with open(base + ".csv", "w", encoding="utf-8-sig") as f:
            names = [c["name"] for c in d.channels]
            f.write("时间(s)," + ",".join(f"{n} 电压(V),{n} 电流(A)" for n in names) + "\n")
            for t, r in zip(self.log_t, self.log_data):
                cells = [f"{t:.3f}"]
                for ch in range(1, d.n + 1):
                    v, i = r.get(ch, (None, None))
                    cells += ["" if v is None else f"{v:.5g}", "" if i is None else f"{i:.5g}"]
                f.write(",".join(cells) + "\n")
        self._save_png(base + ".png", d)
        self.say(f"已保存：{base}.csv 和 .png", "ok")
        return base

    # ------------------------------------------------------------ 后台控制
    def _on_ext_toggle(self):
        self.ext.enabled = self.ext_var.get()
        self.say("已允许后台控制。" if self.ext.enabled else "已关闭后台控制：后台发来的操作会被拒绝。", "ext")

    def _eng(self, fn, timeout=60):
        return remote_api.call_on(lambda w: self.eng.job(w, "后台控制"), fn, timeout)

    def _op(self, fn, timeout=60):
        """后台的设置类操作：执行后立刻读一次设定/状态，调用返回时 status 已是新状态"""
        def run():
            r = fn()
            self.eng.after_job()
            return r
        return self._eng(run, timeout)

    def _ui(self, fn, timeout=120):
        return remote_api.call_on(self.uiq.put, fn, timeout)

    def _need(self):
        if self.eng.drv is None:
            raise RuntimeError("电源还没连接（可先调用 connect）")
        return self.eng.drv

    def _ext_confirm_on(self, chans):
        """后台要打开输出：界面勾选了“打开输出前确认”时，弹窗等人点“是”"""
        ok = self._ui(lambda: self._confirm_on(chans), timeout=300)
        if not ok:
            raise RuntimeError("操作者在界面上拒绝了打开输出")

    def _ext_dispatch(self, method, a):
        if method != "status":
            self.uiq.put(lambda: self.say(f"【后台】{method} {a if a else ''}", "ext"))
        if method == "status":
            d, fr = self.eng.drv, self.eng.frame
            return {"connected": d is not None, "describe": d.describe() if d else None,
                    "tested": d.is_tested() if d else None,
                    "channels": d.channels if d else None,
                    "state": fr["st"] if fr else None,
                    "age_s": round(time.time() - fr["time"], 2) if fr else None}
        if method == "connect":
            self._eng(self._connect)
            return {"connected": self.eng.drv is not None, "describe": self.eng.drv.describe() if self.eng.drv else None}
        if method == "disconnect":
            self._eng(self._disconnect)
            return {"connected": False}
        if method == "log_save":
            base = self._ui(self.on_save)
            return {"csv": base + ".csv", "png": base + ".png"} if base else None
        if method == "log_clear":
            self._ui(self.on_clear)
            return "ok"
        d = self._need()
        ch = int(a.get("ch", 1))
        if method == "set":
            volt, curr = a.get("volt"), a.get("curr")
            d.check_range(ch, None if volt is None else float(volt), None if curr is None else float(curr))
            self._op(lambda: (volt is None or d.set_volt(ch, float(volt)), curr is None or d.set_curr(ch, float(curr))))
            return {"errors": self._eng(d.errors)}
        if method == "output":
            on = bool(a.get("on", True))
            if on:
                self._ext_confirm_on([ch])
            self._op(lambda: d.output(ch, on))
            return "ok"
        if method == "all_on":
            self._ext_confirm_on(list(range(1, d.n + 1)))
            self._op(lambda: d.output_all(True))
            return "ok"
        if method == "all_off":
            self._op(lambda: d.output_all(False))
            return "ok"
        if method == "ovp":
            self._op(lambda: d.set_ovp(ch, float(a["volt"])))
            return {"errors": self._eng(d.errors)}
        if method == "ocp":
            self._op(lambda: d.set_ocp(ch, bool(a.get("on", True))))
            return {"errors": self._eng(d.errors)}
        if method == "clear":
            self._op(lambda: d.clear_prot(ch))
            return "ok"
        if method == "scpi_query":
            return self._eng(lambda: d.query(str(a["cmd"])))
        if method == "scpi_write":
            self._op(lambda: d.write(str(a["cmd"])))
            return {"errors": self._eng(d.errors)}
        raise RuntimeError(f"未实现的方法：{method}")

    def on_close(self):
        try:
            self.ext.stop()
        except Exception:
            pass
        self.eng.quit = True
        time.sleep(0.2)
        if self.eng.drv is not None:
            self.eng.drv.close()
        self.root.destroy()


if __name__ == "__main__":
    if not gen.single_instance("psu_gui"):
        _r = tk.Tk()
        _r.withdraw()
        messagebox.showinfo("已经打开了", "直流电源控制程序已经在运行（可能被其他窗口挡住了，或最小化在任务栏）。")
        _r.destroy()
        sys.exit(0)
    gen.set_app_id("psu")
    root = tk.Tk()
    gen.set_icon(root, "psu.ico", os.path.dirname(os.path.abspath(__file__)))
    App(root)
    root.geometry("1500x920+20+10")
    root.mainloop()
