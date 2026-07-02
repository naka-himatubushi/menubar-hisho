"""build_messages の memories 注入: system prompt への合成と budget 切り詰めを検証。"""
from hisho_core.context import build_messages, PERSONA


def test_memories_go_into_system_prompt():
    msgs = build_messages([], "今日の夕飯は?", 8192, 1024,
                          memories=["私の好物はカレーライスです"])
    assert msgs[0]["role"] == "system"
    assert PERSONA in msgs[0]["content"]
    assert "過去の関連メモ" in msgs[0]["content"]
    assert "カレーライス" in msgs[0]["content"]
    assert msgs[-1] == {"role": "user", "content": "今日の夕飯は?"}


def test_no_memories_keeps_persona_unchanged():
    msgs = build_messages([], "こんにちは", 8192, 1024, memories=[])
    assert msgs[0]["content"] == PERSONA


def test_memories_count_against_budget():
    # 巨大メモでも履歴が budget からはみ出ないこと(落ちない・全メッセージが収まる)
    big = "居" * 3000
    recent = [{"role": "user", "content": "古い発話" * 50}] * 10
    msgs = build_messages(recent, "質問", 8192, 1024, memories=[big])
    total = sum(len(m["content"]) // 3 + 1 for m in msgs)
    assert total <= 8192 - 1024
