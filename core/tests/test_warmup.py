"""warm-up/unload: ollama を実際に叩かず、注入 fake で request 形状と待機ロジックを検証。"""
import asyncio
import pytest
from hisho_core import llm
from hisho_core.server import warmup_when_ready


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


class _FakeClient:
    """httpx.AsyncClient の post/aclose だけ真似て、送られた body を記録する。"""
    def __init__(self):
        self.calls = []

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        return _FakeResponse(200)

    async def aclose(self):
        pass


async def test_warmup_posts_single_token_generation():
    fake = _FakeClient()
    ok = await llm.warmup(model="m1", ollama_host="http://127.0.0.1:11434",
                          num_ctx=8192, keep_alive="30m", client_factory=lambda: fake)
    assert ok is True
    call = fake.calls[0]
    assert call["url"].endswith("/api/chat")
    body = call["json"]
    assert body["model"] == "m1"
    assert body["stream"] is False
    assert body["think"] is False
    assert body["keep_alive"] == "30m"
    assert body["options"] == {"num_ctx": 8192, "num_predict": 1}


async def test_unload_posts_keep_alive_zero():
    fake = _FakeClient()
    ok = await llm.unload(model="m1", ollama_host="http://127.0.0.1:11434",
                          client_factory=lambda: fake)
    assert ok is True
    call = fake.calls[0]
    assert call["url"].endswith("/api/generate")
    assert call["json"] == {"model": "m1", "keep_alive": 0}


async def test_warmup_returns_false_on_connect_error():
    class _Boom:
        async def post(self, url, json=None):
            import httpx
            raise httpx.ConnectError("down")
        async def aclose(self):
            pass
    ok = await llm.warmup(model="m1", ollama_host="http://127.0.0.1:1",
                          num_ctx=8192, keep_alive="30m", client_factory=lambda: _Boom())
    assert ok is False


async def test_warmup_when_ready_waits_for_reachable_then_fires_once():
    probes = [{"reachable": False}, {"reachable": False}, {"reachable": True}]
    fired = []
    sleeps = []

    async def probe():
        return probes.pop(0)

    async def warmup():
        fired.append(1)
        return True

    async def fake_sleep(sec):
        sleeps.append(sec)

    ok = await warmup_when_ready(probe, warmup, attempts=10, interval=0.5, sleep=fake_sleep)
    assert ok is True
    assert fired == [1]          # 一度だけ
    assert sleeps == [0.5, 0.5]  # reachable まで 2 回待った


async def test_warmup_when_ready_retries_failed_warmup():
    """warm-up 自体の失敗(ollama 高負荷等)もリトライする。"""
    results = [False, True]
    fired = []

    async def probe():
        return {"reachable": True}

    async def warmup():
        fired.append(1)
        return results.pop(0)

    async def fake_sleep(sec):
        pass

    ok = await warmup_when_ready(probe, warmup, attempts=5, interval=0.1, sleep=fake_sleep)
    assert ok is True
    assert fired == [1, 1]


async def test_warmup_when_ready_gives_up_after_attempts():
    async def probe():
        return {"reachable": False}

    async def warmup():
        raise AssertionError("must not fire")

    async def fake_sleep(sec):
        pass

    ok = await warmup_when_ready(probe, warmup, attempts=3, interval=0.1, sleep=fake_sleep)
    assert ok is False
