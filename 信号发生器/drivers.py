# -*- coding: utf-8 -*-
"""
信号发生器驱动（被 gen_gui.py 使用）

结构：
  Driver            —— 统一接口 + 公共工具（所有驱动都继承它）
  Keysight33500     —— Keysight/Agilent 33500B、33600A 系列      ✅ 已用 33522B 实测
  Keysight33200     —— Keysight/Agilent 33120A/33210A/33220A/33250A   未实测
  RigolDG           —— Rigol DG800/DG900/DG1000Z/DG2000/DG4000/DG5000 未实测
  SiglentSDG        —— 鼎阳 SDG 系列                                未实测
  TektronixAFG      —— 泰克 AFG1000/AFG2000/AFG3000/AFG31000 系列  未实测
  GenericSCPI       —— 兼容模式：其他品牌（GW Instek、UNI-T 等）    未实测

连接时发送 *IDN? 读出厂商和型号，由 pick_driver() 自动选择驱动。

【以后发现某台仪器哪里不对，怎么改】
  1. 在程序“手动命令”框里试出正确的命令（对照该仪器的编程手册）；
  2. 找到下面对应的驱动类，修改 apply / output / read_settings 等函数里的命令；
  3. 改好并实测后，把该类的 tested 改成 True。
"""
import os
import re

import numpy as np

# Python 3.8+ 不再按 PATH 查找 DLL 依赖，需手动加入 Keysight VISA 的 DLL 目录
for _d in (r"C:\Program Files\Keysight\IO Libraries Suite\bin",
           r"C:\Program Files\IVI Foundation\VISA\Win64\Bin",
           r"C:\Program Files\IVI Foundation\VISA\Win64\ktvisa\ktbin"):
    if os.path.isdir(_d):
        os.add_dll_directory(_d)

import pyvisa  # noqa: E402

KTVISA = r"C:\Windows\System32\ktvisa32.dll"

# 内部统一使用的波形代号 → 中文名
WAVE_NAMES = {"SIN": "正弦", "SQU": "方波", "RAMP": "锯齿（可调对称度）", "TRI": "三角", "PULS": "脉冲",
              "NOIS": "噪声", "DC": "直流", "ARB": "任意波"}


class NotSupported(Exception):
    """该仪器（或该驱动）不支持此功能"""


# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
def num(s):
    """从 '1000HZ'、'+1.0E+03'、'INFINITY'、'9.9E37' 之类的回复中取出数字"""
    t = str(s).strip().upper()
    if t.startswith("INF") or t in ("HZ", "HIGHZ"):
        return float("inf")
    m = re.search(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:E[-+]?\d+)?", t)
    if not m:
        raise ValueError(f"无法从“{s}”中解析出数字")
    return float(m.group())


def is_on(s):
    t = str(s).strip().upper()
    return t in ("1", "ON") or t.startswith("ON")


def wave_code(s):
    """把各品牌的波形名称统一成内部代号"""
    t = str(s).strip().strip('"').upper()
    for prefix, code in (("SIN", "SIN"), ("SQU", "SQU"), ("RAMP", "RAMP"), ("TRI", "TRI"), ("PULS", "PULS"),
                         ("NOIS", "NOIS"), ("PRN", "NOIS"), ("DC", "DC"), ("ARB", "ARB"), ("USER", "ARB"),
                         ("EMEM", "ARB"), ("CUST", "ARB")):
        if t.startswith(prefix):
            return code
    return t


def normalize(data, max_points):
    """检查并把数据归一化到 -1 ~ +1；返回 (归一化数据, 最小值, 最大值)"""
    data = np.asarray(data, dtype=float)
    if data.size < 8:
        raise ValueError(f"数据点太少（{data.size} 点），至少需要 8 点。")
    if max_points and data.size > max_points:
        raise ValueError(f"数据点太多（{data.size} 点），这台仪器的驱动最多支持 {max_points} 点。"
                         "可以先对数据降采样，或截取一段。")
    vmin, vmax = float(data.min()), float(data.max())
    if vmax == vmin:
        raise ValueError("数据是一条直线（最大值 = 最小值），无法作为任意波。")
    center, half = (vmax + vmin) / 2, (vmax - vmin) / 2
    return (data - center) / half, vmin, vmax


def last_digit(model):
    m = re.search(r"(\d+)", model)
    return m.group(1)[-1] if m else ""


