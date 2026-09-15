"""Human intent and agent suggestions are distinct approval inputs."""

from typing import Annotated

from pydantic import Field, StringConstraints

from opsyne.contracts.cases import EvidenceClaim
from opsyne.contracts.core import Model
from opsyne.contracts.observations import AdapterDefinition

ReviewText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class HumanExplanation(Model):
    monitoring_purpose: ReviewText | None = None
    expected_insights: list[ReviewText] = Field(default_factory=list, max_length=30)
    rationale: list[ReviewText] = Field(default_factory=list, max_length=30)
    questions: list[ReviewText] = Field(default_factory=list, max_length=50)


class AgentExplanation(Model):
    task_id: str
    suggested_use: ReviewText | None = None
    rationale: list[EvidenceClaim] = Field(max_length=30)
    unknowns: list[str] = Field(max_length=50)


class AdapterDraftRequest(AdapterDefinition):
    explanation: HumanExplanation | None = None


class ExplanationUpdate(Model):
    digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    explanation: HumanExplanation
