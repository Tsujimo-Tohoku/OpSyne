"""Explicit server exposure settings; loopback remains the default."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    allowed_hosts: list[str]
    trusted_proxies: str

    @classmethod
    def from_env(cls, *, host: str | None = None, port: int | None = None) -> ServerSettings:
        listen_host = host if host is not None else os.environ.get("OPSYNE_HOST", "127.0.0.1")
        try:
            ipaddress.ip_address(listen_host)
        except ValueError as exc:
            raise ValueError("OPSYNE_HOST / --host must be an IP address") from exc
        try:
            listen_port = (
                port
                if port is not None
                else int(os.environ.get("OPSYNE_PORT", os.environ.get("PORT", "8765")))
            )
        except ValueError as exc:
            raise ValueError("server port must be an integer") from exc
        if not 1 <= listen_port <= 65535:
            raise ValueError("server port must be between 1 and 65535")
        allowed = [
            item.strip()
            for item in os.environ.get(
                "OPSYNE_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver"
            ).split(",")
        ]
        if any(
            not item
            or len(item) > 253
            or re.fullmatch(r"[A-Za-z0-9]+(?:[A-Za-z0-9.-]*[A-Za-z0-9])?", item) is None
            for item in allowed
        ):
            raise ValueError(
                "OPSYNE_ALLOWED_HOSTS requires exact hostnames without ports or wildcards"
            )
        proxies = os.environ.get("OPSYNE_TRUSTED_PROXIES", "127.0.0.1")
        if proxies:
            try:
                for address in proxies.split(","):
                    network = ipaddress.ip_network(address.strip(), strict=False)
                    if network.prefixlen == 0:
                        raise ValueError("all-address trust is not allowed")
            except ValueError as exc:
                raise ValueError(
                    "OPSYNE_TRUSTED_PROXIES requires specific proxy IPs or networks"
                ) from exc
        return cls(listen_host, listen_port, allowed, proxies)