# ----------------------------------------------------------------------------
# 统一接口
# ----------------------------------------------------------------------------
class Driver:
    family = "通用 SCPI（兼容模式）"
    tested = False
    waves = ("SIN", "SQU", "RAMP", "TRI", "PULS", "NOIS", "DC")   # 支持的波形
    duty_waves = ("SQU", "PULS", "RAMP")                          # 可调占空比 / 对称度的波形
    has_noise_bw = False      # 噪声可设带宽
    has_sum = False           # 叠加（Sum）
    has_arb = False           # 任意波上传
    has_phase_sync = False    # 两通道相位对齐
    max_arb_points = 0
    err_query = "SYST:ERR?"

    def __init__(self, inst, idn):
        self.inst = inst
        self.idn = idn
        parts = [p.strip() for p in idn.split(",")]
        self.vendor = parts[0] if parts else ""
        self.model = parts[1] if len(parts) > 1 else idn
        self._err_ok = True
        self.channels = self.detect_channels()

    @classmethod
    def match(cls, idn):
        return False

    @classmethod
    def is_tested(cls, idn):
        """这个型号是否用真机测试过"""
        return cls.tested

    def detect_channels(self):
        return 2

    def describe(self):
        ok = self.is_tested(self.idn)
        return f"{self.model} —— {self.family} 驱动（{'已实测' if ok else '未实测，如有不对请反馈修改'}）"

    # ---- 基础读写 ----
    def write(self, cmd):
        self.inst.write(cmd)

    def query(self, cmd):
        return self.inst.query(cmd).strip()

    def errors(self):
        """读取仪器错误队列；仪器不支持错误查询时自动停用（返回空列表）"""
        if not self._err_ok or not self.err_query:
            return []
        errs = []
        old = self.inst.timeout
        self.inst.timeout = 2000
        try:
            for _ in range(20):
                e = self.query(self.err_query)
                if not e or re.match(r'^\+?0\s*,', e) or e.upper().startswith("NO ERROR"):
                    break
                errs.append(e)
        except Exception:
            self._err_ok = False
            try:
                self.inst.clear()
            except Exception:
                pass
        finally:
            self.inst.timeout = old
        return errs

    def try_write(self, *cands):
        """依次尝试几种写法，哪种仪器不报错就用哪种；全部报错则返回最后一次的错误"""
        last = []
        for c in cands:
            self.write(c)
            errs = self.errors()
            if not errs:
                return []
            last = errs
        return last

    def try_query(self, *cands):
        for c in cands:
            try:
                r = self.query(c)
                if not self.errors():
                    return r
            except Exception:
                try:
                    self.inst.clear()
                except Exception:
                    pass
        raise NotSupported(f"仪器不认识这些查询命令：{cands}")

    def init(self):
        try:
            self.write("*CLS")
        except Exception:
            pass
        self.errors()

    def go_local(self):
        """让仪器回到本地（面板解锁）。33522B 实测不支持 SYST:COMM:RLST / SYST:LOC 这类 SCPI 命令，
        只能用 USB/GPIB 的“远程使能(REN)”控制，见 release_local()。"""
        return release_local(self.inst)

    def close(self):
        try:
            self.go_local()
            self.inst.close()
        except Exception:
            pass

    @staticmethod
    def arb_levels(vmin, vmax, keep_volts, amp, offset):
        if keep_volts:
            return vmax - vmin, (vmax + vmin) / 2
        return amp, offset

    # ---- 需要各驱动实现的功能 ----
    def apply(self, ch, func, freq=1000.0, amp=1.0, offset=0.0, phase=0.0, duty=50.0, highz=True):
        raise NotImplementedError

    def output(self, ch, on):
        raise NotImplementedError

    def output_state(self, ch):
        return None

    def read_settings(self, ch):
        return {}

    def set_sum(self, ch, enable, shape="SIN", freq=50.0, percent=20.0):
        raise NotSupported("这台仪器（或当前驱动）不支持叠加（Sum）功能。")

    def sync_phase(self):
        raise NotSupported("这台仪器（或当前驱动）不支持两通道相位对齐。")

    def upload_arb(self, ch, data, srate, keep_volts=True, amp=1.0, offset=0.0):
        raise NotSupported("这台仪器（或当前驱动）不支持上传任意波。")


