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

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

    @filter.command("livepacesub")
    async def livePacemanSub(self, event: AstrMessageEvent, player_name: str, room_id: str):
        if not self._check_player_exists(player_name):
            yield event.plain_result(f"玩家 {player_name} 不存在，请输入正确的玩家名。")
            return

        subscriber_id = event.unified_msg_origin
        formatted_player_name = self._normalize_player_name(player_name)
        
        if formatted_player_name not in self.players:
            self.players.setdefault(formatted_player_name, {
                "subscriber_id": [subscriber_id],
                "room_id": room_id,
                "player_name": player_name
            })
            yield event.plain_result(f"玩家 {player_name} 订阅成功，房间ID为 {room_id}。")
        else:
            self.players[formatted_player_name]["subscriber_id"].append(subscriber_id)
            self.players[formatted_player_name]["room_id"] = room_id
            self.players[formatted_player_name]["player_name"] = player_name
            yield event.plain_result(f"玩家 {player_name} 订阅更新，房间ID为 {room_id}。")

        self._save_players_list(self.players)

    @filter.command("livepacesublist")
    async def livePacemanSubList(self, event: AstrMessageEvent):
        subscriber_id = event.unified_msg_origin
        players = self.players.get(subscriber_id, [])
        if not players:
            yield event.plain_result("你还没有订阅任何玩家。")
            return
        yield event.plain_result(f"你订阅了以下玩家：{', '.join([player['player_name'] for player in players])}")

    async def _fetch_live_paceman(self):
        async with httpx.AsyncClient() as client:
            response = await client.get("https://paceman.gg/api/ars/liveruns")
            return response.json()

    def _format_time(self, time: int):
        # 将6位数毫秒级时间转换为分钟:秒.毫秒格式
        minutes = time // 60000
        seconds = (time % 60000) // 1000
        milliseconds = (time - minutes * 60000 - seconds * 1000)
        return f"{minutes}:{seconds:02d}.{milliseconds:03d}"

    async def _build_message(self, player_name: str, current_stats: dict):
        current_event = current_stats['eventList'][-1]
        rta = self._format_time(current_event['rta'])
        igt = self._format_time(current_event['igt'])
        match current_event['eventId']:
            case 'rsg.first_portal':
                event_name = "盲传"
            case 'rsg.enter_stronghold':
                event_name = "进要塞"
            case 'rsg.enter_end':
                event_name = "进末地"
            case 'rsg.credits':
                event_name = "已通关"
            case _:
                event_name = "未知事件"
        message = (f"{player_name} 当前实时pace:\n"
                   f"游戏版本: {current_stats['gameVersion']}\n"
                   f"时间: {rta} / {igt}\n"
                   f"事件: {event_name}")
        return message

    async def _notify_player(self):
        data = await self._fetch_live_paceman()
        current_players = [player["nickname"] for player in data]
        players = list(self.players.keys())
        
        try:
            for player in players:
                if player in current_players:
                    current_stats = [item for item in data if item["nickname"] == player][0]
                    message = await self._build_message(player, current_stats)
                    for subscriber_id in self.players[player]["subscriber_id"]:
                        await self.context.send_message(
                            subscriber_id, MessageChain(chain=[Comp.Plain(message)])
                        )
                        await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"通知玩家失败: {e}")

    async def _check_live_paceman_periodically(self, event: AstrMessageEvent):
        try:
            while True:
                try:
                    await self._notify_player()
                except Exception as e:
                    logger.error(f"获取实时pace数据失败: {e}")
                    
                await asyncio.sleep(15)
        except Exception as e:
            logger.error(f"停止检查实时pace数据: {e}")
            

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
