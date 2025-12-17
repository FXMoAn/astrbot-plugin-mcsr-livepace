from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp

import os
import json
import httpx
import asyncio

# 用户列表FILE
PLAYERS_LIST_FILE = "data/live_paceman_players_list.json"

@register("livepaceman", "Mo_An", "livepaceman", "1.0.0")
class LivePaceman(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.players = self._load_players_list()
        self.task = None

    def _load_players_list(self):
        if os.path.exists(PLAYERS_LIST_FILE):
            try:
                with open(PLAYERS_LIST_FILE, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载订阅数据失败: {e}")
        return {}

    def _save_players_list(self, players_list):
        try:
            os.makedirs(os.path.dirname(PLAYERS_LIST_FILE), exist_ok=True)
            with open(PLAYERS_LIST_FILE, "w", encoding="utf-8") as f:
                json.dump(players_list, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存用户列表数据失败: {e}")

    def _normalize_player_name(self, player_name):
        return player_name.lower()

    def _check_player_exists(self, player_name):
        return True

    def _format_time(self, time: int):
        # 将6位数毫秒级时间转换为分钟:秒.毫秒格式
        minutes = time // 60000
        seconds = (time % 60000) // 1000
        milliseconds = (time - minutes * 60000 - seconds * 1000)
        return f"{minutes}:{seconds:02d}.{milliseconds:03d}"

    # 订阅玩家命令
    @filter.command("livepacesub")
    async def livePacemanSub(self, event: AstrMessageEvent, player_name: str, room_id: str | None = None):
        """订阅玩家"""
        if not self._check_player_exists(player_name):
            yield event.plain_result(f"玩家 {player_name} 不存在，请输入正确的玩家名。")
            return

        subscriber_id = event.unified_msg_origin
        formatted_player_name = self._normalize_player_name(player_name)
        
        if formatted_player_name not in self.players:
            self.players.setdefault(formatted_player_name, {
                "subscriber_id": [subscriber_id],
                "room_id": room_id,
                "player_name": player_name,
                "last_world_id": None,
                "last_event": None
            })
            yield event.plain_result(f"玩家 {player_name} 订阅成功，房间ID为 {room_id}。")
        else:
            self.players[formatted_player_name]["subscriber_id"].append(subscriber_id)
            self.players[formatted_player_name]["room_id"] = room_id
            self.players[formatted_player_name]["player_name"] = player_name
            yield event.plain_result(f"玩家 {player_name} 订阅更新，房间ID为 {room_id}。")

        self._save_players_list(self.players)

    # 获取订阅列表命令
    @filter.command("livepacesublist")
    async def livePacemanSubList(self, event: AstrMessageEvent):
        """获取订阅列表"""
        subscriber_id = event.unified_msg_origin
        players = self.players.get(subscriber_id, [])
        if not players:
            yield event.plain_result("你还没有订阅任何玩家。")
            return
        yield event.plain_result(f"你订阅了以下玩家：{', '.join([player['player_name'] for player in players])}")

    @filter.command("livepacechangeroom")
    async def livePacemanChangeRoom(self, event: AstrMessageEvent, player_name: str, room_id: str):
        """修改直播间ID"""
        player_name = player_name.lower()
        if player_name not in self.players.keys():
            yield event.plain_result(f"玩家 {player_name} 不存在，请输入正确的玩家名。")   
            return
        if room_id == '0':
            self.players[player_name]["room_id"] = None
        else:
            self.players[player_name]["room_id"] = room_id
        self._save_players_list(self.players)
        yield event.plain_result(f"你修改了 {player_name} 的直播间ID为 {room_id}")

    async def _fetch_live_paceman(self):
        """获取实时pace数据"""
        async with httpx.AsyncClient() as client:
            response = await client.get("https://paceman.gg/api/ars/liveruns")
            return response.json()

    def _should_notify(self, player_name: str, world_id: str, event_id: str, igt: int, version: str):
        """是否需要通知玩家"""
        last_world_id = self.players[player_name]["last_world_id"]
        last_event = self.players[player_name]["last_event"]
        if last_world_id == world_id and last_event == event_id:
            return False

        if version == '1.16.1':
            match event_id:
                case 'rsg.first_portal':
                    # 盲传时间大于8分钟不通知
                    if igt > 480000:
                        return False
                case 'rsg.enter_stronghold':
                    # 进要塞时间大于11分钟不通知
                    if igt > 660000:
                        return False
                case 'rsg.enter_end':
                    # 进末地时间大于13分钟不通知
                    if igt > 780000:
                        return False
                case 'rsg.credits':
                    # 只要通关就通知
                    return True
                case _:
                    return False
        else:
            if event_id not in ['rsg.first_portal', 'rsg.enter_stronghold', 'rsg.enter_end', 'rsg.credits']:
                return False
        return True

    async def _build_message(self, player_name: str, current_stats: dict):
        current_event = current_stats['eventList'][-1]
        world_id = current_stats['worldId']
        version = current_stats['gameVersion']
        
        rta = self._format_time(current_event['rta'])
        igt = self._format_time(current_event['igt'])
        pure_igt = current_event['igt']
        event_id = current_event['eventId']

        if not self._should_notify(player_name, world_id, event_id, pure_igt, version):
            logger.info(f"玩家 {player_name} 不需要通知，世界ID: {world_id}，事件: {event_id}")
            return None

        match event_id:
            case 'rsg.first_portal':
                event_name = "盲传"
            case 'rsg.enter_stronghold':
                event_name = "进要塞"
            case 'rsg.enter_end':
                event_name = "进末地"
            case 'rsg.credits':
                event_name = "已通关"
            case _:
                return None

        message = (f"{player_name} 当前实时pace:\n"
                   f"游戏版本: {version}\n"
                   f"当前进度: {event_name} \n"
                   f"真实时间: {rta} \n"
                   f"游戏时间: {igt} \n")

        logger.info(f"玩家 {player_name} 当前实时pace: {message}")

        self.players[player_name]["last_world_id"] = world_id
        self.players[player_name]["last_event"] = current_event['eventId']
        self._save_players_list(self.players)

        return message

    async def _notify_player(self):
        data = await self._fetch_live_paceman()
        current_players = [player["nickname"].lower() for player in data]
        players = list(self.players.keys())
        
        try:
            for player in players:
                if player in current_players:
                    current_stats = [item for item in data if item["nickname"].lower() == player][0]
                    message = await self._build_message(player, current_stats)
                    for subscriber_id in self.players[player]["subscriber_id"]:
                        if message:
                            await self.context.send_message(
                                subscriber_id, MessageChain(chain=[Comp.Plain(message)])
                            )
                        else:
                            logger.info(f"玩家 {player} 不需要通知")
                        await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"通知玩家失败: {e}")

    async def _check_live_paceman_periodically(self):
        try:
            while True:
                try:
                    await self._notify_player()
                except Exception as e:
                    logger.error(f"获取实时pace数据失败: {e}")
                    
                await asyncio.sleep(15)
        except Exception as e:
            logger.error(f"停止检查实时pace数据: {e}")
            


    # 生命周期方法
    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        self.task = asyncio.create_task(self._check_live_paceman_periodically())
        logger.info("实时pace插件已启动")

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        self._save_players_list(self.players)
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("实时pace插件已停止")