# ----------------------------------------------------------------------------
# Keysight / Agilent 33500B、33600A —— 已实测（33522B）
# ----------------------------------------------------------------------------
class Keysight33500(Driver):
    family = "Keysight 33500B/33600A"
    tested = True
    has_noise_bw = True
    has_sum = True
    has_phase_sync = True
    max_arb_points = 1_000_000

    @classmethod
    def match(cls, idn):
        return bool(re.search(r"(KEYSIGHT|AGILENT)", idn, re.I) and re.search(r",\s*33[56]\d\d[A-Z]?\s*,", idn))

    @classmethod
    def is_tested(cls, idn):
        return bool(re.search(r",\s*335\d\dB", idn))      # 用 33522B 实测；33600A 命令相同但未实测

    def detect_channels(self):
        # 型号末位：1/9 为单通道（33511B、33521B、33509B、33611A…），0/2 为双通道
        return 1 if last_digit(self.model) in ("1", "9") else 2

    def __init__(self, inst, idn):
        super().__init__(inst, idn)
        self.has_arb = last_digit(self.model) in ("1", "2")    # 33509B/33510B/33519B/33520B 无任意波
        self.has_phase_sync = self.channels == 2

    def apply(self, ch, func, freq=1000.0, amp=1.0, offset=0.0, phase=0.0, duty=50.0, highz=True):
        s = f"SOUR{ch}"
        self.write(f"OUTP{ch}:LOAD {'INF' if highz else 50}")
        self.write(f"{s}:FUNC {func}")
        if func == "NOIS":
            self.write(f"{s}:FUNC:NOIS:BAND {freq}")
        elif func != "DC":
            self.write(f"{s}:FREQ {freq}")
        if func != "DC":
            self.write(f"{s}:VOLT:UNIT VPP")
            self.write(f"{s}:VOLT:OFFS 0")      # 先清零偏置，避免幅度+偏置瞬间超限
            self.write(f"{s}:VOLT {amp}")
        self.write(f"{s}:VOLT:OFFS {offset}")
        if func == "SQU":
            self.write(f"{s}:FUNC:SQU:DCYC {duty}")
        elif func == "PULS":
            self.write(f"{s}:FUNC:PULS:DCYC {duty}")
        elif func == "RAMP":
            self.write(f"{s}:FUNC:RAMP:SYMM {duty}")
        if func not in ("NOIS", "DC"):
            self.write(f"{s}:PHAS {phase}")
        return self.errors()

    def output(self, ch, on):
        self.write(f"OUTP{ch} {'ON' if on else 'OFF'}")
        return self.errors()

    def output_state(self, ch):
        return is_on(self.query(f"OUTP{ch}?"))

    def read_settings(self, ch):
        s = f"SOUR{ch}"
        self.write(f"{s}:VOLT:UNIT VPP")
        func = wave_code(self.query(f"{s}:FUNC?"))
        d = {"func": func,
             "freq": num(self.query(f"{s}:FREQ?")),
             "amp": num(self.query(f"{s}:VOLT?")),
             "offset": num(self.query(f"{s}:VOLT:OFFS?")),
             "phase": num(self.query(f"{s}:PHAS?")),
             "highz": num(self.query(f"OUTP{ch}:LOAD?")) > 1e6,
             "out": self.output_state(ch)}
        if func == "SQU":
            d["duty"] = num(self.query(f"{s}:FUNC:SQU:DCYC?"))
        elif func == "PULS":
            d["duty"] = num(self.query(f"{s}:FUNC:PULS:DCYC?"))
        elif func == "RAMP":
            d["duty"] = num(self.query(f"{s}:FUNC:RAMP:SYMM?"))
        elif func == "NOIS":
            d["freq"] = num(self.query(f"{s}:FUNC:NOIS:BAND?"))
        self.errors()
        return d

    def set_sum(self, ch, enable, shape="SIN", freq=50.0, percent=20.0):
        s = f"SOUR{ch}"
        if enable:
            self.write(f"{s}:SUM:SOUR INT")
            self.write(f"{s}:SUM:INT:FUNC {shape}")
            if shape != "NOIS":
                self.write(f"{s}:SUM:INT:FREQ {freq}")
            self.write(f"{s}:SUM:AMPL {percent}")
            self.write(f"{s}:SUM:STAT ON")
        else:
            self.write(f"{s}:SUM:STAT OFF")
        return self.errors()

    def sync_phase(self):
        self.write("SOUR1:PHAS:SYNC")
        return self.errors()

    def upload_arb(self, ch, data, srate, keep_volts=True, amp=1.0, offset=0.0, name="USERARB"):
        norm, vmin, vmax = normalize(data, self.max_arb_points)
        dac = np.round(norm * 32767).astype(np.int16)
        amp, offset = self.arb_levels(vmin, vmax, keep_volts, amp, offset)
        s = f"SOUR{ch}"
        old = self.inst.timeout
        self.inst.timeout = 120000
        try:
            self.write(f"{s}:FUNC SIN")            # 先切离任意波，才能清空波形内存
            self.write(f"{s}:DATA:VOL:CLE")
            self.inst.write_binary_values(f"{s}:DATA:ARB:DAC {name},", dac, datatype="h", is_big_endian=True)
            self.query("*OPC?")
            self.write(f"{s}:FUNC:ARB {name}")
            self.write(f"{s}:FUNC ARB")
            self.write(f"{s}:FUNC:ARB:SRAT {srate}")
            self.write(f"{s}:VOLT:UNIT VPP")
            self.write(f"{s}:VOLT:OFFS 0")
            self.write(f"{s}:VOLT {amp}")
            self.write(f"{s}:VOLT:OFFS {offset}")
        finally:
            self.inst.timeout = old
        return self.errors(), {"points": len(norm), "duration": len(norm) / srate, "amp": amp, "offset": offset}


