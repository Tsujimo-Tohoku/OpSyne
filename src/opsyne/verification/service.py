"""Independent verification invokes a read-only observation port."""

from __future__ import annotations

import time
from collections.abc import Callable

from opsyne.contracts.execution import VerificationResult


class VerificationService:
    def verify(self, check: Callable[[], VerificationResult]) -> VerificationResult:
        try:
            return VerificationResult.model_validate(check())
        except Exception:
            return VerificationResult(
                status="UNKNOWN", detail="Independent observation failed", checked_at=time.time()
            )
