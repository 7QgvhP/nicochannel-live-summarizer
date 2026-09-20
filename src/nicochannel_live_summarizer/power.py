"""power: 監視・録画の実行中に PC がスリープしないようにする。

S3 スリープに入るとプロセスごと停止するため、配信の検知も録音もできない。
そこで Windows に「この作業が終わるまで眠らないでほしい」と申告する。

電源プランの設定そのものは変更しない。SetThreadExecutionState は
実行中のプロセスからの一時的な要求で、プロセスが終了すれば自動的に解除される。
そのため、ツールを止めれば通常どおりスリープするようになる。

参考: https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate
"""

from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

# SetThreadExecutionState に渡すフラグ
ES_CONTINUOUS = 0x80000000  # 次に指示するまで状態を維持する
ES_SYSTEM_REQUIRED = 0x00000001  # システムをスリープさせない
ES_DISPLAY_REQUIRED = 0x00000002  # ディスプレイを消さない

# 現在有効になっている要求を積んでおく。
# 監視中（システムのみ）の内側で録画中（システム＋画面）が始まるため、
# 内側が終わったときに外側の要求まで解除してしまわないようにする。
_requests: list[int] = []


def _is_supported() -> bool:
    """この機能が使える環境かを返す。"""
    return sys.platform == "win32"


def _combined_flags() -> int:
    """積まれている要求をまとめて1つのフラグにする。"""
    flags = ES_CONTINUOUS
    for request in _requests:
        flags |= request
    return flags


def _apply() -> bool:
    """現在の要求内容を Windows に反映する。"""
    if not _is_supported():
        return False
    try:
        result = ctypes.windll.kernel32.SetThreadExecutionState(_combined_flags())
    except (AttributeError, OSError) as exc:
        logger.debug("スリープ抑止の設定に失敗しました: %s", exc)
        return False
    # 戻り値 0 は失敗を意味する
    return result != 0


class KeepAwake:
    """with ブロックの間、PC がスリープしないようにする。

    keep_display=True にすると、ディスプレイの電源も切れないようにする。
    画面録画（Desktop Duplication）はディスプレイが消えると
    取得に失敗することがあるため、録画中は True にする。
    """

    def __init__(self, keep_display: bool = False, reason: str = "") -> None:
        self.keep_display = keep_display
        self.reason = reason
        self._flags = ES_SYSTEM_REQUIRED | (
            ES_DISPLAY_REQUIRED if keep_display else 0
        )
        self._active = False

    def __enter__(self) -> "KeepAwake":
        if not _is_supported():
            logger.debug("Windows 以外のためスリープ抑止は行いません。")
            return self

        _requests.append(self._flags)
        self._active = True
        if _apply():
            target = "スリープと画面オフ" if self.keep_display else "スリープ"
            logger.info(
                "%sを抑止します%s（ツール終了時に自動で元へ戻ります）。",
                target,
                f"（{self.reason}）" if self.reason else "",
            )
        else:
            logger.warning(
                "スリープの抑止に失敗しました。"
                "監視中にPCがスリープすると配信を録り逃す可能性があります。"
            )
        return self

    def __exit__(self, *exc_info) -> None:
        if not self._active:
            return
        try:
            _requests.remove(self._flags)
        except ValueError:
            pass
        self._active = False
        _apply()
        if not _requests:
            logger.info("スリープの抑止を解除しました。")