# ----------------------------------------------------------------------------
# Keysight / Agilent 33120A、33210A、33220A、33250A（老款单通道）—— 未实测
# ----------------------------------------------------------------------------
class Keysight33200(Driver):
    family = "Keysight 33120A/33210A/33220A/33250A"
    has_arb = True
    max_arb_points = 65536

    @classmethod
    def match(cls, idn):
        return bool(re.search(r"(KEYSIGHT|AGILENT|HEWLETT)", idn, re.I) and re.search(r",\s*33[12][0-9]0A\s*,", idn))

    def detect_channels(self):
        return 1

    def __init__(self, inst, idn):
        super().__init__(inst, idn)
        if "33120" in self.model:
            self.max_arb_points = 16000
            self.duty_waves = ("SQU",)

    def apply(self, ch, func, freq=1000.0, amp=1.0, offset=0.0, phase=0.0, duty=50.0, highz=True):
        errs = []
        self.write(f"OUTP:LOAD {'INF' if highz else 50}")
        if func == "TRI":
            errs += self.try_write("FUNC TRI", "FUNC RAMP")
        else:
            self.write(f"FUNC {func}")
        if func not in ("DC", "NOIS"):
            self.write(f"FREQ {freq}")
        if func != "DC":
            self.write("VOLT:UNIT VPP")
            self.write("VOLT:OFFS 0")
            self.write(f"VOLT {amp}")
        self.write(f"VOLT:OFFS {offset}")
        if func == "SQU":
            self.write(f"FUNC:SQU:DCYC {duty}")
        elif func == "RAMP":
            self.write(f"FUNC:RAMP:SYMM {duty}")
        elif func == "TRI" and "33120" not in self.model:
            self.write("FUNC:RAMP:SYMM 50")
        elif func == "PULS":
            errs += self.try_write(f"FUNC:PULS:DCYC {duty}", f"PULS:WIDT {duty / 100 / freq}")
        return errs + self.errors()

    def output(self, ch, on):
        if "33120" in self.model:
            raise NotSupported("33120A 没有输出开关命令，输出一直是打开的。")
        self.write(f"OUTP {'ON' if on else 'OFF'}")
        return self.errors()

    def output_state(self, ch):
        if "33120" in self.model:
            return True
        return is_on(self.query("OUTP?"))

    def read_settings(self, ch):
        self.write("VOLT:UNIT VPP")
        d = {"func": wave_code(self.query("FUNC?")),
             "freq": num(self.query("FREQ?")),
             "amp": num(self.query("VOLT?")),
             "offset": num(self.query("VOLT:OFFS?")),
             "highz": num(self.query("OUTP:LOAD?")) > 1e6,
             "out": self.output_state(ch)}
        self.errors()
        return d

    def upload_arb(self, ch, data, srate, keep_volts=True, amp=1.0, offset=0.0):
        norm, vmin, vmax = normalize(data, self.max_arb_points)
        full = 2047 if ("33250" in self.model or "33120" in self.model) else 8191
        dac = np.round(norm * full).astype(np.int16)
        amp, offset = self.arb_levels(vmin, vmax, keep_volts, amp, offset)
        old = self.inst.timeout
        self.inst.timeout = 120000
        try:
            self.inst.write_binary_values("DATA:DAC VOLATILE,", dac, datatype="h", is_big_endian=True)
            self.query("*OPC?")
            self.write("FUNC:USER VOLATILE")
            self.write("FUNC USER")
            self.write(f"FREQ {srate / len(norm)}")      # 老款没有采样率设置：频率 = 采样率 / 点数
            self.write("VOLT:UNIT VPP")
            self.write("VOLT:OFFS 0")
            self.write(f"VOLT {amp}")
            self.write(f"VOLT:OFFS {offset}")
        finally:
            self.inst.timeout = old
        return self.errors(), {"points": len(norm), "duration": len(norm) / srate, "amp": amp, "offset": offset}


# ----------------------------------------------------------------------------
# Rigol DG 系列 —— 未实测
# ----------------------------------------------------------------------------
class RigolDG(Driver):
    family = "Rigol DG"
    has_arb = True
    has_phase_sync = True
    max_arb_points = 16384

    @classmethod
    def match(cls, idn):
        return bool(re.search(r"RIGOL", idn, re.I) and re.search(r",\s*DG\d", idn, re.I))

    def detect_channels(self):
        m = re.search(r"DG(\d+)", self.model.upper())
        return 1 if (m and len(m.group(1)) >= 3 and m.group(1)[-1] == "1") else 2   # DG811、DG5071 等单通道

    def __init__(self, inst, idn):
        super().__init__(inst, idn)
        self.has_phase_sync = self.channels == 2

    def apply(self, ch, func, freq=1000.0, amp=1.0, offset=0.0, phase=0.0, duty=50.0, highz=True):
        s = f":SOUR{ch}"
        z = "INF" if highz else "50"
        errs = self.try_write(f":OUTP{ch}:LOAD {z}", f":OUTP{ch}:IMP {z}")
        if func == "TRI":
            errs += self.try_write(f"{s}:FUNC TRI", f"{s}:FUNC RAMP")
        else:
            self.write(f"{s}:FUNC {func}")
        if func not in ("DC", "NOIS"):
            self.write(f"{s}:FREQ {freq}")
        if func != "DC":
            self.write(f"{s}:VOLT:UNIT VPP")
            self.write(f"{s}:VOLT:OFFS 0")
            self.write(f"{s}:VOLT {amp}")
        self.write(f"{s}:VOLT:OFFS {offset}")
        if func == "SQU":
            errs += self.try_write(f"{s}:FUNC:SQU:DCYC {duty}")
        elif func == "PULS":
            errs += self.try_write(f"{s}:PULS:DCYC {duty}", f"{s}:FUNC:PULS:DCYC {duty}")
        elif func == "RAMP":
            errs += self.try_write(f"{s}:FUNC:RAMP:SYMM {duty}")
        elif func == "TRI":
            self.try_write(f"{s}:FUNC:RAMP:SYMM 50")     # 若已是 TRI 波形，这条报错可忽略
        if func not in ("NOIS", "DC"):
            self.write(f"{s}:PHAS {phase}")
        return errs + self.errors()

    def output(self, ch, on):
        self.write(f":OUTP{ch} {'ON' if on else 'OFF'}")
        return self.errors()

    def output_state(self, ch):
        return is_on(self.query(f":OUTP{ch}?"))

    def read_settings(self, ch):
        s = f":SOUR{ch}"
        self.write(f"{s}:VOLT:UNIT VPP")
        d = {"func": wave_code(self.query(f"{s}:FUNC?")),
             "freq": num(self.query(f"{s}:FREQ?")),
             "amp": num(self.query(f"{s}:VOLT?")),
             "offset": num(self.query(f"{s}:VOLT:OFFS?")),
             "out": self.output_state(ch)}
        try:
            d["phase"] = num(self.query(f"{s}:PHAS?"))
        except Exception:
            pass
        try:
            d["highz"] = num(self.try_query(f":OUTP{ch}:LOAD?", f":OUTP{ch}:IMP?")) > 1e6
        except Exception:
            pass
        self.errors()
        return d

    def sync_phase(self):
        return self.try_write(":SOUR1:PHAS:INIT", ":SOUR1:PHAS:SYNC")

    def upload_arb(self, ch, data, srate, keep_volts=True, amp=1.0, offset=0.0):
        norm, vmin, vmax = normalize(data, self.max_arb_points)
        amp, offset = self.arb_levels(vmin, vmax, keep_volts, amp, offset)
        s = f":SOUR{ch}"
        old = self.inst.timeout
        self.inst.timeout = 120000
        try:
            values = ",".join(f"{v:.4f}" for v in norm)
            self.write(f"{s}:TRAC:DATA VOLATILE,{values}")    # 数值范围 -1 ~ +1
            self.query("*OPC?")
            self.write(f"{s}:FREQ {srate / len(norm)}")        # 播放一遍 = 一个周期
            self.write(f"{s}:VOLT:UNIT VPP")
            self.write(f"{s}:VOLT:OFFS 0")
            self.write(f"{s}:VOLT {amp}")
            self.write(f"{s}:VOLT:OFFS {offset}")
        finally:
            self.inst.timeout = old
        return self.errors(), {"points": len(norm), "duration": len(norm) / srate, "amp": amp, "offset": offset}


