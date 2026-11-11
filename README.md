# instrument-control-tools

用电脑控制实验室仪器的三个中文图形界面程序：**信号发生器**、**示波器**、**直流电源**。
Windows + Python（tkinter），通过 VISA（USB / 网络）和仪器通信，可以打包成独立的 exe。

> Three Chinese-language Windows GUIs (tkinter + PyVISA) for controlling a function generator,
> an oscilloscope and a DC power supply over USB/LAN, with auto-detected multi-brand drivers.

## 功能

| 程序 | 主要功能 |
|---|---|
| **信号发生器控制** | 波形（正弦/方波/锯齿/脉冲/噪声/直流/任意波）、频率、幅度、偏置、相位、占空比；两通道相位对齐；叠加干扰；上传任意波形 |
| **示波器控制** | 实时波形显示、16 项自动测量、两通道相位差和延迟、光标、XY / 数学运算 / FFT、截图和波形保存（CSV + PNG）；**与信号发生器联动自动扫频**（测幅频、相频特性） |
| **直流电源控制** | 每个通道大字显示实测电压和电流，以及 CV / CC / OV / OC 状态；直接输入电压和限流；OVP / OCP；全部打开 / 全部关闭；**示波器式滚动曲线**（只画正在输出的通道），可保存 CSV + PNG |

共同的设计：
- **连接时不改动仪器的任何设置**；直流电源打开输出前会弹窗确认（可关闭）。
- **每次通信完都把面板交还给仪器**，不用按 Local 就能直接在仪器上手动操作。
- 连接时用 `*IDN?` 识别厂商和型号，**自动选择驱动**。
- 同一个程序只能开一份，避免两份程序同时给同一台仪器发命令。
- 每个程序都有一个**本机后台控制接口**（可在界面上关掉），脚本或 AI 助手可以通过它操作正在运行的程序（见下文）。

## 支持的仪器

✅ = 已在真机上测试；其他是按编程手册写的，未实测，欢迎反馈。

| 类型 | 驱动 | 型号 |
|---|---|---|
| 信号发生器 | Keysight33500 | Keysight / Agilent 33500B、33600A 系列 ✅（33522B） |
| | Keysight33200 | 33120A / 33210A / 33220A / 33250A |
| | RigolDG | Rigol DG800 / DG900 / DG1000Z / DG2000 / DG4000 / DG5000 |
| | SiglentSDG | 鼎阳 SDG 系列 |
| | TektronixAFG | 泰克 AFG1000 / AFG2000 / AFG3000 / AFG31000 |
| 示波器 | UnitUTD | UNI-T UTD2000 / UTD2000CEX(+) 系列 ✅（UTD2202CEX+） |
| | RigolDS | Rigol DS1000Z / DS2000 / DS4000 / MSO5000 / DHO；UNI-T UPO / MSO |
| | KeysightIV | Keysight InfiniiVision（DSOX / MSOX / EDUX） |
| | SiglentSDS | Siglent SDS1000X-E / SDS1000CML / SDS2000X |
| | TektronixTBS | Tektronix TBS1000 / TBS2000 / MDO / MSO / DPO |
| 直流电源 | KeysightE36300 | Keysight E36311A / E36312A / E36313A ✅（E36313A）/ EDU36311A |
| | KeysightE3631 | Keysight / Agilent E3631A / E3646A / E3648A |
| | RigolDP | Rigol DP800 / DP900 系列（DP932U 的 USB 有问题，见“已知问题”） |
| | SiglentSPD | Siglent SPD3303X / SPD3303X-E / SPD3303C |

每类还有一个 `GenericSCPI` 兼容模式，用于其他品牌。驱动都在 `drivers.py`、`scope.py`、`psu.py` 里，
新增或修改某个型号时，照着已有的驱动类改就行。

## 运行

### 1. 安装仪器驱动（必需）
从 Keysight 官网下载并安装 **Keysight IO Libraries Suite**（免费）：
<https://www.keysight.com/find/iosuite>

本项目测试用的是 *IO Libraries Suite 2026 Update 1（21.4.204）*。
安装包属于 Keysight 的软件，请到官网下载，本仓库不提供。

### 2. 用 exe（不需要 Python）
用 `开发工具\重新打包exe.bat` 生成三个 exe（见下文），然后双击即可。

