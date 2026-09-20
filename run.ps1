# NicoChannel Live Summarizer 起動スクリプト
#
# 使い方:
#   .\run.ps1 status      現在の配信状況を確認
#   .\run.ps1 devices     録音デバイスを確認
#   .\run.ps1 watch       配信の監視を開始
#
# venv の有効化を意識せずに実行できます。

$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error "仮想環境が見つかりません: $python`nセットアップ手順は README.md を参照してください。"
    exit 1
}

# src レイアウトのためインポートパスを通す
$env:PYTHONPATH = Join-Path $root "src"

# 日本語が化けないよう UTF-8 で出力する
$env:PYTHONIOENCODING = "utf-8"

& $python -m nicochannel_live_summarizer @args
exit $LASTEXITCODE
