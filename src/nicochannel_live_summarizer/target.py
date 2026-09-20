"""target: 文字起こし・要約の「処理対象」を統一的に扱う。

対象には2種類ある。

  - アーカイブフォルダ（配信を録音したもの。meta.json で状態を管理する）
  - 単体の音声・動画ファイル（手持ちのファイル。ファイルの有無で状態を判断する）

両者は「出力先」と「実行済みかの判定方法」だけが違い、
文字起こし・要約の処理内容そのものは同じ。
その差分をこのクラスに閉じ込めることで、パイプライン側は
対象の種類を意識せずに済むようにしている。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .archive import JST, ArchiveEntry

TRANSCRIPT_JSON_SUFFIX = ".transcript.json"


@dataclass
class ProcessTarget:
    """文字起こし・要約の対象。"""

    title: str
    date_text: str
    transcript_md: Path
    transcript_json: Path
    summary_path: Path
    # 文字起こしの入力。要約だけを行う場合は None のこともある。
    media_path: Path | None = None
    # アーカイブフォルダの場合のみ。状態を meta.json に記録する。
    entry: ArchiveEntry | None = None

    # --- 実行済みかの判定 ---------------------------------------------------

    @property
    def is_transcribed(self) -> bool:
        """すでに文字起こし済みか。"""
        if self.entry is not None:
            return bool(self.entry.meta.get("transcribed"))
        return self.transcript_json.is_file()

    @property
    def is_summarized(self) -> bool:
        """すでに要約済みか。"""
        if self.entry is not None:
            return bool(self.entry.meta.get("summarized"))
        return self.summary_path.is_file()

    # --- 実行結果の記録 -----------------------------------------------------

    def record_transcribed(self, model: str, device: str, chars: int) -> None:
        """文字起こしの完了をメタデータに記録する（アーカイブのみ）。"""
        if self.entry is None:
            return
        self.entry.update_meta(
            transcribed=True,
            transcript_model=model,
            transcript_device=device,
            transcript_chars=chars,
        )

    def record_summarized(
        self, model: str, input_tokens: int, output_tokens: int
    ) -> None:
        """要約の完了をメタデータに記録する（アーカイブのみ）。"""
        if self.entry is None:
            return
        self.entry.update_meta(
            summarized=True,
            summary_model=model,
            summary_input_tokens=input_tokens,
            summary_output_tokens=output_tokens,
        )


def from_entry(entry: ArchiveEntry) -> ProcessTarget:
    """アーカイブフォルダから処理対象を組み立てる。"""
    started_at = entry.started_at
    return ProcessTarget(
        title=entry.title,
        date_text=started_at.strftime("%Y-%m-%d %H:%M") if started_at else "不明",
        transcript_md=entry.transcript_md_path,
        transcript_json=entry.transcript_json_path,
        summary_path=entry.summary_path,
        media_path=entry.find_audio(),
        entry=entry,
    )


def from_media(
    media_path: Path,
    output_dir: Path | None = None,
    title: str | None = None,
    output_stem: str | None = None,
) -> ProcessTarget:
    """単体の音声・動画ファイルから処理対象を組み立てる。

    `*.transcript.json` を直接渡された場合は、それを文字起こし結果とみなし、
    元のファイル名から出力先を決める（要約だけをやり直す用途）。
    """
    is_transcript = media_path.name.endswith(TRANSCRIPT_JSON_SUFFIX)
    if is_transcript:
        stem = media_path.name[: -len(TRANSCRIPT_JSON_SUFFIX)]
    else:
        stem = output_stem or media_path.stem

    destination = output_dir or media_path.parent
    destination.mkdir(parents=True, exist_ok=True)

    # 日時は、要約に添える情報としてファイルの更新日時を使う
    date_text = datetime.fromtimestamp(
        media_path.stat().st_mtime, tz=JST
    ).strftime("%Y-%m-%d %H:%M")

    return ProcessTarget(
        title=title or stem,
        date_text=date_text,
        transcript_md=destination / f"{stem}.transcript.md",
        transcript_json=destination / f"{stem}{TRANSCRIPT_JSON_SUFFIX}",
        summary_path=destination / f"{stem}.summary.md",
        media_path=None if is_transcript else media_path,
        entry=None,
    )
