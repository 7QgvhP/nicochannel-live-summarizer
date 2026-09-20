"""NicoChannel Live Summarizer.

ニコニコチャンネルプラスの配信の開始・終了を自動検知し、
システム音声（と、設定されていれば画面）を録画して配信ごとにアーカイブする
個人利用向けツール。過去のアーカイブ配信を手動で取り込むこともできる。

文字起こし（ローカル実行）と要約（Claude API）は自動では行わず、
`transcribe` / `summarize` コマンドでのみ実行する。
"""

DISPLAY_NAME = "NicoChannel Live Summarizer"

__version__ = "1.19.2"
