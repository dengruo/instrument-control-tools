@echo off
chcp 65001 >nul
rem （本文件在“开发工具”里，%~dp0..\ 就是 C:\测试工具）把三个程序重新打包成 exe（改过 .py 代码之后运行一次）。
rem 打包前请先关掉三个控制程序；每个大约 40 秒。exe 生成在各自的文件夹里，记录仍然存在旁边的“记录”文件夹。
rem Python 路径：默认用 PATH 里的 python；装在别处时改这一行，例如 set PY=C:\Anaconda3\python.exe
if not defined PY set PY=python
if exist C:\Anconda\python.exe set PY=C:\Anconda\python.exe
set W=%TEMP%\InstrumentToolsBuild
set EX=--exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide2 --exclude-module PySide6 --exclude-module IPython --exclude-module jupyter_client --exclude-module notebook --exclude-module scipy --exclude-module pandas --exclude-module sklearn --exclude-module tornado --exclude-module sympy --exclude-module numba --exclude-module torch --exclude-module tensorflow --exclude-module cv2 --exclude-module pytest --exclude-module sphinx
set COMMON=--noconfirm --onefile --windowed --hidden-import pyvisa.ctwrapper --paths "%~dp0..\信号发生器" %EX%

echo [1/3] 信号发生器控制.exe
"%PY%" -m PyInstaller %COMMON% --name 信号发生器控制 --icon "%~dp0..\信号发生器\gen.ico" --add-data "%~dp0..\信号发生器\gen.ico;." --distpath "%~dp0..\信号发生器" --workpath "%W%\gen" --specpath "%W%\gen" "%~dp0..\信号发生器\gen_gui.py" || goto :fail

echo [2/3] 示波器控制.exe
"%PY%" -m PyInstaller %COMMON% --name 示波器控制 --icon "%~dp0..\示波器\scope.ico" --add-data "%~dp0..\示波器\scope.ico;." --paths "%~dp0..\示波器" --distpath "%~dp0..\示波器" --workpath "%W%\scope" --specpath "%W%\scope" "%~dp0..\示波器\scope_gui.py" || goto :fail

echo [3/3] 直流电源控制.exe
"%PY%" -m PyInstaller %COMMON% --name 直流电源控制 --icon "%~dp0..\直流电源\psu.ico" --add-data "%~dp0..\直流电源\psu.ico;." --paths "%~dp0..\直流电源" --distpath "%~dp0..\直流电源" --workpath "%W%\psu" --specpath "%W%\psu" "%~dp0..\直流电源\psu_gui.py" || goto :fail

echo.
echo 三个 exe 都打包好了。
pause
exit /b 0

:fail
echo.
echo 打包失败，请把上面的报错截图发给我。
pause
exit /b 1
