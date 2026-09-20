"""lock: 録音を伴うコマンドの多重起動を防ぐ。

`watch` や `capture` が同時に複数動くと、同じアーカイブフォルダの
同じ `audio.wav` を複数のプロセスが奪い合い、録音が壊れる。
実際に、後始末で WAV を削除しようとしたときに
「別のプロセスが使用中です」というエラーが発生した。

実行中のプロセスIDをロックファイルに書いておき、
起動時にそれが生きているかを確認することで多重起動を防ぐ。
異常終了でロックが残った場合は、そのPIDが既に終了していることを
確認したうえで自動的に引き継ぐ。
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

LOCK_FILENAME = "recording.lock"

# OpenProcess に渡す権限（存在確認だけなので最小限）
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# GetExitCodeProcess が返す「まだ実行中」を表す値
_STILL_ACTIVE = 259


class AlreadyRunningError(RuntimeError):
    """すでに別のプロセスが録音中であることを表す。"""

    def __init__(self, pid: int, command: str) -> None:
        self.pid = pid
        self.command = command
        super().__init__(
            f"すでに別のプロセスが実行中です（PID={pid} / {command}）。\n"
            "録音が競合して壊れるため起動を中止しました。\n"
            "先に実行中のものを終了してください:\n"
            f"    Stop-Process -Id {pid} -Force"
        )


def _is_process_alive(pid: int) -> bool:
    """指定したPIDのプロセスが生きているかを返す。"""
    if pid <= 0:
        return False

    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            # 状態を取得できない場合は、安全側に倒して「生きている」とみなす
            return True
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class RecordingLock:
    """録音を伴うコマンドの排他ロック。

    with ブロックを抜けると自動的に解放される。
    """

    def __init__(self, lock_dir: Path, command: str) -> None:
        self.path = lock_dir / LOCK_FILENAME
        self.command = command
        self._acquired = False

    def _read_holder(self) -> tuple[int, str] | None:
        """ロックファイルから (PID, コマンド名) を読み取る。"""
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            return None
        pid_text, _, command = text.partition("\n")
        try:
            return int(pid_text), (command or "不明")
        except ValueError:
            return None

    def acquire(self) -> None:
        """ロックを取得する。取得できなければ AlreadyRunningError を投げる。"""
        holder = self._read_holder() if self.path.is_file() else None
        if holder is not None:
            pid, command = holder
            if pid != os.getpid() and _is_process_alive(pid):
                raise AlreadyRunningError(pid, command)
            logger.debug("前回のロックが残っていたため引き継ぎます（PID=%s）。", pid)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(f"{os.getpid()}\n{self.command}", encoding="utf-8")
        self._acquired = True

    def release(self) -> None:
        """ロックを解放する。他プロセスのロックは消さない。"""
        if not self._acquired:
            return
        holder = self._read_holder()
        if holder is not None and holder[0] == os.getpid():
            try:
                self.path.unlink()
            except OSError as exc:
                logger.debug("ロックファイルを削除できませんでした: %s", exc)
        self._acquired = False

    def __enter__(self) -> "RecordingLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()
