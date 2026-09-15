"""Unapproved declarative adapter proposals, with no activation or I/O capabilities."""

from __future__ import annotations

from pydantic import ConfigDict, Field, field_validator, model_validator

from opsyne.contracts.cases import EvidenceClaim
from opsyne.contracts.core import Model, Service
from opsyne.contracts.observations import (
    AdapterDefinition,
    CommonField,
    Outcome,
    Severity,
    Source,
)


class ProposalModel(Model):
    model_config = ConfigDict(strict=True)


class FieldMapping(ProposalModel):
    field: CommonField
    path: str

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        AdapterDefinition.check_path(value)
        return value


class Condition(ProposalModel):
    path: str
    value: str | int | bool | None

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        AdapterDefinition.check_path(value)
        return value


class OutcomeMapping(ProposalModel):
    input: str
    output: Outcome


class SeverityMapping(ProposalModel):
    input: str
    output: Severity


class AdapterProposal(ProposalModel):
    name: str = Field(min_length=1, max_length=200)
    fields: list[FieldMapping] = Field(max_length=11)
    conditions: list[Condition] = Field(max_length=30)
    outcome_map: list[OutcomeMapping] = Field(max_length=100)
    severity_map: list[SeverityMapping] = Field(max_length=100)
    rationale: list[EvidenceClaim] = Field(min_length=1, max_length=30)
    unknowns: list[str] = Field(max_length=50)

    @model_validator(mode="after")
    def consistent_proposal(self) -> AdapterProposal:
        keys: list[list[str]] = [
            [mapping.field for mapping in self.fields],
            [condition.path for condition in self.conditions],
            [mapping.input for mapping in self.outcome_map],
            [mapping.input for mapping in self.severity_map],
        ]
        if any(len(items) != len(set(items)) for items in keys):
            raise ValueError("duplicate fields, conditions, or mapping inputs are ambiguous")
        if any(not claim.text.strip() or not claim.evidence_ids for claim in self.rationale):
            raise ValueError("every rationale needs a nonempty claim and evidence references")
        if not self.fields and not any(value.strip() for value in self.unknowns):
            raise ValueError("a withheld proposal must explain the missing information")
        selected_fields = {mapping.field for mapping in self.fields}
        if self.outcome_map and "outcome" not in selected_fields:
            raise ValueError("outcome mapping requires an outcome field")
        if self.severity_map and "severity" not in selected_fields:
            raise ValueError("severity mapping requires a severity field")
        return self

    def to_definition(
        self,
        id: str,
        source: Source,
        service: Service,
        version: int = 1,
    ) -> AdapterDefinition:
        """Bind a draft to trusted registration; the returned definition is not approved."""
        if source.service_id != service.id or not source.enabled or not service.enabled:
            raise ValueError("source and service must be enabled and share the registered identity")
        if not self.fields:
            raise ValueError("proposal withheld: no safe field mappings are available")
        return AdapterDefinition(
            id=id,
            name=self.name,
            source_id=source.id,
            target_instance_id=service.instance_id,
            target_version=service.version,
            version=version,
            fields={mapping.field: mapping.path for mapping in self.fields},
            conditions={condition.path: condition.value for condition in self.conditions},
            outcome_map={mapping.input: mapping.output for mapping in self.outcome_map},
            severity_map={mapping.input: mapping.output for mapping in self.severity_map},
        )