# ----------------------------------------------------------------------------
# 鼎阳 Siglent SDG 系列 —— 未实测
# ----------------------------------------------------------------------------
class SiglentSDG(Driver):
    family = "鼎阳 Siglent SDG"
    has_arb = True
    max_arb_points = 16384
    WV = {"SIN": "SINE", "SQU": "SQUARE", "RAMP": "RAMP", "TRI": "RAMP", "PULS": "PULSE", "NOIS": "NOISE", "DC": "DC"}

    @classmethod
    def match(cls, idn):
        return bool(re.search(r"SIGLENT", idn, re.I) and re.search(r",\s*SDG", idn, re.I))

    def detect_channels(self):
        return 1 if re.match(r"SDG8\d\d", self.model.upper()) else 2     # SDG805/810/830 单通道

    def __init__(self, inst, idn):
        super().__init__(inst, idn)
        if re.match(r"SDG[267]", self.model.upper()):
            self.max_arb_points = 1_000_000

    @staticmethod
    def _kv(resp):
        body = resp.split(" ", 1)[1] if " " in resp else resp
        items = [x.strip() for x in body.split(",")]
        return {items[i].upper(): items[i + 1] for i in range(0, len(items) - 1, 2)}

    def apply(self, ch, func, freq=1000.0, amp=1.0, offset=0.0, phase=0.0, duty=50.0, highz=True):
        c = f"C{ch}"
        self.write(f"{c}:OUTP LOAD,{'HZ' if highz else 50}")
        if func == "DC":
            self.write(f"{c}:BSWV WVTP,DC,OFST,{offset}")
        elif func == "NOIS":
            # 噪声用标准差设定：峰峰值约等于 6 倍标准差
            self.write(f"{c}:BSWV WVTP,NOISE,STDEV,{amp / 6},MEAN,{offset}")
        else:
            p = f"WVTP,{self.WV[func]},FRQ,{freq},AMP,{amp},OFST,{offset},PHSE,{phase}"
            if func in ("SQU", "PULS"):
                p += f",DUTY,{duty}"
            elif func == "RAMP":
                p += f",SYM,{duty}"
            elif func == "TRI":
                p += ",SYM,50"
            self.write(f"{c}:BSWV {p}")
        return self.errors()

    def output(self, ch, on):
        self.write(f"C{ch}:OUTP {'ON' if on else 'OFF'}")
        return self.errors()

    def output_state(self, ch):
        r = self.query(f"C{ch}:OUTP?")              # 例：C1:OUTP ON,LOAD,HZ,PLRT,NOR
        body = r.split(" ", 1)[1] if " " in r else r
        return is_on(body.split(",")[0])

    def read_settings(self, ch):
        kv = self._kv(self.query(f"C{ch}:BSWV?"))
        d = {"func": wave_code({"SINE": "SIN", "SQUARE": "SQU", "PULSE": "PULS", "NOISE": "NOIS"}
                               .get(kv.get("WVTP", "").upper(), kv.get("WVTP", "")))}
        for key, name in (("FRQ", "freq"), ("AMP", "amp"), ("OFST", "offset"), ("PHSE", "phase")):
            if key in kv:
                d[name] = num(kv[key])
        if "DUTY" in kv:
            d["duty"] = num(kv["DUTY"])
        elif "SYM" in kv:
            d["duty"] = num(kv["SYM"])
        r = self.query(f"C{ch}:OUTP?")
        body = r.split(" ", 1)[1] if " " in r else r
        items = [x.strip().upper() for x in body.split(",")]
        d["out"] = is_on(items[0]) if items else None
        if "LOAD" in items and items.index("LOAD") + 1 < len(items):
            d["highz"] = items[items.index("LOAD") + 1] == "HZ"
        return d

    def upload_arb(self, ch, data, srate, keep_volts=True, amp=1.0, offset=0.0):
        norm, vmin, vmax = normalize(data, self.max_arb_points)
        amp, offset = self.arb_levels(vmin, vmax, keep_volts, amp, offset)
        dac = np.round(norm * 32767).astype("<i2")
        freq = srate / len(norm)
        old = self.inst.timeout
        self.inst.timeout = 120000
        try:
            head = f"C{ch}:WVDT WVNM,USERARB,FREQ,{freq},AMPL,{amp},OFST,{offset},PHASE,0,WAVEDATA,"
            self.inst.write_raw(head.encode("ascii") + dac.tobytes())
            self.write(f"C{ch}:ARWV NAME,USERARB")
            self.write(f"C{ch}:BSWV FRQ,{freq},AMP,{amp},OFST,{offset}")
        finally:
            self.inst.timeout = old
        return self.errors(), {"points": len(norm), "duration": len(norm) / srate, "amp": amp, "offset": offset}


