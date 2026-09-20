"""pipeline: 検知 → 録音 → 文字起こし → 要約 → アーカイブ の一連の流れ。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

from . import target
from .archive import JST, ArchiveEntry, ArchiveManager, format_duration
from .config import Config
from .detector import ContentInfo, LiveDetector
from .ffmpeg_tools import convert_to_opus, mux_audio_video, probe_duration
from .power import KeepAwake
from .recorder import SystemAudioRecorder
from .screen_recorder import ScreenRecorder, ScreenRecorderError
from .summarizer import Summarizer, SummarizerError, write_summary_file
from .transcriber import (
    Transcriber,
    build_timestamped_text,
    format_timestamp,
    load_transcript,
    write_transcript_files,
)

logger = logging.getLogger(__name__)

# 録音中、この秒数だけ音声が届かなければ警告する。
# 配信の「間」で誤検知しないよう、余裕をもった値にしている。
SILENCE_WARNING_SEC = 180.0

# 警告を繰り返す間隔（秒）。ログが埋まらないように間引く。
SILENCE_WARNING_INTERVAL_SEC = 300.0

# 自動復旧を試みる間隔（秒）。再読み込み自体が数秒〜十数秒を失うため、
# 短い間隔で繰り返さないようにする。
RECOVERY_INTERVAL_SEC = 120.0


def _try_unlink(path: Path, description: str) -> bool:
    """中間ファイルを削除する。失敗しても例外にせず警告に留める。

    Windows では別のプロセスがファイルを掴んでいると削除できない。
    ここで例外を投げると、録音済みの音声・映像や、そのあとに続く
    文字起こし・要約まで巻き添えで中断してしまうため、
    「消せなかった」ことだけを伝えて処理を続ける。
    """
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as exc:
        logger.warning(
            "%sを削除できませんでした（処理は続行します）。"
            "不要であれば手動で削除してください: %s（%s）",
            description,
            path,
            exc,
        )
        return False


def _make_progress_reporter(interval_sec: float = 60.0):
    """文字起こしの進捗を、一定時間ごとにログへ出すコールバックを作る。

    毎区間ログを出すとログが流れすぎるため、指定秒数ぶん進むごとに間引く。
    """
    state = {"last": 0.0}

    def report(position: float, total: float) -> None:
        if position - state["last"] < interval_sec:
            return
        state["last"] = position
        percent = (position / total * 100) if total else 0
        logger.info(
            "  文字起こし %s / %s (%.0f%%)",
            format_timestamp(position),
            format_timestamp(total),
            percent,
        )

    return report


class Pipeline:
    """アプリケーション全体の処理を束ねる。"""

    # 実行できる後処理の工程
    STEP_TRANSCRIBE = "transcribe"
    STEP_SUMMARIZE = "summarize"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.detector = LiveDetector(
            config.channel.normalized_url, config.channel.site_id
        )
        self.archive = ArchiveManager(config.archive.root)
        # モデルの読み込みは重いので、実際に必要になるまで遅延させる
        self._transcriber: Transcriber | None = None
        self._summarizer: Summarizer | None = None
        self._stop_requested = False

    # --- 遅延初期化 ---------------------------------------------------------

    @property
    def transcriber(self) -> Transcriber:
        if self._transcriber is None:
            cfg = self.config.transcriber
            self._transcriber = Transcriber(
                model_size=cfg.model,
                device=cfg.device,
                compute_type=cfg.compute_type,
                language=cfg.language,
                beam_size=cfg.beam_size,
                vad_filter=cfg.vad_filter,
            )
        return self._transcriber

    @property
    def summarizer(self) -> Summarizer | None:
        cfg = self.config.summarizer
        if not cfg.available:
            return None
        if self._summarizer is None:
            self._summarizer = Summarizer(
                api_key=cfg.api_key,
                model=cfg.model,
                max_tokens=cfg.max_tokens,
                effort=cfg.effort,
            )
        return self._summarizer

    # --- スリープ抑止 -------------------------------------------------------

    def _keep_awake(self, keep_display: bool, reason: str):
        """設定が有効なら、その間 PC をスリープさせない with 対象を返す。"""
        if not self.config.power.prevent_sleep:
            return nullcontext()
        return KeepAwake(keep_display=keep_display, reason=reason)

    # --- 監視ループ ---------------------------------------------------------

    def watch(self) -> None:
        """配信の開始を待ち受け、検知したら自動で録音・録画する。

        文字起こしと要約は行わない。録画後に `transcribe` / `summarize` を実行する。
        """
        logger.info("監視を開始します: %s", self.config.channel.normalized_url)
        logger.info("停止するには Ctrl+C を押してください。")

        # 監視中にスリープされると配信を丸ごと録り逃すため抑止する。
        #
        # 画面録画を使う場合は、待機中から画面も点けたままにする。
        # SetThreadExecutionState は「画面が消えるのを防ぐ」APIであって
        # 「消えた画面を点ける」ことはできないため、配信開始を検知してから
        # 画面の維持を要求しても、すでに暗くなっていると復帰できない。
        keep_display = self.config.video.enabled
        with self._keep_awake(keep_display=keep_display, reason="配信の監視中"):
            self._watch_loop()

    def _watch_loop(self) -> None:
        """配信の検知と処理を繰り返す。"""
        cfg = self.config.detector

        while not self._stop_requested:
            try:
                live = self.detector.get_live_now()
            except Exception as exc:  # noqa: BLE001 - 一時的な通信断で監視を止めない
                logger.warning("配信状態の取得に失敗しました。再試行します: %s", exc)
                time.sleep(cfg.poll_interval_idle_sec)
                continue

            if live is not None:
                logger.info("配信を検知しました: %s", live.title)
                try:
                    self.handle_live(live)
                except KeyboardInterrupt:
                    raise
                except Exception:  # noqa: BLE001 - 1本失敗しても監視は継続する
                    logger.exception("配信の処理中にエラーが発生しました。")
                # 同じ配信を二重に拾わないよう、終了直後は少し待つ
                time.sleep(60)
                continue

            interval = self._idle_interval()
            time.sleep(interval)

    def _idle_interval(self) -> int:
        """次のポーリングまでの待ち時間を決める。

        配信予定の時刻が近いときは間隔を詰め、それ以外は緩やかに監視する。
        """
        cfg = self.config.detector
        try:
            upcoming = self.detector.next_upcoming()
        except Exception as exc:  # noqa: BLE001 - 予定が取れなくても監視は続ける
            logger.debug("配信予定の取得に失敗しました: %s", exc)
            return cfg.poll_interval_idle_sec

        if upcoming is None or upcoming.scheduled_start_at is None:
            logger.debug("配信予定なし。%d 秒後に再確認します。", cfg.poll_interval_idle_sec)
            return cfg.poll_interval_idle_sec

        now = datetime.now(JST)
        minutes_until = (upcoming.scheduled_start_at - now).total_seconds() / 60

        if minutes_until <= cfg.near_window_min:
            logger.info(
                "配信予定が近づいています（%s / あと %.0f 分）。監視間隔を %d 秒に切り替えます。",
                upcoming.title,
                max(minutes_until, 0),
                cfg.poll_interval_near_sec,
            )
            return cfg.poll_interval_near_sec

        # 予定時刻の near_window_min 前に必ず起きるよう、待ち時間を調整する
        seconds_until_near = (minutes_until - cfg.near_window_min) * 60
        return int(min(cfg.poll_interval_idle_sec, max(seconds_until_near, 10)))

    # --- 1配信分の処理 ------------------------------------------------------

    def handle_live(self, live: ContentInfo) -> ArchiveEntry:
        """配信1本を録音してアーカイブに保存する。

        文字起こしと要約は行わない。保存したフォルダを指定して
        `transcribe` / `summarize` を実行する。
        """
        started_at = live.started_at or datetime.now(JST)
        entry = self.archive.create_entry(
            title=live.title,
            started_at=started_at,
            content_code=live.content_code,
            page_url=live.page_url,
        )

        self._record_live(entry, live)
        self.archive.write_index()
        logger.info(
            "文字起こしするには次を実行してください: "
            '.\\run.ps1 transcribe "%s"',
            entry.directory,
        )
        return entry

    def capture(
        self,
        title: str,
        page_url: str,
        content_code: str,
        started_at: datetime | None = None,
        max_seconds: float | None = None,
    ) -> ArchiveEntry:
        """任意のタイミングで録音を開始し、アーカイブに保存する。

        アーカイブ配信を取り込む用途を想定している。
        配信の自動検知は使わず、指定時間または Ctrl+C で停止する。
        文字起こしと要約は行わない。
        """
        entry = self.archive.create_entry(
            title=title,
            started_at=started_at or datetime.now(JST),
            content_code=content_code,
            page_url=page_url,
        )
        entry.update_meta(capture_mode="manual")

        # 取り込みは配信の尺ぶん実時間がかかる。
        # 途中でスリープされると録音が切れるため、その間だけ抑止する。
        with self._keep_awake(keep_display=False, reason="アーカイブの取り込み中"):
            self._record_manual(entry, page_url, max_seconds)

        self.archive.write_index()
        return entry

    def _record_live(self, entry: ArchiveEntry, live: ContentInfo) -> None:
        """配信が終わるまで録音（と、設定されていれば録画）する。

        停止の判断は「配信中APIから消えたか」で行う。
        一時的な通信の揺らぎで録音が切れないよう、
        規定回数連続で消えたときにだけ終了と判定する。
        """
        cfg = self.config.detector
        state = {"missing": 0}

        def should_continue(recorder: SystemAudioRecorder) -> bool:
            try:
                current = self.detector.get_live_now()
            except Exception as exc:  # noqa: BLE001 - 通信断で録音を止めない
                logger.debug("配信状態の確認に失敗しました: %s", exc)
                return True

            if current is None or current.content_code != live.content_code:
                state["missing"] += 1
                logger.info(
                    "配信終了の可能性を検知しました (%d/%d)",
                    state["missing"],
                    cfg.finish_confirm_count,
                )
                if state["missing"] >= cfg.finish_confirm_count:
                    logger.info("配信の終了を確認しました。録音を停止します。")
                    return False
                return True

            if state["missing"]:
                logger.info("配信が継続していました。録音を続けます。")
            state["missing"] = 0
            logger.info("録音中… %s", format_timestamp(recorder.recorded_seconds))
            return True

        self._record_session(
            entry=entry,
            page_url=live.page_url,
            tick_sec=cfg.poll_interval_live_sec,
            should_continue=should_continue,
        )

    def _record_manual(
        self, entry: ArchiveEntry, page_url: str, max_seconds: float | None
    ) -> None:
        """手動で開始した録音を、指定時間または Ctrl+C まで続ける。

        アーカイブ配信の取り込みに使う。max_seconds が None の場合は
        Ctrl+C を押すまで録り続ける。
        """
        if max_seconds is None:
            logger.info("停止するには Ctrl+C を押してください。")
        else:
            logger.info(
                "%s 後に自動停止します（Ctrl+C で途中終了できます）。",
                format_duration(max_seconds),
            )

        def should_continue(recorder: SystemAudioRecorder) -> bool:
            elapsed = recorder.recorded_seconds
            if max_seconds is None:
                logger.info("録音中… %s", format_timestamp(elapsed))
                return True

            remaining = max_seconds - elapsed
            if remaining <= 0:
                logger.info("指定時間に達したため録音を停止します。")
                return False
            logger.info(
                "録音中… %s / %s（残り %s）",
                format_timestamp(elapsed),
                format_timestamp(max_seconds),
                format_duration(remaining),
            )
            return True

        # 短い録音でも指定時間ちょうどで止まるよう、確認間隔を長さに合わせる
        tick_sec = 30.0
        if max_seconds is not None:
            tick_sec = min(30.0, max(2.0, max_seconds / 20))

        self._record_session(
            entry=entry,
            page_url=page_url,
            tick_sec=tick_sec,
            should_continue=should_continue,
        )

    def _record_session(
        self,
        entry: ArchiveEntry,
        page_url: str,
        tick_sec: float,
        should_continue: Callable[[SystemAudioRecorder], bool],
    ) -> None:
        """録音（と録画）を実行し、終了後にファイルを整える。

        停止条件だけが配信の自動検知と手動実行で異なるため、
        その判定を should_continue に委ねている。
        """
        cfg = self.config.recorder
        wav_path = entry.audio_path(".wav")

        recorder = SystemAudioRecorder(
            output_path=wav_path,
            device_name=cfg.device_name,
            max_seconds=cfg.max_hours * 3600,
        )
        recorder.start()
        logger.info("録音を開始しました → %s", wav_path.name)

        screen = self._start_screen_recorder(entry)
        browser = self._start_stream_browser(page_url)

        if browser is None:
            logger.info(
                "配信ページ: %s （ブラウザで再生してください。再生中の%sをキャプチャします）",
                page_url,
                "画面と音声" if screen is not None else "音声",
            )

        # 画面録画中はディスプレイが消えるとキャプチャに失敗することがあるため、
        # 録画している間だけ画面の電源も維持する。
        display_guard = self._keep_awake(
            keep_display=screen is not None, reason="録画中"
        )

        interrupted = False
        silence_state = {"last_warned": 0.0, "last_recovery": 0.0, "failures": 0}
        try:
            with display_guard:
                while recorder.is_running:
                    time.sleep(tick_sec)
                    if screen is not None:
                        # 画面録画が途中で落ちていれば警告する（初回のみ）
                        screen.check_alive()
                    self._handle_silence(recorder, page_url, browser, silence_state)
                    if not should_continue(recorder):
                        break
        except KeyboardInterrupt:
            interrupted = True
            self._stop_requested = True
            logger.warning("中断を検知しました。ここまでの録音を保存して処理を続けます。")
        finally:
            if browser is not None:
                browser.stop()
            # 映像と音声の終端を揃えるため、先に両方へ停止を伝えてから終了を待つ
            if screen is not None:
                screen.request_stop()
            # 録音の停止は失敗しうる（スレッド内の例外を送出する）。
            # ここで抜けると ffmpeg を待たずに終わり、プロセスが残って
            # 映像ファイルも壊れるため、必ず両方の後始末を行う。
            try:
                recorder.stop()
            finally:
                if screen is not None:
                    screen.wait_stop()

        duration = recorder.recorded_seconds
        logger.info("録音を終了しました（%s）", format_duration(duration))

        if recorder.silence_ratio > 0.95:
            logger.warning(
                "録音の %.0f%% が無音でした。配信をブラウザで再生していたか、"
                "録音デバイスの選択が正しいか確認してください"
                "（`.\\run.ps1 devices` で確認できます）。",
                recorder.silence_ratio * 100,
            )

        audio_path = self._finalize_audio(entry, wav_path)
        video_path = self._finalize_video(entry, screen, audio_path, duration)

        entry.update_meta(
            finished_at=datetime.now(JST).isoformat(),
            duration_sec=round(duration, 2),
            audio_file=audio_path.name if audio_path else None,
            video_file=video_path.name if video_path else None,
            interrupted=interrupted,
        )

    def _handle_silence(
        self,
        recorder: SystemAudioRecorder,
        page_url: str,
        browser,
        state: dict,
    ) -> None:
        """音声が届かない状態が続いていたら、警告し、可能なら復旧する。

        ブラウザの再生が止まっていても録音自体は無音を記録し続けるため、
        終わってから気づくと配信を丸ごと失う。
        専用ブラウザを使っている場合は、その場で再生を復旧する。
        """
        silent_for = recorder.seconds_since_audio
        if silent_for < SILENCE_WARNING_SEC:
            # 音が戻ったら失敗回数をリセットする
            state["failures"] = 0
            return

        now = time.monotonic()

        if browser is not None:
            if now - state["last_recovery"] < RECOVERY_INTERVAL_SEC:
                return
            state["last_recovery"] = now
            self._recover_playback(browser, silent_for, state)
            return

        if now - state["last_warned"] < SILENCE_WARNING_INTERVAL_SEC:
            return
        state["last_warned"] = now

        if recorder.has_received_audio:
            logger.warning(
                "%s のあいだ音声が検出されていません。"
                "ブラウザの再生が止まっていないか確認してください: %s",
                format_duration(silent_for),
                page_url,
            )
        else:
            logger.warning(
                "録音開始から %s、一度も音声が検出されていません。"
                "配信ページを開いて再生を始めてください"
                "（すでに再生中の場合はページの再読み込みをお試しください）: %s",
                format_duration(silent_for),
                page_url,
            )

    def _recover_playback(self, browser, silent_for: float, state: dict) -> None:
        """専用ブラウザ上の再生を復旧する。"""
        logger.warning(
            "%s のあいだ音声が検出されていません。再生の復旧を試みます。",
            format_duration(silent_for),
        )
        try:
            playback = browser.playback_state(sample_sec=2.0)
            logger.info("ページの状態: %s", playback.describe())
            if playback.is_playing:
                # ページ上は再生されているのに音が来ない場合、
                # 音量ゼロや出力先の問題が疑われる。再読み込みでは直らない。
                logger.warning(
                    "ページ上は再生中です。音量がゼロになっていないか、"
                    "録音デバイスの設定が正しいか確認してください。"
                )
                return

            if browser.recover():
                logger.info("再生を再開しました。")
                state["failures"] = 0
            else:
                state["failures"] += 1
                logger.warning(
                    "再生を復旧できませんでした（%d回目）。次は %s 後に再試行します。",
                    state["failures"],
                    format_duration(RECOVERY_INTERVAL_SEC),
                )
        except Exception as exc:  # noqa: BLE001 - 復旧の失敗で録音を止めない
            state["failures"] += 1
            logger.warning("復旧処理でエラーが発生しました（録音は継続します）: %s", exc)

    def _start_stream_browser(self, page_url: str):
        """設定されていれば、専用ブラウザで配信ページを開いて再生を始める。

        起動や再生に失敗しても録音は続ける（手動で再生すれば記録できるため）。
        """
        cfg = self.config.browser
        if not cfg.enabled:
            return None

        from .browser import BrowserError, browser_from_config

        browser = browser_from_config(cfg)
        try:
            browser.start()
            browser.open(page_url)
            # プレイヤーの読み込みを待ってから再生を試みる
            browser.page.wait_for_timeout(8000)
            if browser.try_play():
                logger.info("配信ページの再生を開始しました: %s", page_url)
                browser.enter_fullscreen()
            else:
                logger.warning(
                    "自動での再生を開始できませんでした。"
                    "開いているブラウザで手動で再生してください: %s",
                    page_url,
                )
            return browser
        except BrowserError as exc:
            logger.warning(
                "専用ブラウザを利用できません。手動で再生してください: %s", exc
            )
        except Exception as exc:  # noqa: BLE001 - ブラウザの問題で録音を止めない
            logger.warning(
                "専用ブラウザの操作でエラーが発生しました。手動で再生してください: %s",
                exc,
            )
        browser.stop()
        return None

    def _start_screen_recorder(self, entry: ArchiveEntry) -> ScreenRecorder | None:
        """設定されていれば画面録画を開始する。

        録画に失敗しても音声の録音は続行する（映像はあくまで補助的な機能のため）。
        """
        cfg = self.config.video
        if not cfg.enabled:
            return None

        screen = ScreenRecorder(
            # 音声と結合する前の一時ファイル
            output_path=entry.directory / "video_raw.mp4",
            display_index=cfg.display_index,
            height=cfg.height,
            framerate=cfg.framerate,
            quality_cq=cfg.quality_cq,
            crop=cfg.crop,
            encoder=cfg.encoder,
        )
        try:
            screen.start()
        except ScreenRecorderError as exc:
            logger.warning("画面録画を開始できませんでした。音声のみ記録します: %s", exc)
            return None
        return screen

    def _finalize_video(
        self,
        entry: ArchiveEntry,
        screen: ScreenRecorder | None,
        audio_path: Path | None,
        audio_duration: float,
    ) -> Path | None:
        """録画した映像に音声を合成し、1つの MP4 にまとめる。"""
        if screen is None:
            return None

        raw_path = screen.output_path
        if not raw_path.is_file() or raw_path.stat().st_size == 0:
            logger.error("映像ファイルが生成されませんでした。")
            return None

        if audio_path is None:
            logger.warning("音声が無いため、映像のみを保存します。")
            return self._rename_raw_video(raw_path, entry)

        delay = self._estimate_video_delay(raw_path, audio_duration)
        final_path = entry.video_path(".mp4")

        logger.info("映像と音声を結合しています（同期補正 %+.2f 秒）…", delay)
        if not mux_audio_video(raw_path, audio_path, final_path, delay):
            logger.warning("結合に失敗したため、映像のみを保存します。")
            return self._rename_raw_video(raw_path, entry)

        _try_unlink(raw_path, "結合前の映像")
        size_mb = final_path.stat().st_size / 1024 / 1024
        logger.info("映像を保存しました（%.1f MB）→ %s", size_mb, final_path.name)
        return final_path

    def _estimate_video_delay(self, raw_path: Path, audio_duration: float) -> float:
        """映像が音声よりどれだけ遅れて始まったかを推定する（秒）。

        録音と録画はほぼ同時に停止しているため、両者の長さの差が
        そのまま「開始時刻のずれ」に相当する。
        """
        video_duration = probe_duration(raw_path)
        if video_duration is None or audio_duration <= 0:
            return 0.0

        delay = audio_duration - video_duration
        # 想定外の値になった場合は補正しない方が安全
        if abs(delay) > 30:
            logger.warning(
                "映像と音声の長さの差が大きすぎるため同期補正を行いません"
                "（音声 %.1f 秒 / 映像 %.1f 秒）。",
                audio_duration,
                video_duration,
            )
            return 0.0
        return delay

    @staticmethod
    def _rename_raw_video(raw_path: Path, entry: ArchiveEntry) -> Path | None:
        """結合できなかった映像を、そのまま video.mp4 として残す。"""
        final_path = entry.video_path(".mp4")
        try:
            raw_path.replace(final_path)
        except OSError as exc:
            logger.warning("映像ファイルの整理に失敗しました: %s", exc)
            return raw_path if raw_path.is_file() else None
        return final_path

    def _finalize_audio(self, entry: ArchiveEntry, wav_path: Path) -> Path | None:
        """録音した WAV を設定に応じて Opus に変換する。"""
        if not wav_path.is_file() or wav_path.stat().st_size == 0:
            logger.error("録音ファイルが空です。音声デバイスの設定を確認してください。")
            return None

        if self.config.recorder.audio_format != "opus":
            return wav_path

        opus_path = entry.audio_path(".opus")
        if convert_to_opus(
            wav_path, opus_path, self.config.recorder.opus_bitrate_kbps
        ):
            wav_size_mb = wav_path.stat().st_size / 1024 / 1024
            opus_size_mb = opus_path.stat().st_size / 1024 / 1024
            _try_unlink(wav_path, "変換前の WAV")
            logger.info(
                "音声を圧縮しました: %.1f MB → %.1f MB", wav_size_mb, opus_size_mb
            )
            return opus_path
        return wav_path

    # --- 文字起こし・要約 ---------------------------------------------------

    def run_on_path(
        self,
        path: Path,
        steps: Sequence[str],
        force: bool = False,
        output_dir: Path | None = None,
        title: str | None = None,
        output_stem: str | None = None,
    ) -> None:
        """パスの種類を判別して、指定された工程を実行する。

        対象は次の3種類。
          - アーカイブフォルダ（meta.json のあるフォルダ）
          - 音声・動画ファイル（mp3 / mp4 など）
          - 文字起こしの生データ（*.transcript.json）
        """
        if path.is_dir():
            entry = ArchiveEntry.load(path)
            if entry is None:
                raise ValueError(
                    f"アーカイブフォルダとして読み込めませんでした: {path}\n"
                    "（meta.json のあるフォルダを指定してください）"
                )
            self.run_on_target(target.from_entry(entry), steps, force)
            self.archive.write_index()
            return

        if not path.is_file():
            raise FileNotFoundError(f"ファイルが見つかりません: {path}")

        self.run_on_target(
            target.from_media(path, output_dir, title, output_stem), steps, force
        )

    def run_on_target(
        self,
        item: target.ProcessTarget,
        steps: Sequence[str],
        force: bool = False,
    ) -> None:
        """処理対象に対して、指定された工程を実行する。

        アーカイブフォルダでも単体ファイルでも、ここから先の扱いは同じ。
        工程は必ず呼び出し側が指定する（既定値を持たせると、
        意図せず要約まで走って課金が発生しうるため）。
        """
        if self.STEP_TRANSCRIBE in steps:
            if item.is_transcribed and not force:
                logger.info(
                    "文字起こしは実行済みのためスキップします（--force でやり直せます）。"
                )
            else:
                self._transcribe(item)

        if self.STEP_SUMMARIZE in steps:
            if item.is_summarized and not force:
                logger.info(
                    "要約は実行済みのためスキップします（--force でやり直せます）。"
                )
            else:
                self._summarize(item)

    def _transcribe(self, item: target.ProcessTarget) -> None:
        """対象を文字起こしして保存する。"""
        if item.media_path is None or not item.media_path.is_file():
            logger.error(
                "文字起こしの対象となる音声が見つかりません: %s", item.title
            )
            return

        logger.info("文字起こしを開始します: %s", item.media_path.name)
        started = time.monotonic()
        report = _make_progress_reporter()

        try:
            with self._keep_awake(keep_display=False, reason="文字起こし中"):
                result = self.transcriber.transcribe(
                    item.media_path, progress_callback=report
                )
        except Exception:  # noqa: BLE001 - 失敗しても元の音声は残す
            logger.exception("文字起こしに失敗しました。音声ファイルは保持されます。")
            return

        write_transcript_files(
            result, item.transcript_md, item.transcript_json, item.title
        )
        item.record_transcribed(
            model=result.model, device=result.device, chars=len(result.full_text)
        )
        logger.info(
            "文字起こしが完了しました（%d文字 / 所要 %s）→ %s",
            len(result.full_text),
            format_duration(time.monotonic() - started),
            item.transcript_md.name,
        )
        if not result.segments:
            logger.warning(
                "発話を検出できませんでした。録音が無音だった可能性があります"
                "（録音デバイスの設定、または配信を再生していたかを確認してください）。"
            )

    def _summarize(self, item: target.ProcessTarget) -> None:
        """対象の文字起こしを要約して保存する。"""
        summarizer = self.summarizer
        if summarizer is None:
            logger.info(
                "要約が有効になっていないためスキップします（設定または APIキーを確認してください）。"
            )
            return

        if not item.transcript_json.is_file():
            logger.error(
                "文字起こしが見つかりません: %s\n先に `transcribe` を実行してください。",
                item.transcript_json.name,
            )
            return

        result = load_transcript(item.transcript_json)
        if not result.segments:
            logger.warning("文字起こしが空のため要約をスキップします。")
            return

        logger.info("要約を生成します（%s）…", summarizer.model)
        try:
            summary = summarizer.summarize(
                title=item.title,
                date=item.date_text,
                duration=format_timestamp(result.duration),
                transcript=build_timestamped_text(result),
            )
        except SummarizerError as exc:
            logger.error("要約に失敗しました: %s", exc)
            return
        except Exception:  # noqa: BLE001 - 要約の失敗で文字起こしを失わない
            logger.exception("要約中に予期しないエラーが発生しました。")
            return

        write_summary_file(summary, item.summary_path, item.title, item.date_text)
        item.record_summarized(
            model=summary.model,
            input_tokens=summary.input_tokens,
            output_tokens=summary.output_tokens,
        )
        logger.info(
            "要約が%sしました（概算 $%.3f）→ %s",
            "完了" if not summary.truncated else "途中で終了",
            summary.estimated_cost_usd,
            item.summary_path.name,
        )

    def close(self) -> None:
        """リソースを解放する。"""
        self.detector.close()
