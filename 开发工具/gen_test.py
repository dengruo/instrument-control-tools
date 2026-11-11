# -*- coding: utf-8 -*-
"""
Keysight 33500B USB 控制测试脚本
功能：自动找到信号发生器 -> 读取型号 -> CH1 输出 1 kHz、1 Vpp 正弦波（High Z）

运行前准备：
  1. 安装 Keysight IO Libraries Suite（VISA 驱动）
  2. pip install pyvisa
  3. 用 USB-A 转 USB-B 线连接电脑和信号发生器【背面】的方口 USB
运行：python gen_test.py
"""
import os
import sys

# Python 3.8+ 不再按 PATH 查找 DLL 依赖，需手动告诉它 Keysight VISA 的 DLL 目录，
# 否则会报 VI_ERROR_LIBRARY_NFOUND
for _d in (r"C:\Program Files\Keysight\IO Libraries Suite\bin",
           r"C:\Program Files\IVI Foundation\VISA\Win64\Bin",
           r"C:\Program Files\IVI Foundation\VISA\Win64\ktvisa\ktbin"):
    if os.path.isdir(_d):
        os.add_dll_directory(_d)

import pyvisa  # noqa: E402

KEYSIGHT_VIDS = ("0x0957", "0x2A8D")  # Agilent / Keysight 的 USB 厂商 ID


def find_generator(rm):
    resources = rm.list_resources()
    print("检测到的仪器：", resources if resources else "（无）")
    for r in resources:
        if r.upper().startswith("USB") and any(v.upper() in r.upper() for v in KEYSIGHT_VIDS):
            return r
    return None


def main():
    # 直接使用 Keysight 的 VISA 库（这台电脑上默认路由 visa32/visa64 加载会失败）
    ktvisa = r"C:\Windows\System32\ktvisa32.dll"
    try:
        rm = pyvisa.ResourceManager(ktvisa) if os.path.isfile(ktvisa) else pyvisa.ResourceManager()
    except Exception as e:
        print("打开 VISA 失败，请确认已安装 Keysight IO Libraries Suite：", e)
        sys.exit(1)

    addr = find_generator(rm)
    if addr is None:
        print("没有找到 Keysight 信号发生器。请检查：USB 线是否插在仪器背面、仪器是否开机、"
              "Connection Expert 里能否看到它。")
        sys.exit(1)

    gen = rm.open_resource(addr)
    gen.timeout = 5000  # 毫秒
    print("已连接：", addr)
    print("型号：", gen.query("*IDN?").strip())

    gen.write("*RST")                # 恢复默认
    gen.write("*CLS")                # 清除错误队列
    gen.write("OUTP1:LOAD INF")      # 负载 High Z（接示波器 / 电路时用）
    gen.write("SOUR1:FUNC SIN")      # 正弦波
    gen.write("SOUR1:FREQ 1000")     # 1000 Hz
    gen.write("SOUR1:VOLT 1")        # 1 Vpp
    gen.write("SOUR1:VOLT:OFFS 0")   # 偏置 0 V
    gen.write("OUTP1 ON")            # 打开 CH1 输出

    err = gen.query("SYST:ERR?").strip()
    print("仪器错误队列：", err)     # 正常应为 +0,"No error"
    print("完成：CH1 正在输出 1 kHz / 1 Vpp 正弦波。按仪器上的 System/Local 可回到手动操作。")
    gen.close()


if __name__ == "__main__":
    main()
