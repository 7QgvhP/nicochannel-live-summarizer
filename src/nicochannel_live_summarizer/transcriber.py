"""transcriber: faster-whisper による日本語のタイムスタンプ付き文字起こし。

すべてローカルで実行するため、音声が外部に送信されることはない。
GPU が使えない場合は自動的に CPU へフォールバックする。
"""

from __future__ import annotations

import json
import logging
import os
import site
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# pip で入れた NVIDIA ライブラリが置かれるサブディレクトリ
_NVIDIA_DLL_SUBDIRS = (
    "cublas/bin",
    "cudnn/bin",
    "cuda_nvrtc/bin",
)


def _register_cuda_dll_directories() -> None:
    """pip 版 NVIDIA ライブラリの DLL 検索パスを登録する。

    Windows では nvidia-cublas-cu12 等を pip で入れても DLL の場所が
    自動では解決されない。さらに CTranslate2 は cuBLAS / cuDNN を
    遅延ロード（LoadLibrary）で読み込むため、os.add_dll_directory() だけでは
    解決されず、PATH への追加も必要になる。
    """
    if sys.platform != "win32":
        return

    search_bases = list(site.getsitepackages())
    try:
        search_bases.append(site.getusersitepackages())
    except AttributeError:  # 環境によっては未定義
        pass

    found: list[Path] = []
    for base in search_bases:
        nvidia_root = Path(base) / "nvidia"
        if not nvidia_root.is_dir():
            continue
        for sub in _NVIDIA_DLL_SUBDIRS:
            path = nvidia_root / sub
            if path.is_dir() and path not in found:
                found.append(path)

    if not found:
        logger.debug("pip 版 NVIDIA ライブラリは見つかりませんでした。")
        return

    for path in found:
        try:
            os.add_dll_directory(str(path))
        except OSError:
            logger.debug("DLL 検索パスの追加に失敗しました: %s", path)

    # CTranslate2 の遅延ロードは PATH の通常検索順を使うため、そちらにも追加する
    current_path = os.environ.get("PATH", "")
    prefix = os.pathsep.join(str(path) for path in found)
    if prefix not in current_path:
        os.environ["PATH"] = prefix + os.pathsep + current_path

    logger.debug("CUDA DLL パスを登録しました: %s", prefix)


@dataclass
class Segment:
    """文字起こしの1区間。"""

    start: float
    end: float
    text: str


@dataclass
class TranscriptionResult:
    """文字起こしの結果全体。"""

    segments: list[Segment]
    language: str
    duration: float
    model: str
    device: str

    @property
    def full_text(self) -> str:
        """タイムスタンプなしの全文を返す。"""
        return "".join(segment.text for segment in self.segments).strip()


def format_timestamp(seconds: float) -> str:
    """秒数を [HH:MM:SS] 形式に整形する。"""
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


class Transcriber:
    """faster-whisper モデルのラッパー。モデルは初回利用時に読み込む。"""

    def __init__(
        self,
        model_size: str = "large-v3",
        device: str = "cuda",
        compute_type: str = "float16",
        language: str = "ja",
        beam_size: int = 5,
        vad_filter: bool = True,
    ) -> None:
        self.model_size = model_size
        self.requested_device = device
        self.compute_type = compute_type
        self.language = language or None
        self.beam_size = beam_size
        self.vad_filter = vad_filter

        self._model = None
        self._actual_device = device

    def _load_model(self):
        """モデルを読み込む。GPU で失敗した場合は CPU にフォールバックする。"""
        if self._model is not None:
            return self._model

        from faster_whisper import WhisperModel

        _register_cuda_dll_directories()

        if self.requested_device == "cuda":
            try:
                logger.info(
                    "文字起こしモデルを読み込みます: %s (GPU / %s)",
                    self.model_size,
                    self.compute_type,
                )
                self._model = WhisperModel(
                    self.model_size,
                    device="cuda",
                    compute_type=self.compute_type,
                )
                self._actual_device = "cuda"
                return self._model
            except Exception as exc:  # noqa: BLE001 - CPUへのフォールバックを試みる
                logger.warning(
                    "GPU でのモデル読み込みに失敗したため CPU に切り替えます: %s", exc
                )

        logger.info("文字起こしモデルを読み込みます: %s (CPU / int8)", self.model_size)
        self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
        self._actual_device = "cpu"
        return self._model

    def transcribe(
        self,
        audio_path: Path,
        progress_callback: Callable[[float, float], None] | None = None,
    ) -> TranscriptionResult:
        """音声ファイルを文字起こしする。

        progress_callback には (現在位置の秒数, 音声全体の秒数) が渡される。
        """
        if not audio_path.is_file():
            raise FileNotFoundError(f"音声ファイルが見つかりません: {audio_path}")

        try:
            return self._run(self._load_model(), audio_path, progress_callback)
        except RuntimeError as exc:
            # CUDA ライブラリの不足はモデル構築時ではなく推論時に判明することがある
            if self._actual_device != "cuda":
                raise
            logger.warning(
                "GPU での推論に失敗したため CPU で再実行します: %s", exc
            )
            self._model = None
            self.requested_device = "cpu"
            return self._run(self._load_model(), audio_path, progress_callback)

    def _run(
        self,
        model,
        audio_path: Path,
        progress_callback: Callable[[float, float], None] | None,
    ) -> TranscriptionResult:
        """1回分の文字起こしを実行する。"""
        segment_iter, info = model.transcribe(
            str(audio_path),
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=self.vad_filter,
            # 同じ語の無限ループを抑制する
            condition_on_previous_text=False,
        )

        segments: list[Segment] = []
        for raw in segment_iter:
            text = raw.text.strip()
            if text:
                segments.append(Segment(start=raw.start, end=raw.end, text=text))
            if progress_callback is not None:
                progress_callback(raw.end, info.duration)

        return TranscriptionResult(
            segments=segments,
            language=info.language,
            duration=info.duration,
            model=self.model_size,
            device=self._actual_device,
        )


def write_transcript_files(
    result: TranscriptionResult, markdown_path: Path, json_path: Path, title: str
) -> None:
    """文字起こし結果を Markdown と JSON の両方で保存する。"""
    lines = [
        f"# 文字起こし: {title}",
        "",
        f"- 認識モデル: `{result.model}` ({result.device})",
        f"- 音声の長さ: {format_timestamp(result.duration)}",
        f"- 認識言語: {result.language}",
        f"- 区間数: {len(result.segments)}",
        "",
        "---",
        "",
    ]
    for segment in result.segments:
        lines.append(f"**[{format_timestamp(segment.start)}]** {segment.text}")
        lines.append("")

    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "model": result.model,
                "device": result.device,
                "language": result.language,
                "duration": result.duration,
                "segments": [asdict(segment) for segment in result.segments],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def load_transcript(json_path: Path) -> TranscriptionResult:
    """write_transcript_files が保存した JSON を読み戻す。"""
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return TranscriptionResult(
        segments=[Segment(**segment) for segment in data["segments"]],
        language=data.get("language", "ja"),
        duration=data.get("duration", 0.0),
        model=data.get("model", ""),
        device=data.get("device", ""),
    )


def build_timestamped_text(result: TranscriptionResult) -> str:
    """要約モデルに渡すためのタイムスタンプ付きテキストを組み立てる。"""
    return "\n".join(
        f"[{format_timestamp(segment.start)}] {segment.text}"
        for segment in result.segments
    )