### 3. 用 Python 源代码运行
Python 3.10 以上（测试用的是 3.12）：

```bash
pip install -r requirements.txt
```

```bash
python 信号发生器/gen_gui.py
```

```bash
python 示波器/scope_gui.py
```

```bash
python 直流电源/psu_gui.py
```

示波器和直流电源程序会用到 `信号发生器` 文件夹里的 `drivers.py`、`remote_api.py`，**三个文件夹要放在同一个目录下**。

### 4. 打包成 exe
需要 `pip install pyinstaller`。双击 `开发工具\重新打包exe.bat`，大约 2 分钟后，三个 exe
（每个都有自己的图标）会生成在各自的文件夹里。打包前请先关掉正在运行的程序。
脚本默认用 PATH 里的 `python`，装在别处时请修改脚本开头的 `PY=` 那一行。

## 后台控制接口

每个程序启动后，会在本机开一个 HTTP 接口（只监听 127.0.0.1）：信号发生器 `17601`，示波器 `17602`，
直流电源 `17603`。程序右上角取消勾选“允许后台控制”就会关闭。直流电源通过后台打开输出时，
界面上仍然会弹出确认窗。

`开发工具/remote.py` 是命令行和 Python 客户端：

```bash
python 开发工具/remote.py scope
```

```bash
python 开发工具/remote.py gen apply ch=1 func=SIN freq=1000 amp=2
```

```bash
python 开发工具/remote.py scope measure ch=1 items='["FREQ","VPP"]'
```

```bash
python 开发工具/remote.py scope sweep f1=100 f2=100000 n=20 amp=2 cout=2
```

不带方法名运行时，会列出该程序支持的全部方法。

## 已知问题

### Keysight VISA 报 `VI_ERROR_ALLOC`，所有 USB 仪器都连不上
现象：所有 USB 仪器打开时都报 `VI_ERROR_ALLOC`（Insufficient system resources），**单独重启电脑、单独重装都不一定能恢复**。

实测原因：只要打开某台**不回应的 USB 仪器**卡住了，Keysight VISA 之后就会对所有 USB 仪器报这个错。
打开它的可能是 Keysight 自带的“插上就自动查询型号”，也可能是程序自己。
Keysight 的日志 `C:\ProgramData\Keysight\Keysight IO Libraries\ds-logs\usb-agent.log` 里，
插上仪器之后会出现 `timeout waiting for IDN result`。

恢复方法：双击 `一键修复Keysight.bat`（会自动找“下载”文件夹里的 `IOLibrariesSuite-*.exe`，覆盖重装），
**然后重启电脑**，两步都要做。三个程序扫描时如果遇到这个错误，会弹窗提示上面的步骤。

预防：仪器先开机、等它完全启动后再插 USB；新仪器一次只插一台。
修复脚本还会把 Keysight 的 `ds-config.yml` 改成“插上后等 5 秒、先清空通道再查询型号”
（`usbVerifyWaitWhenDiscoveredInMs: 5000`、`usbAddViClearForIdn: true`）。

### Rigol DP932U 不能用 USB 连接
实测 DP932U（USB PID `0xA4A8`，USB 报告的产品名是 “DP932S”）通过 Keysight VISA 打开时，`viOpen` 会一直卡住，
接着就会触发上面的 `VI_ERROR_ALLOC`。还没有查清是电源固件的问题还是驱动的问题。请改用网线（LXI）连接。

### UNI-T UTD2000 示波器的怪癖
写在 `示波器/scope.py` 的开头，程序里都已经处理了。例如发 `:DISPlay:DATA?`（截屏）或者切换 `:WAVeform:FORMat`
会让它的 USB 卡死，只能给示波器断电重开。

## 目录结构

```
├─ 信号发生器/        gen_gui.py（界面）、drivers.py（各品牌驱动 + VISA 公共函数）、remote_api.py（后台接口）、gen.ico
├─ 示波器/            scope_gui.py、scope.py（驱动）、scope.ico
├─ 直流电源/          psu_gui.py、psu.py（驱动）、psu.ico
├─ 开发工具/          重新打包exe.bat、remote.py（后台接口客户端）、gen_test.py（最早的测试脚本）
├─ 一键修复Keysight.bat / .ps1
├─ 使用说明.txt       给最终使用者看的简短说明
└─ requirements.txt
```

## 许可证

[MIT](LICENSE)
