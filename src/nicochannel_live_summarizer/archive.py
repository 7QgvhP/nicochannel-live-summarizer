"""archive: 配信ごとのフォルダ管理と一覧インデックスの生成。

保存構成:
    archive/
      INDEX.md                                  ← 全配信の一覧（自動生成）
      2026-08-17_1200_配信タイトル_smQy99EP/
        meta.json         配信のメタデータと処理状況
        audio.opus        録音（保持期限を過ぎると自動削除）
        transcript.md     タイムスタンプ付き文字起こし
        transcript.json   文字起こしの生データ
        summary.md        要約
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from urllib.parse import quote
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")

META_FILENAME = "meta.json"
INDEX_FILENAME = "INDEX.md"
TRANSCRIPT_MD = "transcript.md"
TRANSCRIPT_JSON = "transcript.json"
SUMMARY_MD = "summary.md"

# Windows のファイル名に使えない文字
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# タイトルに含まれがちな絵文字などを除去した際に残る余分な空白
_MULTISPACE = re.compile(r"\s+")


def _link(target: str, label: str) -> str:
    """Markdown のリンクを組み立てる。

    フォルダ名には空白や括弧が入りうる。そのまま書くとリンクが途中で
    切れてしまうため、リンク先は URL エンコードする（/ は残す）。
    """
    return f"[{label}]({quote(target, safe='/')})"


def sanitize_for_filename(text: str, max_length: int = 60) -> str:
    """文字列を Windows のフォルダ名として安全な形に整える。"""
    normalized = unicodedata.normalize("NFKC", text)
    cleaned = _INVALID_FILENAME_CHARS.sub("", normalized)
    cleaned = _MULTISPACE.sub(" ", cleaned).strip()
    # Windows では末尾のピリオド・空白が使えない
    cleaned = cleaned.rstrip(". ")
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(". ")
    return cleaned or "untitled"


@dataclass
class ArchiveEntry:
    """1配信分のアーカイブフォルダ。"""

    directory: Path
    meta: dict = field(default_factory=dict)

    # --- ファイルパス -------------------------------------------------------

    @property
    def meta_path(self) -> Path:
        return self.directory / META_FILENAME

    @property
    def transcript_md_path(self) -> Path:
        return self.directory / TRANSCRIPT_MD

    @property
    def transcript_json_path(self) -> Path:
        return self.directory / TRANSCRIPT_JSON

    @property
    def summary_path(self) -> Path:
        return self.directory / SUMMARY_MD

    def audio_path(self, suffix: str) -> Path:
        return self.directory / f"audio{suffix}"

    def video_path(self, suffix: str = ".mp4") -> Path:
        return self.directory / f"video{suffix}"

    def find_audio(self) -> Path | None:
        """保存されている音声ファイルを探す（削除済みなら None）。"""
        for suffix in (".opus", ".wav", ".m4a", ".mp3"):
            candidate = self.audio_path(suffix)
            if candidate.is_file():
                return candidate
        return None

    def find_video(self) -> Path | None:
        """保存されている映像ファイルを探す（削除済みなら None）。"""
        for suffix in (".mp4", ".mkv"):
            candidate = self.video_path(suffix)
            if candidate.is_file():
                return candidate
        return None

    # --- メタデータ ---------------------------------------------------------

    def save_meta(self) -> None:
        """meta.json を書き出す。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        self.meta_path.write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def update_meta(self, **values) -> None:
        """メタデータを部分更新して保存する。"""
        self.meta.update(values)
        self.save_meta()

    @classmethod
    def load(cls, directory: Path) -> "ArchiveEntry | None":
        """既存のアーカイブフォルダを読み込む。"""
        meta_path = directory / META_FILENAME
        if not meta_path.is_file():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("meta.json を読めませんでした (%s): %s", directory.name, exc)
            return None
        return cls(directory=directory, meta=meta)

    # --- 表示用ヘルパ -------------------------------------------------------

    @property
    def title(self) -> str:
        return self.meta.get("title", "無題の配信")

    @property
    def started_at(self) -> datetime | None:
        value = self.meta.get("started_at")
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @property
    def has_summary(self) -> bool:
        return self.summary_path.is_file()

    @property
    def has_transcript(self) -> bool:
        return self.transcript_md_path.is_file()


