"""コマンドラインインターフェース。

通常は run.ps1 から実行する（仮想環境の有効化とパス設定を代行してくれる）。

録音・録画と、文字起こし・要約は分かれている。
watch / capture は録るところまでで、そのあと transcribe / summarize を実行する。

使い方の例:
    .\\run.ps1 watch              配信を監視して自動で録音・録画（新規配信向け）
    .\\run.ps1 archives           過去の配信（アーカイブ）を一覧表示
    .\\run.ps1 capture --index 1  アーカイブを録音・録画（過去配信向け）
    .\\run.ps1 status             現在の配信状況と配信予定を表示
    .\\run.ps1 devices            録音に使えるデバイスの一覧
    .\\run.ps1 displays           画面録画に使えるディスプレイの一覧
    .\\run.ps1 record -m 10       今の画面をそのまま10分録画
    .\\run.ps1 transcribe <対象>  文字起こしを実行
    .\\run.ps1 summarize <対象>   要約を実行
      （対象はアーカイブフォルダ、または手持ちの音声・動画ファイル）
    .\\run.ps1 index              INDEX.md を再生成
    .\\run.ps1 cleanup            保持期限を過ぎた音声・映像を削除
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path

from . import DISPLAY_NAME
from .archive import JST, ArchiveManager, format_duration
from .browser import BrowserError, browser_from_config
from .config import PROJECT_ROOT, Config, load_config
from .detector import KIND_LIVE, LiveDetector
from .lock import AlreadyRunningError, RecordingLock
from .pipeline import Pipeline

logger = logging.getLogger("nicochannel_live_summarizer")

# 端末以外（GUI など）へ録画の進捗を出す間隔（秒）。
PROGRESS_INTERVAL_SEC = 60.0

# アーカイブの尺に上乗せする余裕（秒）。
# 実際の配信はAPIが返す尺より少し長いことがあるため、末尾が切れないようにする。
CAPTURE_MARGIN_SEC = 120.0


def _raise_interrupt(signum, frame) -> None:
    """Ctrl+Break を Ctrl+C と同じ中断として扱う。"""
    raise KeyboardInterrupt


def install_break_handler() -> None:
    """Ctrl+Break で KeyboardInterrupt を発生させる。

    GUI の停止ボタンは子プロセスへ CTRL_BREAK_EVENT を送る。
    Windows の既定ではこれを受けると後始末をせずに即終了してしまい、
    録画の結合・一時ファイルの削除・ロックの解放が行われない。
    Ctrl+C と同じ経路に載せることで、途中停止でも保存まで完了させる。
    """
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _raise_interrupt)


def setup_logging(verbose: bool = False) -> None:
    """ログ出力を設定する。"""
    # Windows のコンソールでも日本語・絵文字が化けないようにする
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # 依存ライブラリの冗長なログを抑える
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)


# --- 各サブコマンド ---------------------------------------------------------


def _recording_lock(config: Config, command: str) -> RecordingLock:
    """録音を伴うコマンド用の排他ロックを返す。"""
    return RecordingLock(config.archive.root, command)


def cmd_gui(config: Config, args: argparse.Namespace) -> int:
    """GUI を起動する。"""
    # tkinter の読み込みはこのコマンドでだけ必要なので遅延させる
    from .gui import launch

    return launch(config)


def cmd_watch(config: Config, args: argparse.Namespace) -> int:
    """配信を監視して自動で録音・録画する。"""
    try:
        lock = _recording_lock(config, "watch")
        lock.acquire()
    except AlreadyRunningError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    pipeline = Pipeline(config)
    try:
        pipeline.watch()
    except KeyboardInterrupt:
        print()
        logger.info("監視を終了しました。")
    finally:
        pipeline.close()
        lock.release()
    return 0


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    """現在の配信状況と配信予定を表示する。"""
    detector = LiveDetector(config.channel.normalized_url, config.channel.site_id)
    try:
        print(f"チャンネル : {config.channel.normalized_url}")
        print(f"チャンネルID: {detector.site_id}")
        print()

        live = detector.get_live_now()
        if live is not None:
            started = live.started_at.strftime("%H:%M") if live.started_at else "不明"
            print(f"● 配信中: {live.title}")
            print(f"  開始時刻: {started}")
            print(f"  URL     : {live.page_url}")
        else:
            print("○ 現在配信していません")

        print()
        upcoming = detector.get_upcoming()
        if upcoming:
            print(f"配信予定 ({len(upcoming)} 件):")
            now = datetime.now(JST)
            for live in upcoming:
                scheduled = live.scheduled_start_at
                if scheduled is None:
                    print(f"  - 日時未定  {live.title}")
                    continue
                delta = scheduled - now
                hours = delta.total_seconds() / 3600
                remaining = (
                    f"あと {hours:.1f} 時間" if hours >= 0 else f"{-hours:.1f} 時間前"
                )
                print(
                    f"  - {scheduled.strftime('%Y-%m-%d %H:%M')}  "
                    f"{live.title}  ({remaining})"
                )
        else:
            print("配信予定はありません。")

        print()
        key_state = "設定済み" if config.summarizer.api_key else "未設定（要約はスキップされます）"
        print(f"ANTHROPIC_API_KEY: {key_state}")
        print(f"アーカイブ保存先  : {config.archive.root}")
    finally:
        detector.close()
    return 0


def _format_length(seconds: float | None) -> str:
    """アーカイブの尺を整形する。

    尺が取得できない＝配信終了後の変換処理が未完了で、まだ視聴できない状態。
    """
    if not seconds:
        return "未公開"
    return format_duration(seconds)


def cmd_archives(config: Config, args: argparse.Namespace) -> int:
    """過去の配信と動画を一覧表示する。"""
    detector = LiveDetector(config.channel.normalized_url, config.channel.site_id)
    try:
        archives = detector.get_all_contents(limit=args.number)
    finally:
        detector.close()

    if not archives:
        print("過去の配信・動画が見つかりませんでした。")
        return 1

    lives = sum(1 for item in archives if item.kind == KIND_LIVE)
    print(
        f"過去の配信・動画（新しい順に {len(archives)} 件"
        f" / 配信 {lives} 件・動画 {len(archives) - lives} 件）:"
    )
    print()
    for position, item in enumerate(archives, start=1):
        when = item.date
        date_text = when.strftime("%Y-%m-%d %H:%M") if when else "日時不明"
        print(
            f"  [{position:3d}] {date_text}  {item.kind_label}  "
            f"{_format_length(item.duration_sec):>9s}  {item.title}"
        )
        print(f"        {item.content_code}")

    print()
    if any(not item.duration_sec for item in archives):
        print("※「未公開」は配信終了後の変換処理が未完了で、まだ視聴・取り込みできません。")
        print()
    print("取り込むには次を実行してください:")
    print("  .\\run.ps1 capture --index 1")
    return 0


def cmd_capture(config: Config, args: argparse.Namespace) -> int:
    """任意のタイミングで録音を開始し、アーカイブに保存する。"""
    if args.index is not None and args.index < 1:
        print("--index は 1 以上を指定してください。")
        return 1

    if args.no_video:
        config.video.enabled = False

    title = args.title
    page_url = config.channel.normalized_url
    content_code = "manual"
    started_at = None
    max_seconds: float | None = None

    # 対象のアーカイブが指定されていれば、タイトルと尺をAPIから取得する
    item = None
    if args.index:
        detector = LiveDetector(config.channel.normalized_url, config.channel.site_id)
        try:
            archives = detector.get_all_contents(limit=args.index)
            if args.index > len(archives):
                print(f"指定された番号が範囲外です（1〜{len(archives)}）。")
                return 1
            item = archives[args.index - 1]
        finally:
            detector.close()

        title = title or item.title
        page_url = item.page_url
        content_code = item.content_code
        # 動画には配信の開始時刻が無いため、公開日時まで含めて拾う
        # （None のままだと保存先フォルダが取り込んだ日付になってしまう）
        started_at = item.date

        if item.duration_sec:
            max_seconds = item.duration_sec + CAPTURE_MARGIN_SEC
        elif args.minutes is None:
            # 配信終了直後はエンコード処理が終わるまで尺が取得できず、
            # この状態ではアーカイブをまだ再生できない。
            print()
            print(f"これはまだ視聴できません: {item.title}")
            print(
                "  配信終了後の変換処理が完了していないため、再生時間を取得できませんでした。"
            )
            print("  しばらく経ってから `.\\run.ps1 archives` で尺が表示されるか確認してください。")
            print("  （時間を自分で指定して録る場合は --minutes を付けてください）")
            return 1

    if args.minutes is not None:
        max_seconds = args.minutes * 60

    if not title:
        print("タイトルを指定してください（--title、または --index）。")
        return 1

    print()
    print(f"タイトル : {title}")
    print(f"ページ   : {page_url}")
    if item is not None and item.duration_sec:
        print(f"配信の尺 : {_format_length(item.duration_sec)}")
    if max_seconds is not None:
        print(f"録音時間 : {format_duration(max_seconds)}（自動停止）")
    else:
        print("録音時間 : 無制限（Ctrl+C で停止）")
    print(f"画面録画 : {'あり' if config.video.enabled else 'なし'}")
    print()
    print("★ 先にこのコマンドを開始し、そのあとブラウザで再生を始めてください。")
    print("  （再生中の音声・画面をキャプチャするため、再生していないと無音になります）")
    print()

    try:
        lock = _recording_lock(config, "capture")
        lock.acquire()
    except AlreadyRunningError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    pipeline = Pipeline(config)
    try:
        entry = pipeline.capture(
            title=title,
            page_url=page_url,
            content_code=content_code,
            started_at=started_at,
            max_seconds=max_seconds,
        )
    except KeyboardInterrupt:
        print()
        logger.info("取り込みを中断しました。")
        return 130
    finally:
        pipeline.close()
        lock.release()

    print()
    print(f"保存先: {entry.directory}")
    print()
    print("文字起こしと要約は別のコマンドで実行します:")
    print(f'  .\run.ps1 transcribe "{entry.directory}"')
    print(f'  .\run.ps1 summarize  "{entry.directory}"')
    return 0


def _start_browser(config: Config):
    """専用ブラウザを起動する。失敗したらエラーを表示して None を返す。"""
    browser = browser_from_config(config.browser)
    try:
        browser.start()
    except BrowserError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return None
    return browser


def cmd_browser_login(config: Config, args: argparse.Namespace) -> int:
    """専用ブラウザを開き、利用者自身にログインしてもらう。"""
    profile = config.browser.resolve_profile_dir(PROJECT_ROOT)
    print("専用ブラウザを起動します。")
    print(f"  プロファイル: {profile}")
    print()
    print("開いたウィンドウでチャンネルにログインしてください。")
    print("本ツールが ID / パスワードを受け取ることはありません。")
    print()

    browser = _start_browser(config)
    if browser is None:
        return 1

    try:
        browser.open(config.channel.normalized_url)
        print("─" * 60)
        input("ログインが完了したら Enter を押してください… ")
        print("─" * 60)

        if browser.is_logged_in():
            print("ログイン済みとして認識しました。")
        else:
            print("⚠ ログイン状態を確認できませんでした。")
            print("  画面上でログインできているか確認し、必要なら再実行してください。")

        print(f"\nログイン情報は次のフォルダに保存されました: {profile}")
        print("このフォルダにはセッション情報が含まれるため、共有しないでください。")
        print("\n続けて次のコマンドで再生できるか確認してください:")
        print("  .\\run.ps1 browser-check --index 1")
    finally:
        browser.stop()
    return 0


def cmd_browser_check(config: Config, args: argparse.Namespace) -> int:
    """専用ブラウザで配信ページを開き、実際に再生できるかを確認する。"""
    detector = LiveDetector(config.channel.normalized_url, config.channel.site_id)
    try:
        if args.url:
            url = args.url
            label = url
        else:
            archives = detector.get_all_contents(limit=args.index)
            if args.index > len(archives):
                print(f"指定された番号が範囲外です（1〜{len(archives)}）。")
                return 1
            item = archives[args.index - 1]
            if not item.duration_sec:
                print(f"このアーカイブはまだ視聴できません: {item.title}")
                return 1
            url = item.page_url
            label = item.title
    finally:
        detector.close()

    print(f"確認対象: {label}")
    print(f"  {url}")
    print()

    browser = _start_browser(config)
    if browser is None:
        return 1

    try:
        browser.open(url)
        print("ページの読み込みを待っています…")
        browser.page.wait_for_timeout(args.wait * 1000)

        print(f"  ログイン状態   : {'ログイン済み' if browser.is_logged_in() else '未ログインの可能性'}")

        state = browser.playback_state()
        print(f"  プレイヤー     : {'あり' if state.has_video else 'なし'}")
        if state.has_video:
            print(f"  再生位置       : {state.current_time:.1f} 秒")
            print(f"  一時停止       : {state.paused}")
            print(f"  読み込み状態   : readyState={state.ready_state}")
            print(f"  音量           : {state.volume:.2f}（ミュート={state.muted}）")
        print(f"  判定           : {state.describe()}")

        if not state.is_playing:
            print("\n自動での再生を試みます…")
            recovered = browser.recover()
            print(f"  復旧結果       : {'成功' if recovered else '失敗'}")
            state = browser.playback_state()
            print(f"  判定           : {state.describe()}")

        print()
        if state.is_playing:
            print("✅ 自動操作でも再生できました。")
            result = 0
        else:
            print("❌ 自動操作では再生できませんでした。次の順に確認してください。")
            print("   1. その配信を普段のブラウザで視聴できますか")
            print("      （プラン対象外の配信は自動操作でも再生できません）")
            print("   2. `.\\run.ps1 browser-login` でログインは済んでいますか")
            print("   3. 1と2に問題がなければ、サイト側が自動操作を制限している可能性があります")
            result = 1

        input("\n画面を確認したら Enter を押してください（ブラウザを閉じます）… ")
    finally:
        browser.stop()
    return result


def cmd_devices(config: Config, args: argparse.Namespace) -> int:
    """録音に使えるループバックデバイスの一覧を表示する。"""
    from .ffmpeg_tools import ffmpeg_available
    from .recorder import list_loopback_devices

    devices = list_loopback_devices()
    if not devices:
        print("ループバックデバイスが見つかりませんでした。")
        return 1

    print("録音に使えるデバイス:")
    for device in devices:
        mark = " ← 既定（設定が空ならこれを使用）" if device.is_default else ""
        print(f"  [{device.index:3d}] {device.name}")
        print(f"        {device.channels} ch / {device.sample_rate} Hz{mark}")

    print()
    print(f"ffmpeg: {'利用可能' if ffmpeg_available() else '見つかりません（WAVのまま保存されます）'}")
    return 0


def cmd_record(config: Config, args: argparse.Namespace) -> int:
    """今表示している画面をそのまま録画する。

    配信ページかどうかを問わず、ディスプレイに映っているものを記録する。
    専用ブラウザは起動せず、アーカイブにも登録しない。
    """
    # watch / capture と同時に走らせると録音デバイスとGPUを奪い合い、
    # 本命の録画を劣化させるため、他の録画コマンドとは排他にする。
    try:
        lock = _recording_lock(config, "record")
        lock.acquire()
    except AlreadyRunningError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    try:
        return _run_record(config, args)
    finally:
        lock.release()


def _run_record(config: Config, args: argparse.Namespace) -> int:
    """record コマンドの本体。ロックの解放は呼び出し側が行う。"""
    import time

    from .ffmpeg_tools import mux_audio_video, probe_duration
    from .recorder import SystemAudioRecorder
    from .screen_recorder import ScreenRecorder, ScreenRecorderError

    max_seconds = args.minutes * 60 if args.minutes else None
    stamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    # 既定の保存先はアーカイブフォルダ。--output があればそちらを優先する。
    output = (
        Path(args.output).resolve()
        if args.output
        else config.archive.root / f"record_{stamp}.mp4"
    )
    if output.suffix.lower() != ".mp4":
        output = output.with_suffix(".mp4")
    output.parent.mkdir(parents=True, exist_ok=True)

    video_tmp = output.with_name(f"{output.stem}_video.mp4")
    wav_tmp = output.with_name(f"{output.stem}_audio.wav")

    screen = ScreenRecorder(
        output_path=video_tmp,
        display_index=config.video.display_index,
        height=config.video.height,
        framerate=config.video.framerate,
        quality_cq=config.video.quality_cq,
        crop=config.video.crop,
        encoder=config.video.encoder,
    )
    recorder = None
    if not args.no_audio:
        recorder = SystemAudioRecorder(
            output_path=wav_tmp,
            device_name=config.recorder.device_name,
            # 停止はこのあとのループで判断する。ここは暴走を防ぐ保険。
            max_seconds=config.recorder.max_hours * 3600,
        )

    print(f"ディスプレイ {config.video.display_index} を {config.video.height}p で録画します。")
    print(f"  出力: {output}")
    if max_seconds is None:
        print("  停止するには Ctrl+C を押してください。")
    else:
        print(f"  {format_duration(max_seconds)} 後に自動停止します（Ctrl+C で途中終了）。")
    print("  画面に映っているものがそのまま記録されます。")
    print("  別のアプリに切り替えると、そのアプリが記録されます。")
    print()

    try:
        screen.start()
    except ScreenRecorderError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    if recorder is not None:
        recorder.start()

    # 端末なら同じ行を書き換えて進捗を出す。GUI など出力先がファイルの場合は
    # 1秒ごとに行が積み上がってログが読めなくなるため、一定間隔に間引く。
    interactive = sys.stdout.isatty()
    started = time.monotonic()
    last_report = 0.0
    try:
        while True:
            time.sleep(1)
            elapsed = time.monotonic() - started
            # 画面のロックなどで録画が落ちていれば、警告を出して打ち切る
            if not screen.check_alive():
                break
            if max_seconds is not None and elapsed >= max_seconds:
                break
            if interactive:
                print(f"\r  録画中… {format_duration(elapsed)}", end="", flush=True)
            elif elapsed - last_report >= PROGRESS_INTERVAL_SEC:
                last_report = elapsed
                print(f"  録画中… {format_duration(elapsed)}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        if interactive:
            print()
        # 映像と音声の終端を揃えるため、先に停止を伝えてから終了を待つ
        screen.request_stop()
        # 録音の停止が失敗しても、画面録画の後始末は必ず行う
        # （待たずに抜けると ffmpeg が残り、映像ファイルも壊れる）
        try:
            if recorder is not None:
                recorder.stop()
        finally:
            screen.wait_stop()

    if not video_tmp.is_file() or video_tmp.stat().st_size == 0:
        print("録画ファイルが作られませんでした。", file=sys.stderr)
        return 1

    if recorder is None:
        video_tmp.replace(output)
    else:
        # 録音と録画はほぼ同時に停止しているため、長さの差が開始のずれに相当する
        video_duration = probe_duration(video_tmp)
        delay = 0.0
        if video_duration and recorder.recorded_seconds > 0:
            delay = recorder.recorded_seconds - video_duration
            if abs(delay) > 30:
                delay = 0.0
        print(f"映像と音声を結合しています（同期補正 {delay:+.2f} 秒）…")
        if not mux_audio_video(video_tmp, wav_tmp, output, delay):
            print("音声との結合に失敗しました。映像と音声を個別に残します。", file=sys.stderr)
            print(f"  映像: {video_tmp}")
            print(f"  音声: {wav_tmp}")
            return 1
        video_tmp.unlink(missing_ok=True)
        wav_tmp.unlink(missing_ok=True)

    size_mb = output.stat().st_size / 1024 / 1024
    print(f"録画完了: {output} ({size_mb:.1f} MB)")
    if recorder is not None and recorder.silence_ratio > 0.95:
        print(
            f"⚠ 録音の {recorder.silence_ratio * 100:.0f}% が無音でした。"
            "音声を再生していたか、録音デバイスの設定を確認してください。"
        )
    return 0


STEP_LABELS = {"transcribe": "文字起こし", "summarize": "要約"}


def _run_steps(config: Config, args: argparse.Namespace, steps: tuple[str, ...]) -> int:
    """transcribe / summarize の共通処理。

    対象はアーカイブフォルダでも、手持ちの音声・動画ファイルでもよい。
    """
    paths = [Path(p).resolve() for p in args.paths]
    missing = [p for p in paths if not p.exists()]
    if missing:
        for path in missing:
            print(f"見つかりません: {path}")
        return 1

    output_dir = Path(args.output).resolve() if getattr(args, "output", None) else None
    title = getattr(args, "title", None)
    if title and len(paths) > 1:
        print("⚠ --title は複数指定時には使えないため無視します。")
        title = None

    if "summarize" in steps and not config.summarizer.available:
        if not config.summarizer.enabled:
            print("⚠ config.toml で要約が無効のため、要約はスキップされます。")
        else:
            print("⚠ ANTHROPIC_API_KEY が未設定のため、要約はスキップされます。")

    # 同じ出力先に「名前が同じで拡張子だけ違う」ファイルを出すと上書きされるため、
    # その場合だけ出力名に拡張子を含める。
    file_stems = [p.stem for p in paths if p.is_file()]
    keep_extension = len(set(file_stems)) != len(file_stems)
    if keep_extension:
        print("※ 名前が重複するため、出力ファイル名に拡張子を含めます。")

    print(f"実行する工程: {' → '.join(STEP_LABELS[s] for s in steps)}")

    pipeline = Pipeline(config)
    failed = 0
    try:
        for position, path in enumerate(paths, start=1):
            if len(paths) > 1:
                print(f"\n[{position}/{len(paths)}] {path.name}")
            try:
                pipeline.run_on_path(
                    path=path,
                    steps=steps,
                    force=args.force,
                    output_dir=output_dir,
                    title=title,
                    output_stem=path.name if keep_extension and path.is_file() else None,
                )
            except (FileNotFoundError, ValueError) as exc:
                failed += 1
                print(f"エラー: {exc}")
            except Exception as exc:  # noqa: BLE001 - 1件失敗しても残りを続ける
                failed += 1
                logger.error("処理に失敗しました (%s): %s", path.name, exc)
    finally:
        pipeline.close()

    if failed:
        print(f"\n{failed} 件が失敗しました。")
        return 1
    return 0


def cmd_transcribe(config: Config, args: argparse.Namespace) -> int:
    """文字起こしだけを実行する。"""
    return _run_steps(config, args, (Pipeline.STEP_TRANSCRIBE,))


def cmd_summarize(config: Config, args: argparse.Namespace) -> int:
    """要約だけを実行する。"""
    return _run_steps(config, args, (Pipeline.STEP_SUMMARIZE,))


def cmd_index(config: Config, args: argparse.Namespace) -> int:
    """INDEX.md を再生成する。"""
    manager = ArchiveManager(config.archive.root)
    path = manager.write_index()
    entries = manager.list_entries()
    print(f"INDEX.md を更新しました ({len(entries)} 件) → {path}")
    return 0


def cmd_displays(config: Config, args: argparse.Namespace) -> int:
    """画面録画に使えるディスプレイの一覧を表示する。"""
    from .screen_recorder import ScreenRecorderError, probe_displays

    try:
        displays = probe_displays()
    except ScreenRecorderError as exc:
        print(f"ディスプレイを調べられませんでした: {exc}")
        return 1

    if not displays:
        print("録画できるディスプレイが見つかりませんでした。")
        return 1

    print("画面録画に使えるディスプレイ:")
    for display in displays:
        mark = " ← 現在の設定" if display.index == config.video.display_index else ""
        print(f"  display_index = {display.index}   {display.width}x{display.height}{mark}")

    print()
    print("config.toml の [video] display_index で切り替えられます。")
    state = "有効" if config.video.enabled else "無効"
    print(f"現在の画面録画: {state}（{config.video.height}p / {config.video.framerate}fps）")
    return 0


def cmd_cleanup(config: Config, args: argparse.Namespace) -> int:
    """保持期限を過ぎた音声・映像ファイルを削除する。"""
    audio_days = config.retention.audio_days
    video_days = config.retention.video_days

    manager = ArchiveManager(config.archive.root)
    labels = {"audio": "音声", "video": "映像"}

    if args.dry_run:
        total = 0
        for kind, days in (("audio", audio_days), ("video", video_days)):
            targets = manager.find_expired(days, kind)
            total += len(targets)
            if days <= 0:
                print(f"{labels[kind]}: 保持日数が 0 のため削除しません（永久保持）")
                continue
            print(f"{labels[kind]}: 削除対象 {len(targets)} 件（{days} 日より古い）")
            for entry, media in targets:
                size_mb = media.stat().st_size / 1024 / 1024
                print(f"  - {entry.directory.name}/{media.name}  ({size_mb:.1f} MB)")
        if total == 0:
            print("削除対象はありませんでした。")
        return 0

    deleted = manager.cleanup_old_media(audio_days, video_days)
    manager.write_index()

    total = sum(len(paths) for paths in deleted.values())
    if total == 0:
        print("削除対象はありませんでした。")
        return 0

    for kind, paths in deleted.items():
        if not paths:
            continue
        print(f"{labels[kind]}: {len(paths)} 件を削除しました")
        for path in paths:
            print(f"  - {path.parent.name}/{path.name}")
    print(f"合計 {total} 件を削除しました。")
    return 0


# --- エントリポイント -------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """コマンドライン引数のパーサを組み立てる。"""
    parser = argparse.ArgumentParser(
        prog="run.ps1",
        description=(
            f"{DISPLAY_NAME} — "
            "ニコニコチャンネルプラスの配信を自動で録音・文字起こし・要約します。"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="詳細ログを出力")
    parser.add_argument("-c", "--config", type=Path, help="設定ファイルのパス")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("gui", help="ボタンで操作できる画面を開く")
    sub.add_parser("watch", help="配信を監視して自動で録音・録画する")
    sub.add_parser("status", help="現在の配信状況と配信予定を表示する")
    sub.add_parser("devices", help="録音に使えるデバイスを一覧表示する")
    sub.add_parser("displays", help="画面録画に使えるディスプレイを一覧表示する")

    sub.add_parser(
        "browser-login", help="専用ブラウザを開いてログインする（初回に1度だけ）"
    )

    browser_check = sub.add_parser(
        "browser-check", help="専用ブラウザで再生できるかを確認する"
    )
    browser_check.add_argument(
        "--index",
        type=int,
        default=1,
        help="archives の番号（既定: 1）。視聴できる配信を指定してください",
    )
    browser_check.add_argument("--url", help="確認するURLを直接指定する")
    browser_check.add_argument(
        "-w", "--wait", type=int, default=15, help="読み込みを待つ秒数（既定: 15）"
    )

    archives = sub.add_parser("archives", help="過去の配信と動画を一覧表示する")
    archives.add_argument(
        "-n", "--number", type=int, help="表示件数（既定: すべて）"
    )

    capture = sub.add_parser(
        "capture", help="任意のタイミングで録音を開始し、アーカイブに保存する"
    )
    capture.add_argument("--index", type=int, help="archives で表示された番号（配信・動画とも）")
    capture.add_argument("-t", "--title", help="タイトル（未指定ならAPIから取得）")
    capture.add_argument(
        "-m", "--minutes", type=float, help="録音する分数（指定するとAPIの尺より優先）"
    )
    capture.add_argument(
        "--no-video", action="store_true", help="画面録画をせず音声のみ取り込む"
    )

    record = sub.add_parser("record", help="今の画面をそのまま録画する")
    record.add_argument(
        "-m", "--minutes", type=float,
        help="録画する分数（未指定なら Ctrl+C まで）",
    )
    record.add_argument("-o", "--output", help="出力ファイル名（既定: record_日時.mp4）")
    record.add_argument(
        "--no-audio", action="store_true", help="音声を録らず映像だけ記録する"
    )

    # transcribe と summarize は対象の指定方法が同じなので共通化する
    target_help = (
        "アーカイブフォルダ、または音声・動画ファイル（複数指定可）"
    )
    for name, help_text in (
        ("transcribe", "文字起こしだけを実行する"),
        ("summarize", "要約だけを実行する"),
    ):
        parser_obj = sub.add_parser(name, help=help_text)
        parser_obj.add_argument("paths", nargs="+", help=target_help)
        parser_obj.add_argument(
            "-f", "--force", action="store_true", help="実行済みでもやり直す"
        )
        parser_obj.add_argument(
            "-o", "--output", help="出力先フォルダ（ファイル指定時のみ有効）"
        )
        parser_obj.add_argument(
            "-t", "--title", help="タイトル（既定: ファイル名 / 配信タイトル）"
        )

    sub.add_parser("index", help="INDEX.md を再生成する")

    cleanup = sub.add_parser("cleanup", help="保持期限を過ぎた音声・映像を削除する")
    cleanup.add_argument(
        "-n", "--dry-run", action="store_true", help="削除せず対象だけ表示する"
    )

    return parser


COMMANDS = {
    "gui": cmd_gui,
    "watch": cmd_watch,
    "status": cmd_status,
    "devices": cmd_devices,
    "displays": cmd_displays,
    "archives": cmd_archives,
    "capture": cmd_capture,
    "record": cmd_record,
    "browser-login": cmd_browser_login,
    "browser-check": cmd_browser_check,
    "transcribe": cmd_transcribe,
    "summarize": cmd_summarize,
    "index": cmd_index,
    "cleanup": cmd_cleanup,
}


def main(argv: list[str] | None = None) -> int:
    """エントリポイント。"""
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    install_break_handler()

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"設定の読み込みに失敗しました: {exc}", file=sys.stderr)
        return 1

    try:
        return COMMANDS[args.command](config, args)
    except KeyboardInterrupt:
        print()
        return 130
    except Exception as exc:  # noqa: BLE001 - CLIの最終防衛線
        logger.exception("処理中にエラーが発生しました: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