# ----------------------------------------------------------------------------
# 泰克 Tektronix AFG 系列 —— 未实测
# ----------------------------------------------------------------------------
class TektronixAFG(Driver):
    family = "泰克 Tektronix AFG"
    duty_waves = ("PULS", "RAMP")
    has_arb = True
    max_arb_points = 131072

    @classmethod
    def match(cls, idn):
        return bool(re.search(r"TEKTRONIX", idn, re.I) and re.search(r",\s*AFG", idn, re.I))

    def detect_channels(self):
        return 1 if last_digit(self.model) == "1" else 2      # AFG3021C、AFG31021 单通道

    def __init__(self, inst, idn):
        super().__init__(inst, idn)
        self.has_phase_sync = self.channels == 2
        if self.model.upper().startswith("AFG1"):
            self.max_arb_points = 8192

    def apply(self, ch, func, freq=1000.0, amp=1.0, offset=0.0, phase=0.0, duty=50.0, highz=True):
        s = f"SOUR{ch}"
        self.write(f"OUTP{ch}:IMP {'INF' if highz else 50}")
        self.write(f"{s}:FUNC {'PRN' if func == 'NOIS' else 'RAMP' if func == 'TRI' else func}")
        if func not in ("DC", "NOIS"):
            self.write(f"{s}:FREQ {freq}")
        if func != "DC":
            self.write(f"{s}:VOLT:UNIT VPP")
            self.write(f"{s}:VOLT:OFFS 0")
            self.write(f"{s}:VOLT {amp}")
        self.write(f"{s}:VOLT:OFFS {offset}")
        if func == "RAMP":
            self.write(f"{s}:FUNC:RAMP:SYMM {duty}")
        elif func == "TRI":
            self.write(f"{s}:FUNC:RAMP:SYMM 50")
        elif func == "PULS":
            self.write(f"{s}:PULS:DCYC {duty}")
        if func not in ("NOIS", "DC"):
            self.write(f"{s}:PHAS {phase}DEG")
        return self.errors()

    def output(self, ch, on):
        self.write(f"OUTP{ch} {'ON' if on else 'OFF'}")
        return self.errors()

    def output_state(self, ch):
        return is_on(self.query(f"OUTP{ch}?"))

    def read_settings(self, ch):
        s = f"SOUR{ch}"
        self.write(f"{s}:VOLT:UNIT VPP")
        d = {"func": wave_code(self.query(f"{s}:FUNC?")),
             "freq": num(self.query(f"{s}:FREQ?")),
             "amp": num(self.query(f"{s}:VOLT?")),
             "offset": num(self.query(f"{s}:VOLT:OFFS?")),
             "highz": num(self.query(f"OUTP{ch}:IMP?")) > 1e6,
             "out": self.output_state(ch)}
        try:
            d["phase"] = np.degrees(num(self.query(f"{s}:PHAS?")))   # 泰克返回弧度
        except Exception:
            pass
        self.errors()
        return d

    def sync_phase(self):
        self.write("SOUR1:PHAS:INIT")
        return self.errors()

    def upload_arb(self, ch, data, srate, keep_volts=True, amp=1.0, offset=0.0):
        norm, vmin, vmax = normalize(data, self.max_arb_points)
        amp, offset = self.arb_levels(vmin, vmax, keep_volts, amp, offset)
        dac = np.round((norm + 1) / 2 * 16382).astype(np.uint16)       # 0 ~ 16382
        s = f"SOUR{ch}"
        old = self.inst.timeout
        self.inst.timeout = 120000
        try:
            self.write(f"DATA:DEF EMEM,{len(norm)}")
            self.inst.write_binary_values("DATA:DATA EMEM,", dac, datatype="H", is_big_endian=True)
            self.query("*OPC?")
            self.write(f"{s}:FUNC EMEM")
            self.write(f"{s}:FREQ {srate / len(norm)}")
            self.write(f"{s}:VOLT:UNIT VPP")
            self.write(f"{s}:VOLT:OFFS 0")
            self.write(f"{s}:VOLT {amp}")
            self.write(f"{s}:VOLT:OFFS {offset}")
        finally:
            self.inst.timeout = old
        return self.errors(), {"points": len(norm), "duration": len(norm) / srate, "amp": amp, "offset": offset}