class ArchiveManager:
    """アーカイブ全体を管理する。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def create_entry(
        self, title: str, started_at: datetime, content_code: str, page_url: str
    ) -> ArchiveEntry:
        """新しい配信用のフォルダを作成する。

        同じ配信を録り直した場合に備え、既存フォルダがあれば再利用する。
        """
        stamp = started_at.astimezone(JST).strftime("%Y-%m-%d_%H%M")
        name = f"{stamp}_{sanitize_for_filename(title)}_{content_code[:8]}"
        directory = self.root / name

        existing = ArchiveEntry.load(directory)
        if existing is not None:
            logger.info("既存のアーカイブフォルダを再利用します: %s", name)
            return existing

        entry = ArchiveEntry(
            directory=directory,
            meta={
                "title": title,
                "content_code": content_code,
                "page_url": page_url,
                "started_at": started_at.astimezone(JST).isoformat(),
                "finished_at": None,
                "duration_sec": None,
                "audio_file": None,
                "audio_deleted_at": None,
                "video_file": None,
                "video_deleted_at": None,
                "transcribed": False,
                "summarized": False,
                "created_at": datetime.now(JST).isoformat(),
            },
        )
        entry.save_meta()
        logger.info("アーカイブフォルダを作成しました: %s", directory)
        return entry

    def list_entries(self) -> list[ArchiveEntry]:
        """全アーカイブを新しい順に返す。"""
        entries = []
        for directory in self.root.iterdir():
            if not directory.is_dir():
                continue
            entry = ArchiveEntry.load(directory)
            if entry is not None:
                entries.append(entry)
        entries.sort(
            key=lambda e: e.started_at or datetime.min.replace(tzinfo=JST), reverse=True
        )
        return entries

    def write_index(self) -> Path:
        """INDEX.md を再生成する。"""
        entries = self.list_entries()
        lines = [
            "# 配信アーカイブ一覧",
            "",
            f"最終更新: {datetime.now(JST).strftime('%Y-%m-%d %H:%M')}　/　"
            f"全 {len(entries)} 件",
            "",
            "| 配信日時 | タイトル | 長さ | 要約 | 文字起こし | 音声 | 映像 |",
            "|---|---|---|---|---|---|---|",
        ]

        for entry in entries:
            started = entry.started_at
            date_text = started.strftime("%Y-%m-%d %H:%M") if started else "不明"
            duration = entry.meta.get("duration_sec")
            duration_text = format_duration(duration) if duration else "—"

            folder = entry.directory.name
            summary_cell = (
                _link(f"{folder}/{SUMMARY_MD}", "要約") if entry.has_summary else "—"
            )
            transcript_cell = (
                _link(f"{folder}/{TRANSCRIPT_MD}", "全文")
                if entry.has_transcript
                else "—"
            )
            audio_cell = _media_cell(
                entry.find_audio(), folder, "音声", entry.meta.get("audio_deleted_at")
            )
            video_cell = _media_cell(
                entry.find_video(), folder, "映像", entry.meta.get("video_deleted_at")
            )

            title = entry.title.replace("|", "\\|")
            lines.append(
                f"| {date_text} | {_link(folder + '/', title)} | {duration_text} | "
                f"{summary_cell} | {transcript_cell} | {audio_cell} | {video_cell} |"
            )

        lines.append("")
        index_path = self.root / INDEX_FILENAME
        index_path.write_text("\n".join(lines), encoding="utf-8")
        return index_path

    def find_expired(self, retention_days: int, kind: str) -> list[tuple[ArchiveEntry, Path]]:
        """保持期限を過ぎたメディアファイルの一覧を返す（削除はしない）。

        kind は "audio" または "video"。
        文字起こしが済んでいるかは問わず、期限を過ぎたものはすべて対象にする。
        """
        if retention_days <= 0:
            return []

        threshold = datetime.now(JST) - timedelta(days=retention_days)
        targets: list[tuple[ArchiveEntry, Path]] = []

        for entry in self.list_entries():
            started = entry.started_at
            if started is None or started > threshold:
                continue
            media = entry.find_audio() if kind == "audio" else entry.find_video()
            if media is not None:
                targets.append((entry, media))

        return targets

    def cleanup_old_media(self, audio_days: int, video_days: int) -> dict[str, list[Path]]:
        """保持期限を過ぎた音声・映像を削除し、削除したパスを種別ごとに返す。

        文字起こしと要約は削除しない。
        """
        deleted: dict[str, list[Path]] = {"audio": [], "video": []}
        label = {"audio": "音声", "video": "映像"}

        for kind, days in (("audio", audio_days), ("video", video_days)):
            for entry, media in self.find_expired(days, kind):
                try:
                    media.unlink()
                except OSError as exc:
                    logger.warning(
                        "%sの削除に失敗しました (%s): %s", label[kind], media, exc
                    )
                    continue
                deleted[kind].append(media)
                entry.update_meta(
                    **{f"{kind}_deleted_at": datetime.now(JST).isoformat()}
                )
                logger.info(
                    "保持期限を過ぎた%sを削除しました: %s/%s",
                    label[kind],
                    entry.directory.name,
                    media.name,
                )

        return deleted


def _media_cell(
    media: Path | None, folder: str, label: str, deleted_at: str | None
) -> str:
    """INDEX.md のメディア列のセルを組み立てる。"""
    if media is not None:
        return _link(f"{folder}/{media.name}", label)
    if deleted_at:
        return "削除済"
    return "—"


def format_duration(seconds: float) -> str:
    """秒数を「1時間23分」形式に整形する。"""
    total = int(seconds)
    hours, minutes = total // 3600, (total % 3600) // 60
    if hours:
        return f"{hours}時間{minutes}分"
    if minutes:
        return f"{minutes}分"
    return f"{total}秒"
