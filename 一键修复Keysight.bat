@echo off
chcp 65001 >nul
rem 双击运行：修复 Keysight 仪器通信软件（所有 USB 仪器报 VI_ERROR_ALLOC、连不上时用）。详细步骤见同名 .ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0一键修复Keysight.ps1"
