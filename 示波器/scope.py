"""
示波器驱动（通用版）—— 统一接口 + 各品牌驱动，连接时按 *IDN? 自动选择

  驱动               适用型号                                              状态
  UnitUTD           UNI-T UTD2000 / UTD2000CEX(+) 系列（IDN 型号显示为 DSO） 已实测（UTD2202CEX+）
  RigolDS           Rigol DS1000Z/DS2000/DS4000/MSO5000/DHO 等；UNI-T UPO/MSO  未实测
  KeysightIV        Keysight/Agilent InfiniiVision：DSOX/MSOX/EDUX/DSO-X     未实测
  SiglentSDS        Siglent SDS1000X-E / SDS1000CML / SDS2000X（经典命令集）    未实测
  TektronixTBS      Tektronix TBS1000/TBS2000/MDO/MSO/DPO                    未实测
  GenericSCPI       其他品牌（兼容模式，按最常见的 SCPI 写法）                 未实测

统一约定（界面只和这些打交道，不关心品牌差异）：
  电压一律是“屏幕显示值”（已含探头倍率）。
  通道位置 pos（伏）：屏幕上第 y 格 = (电压 + pos) / V每格 —— 正值让波形上移。
  时间：波形数据自带绝对时间（触发点为 0），界面按数据范围居中显示。
  选项代码：耦合 DC/AC/GND；触发源 CH1~CH4/EXT/EXT5/AC；斜率 POS/NEG/ALT；
            触发方式 AUTO/NORM/SING；触发耦合 DC/AC/LF/HF/NOISE；采集 NORM/AVER/PEAK/HRES。
  测量项代码见 MEAS。

UNI-T UTD2000 实测到的“怪癖”（已处理，见 UnitUTD）：
  1. :CHANnel<n>:SCALe 读回含探头倍率，写入却不含 —— 写之前除以探头倍率。
  2. PREamble 声明 800 点，实际返回 1600 个值（每个时间点一对最大/最小值）。
  3. 不要切换 :WAVeform:FORMat、不要发 :DISPlay:DATA?（截屏）、不要读 MATH 波形 —— 会导致 USB 卡死。
  4. 示波器自带光标的远程读数不正确 —— 光标由电脑端计算。
  5. 查询 :CHANnel<n>:SCALe? 会让面板“选中”该通道（面板旋钮改调它）—— 只查询面板选中的通道，
     其他通道的 V/格 由波形数据得出（yinc × 25）。
"""
import os
import re
import sys
import time
import math

import numpy as np

# 程序文件夹（记录存在这里的“记录”子文件夹）：打包成 exe 后是 exe 所在文件夹，否则是本文件所在文件夹
HERE = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
GEN_DIR = os.path.join(os.path.dirname(HERE), "信号发生器")
if GEN_DIR not in sys.path:
    sys.path.insert(0, GEN_DIR)
import drivers as gen          # noqa: E402  复用信号发生器程序的 VISA 加载与驱动

# 标准测量项：(代码, 中文名, 单位)
MEAS = [("FREQ", "频率", "Hz"), ("PER", "周期", "s"), ("VPP", "峰峰值", "V"), ("VMAX", "最大值", "V"),
        ("VMIN", "最小值", "V"), ("VAVG", "平均值", "V"), ("VRMS", "有效值", "V"), ("VAMP", "幅度", "V"),
        ("VTOP", "顶端值", "V"), ("VBASE", "底端值", "V"), ("PWID", "正脉宽", "s"), ("NWID", "负脉宽", "s"),
        ("PDUT", "正占空比", "%"), ("RISE", "上升时间", "s"), ("FALL", "下降时间", "s"), ("OVER", "过冲", "%")]

SCOPE_HINT = re.compile(r",\s*(DSO|UTD|UPO|MSO|DS\d|DHO|SDS|TBS|TDS|MDO|DPO|EDUX|DSOX|MSOX|GDS|ADS|ADO)", re.I)


class NotSupported(Exception):
    pass


def nice_125(x):
    """把 x 规整到 1-2-5 序列中不小于 x 的最小值"""
    if x <= 0:
        return 1.0
    e = math.floor(math.log10(x))
    for m in (1, 2, 5, 10):
        v = m * 10 ** e
        if v >= x * (1 - 1e-9):
            return float(f"{v:.3g}")
    return 10.0 ** (e + 1)


def step_125(x, up):
    """1-2-5 序列中比 x 大一档（up=True）或小一档的值"""
    seq = series_125(x / 100, x * 100)
    if up:
        return next((v for v in seq if v > x * 1.001), seq[-1])
    return next((v for v in reversed(seq) if v < x * 0.999), seq[0])


def series_125(lo, hi):
    out, e = [], math.floor(math.log10(lo))
    while True:
        for m in (1, 2, 5):
            v = float(f"{m * 10 ** e:.3g}")
            if v > hi * (1 + 1e-9):
                return out
            if v >= lo * (1 - 1e-9):
                out.append(v)
        e += 1


