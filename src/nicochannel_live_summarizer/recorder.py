"""recorder: Windows の WASAPI ループバックでシステム音声を録音する。

「画面で実際に再生されている音声」をキャプチャする方式に限定しており、
配信の暗号化ストリームには一切アクセスしない。
PyAudioWPatch を使うことで、VB-Cable 等の仮想オーディオデバイスを
別途インストールしなくても既定の再生デバイスをそのまま録音できる。
"""

from __future__ import annotations

import logging
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import pyaudiowpatch as pyaudio

logger = logging.getLogger(__name__)

# 1回の読み取りで取得する最大フレーム数。
CHUNK_FRAMES = 2048

# データが無いときのポーリング間隔（秒）。
POLL_SLEEP_SEC = 0.02

# WASAPI ループバックは「何も再生されていない」間、パケットを一切返さない。
# 実時間よりこの秒数以上遅れたら無音を書き足し、タイムラインのずれを防ぐ。
SILENCE_PAD_THRESHOLD_SEC = 1.0

# 一度に書き足す無音の上限（秒）。長い無音でも小分けに埋める。
SILENCE_PAD_CHUNK_SEC = 2.0


class RecorderError(RuntimeError):
    """録音に関する回復不能なエラー。"""


@dataclass
class DeviceInfo:
    """録音に使えるループバックデバイスの情報。"""

    index: int
    name: str
    channels: int
    sample_rate: int
    is_default: bool


def list_loopback_devices() -> list[DeviceInfo]:
    """利用可能なループバックデバイスの一覧を返す。"""
    devices: list[DeviceInfo] = []
    with pyaudio.PyAudio() as audio:
        default_name = ""
        try:
            wasapi = audio.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_name = audio.get_device_info_by_index(
                wasapi["defaultOutputDevice"]
            )["name"]
        except (OSError, KeyError):
            logger.warning("既定の再生デバイスを取得できませんでした。")

        for device in audio.get_loopback_device_info_generator():
            devices.append(
                DeviceInfo(
                    index=int(device["index"]),
                    name=str(device["name"]),
                    channels=int(device["maxInputChannels"]),
                    sample_rate=int(device["defaultSampleRate"]),
                    is_default=bool(default_name and default_name in str(device["name"])),
                )
            )
    return devices


def _select_device(audio: pyaudio.PyAudio, device_name: str = "") -> dict:
    """録音に使うループバックデバイスを決定する。

    device_name が指定されていれば名前の部分一致で、
    指定がなければ「既定の再生デバイス」に対応するループバックを選ぶ。
    """
    loopbacks = list(audio.get_loopback_device_info_generator())
    if not loopbacks:
        raise RecorderError(
            "ループバックデバイスが見つかりませんでした。"
            "サウンド設定で再生デバイスが有効になっているか確認してください。"
        )

    if device_name:
        for device in loopbacks:
            if device_name.lower() in str(device["name"]).lower():
                return device
        available = ", ".join(str(d["name"]) for d in loopbacks)
        raise RecorderError(
            f"指定されたデバイスが見つかりません: {device_name!r}\n"
            f"利用可能: {available}"
        )

    try:
        wasapi = audio.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_output = audio.get_device_info_by_index(wasapi["defaultOutputDevice"])
    except (OSError, KeyError) as exc:
        logger.warning("既定の再生デバイスの取得に失敗しました: %s", exc)
        return loopbacks[0]

    for device in loopbacks:
        if str(default_output["name"]) in str(device["name"]):
            return device

    logger.warning(
        "既定の再生デバイス (%s) に対応するループバックが見つからないため、"
        "先頭のデバイスを使用します。",
        default_output["name"],
    )
    return loopbacks[0]


