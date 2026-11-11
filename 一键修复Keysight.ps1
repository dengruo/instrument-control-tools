# 一键修复 Keysight 仪器通信软件（报 VI_ERROR_ALLOC、所有 USB 仪器都连不上、重启电脑也不好时用）
# 做的事：关掉三个控制程序 → 用安装包覆盖重装 Keysight IO Libraries → 重新应用「插上仪器后多等一会再问型号」的设置 → 重启电脑（必须）
$ErrorActionPreference = "Stop"
# 安装包：自动在当前用户的「下载」文件夹里找最新的 IOLibrariesSuite-*.exe（从 Keysight 官网下载，见 README）
$Installer = Get-ChildItem (Join-Path $env:USERPROFILE "Downloads") -Filter "IOLibrariesSuite-*.exe" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $Installer) { $Installer = Join-Path $env:USERPROFILE "Downloads\IOLibrariesSuite-*.exe" }
$Config = "C:\ProgramData\Keysight\Keysight IO Libraries\ds-config.yml"

# 需要管理员权限：不是管理员就以管理员身份重新打开自己（Windows 会弹一次「是否允许」）
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`""
    exit
}

function Done($msg, $ok) {
    Write-Host ""
    Write-Host $msg -ForegroundColor $(if ($ok) { "Green" } else { "Red" })
    Read-Host "按回车关闭这个窗口"
    exit
}

Write-Host "==== 一键修复 Keysight 仪器通信软件 ====" -ForegroundColor Cyan
Write-Host "开始前请确认：最近新插上、不回应的那台仪器的 USB 线已经拔掉。"
Write-Host ""

Write-Host "[1/3] 关闭三个控制程序……"
Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -in "信号发生器控制", "示波器控制", "直流电源控制" } |
    Stop-Process -Force -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process | Where-Object { $_.Name -match "python" -and $_.CommandLine -match "(gen|scope|psu)_gui" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

if (-not (Test-Path $Installer)) {
    Done ("找不到安装包：$Installer`n请到 Keysight 官网（keysight.com/find/iosuite）下载 IO Libraries Suite，" +
          "放到「下载」文件夹（文件名以 IOLibrariesSuite- 开头），再运行一次本脚本。") $false
}
Write-Host "使用安装包：$Installer"

Write-Host "[2/3] 覆盖重装 Keysight IO Libraries（约 6 分钟，期间不要关闭这个窗口）……"
$sw = [Diagnostics.Stopwatch]::StartNew()
$p = Start-Process -FilePath $Installer -ArgumentList "--mode", "unattended", "--unattendedmodeui", "minimal" -PassThru
while (-not $p.HasExited) { Start-Sleep -Seconds 10; Write-Host ("   已用 {0:N0} 秒……" -f $sw.Elapsed.TotalSeconds) }
if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { Done "重装失败（退出码 $($p.ExitCode)）。请把这个窗口截图保存下来，用于排查。" $false }

Write-Host "[3/3] 应用设置：仪器插上后等 5 秒再问型号，提问前先清空 USB 通道……"
if (Test-Path $Config) {
    $t = Get-Content $Config -Raw -Encoding UTF8
    $t = $t -replace "usbAddViClearForIdn:\s*\w+", "usbAddViClearForIdn: true" `
            -replace "usbVerifyWaitWhenDiscoveredInMs:\s*\d+", "usbVerifyWaitWhenDiscoveredInMs: 5000"
    [IO.File]::WriteAllText($Config, $t, (New-Object Text.UTF8Encoding($false)))
    Restart-Service -Name "KeysightNewInstrumentDiscoveryService" -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "软件重装完成。必须重启电脑才能真正修好（实测只重装、不重启，仪器仍然连不上）。" -ForegroundColor Yellow
Write-Host "重启前请保存好其他程序里的工作。"
$a = Read-Host "现在立即重启电脑吗？输入 Y 后按回车重启；直接按回车则稍后自己重启"
if ($a -match '^[Yy]') { Restart-Computer -Force }
Done "请稍后手动重启电脑。重启后再打开控制程序。" $true
