"""persona 常在・budget 切り詰め・順序(system→履歴古新→user)・レポート注入を検証。"""
from hisho_core.context import (build_messages, PERSONA, SENSOR_NOTE, ACTION_NOTE,
                                approx_tokens)


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


def test_sensor_report_injected_as_second_system_message():
    """sensor_report は SENSOR_NOTE 前置の追加 system として persona 直後に入る。"""
    msgs = build_messages([], "調子どう?", num_ctx=8192, response_reserve=1024,
                          sensor_report="14:05 実測\n\n【A】\nOK")
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == PERSONA
    assert msgs[1]["role"] == "system"
    assert msgs[1]["content"].startswith(SENSOR_NOTE)
    assert "14:05 実測" in msgs[1]["content"]
    assert msgs[-1] == {"role": "user", "content": "調子どう?"}


def test_no_sensor_report_means_single_system_message():
    msgs = build_messages([], "hi", num_ctx=8192, response_reserve=1024)
    assert sum(1 for m in msgs if m["role"] == "system") == 1


def test_action_report_injected_as_system_message():
    """action_report は ACTION_NOTE 前置の追加 system として注入される。"""
    msgs = build_messages([], "はい", num_ctx=8192, response_reserve=1024,
                          action_report="14:05 実行\n実行内容: tmutil startbackup\n結果:\nok")
    assert msgs[0]["content"] == PERSONA
    assert msgs[1]["role"] == "system"
    assert msgs[1]["content"].startswith(ACTION_NOTE)
    assert "tmutil startbackup" in msgs[1]["content"]
    assert msgs[-1] == {"role": "user", "content": "はい"}


def test_sensor_report_counts_against_history_budget():
    """実測レポートの分は履歴予算から差し引かれる (溢れたら古い履歴から落ちる)。
    レポート無しなら両方入る予算で、レポート有りだと最古だけ落ちることを見る。
    (persona 文言を変えたら予算の再調整が要る: persona+user+2項目 ≤ budget <
     persona+user+レポート+2項目 かつ persona+user+レポート+1項目 ≤ budget)"""
    big = "x" * 150  # ≒52 tokens/項目
    recent = [{"role": "user", "content": big + "_old"},
              {"role": "assistant", "content": big + "_new"}]
    p = approx_tokens(PERSONA)
    item = approx_tokens(big + "_old")
    report_tokens = approx_tokens(SENSOR_NOTE + "y" * 150)
    budget = p + approx_tokens("now") + report_tokens + item + 5  # 1項目は入るが2項目は溢れる
    num_ctx, reserve = budget + 100, 100
    assert p + approx_tokens("now") + 2 * item <= budget  # 前提: レポート無しなら両方入る

    without = build_messages(recent, "now", num_ctx=num_ctx, response_reserve=reserve)
    contents = [m["content"] for m in without]
    assert (big + "_old") in contents and (big + "_new") in contents

    withreport = build_messages(recent, "now", num_ctx=num_ctx, response_reserve=reserve,
                                sensor_report="y" * 150)
    contents = [m["content"] for m in withreport]
    assert (big + "_old") not in contents   # レポート分で予算が減り最古が落ちる
    assert (big + "_new") in contents       # 新しい方は残る
    assert any(c.startswith(SENSOR_NOTE) for c in contents)  # レポート自体は必ず残る
