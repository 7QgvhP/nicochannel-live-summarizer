"""ffmpeg を使う共通処理（実行ファイルの探索、音声変換、映像と音声の結合）。"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


@lru_cache(maxsize=None)
def find_ffmpeg(name: str = "ffmpeg") -> str | None:
    """ffmpeg / ffprobe の実行ファイルパスを返す。見つからなければ None。

    録音・録画・変換のたびに呼ばれるが、実行中に場所が変わることはないため
    結果を再利用する（見つからない場合のフォルダ探索が毎回走るのを防ぐ）。

    winget などでインストール直後は、既に起動しているターミナルの PATH に
    反映されていないことがある。そのため PATH に無い場合は既知の
    インストール先も探索する。
    """
    found = shutil.which(name)
    if found:
        return found

    if sys.platform != "win32":
        return None

    local_appdata = os.environ.get("LOCALAPPDATA", "")
    search_roots = [
        Path(local_appdata) / "Microsoft" / "WinGet" / "Links",
        Path(local_appdata) / "Microsoft" / "WinGet" / "Packages",
        Path(os.environ.get("ProgramData", "")) / "chocolatey" / "bin",
    ]
    for root in search_roots:
        if not root.is_dir():
            continue
        direct = root / f"{name}.exe"
        if direct.is_file():
            return str(direct)
        # Packages 配下はバージョン付きのサブフォルダに入っている
        for candidate in root.glob(f"*/**/bin/{name}.exe"):
            return str(candidate)
    return None


def ffmpeg_available() -> bool:
    """ffmpeg が利用可能かを返す。"""
    return find_ffmpeg() is not None


def _run(command: list[str], what: str) -> bool:
    """ffmpeg コマンドを実行し、成功したら True を返す。"""
    try:
        subprocess.run(command, check=True, capture_output=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        stderr = getattr(exc, "stderr", b"") or b""
        logger.warning(
            "%sに失敗しました: %s",
            what,
            stderr.decode("utf-8", errors="replace").strip()[-500:] or exc,
        )
        return False
    return True


def convert_to_opus(wav_path: Path, opus_path: Path, bitrate_kbps: int = 64) -> bool:
    """WAV を Opus に変換する。成功したら True を返す。

    雑談配信の音声なら 64kbps モノラルで実用上十分な音質を保てる。
    """
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        logger.warning("ffmpeg が見つからないため、WAV のまま保存します。")
        return False

    return _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(wav_path),
            "-ac", "1",  # モノラル化（音声認識にはステレオ不要でサイズが半分になる）
            "-c:a", "libopus",
            "-b:a", f"{bitrate_kbps}k",
            str(opus_path),
        ],
        "Opus への変換",
    )


def mux_audio_video(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    video_delay_sec: float = 0.0,
) -> bool:
    """映像と音声を1つの MP4 に結合する。

    映像と音声は別プロセスで録っているため開始時刻がわずかにずれる。
    video_delay_sec に「音声より映像がどれだけ遅れて始まったか」を渡すと、
    その分だけ映像をずらして同期を合わせる。

    映像は再エンコードせずコピーし、音声のみ MP4 互換の AAC に変換する。
    """
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        logger.warning("ffmpeg が見つからないため、映像と音声を結合できません。")
        return False

    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]

    # -itsoffset は対象の入力の直前に置く必要がある
    if video_delay_sec >= 0:
        command += ["-itsoffset", f"{video_delay_sec:.3f}", "-i", str(video_path)]
        command += ["-i", str(audio_path)]
    else:
        command += ["-i", str(video_path)]
        command += ["-itsoffset", f"{-video_delay_sec:.3f}", "-i", str(audio_path)]

    command += [
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "96k",
        "-movflags", "+faststart",
        str(output_path),
    ]
    return _run(command, "映像と音声の結合")


def probe_duration(path: Path) -> float | None:
    """メディアファイルの長さ（秒）を返す。取得できなければ None。"""
    ffprobe = find_ffmpeg("ffprobe")
    if ffprobe is None:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, OSError, ValueError):
        return None
