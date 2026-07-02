"""秘書 persona と、num_ctx 予算内に履歴を切り詰めるメッセージ合成(純粋関数)。"""
from __future__ import annotations

PERSONA = (
    "あなたはユーザー専属の秘書アシスタント「Hisho」です。"
    "簡潔で丁寧、要点先出し。分からないことは推測せず確認します。"
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
