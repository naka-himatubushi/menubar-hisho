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
    "それ以外の実行手段 (バックアップ起動やリアルタイム確認など) はまだ持たないため、"
    "「確認しました」等の実行したかのような表現は使わず、根拠は与えられたメモの内容と収集時刻だけを述べます。"
    "メモに無いことは「わからない」と答えます。"
    "日本語で応答します。"
)


def approx_tokens(text: str) -> int:
    # 日本語混在の粗い近似。正確なトークナイザは持たないため保守的に3文字≒1token。
    return len(text) // 3 + 1


def build_messages(recent, user_message, num_ctx, response_reserve,
                   persona=PERSONA, memories=()):
    """num_ctx 予算内に履歴を切り詰め、memories があれば system prompt に注入する。"""
    system_text = persona
    if memories:
        notes = "\n".join(f"- {m}" for m in memories)
        system_text = (
            f"{persona}\n\n過去の関連メモ(参考。矛盾したら新しい発言を優先):\n{notes}"
        )
    budget = num_ctx - response_reserve
    system_msg = {"role": "system", "content": system_text}
    user_msg = {"role": "user", "content": user_message}
    used = approx_tokens(system_text) + approx_tokens(user_message)
    kept: list[dict] = []
    for m in reversed(recent):  # 新しい方から詰める
        t = approx_tokens(m["content"])
        if used + t > budget:
            break
        kept.append({"role": m["role"], "content": m["content"]})
        used += t
    kept.reverse()  # 古→新に戻す
    return [system_msg, *kept, user_msg]
