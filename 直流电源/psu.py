"""
直流电源驱动（通用版）—— 统一接口 + 各品牌驱动，连接时按 *IDN? 自动选择

  驱动              适用型号                                                  状态
  KeysightE36300   Keysight E36311A/E36312A/E36313A、EDU36311A（命令带 (@通道)） E36313A 已实测（2026-11-11），其他型号未实测
  KeysightE3631    Keysight/Agilent E3631A/E3646A/E3648A 等老型号               未实测
  RigolDP          Rigol DP800 系列（DP811/DP821/DP831/DP832 等）、DP900 系列    未实测
                   （注意：实测 Rigol DP932U 通过 USB + Keysight VISA 打开时会卡死，并让 Keysight VISA 报
                    VI_ERROR_ALLOC，见 README“已知问题”；DP932U 请改用网线 LXI 连接）
  SiglentSPD       Siglent SPD3303X/SPD3303X-E/SPD3303C 等                     未实测
  GenericSCPI      其他品牌（兼容模式，按 E36300 的写法）                       未实测

统一约定：通道编号从 1 开始；电压单位 V，电流单位 A。
状态：OFF（输出关）、CV（恒压）、CC（恒流）、OV（过压保护动作）、OC（过流保护动作）、?（读不出来）。
注意：给电源发任何命令都会让它进入“远程”、面板锁住 —— 每次操作完都调用 go_local() 交还面板。
"""
import os
import re
import sys
import time

# 程序文件夹（记录存在这里的“记录”子文件夹）：打包成 exe 后是 exe 所在文件夹，否则是本文件所在文件夹
HERE = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
GEN_DIR = os.path.join(os.path.dirname(HERE), "信号发生器")
if GEN_DIR not in sys.path:
    sys.path.insert(0, GEN_DIR)
import drivers as gen          # noqa: E402  复用 VISA 加载、identify、release_local、single_instance

PSU_HINT = re.compile(r",\s*(E36\d|E364\d|E363\d|EDU36|DP\d|SPD\d|PWS\d|2230|2231|GPP|GPD|KA3|KD3|PSU|N67|N57)", re.I)


class NotSupported(Exception):
    pass


