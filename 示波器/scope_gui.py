"""
示波器电脑控制（通用版）—— 中文界面
  · 所有打开的通道实时显示在同一个屏幕上（和真示波器一样），可单独开关任一通道
  · 示波器面板上的常用功能都能在这里操作：运行/停止/单次/自动设置、V/格、位置、耦合、探头、带宽限制、反相、
    时基、水平位置、触发（信源/斜率/方式/耦合/电平/50%）、采集方式
  · 电脑端功能：自动测量表、光标（时间/电压）、数学运算（加减乘、FFT 频谱）、XY 显示、保存截图和数据、自动扫频
支持的示波器品牌见 scope.py 开头；连接时自动识别。
"""
import os
import sys
import time
import queue
import threading
import datetime
import traceback

try:
    from ctypes import windll
    windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

import tkinter as tk
from tkinter import ttk, messagebox

# 注意：VISA 库必须在 matplotlib 之前加载，否则 ktvisa32.dll 初始化失败（WinError 1114）
import scope as S          # noqa: E402
import remote_api          # noqa: E402  本机控制接口（在信号发生器文件夹里，两个程序共用）
gen = S.gen

# 本机控制接口（给脚本等外部程序用）：方法名 -> 说明
EXT_METHODS = {
    "status": "连接状态 + 最新一帧的全部设置和测量值（不额外读仪器）",
    "connect": "连接示波器", "disconnect": "断开示波器",
    "gen_connect": "连接信号发生器（扫频用）", "gen_disconnect": "断开信号发生器",
    "state": "立即从示波器读取全部设置",
    "measure": "测量 ch=1 [items=[\"FREQ\",\"VPP\",...]]，不给 items 测全部 16 项",
    "pair": "CH1→CH2 相位差(°)和延迟(s)",
    "waveform": "读取波形 ch=1 → {t:[秒], v:[伏]}",
    "screenshot": "把软件屏幕保存成 PNG [path=...] → 文件路径",
    "set": "设置 what=ch_on|ch_scale|ch_pos|ch_coupling|ch_probe|ch_bw|ch_invert|timebase|tb_offset|"
           "trig_source|trig_level|trig_slope|trig_sweep|trig_coupling|acq_type|avg_count [ch=] value=",
    "step": "按档位调 what=ch_scale|timebase up=true [ch=]",
    "run": "运行", "stop": "停止", "single": "单次", "auto": "自动设置", "trig_50": "触发电平设到 50%",
    "autoscale_ch": "自动调整该通道 V/格 ch=1 → 峰峰值",
    "display": "电脑端显示 mode=YT|XY math=关|CH1+CH2|CH1−CH2|CH1×CH2|FFT CH1 cursor=关|时间|电压 csrc=1 A=格 B=格",
    "sweep": "扫频 f1 f2 n amp gch=1 cin=1 cout=2(或省略) → rows/png/csv",
    "scpi_query": "直接发 SCPI 查询 cmd=...", "scpi_write": "直接发 SCPI 命令 cmd=...",
}
SET_METHODS = {   # what -> (驱动方法名, 是否带通道, 选项类别)
    "ch_on": ("set_ch_on", True, None), "ch_scale": ("set_ch_scale", True, None),
    "ch_pos": ("set_ch_pos", True, None), "ch_coupling": ("set_ch_coupling", True, "coup"),
    "ch_probe": ("set_ch_probe", True, None), "ch_bw": ("set_ch_bw", True, None),
    "ch_invert": ("set_ch_invert", True, None), "timebase": ("set_timebase", False, None),
    "tb_offset": ("set_tb_offset", False, None), "trig_source": ("set_trig_source", False, "src"),
    "trig_level": ("set_trig_level", False, None), "trig_slope": ("set_trig_slope", False, "slope"),
    "trig_sweep": ("set_trig_sweep", False, "sweep"), "trig_coupling": ("set_trig_coupling", False, "tcoup"),
    "acq_type": ("set_acq_type", False, "acq"), "avg_count": ("set_avg_count", False, None),
}
# UNI-T 示波器上会导致 USB 卡死的命令，外部直接发 SCPI 时拦截
SCPI_BLOCK = ("DISP:DATA", "DISPLAY:DATA", "WAV:FORM", "WAVEFORM:FORM")
try:
    RM, RM_ERR = gen.open_rm(), None
except Exception as _e:
    RM, RM_ERR = None, _e

import numpy as np                                                    # noqa: E402
import matplotlib                                                     # noqa: E402
matplotlib.use("TkAgg")
matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "Segoe UI Symbol", "SimHei", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False
from matplotlib.figure import Figure                                  # noqa: E402
from matplotlib.transforms import blended_transform_factory           # noqa: E402
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg       # noqa: E402

SAVE_DIR = os.path.join(S.HERE, "记录")
MATH_COLOR = "#ff5050"
SCREEN_BG = "#05080c"
GRID_COLOR = "#3a4450"

NAMES = {
    "coup": {"DC": "直流 DC", "AC": "交流 AC", "GND": "接地 GND"},
    "src": {"CH1": "CH1", "CH2": "CH2", "CH3": "CH3", "CH4": "CH4", "EXT": "外部 EXT", "EXT5": "外部/5", "AC": "市电"},
    "slope": {"POS": "上升沿", "NEG": "下降沿", "ALT": "双沿"},
    "sweep": {"AUTO": "自动", "NORM": "正常", "SING": "单次"},
    "tcoup": {"DC": "直流", "AC": "交流", "LF": "低频抑制", "HF": "高频抑制", "NOISE": "噪声抑制"},
    "acq": {"NORM": "普通", "AVER": "平均", "PEAK": "峰值检测", "HRES": "高分辨率"},
}
STATUS_TXT = {"TRIGED": ("已触发", "#38d838"), "AUTO": ("自动", "#38d838"), "RUN": ("运行中", "#38d838"),
              "READY": ("等待触发", "#ffaa00"), "WAIT": ("等待触发", "#ffaa00"), "STOP": ("已停止", "#ff4040")}
MEAS_NAME = {c: n for c, n, _ in S.MEAS}
MEAS_UNIT = {c: u for c, _, u in S.MEAS}


def fmt_si(v, unit="", digits=4):
    if v is None:
        return "—"
    if unit == "%":
        return f"{v:.{max(digits - 2, 1)}f} %"
    a = abs(v)
    if a == 0:
        return f"0 {unit}"
    for k, p in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "μ"), (1e-9, "n"), (1e-12, "p")):
        if a >= k * 0.99995:
            return f"{v / k:.{digits}g} {p}{unit}"
    return f"{v:.3g} {unit}"


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "μ": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}


def parse_si(text):
    """把手动输入的“1.5”“200mV”“-0.3 V”“2ms”“500us”“1.2k”解析成数值；解析不了返回 None"""
    import re
    s = str(text).strip().replace(" ", "").replace("，", ".").replace("。", ".")
    s = s.replace("−", "-").replace("－", "-").replace("＋", "+")
    m = re.fullmatch(r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)([pnuμµmkKMG]?)(?:[Vv]|[Ss]|Hz|格)?", s)
    if not m:
        return None
    return float(m.group(1)) * _SI.get(m.group(2), 1.0)


class Marker:
    """屏幕边缘的小三角 + 文字（通道零点、触发电平）"""

    def __init__(self, ax, transform, x, shape, label, color, side):
        self.tri = ax.plot([x], [0], marker=shape, ms=9, color=color, ls="", transform=transform, clip_on=False)[0]
        dx = -0.009 if side == "right" else 0.009
        self.txt = ax.text(x + dx, 0, label, transform=transform, ha=side, va="center", color=color,
                           fontsize=10, fontweight="bold", clip_on=False)

    def set_y(self, y):
        self.tri.set_ydata([y])
        self.txt.set_y(y)

    def set_visible(self, on):
        self.tri.set_visible(on)
        self.txt.set_visible(on)


# =============================================================================
# 后台引擎：唯一和仪器打交道的线程（界面通过“任务”让它做事，避免两边同时发命令）
# =============================================================================
class Engine(threading.Thread):
    def __init__(self, ui_put):
        super().__init__(daemon=True)
        self.ui_put = ui_put          # 把一个函数交给界面线程执行
        self.jobs = queue.Queue()
        self.drv = None
        self.gen = None
        self.live = True
        self.quit = False
        self.frame = None
        self.frame_id = 0
        self.lock = threading.Lock()
        self.last_meas = 0.0
        self.meas = {}
        self.fail = 0
        self.stop_flag = threading.Event()

    def job(self, fn, desc=""):
        self.jobs.put((fn, desc))

    def say(self, msg):
        self.ui_put(lambda: self.app.say(msg))

    def run(self):
        while not self.quit:
            did = False
            while True:
                try:
                    fn, desc = self.jobs.get_nowait()
                except queue.Empty:
                    break
                did = True
                try:
                    fn()
                except S.NotSupported as e:
                    self.say(f"{desc}：{e}")
                except Exception as e:
                    self.say(f"{desc} 出错：{e}")
                    if self.drv:
                        self.drv.recover()
            if self.drv is not None and self.live:
                try:
                    self.acquire()
                    self.fail = 0
                except Exception as e:
                    self.fail += 1
                    self.drv.recover()
                    if self.fail == 3:
                        self.say(f"示波器连续读取失败（{e}）。如果一直这样，请重启示波器后点「重新连接」。")
                    time.sleep(0.5)
            elif not did:
                time.sleep(0.08)

    def acquire(self):
        d = self.drv
        st = d.state()
        waves = {}
        for ch in range(1, d.channels + 1):
            if st[ch]["on"]:
                t, v = d.waveform(ch)
                if len(t) > 1:              # 刚开关通道 / 切换设置的瞬间可能读到空数据，跳过这一次
                    waves[ch] = (t, v)
                    cache = getattr(d, "_scale_cache", None)      # UNI-T：用波形数据里的 V/格（见 scope.py 怪癖 5）
                    if cache and ch in cache:
                        st[ch]["scale"] = cache[ch]
        if time.time() - self.last_meas > 1.0:
            self.meas = {ch: d.measure_all(ch) for ch in waves}
            if d.has_pair and 1 in waves and 2 in waves:
                self.meas["pair"] = d.measure_pair()
            self.last_meas = time.time()
        with self.lock:
            self.frame = {"st": st, "waves": waves, "meas": dict(self.meas), "time": time.time()}
            self.frame_id += 1

    def get_frame(self):
        with self.lock:
            return self.frame_id, self.frame


