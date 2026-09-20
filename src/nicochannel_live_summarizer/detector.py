"""detector: チャンネルの配信・動画の情報を公開APIから取得する。

ニコニコチャンネルプラスはページ自体がJS描画のSPAだが、
その裏側で叩かれている公開JSON APIから配信状態を直接取得できる。
ヘッドレスブラウザを常駐させるより軽量・安定で、サイトへの負荷も小さい。

取得しているのは「配信中かどうか」「タイトル」「予定時刻」といった
公開メタデータのみで、映像・音声ストリームには一切アクセスしない。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.nicochannel.jp"

# 配信一覧APIの live_type パラメータの意味
LIVE_TYPE_ONAIR = 1  # 配信中
LIVE_TYPE_SCHEDULED = 2  # 配信予定
LIVE_TYPE_ARCHIVED = 3  # 過去の配信

# APIのタイムスタンプはタイムゾーン情報を持たない日本時間で返る
JST = ZoneInfo("Asia/Tokyo")

REQUEST_TIMEOUT_SEC = 20

# 一覧は1回のリクエストでこの件数まで取得できる
ARCHIVE_PAGE_SIZE = 50

# 種別。配信（ライブおよびそのアーカイブ）と、アップロードされた動画。
KIND_LIVE = "live"
KIND_VIDEO = "video"

# ページをたどる上限（応答が想定外でも無限ループにしないための保険）
MAX_ARCHIVE_PAGES = 100


class DetectorError(RuntimeError):
    """配信検知に関する回復不能なエラー。"""


@dataclass
class ContentInfo:
    """チャンネルの視聴対象1件を表す。

    配信（配信中 / 配信予定 / 過去の配信）と、アップロードされた動画の
    どちらもこの型で扱う。区別は kind を見る。
    """

    content_code: str
    title: str
    started_at: datetime | None
    scheduled_start_at: datetime | None
    scheduled_end_at: datetime | None
    channel_url: str
    # 視聴可能な尺。まだ公開されていないものは None。
    duration_sec: float | None = None
    # KIND_LIVE（配信）か KIND_VIDEO（動画）か
    kind: str = KIND_LIVE
    # 動画の公開日時。配信には入らないことがある。
    display_date: datetime | None = None

    @property
    def page_url(self) -> str:
        """視聴ページのURLを返す。

        サイト側のリンクはどちらも /video/ 形式だが、配信は
        /live/ でも開ける。実績のある形式をそれぞれ使う。
        """
        path = "video" if self.kind == KIND_VIDEO else "live"
        return f"{self.channel_url}/{path}/{self.content_code}"

    @property
    def date(self) -> datetime | None:
        """一覧表示や並べ替えに使う日時。"""
        return self.scheduled_start_at or self.started_at or self.display_date

    @property
    def kind_label(self) -> str:
        """画面に出す種別の表記。"""
        return "動画" if self.kind == KIND_VIDEO else "配信"


def _parse_jst(value: str | None) -> datetime | None:
    """APIの "YYYY-MM-DD HH:MM:SS" 形式を日本時間の datetime に変換する。"""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    except ValueError:
        logger.warning("日時の解釈に失敗しました: %r", value)
        return None


class LiveDetector:
    """チャンネルの配信状態を問い合わせるクライアント。"""

    def __init__(self, channel_url: str, site_id: str = "") -> None:
        self.channel_url = channel_url.rstrip("/")
        self._site_id = site_id or ""
        self._session = requests.Session()
        self._session.headers.update(
            {
                # 公式サイトのフロントエンドが送っているヘッダに合わせる
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
                "Origin": "https://nicochannel.jp",
                "Referer": f"{self.channel_url}/",
                "fc_use_device": "null",
            }
        )

    # --- 内部ユーティリティ -------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> dict:
        """APIにGETし、JSONの data 部分を返す。"""
        url = f"{API_BASE}/{path.lstrip('/')}"
        try:
            response = self._session.get(
                url, params=params, timeout=REQUEST_TIMEOUT_SEC
            )
            response.raise_for_status()
            return response.json().get("data", {})
        except requests.RequestException as exc:
            raise DetectorError(f"APIへの接続に失敗しました ({url}): {exc}") from exc
        except ValueError as exc:
            raise DetectorError(f"APIの応答がJSONではありません ({url}): {exc}") from exc

    def _to_content(self, item: dict, kind: str = KIND_LIVE) -> ContentInfo:
        """APIの1件分のレスポンスを ContentInfo に変換する。"""
        video_file = item.get("active_video_filename") or {}
        length = video_file.get("length")
        return ContentInfo(
            content_code=item.get("content_code", ""),
            title=item.get("title", "無題"),
            started_at=_parse_jst(item.get("live_started_at")),
            scheduled_start_at=_parse_jst(item.get("live_scheduled_start_at")),
            scheduled_end_at=_parse_jst(item.get("live_scheduled_end_at")),
            channel_url=self.channel_url,
            duration_sec=float(length) if length else None,
            kind=kind,
            display_date=_parse_jst(item.get("display_date")),
        )

    def _list_live_pages(
        self, live_type: int, per_page: int = 20, page: int = 1
    ) -> list[ContentInfo]:
        """指定した種別の配信一覧を1ページ分取得する。"""
        data = self._get(
            f"fc/fanclub_sites/{self.site_id}/live_pages",
            params={"page": page, "per_page": per_page, "live_type": live_type},
        )
        items = data.get("video_pages", {}).get("list") or []
        return [self._to_content(item) for item in items]

    # --- 公開API -----------------------------------------------------------

    @property
    def site_id(self) -> str:
        """チャンネルの内部ID。未設定ならURLから自動解決する。"""
        if not self._site_id:
            self._site_id = self._resolve_site_id()
        return self._site_id

    def _resolve_site_id(self) -> str:
        """チャンネルURLから fanclub_site_id を解決する。"""
        data = self._get(
            "fc/content_providers/channel_domain",
            params={"current_site_domain": self.channel_url},
        )
        providers = data.get("content_providers")
        if not providers:
            raise DetectorError(
                f"チャンネルが見つかりませんでした: {self.channel_url}\n"
                "config.toml の [channel] url が正しいか確認してください "
                "（例: https://nicochannel.jp/kumonoue_yumemi）。"
            )
        site_id = providers.get("fanclub_site", {}).get("id") or providers.get("id")
        if not site_id:
            raise DetectorError("APIの応答からチャンネルIDを取得できませんでした。")
        logger.info("チャンネルID を解決しました: %s", site_id)
        return str(site_id)

    def get_live_now(self) -> ContentInfo | None:
        """現在配信中なら ContentInfo を、配信していなければ None を返す。"""
        lives = self._list_live_pages(LIVE_TYPE_ONAIR, per_page=5)
        return lives[0] if lives else None

    def get_upcoming(self) -> list[ContentInfo]:
        """配信予定の一覧を、開始予定時刻の昇順で返す。"""
        lives = self._list_live_pages(LIVE_TYPE_SCHEDULED, per_page=20)
        return sorted(
            lives,
            key=lambda live: live.scheduled_start_at or datetime.max.replace(tzinfo=JST),
        )

    def get_live_archives(self, limit: int | None = None) -> list[ContentInfo]:
        """過去の配信のアーカイブを新しい順に返す。動画は含まない。

        limit を省略すると全件返す。1回のリクエストでは
        ARCHIVE_PAGE_SIZE 件までしか取得できないため、
        必要な分だけページをたどって集める。
        """
        archives: list[ContentInfo] = []
        for page in range(1, MAX_ARCHIVE_PAGES + 1):
            lives = self._list_live_pages(
                LIVE_TYPE_ARCHIVED, per_page=ARCHIVE_PAGE_SIZE, page=page
            )
            archives += lives
            # 最終ページに達したか、必要な件数がそろったら打ち切る
            if len(lives) < ARCHIVE_PAGE_SIZE:
                break
            if limit is not None and len(archives) >= limit:
                break
        return archives[:limit] if limit is not None else archives

    def get_videos(self, limit: int | None = None) -> list[ContentInfo]:
        """チャンネルの動画（配信アーカイブを含む）を新しい順に返す。

        video_pages は「視聴できる動画」の一覧で、配信のアーカイブも
        変換が済んだものはここに現れる。重複は get_all_contents で除く。
        """
        videos: list[ContentInfo] = []
        for page in range(1, MAX_ARCHIVE_PAGES + 1):
            data = self._get(
                f"fc/fanclub_sites/{self.site_id}/video_pages",
                params={"page": page, "per_page": ARCHIVE_PAGE_SIZE},
            )
            items = data.get("video_pages", {}).get("list") or []
            videos += [self._to_content(item, kind=KIND_VIDEO) for item in items]
            if len(items) < ARCHIVE_PAGE_SIZE:
                break
            if limit is not None and len(videos) >= limit:
                break
        return videos[:limit] if limit is not None else videos

    def get_all_contents(self, limit: int | None = None) -> list[ContentInfo]:
        """過去の配信と動画をまとめて、新しい順に返す。

        配信のアーカイブは video_pages にも現れるため content_code で
        重複を除く。その際は配信側を優先する（開始時刻などの情報が多く、
        まだ動画化されていないものも拾えるため）。
        """
        merged: dict[str, ContentInfo] = {}
        for item in self.get_live_archives() + self.get_videos():
            merged.setdefault(item.content_code, item)
        oldest = datetime.min.replace(tzinfo=JST)
        ordered = sorted(
            merged.values(), key=lambda c: c.date or oldest, reverse=True
        )
        return ordered[:limit] if limit is not None else ordered

    def next_upcoming(self) -> ContentInfo | None:
        """まだ開始していない直近の配信予定を返す。"""
        now = datetime.now(JST)
        for live in self.get_upcoming():
            scheduled = live.scheduled_start_at
            # 予定時刻を多少過ぎていても、まだ始まっていない可能性があるので許容する
            if scheduled is None or scheduled >= now - timedelta(hours=1):
                return live
        return None

    def close(self) -> None:
        """HTTPセッションを閉じる。"""
        self._session.close()
