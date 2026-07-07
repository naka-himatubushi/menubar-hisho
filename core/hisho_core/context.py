"""秘書 persona と、num_ctx 予算内に履歴を切り詰めるメッセージ合成(純粋関数)。"""
from __future__ import annotations

PERSONA = (
    "あなたはユーザー専属の秘書アシスタント「JARVIS」です。"
    "口調は簡潔で丁寧。結論を先に述べ、根拠や補足は後に短く添えます。"
    "不確実なことは断定せず、推測する時は「推測ですが」と明示します。"
    "専門用語を使う時は一言の補足を添えます。長い説明より要点の箇条書きを好みます。"
    "出力は読みやすい平文にします。Markdown の装飾記号 (**太字**、`バッククォート`、# 見出し、* リスト) は使いません。"
    "箇条書きは「・」を使い、コマンドやパスは記号で囲まず改行して単独の行に書きます。"
    "「過去の関連メモ」が与えられた場合はユーザーに関する既知の事実として自然に活用します。"
    "記憶の忘却は forget 機能で実際に実行でき、実行後は結果 (件数) を事実として報告します。"
    "バックアップ状況・マシンの稼働・ディスク容量などの質問には、システムが実測した"
    "データが文脈で与えられます。状態を答える時は実測した時刻 (「HH:MM 実測」等) を必ず添え、"
    "実測結果に無いことを推測で補って答えません。実測できなかった項目は正直にそう伝えます。"
    "バックアップ開始と作業タスク投入は、実行内容を提示してユーザーが「はい」と確認した時"
    "だけシステムが実行し、その結果が文脈で与えられます。与えられた結果に無いことを"
    "実行済みかのように語りません。それ以外の実行手段はまだ持たないため、"
    "「確認しました」等の実行したかのような表現は使わず、根拠は与えられたメモの内容と収集時刻だけを述べます。"
    "メモに無いことは「わからない」と答えます。"
    "日本語で応答します。"
)

# センサー実測レポートを文脈注入する時の前置き。サーバが応答生成の前に実測を済ませ、
# モデルには「要約する係」だけをさせる (実LLMスモークで、tool-calling 方式だと
# モデルの語りが実測より先に生成され古い記憶で汚染される欠陥を確認したため)。
SENSOR_NOTE = (
    "以下はたった今この瞬間の実測データ (サーバが計測済み)。"
    "これだけに基づき平文で要約し、実測時刻を必ず添えること:\n"
)

# アクション実行結果を文脈注入する時の前置き。実行は確認後にサーバが済ませており、
# モデルには「結果を報告する係」だけをさせる (sensors と同じ決定的サーバ主導の思想)。
ACTION_NOTE = (
    "以下はユーザー確認済みでたった今実行した操作の結果 (サーバが実行済み)。"
    "これだけに基づき平文で結果を報告し、実行時刻を必ず添えること。"
    "失敗していたら失敗と正直に伝えること:\n"
)


def approx_tokens(text: str) -> int:
    # 日本語混在の粗い近似。正確なトークナイザは持たないため保守的に3文字≒1token。
    return len(text) // 3 + 1


def build_messages(recent, user_message, num_ctx, response_reserve,
                   persona=PERSONA, memories=(), sensor_report=None,
                   action_report=None):
    """num_ctx 予算内に履歴を切り詰めてメッセージ列を組む。
    memories があれば system prompt に注入し、sensor_report / action_report が
    あればそれぞれ NOTE を前置した追加 system メッセージとして persona の直後に置く。"""
    system_text = persona
    if memories:
        notes = "\n".join(f"- {m}" for m in memories)
        system_text = (
            f"{persona}\n\n過去の関連メモ(参考。矛盾したら新しい発言を優先):\n{notes}"
        )
    extra: list[dict] = []
    if sensor_report:
        extra.append({"role": "system", "content": f"{SENSOR_NOTE}{sensor_report}"})
    if action_report:
        extra.append({"role": "system", "content": f"{ACTION_NOTE}{action_report}"})
    budget = num_ctx - response_reserve
    system_msg = {"role": "system", "content": system_text}
    user_msg = {"role": "user", "content": user_message}
    used = approx_tokens(system_text) + approx_tokens(user_message)
    used += sum(approx_tokens(m["content"]) for m in extra)
    kept: list[dict] = []
    for m in reversed(recent):  # 新しい方から詰める
        t = approx_tokens(m["content"])
        if used + t > budget:
            break
        kept.append({"role": m["role"], "content": m["content"]})
        used += t
    kept.reverse()  # 古→新に戻す
    return [system_msg, *extra, *kept, user_msg]
