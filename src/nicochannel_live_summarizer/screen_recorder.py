"""screen_recorder: ffmpeg の ddagrab で配信画面を録画する。

音声録音と同様、「画面に実際に表示されているもの」をキャプチャする方式に限定しており、
配信の暗号化ストリームには一切アクセスしない。

Windows の Desktop Duplication API (ddagrab) を使い、エンコードは GPU の
NVENC に任せるため CPU 負荷が小さく、文字起こしと同時に動かしても支障が出にくい。

注意: ffmpeg 単体では Windows のシステム音声を取得できない
（DirectShow にループバック入力が現れないため）。そのため音声は recorder.py が
別プロセスで録音し、最後に ffmpeg_tools.mux_audio_video() で結合する。
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .ffmpeg_tools import find_ffmpeg

logger = logging.getLogger(__name__)

# 起動直後に落ちていないかを確認するまでの待ち時間（秒）
STARTUP_CHECK_SEC = 2.5

# ffmpeg に 'q' を送ってから強制終了するまでの猶予（秒）
GRACEFUL_STOP_SEC = 20.0

_RESOLUTION_PATTERN = re.compile(r"(\d{3,5})x(\d{3,5})")


class ScreenRecorderError(RuntimeError):
    """画面録画に関する回復不能なエラー。"""


@dataclass
class DisplayInfo:
    """ddagrab で取得できるディスプレイの情報。"""

    index: int
    width: int
    height: int


def probe_displays(max_index: int = 4) -> list[DisplayInfo]:
    """録画対象にできるディスプレイを調べる。

    ごく短時間だけキャプチャを試み、解像度だけを読み取る。
    映像はどこにも保存しない（出力先は null）。
    """
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise ScreenRecorderError("ffmpeg が見つかりません。")

    displays: list[DisplayInfo] = []
    for index in range(max_index):
        try:
            result = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-f", "lavfi",
                    "-i", f"ddagrab=output_idx={index}:framerate=5",
                    "-t", "0.3",
                    "-f", "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            break

        if result.returncode != 0:
            # 存在しないディスプレイ番号に達したら打ち切る
            break

        match = None
        for line in result.stderr.splitlines():
            if "Stream #0:0" in line:
                match = _RESOLUTION_PATTERN.search(line)
                break
        if match is None:
            continue
        displays.append(
            DisplayInfo(index=index, width=int(match.group(1)), height=int(match.group(2)))
        )

    return displays


class ScreenRecorder:
    """ffmpeg を子プロセスとして起動し、画面を録画する。"""

    def __init__(
        self,
        output_path: Path,
        display_index: int = 0,
        height: int = 720,
        framerate: int = 30,
        quality_cq: int = 30,
        crop: str = "",
        encoder: str = "auto",
    ) -> None:
        self.output_path = output_path
        self.display_index = display_index
        self.height = height
        self.framerate = framerate
        self.quality_cq = quality_cq
        self.crop = crop.strip()
        self.encoder = encoder

        self._process: subprocess.Popen | None = None
        # 停止を要求したか（想定内の終了と、途中で落ちたのを区別するため）
        self._stop_requested = False
        # 途中で落ちたことを既に警告したか（同じ警告を繰り返さない）
        self._death_reported = False

    # --- 状態参照 -----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """ffmpeg プロセスが動作中かを返す。"""
        return self._process is not None and self._process.poll() is None

    def check_alive(self) -> bool:
        """録画が続いているかを確認し、途中で落ちていれば警告する。

        画面のロック（Win+L）、UAC の昇格ダイアログ、解像度の変更などで
        Desktop Duplication が画面を取得できなくなると ffmpeg が終了する。
        そのまま気づかず録り続けることがないよう、最初の1回だけ理由を知らせる。
        自動での再開は行わない（録画済みのファイルを壊さないため）。
        """
        if self._process is None or self._stop_requested:
            return False
        if self._process.poll() is None:
            return True

        if not self._death_reported:
            self._death_reported = True
            logger.warning(
                "画面録画が途中で停止しました（ffmpeg 終了コード %s）。"
                "画面のロック、UAC ダイアログ、解像度の変更などで"
                "画面を取得できなくなった可能性があります。"
                "ここまでの映像は保存されますが、以降は記録されません。",
                self._process.returncode,
            )
            detail = self._read_stderr()
            if detail:
                logger.warning("  ffmpeg のエラー出力: %s", detail)
        return False

    def _read_stderr(self) -> str:
        """終了した ffmpeg のエラー出力（末尾のみ）を返す。無ければ空文字。"""
        stream = self._process.stderr if self._process is not None else None
        if stream is None:
            return ""
        try:
            message = stream.read().decode("utf-8", errors="replace").strip()
        except (OSError, ValueError):
            return ""
        return message[-400:]

    # --- コマンド組み立て ---------------------------------------------------

    def _build_filters(self) -> str:
        """映像フィルタのチェーンを組み立てる。

        ddagrab は GPU 上のフレームを返すので、いったん CPU に降ろしてから
        切り出し・縮小する。GPU 上での縮小 (scale_cuda) はこの環境では
        動作しなかったため使用しない。
        """
        filters = ["hwdownload", "format=bgra"]
        if self.crop:
            filters.append(f"crop={self.crop}")
        # 幅は縦横比を保った偶数に自動調整する
        filters.append(f"scale=-2:{self.height}")
        return ",".join(filters)

    def _build_command(self, ffmpeg: str, encoder: str) -> list[str]:
        """ffmpeg のコマンドラインを組み立てる。"""
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-f", "lavfi",
            "-i", f"ddagrab=output_idx={self.display_index}:framerate={self.framerate}",
            "-vf", self._build_filters(),
            "-pix_fmt", "yuv420p",
        ]

        if encoder == "h264_nvenc":
            command += [
                "-c:v", "h264_nvenc",
                "-preset", "p4",
                "-rc", "vbr",
                "-cq", str(self.quality_cq),
            ]
        else:
            command += [
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", str(self.quality_cq),
            ]

        command += [
            # 途中で強制終了しても再生できるよう、フラグメント化して書き出す
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            str(self.output_path),
        ]
        return command

    # --- 制御 ---------------------------------------------------------------

    def start(self) -> None:
        """録画を開始する。NVENC が使えない場合は libx264 にフォールバックする。"""
        if self.is_running:
            raise ScreenRecorderError("すでに録画中です。")

        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            raise ScreenRecorderError(
                "ffmpeg が見つからないため画面録画を開始できません。"
            )

        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        candidates = (
            ["h264_nvenc", "libx264"] if self.encoder == "auto" else [self.encoder]
        )
        errors: list[str] = []

        for encoder in candidates:
            command = self._build_command(ffmpeg, encoder)
            logger.debug("画面録画コマンド: %s", " ".join(command))
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            # 起動直後に落ちていないかを確認する
            time.sleep(STARTUP_CHECK_SEC)
            if process.poll() is None:
                self._process = process
                self._stop_requested = False
                self._death_reported = False
                logger.info(
                    "画面録画を開始しました（ディスプレイ %d / %dp / %s）",
                    self.display_index,
                    self.height,
                    encoder,
                )
                return

            stderr = (process.stderr.read() if process.stderr else b"") or b""
            message = stderr.decode("utf-8", errors="replace").strip()[-400:]
            errors.append(f"{encoder}: {message}")
            logger.warning("エンコーダ %s では開始できませんでした。", encoder)

        raise ScreenRecorderError(
            "画面録画を開始できませんでした。\n" + "\n".join(errors)
        )

    def request_stop(self) -> None:
        """停止を要求する（待たずにすぐ戻る）。

        ffmpeg の標準入力に 'q' を送ると、書きかけのファイルを正しく
        閉じてから終了してくれる。

        音声録音との同期を保つため、停止の「合図」と「終了待ち」を分けている。
        合図だけ先に両方へ送っておき、そのあとで終了を待つことで、
        映像と音声の終端をほぼ揃えられる。
        """
        self._stop_requested = True
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            if process.stdin is not None:
                process.stdin.write(b"q")
                process.stdin.flush()
                process.stdin.close()
        except (OSError, ValueError):
            pass

    def wait_stop(self) -> bool:
        """停止要求後の終了を待つ。正常に終了できたら True を返す。"""
        process = self._process
        if process is None:
            return False

        graceful = True
        try:
            process.wait(timeout=GRACEFUL_STOP_SEC)
        except subprocess.TimeoutExpired:
            logger.warning("画面録画が時間内に終了しなかったため強制停止します。")
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            graceful = False

        self._process = None

        if not self.output_path.is_file() or self.output_path.stat().st_size == 0:
            logger.error("画面録画のファイルが空です。")
            return False

        size_mb = self.output_path.stat().st_size / 1024 / 1024
        logger.info("画面録画を終了しました（%.1f MB）", size_mb)
        return graceful

    def stop(self) -> bool:
        """停止を要求し、終了まで待つ。"""
        self.request_stop()
        return self.wait_stop()