# ----------------------------------------------------------------------------
# 兼容模式：其他品牌（GW Instek、UNI-T 等）—— 未实测
# 每一步都依次尝试两种常见写法：标准 SCPI（SOURce/OUTPut）与 UNI-T 风格（:CHANnel:BASE）
# ----------------------------------------------------------------------------
class GenericSCPI(Driver):
    family = "通用 SCPI（兼容模式）"
    UNI = {"SIN": "SINE", "SQU": "SQUARE", "RAMP": "RAMP", "TRI": "RAMP", "PULS": "PULSE", "NOIS": "NOISE",
           "DC": "DC"}

    def apply(self, ch, func, freq=1000.0, amp=1.0, offset=0.0, phase=0.0, duty=50.0, highz=True):
        s, c = f"SOUR{ch}", f":CHAN{ch}"
        z = "INF" if highz else "50"
        errs = []
        errs += self.try_write(f"OUTP{ch}:LOAD {z}", f"OUTP{ch}:IMP {z}", f"{c}:LOAD {'HIGHZ' if highz else 50}")
        errs += self.try_write(f"{s}:FUNC {func}", f"{c}:BASE:WAV {self.UNI[func]}")
        if func not in ("DC", "NOIS"):
            errs += self.try_write(f"{s}:FREQ {freq}", f"{c}:BASE:FREQ {freq}")
        if func != "DC":
            errs += self.try_write(f"{s}:VOLT {amp}", f"{c}:BASE:AMPL {amp}")
        errs += self.try_write(f"{s}:VOLT:OFFS {offset}", f"{c}:BASE:OFFS {offset}")
        if func == "SQU":
            errs += self.try_write(f"{s}:FUNC:SQU:DCYC {duty}", f"{s}:SQU:DCYC {duty}", f"{c}:BASE:DUTY {duty}")
        elif func == "PULS":
            errs += self.try_write(f"{s}:FUNC:PULS:DCYC {duty}", f"{s}:PULS:DCYC {duty}", f"{c}:BASE:DUTY {duty}")
        elif func == "RAMP":
            errs += self.try_write(f"{s}:FUNC:RAMP:SYMM {duty}", f"{s}:RAMP:SYMM {duty}", f"{c}:RAMP:SYMM {duty}")
        if func not in ("NOIS", "DC"):
            errs += self.try_write(f"{s}:PHAS {phase}", f"{c}:BASE:PHAS {phase}")
        return errs

    def output(self, ch, on):
        v = "ON" if on else "OFF"
        return self.try_write(f"OUTP{ch} {v}", f":CHAN{ch}:OUTP {v}")

    def output_state(self, ch):
        try:
            return is_on(self.try_query(f"OUTP{ch}?", f":CHAN{ch}:OUTP?"))
        except Exception:
            return None

    def read_settings(self, ch):
        s, c = f"SOUR{ch}", f":CHAN{ch}"
        d = {}
        for name, cands in (("func", (f"{s}:FUNC?", f"{c}:BASE:WAV?")),
                            ("freq", (f"{s}:FREQ?", f"{c}:BASE:FREQ?")),
                            ("amp", (f"{s}:VOLT?", f"{c}:BASE:AMPL?")),
                            ("offset", (f"{s}:VOLT:OFFS?", f"{c}:BASE:OFFS?"))):
            try:
                r = self.try_query(*cands)
                d[name] = wave_code(r) if name == "func" else num(r)
            except Exception:
                pass
        d["out"] = self.output_state(ch)
        return d


# 识别顺序：专用驱动优先，最后是兼容模式
DRIVERS = [Keysight33500, Keysight33200, RigolDG, SiglentSDG, TektronixAFG]
# 认不出品牌时，型号像信号发生器才用兼容模式（避免把示波器等仪器当成信号发生器）
GENERATOR_HINT = re.compile(r",\s*(AFG|AWG|SDG|DG\d|UTG|MFG|SFG|FG|WG|33\d{3})", re.I)


def pick_driver(idn):
    for cls in DRIVERS:
        if cls.match(idn):
            return cls
    if GENERATOR_HINT.search(idn):
        return GenericSCPI
    return None


_MUTEXES = []


def set_app_id(app_id):
    """让 Windows 任务栏把本程序当成独立程序（显示它自己的图标，而不是 Python 的图标）。要在创建窗口之前调用。"""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"InstrumentTools.{app_id}")
    except Exception:
        pass


def set_icon(root, ico_name, here):
    """窗口左上角和任务栏图标。打包成 exe 后图标文件在解包目录（sys._MEIPASS）里，否则在程序文件夹 here 里。"""
    import sys
    path = os.path.join(getattr(sys, "_MEIPASS", here), ico_name)
    if os.path.isfile(path):
        try:
            root.iconbitmap(default=path)        # default= 让之后弹出的对话框也用这个图标
        except Exception:
            pass


def single_instance(name):
    """防止同一个程序被打开两份（两份程序同时给仪器发命令会互相打乱，示波器甚至会卡死）。
    返回 True 表示这是第一份；False 表示已经有一份在运行。"""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        handle = k32.CreateMutexW(None, False, f"Local\\InstrumentTools_{name}")
        already = k32.GetLastError() == 183          # ERROR_ALREADY_EXISTS
        _MUTEXES.append(handle)                      # 句柄要一直保留到程序退出
        return not already
    except Exception:
        return True


def open_rm():
    return pyvisa.ResourceManager(KTVISA) if os.path.isfile(KTVISA) else pyvisa.ResourceManager()


