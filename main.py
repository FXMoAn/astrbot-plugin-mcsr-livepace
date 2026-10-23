import asyncio
import json
import os
import tempfile
from pathlib import Path

import httpx

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

# 用户列表FILE
PLAYERS_LIST_FILE = "data/live_paceman_players_list.json"
NOTIFY_EVENTS = (
    "rsg.first_portal",
    "rsg.enter_stronghold",
    "rsg.enter_end",
    "rsg.credits",
)
MAX_NOTIFY_WORLDS = 64


@register("livepaceman", "Mo_An", "livepaceman", "1.0.6")
class LivePaceman(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.players = self._load_players_list()
        self.task = None
        self._poll_lock = asyncio.Lock()
        self._stopping = False
        for player in self.players.values():
            self._prepare_notify_state(player)

    def _load_players_list(self):
        if os.path.exists(PLAYERS_LIST_FILE):
            try:
                with open(PLAYERS_LIST_FILE, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError) as e:
                logger.error(f"加载订阅数据失败: {e}")
        return {}

    def _save_players_list(self, players_list):
        temporary_path = None
        try:
            target = Path(PLAYERS_LIST_FILE)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(players_list, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(target)
            return True
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"保存用户列表数据失败: {e}")
            return False
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _prepare_notify_state(self, player):
        # State size follows active subscriptions and at most 64 worlds per player.
        subscribers = list(dict.fromkeys(player.get("subscriber_id", [])))
        player["subscriber_id"] = subscribers
        active_subscribers = set(subscribers)
        state = player.get("notify_state")
        if not isinstance(state, dict) or state.get("version") != 1:
            state = {"version": 1, "worlds": []}
            world_id = player.get("last_world_id")
            event_id = player.get("last_event")
            if world_id and event_id in NOTIFY_EVENTS:
                # Legacy state was committed only after every subscriber was sent.
                # Earlier milestones must not replay after this migration either.
                state["worlds"].append(
                    {
                        "world_id": world_id,
                        "highest_event": event_id,
                        "sent_to": {
                            event: subscribers.copy()
                            for event in NOTIFY_EVENTS[
                                : NOTIFY_EVENTS.index(event_id) + 1
                            ]
                        },
                    }
                )
            player["notify_state"] = state

        worlds = state.get("worlds")
        if not isinstance(worlds, list):
            worlds = []
        unique_worlds = {}
        for world in worlds:
            if not isinstance(world, dict) or not world.get("world_id"):
                continue
            sent_to = world.get("sent_to")
            if not isinstance(sent_to, dict):
                sent_to = {}
            world["sent_to"] = {
                event: list(
                    dict.fromkeys(
                        subscriber
                        for subscriber in recipients
                        if subscriber in active_subscribers
                    )
                )
                for event, recipients in sent_to.items()
                if event in NOTIFY_EVENTS and isinstance(recipients, list)
            }
            if world.get("highest_event") not in NOTIFY_EVENTS:
                world["highest_event"] = None
            unique_worlds[world["world_id"]] = world
        state["worlds"] = list(unique_worlds.values())[-MAX_NOTIFY_WORLDS:]
        return state

    def _world_notify_state(self, player, world_id, create=False):
        state = self._prepare_notify_state(player)
        for world in state["worlds"]:
            if world["world_id"] == world_id:
                return world
        if create:
            world = {"world_id": world_id, "highest_event": None, "sent_to": {}}
            state["worlds"].append(world)
            state["worlds"] = state["worlds"][-MAX_NOTIFY_WORLDS:]
            return world
        return None

    def _normalize_player_name(self, player_name):
        return player_name.lower()

    async def _check_player_exists(self, player_name):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"https://paceman.gg/stats/api/getSessionStats/?name={player_name}&hours=24&hoursBetween=24"
                )
                response.raise_for_status()
                data = response.json()
                return bool(data["nether"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return False

    def _format_time(self, time: int):
        # 将6位数毫秒级时间转换为分钟:秒.毫秒格式
        minutes = time // 60000
        seconds = (time % 60000) // 1000
        milliseconds = time - minutes * 60000 - seconds * 1000
        return f"{minutes}:{seconds:02d}.{milliseconds:03d}"

    # 订阅玩家命令
    @filter.command("livepacesub")
    async def livePacemanSub(
        self, event: AstrMessageEvent, player_name: str, room_id: str | None = None
    ):
        """订阅玩家"""
        if not await self._check_player_exists(player_name):
            yield event.plain_result(f"玩家 {player_name} 不存在，请输入正确的玩家名。")
            return

        subscriber_id = event.unified_msg_origin
        formatted_player_name = self._normalize_player_name(player_name)

        if formatted_player_name not in self.players:
            self.players.setdefault(
                formatted_player_name,
                {
                    "subscriber_id": [subscriber_id],
                    "room_id": room_id,
                    "player_name": player_name,
                    "last_world_id": None,
                    "last_event": None,
                },
            )
            yield event.plain_result(
                f"玩家 {player_name} 订阅成功，房间ID为 {room_id}。"
            )
        else:
            if (
                subscriber_id
                not in self.players[formatted_player_name]["subscriber_id"]
            ):
                self.players[formatted_player_name]["subscriber_id"].append(
                    subscriber_id
                )
            self.players[formatted_player_name]["room_id"] = room_id
            self.players[formatted_player_name]["player_name"] = player_name
            yield event.plain_result(
                f"玩家 {player_name} 订阅更新，房间ID为 {room_id}。"
            )

        self._save_players_list(self.players)

    @filter.command("livepaceunsub")
    async def livePacemanUnsub(self, event: AstrMessageEvent, player_name: str):
        """取消订阅玩家"""
        player_name = player_name.lower()
        if player_name not in self.players:
            yield event.plain_result(f"玩家 {player_name} 不存在，请输入正确的玩家名。")
            return
        self.players[player_name]["subscriber_id"].remove(event.unified_msg_origin)
        if not self.players[player_name]["subscriber_id"]:
            del self.players[player_name]
        self._save_players_list(self.players)
        yield event.plain_result(f"你取消了订阅 {player_name}。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("livepaceunsuball")
    async def livePacemanUnsubAll(self, event: AstrMessageEvent, player_name: str):
        """取消所有订阅,需要管理员权限"""
        player_name = player_name.lower()
        if player_name not in self.players:
            yield event.plain_result(f"玩家 {player_name} 不存在，请输入正确的玩家名。")
            return
        for subscriber_id in self.players[player_name]["subscriber_id"]:
            self.players[player_name]["subscriber_id"].remove(subscriber_id)
        if not self.players[player_name]["subscriber_id"]:
            del self.players[player_name]
        self._save_players_list(self.players)
        yield event.plain_result(f"你取消了订阅 {player_name} 的所有订阅。")

    # 获取订阅列表命令
    @filter.command("livepacesublist")
    async def livePacemanSubList(self, event: AstrMessageEvent):
        """获取订阅列表"""
        subscriber_id = event.unified_msg_origin
        players = self.players.get(subscriber_id, [])
        if not players:
            yield event.plain_result("你还没有订阅任何玩家。")
            return
        yield event.plain_result(
            f"你订阅了以下玩家：{', '.join([player['player_name'] for player in players])}"
        )

    @filter.command("livepacechangeroom")
    async def livePacemanChangeRoom(
        self, event: AstrMessageEvent, player_name: str, room_id: str
    ):
        """修改直播间ID"""
        player_name = player_name.lower()
        if player_name not in self.players:
            yield event.plain_result(f"玩家 {player_name} 不存在，请输入正确的玩家名。")
            return
        if room_id == "0":
            self.players[player_name]["room_id"] = None
        else:
            self.players[player_name]["room_id"] = room_id
        self._save_players_list(self.players)
        yield event.plain_result(f"你修改了 {player_name} 的直播间ID为 {room_id}")

    async def _fetch_live_paceman(self):
        """获取实时pace数据"""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    "https://paceman.gg/api/ars/liveruns?gameVersion=all&liveOnly=false"
                )
                return response.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.error(f"获取实时pace数据失败: {e}")
            return None

    def _should_notify(
        self, player_name: str, world_id: str, event_id: str, igt: int, version: str
    ):
        """是否需要通知玩家"""
        if event_id not in NOTIFY_EVENTS:
            return False
        player = self.players[player_name]
        world = self._world_notify_state(player, world_id)
        if not player["subscriber_id"]:
            return False
        if world is not None:
            highest_event = world["highest_event"]
            if highest_event and NOTIFY_EVENTS.index(event_id) < NOTIFY_EVENTS.index(
                highest_event
            ):
                return False
            sent_to = world["sent_to"].get(event_id, [])
            if all(subscriber in sent_to for subscriber in player["subscriber_id"]):
                return False

        if version == "1.16.1":
            match event_id:
                case "rsg.first_portal":
                    # 盲传时间大于8分钟不通知
                    if igt > 480000:
                        return False
                case "rsg.enter_stronghold":
                    # 进要塞时间大于11分钟不通知
                    if igt > 660000:
                        return False
                case "rsg.enter_end":
                    # 进末地时间大于13分钟不通知
                    if igt > 780000:
                        return False
                case "rsg.credits":
                    # 只要通关就通知
                    return True
                case _:
                    return False
        else:
            if event_id not in [
                "rsg.first_portal",
                "rsg.enter_stronghold",
                "rsg.enter_end",
                "rsg.credits",
            ]:
                return False
        return True

    async def _is_player_online(self, room_id: str | None):
        if room_id is None:
            return False
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": f"https://live.bilibili.com/{room_id}",
                "Accept": "application/json, text/plain, */*",
            }
            async with httpx.AsyncClient(timeout=10, headers=headers) as client:
                response = await client.get(
                    f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}"
                )
                response.raise_for_status()
                logger.info(f"检查玩家 {room_id} 是否在线成功")
                data = response.json()
                return bool(data.get("data") and data["data"].get("live_status") == 1)

        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as e:
            logger.error(f"检查玩家 {room_id} 是否在线失败: {e}")
            return False

    async def _build_message(self, player_name: str, current_stats: dict):
        current_event = current_stats["eventList"][-1]
        world_id = current_stats["worldId"]
        version = current_stats["gameVersion"]

        rta = self._format_time(current_event["rta"])
        igt = self._format_time(current_event["igt"])
        pure_igt = current_event["igt"]
        event_id = current_event["eventId"]

        if not self._should_notify(player_name, world_id, event_id, pure_igt, version):
            logger.info(
                f"玩家 {player_name} 不需要通知，世界ID: {world_id}，事件: {event_id}"
            )
            return None

        match event_id:
            case "rsg.first_portal":
                event_name = "盲传"
            case "rsg.enter_stronghold":
                event_name = "进要塞"
            case "rsg.enter_end":
                event_name = "进末地"
            case "rsg.credits":
                event_name = "已通关"
            case _:
                return None

        message = (
            f"{player_name} 当前实时pace:\n"
            f"游戏版本: {version}\n"
            f"当前进度: {event_name} \n"
            f"真实时间: {rta} \n"
            f"游戏时间: {igt} \n"
        )

        room_id = self.players[player_name]["room_id"]
        logger.info(f"[Debug] 玩家 {player_name} 直播间ID: {room_id}")
        if room_id is not None:
            logger.info(f"[Debug] 检查玩家 {player_name} 直播间是否在线: {room_id}")
            if await self._is_player_online(room_id):
                message += f"直播间: https://live.bilibili.com/{room_id}"

        logger.info(f"玩家 {player_name} 当前实时pace: {message}")

        # 返回消息和状态信息，在发送成功后再更新
        return message, world_id, event_id

    async def _notify_player(self):
        async with self._poll_lock:
            if not self._stopping:
                await self._notify_players_locked()

    async def _notify_players_locked(self):
        data = await self._fetch_live_paceman()
        if data is None:
            logger.error("获取实时pace数据失败，跳过通知")
            return
        current_players = [player["nickname"].lower() for player in data]
        players = list(self.players.keys())

        for player in players:
            try:
                if player in current_players:
                    current_stats = next(
                        item for item in data if item["nickname"].lower() == player
                    )
                    result = await self._build_message(player, current_stats)

                    if result is None:
                        logger.info(f"玩家 {player} 不需要通知")
                        continue

                    message, world_id, event_id = result

                    record = self.players.get(player)
                    if record is None:
                        continue
                    world = self._world_notify_state(record, world_id, create=True)
                    highest_event = world["highest_event"]
                    if highest_event is None or NOTIFY_EVENTS.index(
                        event_id
                    ) > NOTIFY_EVENTS.index(highest_event):
                        world["highest_event"] = event_id
                    sent_to = world["sent_to"].setdefault(event_id, [])
                    # Persist observed progress separately from successful delivery.
                    self._save_players_list(self.players)
                    for subscriber_id in record["subscriber_id"].copy():
                        if (
                            self.players.get(player) is not record
                            or subscriber_id not in record["subscriber_id"]
                            or subscriber_id in sent_to
                        ):
                            continue
                        try:
                            delivered = await self.context.send_message(
                                subscriber_id, MessageChain(chain=[Comp.Plain(message)])
                            )
                            if delivered is False:
                                raise RuntimeError(
                                    "No platform was found for the subscribed session"
                                )
                        except Exception as e:  # noqa: BLE001 - isolate arbitrary platform adapter errors
                            logger.exception(
                                f"通知会话失败: player={player}, world_id={world_id}, "
                                f"event={event_id}, subscriber={subscriber_id}: {e}"
                            )
                            continue
                        # Only acknowledge after the platform reports successful delivery.
                        # A network timeout can still leave the remote outcome unknown.
                        sent_to.append(subscriber_id)
                        if all(
                            recipient in sent_to
                            for recipient in record["subscriber_id"]
                        ):
                            record["last_world_id"] = world_id
                            record["last_event"] = event_id
                        if not self._save_players_list(self.players):
                            logger.warning(
                                f"通知已成功但去重状态仅保留在内存中，重启后可能重复: "
                                f"player={player}, world_id={world_id}, event={event_id}, "
                                f"subscriber={subscriber_id}"
                            )
                        logger.info(
                            f"会话通知状态已更新: player={player}, world_id={world_id}, "
                            f"event={event_id}, subscriber={subscriber_id}"
                        )
                        await asyncio.sleep(0.5)
            except Exception as e:  # noqa: BLE001 - one malformed player must not block the others
                logger.exception(f"通知玩家失败: player={player}: {e}")

    async def _check_live_paceman_periodically(self):
        try:
            while True:
                try:
                    await self._notify_player()
                except Exception as e:  # noqa: BLE001 - keep the polling task alive after an isolated failure
                    logger.exception(f"获取实时pace数据失败: {e}")

                # 每15秒检查一次
                await asyncio.sleep(15)
        except Exception as e:  # noqa: BLE001 - report unexpected polling task failures
            logger.exception(f"停止检查实时pace数据: {e}")

    # 生命周期方法
    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        if self.task is not None and not self.task.done():
            logger.warning("实时pace插件已经在运行中，跳过重复初始化")
            return

        self._stopping = False
        self._save_players_list(self.players)
        self.task = asyncio.create_task(self._check_live_paceman_periodically())
        logger.info("实时pace插件已启动")

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        self._stopping = True
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        async with self._poll_lock:
            self._save_players_list(self.players)
        logger.info("实时pace插件已停止")
