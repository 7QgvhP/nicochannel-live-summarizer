# NicoChannel Live Summarizer - デスクトップにショートカットを作る
#
#   .\create-shortcut.ps1
#
# gui.bat を直接ダブルクリックしても起動できますが、
# ショートカットを作るとデスクトップから 1 クリックで開けます。
# また、bat を直接開いたときに一瞬出る黒い窓も表示されません
# （pythonw.exe を直接呼ぶため）。

$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $pythonw)) {
    Write-Error "仮想環境が見つかりません: $pythonw`nセットアップ手順は README.md を参照してください。"
    exit 1
}

$desktop = [Environment]::GetFolderPath("Desktop")
$linkPath = Join-Path $desktop "NicoChannel Live Summarizer.lnk"

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($linkPath)
$link.TargetPath = $pythonw
$link.Arguments = '"' + (Join-Path $root "gui_launcher.py") + '"'
$link.WorkingDirectory = $root
$link.Description = "ニコニコチャンネルプラスの配信を録画・文字起こし・要約するツール"
# python.exe のアイコンを流用する（専用アイコンは用意していない）
$link.IconLocation = (Join-Path $root ".venv\Scripts\python.exe") + ",0"
$link.Save()

Write-Host "ショートカットを作成しました:"
Write-Host "  $linkPath"
Write-Host ""
Write-Host "デスクトップのアイコンをダブルクリックすると操作画面が開きます。"
