import json
import sqlite3

import pytest

from tools.sheet_selection_agent import _complete, main, select_from_pool


def test_selector_uses_only_open_candidates_and_returns_agent_reasons():
    rows = [
        {"candidate_id": "shopee:1:2", "state": "OPEN", "product_name": "Snack", "source_url": "https://shopee.vn/product/1/2"},
        {"candidate_id": "shopee:1:3", "state": "NEEDS_REVIEW", "product_name": "Other", "source_url": "https://shopee.vn/product/1/3"},
    ]
    calls = []
    def complete(messages):
        calls.append(messages)
        return {"selected": [{"candidate_id": "shopee:1:2", "selection_reason": "Clear PDP and visual action.",
                              "video_concept": "Show package", "script_draft": "", "claims_to_verify": ""}]}
    result = select_from_pool(rows, complete=complete)
    assert [x["candidate_id"] for x in result["selected"]] == ["shopee:1:2"]
    assert "shopee:1:3" not in json.dumps(calls)


def test_selector_rejects_candidate_not_open():
    rows = [{"candidate_id": "shopee:1:2", "state": "OPEN", "product_name": "Snack", "source_url": "https://shopee.vn/product/1/2"}]
    with pytest.raises(ValueError, match="OPEN"):
        select_from_pool(rows, complete=lambda _: {"selected": [{"candidate_id": "shopee:1:3", "selection_reason": "Invented"}]})


def test_existing_proposal_replays_without_another_model_call(tmp_path, monkeypatch):
    pool = tmp_path / "candidate_pool.csv"
    pool.write_text("candidate_id,state,product_name,source_url\nshopee:1:2,OPEN,Snack,https://shopee.vn/product/1/2\n", encoding="utf-8")
    proposal = tmp_path / "selection.json"
    proposal.write_text('{"selected":[{"candidate_id":"shopee:1:2","selection_reason":"Clear listing"}]}', encoding="utf-8")
    def unexpected_model_call(*args, **kwargs):
        raise AssertionError("Replay must not call 9Router")
    monkeypatch.setattr("tools.sheet_selection_agent.httpx.post", unexpected_model_call)
    assert main(["--pool", str(pool), "--output", str(proposal)]) == 0


def test_router_reads_active_local_key_without_copying_it_to_pi_env(tmp_path, monkeypatch):
    db = tmp_path / "db" / "data.sqlite"
    db.parent.mkdir()
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE apiKeys (key TEXT, isActive INTEGER, createdAt TEXT)")
        connection.execute("INSERT INTO apiKeys VALUES ('local-test-key', 1, '2026-11-02')")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NINEROUTER_BASE_URL", "http://127.0.0.1:20128/v1")
    monkeypatch.setenv("NINEROUTER_MODEL", "other-agent-combo")
    monkeypatch.setenv("PI_SELECTION_MODEL", "antigravity")
    monkeypatch.setenv("PI_SELECTION_TIMEOUT_SECONDS", "300")
    monkeypatch.delenv("NINEROUTER_API_KEY", raising=False)
    captured = {}
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"content": '```json\n{"selected":[]}\n```'}}]}
    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()
    monkeypatch.setattr("tools.sheet_selection_agent.httpx.post", fake_post)
    assert _complete([{"role": "user", "content": "[]"}]) == {"selected": []}
    assert captured["headers"]["Authorization"] == "Bearer local-test-key"
    assert captured["url"] == "http://127.0.0.1:20128/v1/chat/completions"
    assert captured["json"]["model"] == "antigravity"
    assert captured["timeout"] == 300.0


def test_candidate_limit_caps_model_input_after_filtering_open():
    rows = [{"candidate_id": f"shopee:1:{i}", "state": "OPEN"} for i in range(26)]
    rows.insert(0, {"candidate_id": "shopee:1:999", "state": "SELECTED"})
    def complete(messages):
        candidates = json.loads(messages[1]["content"])
        assert len(candidates) == 25
        assert candidates[0]["candidate_id"] == "shopee:1:0"
        assert candidates[-1]["candidate_id"] == "shopee:1:24"
        return {"selected": []}
    assert select_from_pool(rows, complete=complete, candidate_limit=25) == {"selected": []}
