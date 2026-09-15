"""Approval binds human intent and agent evidence to the reviewed draft."""

from pathlib import Path
from typing import Any

import pytest

from opsyne.contracts.adapter_reviews import HumanExplanation
from opsyne.contracts.core import Actor, digest
from opsyne.control.repository import Conflict, Denied
from opsyne.runtime import Runtime

OWNER = Actor(actor="owner", role="admin")
REVIEWER = Actor(actor="reviewer", role="approver")


def setup(path: Path) -> tuple[Runtime, dict[str, Any]]:
    runtime = Runtime(path)
    runtime.seed_demo(OWNER)
    return runtime, runtime.control.get("adapter", "demo-json-v1")


def test_explanation_revision_and_approval(tmp_path: Path) -> None:
    runtime, draft = setup(tmp_path)
    explanation = HumanExplanation(
        monitoring_purpose="決済障害の検知",
        expected_insights=["処理の失敗"],
        rationale=["運用担当の監視要件"],
        questions=["重大度の意味は要確認"],
    )
    updated = runtime.adapters.update_explanation(draft["id"], draft["digest"], explanation, OWNER)
    assert updated["digest"] != draft["digest"]
    assert updated["explanation_revision"] == 2
    assert updated["explanation"]["recorded_by"] == "owner"
    assert runtime.control.get("adapter_review", f"{draft['id']}:1") == draft
    with pytest.raises(Conflict):
        runtime.adapters.approve(draft["id"], draft["digest"], REVIEWER)
    with pytest.raises(Conflict):
        runtime.adapters.update_explanation(draft["id"], draft["digest"], explanation, OWNER)
    with pytest.raises(Denied):
        runtime.adapters.approve(draft["id"], updated["digest"], OWNER)
    updated = runtime.adapters.update_explanation(
        draft["id"], updated["digest"], explanation, Actor(actor="operator", role="operator")
    )
    # Editing again cannot erase previous contributors and enable self approval.
    with pytest.raises(Denied):
        runtime.adapters.approve(draft["id"], updated["digest"], OWNER)
    reopened = Runtime(tmp_path)
    assert reopened.control.get("adapter", draft["id"]) == updated
    approved = reopened.adapters.approve(draft["id"], updated["digest"], REVIEWER)
    assert approved["explanation"]["human"] == explanation.model_dump(mode="json")
    with pytest.raises(Conflict):
        reopened.adapters.update_explanation(draft["id"], updated["digest"], explanation, OWNER)


@pytest.mark.parametrize("upgrade", [False, True])
def test_legacy_draft_approval_and_upgrade(tmp_path: Path, upgrade: bool) -> None:
    runtime, draft = setup(tmp_path)
    for key in (
        "review_schema_version",
        "explanation_revision",
        "explanation",
        "explanation_editors",
    ):
        draft.pop(key)
    draft["digest"] = digest(runtime.adapters.definition(draft).model_dump(mode="json"))
    with runtime.control.db.connection() as db:
        runtime.control._put(db, "adapter", draft["id"], draft)
    if upgrade:
        draft = runtime.adapters.update_explanation(
            draft["id"], draft["digest"], HumanExplanation(), OWNER
        )
        assert draft["explanation"]["human"]["monitoring_purpose"] is None
        assert draft["explanation"]["agent"] is None
    assert runtime.adapters.approve(draft["id"], draft["digest"], REVIEWER)["status"] == "APPROVED"


def test_modified_explanation_is_not_approved(tmp_path: Path) -> None:
    runtime, draft = setup(tmp_path)
    draft["explanation"]["human"] = {"monitoring_purpose": "改変された目的"}
    with runtime.control.db.connection() as db:
        runtime.control._put(db, "adapter", draft["id"], draft)
    with pytest.raises(Denied):
        runtime.adapters.approve(draft["id"], draft["digest"], REVIEWER)
