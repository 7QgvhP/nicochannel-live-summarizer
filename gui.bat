@echo off
rem NicoChannel Live Summarizer - GUI 起動用
rem このファイルをダブルクリックすると操作画面が開きます。
rem デスクトップにアイコンを置きたい場合は create-shortcut.ps1 を実行してください。

setlocal
set "ROOT=%~dp0"
set "PYW=%ROOT%.venv\Scripts\pythonw.exe"
set "PY=%ROOT%.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo 仮想環境が見つかりません:
    echo   %PY%
    echo.
    echo README.md のセットアップ手順を実行してください。
    pause
    exit /b 1
)

rem pythonw.exe はコンソールを開かないため、黒い窓が残らない
if exist "%PYW%" (
    start "" "%PYW%" "%ROOT%gui_launcher.py"
) else (
    start "" "%PY%" "%ROOT%gui_launcher.py"
)

endlocal
