"""browser: 配信ページを再生し続けるための専用ブラウザ。

不在時の録画では、ブラウザ側で再生が止まっても誰も気づけない。
そこで配信ページを専用のブラウザで開き、再生状態を直接確認して、
止まっていれば再読み込みと再生操作で復旧できるようにする。

ログインは利用者自身がこのブラウザ上で1回だけ行い、
そのプロファイル（Cookie等）を再利用する。
本ツールが ID / パスワードを受け取ることはない。

実機にインストール済みの Chrome を使う（channel="chrome"）。
Playwright 同梱の Chromium は一部の再生方式に対応していないため。
また、画面キャプチャの対象になる必要があるので必ず画面に表示する
（ヘッドレスでは録画できない）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import BrowserConfig

logger = logging.getLogger(__name__)

# 再生中かどうかを判定するために currentTime を比較する間隔（秒）
PLAYBACK_SAMPLE_SEC = 3.0

# ページの読み込みを待つ上限（ミリ秒）
NAVIGATION_TIMEOUT_MS = 60_000

# 全画面ボタンの aria-label（プレイヤーのコントロールバー内）
FULLSCREEN_LABEL = "フルスクリーン"

# 全画面化を試す回数と、その間隔（秒）。
# ブラウザを起動した直後はプレイヤーの読み込みが遅れ、
# コントロールバーがまだ無いことがあるため、1回で諦めない。
FULLSCREEN_ATTEMPTS = 5
FULLSCREEN_RETRY_SEC = 2.0

# コメントを隠すCSS。
# クラス名にハッシュが付く作りのため、前方一致で指定して変更に強くしている。
#   #player-comment-container : 映像の上に流れるコメント
#   EnableCommentArea-*       : 右側・上部のコメント欄
#   CommentDetail-*           : 個々のコメント
HIDE_COMMENTS_CSS = """
#player-comment-container,
[class*="EnableCommentArea"],
[class*="CommentDetail"] {
    display: none !important;
}
"""


class BrowserError(RuntimeError):
    """ブラウザ操作に関する回復不能なエラー。"""


@dataclass
class PlaybackState:
    """配信ページの再生状態。"""

    has_video: bool
    paused: bool
    ready_state: int
    current_time: float
    duration: float
    muted: bool
    volume: float
    advancing: bool

    @property
    def is_playing(self) -> bool:
        """実際に再生が進んでいるか。"""
        return self.has_video and not self.paused and self.advancing

    def describe(self) -> str:
        """人が読める形で状態を返す。"""
        if not self.has_video:
            return "動画プレイヤーが見つかりません"
        if self.paused:
            return "一時停止中"
        if not self.advancing:
            return "再生位置が進んでいません（読み込み待ち、または停止）"
        if self.muted or self.volume == 0:
            return "再生中ですが音量がゼロです"
        return "再生中"


# ページ内で再生状態を調べる JavaScript
_PLAYBACK_SCRIPT = """
() => {
    const v = document.querySelector('video');
    if (!v) return { has_video: false };
    return {
        has_video: true,
        paused: v.paused,
        ready_state: v.readyState,
        current_time: v.currentTime,
        duration: isFinite(v.duration) ? v.duration : 0,
        muted: v.muted,
        volume: v.volume,
    };
}
"""


# ボタンを押しても切り替わらないときに、Fullscreen API を直接呼ぶスクリプト。
# 失敗した場合は理由の文字列を、成功した場合は空文字を返す。
_REQUEST_FULLSCREEN_SCRIPT = """
async () => {
    const el = document.querySelector('#player-container') || document.querySelector('video');
    if (!el || !el.requestFullscreen) return 'Fullscreen API を利用できません';
    try { await el.requestFullscreen(); return ''; }
    catch (e) { return e.name + ': ' + e.message; }
}
"""


class LiveBrowser:
    """配信ページを開き、再生状態を監視・復旧するブラウザ。"""

    def __init__(
        self,
        profile_dir: Path,
        channel: str = "chrome",
        fullscreen: bool = True,
        hide_comments: bool = True,
    ) -> None:
        self.profile_dir = profile_dir
        self.channel = channel
        self.fullscreen = fullscreen
        self.hide_comments = hide_comments
        self._playwright = None
        self._context = None
        self._page = None

    # --- 起動と終了 ---------------------------------------------------------

    def start(self) -> None:
        """ブラウザを起動する。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # noqa: BLE001
            raise BrowserError(
                "playwright がインストールされていません。\n"
                "    .\\.venv\\Scripts\\python.exe -m pip install playwright"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                channel=self.channel,
                headless=False,  # 画面キャプチャの対象にするため必須
                viewport=None,
                args=["--start-maximized"],
            )
        except Exception as exc:  # noqa: BLE001 - 起動失敗の理由をまとめて伝える
            self.stop()
            raise BrowserError(
                f"ブラウザを起動できませんでした（channel={self.channel}）: {exc}"
            ) from exc

        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

    def stop(self) -> None:
        """ブラウザを閉じる。"""
        if self._context is not None:
            try:
                self._context.close()
            except Exception as exc:  # noqa: BLE001 - 後始末なので握りつぶす
                logger.debug("ブラウザを閉じられませんでした: %s", exc)
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Playwright を終了できませんでした: %s", exc)
        self._context = None
        self._playwright = None
        self._page = None

    def __enter__(self) -> "LiveBrowser":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # --- ページ操作 ---------------------------------------------------------

    @property
    def page(self):
        if self._page is None:
            raise BrowserError("ブラウザが起動していません。")
        return self._page

    def open(self, url: str) -> None:
        """指定したURLを開く。"""
        logger.info("ページを開きます: %s", url)
        self.page.goto(url, wait_until="domcontentloaded")
        self.apply_view_settings()

    def reload(self) -> None:
        """ページを再読み込みする。"""
        logger.info("ページを再読み込みします。")
        self.page.reload(wait_until="domcontentloaded")
        self.apply_view_settings()

    def apply_view_settings(self) -> None:
        """コメント非表示などの見た目の設定を適用する。

        挿入したスタイルは再読み込みで失われるため、
        ページを開き直すたびに呼び直す必要がある。
        """
        if not self.hide_comments:
            return
        try:
            self.page.add_style_tag(content=HIDE_COMMENTS_CSS)
            logger.debug("コメントを非表示にしました。")
        except Exception as exc:  # noqa: BLE001 - 表示設定の失敗で録画を止めない
            logger.debug("コメントの非表示に失敗しました: %s", exc)

    def is_fullscreen(self) -> bool:
        """全画面表示になっているかを返す。"""
        try:
            return bool(self.page.evaluate("() => document.fullscreenElement !== null"))
        except Exception:  # noqa: BLE001
            return False

    def enter_fullscreen(self) -> bool:
        """プレイヤーを全画面にする。成功したら True を返す。

        プレイヤーの読み込みが終わるまではボタンが存在しない、
        あるいは存在しても visibility:hidden で押せないため、
        間隔をあけて複数回試す。
        """
        if not self.fullscreen:
            return False
        if self.is_fullscreen():
            return True

        reason = "理由不明"
        for attempt in range(1, FULLSCREEN_ATTEMPTS + 1):
            succeeded, reason = self._try_enter_fullscreen()
            if succeeded:
                logger.info("プレイヤーを全画面にしました（%d 回目）。", attempt)
                # 全画面化でコメント欄が再描画されることがあるため入れ直す
                self.apply_view_settings()
                return True
            logger.debug(
                "全画面にできませんでした (%d/%d): %s",
                attempt,
                FULLSCREEN_ATTEMPTS,
                reason,
            )
            if attempt < FULLSCREEN_ATTEMPTS:
                self.page.wait_for_timeout(int(FULLSCREEN_RETRY_SEC * 1000))

        # 原因を追えるよう、最後の失敗理由を警告に含める
        logger.warning(
            "全画面にできませんでした（%d 回試行 / 最後の理由: %s）。"
            "ウィンドウ表示のまま録画を続けます。",
            FULLSCREEN_ATTEMPTS,
            reason,
        )
        return False

    def _try_enter_fullscreen(self) -> tuple[bool, str]:
        """全画面化を1回だけ試し、成否と失敗理由を返す。"""
        try:
            # コントロールバーは操作するまで visibility:hidden なので、
            # まずプレイヤー上にマウスを乗せて表示させる
            player = self.page.query_selector("#player-container") or self.page.query_selector(
                "video"
            )
            if player is None:
                return False, "プレイヤーがまだ表示されていません"
            player.hover(timeout=5000)
            self.page.wait_for_timeout(700)

            button = self.page.query_selector(f'button[aria-label="{FULLSCREEN_LABEL}"]')
            if button is None:
                return False, f"「{FULLSCREEN_LABEL}」ボタンが見つかりません"
            if not button.is_visible():
                return False, "全画面ボタンがまだ表示されていません"

            button.click(timeout=5000)
            self.page.wait_for_timeout(1500)
            if self.is_fullscreen():
                return True, ""

            # ボタンが効かない場合の保険として Fullscreen API を直接呼ぶ
            error = self.page.evaluate(_REQUEST_FULLSCREEN_SCRIPT)
            self.page.wait_for_timeout(1000)
            if self.is_fullscreen():
                logger.debug("Fullscreen API の直接呼び出しで全画面にしました。")
                return True, ""
            return False, error or "ボタンを押しても全画面になりませんでした"
        except Exception as exc:  # noqa: BLE001 - 全画面化の失敗は致命的ではない
            return False, f"操作に失敗しました: {exc}"

    def is_logged_in(self) -> bool:
        """ログイン済みらしいかを判定する。

        未ログインだとヘッダーに「ログイン」が出るため、それを手がかりにする。
        画面構成が変わると判定できなくなるので、あくまで目安として使う。
        """
        try:
            return self.page.get_by_text("ログイン", exact=True).count() == 0
        except Exception as exc:  # noqa: BLE001 - 判定できなくても致命的ではない
            logger.debug("ログイン状態を判定できませんでした: %s", exc)
            return False

    def playback_state(self, sample_sec: float = PLAYBACK_SAMPLE_SEC) -> PlaybackState:
        """再生状態を調べる。

        一時停止していなくても読み込みが止まっていることがあるため、
        少し時間を空けて再生位置が進んでいるかまで確認する。
        """
        first = self.page.evaluate(_PLAYBACK_SCRIPT)
        if not first.get("has_video"):
            return PlaybackState(
                has_video=False, paused=True, ready_state=0, current_time=0.0,
                duration=0.0, muted=False, volume=0.0, advancing=False,
            )

        time.sleep(sample_sec)
        second = self.page.evaluate(_PLAYBACK_SCRIPT)
        advancing = bool(
            second.get("has_video")
            and second["current_time"] > first["current_time"] + 0.1
        )

        return PlaybackState(
            has_video=True,
            paused=bool(second.get("paused", True)),
            ready_state=int(second.get("ready_state", 0)),
            current_time=float(second.get("current_time", 0.0)),
            duration=float(second.get("duration", 0.0)),
            muted=bool(second.get("muted", False)),
            volume=float(second.get("volume", 0.0)),
            advancing=advancing,
        )

    def try_play(self) -> bool:
        """再生を試みる。再生できたら True を返す。

        自動再生が制限されている場合に備え、要素のクリックも試す。
        """
        # まずは video 要素へ直接指示する
        try:
            self.page.evaluate(
                "() => { const v = document.querySelector('video');"
                " if (v) { v.muted = false; v.play().catch(() => {}); } }"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("video.play() の呼び出しに失敗しました: %s", exc)

        if self.playback_state(sample_sec=2.0).is_playing:
            return True

        # 自動再生が拒否された場合はプレイヤーをクリックする
        for selector in ("video", "[class*='play']", "button[aria-label*='再生']"):
            try:
                element = self.page.query_selector(selector)
                if element is None:
                    continue
                element.click(timeout=5000)
                if self.playback_state(sample_sec=2.0).is_playing:
                    logger.info("プレイヤーをクリックして再生を再開しました。")
                    return True
            except Exception as exc:  # noqa: BLE001 - 次の候補を試す
                logger.debug("%s のクリックに失敗しました: %s", selector, exc)

        return False

    def recover(self) -> bool:
        """止まっている再生を復旧する。成功したら True を返す。

        再読み込みすると全画面もコメント非表示も解除されるため、
        復旧できたらそれらを掛け直す。
        """
        if self.try_play():
            self.enter_fullscreen()
            return True

        self.reload()
        self.page.wait_for_timeout(5000)
        if not self.try_play():
            return False

        self.enter_fullscreen()
        return True


def browser_from_config(config: "BrowserConfig") -> LiveBrowser:
    """BrowserConfig から LiveBrowser を組み立てる。"""
    from .config import PROJECT_ROOT

    return LiveBrowser(
        profile_dir=config.resolve_profile_dir(PROJECT_ROOT),
        channel=config.channel,
        fullscreen=config.fullscreen,
        hide_comments=config.hide_comments,
    )
