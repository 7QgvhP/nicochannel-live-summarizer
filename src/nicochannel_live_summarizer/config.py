"""設定ファイル（config.toml）と環境変数（.env）の読み込み。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# プロジェクトのルートディレクトリ（src/<パッケージ>/config.py から2階層上）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


@dataclass
class ChannelConfig:
    """監視対象チャンネルの設定。"""

    url: str
    site_id: str = ""

    @property
    def normalized_url(self) -> str:
        """APIに渡す形式（末尾スラッシュなし）のURLを返す。"""
        return self.url.rstrip("/")


@dataclass
class DetectorConfig:
    """配信検知の設定。"""

    poll_interval_idle_sec: int = 300
    poll_interval_near_sec: int = 30
    near_window_min: int = 30
    poll_interval_live_sec: int = 30
    finish_confirm_count: int = 3


@dataclass
class RecorderConfig:
    """録音の設定。"""

    device_name: str = ""
    max_hours: float = 8.0
    audio_format: str = "opus"
    opus_bitrate_kbps: int = 64


@dataclass
class VideoConfig:
    """画面録画の設定。"""

    enabled: bool = True
    display_index: int = 0
    height: int = 1080
    framerate: int = 30
    quality_cq: int = 30
    crop: str = ""
    encoder: str = "auto"


@dataclass
class BrowserConfig:
    """配信ページを再生し続ける専用ブラウザの設定。"""

    enabled: bool = False
    profile_dir: str = ".browser-profile"
    channel: str = "chrome"
    fullscreen: bool = True
    hide_comments: bool = True

    def resolve_profile_dir(self, base: Path) -> Path:
        """プロファイルの保存先を絶対パスで返す。"""
        path = Path(self.profile_dir)
        return path if path.is_absolute() else (base / path).resolve()


@dataclass
class PowerConfig:
    """スリープ抑止の設定。"""

    prevent_sleep: bool = True


@dataclass
class TranscriberConfig:
    """文字起こしの設定。"""

    model: str = "large-v3"
    device: str = "cuda"
    compute_type: str = "float16"
    language: str = "ja"
    beam_size: int = 5
    vad_filter: bool = True


@dataclass
class SummarizerConfig:
    """要約の設定。"""

    enabled: bool = True
    model: str = "claude-opus-5"
    max_tokens: int = 8000
    effort: str = "high"
    api_key: str = ""

    @property
    def available(self) -> bool:
        """要約を実行できる状態か（有効かつAPIキーがある）を返す。"""
        return self.enabled and bool(self.api_key)


@dataclass
class ArchiveConfig:
    """アーカイブ保存先の設定。"""

    root: Path = field(default_factory=lambda: PROJECT_ROOT / "archive")


@dataclass
class RetentionConfig:
    """保持期間の設定。"""

    audio_days: int = 30
    video_days: int = 7


@dataclass
class Config:
    """アプリケーション全体の設定。"""

    channel: ChannelConfig
    detector: DetectorConfig
    recorder: RecorderConfig
    video: VideoConfig
    browser: BrowserConfig
    power: PowerConfig
    transcriber: TranscriberConfig
    summarizer: SummarizerConfig
    archive: ArchiveConfig
    retention: RetentionConfig


def _load_env_file(env_path: Path) -> None:
    """.env ファイルを読み込み、未設定の環境変数だけを補う。

    既に環境変数が設定されている場合はそちらを優先する（上書きしない）。
    """
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(config_path: Path | None = None) -> Config:
    """config.toml と .env を読み込み、Config を組み立てて返す。"""
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.is_file():
        raise FileNotFoundError(
            f"設定ファイルが見つかりません: {path}\n"
            "config.toml をプロジェクトルートに配置してください。"
        )

    _load_env_file(DEFAULT_ENV_PATH)

    with path.open("rb") as f:
        raw = tomllib.load(f)

    channel_raw = raw.get("channel", {})
    if not channel_raw.get("url"):
        raise ValueError("config.toml の [channel] url が設定されていません。")

    archive_raw = raw.get("archive", {})
    archive_root = Path(archive_raw.get("root", "archive"))
    if not archive_root.is_absolute():
        archive_root = (path.parent / archive_root).resolve()

    summarizer_raw = raw.get("summarizer", {})

    return Config(
        channel=ChannelConfig(
            url=channel_raw["url"],
            site_id=str(channel_raw.get("site_id", "") or ""),
        ),
        detector=DetectorConfig(**raw.get("detector", {})),
        recorder=RecorderConfig(**raw.get("recorder", {})),
        video=VideoConfig(**raw.get("video", {})),
        browser=BrowserConfig(**raw.get("browser", {})),
        power=PowerConfig(**raw.get("power", {})),
        transcriber=TranscriberConfig(**raw.get("transcriber", {})),
        summarizer=SummarizerConfig(
            enabled=summarizer_raw.get("enabled", True),
            model=summarizer_raw.get("model", "claude-opus-5"),
            max_tokens=summarizer_raw.get("max_tokens", 8000),
            effort=summarizer_raw.get("effort", "high"),
            # APIキーはコードにも設定ファイルにも書かず、環境変数からのみ取得する。
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        ),
        archive=ArchiveConfig(root=archive_root),
        retention=RetentionConfig(**raw.get("retention", {})),
    )
