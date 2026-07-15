from app import llm


class _Response:
    def __init__(self, body: str):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self) -> bytes:
        return self.body.encode("utf-8")


def test_call_llm_structured_sends_user_agent(monkeypatch):
    captured = {}
    response_body = (
        '{"choices":[{"message":{"role":"assistant",'
        '"content":"{\\"answer\\":\\"ok\\"}"}}]}'
    )

    def urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(response_body)

    monkeypatch.setattr(
        llm.urllib.request,
        "urlopen",
        urlopen,
    )

    result = llm.call_llm_structured(
        "the complete prompt",
        {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        base_url="https://example.test/v1",
        model="test-model",
        api_key="secret-api-key",
    )

    assert result == '{"answer":"ok"}'
    assert captured["request"].get_header("User-agent") == "ripe-clips/0.1.0"
