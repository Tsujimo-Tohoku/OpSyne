"""Local installation credentials; only authenticated identities reach Control."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from opsyne.contracts.core import Actor


class AccessTokens:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            entries = [
                {"actor": name, "role": role, "token": secrets.token_urlsafe(32)}
                for name, role in [
                    ("owner", "admin"),
                    ("operator", "operator"),
                    ("reviewer", "approver"),
                    ("viewer", "viewer"),
                ]
            ]
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    json.dump(entries, file, indent=2)
                    file.flush()
                    os.fsync(file.fileno())
            except FileExistsError:
                pass
        self._tokens = [
            (
                hashlib.sha256(item["token"].encode()).digest(),
                Actor(actor=item["actor"], role=item["role"]),
            )
            for item in json.loads(path.read_text(encoding="utf-8"))
        ]

    def authenticate(self, token: str) -> Actor | None:
        candidate = hashlib.sha256(token.encode()).digest()
        for known, actor in self._tokens:
            if hmac.compare_digest(known, candidate):
                return actor
        return None