def to_float(s):
    """解析示波器返回的数值；测不出时（*、****、9.9E37 等）返回 None"""
    if s is None:
        return None
    m = re.search(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", str(s))
    if not m:
        return None
    v = float(m.group())
    if math.isnan(v) or abs(v) > 1e30:
        return None
    return v


def after_header(s):
    """Siglent 经典命令返回 'C1:VDIV 5.00E-01V' 这种带头的格式，取空格后的部分"""
    s = str(s).strip()
    return s.split(" ", 1)[1] if " " in s else s


def model_of(idn):
    parts = [p.strip() for p in idn.split(",")]
    return parts[1] if len(parts) > 1 else ""


def channels_from_model(model, default=2):
    """型号数字的最后一位通常是通道数：DS1104Z→4、DSOX1102G→2、TBS1052B→2、SDS1204X-E→4"""
    m = re.search(r"(\d{3,})", model)
    if m and m.group(1)[-1] in "24":
        return int(m.group(1)[-1])
    return default


# =============================================================================
# 基类：通用的辅助功能和默认实现
# =============================================================================
class ScopeDriver:
    family = "未知"
    tested_re = None             # 匹配到的型号视为已实测
    hdiv, vdiv = 10, 8
    colors = {1: "#f2d200", 2: "#00c8f0", 3: "#ff4fd8", 4: "#3d7bff"}
    couplings = ["DC", "AC", "GND"]
    probes = [1, 10, 100, 1000]
    trig_extra = ["EXT", "AC"]
    slopes = ["POS", "NEG", "ALT"]
    sweeps = ["AUTO", "NORM", "SING"]
    trig_coups = ["DC", "AC", "LF", "HF"]
    acq_types = ["NORM", "AVER", "PEAK"]
    avg_counts = [2, 4, 8, 16, 32, 64, 128, 256]
    has_bw = True
    has_invert = True
    has_pair = True               # 能否测 CH1→CH2 相位/延迟
    err_cmd = ":SYSTem:ERRor?"
    vdiv_range = (1e-3, 10.0)     # 不含探头倍率
    tdiv_range = (2e-9, 50.0)

    def __init__(self, inst, idn):
        self.inst = inst
        self.idn = idn
        self.vendor = idn.split(",")[0].strip()
        self.model = model_of(idn)
        self.channels = self.detect_channels()
        self._err_ok = bool(self.err_cmd)
        self._probe_cache = {}

    @classmethod
    def match(cls, idn):
        return False

    def is_tested(self):
        return bool(self.tested_re and re.search(self.tested_re, self.idn, re.I))

    def detect_channels(self):
        return channels_from_model(self.model)

    def describe(self):
        return f"{self.vendor} {self.model}（{self.family}，{self.channels} 通道）"

    def trig_sources(self):
        return [f"CH{c}" for c in range(1, self.channels + 1)] + list(self.trig_extra)

    # ---------- 底层 ----------
    def write(self, cmd):
        self.inst.write(cmd)
        time.sleep(0.03)

    def query(self, cmd):
        return self.inst.query(cmd).strip()

    def qstr(self, cmd, default=""):
        try:
            return self.query(cmd)
        except Exception:
            self.recover()
            return default

    def qfloat(self, cmd):
        try:
            return to_float(self.query(cmd))
        except Exception:
            self.recover()
            return None

    def try_query(self, *cmds):
        for c in cmds:
            try:
                return self.query(c)
            except Exception:
                self.recover()
        return None

    def recover(self):
        try:
            self.inst.clear()
        except Exception:
            pass

    def errors(self):
        if not self._err_ok:
            return []
        out = []
        for _ in range(10):
            try:
                e = self.query(self.err_cmd)
            except Exception:
                self.recover()
                self._err_ok = False
                break
            if re.match(r"^\s*[+]?0\b", e) or "NO ERROR" in e.upper():
                break
            out.append(e)
        return out

    def init(self):
        """连接后做一次初始化（比如设置波形读取格式）"""
        pass

    def close(self):
        try:
            self.inst.close()
        except Exception:
            pass

    # ---------- 运行控制（子类实现） ----------
    def run(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def single(self):
        raise NotImplementedError

    def auto(self):
        raise NotImplementedError

    def trig_50(self):
        """默认：用信源通道的最大/最小值算出中点"""
        st = self.state()
        src = st["trig_src"]
        if not src.startswith("CH"):
            raise NotSupported("外部/市电触发无法自动设到 50%。")
        ch = int(src[2:])
        vmax, vmin = self.measure("VMAX", ch), self.measure("VMIN", ch)
        if vmax is None or vmin is None:
            raise NotSupported("信源通道测不出最大/最小值，无法设到 50%。")
        self.set_trig_level((vmax + vmin) / 2)

    # ---------- 通道 / 水平 / 触发 / 采集 —— 读写（子类实现） ----------
    def state(self):
        raise NotImplementedError

    def set_ch_on(self, ch, on):
        raise NotImplementedError

    def set_ch_scale(self, ch, volts):
        raise NotImplementedError

    def step_ch_scale(self, ch, up):
        st = self.ch_state(ch)
        self.set_ch_scale(ch, step_125(st["scale"], up))

    def set_ch_pos(self, ch, volts):
        raise NotImplementedError

    def set_ch_coupling(self, ch, code):
        raise NotImplementedError

    def set_ch_probe(self, ch, factor):
        raise NotImplementedError

    def set_ch_bw(self, ch, on):
        raise NotSupported("不支持带宽限制。")

    def set_ch_invert(self, ch, on):
        raise NotSupported("不支持反相。")

    def set_timebase(self, sec):
        raise NotImplementedError

    def step_timebase(self, up):
        self.set_timebase(step_125(self.timebase(), up))

    def set_tb_offset(self, sec):
        raise NotImplementedError

    def set_trig_source(self, code):
        raise NotImplementedError

    def set_trig_level(self, volts):
        raise NotImplementedError

    def set_trig_slope(self, code):
        raise NotImplementedError

    def set_trig_sweep(self, code):
        raise NotImplementedError

    def set_trig_coupling(self, code):
        raise NotImplementedError

    def set_acq_type(self, code):
        raise NotImplementedError

    def set_avg_count(self, n):
        raise NotImplementedError

    def timebase(self):
        raise NotImplementedError

    def ch_state(self, ch):
        raise NotImplementedError

    # ---------- 测量 / 波形（子类实现） ----------
    def measure(self, code, ch):
        raise NotImplementedError

    def measure_all(self, ch):
        return {code: self.measure(code, ch) for code, _, _ in MEAS}

    def measure_pair(self):
        return (None, None)

    def waveform(self, ch):
        raise NotImplementedError

    # ---------- 扫频用：快照 / 恢复 / 自动调档 ----------
    def snapshot(self):
        d = {"tb": self.timebase(), "tbofs": self.state()["tbofs"]}
        for ch in range(1, self.channels + 1):
            s = self.ch_state(ch)
            d[ch] = (s["scale"], s["pos"])
        return d

    def restore(self, d):
        for _ in range(2):
            if d.get("tb"):
                self.set_timebase(d["tb"])
            if d.get("tbofs") is not None:
                self.set_tb_offset(d["tbofs"])
            for ch in range(1, self.channels + 1):
                sc, pos = d.get(ch, (None, None))
                if sc:
                    self.set_ch_scale(ch, sc)
                    time.sleep(0.2)
                if pos is not None:
                    self.set_ch_pos(ch, pos)
            time.sleep(0.3)
            now = self.snapshot()
            if all(self._close(now.get(k), d.get(k)) for k in d):
                return True
        return False

    @staticmethod
    def _close(a, b):
        if isinstance(a, tuple):
            return all(ScopeDriver._close(x, y) for x, y in zip(a, b))
        if a is None or b is None:
            return a == b
        return abs(a - b) <= 1e-6 * max(abs(a), abs(b), 1e-12)

    def _meas_retry(self, code, ch, tries=3, wait=0.25):
        for _ in range(tries):
            v = self.measure(code, ch)
            if v is not None:
                return v
            time.sleep(wait)
        return None

    def stable_measure(self, code, ch, tol=0.03, tries=5, wait=0.3):
        """连续读，直到相邻两次相差不超过 3%，避免读到刷新中途的旧数据"""
        prev = self._meas_retry(code, ch)
        for _ in range(tries):
            time.sleep(wait)
            cur = self._meas_retry(code, ch)
            if prev is not None and cur is not None and abs(cur - prev) <= tol * max(abs(prev), 1e-12):
                return (prev + cur) / 2
            prev = cur
        return prev

    def autoscale_volts(self, ch, tries=6, settle=0.5):
        """调整 V/格，让波形占屏幕约 3~7 格（通道位置需为 0）；返回最终峰峰值"""
        pf = self.ch_state(ch)["probe"]
        lo_limit, hi_limit = self.vdiv_range[0] * pf, self.vdiv_range[1] * pf
        half = self.vdiv / 2
        for _ in range(tries):
            sc = self.ch_state(ch)["scale"] or 1.0
            vmax, vmin = self._meas_retry("VMAX", ch), self._meas_retry("VMIN", ch)
            if vmax is None or vmin is None:
                new = sc * 2
            else:
                vpp = vmax - vmin
                if max(vmax, -vmin) > (half - 0.2) * sc:
                    new = sc * 2.5
                elif vpp < 3.0 * sc:
                    new = vpp / 6
                else:
                    break
            new = min(max(nice_125(new / pf) * pf, lo_limit), hi_limit)
            if abs(new - sc) / sc < 1e-6:
                break
            self.set_ch_scale(ch, new)
            time.sleep(settle)
        return self.stable_measure("VPP", ch)


# =============================================================================
# 以 SCPI “:CHANnel<n>:xxx” 风格为基础的公共实现（UNI-T / Rigol / Keysight / 通用）
# =============================================================================
class _ScpiStyle(ScopeDriver):
    # 子类可改写的命令名
    tb_scale_cmd = ":TIMebase:SCALe"
    tb_offset_cmd = ":TIMebase:OFFSet"
    trig_src_cmd = ":TRIGger:EDGE:SOURce"
    trig_lev_cmd = ":TRIGger:EDGE:LEVel"
    trig_slope_cmd = ":TRIGger:EDGE:SLOPe"
    trig_coup_cmd = ":TRIGger:COUPling"
    status_cmd = ":TRIGger:STATus?"
    acq_count_cmd = ":ACQuire:AVERages"
    scale_write_excludes_probe = False     # UNI-T 怪癖 1
    offset_is_center = False               # Keysight：OFFSet = 屏幕中心对应的电压（与“位置”符号相反）
    # SCPI 值 <-> 标准代码
    SRC = {"CH1": "CHANnel1", "CH2": "CHANnel2", "CH3": "CHANnel3", "CH4": "CHANnel4",
           "EXT": "EXT", "EXT5": "EXT5", "AC": "ACLine"}
    SLOPE = {"POS": "POSitive", "NEG": "NEGative", "ALT": "RFALl"}
    SWEEP = {"AUTO": "AUTO", "NORM": "NORMal", "SING": "SINGle"}
    TCOUP = {"DC": "DC", "AC": "AC", "LF": "LFReject", "HF": "HFReject", "NOISE": "NOISE"}
    ACQ = {"NORM": "NORMal", "AVER": "AVERages", "PEAK": "PEAK", "HRES": "HRESolution"}
    COUP = {"DC": "DC", "AC": "AC", "GND": "GND"}
    STATUS = {"TD": "TRIGED", "TRIGED": "TRIGED", "WAIT": "WAIT", "RUN": "RUN", "AUTO": "AUTO",
              "STOP": "STOP", "READY": "READY", "ARMED": "READY", "SCAN": "RUN", "SING": "READY"}

    @staticmethod
    def code_of(table, value, default=None):
        v = (value or "").strip().upper()
        if not v:
            return default
        for code, scpi in table.items():
            short = "".join(c for c in scpi if c.isupper() or c.isdigit())
            if v in (scpi.upper(), short.upper()):
                return code
        for code, scpi in table.items():
            if scpi.upper().startswith(v) or v.startswith(scpi.upper()):
                return code
        return default

    def probe_factor(self, ch):
        return to_float(self.qstr(f":CHANnel{ch}:PROBe?")) or 1.0

    # ----- 读 -----
    def timebase(self):
        return self.qfloat(f"{self.tb_scale_cmd}?") or 1e-3

    def read_scale(self, ch):
        return self.qfloat(f":CHANnel{ch}:SCALe?") or 1.0

    def ch_state(self, ch):
        scale = self.read_scale(ch)
        off = self.qfloat(f":CHANnel{ch}:OFFSet?") or 0.0
        return {"on": self.qstr(f":CHANnel{ch}:DISPlay?") in ("1", "ON"),
                "scale": scale,
                "pos": -off if self.offset_is_center else off,
                "probe": self.probe_factor(ch),
                "coup": self.code_of(self.COUP, self.qstr(f":CHANnel{ch}:COUPling?"), "DC"),
                "bw": self.qstr(f":CHANnel{ch}:BWLimit?").upper() not in ("0", "OFF", ""),
                "inv": self.qstr(f":CHANnel{ch}:INVert?") in ("1", "ON")}

    def counter(self):
        return None

    def state(self):
        status = self.qstr(self.status_cmd, "?")
        s = {"status": self.STATUS.get(status.upper(), status),
             "tb": self.timebase(),
             "tbofs": self.qfloat(f"{self.tb_offset_cmd}?") or 0.0,
             "trig_src": self.code_of(self.SRC, self.qstr(f"{self.trig_src_cmd}?"), "CH1"),
             "trig_lev": self.qfloat(f"{self.trig_lev_cmd}?") or 0.0,
             "trig_slope": self.code_of(self.SLOPE, self.qstr(f"{self.trig_slope_cmd}?"), "POS"),
             "trig_sweep": self.code_of(self.SWEEP, self.qstr(":TRIGger:SWEep?"), "AUTO"),
             "trig_coup": self.code_of(self.TCOUP, self.qstr(f"{self.trig_coup_cmd}?"), "DC"),
             "acq": self.code_of(self.ACQ, self.qstr(":ACQuire:TYPE?"), "NORM"),
             "avg": self.qstr(f"{self.acq_count_cmd}?"),
             "counter": self.counter()}
        for ch in range(1, self.channels + 1):
            s[ch] = self.ch_state(ch)
        return s

    # ----- 写 -----
    def set_ch_on(self, ch, on):
        self.write(f":CHANnel{ch}:DISPlay {'ON' if on else 'OFF'}")

    def set_ch_scale(self, ch, volts):
        v = volts / self.probe_factor(ch) if self.scale_write_excludes_probe else volts
        self.write(f":CHANnel{ch}:SCALe {v:.4e}")

    def set_ch_pos(self, ch, volts):
        self.write(f":CHANnel{ch}:OFFSet {(-volts if self.offset_is_center else volts):.4e}")

    def set_ch_coupling(self, ch, code):
        self.write(f":CHANnel{ch}:COUPling {self.COUP[code]}")

    def set_ch_probe(self, ch, factor):
        self.write(f":CHANnel{ch}:PROBe {factor:g}")

    def set_ch_bw(self, ch, on):
        self.write(f":CHANnel{ch}:BWLimit {'20M' if on else 'OFF'}")

    def set_ch_invert(self, ch, on):
        self.write(f":CHANnel{ch}:INVert {'ON' if on else 'OFF'}")

    def set_timebase(self, sec):
        self.write(f"{self.tb_scale_cmd} {sec:.4e}")

    def set_tb_offset(self, sec):
        self.write(f"{self.tb_offset_cmd} {sec:.4e}")

    def set_trig_source(self, code):
        self.write(":TRIGger:MODE EDGE")
        self.write(f"{self.trig_src_cmd} {self.SRC[code]}")

    def set_trig_level(self, volts):
        self.write(f"{self.trig_lev_cmd} {volts:.4e}")

    def set_trig_slope(self, code):
        self.write(f"{self.trig_slope_cmd} {self.SLOPE[code]}")

    def set_trig_sweep(self, code):
        if code == "SING":
            self.single()
        else:
            self.write(f":TRIGger:SWEep {self.SWEEP[code]}")
            self.run()            # 从“单次”切回自动/正常时，示波器可能停着，顺便让它运行

    def set_trig_coupling(self, code):
        self.write(f"{self.trig_coup_cmd} {self.TCOUP[code]}")

    def set_acq_type(self, code):
        self.write(f":ACQuire:TYPE {self.ACQ[code]}")

    def set_avg_count(self, n):
        self.write(f"{self.acq_count_cmd} {int(n)}")

    def run(self):
        self.write(":RUN")

    def stop(self):
        self.write(":STOP")

    def single(self):
        self.write(":SINGle")

    def auto(self):
        self.write(":AUToscale")

    # ----- 波形：读取 IEEE 488.2 定长数据块 -----
    def _read_block(self):
        buf = b""
        while len(buf) < 2 or buf[:1] != b"#":
            buf += self.inst.read_raw()
            if buf and buf[:1] != b"#":
                buf = buf[buf.find(b"#"):] if b"#" in buf else b""
        n = int(buf[1:2])
        while len(buf) < 2 + n:
            buf += self.inst.read_raw()
        length = int(buf[2:2 + n])
        start = 2 + n
        while len(buf) < start + length:
            buf += self.inst.read_raw()
        return buf[start:start + length]


# =============================================================================
# UNI-T UTD2000 系列 —— 已实测（UTD2202CEX+，固件 1.06.0043）
# =============================================================================
class UnitUTD(_ScpiStyle):
    family = "UNI-T UTD2000 系列"
    tested_re = r"UNI-T.*,\s*DSO\s*,"
    hdiv = 16
    colors = {1: "#00c8e6", 2: "#f2d200", 3: "#ff4fd8", 4: "#3d7bff"}   # 和这台示波器屏幕一致
    trig_extra = ["EXT", "EXT5", "AC"]
    trig_coups = ["DC", "AC", "LF", "HF", "NOISE"]
    acq_types = ["NORM", "AVER", "PEAK", "HRES"]
    tb_offset_cmd = ":TIMebase:OFFSet"
    trig_src_cmd = ":TRIGger:SOURce"
    trig_lev_cmd = ":TRIGger:LEVel"
    acq_count_cmd = ":ACQuire:AVERages:COUNt"
    scale_write_excludes_probe = True
    SLOPE = {"POS": "POSitive", "NEG": "NEGative", "ALT": "ALTernation"}
    ACQ = {"NORM": "NORMal", "AVER": "AVERage", "PEAK": "PEAKdetect", "HRES": "HRESolution"}
    TCOUP = {"DC": "DC", "AC": "AC", "LF": "LF", "HF": "HF", "NOISE": "NOISE"}
    MEASCMD = {"FREQ": "FREQuency", "PER": "PERiod", "VPP": "VPP", "VMAX": "VMAX", "VMIN": "VMIN",
               "VAVG": "VAVerage", "VRMS": "VRMS", "VAMP": "VAMP", "VTOP": "VTOP", "VBASE": "VBASe",
               "PWID": "PWIDth", "NWID": "NWIDth", "PDUT": "PDUTy", "RISE": "RISetime", "FALL": "FALLtime",
               "OVER": "OVERshoot"}

    def __init__(self, inst, idn):
        super().__init__(inst, idn)
        self._scale_cache = {}
        self._sel = None

    @classmethod
    def match(cls, idn):
        return bool(re.search(r"UNI-?T", idn, re.I) and re.search(r",\s*(DSO|UTD)", idn, re.I))

    # 怪癖 5（实测）：查询 :CHANnel<n>:SCALe? 会让示波器面板“选中”该通道（CH 灯亮、旋钮改调它）。
    # 每次刷新都依次查 CH1、CH2，面板就总被拽到 CH2 —— 用户在面板上选 CH1 后，旋钮仍在调 CH2。
    # 解决：只直接查询“面板当前选中”的那个通道；其他通道的 V/格 用波形数据算（V/格 = yinc × 25，已实测完全一致），
    # 读波形不会改变选中状态。:CHANnel<n>:SELect? 查询本身也不会改变选中。
    def selected(self):
        for ch in range(1, self.channels + 1):
            if self.qstr(f":CHANnel{ch}:SELect?") == "1":
                return ch
        return None

    def read_scale(self, ch):
        if ch == self._sel or ch not in self._scale_cache:
            v = self.qfloat(f":CHANnel{ch}:SCALe?")
            if v:
                self._scale_cache[ch] = v
            if self._sel and ch != self._sel:              # 只在第一次没有缓存时发生：查完马上把选中还给原通道
                self.qstr(f":CHANnel{self._sel}:SCALe?")
            return v or 1.0
        return self._scale_cache[ch]

    def state(self):
        self._sel = self.selected()
        try:
            return super().state()
        finally:
            self._sel = None

    def detect_channels(self):
        m = re.search(r"UTD\d*?(\d)\d{2}", self.model.upper())
        return int(m.group(1)) if (m and m.group(1) in "24") else 2

    def probe_factor(self, ch):
        if ch not in self._probe_cache or time.time() - self._probe_cache[ch][1] > 1.0:
            self._probe_cache[ch] = (to_float(self.qstr(f":CHANnel{ch}:PROBe?")) or 1.0, time.time())
        return self._probe_cache[ch][0]

    def set_ch_probe(self, ch, factor):
        self.write(f":CHANnel{ch}:PROBe {factor:g}X")
        self._probe_cache.pop(ch, None)

    def set_ch_bw(self, ch, on):
        self.write(f":CHANnel{ch}:BWLimit {'ON' if on else 'OFF'}")

    def step_ch_scale(self, ch, up):
        self.write(f":CHANnel{ch}:SCALe {'UP' if up else 'DOWN'}")

    def step_timebase(self, up):
        self.write(f":TIMebase:SCALe {'UP' if up else 'DOWN'}")

    def set_trig_source(self, code):
        self.write(":TRIGger:MODE EDGE")
        self.write(f":TRIGger:SOURce {self.SRC[code]}")

    def single(self):
        self.write(":TRIGger:SWEep SINGle")
        self.write(":RUN")

    def auto(self):
        self.write(":AUTO")

    def trig_50(self):
        self.write(":TRIGger:LEVel:ASETup")

    def counter(self):
        if self.qstr(":SYSTem:CYMOmeter?") in ("1", "ON"):
            return self.qfloat(":SYSTem:CYMOmeter:FREQuency?")
        return None

    def measure(self, code, ch):
        return self.qfloat(f":MEASure:{self.MEASCMD[code]}? CHANnel{ch}")

    def measure_pair(self):
        return (self.qfloat(":MEASure:PHASe? CHANnel1,CHANnel2"),
                self.qfloat(":MEASure:PDELay? CHANnel1,CHANnel2"))

    def waveform(self, ch):
        self.write(f":WAVeform:SOURce CHANnel{ch}")
        pre = self.query(":WAVeform:PREamble?").split(",")
        xinc, xorg, xref = float(pre[4]), float(pre[5]), float(pre[6])
        yinc, yorg, yref = float(pre[7]), float(pre[8]), float(pre[9])
        self._scale_cache[ch] = float(f"{yinc * 25:.6g}")      # 怪癖 5：顺便得到 V/格，且不改变面板选中
        self.inst.write(":WAVeform:DATA?")
        raw = self._read_block()
        if pre[0].strip().upper().startswith("W"):
            codes = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<u2").astype(float)
        else:
            codes = np.frombuffer(raw, dtype="u1").astype(float)
        v = (codes - yref) * yinc - yorg
        points = int(float(pre[2]))
        step = xinc * points / len(codes) if 0 < points < len(codes) else xinc     # 怪癖 2
        t = (np.arange(len(codes)) - xref) * step + xorg
        return t, v


# =============================================================================
# Rigol DS/MSO/DHO —— 未实测（按 DS1000Z / MSO5000 编程手册编写）；UNI-T UPO/MSO 命令与之相近
# =============================================================================
class RigolDS(_ScpiStyle):
    family = "Rigol DS/MSO/DHO 系列"
    hdiv = 12
    tb_scale_cmd = ":TIMebase:MAIN:SCALe"
    tb_offset_cmd = ":TIMebase:MAIN:OFFSet"
    trig_coups = ["DC", "AC", "LF", "HF"]
    acq_types = ["NORM", "AVER", "PEAK", "HRES"]
    ITEM = {"FREQ": "FREQuency", "PER": "PERiod", "VPP": "VPP", "VMAX": "VMAX", "VMIN": "VMIN", "VAVG": "VAVG",
            "VRMS": "VRMS", "VAMP": "VAMP", "VTOP": "VTOP", "VBASE": "VBASe", "PWID": "PWIDth", "NWID": "NWIDth",
            "PDUT": "PDUTy", "RISE": "RTIMe", "FALL": "FTIMe", "OVER": "OVERshoot"}

    @classmethod
    def match(cls, idn):
        return bool((re.search(r"RIGOL", idn, re.I) and re.search(r",\s*(DS|MSO|DHO)", idn, re.I))
                    or (re.search(r"UNI-?T", idn, re.I) and re.search(r",\s*(UPO|MSO)", idn, re.I)))

    def init(self):
        for c in (":WAVeform:MODE NORMal", ":WAVeform:FORMat BYTE"):
            try:
                self.write(c)
            except Exception:
                self.recover()

    def set_ch_bw(self, ch, on):
        self.write(f":CHANnel{ch}:BWLimit {'20M' if on else 'OFF'}")

    def measure(self, code, ch):
        return self.qfloat(f":MEASure:ITEM? {self.ITEM[code]},CHANnel{ch}")

    def measure_pair(self):
        return (self.qfloat(":MEASure:ITEM? RPHase,CHANnel1,CHANnel2"),
                self.qfloat(":MEASure:ITEM? RDELay,CHANnel1,CHANnel2"))

    def counter(self):
        return self.qfloat(":MEASure:COUNter:VALue?") if self.qstr(":MEASure:COUNter:SOURce?", "OFF").upper() != "OFF" else None

    def waveform(self, ch):
        self.write(f":WAVeform:SOURce CHANnel{ch}")
        pre = [float(x) for x in self.query(":WAVeform:PREamble?").split(",")]
        xinc, xorg, xref, yinc, yorg, yref = pre[4], pre[5], pre[6], pre[7], pre[8], pre[9]
        self.inst.write(":WAVeform:DATA?")
        codes = np.frombuffer(self._read_block(), dtype="u1").astype(float)
        v = (codes - yorg - yref) * yinc
        t = (np.arange(len(codes)) - xref) * xinc + xorg
        return t, v


# =============================================================================
# Keysight / Agilent InfiniiVision —— 未实测（按 InfiniiVision 2000X/3000T 编程手册编写）
# =============================================================================
class KeysightIV(_ScpiStyle):
    family = "Keysight InfiniiVision 系列"
    hdiv = 10
    tb_offset_cmd = ":TIMebase:POSition"
    trig_coup_cmd = ":TRIGger:EDGE:COUPling"
    offset_is_center = True
    couplings = ["DC", "AC"]
    trig_extra = ["EXT", "AC"]
    trig_coups = ["DC", "AC", "LF"]
    sweeps = ["AUTO", "NORM", "SING"]
    acq_types = ["NORM", "AVER", "PEAK", "HRES"]
    acq_count_cmd = ":ACQuire:COUNt"
    SRC = {"CH1": "CHANnel1", "CH2": "CHANnel2", "CH3": "CHANnel3", "CH4": "CHANnel4",
           "EXT": "EXTernal", "AC": "LINE"}
    SLOPE = {"POS": "POSitive", "NEG": "NEGative", "ALT": "EITHer"}
    ACQ = {"NORM": "NORMal", "AVER": "AVERage", "PEAK": "PEAK", "HRES": "HRESolution"}
    COUP = {"DC": "DC", "AC": "AC"}
    MEASCMD = {"FREQ": "FREQuency? {s}", "PER": "PERiod? {s}", "VPP": "VPP? {s}", "VMAX": "VMAX? {s}",
               "VMIN": "VMIN? {s}", "VAVG": "VAVerage? DISPlay,{s}", "VRMS": "VRMS? DISPlay,DC,{s}",
               "VAMP": "VAMPlitude? {s}", "VTOP": "VTOP? {s}", "VBASE": "VBASe? {s}", "PWID": "PWIDth? {s}",
               "NWID": "NWIDth? {s}", "PDUT": "DUTYcycle? {s}", "RISE": "RISetime? {s}", "FALL": "FALLtime? {s}",
               "OVER": "OVERshoot? {s}"}

    @classmethod
    def match(cls, idn):
        return bool(re.search(r"(KEYSIGHT|AGILENT)", idn, re.I)
                    and re.search(r",\s*(DSO|MSO|EDU)[-X]", idn, re.I))

    def init(self):
        for c in (":WAVeform:FORMat BYTE", ":WAVeform:UNSigned 1", ":WAVeform:POINts:MODE NORMal",
                  ":WAVeform:POINts 1000"):
            try:
                self.write(c)
            except Exception:
                self.recover()

    def state(self):
        s = super().state()
        r = self.qstr(":RSTate?", "").upper()          # RUN / STOP / SING
        if r:
            s["status"] = {"RUN": "RUN", "STOP": "STOP", "SING": "READY"}.get(r, r)
        if s["trig_sweep"] != "NORM" and r == "SING":
            s["trig_sweep"] = "SING"
        return s

    def set_ch_bw(self, ch, on):
        self.write(f":CHANnel{ch}:BWLimit {1 if on else 0}")

    def set_trig_coupling(self, code):
        if code == "HF":
            self.write(":TRIGger:HFReject 1")
        else:
            self.write(":TRIGger:HFReject 0")
            self.write(f":TRIGger:EDGE:COUPling {'LFReject' if code == 'LF' else code}")

    def trig_50(self):
        self.write(":TRIGger:LEVel:ASETup")

    def measure(self, code, ch):
        return self.qfloat(":MEASure:" + self.MEASCMD[code].format(s=f"CHANnel{ch}"))

    def measure_pair(self):
        return (self.qfloat(":MEASure:PHASe? CHANnel1,CHANnel2"),
                self.qfloat(":MEASure:DELay? CHANnel1,CHANnel2"))

    def waveform(self, ch):
        self.write(f":WAVeform:SOURce CHANnel{ch}")
        pre = [float(x) for x in self.query(":WAVeform:PREamble?").split(",")]
        xinc, xorg, xref, yinc, yorg, yref = pre[4], pre[5], pre[6], pre[7], pre[8], pre[9]
        self.inst.write(":WAVeform:DATA?")
        codes = np.frombuffer(self._read_block(), dtype="u1").astype(float)
        v = (codes - yref) * yinc + yorg
        t = (np.arange(len(codes)) - xref) * xinc + xorg
        return t, v


# =============================================================================
# Siglent SDS 经典命令集（SDS1000X-E / SDS1000CML / SDS2000X 等）—— 未实测
# =============================================================================
class SiglentSDS(ScopeDriver):
    family = "Siglent SDS 系列（经典命令集）"
    hdiv = 14
    trig_extra = ["EXT", "EXT5", "AC"]
    trig_coups = ["DC", "AC", "LF", "HF"]
    acq_types = ["NORM", "AVER", "PEAK", "HRES"]
    has_pair = False
    err_cmd = None
    PAVA = {"FREQ": "FREQ", "PER": "PER", "VPP": "PKPK", "VMAX": "MAX", "VMIN": "MIN", "VAVG": "MEAN",
            "VRMS": "RMS", "VAMP": "AMPL", "VTOP": "TOP", "VBASE": "BASE", "PWID": "PWID", "NWID": "NWID",
            "PDUT": "DUTY", "RISE": "RISE", "FALL": "FALL", "OVER": "OVSP"}
    SRC = {"CH1": "C1", "CH2": "C2", "CH3": "C3", "CH4": "C4", "EXT": "EX", "EXT5": "EX5", "AC": "LINE"}
    COUP = {"DC": "D1M", "AC": "A1M", "GND": "GND"}
    TCOUP = {"DC": "DC", "AC": "AC", "LF": "LFREJ", "HF": "HFREJ"}

    @classmethod
    def match(cls, idn):
        return bool(re.search(r"SIGLENT", idn, re.I) and re.search(r",\s*SDS", idn, re.I))

    def h(self, cmd):
        """查询并去掉返回值的命令头"""
        return after_header(self.qstr(cmd))

    def timebase(self):
        return to_float(self.h("TDIV?")) or 1e-3

    def ch_state(self, ch):
        coup = self.h(f"C{ch}:CPL?").upper()
        return {"on": self.h(f"C{ch}:TRA?").upper() == "ON",
                "scale": to_float(self.h(f"C{ch}:VDIV?")) or 1.0,
                "pos": to_float(self.h(f"C{ch}:OFST?")) or 0.0,
                "probe": to_float(self.h(f"C{ch}:ATTN?")) or 1.0,
                "coup": "GND" if coup.startswith("GND") else ("AC" if coup.startswith("A") else "DC"),
                "bw": f"C{ch},ON" in self.h("BWL?").upper().replace(" ", ""),
                "inv": self.h(f"C{ch}:INVS?").upper() == "ON"}

    def state(self):
        trse = self.h("TRSE?").split(",")
        src = trse[2].strip().upper() if len(trse) > 2 else "C1"
        sast = self.h("SAST?").upper()
        s = {"status": "STOP" if "STOP" in sast else ("TRIGED" if "TRIG" in sast else
                                                     ("AUTO" if "AUTO" in sast else "READY")),
             "tb": self.timebase(),
             "tbofs": to_float(self.h("TRDL?")) or 0.0,
             "trig_src": next((k for k, v in self.SRC.items() if v == src), "CH1"),
             "trig_lev": to_float(self.h(f"{src}:TRLV?")) or 0.0,
             "trig_slope": {"POS": "POS", "NEG": "NEG", "WINDOW": "ALT"}.get(self.h(f"{src}:TRSL?").upper(), "POS"),
             "trig_sweep": {"AUTO": "AUTO", "NORM": "NORM", "SINGLE": "SING", "STOP": "SING"}.get(
                 self.h("TRMD?").upper(), "AUTO"),
             "trig_coup": next((k for k, v in self.TCOUP.items() if v == self.h(f"{src}:TRCP?").upper()), "DC"),
             "acq": "NORM", "avg": "", "counter": None}
        acqw = self.h("ACQW?").upper()
        s["acq"] = "AVER" if "AVER" in acqw else ("PEAK" if "PEAK" in acqw else ("HRES" if "HIGH" in acqw else "NORM"))
        if "AVER" in acqw and "," in acqw:
            s["avg"] = acqw.split(",")[1]
        for ch in range(1, self.channels + 1):
            s[ch] = self.ch_state(ch)
        return s

    def _src(self):
        trse = self.h("TRSE?").split(",")
        return trse[2].strip() if len(trse) > 2 else "C1"

    def run(self):
        self.write("TRMD AUTO")

    def stop(self):
        self.write("STOP")

    def single(self):
        self.write("TRMD SINGLE")

    def auto(self):
        self.write("ASET")

    def trig_50(self):
        self.write("SET50")

    def set_ch_on(self, ch, on):
        self.write(f"C{ch}:TRA {'ON' if on else 'OFF'}")

    def set_ch_scale(self, ch, volts):
        self.write(f"C{ch}:VDIV {volts:.4e}V")

    def set_ch_pos(self, ch, volts):
        self.write(f"C{ch}:OFST {volts:.4e}V")

    def set_ch_coupling(self, ch, code):
        self.write(f"C{ch}:CPL {self.COUP[code]}")

    def set_ch_probe(self, ch, factor):
        self.write(f"C{ch}:ATTN {factor:g}")

    def set_ch_bw(self, ch, on):
        self.write(f"BWL C{ch},{'ON' if on else 'OFF'}")

    def set_ch_invert(self, ch, on):
        self.write(f"C{ch}:INVS {'ON' if on else 'OFF'}")

    def set_timebase(self, sec):
        self.write(f"TDIV {sec:.4e}S")

    def set_tb_offset(self, sec):
        self.write(f"TRDL {sec:.4e}S")

    def set_trig_source(self, code):
        self.write(f"TRSE EDGE,SR,{self.SRC[code]},HT,OFF")

    def set_trig_level(self, volts):
        self.write(f"{self._src()}:TRLV {volts:.4e}V")

    def set_trig_slope(self, code):
        self.write(f"{self._src()}:TRSL " + {"POS": "POS", "NEG": "NEG", "ALT": "WINDOW"}[code])

    def set_trig_sweep(self, code):
        self.write("TRMD " + {"AUTO": "AUTO", "NORM": "NORM", "SING": "SINGLE"}[code])

    def set_trig_coupling(self, code):
        self.write(f"{self._src()}:TRCP {self.TCOUP[code]}")

    def set_acq_type(self, code):
        self.write("ACQW " + {"NORM": "SAMPLING", "AVER": "AVERAGE,16", "PEAK": "PEAK_DETECT", "HRES": "HIGH_RES"}[code])

    def set_avg_count(self, n):
        self.write(f"ACQW AVERAGE,{int(n)}")

    def measure(self, code, ch):
        r = self.qstr(f"C{ch}:PAVA? {self.PAVA[code]}")
        return to_float(r.split(",", 1)[1]) if "," in r else None

    def waveform(self, ch):
        st = self.ch_state(ch)
        tdiv, sara = self.timebase(), to_float(self.h("SARA?")) or 1e6
        trdl = to_float(self.h("TRDL?")) or 0.0
        total = sara * tdiv * self.hdiv
        sp = max(1, int(total // 2000))
        self.write(f"WFSU SP,{sp},NP,0,FP,0")
        self.inst.write(f"C{ch}:WF? DAT2")
        codes = np.frombuffer(self._read_block(), dtype="i1").astype(float)
        v = codes * (st["scale"] / 25.0) - st["pos"]
        t = -(tdiv * self.hdiv / 2) - trdl + np.arange(len(codes)) * sp / sara
        return t, v

    _read_block = _ScpiStyle._read_block


# =============================================================================
# Tektronix TBS/TDS/MDO/MSO/DPO —— 未实测（按 TBS1000B/2000 与 MDO3000 编程手册编写）
# =============================================================================
class TektronixTBS(ScopeDriver):
    family = "Tektronix TBS/MDO/MSO 系列"
    hdiv = 10
    trig_extra = ["EXT", "EXT5", "AC"]
    slopes = ["POS", "NEG"]
    sweeps = ["AUTO", "NORM", "SING"]
    trig_coups = ["DC", "AC", "LF", "HF", "NOISE"]
    acq_types = ["NORM", "AVER", "PEAK", "HRES"]
    avg_counts = [4, 16, 64, 128]
    err_cmd = None
    IMM = {"FREQ": "FREQuency", "PER": "PERIod", "VPP": "PK2pk", "VMAX": "MAXImum", "VMIN": "MINImum",
           "VAVG": "MEAN", "VRMS": "RMS", "VAMP": "AMPlitude", "VTOP": "HIGH", "VBASE": "LOW", "PWID": "PWIdth",
           "NWID": "NWIdth", "PDUT": "PDUty", "RISE": "RISe", "FALL": "FALL", "OVER": "POVershoot"}
    SRC = {"CH1": "CH1", "CH2": "CH2", "CH3": "CH3", "CH4": "CH4", "EXT": "EXT", "EXT5": "EXT5", "AC": "LINE"}
    TCOUP = {"DC": "DC", "AC": "AC", "LF": "LFRej", "HF": "HFRej", "NOISE": "NOISErej"}
    ACQ = {"NORM": "SAMple", "AVER": "AVErage", "PEAK": "PEAKdetect", "HRES": "HIRes"}

    @classmethod
    def match(cls, idn):
        return bool(re.search(r"TEKTRONIX", idn, re.I) and re.search(r",\s*(TBS|TDS|MDO|MSO|DPO)", idn, re.I))

    def init(self):
        self.write("HEADer OFF")
        for c in ("DATa:ENCdg RIBinary", "DATa:WIDth 1", "DATa:STARt 1", "DATa:STOP 10000"):
            self.write(c)
        self.lev_cmd = "TRIGger:A:LEVel" if self.try_query("TRIGger:A:LEVel?") is not None else None
        self.pre = "WFMOutpre" if self.try_query("WFMOutpre:YMUlt?") is not None else "WFMPre"

    def timebase(self):
        return self.qfloat("HORizontal:MAIn:SCAle?") or self.qfloat("HORizontal:SCAle?") or 1e-3

    def ch_state(self, ch):
        scale = self.qfloat(f"CH{ch}:SCAle?") or 1.0
        coup = self.qstr(f"CH{ch}:COUPling?").upper()
        return {"on": self.qstr(f"SELect:CH{ch}?") in ("1", "ON"),
                "scale": scale,
                "pos": (self.qfloat(f"CH{ch}:POSition?") or 0.0) * scale,     # Tek 的位置以“格”为单位
                "probe": self.qfloat(f"CH{ch}:PRObe?") or (1 / (self.qfloat(f"CH{ch}:PRObe:GAIN?") or 1)),
                "coup": "GND" if coup.startswith("GND") else ("AC" if coup.startswith("AC") else "DC"),
                "bw": not self.qstr(f"CH{ch}:BANdwidth?").upper().startswith("FUL"),
                "inv": self.qstr(f"CH{ch}:INVert?") in ("1", "ON")}

    def state(self):
        stt = self.qstr("TRIGger:STATE?").upper()
        stop_after = self.qstr("ACQuire:STOPAfter?").upper()
        src = self.qstr("TRIGger:A:EDGE:SOUrce?").upper()
        lev = self.qfloat(f"{self.lev_cmd}?") if self.lev_cmd else self.qfloat(f"TRIGger:A:LEVel:{src}?")
        acq = self.qstr("ACQuire:MODe?").upper()
        s = {"status": {"TRIGGER": "TRIGED", "AUTO": "AUTO", "READY": "READY", "ARMED": "READY",
                        "SAVE": "STOP"}.get(stt, stt or "?"),
             "tb": self.timebase(),
             "tbofs": self.qfloat("HORizontal:MAIn:POSition?") or self.qfloat("HORizontal:DELay:TIMe?") or 0.0,
             "trig_src": next((k for k, v in self.SRC.items() if v.upper() == src), "CH1"),
             "trig_lev": lev or 0.0,
             "trig_slope": "NEG" if self.qstr("TRIGger:A:EDGE:SLOpe?").upper().startswith("FALL") else "POS",
             "trig_sweep": "SING" if stop_after.startswith("SEQ") else
                           ("NORM" if self.qstr("TRIGger:A:MODe?").upper().startswith("NORM") else "AUTO"),
             "trig_coup": next((k for k, v in self.TCOUP.items()
                                if self.qstr("TRIGger:A:EDGE:COUPling?").upper().startswith(v.upper()[:3])), "DC"),
             "acq": next((k for k, v in self.ACQ.items() if acq.startswith(v.upper()[:3])), "NORM"),
             "avg": self.qstr("ACQuire:NUMAVg?"),
             "counter": None}
        for ch in range(1, self.channels + 1):
            s[ch] = self.ch_state(ch)
        return s

    def run(self):
        self.write("ACQuire:STOPAfter RUNSTop")
        self.write("ACQuire:STATE RUN")

    def stop(self):
        self.write("ACQuire:STATE STOP")

    def single(self):
        self.write("ACQuire:STOPAfter SEQuence")
        self.write("ACQuire:STATE RUN")

    def auto(self):
        self.write("AUTOSet EXECute")

    def trig_50(self):
        self.write("TRIGger:A SETLevel")

    def set_ch_on(self, ch, on):
        self.write(f"SELect:CH{ch} {'ON' if on else 'OFF'}")

    def set_ch_scale(self, ch, volts):
        pos = self.ch_state(ch)["pos"]
        self.write(f"CH{ch}:SCAle {volts:.4e}")
        self.write(f"CH{ch}:POSition {pos / volts:.4f}")      # 保持“伏”位置不变

    def set_ch_pos(self, ch, volts):
        self.write(f"CH{ch}:POSition {volts / (self.qfloat(f'CH{ch}:SCAle?') or 1.0):.4f}")

    def set_ch_coupling(self, ch, code):
        self.write(f"CH{ch}:COUPling {code}")

    def set_ch_probe(self, ch, factor):
        if self.try_query(f"CH{ch}:PRObe?") is not None:
            self.write(f"CH{ch}:PRObe {factor:g}")
        else:
            self.write(f"CH{ch}:PRObe:GAIN {1 / factor:g}")

    def set_ch_bw(self, ch, on):
        self.write(f"CH{ch}:BANdwidth {'TWEnty' if on else 'FULl'}")

    def set_ch_invert(self, ch, on):
        self.write(f"CH{ch}:INVert {'ON' if on else 'OFF'}")

    def set_timebase(self, sec):
        self.write(f"HORizontal:MAIn:SCAle {sec:.4e}")

    def set_tb_offset(self, sec):
        self.write(f"HORizontal:MAIn:POSition {sec:.4e}")

    def set_trig_source(self, code):
        self.write("TRIGger:A:TYPe EDGe")
        self.write(f"TRIGger:A:EDGE:SOUrce {self.SRC[code]}")

    def set_trig_level(self, volts):
        if self.lev_cmd:
            self.write(f"{self.lev_cmd} {volts:.4e}")
        else:
            src = self.qstr("TRIGger:A:EDGE:SOUrce?")
            self.write(f"TRIGger:A:LEVel:{src} {volts:.4e}")

    def set_trig_slope(self, code):
        self.write(f"TRIGger:A:EDGE:SLOpe {'FALL' if code == 'NEG' else 'RISe'}")

    def set_trig_sweep(self, code):
        if code == "SING":
            self.single()
        else:
            self.write(f"TRIGger:A:MODe {'NORMal' if code == 'NORM' else 'AUTO'}")
            self.run()

    def set_trig_coupling(self, code):
        self.write(f"TRIGger:A:EDGE:COUPling {self.TCOUP[code]}")

    def set_acq_type(self, code):
        self.write(f"ACQuire:MODe {self.ACQ[code]}")

    def set_avg_count(self, n):
        self.write(f"ACQuire:NUMAVg {int(n)}")

    def measure(self, code, ch):
        self.write(f"MEASUrement:IMMed:SOUrce1 CH{ch}")
        self.write(f"MEASUrement:IMMed:TYPe {self.IMM[code]}")
        return self.qfloat("MEASUrement:IMMed:VALue?")

    def measure_pair(self):
        out = []
        for t in ("PHAse", "DELay"):
            self.write("MEASUrement:IMMed:SOUrce1 CH1")
            self.write("MEASUrement:IMMed:SOUrce2 CH2")
            self.write(f"MEASUrement:IMMed:TYPe {t}")
            out.append(self.qfloat("MEASUrement:IMMed:VALue?"))
        return tuple(out)

    def waveform(self, ch):
        self.write(f"DATa:SOUrce CH{ch}")
        p = self.pre
        ymult, yoff, yzero = (self.qfloat(f"{p}:YMUlt?"), self.qfloat(f"{p}:YOFf?"), self.qfloat(f"{p}:YZEro?") or 0.0)
        xincr, xzero = self.qfloat(f"{p}:XINcr?"), self.qfloat(f"{p}:XZEro?") or 0.0
        self.inst.write("CURVe?")
        codes = np.frombuffer(_ScpiStyle._read_block(self), dtype="i1").astype(float)
        v = (codes - (yoff or 0.0)) * (ymult or 1.0) + yzero
        t = xzero + np.arange(len(codes)) * (xincr or 1e-6)
        return t, v


# =============================================================================
# 通用兼容模式 —— 其他品牌（GW Instek、Hantek、OWON 等），按最常见的 SCPI 写法；未实测
# =============================================================================
class GenericSCPI(KeysightIV):
    family = "通用 SCPI（兼容模式）"
    offset_is_center = False
    couplings = ["DC", "AC", "GND"]
    COUP = {"DC": "DC", "AC": "AC", "GND": "GND"}
    tb_offset_cmd = ":TIMebase:OFFSet"

    def waveform(self, ch):
        self.write(f":WAVeform:SOURce CHANnel{ch}")
        pre = [to_float(x) or 0.0 for x in self.query(":WAVeform:PREamble?").split(",")]
        xinc, xorg, xref, yinc, yorg, yref = pre[4], pre[5], pre[6], pre[7], pre[8], pre[9]
        self.inst.write(":WAVeform:DATA?")
        codes = np.frombuffer(self._read_block(), dtype="u1").astype(float)
        v = (codes - yref) * yinc + yorg
        t = (np.arange(len(codes)) - xref) * xinc + xorg
        return t, v


DRIVERS = [UnitUTD, RigolDS, KeysightIV, SiglentSDS, TektronixTBS]


def pick_driver(idn):
    for cls in DRIVERS:
        if cls.match(idn):
            return cls
    return GenericSCPI if SCOPE_HINT.search(idn) else None


def is_scope(idn):
    return pick_driver(idn) is not None and not gen.pick_driver(idn)


def find_scopes(rm):
    """扫描 USB / 网络上的示波器；返回 [(地址, IDN, 驱动类)]"""
    found = []
    for r in rm.list_resources():
        if not r.upper().startswith(("USB", "TCPIP")):
            continue
        idn = gen.identify(rm, r)          # 问完型号会把信号发生器等仪器交还本地，避免面板被锁成“远程”
        if idn and is_scope(idn):
            found.append((r, idn, pick_driver(idn)))
    return found


def connect(rm, addr, idn, cls):
    inst = rm.open_resource(addr)
    inst.timeout = 5000
    drv = cls(inst, idn)
    drv.init()
    return drv
