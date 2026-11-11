import json
from decimal import Decimal

import anthropic
import httpx2 as httpx
import pytest
from PIL import Image

from expenser.extract import ExtractionError, extract_transactions, prepare_image

PEOPLE = ["Alice", "Bob", "Carol"]


def _msg(payload, stop_reason="end_turn", **extra):
    body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 123, "output_tokens": 45},
    }
    body.update(extra)
    return body


def _client(response, status=200, captured=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["body"] = json.loads(request.content)
            captured["headers"] = dict(request.headers)
        return httpx.Response(status, json=response)

    return anthropic.Anthropic(
        api_key="test", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )


@pytest.fixture
def png(tmp_path):
    p = tmp_path / "shot.png"
    Image.new("RGB", (100, 200), "white").save(p)
    return p


PAYLOAD = {
    "transactions": [
        {"name": "Albert Heijn", "amount": -12.5, "date": "2026-03-04", "currency": "EUR",
         "participants": ["alice", "BOB", "Zed"], "paid_by": None, "notes": None},
        {"name": "Cafe", "amount": 8.0, "date": "bogus", "currency": "EUR",
         "participants": [], "paid_by": "Carol", "notes": "blurry"},
    ],
    "skipped": ["Salary: incoming"],
}


def test_extract_ok_and_request_body(png):
    cap = {}
    res = extract_transactions([png], PEOPLE, payer="Alice", hint="split the cafe", client=_client(_msg(PAYLOAD), captured=cap))
    t0, t1 = res.transactions
    assert t0.amount == Decimal("12.50") and t0.participants == ["Alice", "Bob"]
    assert t0.paid_by == "Alice" and str(t0.date) == "2026-03-04"
    assert t1.participants == PEOPLE and t1.paid_by == "Carol" and t1.date is None
    assert "Zed" in res.notes[0] and "blurry" in res.notes[1]
    assert res.skipped == ["Salary: incoming"] and res.currency == "EUR"
    assert (res.usage.input_tokens, res.usage.output_tokens) == (123, 45)

    body = cap["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["max_tokens"] == 16000
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "transactions" in body["output_config"]["format"]["schema"]["properties"]
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in cap["headers"]["anthropic-beta"]
    for k in ("temperature", "top_p", "top_k"):
        assert k not in body
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/png"
    assert content[-1]["type"] == "text" and "split the cafe" in content[-1]["text"]
    assert "Alice" in content[-1]["text"]


def test_model_env_override(png, monkeypatch):
    monkeypatch.setenv("EXPENSER_MODEL", "claude-sonnet-5-5")
    cap = {}
    extract_transactions([png], PEOPLE, client=_client(_msg(PAYLOAD), captured=cap))
    assert cap["body"]["model"] == "claude-sonnet-5-5"
    extract_transactions([png], PEOPLE, model="claude-opus-5-5", client=_client(_msg(PAYLOAD), captured=cap))
    assert cap["body"]["model"] == "claude-opus-5-5"


def test_refusal(png):
    c = _client(_msg({"transactions": []}, stop_reason="refusal", stop_details={"type": "refusal", "category": "x"}))
    with pytest.raises(ExtractionError, match="declined"):
        extract_transactions([png], PEOPLE, client=c)


def test_max_tokens(png):
    with pytest.raises(ExtractionError, match="max_tokens"):
        extract_transactions([png], PEOPLE, client=_client(_msg({"transactions": []}, stop_reason="max_tokens")))


def test_auth_error(png):
    err = {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}
    with pytest.raises(ExtractionError, match="ANTHROPIC_API_KEY"):
        extract_transactions([png], PEOPLE, client=_client(err, status=401))


def test_media_type_from_bytes_and_downscale(tmp_path):
    p = tmp_path / "actually_jpeg.png"
    Image.new("RGB", (50, 50), "red").save(p, format="JPEG")
    assert prepare_image(p)[0] == "image/jpeg"
    big = tmp_path / "big.png"
    Image.new("RGB", (7500, 300), "white").save(big)
    media, data = prepare_image(big)
    assert media == "image/jpeg"


def test_bad_image(tmp_path):
    p = tmp_path / "x.png"
    p.write_text("nope")
    with pytest.raises(ExtractionError):
        prepare_image(p)


def test_currency_required_in_schema_and_prompt(png):
    cap = {}
    extract_transactions([png], PEOPLE, client=_client(_msg(PAYLOAD), captured=cap))
    schema = cap["body"]["output_config"]["format"]["schema"]
    row = schema["$defs"]["ExtractedRow"]
    assert "currency" in row["required"] and row["properties"]["currency"]["type"] == "string"
    sysmsg = json.dumps(cap["body"]["system"])
    assert "ISO 4217" in sysmsg and "THB" in sysmsg and "notes" in sysmsg


def test_source_currency_override(png):
    cap = {}
    res = extract_transactions(
        [png], PEOPLE, source_currency="thb", client=_client(_msg(PAYLOAD), captured=cap)
    )
    assert "authoritative" in cap["body"]["messages"][0]["content"][-1]["text"]
    assert "THB" in cap["body"]["messages"][0]["content"][-1]["text"]
    assert {t.original_currency for t in res.transactions} == {"THB"} and res.currency == "THB"
    res = extract_transactions([png], PEOPLE, client=_client(_msg(PAYLOAD)))
    assert {t.original_currency for t in res.transactions} == {"EUR"}
