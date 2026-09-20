"""gui: コマンドをボタン操作で実行するための画面（tkinter）。

各コマンドは別プロセス（python -m nicochannel_live_summarizer ...）として起動する。
理由は3つある。

  - 長時間の録画中も画面が固まらない
  - 停止ボタンから Ctrl+C 相当（CTRL_BREAK_EVENT）を送れるため、
    途中停止しても「ここまでの録音を保存する」既存の処理がそのまま働く
  - 多重起動を防ぐロック（lock.py）が、コマンド実行時と同じように効く

追加の依存パッケージは不要（tkinter は Python 標準）。
"""

from __future__ import annotations

import ctypes
import os
import queue
import signal
import subprocess
import sys
import threading
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import DISPLAY_NAME, __version__
from .archive import ArchiveManager, format_duration
from .config import PROJECT_ROOT, Config, load_config

# ShowWindow に渡す「隠す」指定
SW_HIDE = 0

# ログ表示の更新間隔（ミリ秒）
POLL_INTERVAL_MS = 100

# 画面に保持するログの最大行数（長時間の録画でメモリを食い潰さないため）
MAX_LOG_LINES = 5000


def _console_python() -> str:
    """子プロセスを起動する python.exe のパスを返す。

    2点の理由から sys.executable をそのまま使わない。

      - gui.bat は pythonw.exe で GUI を起動する。子まで pythonw.exe になると
        コンソール制御イベントを受け取れず、停止ボタンが効かなくなる
      - 起動のされ方によっては sys.executable が仮想環境の外を指すことがあり、
        その場合は依存パッケージが見つからず、どのコマンドも失敗する

    そのため仮想環境の python.exe を最優先で使う。
    """
    venv_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_python.is_file():
        return str(venv_python)

    executable = sys.executable
    if executable.lower().endswith("pythonw.exe"):
        return executable[: -len("pythonw.exe")] + "python.exe"
    return executable


class CommandRunner:
    """コマンドを子プロセスとして実行し、出力を1行ずつ受け取る。"""

    def __init__(self) -> None:
        self.output: queue.Queue[str | None] = queue.Queue()
        self._process: subprocess.Popen | None = None

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self, args: list[str]) -> None:
        """コマンドを開始する。出力は output キューに流れる。"""
        if self.is_running:
            raise RuntimeError("すでに実行中です。")

        # 前回の実行が残した出力・終了通知を捨てる
        # （残っていると、始めたばかりのコマンドが終了したと誤判定される）
        while not self.output.empty():
            self.output.get_nowait()

        env = dict(os.environ)
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        # 出力をためこませず、画面へ即座に流すため
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        command = [_console_python(), "-m", "nicochannel_live_summarizer", *args]
        self.output.put("$ run.ps1 " + " ".join(args))

        self._process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            # 停止ボタンから CTRL_BREAK_EVENT を送るために必要
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        """子プロセスの出力を読み取ってキューへ流す。"""
        process = self._process
        if process is None or process.stdout is None:
            return
        for raw in process.stdout:
            self.output.put(raw.decode("utf-8", errors="replace").rstrip())
        process.wait()
        self.output.put(None)  # 終了の合図

    def stop(self) -> None:
        """Ctrl+C 相当を送って、後始末をさせたうえで終了させる。"""
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            os.kill(process.pid, signal.CTRL_BREAK_EVENT)
        except OSError:
            process.terminate()

    def kill(self) -> None:
        """応答しない場合に強制終了する。"""
        process = self._process
        if process is not None and process.poll() is None:
            process.kill()

    @property
    def returncode(self) -> int | None:
        return self._process.poll() if self._process is not None else None


