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


def build_messages(recent, user_message, num_ctx, response_reserve, persona=PERSONA):
    budget = num_ctx - response_reserve
    system_msg = {"role": "system", "content": persona}
    user_msg = {"role": "user", "content": user_message}
    used = approx_tokens(persona) + approx_tokens(user_message)
    kept: list[dict] = []
    for m in reversed(recent):  # 新しい方から詰める
        t = approx_tokens(m["content"])
        if used + t > budget:
            break
        kept.append({"role": m["role"], "content": m["content"]})
        used += t
    kept.reverse()  # 古→新に戻す
    return [system_msg, *kept, user_msg]
