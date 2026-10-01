import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_usage


def _reply(msg_id, ts, model="claude-opus-5-5", cwd="/work/proj", **usage):
    return json.dumps({
        "type": "assistant",
        "timestamp": ts,
        "requestId": f"req_{msg_id}",
        "cwd": cwd,
        "message": {"id": msg_id, "model": model, "usage": usage},
    })


def _write_log(tmp_path, lines):
    project = tmp_path / "-work-proj"
    project.mkdir()
    (project / "session.jsonl").write_text("\n".join(lines) + "\n")
    return [tmp_path]


def test_cost_of_prices_all_token_types():
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 2_000_000,
        "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000},
    }
    # 4 input + 20 output + 0.20 read + 5 (5m write) + 8 (1h write)
    assert round(ai_usage.cost_of("claude-opus-5-5", usage), 6) == 37.20


def test_price_lookup_uses_longest_prefix():
    assert ai_usage.price_for("claude-opus-5-5")[0] == 4.00
    assert ai_usage.price_for("claude-opus-5")[0] == 5.00
    assert ai_usage.price_for("claude-opus-4-20250514")[0] == 15.00
    assert ai_usage.price_for("gpt-4o") is None


def test_summarize_dedupes_and_buckets(tmp_path):
    dirs = _write_log(tmp_path, [
        _reply("a", "2026-10-01T12:00:00Z", output_tokens=1_000_000),
        _reply("a", "2026-10-01T12:00:00Z", output_tokens=1_000_000),  # duplicate log line
        _reply("b", "2026-09-15T12:00:00Z", model="claude-haiku-4-5", input_tokens=1_000_000),
        _reply("c", "2026-10-01T13:00:00Z", model="<synthetic>", output_tokens=5),
        json.dumps({"type": "user", "message": {"content": "hi"}}),
        "not json",
    ])
    s = ai_usage.summarize(log_dirs=dirs, days=30, today=date(2026, 10, 1), budget=40)

    assert s["all_time"]["replies"] == 2
    assert round(s["all_time"]["cost"], 6) == 21.00
    assert s["month"]["cost"] == 20.00
    assert s["budget_used_pct"] == 50.0
    assert [m["name"] for m in s["by_model"]] == ["claude-opus-5-5", "claude-haiku-4-5"]
    assert s["by_project"][0]["name"] == "/work/proj"
    assert len(s["daily"]) == 30 and s["daily"][-1]["cost"] == 20.00


def test_unknown_model_is_reported_not_priced(tmp_path):
    dirs = _write_log(tmp_path, [_reply("a", "2026-10-01T12:00:00Z", model="mystery-1", output_tokens=10)])
    s = ai_usage.summarize(log_dirs=dirs, today=date(2026, 10, 1), budget=None)
    assert s["unpriced_models"] == ["mystery-1"]
    assert s["all_time"]["cost"] == 0


def test_missing_log_dir_is_empty(tmp_path):
    s = ai_usage.summarize(log_dirs=[tmp_path / "nope"], budget=None)
    assert s["all_time"]["replies"] == 0
