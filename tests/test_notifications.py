"""Offline delivery regressions; no AstrBot process or network is started."""

import asyncio
import importlib.util
import json
import logging
import sys
import tempfile
import types
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, patch


def load_plugin():
    def decorator(*args, **kwargs):
        return lambda value: value

    class Star:
        def __init__(self, context):
            self.context = context

    class MessageChain:
        def __init__(self, chain):
            self.chain = chain

    class Plain:
        def __init__(self, text):
            self.text = text

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    components = types.ModuleType("astrbot.api.message_components")
    api.logger = logging.getLogger("livepace-test")
    event.filter = types.SimpleNamespace(
        command=decorator,
        permission_type=decorator,
        PermissionType=types.SimpleNamespace(ADMIN="admin"),
    )
    event.AstrMessageEvent = object
    event.MessageEventResult = object
    event.MessageChain = MessageChain
    star.Context = object
    star.Star = Star
    star.register = decorator
    components.Plain = Plain
    astrbot.api = api
    api.event, api.star, api.message_components = event, star, components
    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "astrbot.api.message_components": components,
    }
    spec = importlib.util.spec_from_file_location(
        "livepace_test_plugin", Path(__file__).resolve().parents[1] / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


plugin_module = load_plugin()


def subscription(subscribers, *, world=None, event=None):
    return {
        "subscriber_id": subscribers,
        "room_id": None,
        "player_name": "runner",
        "last_world_id": world,
        "last_event": event,
    }


def snapshot(world="world-one", event="rsg.first_portal", name="runner"):
    return {
        "nickname": name,
        "worldId": world,
        "gameVersion": "1.16.1",
        "eventList": [{"eventId": event, "rta": 200000, "igt": 180000}],
    }


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="livepace-test-")
        self.state_path = (
            Path(self.temporary.name) / "data" / "live_paceman_players_list.json"
        )
        self.path_patch = patch.object(
            plugin_module, "PLAYERS_LIST_FILE", str(self.state_path)
        )
        self.path_patch.start()
        self.sleep_patch = patch.object(plugin_module.asyncio, "sleep", new=AsyncMock())
        self.sleep_patch.start()
        self.logger_patch = patch.object(plugin_module, "logger")
        self.logger_patch.start()
        self.plugins = []

    async def asyncTearDown(self):
        for plugin in self.plugins:
            if plugin.task is not None and not plugin.task.done():
                plugin.task.cancel()
                await asyncio.gather(plugin.task, return_exceptions=True)
        self.logger_patch.stop()
        self.sleep_patch.stop()
        self.path_patch.stop()
        self.temporary.cleanup()

    def create_plugin(self, players=None, send=None):
        if players is not None:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(players), encoding="utf-8")
        context = types.SimpleNamespace(
            send_message=send or AsyncMock(return_value=True)
        )
        plugin = plugin_module.LivePaceman(context)
        plugin._fetch_live_paceman = AsyncMock(return_value=[snapshot()])
        self.plugins.append(plugin)
        return plugin

    def persisted_world(self, player="runner", world="world-one"):
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        return next(
            item
            for item in saved[player]["notify_state"]["worlds"]
            if item["world_id"] == world
        )

    async def test_partial_failure_retries_only_failed_subscriber(self):
        calls = Counter()

        async def send(origin, chain):
            calls[origin] += 1
            if origin == "group-b" and calls[origin] == 1:
                world = self.persisted_world()
                self.assertEqual(world["sent_to"]["rsg.first_portal"], ["group-a"])
                raise RuntimeError("second group temporarily unavailable")
            return True

        plugin = self.create_plugin(
            {"runner": subscription(["group-a", "group-b"])},
            AsyncMock(side_effect=send),
        )
        await plugin._notify_player()
        await plugin._notify_player()
        self.assertEqual(calls, Counter({"group-a": 1, "group-b": 2}))
        self.assertEqual(
            set(self.persisted_world()["sent_to"]["rsg.first_portal"]),
            {"group-a", "group-b"},
        )

    async def test_session_failure_does_not_block_other_sessions_or_players(self):
        async def send(origin, chain):
            if origin == "broken":
                raise RuntimeError("group unavailable")
            return True

        plugin = self.create_plugin(
            {
                "runner": subscription(["broken", "group-b", "group-c"]),
                "other": subscription(["group-d"]),
            },
            AsyncMock(side_effect=send),
        )
        plugin._fetch_live_paceman.return_value = [snapshot(), snapshot(name="other")]
        await plugin._notify_player()
        self.assertEqual(
            [call.args[0] for call in plugin.context.send_message.await_args_list],
            ["broken", "group-b", "group-c", "group-d"],
        )
        self.assertEqual(
            self.persisted_world()["sent_to"]["rsg.first_portal"],
            ["group-b", "group-c"],
        )
        self.assertEqual(
            self.persisted_world("other")["sent_to"]["rsg.first_portal"], ["group-d"]
        )

    async def test_false_result_remains_retryable(self):
        calls = Counter()

        async def send(origin, chain):
            calls[origin] += 1
            return not (origin == "group-b" and calls[origin] == 1)

        plugin = self.create_plugin(
            {"runner": subscription(["group-a", "group-b"])},
            AsyncMock(side_effect=send),
        )
        await plugin._notify_player()
        self.assertEqual(
            self.persisted_world()["sent_to"]["rsg.first_portal"], ["group-a"]
        )
        await plugin._notify_player()
        self.assertEqual(calls, Counter({"group-a": 1, "group-b": 2}))

    async def test_event_snapshot_rollback_does_not_replay(self):
        plugin = self.create_plugin({"runner": subscription(["group-a"])})
        for event in ("rsg.first_portal", "rsg.enter_stronghold", "rsg.first_portal"):
            plugin._fetch_live_paceman.return_value = [snapshot(event=event)]
            await plugin._notify_player()
        self.assertEqual(plugin.context.send_message.await_count, 2)
        self.assertEqual(
            self.persisted_world()["highest_event"], "rsg.enter_stronghold"
        )

    async def test_world_snapshot_rollback_does_not_replay(self):
        plugin = self.create_plugin({"runner": subscription(["group-a"])})
        for world in ("world-one", "world-two", "world-one"):
            plugin._fetch_live_paceman.return_value = [snapshot(world=world)]
            await plugin._notify_player()
        self.assertEqual(plugin.context.send_message.await_count, 2)

    async def test_restart_preserves_per_subscriber_success(self):
        async def send(origin, chain):
            if origin == "group-b":
                raise RuntimeError("group unavailable")
            return True

        first = self.create_plugin(
            {"runner": subscription(["group-a", "group-b"])},
            AsyncMock(side_effect=send),
        )
        await first._notify_player()
        second = self.create_plugin()
        await second._notify_player()
        self.assertEqual(
            [call.args[0] for call in second.context.send_message.await_args_list],
            ["group-b"],
        )

    async def test_legacy_state_migrates_without_replaying_earlier_milestones(self):
        plugin = self.create_plugin(
            {
                "runner": subscription(
                    ["group-a", "group-b"], world="world-one", event="rsg.enter_end"
                )
            }
        )
        for event in ("rsg.first_portal", "rsg.enter_stronghold", "rsg.enter_end"):
            plugin._fetch_live_paceman.return_value = [snapshot(event=event)]
            await plugin._notify_player()
        plugin.context.send_message.assert_not_awaited()
        plugin._fetch_live_paceman.return_value = [snapshot(event="rsg.credits")]
        await plugin._notify_player()
        self.assertEqual(plugin.context.send_message.await_count, 2)
        self.assertEqual(self.persisted_world()["highest_event"], "rsg.credits")

    async def test_concurrent_polls_share_delivery_state(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(origin, chain):
            entered.set()
            await release.wait()
            return True

        plugin = self.create_plugin(
            {"runner": subscription(["group-a"])}, AsyncMock(side_effect=send)
        )
        one = asyncio.create_task(plugin._notify_player())
        two = asyncio.create_task(plugin._notify_player())
        await asyncio.wait_for(entered.wait(), 1)
        release.set()
        await asyncio.gather(one, two)
        plugin.context.send_message.assert_awaited_once()

    async def test_duplicate_subscribers_receive_once(self):
        plugin = self.create_plugin(
            {"runner": subscription(["group-a", "group-a", "group-b", "group-b"])}
        )
        await plugin._notify_player()
        self.assertEqual(
            [call.args[0] for call in plugin.context.send_message.await_args_list],
            ["group-a", "group-b"],
        )
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["runner"]["subscriber_id"], ["group-a", "group-b"])

    async def test_world_history_is_bounded(self):
        plugin = self.create_plugin({"runner": subscription(["group-a"])})
        for index in range(plugin_module.MAX_NOTIFY_WORLDS + 3):
            plugin._fetch_live_paceman.return_value = [snapshot(world=f"world-{index}")]
            await plugin._notify_player()
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        worlds = saved["runner"]["notify_state"]["worlds"]
        self.assertEqual(len(worlds), plugin_module.MAX_NOTIFY_WORLDS)
        self.assertEqual(worlds[0]["world_id"], "world-3")

    async def test_atomic_save_failure_keeps_previous_file(self):
        plugin = self.create_plugin({"runner": subscription(["group-a"])})
        previous = self.state_path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("replace failed")):
            self.assertFalse(plugin._save_players_list(plugin.players))
        self.assertEqual(self.state_path.read_bytes(), previous)
        self.assertEqual(list(self.state_path.parent.glob("*.tmp")), [])

    async def test_failed_ack_save_keeps_in_memory_deduplication(self):
        plugin = self.create_plugin({"runner": subscription(["group-a"])})
        save = plugin._save_players_list
        calls = 0

        def fail_ack_save(players):
            nonlocal calls
            calls += 1
            return False if calls == 2 else save(players)

        with patch.object(plugin, "_save_players_list", side_effect=fail_ack_save):
            await plugin._notify_player()
            await plugin._notify_player()
        plugin.context.send_message.assert_awaited_once()
        self.assertEqual(self.persisted_world()["sent_to"]["rsg.first_portal"], [])
        self.assertEqual(
            plugin.players["runner"]["notify_state"]["worlds"][0]["sent_to"][
                "rsg.first_portal"
            ],
            ["group-a"],
        )
        plugin_module.logger.warning.assert_called_once()

    async def test_terminate_waits_for_poll_before_final_save(self):
        entered = asyncio.Event()

        async def send(origin, chain):
            if origin == "group-b":
                entered.set()
                await asyncio.Event().wait()
            return True

        plugin = self.create_plugin(
            {"runner": subscription(["group-a", "group-b"])},
            AsyncMock(side_effect=send),
        )
        plugin.task = asyncio.create_task(plugin._notify_player())
        await asyncio.wait_for(entered.wait(), 1)
        await plugin.terminate()
        self.assertIsNone(plugin.task)
        self.assertEqual(
            self.persisted_world()["sent_to"]["rsg.first_portal"], ["group-a"]
        )
        reloaded = self.create_plugin()
        await reloaded._notify_player()
        self.assertEqual(
            [call.args[0] for call in reloaded.context.send_message.await_args_list],
            ["group-b"],
        )


if __name__ == "__main__":
    unittest.main()