# =============================================================================
# 界面
# =============================================================================
class App:
    def __init__(self, root):
        self.root = root
        root.title("示波器电脑控制（通用版）")
        self.uiq = queue.Queue()
        self.eng = Engine(self.uiq.put)
        self.eng.app = self
        self.vars = {}
        self.entries = {}
        self.touched = {}
        self.override = {}          # 拖动标记时的“预览值”：{("pos", ch) 或 "lev": (数值, 失效时间)}
        self.marker_drag = None     # 正在拖动的标记：("pos", ch) 或 ("lev", None)
        self._last_send = 0.0
        self.maps = {}
        self.drawn_id = -1
        self.mode = "YT"
        self.cursor = {"type": "关", "src": 1, "A": None, "B": None}
        self.math = "关"
        self.last_frame = None
        self.hd = None
        os.makedirs(SAVE_DIR, exist_ok=True)

        # ---- 顶部：连接 ----
        top = ttk.Frame(root)
        top.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(top, text="连接示波器", command=self.on_connect).pack(side="left")
        ttk.Button(top, text="断开", width=5, command=self.on_disconnect).pack(side="left", padx=(2, 0))
        self.dev_lbl = tk.Label(top, text="示波器：未连接", fg="gray", font=("Microsoft YaHei", 10))
        self.dev_lbl.pack(side="left", padx=(8, 20))
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(top, text="连接信号发生器", command=self.on_gen_connect).pack(side="left")
        ttk.Button(top, text="断开", width=5, command=self.on_gen_disconnect).pack(side="left", padx=(2, 0))
        self.gen_lbl = tk.Label(top, text="信号发生器：未连接（只有扫频需要它）", fg="gray", font=("Microsoft YaHei", 9))
        self.gen_lbl.pack(side="left", padx=8)
        self.ext_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="允许后台控制", variable=self.ext_var,
                        command=self._on_ext_toggle).pack(side="right", padx=(0, 12))
        self.fps_lbl = ttk.Label(top, text="", foreground="gray")
        self.fps_lbl.pack(side="right")

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        # 先放右侧控制面板和底部测量表，再放（可伸缩的）波形屏幕，否则屏幕会把它们挤出窗口
        self.ctrl = ttk.Frame(body)
        self.ctrl.pack(side="right", fill="y", padx=(3, 6))
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True, padx=(6, 3))

        bottom = ttk.Frame(left)
        bottom.pack(side="bottom", fill="x", pady=(4, 4))
        self.log = tk.Text(self.ctrl, height=7, width=40, font=("Microsoft YaHei", 9), wrap="char")
        self.log.pack(side="bottom", fill="x", pady=(4, 4))
        self.meas_tree = ttk.Treeview(bottom, height=3, show="headings")
        self.meas_tree.pack(side="top", fill="x")
        self.pair_lbl = ttk.Label(bottom, text="", foreground="#404040")
        self.pair_lbl.pack(side="top", anchor="w", pady=(2, 0))

        self.fig = Figure(figsize=(8, 4.5), dpi=96, facecolor="#101418")
        self.canvas = FigureCanvasTkAgg(self.fig, master=left)
        self.canvas.get_tk_widget().pack(side="top", fill="both", expand=True)
        self.canvas.mpl_connect("button_press_event", self.on_click)
        self.canvas.mpl_connect("motion_notify_event", self.on_motion)
        self.canvas.mpl_connect("button_release_event", self.on_release)
        self.dragging = None

        self.build_axes()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.eng.start()
        if RM_ERR:
            self.say(f"VISA 库加载失败：{RM_ERR}")
        self.ext = remote_api.ControlServer("示波器控制", remote_api.SCOPE_PORT, self._ext_dispatch, EXT_METHODS)
        if self.ext.start():
            self.say("后台控制已开启（不想被后台控制时，取消勾选右上角“允许后台控制”）。")
        else:
            self.ext_var.set(False)
            self.say("后台控制没能开启（可能已经有另一份程序在运行）。")
        root.after(50, self.pump)
        root.after(200, self.on_connect)

    # ------------------------------------------------------------ 通用
    def say(self, msg):
        self.log.insert("end", time.strftime("%H:%M:%S  ") + msg + "\n")
        self.log.see("end")

    def pump(self):
        try:
            while True:
                self.uiq.get_nowait()()
        except queue.Empty:
            pass
        fid, frame = self.eng.get_frame()
        if frame is not None and fid != self.drawn_id:
            self.drawn_id = fid
            try:
                self.draw(frame)
                self.sync(frame["st"])
            except Exception:
                self.say("显示出错：" + traceback.format_exc().splitlines()[-1])
        self.root.after(40, self.pump)

    def job(self, fn, desc, key=None):
        if key:
            self.touched[key] = time.time()
        if self.eng.drv is None:
            self.say("示波器还没连接。")
            return
        self.eng.job(fn, desc)

    # ------------------------------------------------------------ 连接
    def on_connect(self):
        self.eng.job(self._connect, "连接")

    def _connect(self):
        """只连接示波器（信号发生器单独连接，见 _gen_connect）"""
        e = self.eng
        if e.drv is not None:
            e.drv.close()
        e.drv = None
        e.frame = None
        if RM is None:
            return
        self.eng.say("正在扫描示波器……")
        scopes = S.find_scopes(RM)
        if scopes:
            e.drv = S.connect(RM, *scopes[0])
        drv = e.drv
        self.uiq.put(lambda: self._after_connect(drv))

    def _after_connect(self, drv):
        if drv is None:
            self.dev_lbl.config(text="示波器：没找到（检查 USB 线和电源；若刚出错请重启示波器）", fg="red")
            if gen.visa_stuck():
                self.say("原因：Keysight 仪器通信软件卡住了（VI_ERROR_ALLOC）。")
                messagebox.showerror(gen.ALLOC_TITLE, gen.ALLOC_HELP)
        else:
            tested = drv.is_tested()
            self.dev_lbl.config(text=f"示波器：{drv.describe()}" + ("" if tested else "  —— 此型号驱动未实测，若有功能不对请告诉我"),
                                fg="green" if tested else "#d07000")
            self.say(f"已连接示波器：{drv.idn}")
        self.build_controls(drv)
        self.build_axes()

    def on_disconnect(self):
        self.eng.job(self._disconnect, "断开示波器")

    def _disconnect(self):
        e = self.eng
        if e.drv is not None:
            e.drv.close()
            self.eng.say("示波器已断开（面板可以正常手动操作）。")
        e.drv = None
        e.frame = None
        self.uiq.put(lambda: (self.dev_lbl.config(text="示波器：未连接", fg="gray"),
                              self.build_controls(None), self.build_axes()))

    # ---- 信号发生器：单独连接 / 断开 ----
    def on_gen_connect(self):
        self.eng.job(self._gen_connect, "连接信号发生器")

    def _gen_connect(self):
        e = self.eng
        if e.gen is not None:
            e.gen.close()
        e.gen = None
        if RM is None:
            return
        self.eng.say("正在扫描信号发生器……")
        gens = gen.scan(RM)
        if gens:
            e.gen = gen.connect(RM, *gens[0])
            e.gen.go_local()             # 连接时发过初始化命令，马上交还面板
            self.eng.say(f"已连接信号发生器：{e.gen.idn}")
        else:
            self.eng.say("没找到信号发生器（检查 USB 线和电源）。")
        g = e.gen
        self.uiq.put(lambda: self.gen_lbl.config(
            text=f"信号发生器：{g.describe()}" if g else "信号发生器：没找到", fg="green" if g else "red"))

    def on_gen_disconnect(self):
        self.eng.job(self._gen_disconnect, "断开信号发生器")

    def _gen_disconnect(self):
        e = self.eng
        if e.gen is not None:
            e.gen.close()           # 会把信号发生器切回本地（面板可以操作），输出状态不变
            self.eng.say("信号发生器已断开（输出保持不变，面板可以正常操作）。")
        e.gen = None
        self.uiq.put(lambda: self.gen_lbl.config(text="信号发生器：未连接（只有扫频需要它）", fg="gray"))

    # ------------------------------------------------------------ 右侧控制面板
    def var(self, key, kind=tk.StringVar, value=""):
        v = kind(value=value)
        self.vars[key] = v
        return v

    def combo(self, parent, key, labels, width, cb):
        cb_ = ttk.Combobox(parent, textvariable=self.var(key), values=labels, width=width, state="readonly")
        cb_.bind("<<ComboboxSelected>>", lambda e: cb(self.vars[key].get()))
        return cb_

    def entry(self, parent, key, width, apply):
        """可手动输入数值的框：输入后按回车生效（支持 200mV、1.5、2ms、500us 这类写法）"""
        e = ttk.Entry(parent, textvariable=self.var(key), width=width)

        def on_enter(_=None):
            v = parse_si(self.vars[key].get())
            if v is None:
                self.say(f"“{self.vars[key].get()}”不是有效数值。例：1.5、-0.3V、200mV、2ms、500us")
                return
            self.touched[key] = time.time()
            apply(v)
            self.root.focus_set()                 # 输完离开输入框，界面恢复跟随示波器
        e.bind("<Return>", on_enter)
        e.bind("<KP_Enter>", on_enter)
        e.bind("<Escape>", lambda _: (self.touched.pop(key, None), self.root.focus_set()))
        self.entries[key] = e
        return e

    def btn(self, parent, text, cb, width=3):
        return ttk.Button(parent, text=text, width=width, command=cb)

    def build_controls(self, drv):
        for w in self.ctrl.winfo_children():
            if w is not self.log:
                w.destroy()
        self.vars.clear()
        self.maps.clear()
        self.widgets = {}
        self.entries = {}
        if drv is None:
            ttk.Label(self.ctrl, text="未连接示波器").pack(pady=20)
            return
        pad = dict(padx=3, pady=2)

        # ---- 运行控制 ----
        f = ttk.LabelFrame(self.ctrl, text="运行控制")
        f.pack(fill="x", pady=2)
        self.run_btn = tk.Button(f, text="运行/停止", width=11, bg="#38c038", fg="white",
                                 font=("Microsoft YaHei", 10, "bold"), command=self.on_runstop)
        self.run_btn.grid(row=0, column=0, **pad)
        ttk.Button(f, text="单次", width=6, command=lambda: self.job(drv.single, "单次", "sweep")).grid(row=0, column=1, **pad)
        ttk.Button(f, text="自动设置", width=8, command=self.on_auto).grid(row=0, column=2, **pad)
        ttk.Button(f, text="保存截图", width=8, command=self.on_save).grid(row=0, column=3, **pad)

        # ---- 各通道 ----
        for ch in range(1, drv.channels + 1):
            self.build_channel(drv, ch, pad)

        # ---- 水平 ----
        f = ttk.LabelFrame(self.ctrl, text="水平")
        f.pack(fill="x", pady=2)
        tb_vals = S.series_125(*drv.tdiv_range)
        self.maps["tb"] = {fmt_si(v, "s", 3) + "/格": v for v in tb_vals}
        ttk.Label(f, text="时基").grid(row=0, column=0, **pad)
        self.combo(f, "tb", list(self.maps["tb"]), 10,
                   lambda lab: self.job(lambda: drv.set_timebase(self.maps["tb"][lab]), "时基", "tb")).grid(row=0, column=1, **pad)
        self.btn(f, "▲", lambda: self.job(lambda: drv.step_timebase(True), "时基", "tb")).grid(row=0, column=2)
        self.btn(f, "▼", lambda: self.job(lambda: drv.step_timebase(False), "时基", "tb")).grid(row=0, column=3)
        ttk.Label(f, text="位置").grid(row=1, column=0, **pad)
        self.entry(f, "tbofs", 12, lambda v: self.job(lambda: drv.set_tb_offset(v), "水平位置")).grid(row=1, column=1, **pad)
        self.btn(f, "◀", lambda: self.job(lambda: self._step_tbofs(-1), "水平位置")).grid(row=1, column=2)
        self.btn(f, "▶", lambda: self.job(lambda: self._step_tbofs(1), "水平位置")).grid(row=1, column=3)
        self.btn(f, "归零", lambda: self.job(lambda: drv.set_tb_offset(0), "水平位置"), 5).grid(row=1, column=4, **pad)

        # ---- 触发 ----
        f = ttk.LabelFrame(self.ctrl, text="触发（边沿）")
        f.pack(fill="x", pady=2)
        for key, title, codes, setter, r, c in (
                ("src", "信源", drv.trig_sources(), drv.set_trig_source, 0, 0),
                ("slope", "斜率", drv.slopes, drv.set_trig_slope, 0, 2),
                ("sweep", "方式", drv.sweeps, drv.set_trig_sweep, 1, 0),
                ("tcoup", "耦合", drv.trig_coups, drv.set_trig_coupling, 1, 2)):
            self.maps[key] = {NAMES[key if key != "tcoup" else "tcoup"][cd]: cd for cd in codes}
            ttk.Label(f, text=title).grid(row=r, column=c, **pad)
            self.combo(f, key, list(self.maps[key]), 8,
                       lambda lab, k=key, s=setter, t=title: self.job(lambda: s(self.maps[k][lab]), "触发" + t, k)
                       ).grid(row=r, column=c + 1, **pad)
        ttk.Label(f, text="电平").grid(row=2, column=0, **pad)
        self.entry(f, "lev", 10, lambda v: self.job(lambda: drv.set_trig_level(v), "触发电平")).grid(row=2, column=1, **pad)
        g = ttk.Frame(f)
        g.grid(row=2, column=2, columnspan=2, sticky="w")
        self.btn(g, "▲", lambda: self.job(lambda: self._step_lev(1), "触发电平")).pack(side="left")
        self.btn(g, "▼", lambda: self.job(lambda: self._step_lev(-1), "触发电平")).pack(side="left")
        self.btn(g, "50%", lambda: self.job(drv.trig_50, "触发电平 50%"), 5).pack(side="left", padx=2)

        # ---- 采集 ----
        f = ttk.LabelFrame(self.ctrl, text="采集")
        f.pack(fill="x", pady=2)
        self.maps["acq"] = {NAMES["acq"][cd]: cd for cd in drv.acq_types}
        ttk.Label(f, text="方式").grid(row=0, column=0, **pad)
        self.combo(f, "acq", list(self.maps["acq"]), 9,
                   lambda lab: self.job(lambda: drv.set_acq_type(self.maps["acq"][lab]), "采集方式", "acq")).grid(row=0, column=1, **pad)
        ttk.Label(f, text="平均次数").grid(row=0, column=2, **pad)
        self.combo(f, "avg", [str(n) for n in drv.avg_counts], 5,
                   lambda lab: self.job(lambda: drv.set_avg_count(int(lab)), "平均次数", "avg")).grid(row=0, column=3, **pad)

        # ---- 电脑端显示 / 分析 ----
        f = ttk.LabelFrame(self.ctrl, text="显示与分析（电脑端计算）")
        f.pack(fill="x", pady=2)
        ttk.Label(f, text="显示").grid(row=0, column=0, **pad)
        self.combo(f, "mode", ["YT（时间）", "XY（CH1-CH2）"], 11, self.on_mode).grid(row=0, column=1, **pad)
        self.vars["mode"].set("YT（时间）" if self.mode == "YT" else "XY（CH1-CH2）")
        ttk.Label(f, text="数学").grid(row=0, column=2, **pad)
        math_opts = ["关"]
        if drv.channels >= 2:
            math_opts += ["CH1+CH2", "CH1−CH2", "CH1×CH2"]
        math_opts += [f"FFT CH{c}" for c in range(1, drv.channels + 1)]
        self.combo(f, "math", math_opts, 9, self.on_math).grid(row=0, column=3, **pad)
        self.vars["math"].set(self.math)
        ttk.Label(f, text="光标").grid(row=1, column=0, **pad)
        self.combo(f, "ctype", ["关", "时间", "电压"], 11, self.on_cursor).grid(row=1, column=1, **pad)
        self.vars["ctype"].set(self.cursor["type"])
        ttk.Label(f, text="光标信源").grid(row=1, column=2, **pad)
        self.combo(f, "csrc", [f"CH{c}" for c in range(1, drv.channels + 1)], 9, self.on_cursor).grid(row=1, column=3, **pad)
        self.vars["csrc"].set(f"CH{self.cursor['src']}")
        ttk.Label(f, text="光标：左键点放 A、右键点放 B；按住光标线可拖动", foreground="gray").grid(
            row=2, column=0, columnspan=4, sticky="w", **pad)

        # ---- 工具 ----
        f = ttk.LabelFrame(self.ctrl, text="工具")
        f.pack(fill="x", pady=2)
        ttk.Button(f, text="自动扫频（频率响应）…", command=self.open_sweep).pack(side="left", **pad)
        ttk.Button(f, text="打开记录文件夹", command=lambda: os.startfile(SAVE_DIR)).pack(side="left", **pad)

        # 测量表：每个通道一行，每个测量项一列
        cols = ["通道"] + [name for _, name, _ in S.MEAS]
        self.meas_tree.config(columns=cols, height=drv.channels)
        for c in cols:
            self.meas_tree.heading(c, text=c)
            self.meas_tree.column(c, width=50 if c == "通道" else 82, minwidth=40, anchor="center", stretch=True)
        self.meas_tree.delete(*self.meas_tree.get_children())

    def build_channel(self, drv, ch, pad):
        color = drv.colors.get(ch, "#ffffff")
        head = tk.Label(self.ctrl, text=f" CH{ch} ", bg=color, fg="black", font=("Microsoft YaHei", 9, "bold"))
        f = ttk.LabelFrame(self.ctrl, labelwidget=head)
        f.pack(fill="x", pady=2)
        k = f"ch{ch}_"
        ttk.Checkbutton(f, text="显示", variable=self.var(k + "on", tk.BooleanVar, False),
                        command=lambda: self.job(lambda v=self.vars[k + "on"].get(): drv.set_ch_on(ch, v),
                                                 f"CH{ch} 开关", k + "on")).grid(row=0, column=0, **pad)
        ttk.Label(f, text="V/格").grid(row=0, column=1, sticky="e")
        self.maps[k + "vdiv"] = {}
        w = self.combo(f, k + "vdiv", [], 9,
                       lambda lab: self.job(lambda: drv.set_ch_scale(ch, self.maps[k + "vdiv"][lab]), f"CH{ch} V/格", k + "vdiv"))
        w.grid(row=0, column=2, **pad)
        self.widgets[k + "vdiv"] = w
        self.btn(f, "▲", lambda: self.job(lambda: drv.step_ch_scale(ch, True), f"CH{ch} V/格", k + "vdiv")).grid(row=0, column=3)
        self.btn(f, "▼", lambda: self.job(lambda: drv.step_ch_scale(ch, False), f"CH{ch} V/格", k + "vdiv")).grid(row=0, column=4)

        ttk.Label(f, text="位置").grid(row=1, column=0, **pad)
        g0 = ttk.Frame(f)
        g0.grid(row=1, column=1, columnspan=2, sticky="w")
        self.entry(g0, k + "pos", 9, lambda v: self.job(lambda: drv.set_ch_pos(ch, v), f"CH{ch} 位置")).pack(side="left", padx=3)
        ttk.Label(g0, textvariable=self.var(k + "posdiv"), width=9, foreground="gray").pack(side="left")
        self.btn(f, "▲", lambda: self.job(lambda: self._step_pos(ch, 1), f"CH{ch} 位置")).grid(row=1, column=3)
        self.btn(f, "▼", lambda: self.job(lambda: self._step_pos(ch, -1), f"CH{ch} 位置")).grid(row=1, column=4)
        self.btn(f, "归零", lambda: self.job(lambda: drv.set_ch_pos(ch, 0), f"CH{ch} 位置"), 5).grid(row=1, column=5, **pad)

        g = ttk.Frame(f)
        g.grid(row=2, column=0, columnspan=6, sticky="w")
        self.maps[k + "coup"] = {NAMES["coup"][cd]: cd for cd in drv.couplings}
        ttk.Label(g, text="耦合").pack(side="left", **pad)
        self.combo(g, k + "coup", list(self.maps[k + "coup"]), 8,
                   lambda lab: self.job(lambda: drv.set_ch_coupling(ch, self.maps[k + "coup"][lab]), f"CH{ch} 耦合", k + "coup")
                   ).pack(side="left")
        self.maps[k + "probe"] = {f"{p:g}X": p for p in drv.probes}
        ttk.Label(g, text="探头").pack(side="left", **pad)
        self.combo(g, k + "probe", list(self.maps[k + "probe"]), 6,
                   lambda lab: self.job(lambda: drv.set_ch_probe(ch, self.maps[k + "probe"][lab]), f"CH{ch} 探头", k + "probe")
                   ).pack(side="left")
        if drv.has_bw:
            ttk.Checkbutton(g, text="限带宽", variable=self.var(k + "bw", tk.BooleanVar, False),
                            command=lambda: self.job(lambda v=self.vars[k + "bw"].get(): drv.set_ch_bw(ch, v),
                                                     f"CH{ch} 带宽限制", k + "bw")).pack(side="left", padx=4)
        if drv.has_invert:
            ttk.Checkbutton(g, text="反相", variable=self.var(k + "inv", tk.BooleanVar, False),
                            command=lambda: self.job(lambda v=self.vars[k + "inv"].get(): drv.set_ch_invert(ch, v),
                                                     f"CH{ch} 反相", k + "inv")).pack(side="left")

    # ---- 需要“当前值 + 步进”的操作（在后台线程里读当前值，保证是最新的） ----
    def _step_pos(self, ch, sign):
        d = self.eng.drv
        s = d.ch_state(ch)
        d.set_ch_pos(ch, s["pos"] + sign * 0.2 * s["scale"])

    def _step_tbofs(self, sign):
        d = self.eng.drv
        st = d.state()
        d.set_tb_offset(st["tbofs"] + sign * st["tb"])

    def _step_lev(self, sign):
        d = self.eng.drv
        st = d.state()
        src = st["trig_src"]
        scale = st[int(src[2:])]["scale"] if src.startswith("CH") else 1.0
        d.set_trig_level(st["trig_lev"] + sign * 0.2 * scale)

    def on_runstop(self):
        fr = self.eng.frame
        stopped = fr is not None and fr["st"]["status"] == "STOP"
        d = self.eng.drv
        self.job(d.run if stopped else d.stop, "运行/停止")

    def on_auto(self):
        if messagebox.askyesno("自动设置", "自动设置会重新调整示波器的档位、时基和触发。\n确定要执行吗？"):
            self.job(self.eng.drv.auto, "自动设置")

    # ------------------------------------------------------------ 界面跟随示波器状态
    def setv(self, key, value):
        v = self.vars.get(key)
        if v is None or time.time() - self.touched.get(key, 0) < 1.5:
            return
        e = self.entries.get(key)
        if e is not None and self._focused() is e:              # 正在输入时不要覆盖
            return
        if v.get() != value:
            v.set(value)

    def _focused(self):
        """当前有键盘焦点的控件。下拉框展开时 tkinter 的 focus_get() 会抛 KeyError('popdown')（已知问题），当作没有"""
        try:
            return self.root.focus_get()
        except (KeyError, tk.TclError):
            return None

    def sync(self, st):
        d = self.eng.drv
        if d is None or not self.vars:
            return
        stopped = st["status"] == "STOP"
        txt, color = STATUS_TXT.get(st["status"], (st["status"], "#888"))
        self.run_btn.config(text=("■ 已停止（点击运行）" if stopped else f"▶ {txt}（点击停止）"),
                            bg="#d03030" if stopped else ("#d08000" if color == "#ffaa00" else "#2a9a2a"), width=18)
        for ch in range(1, d.channels + 1):
            c, k = st[ch], f"ch{ch}_"
            self.setv(k + "on", c["on"])
            labels = {fmt_si(v, "V", 3) + "/格": v for v in S.series_125(d.vdiv_range[0] * c["probe"], d.vdiv_range[1] * c["probe"])}
            if labels != self.maps.get(k + "vdiv"):
                self.maps[k + "vdiv"] = labels
                self.widgets[k + "vdiv"].config(values=list(labels))
            self.setv(k + "vdiv", fmt_si(c["scale"], "V", 3) + "/格")
            self.setv(k + "pos", fmt_si(c["pos"], "V", 4))
            self.setv(k + "posdiv", f"{c['pos'] / c['scale']:+.2f} 格")
            self.setv(k + "coup", NAMES["coup"].get(c["coup"], c["coup"]))
            self.setv(k + "probe", f"{c['probe']:g}X")
            if k + "bw" in self.vars:
                self.setv(k + "bw", c["bw"])
            if k + "inv" in self.vars:
                self.setv(k + "inv", c["inv"])
        self.setv("tb", fmt_si(st["tb"], "s", 3) + "/格")
        self.setv("tbofs", fmt_si(st["tbofs"], "s", 4))
        self.setv("src", NAMES["src"].get(st["trig_src"], st["trig_src"]))
        self.setv("slope", NAMES["slope"].get(st["trig_slope"], st["trig_slope"]))
        self.setv("sweep", NAMES["sweep"].get(st["trig_sweep"], st["trig_sweep"]))
        self.setv("tcoup", NAMES["tcoup"].get(st["trig_coup"], st["trig_coup"]))
        self.setv("lev", fmt_si(st["trig_lev"], "V", 4))
        self.setv("acq", NAMES["acq"].get(st["acq"], st["acq"]))
        self.setv("avg", str(st["avg"]))

    # ------------------------------------------------------------ 屏幕绘制
    def build_axes(self):
        d = self.eng.drv
        nch = d.channels if d else 2
        vd = d.vdiv if d else 8
        self.fig.clear()
        fft = self.math.startswith("FFT") and self.mode == "YT"
        if self.mode == "XY":
            self.ax = self.fig.add_axes([0.2, 0.06, 0.6, 0.88])
            self.ax.set_xlim(-vd / 2, vd / 2)
            self.ax.set_ylim(-vd / 2, vd / 2)
            self.ax.set_xticks(range(-vd // 2, vd // 2 + 1))
        else:
            self.ax = self.fig.add_axes([0.03, 0.40 if fft else 0.07, 0.94, 0.53 if fft else 0.86])
            self.ax.set_ylim(-vd / 2, vd / 2)
        ax = self.ax
        ax.set_facecolor(SCREEN_BG)
        ax.set_yticks(range(-vd // 2, vd // 2 + 1))
        ax.grid(True, color=GRID_COLOR, ls=":", lw=0.8)
        ax.axhline(0, color=GRID_COLOR, lw=0.8)
        ax.axvline(0, color=GRID_COLOR, lw=0.8)
        ax.tick_params(labelbottom=False, labelleft=False, length=0)
        for sp in ax.spines.values():
            sp.set_color("#6a7480")
        colors = d.colors if d else S.ScopeDriver.colors
        self.lines = {ch: ax.plot([], [], color=colors.get(ch, "#fff"), lw=1)[0] for ch in range(1, nch + 1)}
        self.math_line = ax.plot([], [], color=MATH_COLOR, lw=1)[0]
        bl = blended_transform_factory(ax.transAxes, ax.transData)
        # 通道零点标记（左侧 “1 ▶”）和触发电平标记（右侧 “◀ T”）用绘图符号画，避免字体缺字
        self.gnd = {}
        for ch in range(1, nch + 1):
            col = colors.get(ch, "#fff")
            self.gnd[ch] = Marker(ax, bl, -0.005, ">", str(ch), col, "right")
        self.trig_lev_mark = Marker(ax, bl, 1.005, "<", "T", "#ffa040", "left")
        self.trig_pos_mark = ax.text(0, 1.0, "▼", transform=blended_transform_factory(ax.transData, ax.transAxes),
                                     ha="center", va="bottom", color="#ffa040", fontsize=11, clip_on=False)
        self.status_txt = self.fig.text(0.03, 0.965, "", color="#e0e0e0", fontsize=10, va="center")
        self.status_box = self.fig.text(0.97, 0.965, "", color="#000", fontsize=10, va="center", ha="right",
                                        fontweight="bold", bbox=dict(boxstyle="round,pad=0.25", fc="#888", ec="none"))
        self.ch_txt = {ch: self.fig.text(0.03 + 0.2 * (ch - 1), 0.42 if fft else 0.025, "", color=colors.get(ch, "#fff"),
                                         fontsize=9.5, va="center")
                       for ch in range(1, nch + 1)}
        self.math_txt = self.fig.text(0.83, 0.42 if fft else 0.025, "", color=MATH_COLOR, fontsize=9.5, va="center")
        self.cur_lines = [ax.axvline(0, color="#e8e8e8", ls="--", lw=0.9, visible=False),
                          ax.axvline(0, color="#e8e8e8", ls="-.", lw=0.9, visible=False),
                          ax.axhline(0, color="#e8e8e8", ls="--", lw=0.9, visible=False),
                          ax.axhline(0, color="#e8e8e8", ls="-.", lw=0.9, visible=False)]
        self.cur_txt = ax.text(0.995, 0.985, "", transform=ax.transAxes, ha="right", va="top", color="#fff", fontsize=9,
                               family=["Consolas", "Microsoft YaHei"], bbox=dict(fc="#202830", ec="#606a74", alpha=0.85))
        self.fft_ax = None
        if fft:
            self.fft_ax = self.fig.add_axes([0.06, 0.07, 0.91, 0.28])
            fa = self.fft_ax
            fa.set_facecolor(SCREEN_BG)
            fa.grid(True, color=GRID_COLOR, ls=":", lw=0.8)
            fa.tick_params(colors="#c0c0c0", labelsize=8)
            for sp in fa.spines.values():
                sp.set_color("#6a7480")
            fa.set_ylabel("dBV(rms)", color="#c0c0c0", fontsize=8)
            self.fft_line = fa.plot([], [], color=MATH_COLOR, lw=0.9)[0]
        self.hd = None
        self.canvas.draw_idle()

    def draw(self, fr):
        d = self.eng.drv
        if d is None:
            return
        self.last_frame = fr
        st, waves = self._with_override(fr["st"]), fr["waves"]
        tb = st["tb"]
        # 横向：按波形数据的时间范围居中（各品牌返回的就是屏幕上的那一段）
        if waves:
            t0 = next(iter(waves.values()))[0]
            span = t0[-1] - t0[0]
            hd = max(2, int(round(span / tb))) if tb > 0 else d.hdiv
            center = (t0[0] + t0[-1]) / 2
        else:
            hd, center = d.hdiv, st["tbofs"]
        self.center = center
        if self.mode == "YT" and hd != self.hd:
            self.hd = hd
            self.ax.set_xlim(-hd / 2, hd / 2)
            self.ax.set_xticks(np.arange(-hd / 2, hd / 2 + 0.5, 1))
        half = d.vdiv / 2

        def ydiv(ch, v):
            c = st[ch]
            return (v + c["pos"]) / c["scale"]

        for ch, ln in self.lines.items():
            if ch in waves and self.mode == "YT":
                t, v = waves[ch]
                ln.set_data((t - center) / tb, ydiv(ch, v))
            else:
                ln.set_data([], [])
            c = st[ch]
            on = c["on"]
            self.gnd[ch].set_visible(on and self.mode == "YT")
            self.gnd[ch].set_y(max(-half, min(half, c["pos"] / c["scale"])))
            flags = [NAMES["coup"].get(c["coup"], c["coup"]).split()[-1]]
            if c["bw"]:
                flags.append("限带宽")
            if c["inv"]:
                flags.append("反相")
            self.ch_txt[ch].set_text(f"CH{ch}  {fmt_si(c['scale'], 'V', 3)}/格  {c['probe']:g}X  {' '.join(flags)}"
                                     if on else f"CH{ch}  关")
        # XY 模式
        if self.mode == "XY":
            if 1 in waves and 2 in waves:
                n = min(len(waves[1][1]), len(waves[2][1]))
                self.lines[1].set_data(ydiv(1, waves[1][1][:n]), ydiv(2, waves[2][1][:n]))
                self.lines[1].set_color("#40ff80")
            self.trig_lev_mark.set_visible(False)
            self.trig_pos_mark.set_visible(False)
        else:
            src = st["trig_src"]
            if src.startswith("CH") and int(src[2:]) in st:
                y = ydiv(int(src[2:]), st["trig_lev"])
                self.trig_lev_mark.set_visible(True)
                self.trig_lev_mark.set_y(max(-half, min(half, y)))
            else:
                self.trig_lev_mark.set_visible(False)
            xt = (0 - center) / tb
            self.trig_pos_mark.set_visible(abs(xt) <= hd / 2)
            self.trig_pos_mark.set_x(xt)
        self.draw_math(st, waves, tb, center)
        self.draw_cursor(st, tb, center)
        # 顶部状态
        txt, color = STATUS_TXT.get(st["status"], (st["status"], "#888888"))
        self.status_box.set_text(f" {txt} ")
        self.status_box.get_bbox_patch().set_facecolor(color)
        src_name = NAMES["src"].get(st["trig_src"], st["trig_src"])
        cnt = f"   频率计 {fmt_si(st['counter'], 'Hz', 7)}" if st.get("counter") else ""
        self.status_txt.set_text(f"时基 {fmt_si(tb, 's', 3)}/格    水平位置 {fmt_si(st['tbofs'], 's', 4)}    "
                                 f"触发 {src_name} {NAMES['slope'].get(st['trig_slope'], '')} "
                                 f"{fmt_si(st['trig_lev'], 'V', 3)}  {NAMES['sweep'].get(st['trig_sweep'], '')}{cnt}")
        self.canvas.draw_idle()
        self.update_meas(fr["meas"], st)
        if hasattr(self, "_t_last"):
            dt = time.time() - self._t_last
            self._fps = 0.8 * getattr(self, "_fps", 0) + 0.2 * (1 / dt if dt > 0 else 0)
            self.fps_lbl.config(text=f"刷新 {self._fps:.1f} 次/秒")
        self._t_last = time.time()

    def draw_math(self, st, waves, tb, center):
        self.math_line.set_data([], [])
        self.math_txt.set_text("")
        m = self.math
        if m == "关" or self.mode != "YT":
            return
        if m.startswith("FFT"):
            ch = int(m[-1])
            if ch not in waves or self.fft_ax is None:
                self.fft_line.set_data([], [])
                return
            t, v = waves[ch]
            dt = (t[-1] - t[0]) / (len(t) - 1)
            if self.eng.drv.__class__.__name__ == "UnitUTD" and len(v) % 2 == 0:
                v = v.reshape(-1, 2).mean(axis=1)       # UNI-T：每个时间点一对最大/最小值，取平均
                dt *= 2
            w = np.hanning(len(v))
            spec = np.abs(np.fft.rfft((v - v.mean()) * w)) * 2 / w.sum()
            f = np.fft.rfftfreq(len(v), dt)
            db = 20 * np.log10(np.maximum(spec / np.sqrt(2), 1e-9))
            fu, fs = (1e6, "MHz") if f[-1] >= 2e6 else ((1e3, "kHz") if f[-1] >= 2e3 else (1, "Hz"))
            self.fft_line.set_data(f / fu, db)
            self.fft_ax.set_xlim(0, f[-1] / fu)
            top = np.ceil((db[1:].max() + 10) / 10) * 10
            self.fft_ax.set_ylim(top - 120, top)
            self.fft_ax.set_xlabel(f"频率 ({fs})", color="#c0c0c0", fontsize=8)
            k = int(np.argmax(db[1:])) + 1
            self.math_txt.set_text(f"FFT CH{ch}  峰值 {fmt_si(f[k], 'Hz', 5)}  {db[k]:.1f} dBV   分辨率 {fmt_si(f[1], 'Hz', 3)}")
            return
        if 1 not in waves or 2 not in waves:
            self.math_txt.set_text(f"{m}：需要 CH1 和 CH2 都打开")
            return
        (t1, v1), (_, v2) = waves[1], waves[2]
        n = min(len(v1), len(v2))
        v1, v2, t1 = v1[:n], v2[:n], t1[:n]
        res = v1 + v2 if "+" in m else (v1 - v2 if "−" in m else v1 * v2)
        unit = "V²" if "×" in m else "V"
        rng = max(abs(res.max()), abs(res.min()), 1e-6)
        scale = S.nice_125(rng / (self.eng.drv.vdiv / 2 - 0.5))
        self.math_line.set_data((t1 - center) / tb, res / scale)
        self.math_txt.set_text(f"{m}  {fmt_si(scale, unit, 3)}/格")

    def draw_cursor(self, st, tb, center):
        cur = self.cursor
        for ln in self.cur_lines:
            ln.set_visible(False)
        self.cur_txt.set_visible(False)
        if cur["type"] == "关" or self.mode != "YT":
            return
        lines = []
        if cur["type"] == "时间":
            vals = []
            for i, key in enumerate(("A", "B")):
                if cur[key] is not None:
                    self.cur_lines[i].set_xdata([cur[key]] * 2)
                    self.cur_lines[i].set_visible(True)
                    t = cur[key] * tb + center
                    vals.append(t)
                    lines.append(f"{key}: {fmt_si(t, 's', 5)}")
            if len(vals) == 2:
                dtv = vals[1] - vals[0]
                lines.append(f"ΔT: {fmt_si(dtv, 's', 5)}")
                if dtv:
                    lines.append(f"1/ΔT: {fmt_si(1 / abs(dtv), 'Hz', 5)}")
        else:
            ch = cur["src"]
            if ch not in st:
                return
            c = st[ch]
            vals = []
            for i, key in enumerate(("A", "B")):
                if cur[key] is not None:
                    self.cur_lines[2 + i].set_ydata([cur[key]] * 2)
                    self.cur_lines[2 + i].set_visible(True)
                    v = cur[key] * c["scale"] - c["pos"]
                    vals.append(v)
                    lines.append(f"{key}: {fmt_si(v, 'V', 4)}")
            if len(vals) == 2:
                lines.append(f"ΔV: {fmt_si(vals[1] - vals[0], 'V', 4)}")
            lines.insert(0, f"电压光标 CH{ch}")
        if lines:
            self.cur_txt.set_text("\n".join(lines))
            self.cur_txt.set_visible(True)

    def update_meas(self, meas, st):
        d = self.eng.drv
        tree = self.meas_tree
        rows = []
        for c in range(1, d.channels + 1):
            if c in meas:
                rows.append([f"CH{c}"] + [fmt_si(meas[c].get(code), unit) for code, _, unit in S.MEAS])
            else:
                rows.append([f"CH{c}"] + ["关" if not st[c]["on"] else "…"] + [""] * (len(S.MEAS) - 1))
        items = tree.get_children()
        if len(items) != len(rows):
            tree.delete(*items)
            for r in rows:
                tree.insert("", "end", values=r)
        else:
            for it, r in zip(items, rows):
                if list(tree.item(it, "values")) != r:
                    tree.item(it, values=r)
        if "pair" in meas:
            ph, dl = meas["pair"]
            self.pair_lbl.config(text=f"CH1 → CH2：相位差 {'—' if ph is None else f'{ph:.2f}°'}    "
                                      f"延迟 {fmt_si(dl, 's')}    （“—”表示测不出，例如某一路没有信号）")
        else:
            self.pair_lbl.config(text="")

    # ------------------------------------------------------------ 电脑端功能
    def on_mode(self, lab):
        self.mode = "XY" if lab.startswith("XY") else "YT"
        if self.mode == "XY" and (self.eng.drv is None or self.eng.drv.channels < 2):
            self.mode = "YT"
        self.build_axes()

    def on_math(self, lab):
        self.math = lab
        self.build_axes()

    def on_cursor(self, _=None):
        new_type = self.vars["ctype"].get()
        if new_type != self.cursor["type"]:
            self.cursor["A"] = self.cursor["B"] = None
        self.cursor["type"] = new_type
        self.cursor["src"] = int(self.vars["csrc"].get()[-1])
        if self.last_frame:
            self.draw(self.last_frame)

    # ---- 拖动屏幕边缘的标记：左侧 1/2（通道垂直位置）、右侧 T（触发电平） ----
    def _with_override(self, st):
        """拖动时先在屏幕上“预览”新位置，不必等示波器回读，拖起来才跟手"""
        now = time.time()
        self.override = {k: v for k, v in self.override.items() if v[1] > now}
        if not self.override:
            return st
        st = dict(st)
        for key, (val, _) in self.override.items():
            if key == "lev":
                st["trig_lev"] = val
            elif key[1] in st:
                st[key[1]] = dict(st[key[1]], pos=val)
        return st

    def _marker_hit(self, ev):
        """鼠标是否按在某个标记上：返回 ("pos", 通道) / ("lev", None) / None"""
        fr = self.last_frame
        if fr is None or self.mode != "YT" or ev.x is None:
            return None
        st = self._with_override(fr["st"])
        half = self.eng.drv.vdiv / 2
        ax = self.ax
        cands = []
        for ch, m in self.gnd.items():
            if m.tri.get_visible():
                c = st[ch]
                cands.append((("pos", ch), -0.005, max(-half, min(half, c["pos"] / c["scale"]))))
        src = st["trig_src"]
        if self.trig_lev_mark.tri.get_visible() and src.startswith("CH") and int(src[2:]) in st:
            c = st[int(src[2:])]
            cands.append((("lev", None), 1.005, max(-half, min(half, (st["trig_lev"] + c["pos"]) / c["scale"]))))
        best, bd = None, 1e9
        for key, xa, yd in cands:
            px = ax.transAxes.transform((xa, 0))[0]
            py = ax.transData.transform((0, yd))[1]
            if abs(ev.x - px) <= 26 and abs(ev.y - py) <= 12:
                d = abs(ev.y - py)
                if d < bd:
                    best, bd = key, d
        return best

    def _drag_marker(self, ev, final=False):
        kind, ch = self.marker_drag
        fr = self.last_frame
        d = self.eng.drv
        if fr is None or d is None:
            return
        half = d.vdiv / 2
        y = self.ax.transData.inverted().transform((ev.x, ev.y))[1]
        y = max(-half, min(half, y))
        st = fr["st"]
        expire = time.time() + (1.5 if final else 1e9)
        if kind == "pos":
            val = round(y * st[ch]["scale"] / (st[ch]["scale"] * 0.02)) * st[ch]["scale"] * 0.02   # 以 0.02 格为步长
            self.override[("pos", ch)] = (val, expire)
            send = lambda v=val: d.set_ch_pos(ch, v)
            desc = f"CH{ch} 位置"
        else:
            src = int(st["trig_src"][2:])
            c = st[src]
            val = y * c["scale"] - c["pos"]
            self.override["lev"] = (val, expire)
            send = lambda v=val: d.set_trig_level(v)
            desc = "触发电平"
        now = time.time()
        if final or now - self._last_send > 0.12:      # 拖动中最多每 0.12 秒发一次命令，松开时再发最终值
            self._last_send = now
            self.eng.job(send, desc)
        self.draw(fr)

    # ---- 光标：点击放置 + 按住拖动 ----
    def _cursor_axis_px(self, ev):
        """鼠标在光标方向上的像素坐标（时间光标看 x，电压光标看 y）"""
        return ev.x if self.cursor["type"] == "时间" else ev.y

    def _cursor_px(self, key):
        """某条光标线当前的像素坐标"""
        val = self.cursor[key]
        if val is None:
            return None
        if self.cursor["type"] == "时间":
            return self.ax.transData.transform((val, 0))[0]
        return self.ax.transData.transform((0, val))[1]

    def _near_cursor(self, ev, tol=8):
        """鼠标附近（tol 像素内）的光标线：返回 'A' / 'B' / None"""
        best, dist = None, tol
        for key in ("A", "B"):
            p = self._cursor_px(key)
            if p is not None and abs(p - self._cursor_axis_px(ev)) <= dist:
                best, dist = key, abs(p - self._cursor_axis_px(ev))
        return best

    def _move_cursor(self, key, ev):
        x0, x1 = self.ax.get_xlim()
        y0, y1 = self.ax.get_ylim()
        if self.cursor["type"] == "时间":
            val = self.ax.transData.inverted().transform((ev.x, ev.y))[0]
            self.cursor[key] = min(max(val, x0), x1)
        else:
            val = self.ax.transData.inverted().transform((ev.x, ev.y))[1]
            self.cursor[key] = min(max(val, y0), y1)
        if self.last_frame:
            fr = self.last_frame
            self.draw_cursor(fr["st"], fr["st"]["tb"], getattr(self, "center", 0.0))
            self.canvas.draw_idle()

    def on_click(self, ev):
        if ev.button == 1:
            hit = self._marker_hit(ev)                  # 先看是不是按在 1/2/T 标记上
            if hit:
                self.marker_drag = hit
                self._drag_marker(ev)
                return
        if ev.inaxes is not self.ax or self.cursor["type"] == "关" or self.mode != "YT":
            return
        key = self._near_cursor(ev)                 # 点在已有光标线上 → 拖动它
        if key is None:                             # 点在空白处 → 左键放 A、右键放 B
            key = "A" if ev.button == 1 else ("B" if ev.button == 3 else None)
        if key is None:
            return
        self.dragging = key
        self._move_cursor(key, ev)

    def on_motion(self, ev):
        if self.marker_drag:
            self._drag_marker(ev)
            return
        if self.dragging:
            self._move_cursor(self.dragging, ev)
            return
        # 鼠标靠近可拖动的东西时变成双向箭头：1/2/T 标记 → ↕；光标线 → ↔ 或 ↕
        shape = ""
        if self._marker_hit(ev):
            shape = "sb_v_double_arrow"
        elif self.cursor["type"] != "关" and self.mode == "YT" and ev.inaxes is self.ax and self._near_cursor(ev):
            shape = "sb_h_double_arrow" if self.cursor["type"] == "时间" else "sb_v_double_arrow"
        w = self.canvas.get_tk_widget()
        if str(w.cget("cursor")) != shape:
            w.config(cursor=shape)

    def on_release(self, ev):
        if self.marker_drag:
            self._drag_marker(ev, final=True)
            self.marker_drag = None
        self.dragging = None

    def on_save(self):
        fr = self.last_frame
        if fr is None:
            messagebox.showwarning("提示", "还没有波形。")
            return
        base = os.path.join(SAVE_DIR, "波形_" + stamp())
        self.fig.savefig(base + ".png", dpi=110, facecolor=self.fig.get_facecolor())
        waves = fr["waves"]
        with open(base + ".csv", "w", encoding="utf-8-sig") as f:
            chans = sorted(waves)
            f.write(",".join(f"CH{c} 时间(s),CH{c} 电压(V)" for c in chans) + "\n")
            n = max((len(waves[c][0]) for c in chans), default=0)
            for i in range(n):
                cells = []
                for c in chans:
                    t, v = waves[c]
                    cells += [f"{t[i]:.9g}", f"{v[i]:.5g}"] if i < len(t) else ["", ""]
                f.write(",".join(cells) + "\n")
        self.say(f"已保存：{base}.png 和 .csv")

    # ------------------------------------------------------------ 自动扫频窗口
    def open_sweep(self):
        if self.eng.drv is None:
            messagebox.showwarning("提示", "示波器还没连接。")
            return
        SweepWindow(self)

    # ------------------------------------------------------------ 外部控制（在接口线程里运行）
    def _on_ext_toggle(self):
        self.ext.enabled = self.ext_var.get()
        self.say("已允许后台控制。" if self.ext.enabled else "已关闭后台控制：后台发来的操作会被拒绝。")

    def _eng(self, fn, timeout=120):
        """交给后台引擎（唯一和仪器通信的线程）执行并等结果"""
        return remote_api.call_on(lambda w: self.eng.job(w, "后台控制"), fn, timeout)

    def _ui(self, fn, timeout=30):
        """交给界面主线程执行并等结果"""
        return remote_api.call_on(self.uiq.put, fn, timeout)

    def _ext_need(self):
        if self.eng.drv is None:
            raise RuntimeError("示波器还没连接（可先调用 connect）")
        return self.eng.drv

    @staticmethod
    def _code(cat, value):
        """选项可以给代码（AC、CH2、NORM）也可以给中文名（交流、正常）"""
        if cat is None:
            return value
        v = str(value)
        if v.upper() in NAMES[cat]:
            return v.upper()
        for code, name in NAMES[cat].items():
            if v == name or v in name.split():
                return code
        raise ValueError(f"无法识别的选项“{value}”，可选：{list(NAMES[cat])}")

    def _ext_dispatch(self, method, a):
        if method not in ("status", "state", "measure", "pair", "waveform", "screenshot"):
            self.uiq.put(lambda: self.say(f"【后台】{method} {a if a else ''}"))
        if method == "status":
            fr = self.eng.frame
            d = self.eng.drv
            return {"connected": d is not None, "describe": d.describe() if d else None,
                    "tested": d.is_tested() if d else None, "channels": d.channels if d else None,
                    "gen": self.eng.gen.describe() if self.eng.gen else None,
                    "state": fr["st"] if fr else None,
                    "meas": {str(k): v for k, v in fr["meas"].items()} if fr else None,
                    "frame_age_s": round(time.time() - fr["time"], 2) if fr else None}
        if method == "connect":
            self._eng(self._connect)
            return {"connected": self.eng.drv is not None,
                    "describe": self.eng.drv.describe() if self.eng.drv else None}
        if method == "disconnect":
            self._eng(self._disconnect)
            return {"connected": False}
        if method == "gen_connect":
            self._eng(self._gen_connect)
            return {"gen": self.eng.gen.describe() if self.eng.gen else None}
        if method == "gen_disconnect":
            self._eng(self._gen_disconnect)
            return {"gen": None}
        if method == "screenshot":
            path = a.get("path") or os.path.join(SAVE_DIR, "截图_" + stamp() + ".png")
            self._ui(lambda: self.fig.savefig(path, dpi=110, facecolor=self.fig.get_facecolor()))
            return path
        if method == "display":
            def ui():
                if "mode" in a:
                    m = "XY（CH1-CH2）" if str(a["mode"]).upper().startswith("XY") else "YT（时间）"
                    self.vars["mode"].set(m)
                    self.on_mode(m)
                if "math" in a:
                    self.vars["math"].set(a["math"])
                    self.on_math(a["math"])
                if "cursor" in a or "csrc" in a:
                    if "cursor" in a:
                        self.vars["ctype"].set(a["cursor"])
                    if "csrc" in a:
                        self.vars["csrc"].set(f"CH{int(a['csrc'])}")
                    self.on_cursor()
                for k in ("A", "B"):
                    if k in a:
                        self.cursor[k] = None if a[k] is None else float(a[k])
                if self.last_frame:
                    self.draw(self.last_frame)
                return {"mode": self.mode, "math": self.math, "cursor": dict(self.cursor),
                        "cursor_readout": self.cur_txt.get_text() if self.cur_txt.get_visible() else ""}
            return self._ui(ui)
        if method == "sweep":
            return self._ext_sweep(a)

        d = self._ext_need()
        ch = int(a.get("ch", 1))
        if method == "state":
            return self._eng(d.state)
        if method == "measure":
            items = a.get("items")
            if items:
                return self._eng(lambda: {c: d.measure(c, ch) for c in items})
            return self._eng(lambda: d.measure_all(ch))
        if method == "pair":
            ph, dl = self._eng(d.measure_pair)
            return {"phase_deg": ph, "delay_s": dl}
        if method == "waveform":
            t, v = self._eng(lambda: d.waveform(ch))
            return {"t": t, "v": v}
        if method in ("run", "stop", "single", "auto", "trig_50"):
            self._eng(getattr(d, method))
            return "ok"
        if method == "set":
            what = a.get("what")
            if what not in SET_METHODS:
                raise ValueError(f"what 可选：{list(SET_METHODS)}")
            name, with_ch, cat = SET_METHODS[what]
            val = self._code(cat, a["value"])
            if cat is None and not isinstance(val, bool):
                val = float(val) if what != "avg_count" else int(val)
            fn = getattr(d, name)
            self._eng(lambda: fn(ch, val) if with_ch else fn(val))
            return "ok"
        if method == "step":
            up = bool(a.get("up", True))
            if a.get("what") == "timebase":
                self._eng(lambda: d.step_timebase(up))
            else:
                self._eng(lambda: d.step_ch_scale(ch, up))
            return "ok"
        if method == "autoscale_ch":
            return self._eng(lambda: d.autoscale_volts(ch), timeout=60)
        if method in ("scpi_query", "scpi_write"):
            cmd = str(a["cmd"])
            if isinstance(d, S.UnitUTD) and any(b in cmd.upper().replace(" ", "") for b in SCPI_BLOCK):
                raise RuntimeError("这条命令会让 UNI-T 示波器的 USB 卡死，已拦截")
            if method == "scpi_query":
                return self._eng(lambda: d.query(cmd))
            self._eng(lambda: d.write(cmd))
            return {"errors": self._eng(d.errors)}
        raise RuntimeError(f"未实现的方法：{method}")

    def _ext_sweep(self, a):
        self._ext_need()
        f1, f2 = float(a.get("f1", 100)), float(a.get("f2", 1e6))
        n, amp = int(a.get("n", 25)), float(a.get("amp", 2))
        gch, cin = int(a.get("gch", 1)), int(a.get("cin", 1))
        cout = int(a["cout"]) if a.get("cout") else None
        if not (0 < f1 < f2 and 2 <= n <= 500 and amp > 0):
            raise ValueError("参数要求：0 < f1 < f2，2 ≤ n ≤ 500，amp > 0")
        win = self._ui(lambda: SweepWindow(self))
        self._ui(lambda: [win.v[k].set(str(v)) for k, v in
                          (("f1", f1), ("f2", f2), ("n", n), ("amp", amp), ("gch", gch), ("in", f"CH{cin}"),
                           ("out", f"CH{cout}" if cout else "无"))])
        auto_gen = self.eng.gen is None
        if auto_gen:
            self._eng(self._gen_connect)
        try:
            self.eng.stop_flag.clear()
            res = self._eng(lambda: win.run(f1, f2, n, amp, gch, cin, cout, strict=True), timeout=1800)
        finally:
            if auto_gen:
                self._eng(self._gen_disconnect)
        self._ui(lambda: None)            # 等界面把图画完、存完
        base = res["base"]
        cols = ["freq_set", "freq_meas", "vin_pp", "vout_pp", "gain_db"]
        return {"rows": [dict(zip(cols, r)) for r in res["rows"]],
                "png": base + ".png" if base else None, "csv": base + ".csv" if base else None}

    def on_close(self):
        try:
            self.ext.stop()
        except Exception:
            pass
        self.eng.stop_flag.set()
        self.eng.quit = True
        time.sleep(0.2)
        for dv in (self.eng.drv, self.eng.gen):
            if dv is not None:
                dv.close()
        self.root.destroy()


class SweepWindow:
    """自动扫频：信号发生器逐点改频率，示波器自动调档测量，画出幅频曲线。扫频期间实时显示暂停。"""

    def __init__(self, app):
        self.app = app
        eng = app.eng
        d = eng.drv
        self.win = tk.Toplevel(app.root)
        self.win.title("自动扫频（频率响应）")
        pad = dict(padx=4, pady=3)
        f = ttk.Frame(self.win)
        f.pack(fill="x", **pad)
        self.v = {}
        for i, (lab, key, val) in enumerate([("起始频率 Hz", "f1", "100"), ("终止频率 Hz", "f2", "1000000"),
                                             ("点数", "n", "25"), ("幅度 Vpp", "amp", "2")]):
            ttk.Label(f, text=lab).grid(row=0, column=2 * i, sticky="e", **pad)
            self.v[key] = tk.StringVar(value=val)
            ttk.Entry(f, textvariable=self.v[key], width=10).grid(row=0, column=2 * i + 1, **pad)
        chs = [f"CH{c}" for c in range(1, d.channels + 1)]
        ttk.Label(f, text="信号发生器通道").grid(row=1, column=0, sticky="e", **pad)
        self.v["gch"] = tk.StringVar(value="1")
        ttk.Combobox(f, textvariable=self.v["gch"], values=["1", "2"], width=7, state="readonly").grid(row=1, column=1)
        ttk.Label(f, text="输入 → 示波器").grid(row=1, column=2, sticky="e", **pad)
        self.v["in"] = tk.StringVar(value="CH1")
        ttk.Combobox(f, textvariable=self.v["in"], values=chs, width=7, state="readonly").grid(row=1, column=3)
        ttk.Label(f, text="输出 → 示波器").grid(row=1, column=4, sticky="e", **pad)
        self.v["out"] = tk.StringVar(value="无")
        ttk.Combobox(f, textvariable=self.v["out"], values=["无"] + chs, width=7, state="readonly").grid(row=1, column=5)
        ttk.Button(f, text="开始扫频", command=self.start).grid(row=1, column=6, **pad)
        ttk.Button(f, text="停止", command=eng.stop_flag.set).grid(row=1, column=7, **pad)
        ttk.Label(f, foreground="gray", text="只测一路：输出选「无」。测电路：输入接电路输入端，输出接电路输出端，会画出增益(dB)。"
                                           "扫频时主窗口暂停刷新，扫完自动恢复两台仪器原来的设置。").grid(
            row=2, column=0, columnspan=8, sticky="w", **pad)
        self.fig = Figure(figsize=(9.5, 4.8), dpi=96)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.win)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, **pad)
        self.log = tk.Text(self.win, height=5, font=("Microsoft YaHei", 9))
        self.log.pack(fill="x", **pad)

    def say(self, msg):
        try:
            self.log.insert("end", time.strftime("%H:%M:%S  ") + msg + "\n")
            self.log.see("end")
        except tk.TclError:
            pass

    def start(self):
        eng = self.app.eng
        need_gen = eng.gen is None
        if need_gen and not messagebox.askyesno(
                "需要信号发生器", "扫频需要控制信号发生器，现在连接它吗？\n（扫完后可以在主窗口点信号发生器的「断开」）",
                parent=self.win):
            return
        try:
            f1, f2 = float(self.v["f1"].get()), float(self.v["f2"].get())
            n, amp = int(self.v["n"].get()), float(self.v["amp"].get())
            assert 0 < f1 < f2 and 2 <= n <= 500 and amp > 0
        except Exception:
            messagebox.showerror("参数错误", "请检查：起始频率 < 终止频率，点数 2~500，幅度 > 0。", parent=self.win)
            return
        gch, cin = int(self.v["gch"].get()), int(self.v["in"].get()[-1])
        cout = None if self.v["out"].get() == "无" else int(self.v["out"].get()[-1])
        if cout == cin:
            messagebox.showerror("参数错误", "输入和输出不能是同一个示波器通道。", parent=self.win)
            return
        if eng.gen is not None and gch > eng.gen.channels:
            messagebox.showerror("参数错误", f"这台信号发生器只有 {eng.gen.channels} 个通道。", parent=self.win)
            return
        eng.stop_flag.clear()
        if need_gen:
            eng.job(self.app._gen_connect, "连接信号发生器")
        eng.job(lambda: self.run(f1, f2, n, amp, gch, cin, cout), "扫频")
        if need_gen:                     # 为扫频临时连上的，扫完自动断开，让信号发生器回到本地（面板可操作）
            eng.job(self.app._gen_disconnect, "断开信号发生器")

    def run(self, f1, f2, n, amp, gch, cin, cout, strict=False):
        """执行扫频；返回 {"rows": [...], "base": 文件路径(不含扩展名)}。strict=True（外部控制调用）时出错直接抛异常"""
        eng = self.app.eng
        sc, g = eng.drv, eng.gen
        put = eng.ui_put

        def fail(msg):
            put(lambda: self.say(msg))
            if strict:
                raise RuntimeError(msg)
            return None
        if g is None:
            return fail("没找到信号发生器，无法扫频。请检查它的 USB 线和电源。")
        if sc is None:
            return fail("示波器没连接，无法扫频。")
        if gch > g.channels:
            return fail(f"这台信号发生器只有 {g.channels} 个通道。")
        st = sc.state()
        for ch in (cin, cout):
            if ch and not st[ch]["on"]:
                return fail(f"示波器 CH{ch} 没有打开，请先在主窗口勾选 CH{ch}「显示」。")
        freqs = np.logspace(np.log10(f1), np.log10(f2), n)
        saved_scope, saved_gen, gen_out = sc.snapshot(), g.read_settings(gch), g.output_state(gch)
        put(lambda: self.say(f"开始扫频：{fmt_si(f1, 'Hz')} → {fmt_si(f2, 'Hz')}，{n} 点，{amp} Vpp。"))
        rows = []
        try:
            sc.set_tb_offset(0)
            for ch in (cin, cout):
                if ch:
                    sc.set_ch_pos(ch, 0)
            g.apply(gch, "SIN", float(freqs[0]), amp, 0, 0, 50, True)
            g.output(gch, True)
            for i, f in enumerate(freqs):
                if eng.stop_flag.is_set():
                    put(lambda: self.say("已停止。"))
                    break
                g.apply(gch, "SIN", float(f), amp, 0, 0, 50, True)
                tb = S.nice_125(3.0 / f / sc.hdiv)
                sc.set_timebase(tb)
                time.sleep((1.5 if i == 0 else 0.35) + 2.5 * sc.hdiv * tb)
                vin = sc.autoscale_volts(cin)
                vout = sc.autoscale_volts(cout) if cout else None
                gain = 20 * np.log10(vout / vin) if (vin and vout) else None
                rows.append((f, sc.measure("FREQ", cin), vin, vout, gain))
                msg = (f"[{i + 1}/{n}] {fmt_si(f, 'Hz')}：输入 {fmt_si(vin, 'Vpp')}"
                       + (f"，输出 {fmt_si(vout, 'Vpp')}，增益 {gain:.2f} dB" if gain is not None else ""))
                snap = list(rows)
                put(lambda m=msg, r=snap: (self.say(m), self.plot(r, cout, amp)))
        finally:
            ok = True
            try:
                ok = sc.restore(saved_scope)
            except Exception as e:
                put(lambda e=e: self.say(f"示波器设置恢复失败：{e}"))
            try:
                gs = saved_gen
                if gs.get("func") in g.waves:
                    g.apply(gch, gs["func"], gs["freq"], gs["amp"], gs["offset"], gs.get("phase", 0),
                            gs.get("duty", 50), gs.get("highz", True))
                if gen_out is not None:
                    g.output(gch, gen_out)
            except Exception as e:
                put(lambda e=e: self.say(f"信号发生器设置恢复失败：{e}"))
            g.go_local()                 # 扫完把信号发生器面板交还（即使仍保持连接，也能直接手动操作）
            if not ok:
                put(lambda: self.say("⚠ 示波器设置没能完全恢复，请检查档位和时基。"))
        base = None
        if rows:
            base = os.path.join(SAVE_DIR, "扫频_" + stamp())
            with open(base + ".csv", "w", encoding="utf-8-sig") as fcsv:
                fcsv.write("设定频率(Hz),实测频率(Hz),输入峰峰值(V),输出峰峰值(V),增益(dB)\n")
                for r in rows:
                    fcsv.write(",".join("" if x is None else f"{x:.6g}" for x in r) + "\n")
            put(lambda: (self.plot(rows, cout, amp), self.fig.savefig(base + ".png", dpi=120),
                         self.say(f"扫频完成，已保存：{base}.png 和 .csv")))
        return {"rows": rows, "base": base}

    def plot(self, rows, cout, amp):
        try:
            self.fig.clear()
        except tk.TclError:
            return
        ax = self.fig.add_subplot(111)
        f = [r[0] for r in rows]
        if cout:
            y = [r[4] if r[4] is not None else np.nan for r in rows]
            ax.semilogx(f, y, "o-", color="#d62728", ms=4)
            ax.axhline(-3, color="gray", ls="--", lw=1)
            ax.text(f[0], -3, " −3 dB", va="bottom", color="gray", fontsize=9)
            ax.set_ylabel("增益 (dB) = 20·log(输出/输入)")
            ax.set_title("频率响应（波特图·幅频）")
        else:
            y = [r[2] if r[2] is not None else np.nan for r in rows]
            ax.semilogx(f, y, "o-", color="#00a0c0", ms=4)
            ax.axhline(amp, color="gray", ls="--", lw=1)
            ax.text(f[0], amp, f" 设定值 {amp} Vpp", va="bottom", color="gray", fontsize=9)
            ax.set_ylabel("示波器测得的峰峰值 (Vpp)")
            ax.set_title("扫频：信号发生器输出幅度 vs 频率")
            if np.isfinite(y).any():
                ax.set_ylim(min(np.nanmin(y), amp) * 0.8, max(np.nanmax(y), amp) * 1.15)
        ax.set_xlabel("频率 (Hz)")
        ax.grid(True, which="both", ls=":", alpha=0.6)
        self.fig.tight_layout()
        self.canvas.draw_idle()


if __name__ == "__main__":
    if not gen.single_instance("scope_gui"):
        _r = tk.Tk()
        _r.withdraw()
        messagebox.showinfo("已经打开了", "示波器控制程序已经在运行（可能被其他窗口挡住了，或最小化在任务栏）。\n"
                                       "同时打开两份会同时给示波器发命令，可能导致示波器 USB 卡死，所以这次不再打开新的。")
        _r.destroy()
        sys.exit(0)
    gen.set_app_id("scope")
    root = tk.Tk()
    gen.set_icon(root, "scope.ico", os.path.dirname(os.path.abspath(__file__)))
    App(root)
    root.geometry("1880x990+10+5")
    root.mainloop()
