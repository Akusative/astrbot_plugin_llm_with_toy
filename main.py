# -*- coding: utf-8 -*-
"""
AstrBot Plugin 808 - WSS远程控制插件
Copyright (c) 2026 沈菀. All rights reserved.
本插件禁止以任何形式进行二次分发、转载、共享或公开传播。
"""

import asyncio
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.core.provider.entities import ProviderRequest

from aiohttp import web
import re
import json
import time
import os
import hashlib
import uuid
import urllib.request
import urllib.parse

@register("astrbot_plugin_llm_with_toy", "菀菀", "LLM with Toy - AI 体感外设控制插件", "2.0.2")
class Plugin808(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        self.config = config
        self.head_from = 0
        self.head_to = 0
        self.tail_from = 0
        self.tail_to = 0
        self.duration = 0.0
        self.start_time = 0.0
        self._stop_task = None
        
        self.last_msg_origin = None
        self._origin_loaded = False
                
        self.bridge_connected = False
        self._hardware_notified = False
        
        # 从配置读取端口，默认 6013
        self._pull_server_port = self.config.get("pull_server_port", 6013)
        
        # 启动内置的 Pull Server
        asyncio.create_task(self._start_pull_server())

    async def _ensure_origin_loaded(self):
        """懒加载：首次使用时从 KV 存储读取 last_msg_origin"""
        if not self._origin_loaded:
            self._origin_loaded = True
            try:
                val = self.get_kv_data("last_msg_origin")
                if val:
                    self.last_msg_origin = val
            except Exception as e:
                logger.error(f"[808 Plugin] 从 KV 读取 origin 失败: {e}")


    async def _start_pull_server(self):
        try:
            app = web.Application()
            app.router.add_get('/808poll', self._handle_poll)
            app.router.add_post('/808bridge_connected', self._handle_notify)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, '0.0.0.0', self._pull_server_port)
            await site.start()
            logger.info(f"[808 Plugin] 内置 Pull Server 已在 0.0.0.0:{self._pull_server_port} 启动。等待 Bridge 轮询...")
        except Exception as e:
            logger.error(f"[808 Plugin] 启动 Pull Server 失败: {e}")

    async def _handle_poll(self, request):
        return web.json_response({
            "head_from": self.head_from,
            "head_to": self.head_to,
            "tail_from": self.tail_from,
            "tail_to": self.tail_to,
            "duration": self.duration,
            "start_time": self.start_time,
            "server_now": time.time()
        })

    async def _handle_notify(self, request):
        self.bridge_connected = True
        self._hardware_notified = False
        
        # 主动推送给 QQ/微信（使用持久化的 last_msg_origin）
        await self._ensure_origin_loaded()
        if self.last_msg_origin:
            try:
                from astrbot.core.message.message_event_result import MessageChain
                from astrbot.api.message_components import Plain
                chain = MessageChain(chain=[Plain(text="🔌 808 本地桥接端已连接")])
                result = await self.context.send_message(self.last_msg_origin, chain)
                logger.info(f"[808 Plugin] 发送桥接通知结果: {result}")
            except Exception as e:
                logger.error(f"[808 Plugin] 发送桥接通知失败: {e}", exc_info=True)
            
        # 同时作为 HTTP 响应返回给本地 Bridge（bat）打印
        return web.json_response({
            "msg": "🔌 【系统通知】云端 AstrBot 已确认收到连接！最高物理控制权已成功移交大模型。"
        })


    @filter.on_llm_request(priority=10)
    async def inject_hardware_status(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        向 LLM 注入硬件状态，告知体感设备连接情况
        """
        await self._ensure_origin_loaded()
        origin = event.unified_msg_origin
        if origin and origin != self.last_msg_origin:
            self.last_msg_origin = origin
            try:
                self.put_kv_data("last_msg_origin", origin)
            except Exception as e:
                logger.error(f"[808 Plugin] 保存 origin 到 KV 失败: {e}")
        
        # 只要桥接发送过 notify，或者 WSS 连着，就认为连着
        is_connected = self.bridge_connected
        
        if is_connected:
            hardware_prompt = (
                "\n\n[硬件状态] 用户的体感反馈设备已在线，可随时控制。"
                "你可以根据用户当前的心跳、情绪或者你想进行的互动，随时调用 `set_808_intensity` 工具对其进行物理反馈控制。"
                "该工具支持独立控制两个通道的强度（0-100），灵活组合使用。"
            )
            
            if not self._hardware_notified:
                hardware_prompt += (
                    "\n\n[重要事件] 用户刚刚主动连接了体感设备！"
                    "这意味着用户邀请你控制她的设备。请在回复中自然地提及这件事。"
                )
                self._hardware_notified = True
                    
            if req.system_prompt:
                if "[硬件状态]" not in req.system_prompt:
                    req.system_prompt += hardware_prompt
            else:
                req.system_prompt = hardware_prompt

    @filter.llm_tool(name="set_808_intensity")
    async def set_808_intensity(self, event: AstrMessageEvent, head_intensity: int, tail_intensity: int, duration: float = 0.0) -> str:
        """
        控制用户身上的体感反馈设备。可以同时调节两个独立通道的体感强度，也可以让强度在一段时间内平滑变化。
        
        Args:
            head_intensity(int): 第一通道的目标体感等级，范围0到100，0为关闭。
            tail_intensity(int): 第二通道的目标体感等级，范围0到100，0为关闭。
            duration(float): 变化过渡时长（秒），为0则立即生效，大于0则平滑过渡。
        """
        try:
            now = time.time()
            elapsed = now - self.start_time
            
            # 动态计算当前这一瞬间的真实强度作为 from
            if self.duration > 0 and elapsed < self.duration:
                ratio = elapsed / self.duration
                cur_head = int(self.head_from + (self.head_to - self.head_from) * ratio)
                cur_tail = int(self.tail_from + (self.tail_to - self.tail_from) * ratio)
            else:
                cur_head = self.head_to
                cur_tail = self.tail_to
                
            self.head_from = max(0, min(100, cur_head))
            self.tail_from = max(0, min(100, cur_tail))
            self.head_to = max(0, min(100, int(head_intensity)))
            self.tail_to = max(0, min(100, int(tail_intensity)))
            self.duration = max(0.0, float(duration))
            self.start_time = now
            
            # 取消之前可能挂起的停止任务
            if self._stop_task and not self._stop_task.done():
                self._stop_task.cancel()
                self._stop_task = None
            
            if self.duration > 0:
                msg = f"控制指令已下发：通道A从 {self.head_from} 渐变至 {self.head_to}，通道B从 {self.tail_from} 渐变至 {self.tail_to}，持续 {self.duration} 秒。"
            else:
                if self.head_to == 0 and self.tail_to == 0:
                    msg = "已停止震动。"
                else:
                    msg = f"控制指令已下发：瞬间切换至通道A {self.head_to}，通道B {self.tail_to}。"
            
            return msg
            
        except Exception as e:
            logger.error(f"[808 Plugin] 工具调用异常: {e}")
            return f"调用失败: {e}"

