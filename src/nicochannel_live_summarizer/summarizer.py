"""summarizer: 文字起こしを Claude API で日本語要約する。

APIキーが未設定の場合は呼び出し側でスキップされ、文字起こしのみが保存される。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
あなたは配信アーカイブの整理を専門とするアシスタントです。
配信者の雑談配信の文字起こしを読み、後から見返すための日本語の要約を作成します。

文字起こしは音声認識によるもので、次の特徴があります:
- 固有名詞や専門用語が誤変換されていることがある。文脈から推測できる場合は自然な表記に直してよい
- 相槌・言い直し・言い淀みが多く含まれる。要約では取り除く
- 視聴者コメントへの反応が含まれるが、コメント本文は文字起こしに現れない。
  発言から内容を推測できる場合のみ触れ、推測であることが分かる書き方にする

要約の方針:
- 事実に忠実に。文字起こしに現れない内容を創作しない
- 話題ごとにまとめ、その話題が始まるタイムスタンプを必ず添える
- 後から「あの話どこだっけ」と探せることを最優先にする
- 情報量の薄い雑談も、何を話していたかは1行で残す（丸ごと省略しない）
"""

USER_PROMPT_TEMPLATE = """\
以下は「{title}」という配信の、タイムスタンプ付き文字起こし全文です。
配信日時: {date}
配信の長さ: {duration}

これを読んで、次の構成の Markdown で要約してください。見出しの文言は変えないでください。

## 3行まとめ
配信全体を3行で。どんな雰囲気の回だったかが分かるように。

## 話題ごとの内容
話題の区切りごとに `### [HH:MM:SS] 話題名` の見出しを立て、
その下に2〜5行程度で内容を書く。話題は時系列順に並べる。

## 重要な情報・告知
今後の予定、企画の告知、視聴者への連絡など、後から参照する価値のある情報を箇条書きに。
タイムスタンプを添える。該当がなければ「特になし」と書く。

## 印象的だった場面
盛り上がった場面や記憶に残る発言を3〜5個、タイムスタンプ付きの箇条書きで。

## キーワード
この配信を検索するときに手がかりになる語を10個程度、カンマ区切りで。

---

{transcript}
"""


# モデルごとの単価（USD / 100万トークン）。入力, 出力の順。
# 公開されている標準料金で、値引き前の目安として使う。
MODEL_PRICES = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# 表にないモデルを指定されたときに使う単価（最上位モデル相当で高めに見積もる）
DEFAULT_PRICE = (10.0, 50.0)


class SummarizerError(RuntimeError):
    """要約に関する回復不能なエラー。"""


@dataclass
class SummaryResult:
    """要約の結果。"""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    # max_tokens に達して途中で打ち切られたか
    truncated: bool = False

    @property
    def estimated_cost_usd(self) -> float:
        """概算コスト（USD）。実際に応答したモデルの公開単価で計算する。

        表にないモデルでは高めの単価を使う。実際より安く見せて
        想定外の請求につながるより、多めに見積もる方が安全なため。
        """
        input_price, output_price = MODEL_PRICES.get(self.model, DEFAULT_PRICE)
        return (
            self.input_tokens / 1_000_000 * input_price
            + self.output_tokens / 1_000_000 * output_price
        )


class Summarizer:
    """Claude API による要約クライアント。"""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-opus-5",
        max_tokens: int = 8000,
        effort: str = "high",
    ) -> None:
        if not api_key:
            raise SummarizerError("ANTHROPIC_API_KEY が設定されていません。")
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort

        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key)

    def summarize(
        self, title: str, date: str, duration: str, transcript: str
    ) -> SummaryResult:
        """文字起こしから要約を生成する。"""
        if not transcript.strip():
            raise SummarizerError("文字起こしが空のため要約できません。")

        user_prompt = USER_PROMPT_TEMPLATE.format(
            title=title, date=date, duration=duration, transcript=transcript
        )

        message = self._create_message(user_prompt)
        stop_reason = getattr(message, "stop_reason", None)

        if stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise SummarizerError(
                "モデルが応答を拒否しました"
                + (f"（分類: {category}）" if category else "")
                + "。文字起こしは保存済みです。"
            )

        text = "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        ).strip()
        if not text:
            raise SummarizerError("要約が空でした。")

        # 出力上限に達した場合、末尾の見出しが丸ごと欠けたまま返る。
        # そのまま保存すると欠落に気づけないため、結果に印を付けて呼び出し側へ伝える。
        truncated = stop_reason == "max_tokens"
        if truncated:
            logger.warning(
                "要約が出力上限（max_tokens=%d）に達したため途中で終了しました。"
                "config.toml の [summarizer] max_tokens を増やして "
                "`summarize --force` でやり直せます。",
                self.max_tokens,
            )

        usage = message.usage
        return SummaryResult(
            text=text,
            model=getattr(message, "model", self.model),
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            truncated=truncated,
        )

    def _create_message(self, user_prompt: str):
        """要約リクエストを送る。

        長い文字起こしを扱うためストリーミングで送信する。
        Claude Opus 5 の安全分類器が拒否した場合に備え、サーバ側フォールバックを
        既定で有効にする。SDK やアカウントが未対応の場合は通常呼び出しに落とす。
        """
        common = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_prompt}],
            "output_config": {"effort": self.effort},
        }

        try:
            with self._client.beta.messages.stream(
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                **common,
            ) as stream:
                return stream.get_final_message()
        except (TypeError, self._anthropic.BadRequestError) as exc:
            logger.info(
                "サーバ側フォールバックを利用できないため、通常のリクエストで再試行します: %s",
                exc,
            )
        except self._anthropic.APIStatusError as exc:
            raise SummarizerError(f"Claude API がエラーを返しました: {exc}") from exc
        except self._anthropic.APIConnectionError as exc:
            raise SummarizerError(f"Claude API に接続できませんでした: {exc}") from exc

        try:
            with self._client.messages.stream(**common) as stream:
                return stream.get_final_message()
        except self._anthropic.APIStatusError as exc:
            raise SummarizerError(f"Claude API がエラーを返しました: {exc}") from exc
        except self._anthropic.APIConnectionError as exc:
            raise SummarizerError(f"Claude API に接続できませんでした: {exc}") from exc


def write_summary_file(
    result: SummaryResult, path: Path, title: str, date: str
) -> None:
    """要約結果を Markdown ファイルとして保存する。"""
    lines = [
        f"# 要約: {title}",
        "",
        f"- 配信日時: {date}",
        f"- 生成モデル: `{result.model}`",
        f"- 消費トークン: 入力 {result.input_tokens:,} / 出力 {result.output_tokens:,}"
        f"（概算 ${result.estimated_cost_usd:.3f}）",
    ]
    if result.truncated:
        lines.append(
            "- ⚠ **出力上限に達したため、この要約は途中で終わっています。**"
            "`config.toml` の `[summarizer] max_tokens` を増やし、"
            "`summarize --force` でやり直してください。"
        )
    lines += ["", "---", "", result.text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
