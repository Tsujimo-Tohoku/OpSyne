"""Exercise both HTTP apps and delivery in memory; no Discord or LLM credentials."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import httpx
from nacl.signing import SigningKey

from opsyne.api.app import create_app
from opsyne.contracts.core import Actor
from opsyne.runtime import Runtime


async def check(path: Path) -> None:
    # Integration-only composition: neither distribution imports the other.
    bot_source = Path(__file__).resolve().parents[1] / "apps" / "discord_bot" / "src"
    sys.path.insert(0, str(bot_source))
    bot_app = importlib.import_module("opsyne_discord.app")
    bot_config = importlib.import_module("opsyne_discord.config")
    bot_feed = importlib.import_module("opsyne_discord.feed")
    bot_delivery = importlib.import_module("opsyne_discord.delivery")
    runtime = Runtime(path / "product", api_key=None)
    owner = Actor(actor="owner", role="admin")
    runtime.seed_demo(owner)
    case = next(c for c in runtime.control.objects("case") if c["kind"] == "operation_failure")
    plan = runtime.control.create_plan(case["id"], "demo-restore", "Synthetic recovery", "owner")
    key = SigningKey.generate()
    token = "synthetic-bridge-token-" + "x" * 32
    config_path = path / "discord.json"
    config_path.write_text(
        json.dumps(
            {
                "application_id": "10",
                "public_key": key.verify_key.encode().hex(),
                "guild_id": "20",
                "bridge_token": token,
                "routes": {case["service_id"]: {"channel_id": "30", "disclose_plan_details": True}},
                "users": {"40": {"actor": "reviewer", "service_ids": [case["service_id"]]}},
            }
        ),
        encoding="utf-8",
    )
    with patch.dict(os.environ, {"OPSYNE_DISCORD_CONFIG": str(config_path)}):
        product = create_app(runtime=runtime, background=False)
    settings = bot_config.Settings(
        "10",
        key.verify_key.encode().hex(),
        "20",
        frozenset({"30"}),
        path / "bot",
        "i" * 32,
        bridge_url="http://127.0.0.1/api/discord/interactions",
        bridge_token=token,
        feed_url="http://127.0.0.1/api/discord/notifications",
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=product)) as control_client:
        adapter = bot_app.create_app(settings, client=control_client)
        async with (
            adapter.router.lifespan_context(adapter),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=adapter),
                base_url="http://bot",
            ) as discord_client,
        ):

            async def click(interaction: str, custom_id: str) -> dict[str, object]:
                raw = json.dumps(
                    {
                        "application_id": "10",
                        "guild_id": "20",
                        "channel_id": "30",
                        "id": interaction,
                        "type": 3,
                        "member": {"user": {"id": "40"}},
                        "data": {"component_type": 2, "custom_id": custom_id},
                        "token": "synthetic-interaction-token-do-not-persist",
                    }
                ).encode()
                stamp = str(int(time.time()))
                response = await discord_client.post(
                    "/interactions",
                    content=raw,
                    headers={
                        "X-Signature-Ed25519": key.sign(stamp.encode() + raw).signature.hex(),
                        "X-Signature-Timestamp": stamp,
                    },
                )
                assert response.status_code == 200, response.text
                result: dict[str, object] = response.json()
                return result

            review = await click("100", "review:" + plan["id"])
            # A real main-product plan must fit the Bot's complete-plan renderer.
            view = json.loads(json.dumps(review))
            button = view["data"]["components"][0]["components"][0]["custom_id"]
            assert button.startswith("confirm:")
            approval = await click("101", button)
            assert "Controlに承認を記録しました" in json.dumps(approval, ensure_ascii=False)
            assert not runtime.execution_list()
            execution = runtime.execute(plan["id"], owner)
            assert execution.status == "SUCCEEDED"
            assert runtime.control.get("case", case["id"])["status"] != "RESOLVED"
            assert runtime.verify(execution.id, owner).status == "PASS"
            feed = await control_client.get(
                settings.feed_url, headers={"Authorization": "Bearer " + token}
            )
            assert feed.status_code == 200
            outbox = bot_delivery.Outbox(settings.state_dir / "outbox.sqlite3")
            with httpx.Client(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json=feed.json()))
            ) as client:
                received = bot_feed.sync_once(settings, outbox, client)
                assert received > 0
            sent = []

            def receive(request: httpx.Request) -> httpx.Response:
                message = json.loads(request.content)
                assert message["allowed_mentions"] == {"parse": []}
                sent.append(message)
                return httpx.Response(200, json={"id": str(1000 + len(sent))})

            with httpx.Client(transport=httpx.MockTransport(receive)) as client:
                delivery = bot_delivery.DiscordDelivery(
                    outbox, client, "synthetic-bot-token", settings.channel_ids
                )
                while delivery.run_once(time.time()):
                    pass
            assert len(sent) == received
            print(
                "PASS: signed review/approval, execution, independent verification, "
                f"{received} notifications"
            )


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="opsyne-discord-check-") as directory:
        asyncio.run(check(Path(directory)))
