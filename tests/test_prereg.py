"""The pre-registration of plan 9.6 names the SHA-256 of eval/holdout_v3.jsonl; the file may not change after it was written (the holdout only measures).
The hash is over the file with line endings normalized to LF, so a Windows checkout with autocrlf gives the same value."""
import hashlib
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOLDOUT = REPO / "eval" / "holdout_v3.jsonl"
PREREG = REPO / "docs" / "PREREG_PLAN96.md"


def file_sha() -> str:
    return hashlib.sha256(HOLDOUT.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def test_holdout_v3_sha_matches_the_preregistration():
    found = re.findall(r"SHA-256[^`\n]*`([0-9a-f]{64})`", PREREG.read_text(encoding="utf-8"))
    assert len(found) == 1, "the pre-registration must state exactly one SHA-256"
    assert file_sha() == found[0], "eval/holdout_v3.jsonl changed after it was pre-registered"


def test_preregistration_fixes_hosts_and_seed():
    text = PREREG.read_text(encoding="utf-8")
    for needle in ("deepinfra", "bf16", "alibaba", "qwen/qwen3.8-27b", "allow_fallbacks=false", "20261123"):
        assert needle in text


def test_holdout_v3_schema_matches_the_dataset():
    cases = [json.loads(line) for line in HOLDOUT.read_text(encoding="utf-8").splitlines() if line.strip()]
    dataset = [json.loads(line) for line in (REPO / "eval" / "dataset.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    allowed = set().union(*(set(c) for c in dataset))
    required = {"id", "user_id", "message", "expected_agents", "optional_agents", "expected_tools", "expect_handoff", "criteria"}
    assert [c["id"] for c in cases] == [f"hv3_{i:02d}" for i in range(1, 21)]
    customers = json.loads((REPO / "app" / "data" / "customers.json").read_text(encoding="utf-8"))
    for c in cases:
        assert required <= set(c) <= allowed and c["holdout"] is True
        assert c["user_id"] in customers
        assert all(m["role"] in ("user", "assistant") for m in c.get("history", []))
