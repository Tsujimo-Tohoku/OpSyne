"""Pure Discord message views, independent of the product's internal models.

These cards are snapshots, not authorization. The interaction handler must fetch
and validate the current approval request before accepting a decision.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

_MAX_EMBED = 6000
_MAX_FIELD = 1024
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*):(?=//)")
_MARKDOWN = frozenset("\\`*_~|[]()<>#!")
_SNAPSHOT_NOTE = "保存時点の表示です。承認前に最新の計画・期限・権限を再確認します。"


@dataclass(frozen=True, slots=True)
class CaseView:
    """A disclosure-filtered case snapshot supplied by the Control API."""

    case_id: str
    title: str
    state: str
    summary: str
    observed_at: str
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanView:
    """Fixed plan terms; absence of conditions must be explicitly explained."""

    plan_id: str
    version: int
    digest: str
    target_id: str
    target_version: str
    operation: str
    impact: str
    preconditions: tuple[str, ...]
    success_conditions: tuple[str, ...]
    abort_conditions: tuple[str, ...]
    expires_at: str
    approval_request_id: str
    state: str = "DRAFT"
    approver: str = ""


def _units(value: str) -> int:
    # UTF-16 units conservatively satisfy limits for astral characters too.
    return len(value.encode("utf-16-le")) // 2


def _plain(value: str) -> str:
    """Escape formatting and controls, and disable links and mention syntax."""
    parts: list[str] = []
    for char in value:
        if char != "\n" and unicodedata.category(char).startswith("C"):
            parts.append(f"\\u{ord(char):04x}")
        elif char in _MARKDOWN:
            parts.append("\\" + char)
        elif char in {"@", "."}:
            parts.append(char + "\u200b")
        else:
            parts.append(char)
    # Break URI schemes without altering displayed timestamps.
    return _SCHEME.sub(lambda match: match.group(1) + ":\u200b", "".join(parts))


def _clip(value: str, limit: int) -> str:
    if _units(value) <= limit:
        return value
    parts: list[str] = []
    used = 0
    for char in value:
        cost = _units(char)
        if used + cost > limit - 1:
            break
        parts.append(char)
        used += cost
    return "".join(parts).rstrip("\\") + "…"


def _field(name: str, value: str) -> dict[str, Any]:
    return {"name": name, "value": value, "inline": False}


def _payload(embed: dict[str, Any]) -> dict[str, Any]:
    return {"embeds": [embed], "components": [], "allowed_mentions": {"parse": []}}


def render_case(case: CaseView) -> dict[str, Any]:
    """Render bounded notification text without treating unknown as resolved."""
    states = {
        "OPEN": "未解決",
        "INVESTIGATING": "調査中",
        "AWAITING_APPROVAL": "承認待ち",
        "APPROVED": "承認記録済み・実行前",
        "EXECUTING": "実行中・復旧未確認",
        "EXECUTED": "操作完了・独立した復旧確認待ち",
        "VERIFYING": "復旧確認中",
        "RESOLVED": "復旧確認済み (Controlの案件状態)",
        "FAILED": "失敗・復旧未確認",
        "UNKNOWN": "結果不明・照合が必要",
        "EXECUTION_UNKNOWN": "操作結果不明・照合が必要",
        "VERIFICATION_UNKNOWN": "復旧確認の結果不明",
    }
    state = states.get(case.state, "未対応または欠落した状態・復旧未確認")
    state_value = f"{state}\n受信状態: {_plain(case.state) or '欠落'}"
    # Independent budgets sum to less than 6000, even at their maxima.
    fields = [
        _field("案件ID", _clip(_plain(case.case_id).strip() or "欠落", 256)),
        _field("状態", _clip(state_value, 512)),
        _field("観測時刻", _clip(_plain(case.observed_at).strip() or "不明", 256)),
        _field(
            "証拠参照",
            _clip("\n".join(_plain(ref) for ref in case.evidence_refs).strip() or "未提供", 1024),
        ),
    ]
    return _payload(
        {
            "title": _clip(_plain(case.title).strip() or "異常の通知", 256),
            "description": _clip(_plain(case.summary).strip() or "概要未提供", 3500),
            "fields": fields,
            "color": 0x2E7D32 if case.state == "RESOLVED" else 0xC47F00,
            "footer": {"text": "観測時点の情報です。操作の成功と復旧確認は別に記録します。"},
        }
    )


def _complete(plan: PlanView) -> bool:
    scalars = (
        plan.plan_id,
        plan.digest,
        plan.target_id,
        plan.target_version,
        plan.operation,
        plan.impact,
        plan.expires_at,
    )
    conditions = (plan.preconditions, plan.success_conditions, plan.abort_conditions)
    if plan.version < 1 or not all(value.strip() for value in scalars):
        return False
    if any(not group or any(not item.strip() for item in group) for group in conditions):
        return False
    if _REQUEST_ID.fullmatch(plan.approval_request_id) is None:
        return False
    # Control characters in approval terms must not be silently normalized.
    values = (*scalars, *(item for group in conditions for item in group))
    if any(
        char != "\n" and unicodedata.category(char).startswith("C")
        for value in values
        for char in value
    ):
        return False
    try:
        expiry = datetime.fromisoformat(plan.expires_at)
    except ValueError:
        return False
    return expiry.tzinfo is not None and expiry.utcoffset() is not None


def render_plan(plan: PlanView) -> dict[str, Any]:
    """Display all fixed terms or omit actions when a complete view cannot fit.

    The review button starts a fresh review; it never records approval directly.
    The displayed deadline does not establish that a saved request is still valid.
    """
    terms = [
        ("計画ID", plan.plan_id),
        ("現在の計画状態", plan.state),
        ("承認者", plan.approver or "未記録"),
        ("表示形式の版", str(plan.version)),
        ("計画のdigest", plan.digest),
        ("対象実体", plan.target_id),
        ("対象の版", plan.target_version),
        ("操作", plan.operation),
        ("影響", plan.impact),
        ("事前条件", "\n".join(plan.preconditions)),
        ("成功条件", "\n".join(plan.success_conditions)),
        ("中止条件", "\n".join(plan.abort_conditions)),
        ("承認期限", plan.expires_at),
        ("承認要求ID", plan.approval_request_id),
    ]
    fields = [_field(name, _plain(value)) for name, value in terms]
    title = "対応計画の確認"
    description = "このカードは保存済みの計画です。確認ボタンから現在の承認要求を照会します。"
    total = _units(title + description + _SNAPSHOT_NOTE) + sum(
        _units(str(field["name"]) + str(field["value"])) for field in fields
    )
    if (
        not _complete(plan)
        or any(_units(str(field["value"])) > _MAX_FIELD for field in fields)
        or total > _MAX_EMBED
    ):
        return _payload(
            {
                "title": "対応計画の全文確認が必要",
                "description": (
                    "必須情報が欠けているか、計画を安全に全文表示できません。"
                    "このカードから承認・差し戻しはできません。"
                    "Controlの認証済み画面で計画の全文と最新版を確認してください。"
                ),
                "fields": [
                    _field("計画ID (参照用)", _clip(_plain(plan.plan_id).strip() or "欠落", 256)),
                    _field("表示形式の版 (参照用)", _clip(str(plan.version), 256)),
                ],
                "color": 0xC47F00,
                "footer": {"text": _SNAPSHOT_NOTE},
            }
        )
    payload = _payload(
        {
            "title": title,
            "description": description,
            "fields": fields,
            "color": 0xC47F00,
            "footer": {"text": _SNAPSHOT_NOTE},
        }
    )
    if plan.state != "DRAFT":
        return payload
    payload["components"] = [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 1,
                    "label": "計画を確認",
                    "custom_id": f"review:{plan.approval_request_id}",
                },
                {
                    "type": 2,
                    "style": 2,
                    "label": "差し戻す",
                    "custom_id": f"reject:{plan.approval_request_id}",
                },
            ],
        }
    ]
    return payload
