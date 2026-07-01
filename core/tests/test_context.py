"""persona 常在・budget 切り詰め・順序(system→履歴古新→user)を検証。"""
from hisho_core.context import build_messages, PERSONA, approx_tokens


def test_persona_and_user_always_present():
    msgs = build_messages([], "質問です", num_ctx=8192, response_reserve=1024)
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == PERSONA
    assert msgs[-1] == {"role": "user", "content": "質問です"}


def test_order_oldest_to_newest():
    recent = [{"role": "user", "content": "A"}, {"role": "assistant", "content": "B"}]
    msgs = build_messages(recent, "C", num_ctx=8192, response_reserve=1024)
    assert [m["content"] for m in msgs] == [PERSONA, "A", "B", "C"]


def test_truncates_oldest_when_over_budget():
    # 小さい budget で古い履歴が落ち、system と user は残る
    big = "x" * 400
    recent = [{"role": "user", "content": big + "_old"},
              {"role": "assistant", "content": big + "_new"}]
    msgs = build_messages(recent, "now", num_ctx=300, response_reserve=100)  # budget=200 tokens
    contents = [m["content"] for m in msgs]
    assert contents[0] == PERSONA and contents[-1] == "now"
    assert (big + "_old") not in contents  # 最古が落ちる