class App(tk.Tk):
    """メインウィンドウ。"""

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.settings = config
        self.runner = CommandRunner()
        self.archive = ArchiveManager(config.archive.root)
        self._action_buttons: list[ttk.Button] = []
        self._entry_paths: dict[str, Path] = {}

        self.title(f"{DISPLAY_NAME} v{__version__}")
        self.geometry("1180x930")
        self.minsize(980, 700)

        self._build_layout()
        self.refresh_archives()
        self.after(POLL_INTERVAL_MS, self._drain_output)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # --- 画面の組み立て -----------------------------------------------------

    def _build_layout(self) -> None:
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill="both", expand=True)

        # 上段: 操作パネル（左） と ログ（右）
        top = ttk.Frame(outer)
        top.pack(fill="both", expand=True)
        self._build_controls(top)
        self._build_log(top)

        # 下段: 保存済みの録画一覧
        self._build_archive_list(outer)

        self.status_var = tk.StringVar(value="待機中")
        ttk.Label(
            outer, textvariable=self.status_var, anchor="w",
            relief="sunken", padding=(6, 3),
        ).pack(fill="x", pady=(8, 0))

    def _build_controls(self, parent: ttk.Frame) -> None:
        holder = ttk.Frame(parent, width=368)
        holder.pack(side="left", fill="y", padx=(0, 8))
        holder.pack_propagate(False)

        # 停止ボタンは最優先で下端に確保する。
        # 先に pack しておかないと、上の内容が増えたときに画面外へ押し出され、
        # 実行中の処理を止められなくなる。
        self.stop_button = ttk.Button(holder, text="■ 実行中の処理を停止",
                                      command=self._on_stop, state="disabled")
        self.stop_button.pack(side="bottom", fill="x", pady=(6, 0), ipady=6)

        # 操作ボタンはスクロールできるようにしておく。
        # ウィンドウを小さくしても、すべての項目に手が届くようにするため。
        canvas = tk.Canvas(holder, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)

        panel = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=panel, anchor="nw")

        def is_scroll_needed() -> bool:
            """中身が表示領域に収まりきらないかを返す。"""
            return panel.winfo_reqheight() > canvas.winfo_height()

        def sync_scroll() -> None:
            """スクロール範囲を更新し、必要なときだけスクロールバーを出す。

            スクロール範囲は最低でも表示領域の高さぶん確保する。
            範囲が表示領域より小さいと Tk は中身を上下に動かせてしまい、
            スクロールの必要が無いのに上側へ余白ができてしまう。
            """
            view = max(canvas.winfo_height(), 1)
            canvas.configure(
                scrollregion=(0, 0, canvas.winfo_width(),
                              max(panel.winfo_reqheight(), view))
            )
            needed = is_scroll_needed()
            if needed and not bar.winfo_ismapped():
                bar.pack(side="right", fill="y", before=canvas)
            elif not needed and bar.winfo_ismapped():
                bar.pack_forget()
            if not needed:
                canvas.yview_moveto(0)

        panel.bind("<Configure>", lambda _event: sync_scroll())
        canvas.bind(
            "<Configure>",
            lambda event: (canvas.itemconfigure(window, width=event.width), sync_scroll()),
        )

        def scroll_wheel(event) -> None:
            # 収まっているときはホイールでも動かさない
            if is_scroll_needed():
                canvas.yview_scroll(-event.delta // 120, "units")

        # ホイールはこのパネル上にいるときだけ受け取る（ログ側の操作を邪魔しない）
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", scroll_wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        # --- 録画 ---
        box = ttk.LabelFrame(panel, text="録画", padding=6)
        box.pack(fill="x", pady=(0, 6))

        self._button(box, "配信を監視して自動録画", lambda: self.run(["watch"])).pack(fill="x")

        # 設定はそれを使うボタンと同じ枠に入れて、対応関係が分かるようにする
        group = ttk.LabelFrame(box, text="過去の配信を取り込む", padding=5)
        group.pack(fill="x", pady=(6, 0))
        row = ttk.Frame(group)
        row.pack(fill="x")
        ttk.Label(row, text="アーカイブ番号").pack(side="left")
        self.capture_index = tk.StringVar(value="1")
        ttk.Spinbox(row, from_=1, to=500, width=5,
                    textvariable=self.capture_index).pack(side="left", padx=4)
        self.capture_no_video = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="音声のみ", variable=self.capture_no_video).pack(side="left")
        self._button(group, "取り込みを開始", self._run_capture).pack(fill="x", pady=(6, 0))

        group = ttk.LabelFrame(box, text="今の画面をそのまま録画", padding=5)
        group.pack(fill="x", pady=(6, 0))
        row = ttk.Frame(group)
        row.pack(fill="x")
        ttk.Label(row, text="録画する分数").pack(side="left")
        self.record_minutes = tk.StringVar(value="10")
        ttk.Entry(row, width=6, textvariable=self.record_minutes).pack(side="left", padx=4)
        self.record_no_audio = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="音声なし", variable=self.record_no_audio).pack(side="left")
        self._button(group, "録画を開始", self._run_record).pack(fill="x", pady=(6, 0))
        self._hint(group, "分数を空にすると、停止を押すまで録画し続けます。")

        # --- 文字起こし・要約 ---
        box = ttk.LabelFrame(panel, text="文字起こし・要約", padding=6)
        box.pack(fill="x", pady=(0, 6))
        grid = ttk.Frame(box)
        grid.pack(fill="x")
        self._button(grid, "ファイルを文字起こし",
                     lambda: self._run_on_files("transcribe")).grid(
            row=0, column=0, sticky="ew", padx=2)
        self._button(grid, "ファイルを要約",
                     lambda: self._run_on_files("summarize")).grid(
            row=0, column=1, sticky="ew", padx=2)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        # --- 確認 ---
        box = ttk.LabelFrame(panel, text="確認", padding=6)
        box.pack(fill="x", pady=(0, 6))
        grid = ttk.Frame(box)
        grid.pack(fill="x")
        pairs = [
            ("配信状況", ["status"]),
            ("配信一覧", ["archives"]),
            ("録音デバイス", ["devices"]),
            ("ディスプレイ", ["displays"]),
        ]
        for position, (label, args) in enumerate(pairs):
            self._button(grid, label, lambda a=args: self.run(a)).grid(
                row=position // 2, column=position % 2, sticky="ew", padx=2, pady=2
            )
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        # 実行を伴わないので、処理中でも押せるようにしておく
        ttk.Button(box, text="チャンネルをブラウザで開く",
                   command=self._open_channel).pack(fill="x", pady=(6, 0))
        self._button(box, "専用ブラウザにログイン（初回のみ）",
                     lambda: self.run(["browser-login"])).pack(fill="x", pady=(4, 0))

        group = ttk.LabelFrame(box, text="再生できるか確認", padding=5)
        group.pack(fill="x", pady=(6, 0))
        row = ttk.Frame(group)
        row.pack(fill="x")
        ttk.Label(row, text="アーカイブ番号").pack(side="left")
        self.check_index = tk.StringVar(value="1")
        ttk.Spinbox(row, from_=1, to=500, width=5,
                    textvariable=self.check_index).pack(side="left", padx=4)
        self._button(group, "確認を開始", self._run_browser_check).pack(fill="x", pady=(6, 0))

        # --- 整理 ---
        box = ttk.LabelFrame(panel, text="整理", padding=6)
        box.pack(fill="x", pady=(0, 6))
        grid = ttk.Frame(box)
        grid.pack(fill="x")
        self._button(grid, "INDEX.md を更新", lambda: self.run(["index"])).grid(
            row=0, column=0, sticky="ew", padx=2)
        self._button(grid, "削除対象を確認", lambda: self.run(["cleanup", "--dry-run"])).grid(
            row=0, column=1, sticky="ew", padx=2)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        self._button(box, "保持期限を過ぎたファイルを削除",
                     self._run_cleanup).pack(fill="x", pady=(4, 0))

    def _button(self, parent, text: str, command) -> ttk.Button:
        """実行中は自動で無効になるボタンを作る。"""
        button = ttk.Button(parent, text=text, command=command)
        self._action_buttons.append(button)
        return button

    @staticmethod
    def _hint(parent, text: str) -> None:
        """ボタンの下に添える説明を置く。"""
        ttk.Label(parent, text=text, wraplength=310,
                  foreground="#555555").pack(fill="x", pady=(2, 0))

    def _build_log(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="ログ", padding=4)
        box.pack(side="left", fill="both", expand=True)

        self.log = tk.Text(box, wrap="none", state="disabled", font=("Consolas", 9),
                           background="#1e1e1e", foreground="#e6e6e6")
        scroll_y = ttk.Scrollbar(box, orient="vertical", command=self.log.yview)
        scroll_x = ttk.Scrollbar(box, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)

        self.log.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)

        # 見落としたくない行に色を付ける
        self.log.tag_configure("command", foreground="#7fd1ff")
        self.log.tag_configure("warning", foreground="#ffcc66")
        self.log.tag_configure("error", foreground="#ff8080")
        self.log.tag_configure("done", foreground="#9fe08f")

        bar = ttk.Frame(box)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Button(bar, text="ログを消去", command=self._clear_log).pack(side="right")

    def _build_archive_list(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="保存済みの録画", padding=6)
        box.pack(fill="both", pady=(8, 0))

        columns = (
            ("date", "配信日時", 130),
            ("title", "タイトル", 470),
            ("duration", "長さ", 90),
            ("transcribed", "文字起こし", 90),
            ("summarized", "要約", 70),
        )
        self.tree = ttk.Treeview(
            box, columns=[key for key, _, _ in columns], show="headings", height=4
        )
        for key, label, width in columns:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w")

        scroll = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)

        bar = ttk.Frame(box)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self._button(bar, "選択したものを文字起こし",
                     lambda: self._run_on_selection("transcribe")).pack(side="left")
        self._button(bar, "選択したものを要約",
                     lambda: self._run_on_selection("summarize")).pack(side="left", padx=4)
        ttk.Button(bar, text="フォルダを開く", command=self._open_selection).pack(side="left")
        ttk.Button(bar, text="一覧を更新", command=self.refresh_archives).pack(side="right")

    # --- 一覧 ---------------------------------------------------------------

    def refresh_archives(self) -> None:
        """アーカイブ一覧を読み直して表示する。"""
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._entry_paths = {}

        try:
            entries = self.archive.list_entries()
        except OSError as exc:
            self._append(f"アーカイブを読み込めませんでした: {exc}", "error")
            return

        for entry in entries:
            started = entry.started_at
            duration = entry.meta.get("duration_sec")
            item = self.tree.insert("", "end", values=(
                started.strftime("%Y-%m-%d %H:%M") if started else "不明",
                entry.title,
                format_duration(duration) if duration else "—",
                "済" if entry.meta.get("transcribed") else "—",
                "済" if entry.meta.get("summarized") else "—",
            ))
            self._entry_paths[item] = entry.directory

    def _selected_paths(self) -> list[Path]:
        return [self._entry_paths[item] for item in self.tree.selection()
                if item in self._entry_paths]

    def _open_selection(self) -> None:
        paths = self._selected_paths()
        if not paths:
            messagebox.showinfo("選択してください", "一覧から録画を選んでください。")
            return
        for path in paths:
            os.startfile(path)

    # --- 実行 ---------------------------------------------------------------

    def run(self, args: list[str]) -> None:
        """コマンドを開始し、画面を実行中の状態にする。"""
        if self.runner.is_running:
            messagebox.showwarning("実行中です", "先に実行中の処理を停止してください。")
            return
        try:
            self.runner.start(args)
        except OSError as exc:
            self._append(f"起動に失敗しました: {exc}", "error")
            return
        self._set_running(True, args[0])

    def _open_channel(self) -> None:
        """チャンネルのトップページを既定のブラウザで開く。"""
        url = self.settings.channel.normalized_url
        self._append(f"ブラウザでチャンネルを開きます: {url}")
        webbrowser.open(url)

    def _read_index(self, variable: tk.StringVar) -> int | None:
        """アーカイブ番号の入力を読み取る。不正なら None。"""
        try:
            return int(variable.get())
        except ValueError:
            messagebox.showerror("入力を確認してください",
                                 "アーカイブ番号は数字で指定してください。")
            return None

    def _run_capture(self) -> None:
        index = self._read_index(self.capture_index)
        if index is None:
            return
        args = ["capture", "--index", str(index)]
        if self.capture_no_video.get():
            args.append("--no-video")
        self.run(args)

    def _run_record(self) -> None:
        args = ["record"]
        minutes = self.record_minutes.get().strip()
        if minutes:
            try:
                float(minutes)
            except ValueError:
                messagebox.showerror("入力を確認してください",
                                     "録画する分数は数字で指定してください。")
                return
            args += ["--minutes", minutes]
        if self.record_no_audio.get():
            args.append("--no-audio")
        self.run(args)

    def _run_browser_check(self) -> None:
        index = self._read_index(self.check_index)
        if index is None:
            return
        self.run(["browser-check", "--index", str(index)])

    def _run_cleanup(self) -> None:
        if not messagebox.askyesno(
            "削除してよろしいですか",
            "保持期限を過ぎた音声・映像を削除します。\n"
            "文字起こしと要約は削除されません。\n\n"
            "先に「削除対象を確認」で内容を確かめることをおすすめします。",
        ):
            return
        self.run(["cleanup"])

    def _run_on_selection(self, step: str) -> None:
        paths = self._selected_paths()
        if not paths:
            messagebox.showinfo("選択してください", "一覧から録画を選んでください。")
            return
        self.run([step, *[str(path) for path in paths]])

    def _run_on_files(self, step: str) -> None:
        chosen = filedialog.askopenfilenames(
            title="対象の音声・動画ファイルを選んでください",
            filetypes=[
                ("音声・動画", "*.mp4 *.mkv *.mp3 *.m4a *.wav *.flac *.opus"),
                ("文字起こしデータ", "*.json"),
                ("すべてのファイル", "*.*"),
            ],
        )
        if not chosen:
            return
        self.run([step, *chosen])

    # --- 出力の取り込み -----------------------------------------------------

    def _drain_output(self) -> None:
        """キューに溜まった出力を画面へ流す。"""
        finished = False
        while True:
            try:
                line = self.runner.output.get_nowait()
            except queue.Empty:
                break
            if line is None:
                finished = True
                continue
            self._append(line, self._tag_for(line))

        if finished:
            code = self.runner.returncode
            self._append(f"--- 終了しました（終了コード {code}）---",
                         "done" if code == 0 else "error")
            self._set_running(False)
            self.refresh_archives()

        self.after(POLL_INTERVAL_MS, self._drain_output)

    @staticmethod
    def _tag_for(line: str) -> str | None:
        if line.startswith("$ "):
            return "command"
        if "WARNING" in line or line.lstrip().startswith("⚠"):
            return "warning"
        if "ERROR" in line or "エラー" in line or "Traceback" in line:
            return "error"
        return None

    def _append(self, line: str, tag: str | None = None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n", tag or ())
        # 古い行を捨てて、長時間の実行でも重くならないようにする
        excess = int(self.log.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if excess > 0:
            self.log.delete("1.0", f"{excess + 1}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _set_running(self, running: bool, name: str = "") -> None:
        for button in self._action_buttons:
            button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.status_var.set(
            f"実行中: {name}（停止ボタンで中断できます）" if running else "待機中"
        )

    def _on_stop(self) -> None:
        self._append("--- 停止を要求しました。後始末が終わるまでお待ちください ---",
                     "warning")
        self.status_var.set("停止しています…")
        self.runner.stop()

    def _on_close(self) -> None:
        if self.runner.is_running:
            if not messagebox.askyesno(
                "実行中です",
                "処理が実行中です。停止して終了しますか。\n"
                "録画中の場合、ここまでの内容は保存されます。",
            ):
                return
            self.runner.stop()
            # 書き出しの後始末を少し待ってから閉じる
            self.after(5000, self._force_close)
            return
        self.destroy()

    def _force_close(self) -> None:
        self.runner.kill()
        self.destroy()


def ensure_console() -> None:
    """コンソールが無ければ確保して、ウィンドウを隠す。

    停止ボタンは子プロセスへ CTRL_BREAK_EVENT を送るが、これは
    「呼び出し元がコンソールを持っている」ことが前提の仕組みで、
    pythonw.exe（コンソール無し）から起動すると送れない。
    送れないと強制終了に切り替わり、録画の保存やロックの解放が行われない。

    そこで見えないコンソールを用意しておく。子プロセスはこのコンソールを
    引き継ぐため、実行のたびに黒い窓が出ることもない。
    """
    kernel32 = ctypes.windll.kernel32
    if kernel32.GetConsoleWindow() != 0:
        return
    if kernel32.AllocConsole():
        ctypes.windll.user32.ShowWindow(kernel32.GetConsoleWindow(), SW_HIDE)


def _show_fatal(title: str, detail: str) -> None:
    """起動に失敗したことをダイアログで知らせる。

    pythonw.exe から起動された場合、標準エラーはどこにも表示されないため。
    """
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(title, detail)
    root.destroy()


def launch(config: Config) -> int:
    """GUI を起動する。"""
    ensure_console()
    App(config).mainloop()
    return 0


def main() -> int:
    """pythonw.exe から直接起動するためのエントリポイント（gui.bat が使う）。"""
    try:
        config = load_config()
    except (FileNotFoundError, ValueError) as exc:
        _show_fatal("設定を読み込めませんでした", str(exc))
        return 1

    try:
        return launch(config)
    except Exception:  # noqa: BLE001 - 画面が出ないまま落ちるのを避ける
        _show_fatal("予期しないエラーが発生しました", traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