class SystemAudioRecorder:
    """システム音声をバックグラウンドスレッドで WAV に録音する。"""

    def __init__(
        self,
        output_path: Path,
        device_name: str = "",
        max_seconds: float = 8 * 3600,
    ) -> None:
        self.output_path = output_path
        self.device_name = device_name
        self.max_seconds = max_seconds

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None
        self._started_at: float | None = None
        self._frames_written = 0
        self._silence_frames = 0
        # 実際に音声データが届いた最後の時刻（無音の埋め合わせは含めない）
        self._last_audio_at: float | None = None
        self._sample_rate = 0
        self._channels = 0

    # --- 状態参照 -----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """録音スレッドが動作中かを返す。"""
        return self._thread is not None and self._thread.is_alive()

    @property
    def elapsed_seconds(self) -> float:
        """録音開始からの経過秒数。"""
        if self._started_at is None:
            return 0.0
        return time.monotonic() - self._started_at

    @property
    def recorded_seconds(self) -> float:
        """実際に書き込まれた音声の長さ（秒）。"""
        if not self._sample_rate:
            return 0.0
        return self._frames_written / self._sample_rate

    @property
    def seconds_since_audio(self) -> float:
        """最後に実際の音声が届いてからの経過秒数。

        まだ一度も届いていない場合は、録音開始からの経過秒数を返す。
        ブラウザの再生が止まっているとこの値が伸び続けるため、
        録音中に異常へ気づくための手がかりになる。
        """
        if self._started_at is None:
            return 0.0
        reference = self._last_audio_at or self._started_at
        return time.monotonic() - reference

    @property
    def has_received_audio(self) -> bool:
        """一度でも実際の音声が届いたか。"""
        return self._last_audio_at is not None

    @property
    def silence_ratio(self) -> float:
        """録音全体のうち、無音で埋めた割合（0.0〜1.0）。

        1.0 に近い場合は「録音デバイスの選択が誤っている」か
        「配信を再生していない」可能性が高い。
        """
        if not self._frames_written:
            return 0.0
        return self._silence_frames / self._frames_written

    # --- 制御 ---------------------------------------------------------------

    def start(self) -> None:
        """録音を開始する。"""
        if self.is_running:
            raise RecorderError("すでに録音中です。")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        # 前回の結果が混ざらないよう、統計もまとめて初期化する
        self._stop_event.clear()
        self._error = None
        self._frames_written = 0
        self._silence_frames = 0
        self._last_audio_at = None
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, name="system-audio-recorder", daemon=True
        )
        self._thread.start()

        # デバイスのオープンに失敗した場合は、すぐに例外として呼び出し元へ返す
        time.sleep(1.0)
        if self._error is not None:
            raise RecorderError(f"録音を開始できませんでした: {self._error}") from self._error

    def stop(self, timeout: float = 15.0) -> None:
        """録音を停止し、スレッドの終了を待つ。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._error is not None:
            raise RecorderError(f"録音中にエラーが発生しました: {self._error}") from self._error

    # --- 録音スレッド本体 ---------------------------------------------------

    def _run(self) -> None:
        """録音ループ。例外はスレッド外に持ち出すため self._error に保存する。"""
        try:
            with pyaudio.PyAudio() as audio:
                device = _select_device(audio, self.device_name)
                self._sample_rate = int(device["defaultSampleRate"])
                self._channels = int(device["maxInputChannels"])
                logger.info(
                    "録音デバイス: %s (%d ch / %d Hz)",
                    device["name"],
                    self._channels,
                    self._sample_rate,
                )

                stream = audio.open(
                    format=pyaudio.paInt16,
                    channels=self._channels,
                    rate=self._sample_rate,
                    frames_per_buffer=CHUNK_FRAMES,
                    input=True,
                    input_device_index=int(device["index"]),
                )
                try:
                    self._write_stream(stream)
                finally:
                    stream.stop_stream()
                    stream.close()
        except Exception as exc:  # noqa: BLE001 - スレッド外へ伝播させるため捕捉する
            logger.exception("録音スレッドで例外が発生しました")
            self._error = exc

    def _write_stream(self, stream: pyaudio.Stream) -> None:
        """ストリームから読み取り、WAVファイルへ書き出し続ける。

        WASAPI ループバックは何も再生されていない間はデータを返さず、
        ブロッキング読み取りだと停止できなくなる。そのため
        get_read_available() で残量を確認してから読み取る方式にしている。
        """
        sample_width = pyaudio.get_sample_size(pyaudio.paInt16)
        bytes_per_frame = self._channels * sample_width

        with wave.open(str(self.output_path), "wb") as wav:
            wav.setnchannels(self._channels)
            wav.setsampwidth(sample_width)
            wav.setframerate(self._sample_rate)

            while not self._stop_event.is_set():
                if self.elapsed_seconds > self.max_seconds:
                    logger.warning(
                        "最大録音時間 (%.1f 時間) に達したため録音を停止します。",
                        self.max_seconds / 3600,
                    )
                    break

                available = stream.get_read_available()
                if available > 0:
                    data = stream.read(
                        min(available, CHUNK_FRAMES), exception_on_overflow=False
                    )
                    wav.writeframes(data)
                    self._frames_written += len(data) // bytes_per_frame
                    self._last_audio_at = time.monotonic()
                else:
                    time.sleep(POLL_SLEEP_SEC)

                self._pad_silence_if_behind(wav, bytes_per_frame)

    def _pad_silence_if_behind(self, wav: wave.Wave_write, bytes_per_frame: int) -> None:
        """実時間より録音が遅れている分を無音で埋める。

        再生が止まっている間はループバックからデータが来ないため、
        そのままだと録音時間が実時間より短くなり、文字起こしの
        タイムスタンプが配信の経過時間とずれてしまう。
        """
        expected_frames = int(self.elapsed_seconds * self._sample_rate)
        lag_frames = expected_frames - self._frames_written
        if lag_frames <= self._sample_rate * SILENCE_PAD_THRESHOLD_SEC:
            return

        pad_frames = min(lag_frames, int(self._sample_rate * SILENCE_PAD_CHUNK_SEC))
        wav.writeframes(b"\x00" * (pad_frames * bytes_per_frame))
        self._frames_written += pad_frames
        self._silence_frames += pad_frames
