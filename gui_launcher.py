"""GUI をダブルクリックで起動するための入口。

gui.bat やデスクトップのショートカットから pythonw.exe で直接呼ばれる。
その場合 PYTHONPATH を渡せないため、ここで src をインポートパスに加える。

コマンドから使う場合は `.\run.ps1 gui` でも同じ画面が開く。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from nicochannel_live_summarizer.gui import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