SCOPE_LIKE = re.compile(r",\s*(DSO|UTD|UPO|MSO|DS\d|DHO|SDS|TBS|TDS|MDO|DPO|EDUX|DSOX|MSOX|GDS)", re.I)


def release_local(inst):
    """让仪器回到本地（面板可操作）：先释放“远程使能”并回本地，不支持时再单独发“回本地”。
    注意：只要给仪器发过任何命令（哪怕只是 *IDN? 问型号），33522B 等仪器就会进入“远程”，面板锁住，
    必须这样交还，否则要手动按面板上的 Local。"""
    ops = pyvisa.constants.RENLineOperation
    for op in (ops.deassert_gtl, ops.address_gtl):
        try:
            inst.control_ren(op)
            return True
        except Exception:
            continue
    return False


LAST_ERRORS = {}   # 最近一次 identify 失败的原因：{地址: 文字}，扫描“没找到”时给界面显示用

# 实测（2026-11-11 三次）：打开某台不回应的 USB 仪器（Keysight 自带的自动查询，或我们自己打开）卡住后，
# Keysight VISA 打开任何 USB 仪器都报 VI_ERROR_ALLOC。单独重启电脑、单独重装都不一定恢复；
# “覆盖重装 Keysight IO Libraries + 重启电脑”可以恢复。Rigol DP932U（USB PID 0xA4A8）每次打开都会这样卡住。
ALLOC_TITLE = "仪器通信软件卡住了"
ALLOC_HELP = ("电脑上的 Keysight 仪器通信软件卡住了（报错 VI_ERROR_ALLOC），所有 USB 仪器暂时都连不上。"
              "重启电脑也恢复不了。\n\n"
              "通常是因为：某台仪器刚插上 USB 时没有回应 Keysight 的自动查询。\n\n"
              "解决办法：\n"
              "1. 关掉信号发生器、示波器、直流电源三个控制程序；\n"
              "2. 拔掉最近新插上的那台仪器的 USB 线；\n"
              "3. 双击 C:\\测试工具 文件夹里的“一键修复Keysight.bat”，等待约 6 分钟；\n"
              "4. 修复完成后一定要重启电脑（只重装不重启，实测仍然连不上）。\n\n"
              "注意：Rigol DP932U 电源用 USB 接这台电脑会触发这个问题，请不要再插它的 USB 线。")


def visa_stuck():
    """最近一次扫描是否遇到了 VI_ERROR_ALLOC（Keysight VISA 卡住）"""
    return any("VI_ERROR_ALLOC" in m for m in LAST_ERRORS.values())


def _identify(rm, addr, timeout):
    inst = None
    try:
        inst = rm.open_resource(addr)
        inst.timeout = timeout
        idn = inst.query("*IDN?").strip()
        if not SCOPE_LIKE.search(idn):
            release_local(inst)
        LAST_ERRORS.pop(addr, None)
        return idn
    except Exception as e:
        LAST_ERRORS[addr] = f"{type(e).__name__}: {e}"
        return None
    finally:
        if inst is not None:
            try:
                inst.close()
            except Exception:
                pass


def identify(rm, addr, timeout=3000):
    """问一台仪器的型号（*IDN?），问完立刻交还面板再关闭连接；失败或超时返回 None。
    示波器不发“交还”命令（UNI-T 示波器的 USB 比较脆弱，而且示波器不会因为远程而锁面板）。
    实测：有时某台仪器的 USB 卡住，VISA 调用会无限期等下去（不受 timeout 控制），整个扫描就卡死。
    所以放到单独的线程里做，超过 timeout + 2 秒就放弃这台，继续扫描其他仪器。"""
    import threading
    box = {}
    th = threading.Thread(target=lambda: box.__setitem__("idn", _identify(rm, addr, timeout)), daemon=True)
    th.start()
    th.join(timeout / 1000 + 2)
    if th.is_alive():
        LAST_ERRORS[addr] = f"超过 {timeout / 1000 + 2:g} 秒没有回应，已跳过"
    return box.get("idn")


def scan(rm):
    """扫描 USB / 网络上的仪器；返回 [(地址, IDN, 驱动类)]，只保留认得出的信号发生器"""
    found = []
    for r in rm.list_resources():
        if not r.upper().startswith(("USB", "TCPIP")):
            continue
        idn = identify(rm, r)
        if idn is None:
            continue
        cls = pick_driver(idn)
        if cls:
            found.append((r, idn, cls))
    return found


def connect(rm, addr, idn, cls):
    inst = rm.open_resource(addr)
    inst.timeout = 5000
    drv = cls(inst, idn)
    drv.init()
    return drv


def load_numbers(path, column=-1):
    """读取 CSV / TXT 中的一列数字；自动跳过表头等非数字行。column 从 1 开始，-1 表示最后一列"""
    values = []
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        for line in f:
            parts = [p for p in re.split(r"[,;\t ]+", line.strip()) if p]
            if not parts:
                continue
            try:
                nums = [float(p) for p in parts]
            except ValueError:
                continue
            idx = column - 1 if column > 0 else column
            try:
                values.append(nums[idx])
            except IndexError:
                continue
    if not values:
        raise ValueError("文件里没有读到数字。请确认是每行一个数（或用逗号/空格分隔的几列数字）。")
    return np.array(values)