def to_float(s):
    m = re.search(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", str(s))
    return float(m.group()) if m else None


def floats(s):
    return [float(x) for x in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", str(s))]


def model_of(idn):
    p = [x.strip() for x in idn.split(",")]
    return p[1] if len(p) > 1 else ""


def judge_status(on, set_v, set_i, meas_v, meas_i, ov=False, oc=False):
    """由测量值判断 CV / CC（各品牌通用的判断方法）"""
    if ov:
        return "OV"
    if oc:
        return "OC"
    if not on:
        return "OFF"
    if None in (set_v, set_i, meas_v, meas_i):
        return "?"
    if set_i > 0 and abs(meas_i) >= 0.98 * set_i - 0.0005:
        return "CC"
    if abs(set_v) - abs(meas_v) > max(0.03, 0.02 * abs(set_v)):
        return "CC"
    return "CV"


def compose_state(settings, mv, mi):
    """把 read_settings() 的结果和实测电压/电流列表合成完整状态（格式同 state()）"""
    out = {}
    for ch, s in settings.items():
        v = mv[ch - 1] if mv else None
        i = mi[ch - 1] if mi else None
        d = dict(s, meas_v=v, meas_i=i)
        d["status"] = judge_status(d["on"], d["set_v"], d["set_i"], v, i, d["ov"], d["oc"])
        out[ch] = d
    return out


# =============================================================================
class PSUDriver:
    family = "未知"
    tested_re = None
    has_ovp = True
    has_ocp = True
    per_channel_output = True      # 能否单独开关每个通道（E3631A 只能三路一起开关）
    split_poll = False             # True：驱动提供 read_settings / read_meas_v / read_meas_i，界面分段读取
    # 默认额定值（连接后会尽量向仪器查询实际上限）：[(名称, 最大电压, 最大电流), ...]
    RATINGS = [("CH1", 30.0, 3.0)]

    def __init__(self, inst, idn):
        self.inst = inst
        self.idn = idn
        self.vendor = idn.split(",")[0].strip()
        self.model = model_of(idn)
        self.channels = [{"name": n, "vmax": v, "imax": i} for n, v, i in self.ratings()]

    @classmethod
    def match(cls, idn):
        return False

    def ratings(self):
        return self.RATINGS

    def is_tested(self):
        return bool(self.tested_re and re.search(self.tested_re, self.idn, re.I))

    def describe(self):
        return f"{self.vendor} {self.model}（{self.family}，{len(self.channels)} 通道）"

    @property
    def n(self):
        return len(self.channels)

    # ---------- 底层 ----------
    def write(self, cmd):
        self.inst.write(cmd)
        time.sleep(0.02)

    def query(self, cmd):
        return self.inst.query(cmd).strip()

    def qfloat(self, cmd):
        try:
            return to_float(self.query(cmd))
        except Exception:
            self.recover()
            return None

    def recover(self):
        try:
            self.inst.clear()
        except Exception:
            pass

    def errors(self):
        out = []
        for _ in range(10):
            try:
                e = self.query("SYST:ERR?")
            except Exception:
                self.recover()
                break
            if re.match(r"^\s*[+]?0\b", e) or "NO ERROR" in e.upper():
                break
            out.append(e)
        return out

    def init(self):
        pass

    def go_local(self):
        return gen.release_local(self.inst)

    def close(self):
        try:
            self.go_local()
            self.inst.close()
        except Exception:
            pass

    def check_range(self, ch, volt=None, curr=None):
        c = self.channels[ch - 1]
        if volt is not None and not (0 <= volt <= c["vmax"] + 1e-9):
            raise ValueError(f"{c['name']} 电压只能是 0 ~ {c['vmax']:g} V")
        if curr is not None and not (0 <= curr <= c["imax"] + 1e-9):
            raise ValueError(f"{c['name']} 限流只能是 0 ~ {c['imax']:g} A")

    # ---------- 子类实现 ----------
    def set_volt(self, ch, v):
        raise NotImplementedError

    def set_curr(self, ch, i):
        raise NotImplementedError

    def output(self, ch, on):
        raise NotImplementedError

    def output_all(self, on):
        for ch in range(1, self.n + 1):
            self.output(ch, on)

    def set_ovp(self, ch, v):
        raise NotSupported("这台电源（或驱动）不支持过压保护设置。")

    def set_ocp(self, ch, on):
        raise NotSupported("这台电源（或驱动）不支持过流保护设置。")

    def clear_prot(self, ch):
        raise NotSupported("这台电源（或驱动）不支持清除保护。")

    def channel_state(self, ch):
        raise NotImplementedError

    def state(self):
        """返回 {通道: {set_v, set_i, meas_v, meas_i, on, ovp, ocp_on, ov, oc, status}}"""
        return {ch: self.channel_state(ch) for ch in range(1, self.n + 1)}


# =============================================================================
# Keysight E36300 系列（E36311A/E36312A/E36313A、EDU36311A）—— 命令带通道列表 (@1:3)
# =============================================================================
class KeysightE36300(PSUDriver):
    family = "Keysight E36300 系列"
    tested_re = r",\s*E36313A"  # 2026-11-11 在 E36313A 上实测通过（读取、设置、开关输出、OVP 触发与清除、面板交还）
    TABLE = {
        "E36311A": [("CH1", 6.0, 5.0), ("CH2", 25.0, 1.0), ("CH3", 25.0, 1.0)],   # CH3 实际为 -25V（负）
        "E36312A": [("CH1", 6.0, 5.0), ("CH2", 25.0, 1.0), ("CH3", 25.0, 1.0)],
        "E36313A": [("CH1", 6.0, 10.0), ("CH2", 25.0, 2.0), ("CH3", 25.0, 2.0)],
        "EDU36311A": [("CH1", 6.0, 5.0), ("CH2", 30.0, 1.0), ("CH3", 30.0, 1.0)],
    }

    @classmethod
    def match(cls, idn):
        return bool(re.search(r"(KEYSIGHT|AGILENT)", idn, re.I) and re.search(r",\s*(E363[01]\d|EDU363)", idn, re.I))

    def ratings(self):
        for k, v in self.TABLE.items():
            if k in self.model.upper():
                return v
        return self.TABLE["E36312A"]

    def init(self):
        """向仪器查询每个通道的实际上限（MAX），查不到就用表里的额定值"""
        for i, c in enumerate(self.channels, start=1):
            vmax = self.qfloat(f"VOLT? MAX,(@{i})")
            imax = self.qfloat(f"CURR? MAX,(@{i})")
            if vmax and vmax > 0:
                c["vmax"] = abs(vmax)
            if imax and imax > 0:
                c["imax"] = imax
        self.errors()

    @property
    def all_ch(self):
        return f"(@1:{self.n})"

    def set_volt(self, ch, v):
        self.check_range(ch, volt=v)
        self.write(f"VOLT {v:.6g},(@{ch})")

    def set_curr(self, ch, i):
        self.check_range(ch, curr=i)
        self.write(f"CURR {i:.6g},(@{ch})")

    def output(self, ch, on):
        self.write(f"OUTP {'ON' if on else 'OFF'},(@{ch})")

    def output_all(self, on):
        self.write(f"OUTP {'ON' if on else 'OFF'},{self.all_ch}")

    def set_ovp(self, ch, v):
        self.write(f"VOLT:PROT {v:.6g},(@{ch})")

    def set_ocp(self, ch, on):
        self.write(f"CURR:PROT:STAT {'ON' if on else 'OFF'},(@{ch})")

    def clear_prot(self, ch):
        self.write(f"OUTP:PROT:CLE (@{ch})")

    def _list(self, cmd):
        try:
            vals = floats(self.query(f"{cmd} {self.all_ch}"))
        except Exception:
            self.recover()
            vals = []
        return vals + [None] * (self.n - len(vals))

    # 分段读取（E36313A 实测）：设定值/开关/保护状态很快（7 条共约 0.08 s），实测电压三路一起约 0.3 s，
    # 实测电流三路一起约 1.07 s、单路约 0.35 s（所以电流按通道分开测）。
    # 界面分三段轮流读，每段之间都能插入操作，操作后先读快的那段，显示就不会慢一拍。
    split_poll = True

    def read_settings(self):
        sv, si = self._list("VOLT?"), self._list("CURR?")
        on = self._list("OUTP?")            # 注意：OV/OC 保护触发后这里仍返回 1，要看 *:PROT:TRIP?
        ovp = self._list("VOLT:PROT?")
        ocp = self._list("CURR:PROT:STAT?")
        ovt, oct_ = self._list("VOLT:PROT:TRIP?"), self._list("CURR:PROT:TRIP?")
        return {k + 1: {"set_v": sv[k], "set_i": si[k], "on": bool(on[k]), "ovp": ovp[k],
                        "ocp_on": bool(ocp[k]), "ov": bool(ovt[k]), "oc": bool(oct_[k])} for k in range(self.n)}

    def read_meas_v(self, chs=None):
        """chs=None：所有通道；给定通道列表（如 [1, 3]）：只测这几路，按同样顺序返回"""
        if chs is None:
            return self._list("MEAS:VOLT?")
        try:
            vals = floats(self.query(f"MEAS:VOLT? (@{','.join(str(c) for c in chs)})"))
        except Exception:
            self.recover()
            vals = []
        return vals + [None] * (len(chs) - len(vals))

    def read_meas_i(self, ch=None):
        """ch=None：三个通道一起测（E36313A 约 1.07 s）；给定 ch：只测这一路（约 0.35 s），界面分开测，操作等得更短"""
        if ch is None:
            return self._list("MEAS:CURR?")
        try:
            return to_float(self.query(f"MEAS:CURR? (@{ch})"))
        except Exception:
            self.recover()
            return None

    def state(self):
        return compose_state(self.read_settings(), self.read_meas_v(), self.read_meas_i())


# =============================================================================
# Keysight/Agilent E3631A、E3646A 等老型号 —— 用 INST:SEL 选通道，输出只能整体开关
# =============================================================================
class KeysightE3631(PSUDriver):
    family = "Keysight E3631A/E364xA 系列"
    per_channel_output = False
    has_ocp = False

    @classmethod
    def match(cls, idn):
        return bool(re.search(r"(KEYSIGHT|AGILENT|HEWLETT)", idn, re.I) and re.search(r",\s*E36[34]\d[A-Z]?\s*,", idn, re.I))

    def ratings(self):
        m = self.model.upper()
        if "E3631" in m:
            self.sel = ["P6V", "P25V", "N25V"]
            return [("+6V", 6.0, 5.0), ("+25V", 25.0, 1.0), ("-25V", 25.0, 1.0)]
        if re.search(r"E364[6-9]", m):
            self.sel = ["OUT1", "OUT2"]
            return [("OUT1", 8.0, 3.0), ("OUT2", 8.0, 3.0)]
        self.sel = ["OUT1"]
        return [("OUT1", 20.0, 1.5)]

    def _select(self, ch):
        self.write(f"INST:SEL {self.sel[ch - 1]}")

    def set_volt(self, ch, v):
        self.check_range(ch, volt=v)
        self._select(ch)
        self.write(f"VOLT {v:.6g}")

    def set_curr(self, ch, i):
        self.check_range(ch, curr=i)
        self._select(ch)
        self.write(f"CURR {i:.6g}")

    def output(self, ch, on):
        self.write(f"OUTP {'ON' if on else 'OFF'}")        # 只能所有通道一起开关

    def output_all(self, on):
        self.write(f"OUTP {'ON' if on else 'OFF'}")

    def set_ovp(self, ch, v):
        self._select(ch)
        self.write(f"VOLT:PROT {v:.6g}")
        self.write("VOLT:PROT:STAT ON")

    def clear_prot(self, ch):
        self._select(ch)
        self.write("VOLT:PROT:CLE")

    def channel_state(self, ch):
        self._select(ch)
        sv, si = self.qfloat("VOLT?"), self.qfloat("CURR?")
        mv, mi = self.qfloat(f"MEAS:VOLT? {self.sel[ch - 1]}"), self.qfloat(f"MEAS:CURR? {self.sel[ch - 1]}")
        on = self.qfloat("OUTP?") == 1
        ovp = self.qfloat("VOLT:PROT?")
        ov = self.qfloat("VOLT:PROT:TRIP?") == 1
        return {"set_v": sv, "set_i": si, "meas_v": mv, "meas_i": mi, "on": on, "ovp": ovp, "ocp_on": False,
                "ov": ov, "oc": False, "status": judge_status(on, sv, si, mv, mi, ov)}


# =============================================================================
# Rigol DP800 系列
# =============================================================================
class RigolDP(PSUDriver):
    family = "Rigol DP800/DP900 系列"
    TABLE = {
        # DP900 系列（DP932U/DP932A/DP932E）命令和 DP800 基本相同；用户有一台 DP932U（2026-11-11 首次接入，尚未实测）
        "DP932": [("CH1", 32.0, 3.0), ("CH2", 32.0, 3.0), ("CH3", 6.0, 3.0)],
        "DP832": [("CH1", 30.0, 3.0), ("CH2", 30.0, 3.0), ("CH3", 5.0, 3.0)],
        "DP831": [("CH1", 8.0, 5.0), ("CH2", 30.0, 2.0), ("CH3", 30.0, 2.0)],   # CH3 实际为 -30V（负）
        "DP821": [("CH1", 60.0, 1.0), ("CH2", 8.0, 10.0)],
        "DP811": [("CH1", 20.0, 10.0)],
        "DP712": [("CH1", 50.0, 3.0)],
        "DP711": [("CH1", 30.0, 5.0)],
    }

    @classmethod
    def match(cls, idn):
        return bool(re.search(r"RIGOL", idn, re.I) and re.search(r",\s*DP\d", idn, re.I))

    def ratings(self):
        m = self.model.upper()
        for k, v in self.TABLE.items():
            if m.startswith(k):
                return v
        return self.TABLE["DP832"]

    def set_volt(self, ch, v):
        self.check_range(ch, volt=v)
        self.write(f":SOUR{ch}:VOLT {v:.6g}")

    def set_curr(self, ch, i):
        self.check_range(ch, curr=i)
        self.write(f":SOUR{ch}:CURR {i:.6g}")

    def output(self, ch, on):
        self.write(f":OUTP CH{ch},{'ON' if on else 'OFF'}")

    def set_ovp(self, ch, v):
        self.write(f":OUTP:OVP:VAL CH{ch},{v:.6g}")
        self.write(f":OUTP:OVP CH{ch},ON")

    def set_ocp(self, ch, on):
        if on:
            i = self.qfloat(f":SOUR{ch}:CURR?") or 0.1
            self.write(f":OUTP:OCP:VAL CH{ch},{i:.6g}")
        self.write(f":OUTP:OCP CH{ch},{'ON' if on else 'OFF'}")

    def clear_prot(self, ch):
        self.write(f":OUTP:OVP:CLEAR CH{ch}")
        self.write(f":OUTP:OCP:CLEAR CH{ch}")

    def channel_state(self, ch):
        sv, si = self.qfloat(f":SOUR{ch}:VOLT?"), self.qfloat(f":SOUR{ch}:CURR?")
        mv, mi = self.qfloat(f":MEAS:VOLT? CH{ch}"), self.qfloat(f":MEAS:CURR? CH{ch}")
        on = "ON" in self._q(f":OUTP? CH{ch}").upper()
        ov = "YES" in self._q(f":OUTP:OVP:QUES? CH{ch}").upper()
        oc = "YES" in self._q(f":OUTP:OCP:QUES? CH{ch}").upper()
        mode = self._q(f":OUTP:MODE? CH{ch}").upper()
        st = judge_status(on, sv, si, mv, mi, ov, oc)
        if st in ("CV", "CC") and mode in ("CV", "CC"):
            st = mode
        return {"set_v": sv, "set_i": si, "meas_v": mv, "meas_i": mi, "on": on,
                "ovp": self.qfloat(f":OUTP:OVP:VAL? CH{ch}"),
                "ocp_on": "ON" in self._q(f":OUTP:OCP? CH{ch}").upper(), "ov": ov, "oc": oc, "status": st}

    def _q(self, cmd):
        try:
            return self.query(cmd)
        except Exception:
            self.recover()
            return ""


# =============================================================================
# Siglent SPD3303X 系列（两路可调 + CH3 固定档）—— 没有 OVP/OCP；命令之间需要间隔
# =============================================================================
class SiglentSPD(PSUDriver):
    family = "Siglent SPD3303 系列"
    has_ovp = False
    has_ocp = False
    RATINGS = [("CH1", 32.0, 3.2), ("CH2", 32.0, 3.2)]

    @classmethod
    def match(cls, idn):
        return bool(re.search(r"SIGLENT", idn, re.I) and re.search(r",\s*SPD", idn, re.I))

    def write(self, cmd):
        self.inst.write(cmd)
        time.sleep(0.1)          # SPD3303 处理命令较慢

    def errors(self):
        return []

    def set_volt(self, ch, v):
        self.check_range(ch, volt=v)
        self.write(f"CH{ch}:VOLT {v:.3f}")

    def set_curr(self, ch, i):
        self.check_range(ch, curr=i)
        self.write(f"CH{ch}:CURR {i:.3f}")

    def output(self, ch, on):
        self.write(f"OUTP CH{ch},{'ON' if on else 'OFF'}")

    def channel_state(self, ch):
        sv, si = self.qfloat(f"CH{ch}:VOLT?"), self.qfloat(f"CH{ch}:CURR?")
        mv, mi = self.qfloat(f"MEAS:VOLT? CH{ch}"), self.qfloat(f"MEAS:CURR? CH{ch}")
        on, st = None, None
        try:
            bits = int(self.query("SYST:STAT?").strip(), 16)
            on = bool(bits >> (3 + ch) & 1)                  # bit4 = CH1 输出，bit5 = CH2 输出
            st = "CC" if (bits >> (ch - 1) & 1) else "CV"   # bit0 = CH1 CC，bit1 = CH2 CC
        except Exception:
            self.recover()
        status = judge_status(bool(on), sv, si, mv, mi)
        if on and st:
            status = st
        return {"set_v": sv, "set_i": si, "meas_v": mv, "meas_i": mi, "on": bool(on), "ovp": None,
                "ocp_on": False, "ov": False, "oc": False, "status": status}


# =============================================================================
class GenericSCPI(KeysightE36300):
    family = "通用 SCPI（兼容模式）"

    def ratings(self):
        return [("CH1", 30.0, 3.0), ("CH2", 30.0, 3.0), ("CH3", 6.0, 3.0)]

    def init(self):
        super().init()
        # 用 MAX 查询试探实际通道数：查不到的通道去掉
        keep = []
        for i, c in enumerate(self.channels, start=1):
            if self.qfloat(f"VOLT? MAX,(@{i})") is not None:
                keep.append(c)
        if keep:
            self.channels = keep


DRIVERS = [KeysightE36300, KeysightE3631, RigolDP, SiglentSPD]


def pick_driver(idn):
    for cls in DRIVERS:
        if cls.match(idn):
            return cls
    return GenericSCPI if PSU_HINT.search(idn) else None


def find_psus(rm):
    """扫描 USB / 网络上的直流电源；返回 [(地址, IDN, 驱动类)]。不会去问示波器（UNI-T 示波器的 USB 怕打扰）"""
    found = []
    for r in rm.list_resources():
        if not r.upper().startswith(("USB", "TCPIP")):
            continue
        if "0x5656::0x0832" in r.lower().replace("0X", "0x"):     # UNI-T UTD2000 示波器：不打扰
            continue
        idn = gen.identify(rm, r)
        if idn and pick_driver(idn):
            found.append((r, idn, pick_driver(idn)))
    return found


def connect(rm, addr, idn, cls):
    inst = rm.open_resource(addr)
    inst.timeout = 5000
    d = cls(inst, idn)
    d.init()
    d.go_local()
    return d
