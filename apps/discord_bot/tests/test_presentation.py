"""Checks for disclosure-safe, bounded notification and plan rendering."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from opsyne_discord.presentation import CaseView, PlanView, render_case, render_plan


@pytest.mark.parametrize("state", ["APPROVED", "EXECUTED", "REJECTED", "UNKNOWN"])
def test_non_draft_plan_is_read_only(state: str) -> None:
    payload = render_plan(replace(_plan(), state=state, approver="reviewer"))
    assert not payload.get("components")
    fields = {field["name"]: field["value"] for field in payload["embeds"][0]["fields"]}
    assert fields["現在の計画状態"] == state
    assert fields["承認者"] == "reviewer"


def _plan() -> PlanView:
    return PlanView(
        plan_id="P-87",
        version=3,
        digest="sha256:" + "a" * 64,
        target_id="deployment-abc",
        target_version="generation-10",
        operation="検証環境の直前版へロールバック",
        impact="新機能が一時的に利用できなくなる",
        preconditions=("DBの互換性を確認済み",),
        success_conditions=("試験取引が成功しエラー率が基準内",),
        abort_conditions=("対象の版または互換性を確認できない",),
        expires_at="2026-09-15T14:45:00+09:00",
        approval_request_id="req-87-v3",
    )


def _case(state: str = "UNKNOWN") -> CaseView:
    return CaseView(
        case_id="INC-204",
        title="決済APIのエラー率上昇",
        state=state,
        summary="直近5分のエラー率12%",
        observed_at="2026-09-15T14:32:00+09:00",
        evidence_refs=("evidence-1",),
    )


def _assert_limits(payload: dict[str, Any]) -> None:
    def size(value: str) -> int:
        return len(value.encode("utf-16-le")) // 2

    assert payload["allowed_mentions"] == {"parse": []}
    assert len(payload["embeds"]) <= 10
    total = 0
    for embed in payload["embeds"]:
        assert size(embed["title"]) <= 256
        assert size(embed["description"]) <= 4096
        total += size(embed["title"]) + size(embed["description"])
        total += size(embed["footer"]["text"])
        assert len(embed["fields"]) <= 25
        for field in embed["fields"]:
            assert 0 < size(field["name"]) <= 256
            assert 0 < size(field["value"]) <= 1024
            total += size(field["name"]) + size(field["value"])
    assert total <= 6000
    for row in payload["components"]:
        assert len(row["components"]) <= 5
        for button in row["components"]:
            assert 1 <= size(button["custom_id"]) <= 100
            assert size(button["label"]) <= 80
            assert "url" not in button


@pytest.mark.parametrize(
    "state", ["UNKNOWN", "EXECUTION_UNKNOWN", "VERIFICATION_UNKNOWN", "unrecognized", "", "SUCCESS"]
)
def test_unknown_state_never_implies_resolved(state: str) -> None:
    payload = render_case(_case(state))
    _assert_limits(payload)
    state_text = payload["embeds"][0]["fields"][1]["value"]
    assert "復旧確認済み" not in state_text
    assert "不明" in state_text or "未確認" in state_text
    assert payload["embeds"][0]["color"] != 0x2E7D32


def test_operation_success_remains_distinct_from_recovery() -> None:
    executed = render_case(_case("EXECUTED"))
    resolved = render_case(_case("RESOLVED"))
    assert "復旧確認待ち" in executed["embeds"][0]["fields"][1]["value"]
    assert "復旧確認済み" in resolved["embeds"][0]["fields"][1]["value"]


def test_untrusted_text_does_not_create_links_mentions_or_formatting() -> None:
    hostile = "@everyone <@123> <@&456> [open](https://evil.example/) ``` **ok** \u202e"
    payload = render_case(
        replace(_case(), title=hostile, summary=hostile, evidence_refs=(hostile,))
    )
    _assert_limits(payload)
    text = json.dumps(payload, ensure_ascii=False)
    assert "@everyone" not in text
    assert "https://" not in text
    assert "evil.example" not in text
    assert "```" not in text
    assert "\u202e" not in text
    assert "\\u202e" in text
    assert payload["components"] == []


def test_huge_unicode_case_and_evidence_stay_within_all_limits() -> None:
    huge = "😀\\`@https://example.com\n" * 1000
    payload = render_case(CaseView(huge, huge, huge, huge, huge, evidence_refs=(huge,) * 30))
    _assert_limits(payload)
    assert payload["embeds"][0]["description"].endswith("…")


@pytest.mark.parametrize("missing", ["", " \n "])
def test_empty_case_data_is_marked_missing(missing: str) -> None:
    payload = render_case(CaseView(missing, missing, missing, missing, missing, (missing,)))
    _assert_limits(payload)
    assert "欠落" in json.dumps(payload, ensure_ascii=False)


def test_complete_plan_displays_all_terms_and_only_starts_review() -> None:
    plan = _plan()
    payload = render_plan(plan)
    _assert_limits(payload)
    fields = {field["name"]: field["value"] for field in payload["embeds"][0]["fields"]}
    assert fields["計画ID"] == plan.plan_id
    assert fields["表示形式の版"] == "3"
    assert fields["計画のdigest"] == plan.digest
    assert fields["対象実体"] == plan.target_id
    assert fields["対象の版"] == plan.target_version
    assert fields["事前条件"] == plan.preconditions[0]
    assert fields["成功条件"] == plan.success_conditions[0]
    assert fields["中止条件"] == plan.abort_conditions[0]
    assert fields["承認期限"] == plan.expires_at
    ids = [button["custom_id"] for button in payload["components"][0]["components"]]
    assert ids == ["review:req-87-v3", "reject:req-87-v3"]
    assert not any(item.startswith("approve:") for item in ids)


@pytest.mark.parametrize(
    "plan",
    [
        replace(_plan(), operation=" "),
        replace(_plan(), digest=""),
        replace(_plan(), target_version=""),
        replace(_plan(), version=0),
        replace(_plan(), preconditions=()),
        replace(_plan(), success_conditions=("",)),
        replace(_plan(), abort_conditions=()),
        replace(_plan(), expires_at="sometime later"),
        replace(_plan(), expires_at="2026-09-15T14:45:00"),
        replace(_plan(), approval_request_id="../approve:anything"),
        replace(_plan(), approval_request_id="x" * 81),
        replace(_plan(), operation="approved\u202eabc"),
        replace(_plan(), impact="x" * 1025),
        replace(_plan(), operation="😀" * 513),
        replace(_plan(), impact="*" * 513),
        replace(
            _plan(),
            plan_id="a" * 1000,
            digest="b" * 1000,
            target_id="c" * 1000,
            target_version="d" * 1000,
            operation="e" * 1000,
            impact="f" * 1000,
        ),
    ],
)
def test_incomplete_or_oversized_plan_has_no_action_buttons(plan: PlanView) -> None:
    payload = render_plan(plan)
    _assert_limits(payload)
    assert payload["components"] == []
    assert "全文" in payload["embeds"][0]["description"]


def test_stale_saved_plan_is_labelled_snapshot_without_asserting_validity() -> None:
    payload = render_plan(replace(_plan(), expires_at="2000-01-01T00:00:00+00:00"))
    _assert_limits(payload)
    embed = payload["embeds"][0]
    assert "保存済み" in embed["description"]
    assert "期限" in embed["footer"]["text"]
    assert "再確認" in embed["footer"]["text"]
    assert "有効" not in json.dumps(payload, ensure_ascii=False)
    assert all(
        not button["custom_id"].startswith("approve:")
        for button in payload["components"][0]["components"]
    )


def test_maximum_supported_request_id_fits_discord_buttons() -> None:
    payload = render_plan(replace(_plan(), approval_request_id="r" * 80))
    _assert_limits(payload)
    assert len(payload["components"][0]["components"][0]["custom_id"]) <= 100
