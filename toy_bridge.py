# -*- coding: utf-8 -*-
"""
LLM with Toy - 本地 Bridge 桥接端
在你自己的电脑上运行，通过蓝牙连接体感设备，轮询云端 AstrBot 获取控制指令。

使用方法：
1. 修改下方【用户配置区】的参数（主要是蓝牙相关）
2. pip install bleak aiohttp
3. python toy_bridge.py（或双击"启动808桥接.bat"）

Made with ❤️ by 沈菀
"""

import asyncio
import aiohttp
from bleak import BleakClient
import logging
import sys
import time
import os
import json

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# ============================================================
#                    【用户配置区】
#     请根据你的实际情况修改以下参数
# ============================================================

# ---------- 蓝牙设备配置 ----------

# 设备蓝牙 MAC 地址（必填，格式如 "AA:BB:CC:DD:EE:FF"）
# 可通过手机蓝牙扫描工具（如 nRF Connect）查看你的设备 MAC 地址
MAC_ADDRESS = "AA:BB:CC:DD:EE:FF"

# BLE 请求特征 UUID（用于发送握手/初始化序列）
# 请查阅你的设备文档或使用 BLE 扫描工具获取
UUID_REQ = "00009001-0000-1000-8000-00805f9b34fb"

# BLE 命令特征 UUID（用于发送控制指令）
# 请查阅你的设备文档或使用 BLE 扫描工具获取
UUID_CMD = "00009002-0000-1000-8000-00805f9b34fb"

# ---------- 握手序列配置 ----------

# 设备连接后的初始化握手序列（不同设备可能不同）
# 如果你的设备不需要握手，可以将此列表清空：INIT_SEQUENCE = []
INIT_SEQUENCE = [
    bytes([0x05, 0x00]),
    bytes([0x08, 0x00]),
    bytes([0x07, 0x00]),
    bytes([0x09, 0x00]),
    bytes([0x01, 0x00]),
    bytes([0x03, 0x00]),
    bytes([0x02, 0x00]),
    bytes([0x04, 0x00])
]

# ---------- 强度映射配置 ----------

# 插件下发的强度范围是 0-100，你的设备实际接受的模式数量可能不同
# 以下函数将 0-100 的强度值映射为设备的模式编号（默认映射为 0-7 共 8 个模式）
# 如果你的设备直接接受 0-100 的数值，请自行修改此函数
def get_mode_from_intensity(val):
    """将 0-100 的强度值映射为设备模式编号"""
    if val <= 0:
        return None
    # 将 1-100 线性映射为 0-7（共 8 个模式）
    mode = int((val - 1) / 12.5)
    mode = max(0, min(7, mode))
    return mode

# ---------- 控制指令格式 ----------

# 构建发送给设备的蓝牙控制指令
# ⚠️ 不同品牌/型号的设备，指令格式完全不同！请根据你的设备协议修改
# 默认格式为 bytes([0xa0, 0x98, mode])，其中 mode 为上面映射出的模式编号
def build_command(mode):
    """根据模式编号构建蓝牙控制指令"""
    return bytes([0xa0, 0x98, mode])

# ============================================================
#              【以下为脚本逻辑，一般无需修改】
# ============================================================

# 服务器配置文件（运行时交互输入，自动保存）
CONFIG_FILE = "bridge_config.json"
SERVER_URL = "http://127.0.0.1:6013/808poll"

current_cmd = None
is_running = True
last_printed_mode = -1

current_ramp = {
    "head_from": 0.0, "head_to": 0.0,
    "tail_from": 0.0, "tail_to": 0.0,
    "duration": 0.0, "start_time": 0.0,
    "server_now": 0.0, "local_update_time": 0.0
}


def init_server_url():
    """交互式配置服务器地址，支持记忆上次输入"""
    global SERVER_URL
    saved_url = None
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                conf = json.load(f)
                if "server_url" in conf:
                    saved_url = conf["server_url"]
        except Exception:
            pass
            
    print("\n" + "=" * 50)
    print("  LLM with Toy - Bridge 桥接端")
    print("  Made with ❤️ by 沈菀")
    print("=" * 50)
    if saved_url:
        print(f"上次使用的服务器地址为: {saved_url}")
        print("【直接按回车】继续使用该地址，或输入新的 IP/域名 进行覆盖")
    else:
        print("请输入 AstrBot 所在服务器的 IP 地址或域名")
        print("（例如: 123.45.67.89，如果 AstrBot 就在本机运行，请直接按回车）")
    print("=" * 50)
    
    user_input = input(">> 服务器地址: ").strip()
    
    if user_input:
        if not user_input.startswith("http"):
            user_input = "http://" + user_input
        if ":" not in user_input.replace("http://", "").replace("https://", ""):
            user_input += ":6013"
        if not user_input.endswith("/808poll"):
            user_input = user_input.rstrip("/") + "/808poll"
        SERVER_URL = user_input
    elif saved_url:
        SERVER_URL = saved_url
        
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump({"server_url": SERVER_URL}, f, indent=4)
        print(f"[+] 当前连接的服务器地址已设为: {SERVER_URL}")
        print("=" * 50 + "\n")
    except Exception:
        pass


async def poll_server():
    """持续轮询云端获取最新控制指令"""
    global current_cmd, is_running, last_printed_mode
    async with aiohttp.ClientSession() as session:
        while is_running:
            try:
                async with session.get(SERVER_URL, timeout=5) as response:
                    if response.status == 200:
                        data = await response.json()
                        current_ramp["head_from"] = float(data.get('head_from', 0))
                        current_ramp["head_to"] = float(data.get('head_to', 0))
                        current_ramp["tail_from"] = float(data.get('tail_from', 0))
                        current_ramp["tail_to"] = float(data.get('tail_to', 0))
                        current_ramp["duration"] = float(data.get('duration', 0))
                        current_ramp["start_time"] = float(data.get('start_time', 0))
                        current_ramp["server_now"] = float(data.get('server_now', 0))
                        current_ramp["local_update_time"] = time.time()
            except Exception as e:
                logging.debug(f"Polling 失败: {e}")
            
            await asyncio.sleep(1)


async def keep_alive_task(client):
    """持续向蓝牙设备发送控制指令"""
    global current_cmd, is_running, last_printed_mode
    while is_running and client.is_connected:
        try:
            start_time = current_ramp["start_time"]
            if start_time > 0:
                # 计算已逝去的时间（规避云端与本地的时钟差）
                elapsed = (current_ramp["server_now"] - start_time) + (time.time() - current_ramp["local_update_time"])
                duration = current_ramp["duration"]
                
                if elapsed >= duration or duration <= 0:
                    head = current_ramp["head_to"]
                    tail = current_ramp["tail_to"]
                else:
                    ratio = elapsed / duration
                    head = current_ramp["head_from"] + (current_ramp["head_to"] - current_ramp["head_from"]) * ratio
                    tail = current_ramp["tail_from"] + (current_ramp["tail_to"] - current_ramp["tail_from"]) * ratio
                    
                max_val = max(head, tail)
                mode = get_mode_from_intensity(max_val)
                
                if mode is None:
                    current_cmd = None
                    if last_printed_mode != -1:
                        logging.info("目标强度为 0，断开蓝牙连接以触发停震...")
                        last_printed_mode = -1
                        await client.disconnect()
                        break
                else:
                    current_cmd = build_command(mode)
                    if mode != last_printed_mode:
                        logging.info(f"拉取到新强度: {max_val:.1f} -> 映射为模式: {' '.join(f'{b:02x}' for b in current_cmd)}")
                        last_printed_mode = mode
                        
            if current_cmd:
                await client.write_gatt_char(UUID_CMD, current_cmd, response=False)
        except Exception as e:
            logging.error(f"发送指令失败: {e}")
            break
        await asyncio.sleep(0.2)


async def run_bridge():
    """主循环：连接蓝牙设备 → 握手 → 通知云端 → 轮询控制"""
    global is_running, current_cmd
    logging.info(f"[*] 开始 Bridge，准备连接到设备 {MAC_ADDRESS} ...")
    
    if MAC_ADDRESS == "AA:BB:CC:DD:EE:FF":
        logging.error("❌ 请先在脚本顶部的【用户配置区】填写你的设备蓝牙 MAC 地址！")
        return
    
    while is_running:
        try:
            async with BleakClient(MAC_ADDRESS) as client:
                if not client.is_connected:
                    logging.error("[-] 连接失败，2秒后重试...")
                    await asyncio.sleep(2)
                    continue
                
                logging.info("[+] 蓝牙连接成功！")
                
                # 发送握手序列
                if INIT_SEQUENCE:
                    logging.info("[+] 开始发送初始化握手序列...")
                    for seq in INIT_SEQUENCE:
                        await client.write_gatt_char(UUID_REQ, seq, response=True)
                        await asyncio.sleep(0.1)
                
                # 通知云端 Bridge 已连接
                try:
                    async with aiohttp.ClientSession() as session:
                        notify_url = SERVER_URL.replace("/808poll", "/808bridge_connected")
                        async with session.post(notify_url, timeout=3) as response:
                            if response.status == 200:
                                res = await response.json()
                                if "msg" in res:
                                    logging.info(f"==> {res['msg']}")
                except Exception as e:
                    logging.debug(f"获取 AstrBot 通知反馈失败 (忽略): {e}")
                    
                logging.info("[+] 初始化完成！开始轮询云端指令...")
                
                poller = asyncio.create_task(poll_server())
                sender = asyncio.create_task(keep_alive_task(client))
                
                done, pending = await asyncio.wait(
                    [poller, sender],
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                for task in pending:
                    task.cancel()
                    
                logging.info("[-] 连接断开或任务出错，准备重新连接...")
                current_cmd = None
                await asyncio.sleep(2)
        except Exception as e:
            logging.error(f"蓝牙循环异常: {e}")
            await asyncio.sleep(2)


# 初始化服务器配置
init_server_url()

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    try:
        asyncio.run(run_bridge())
    except KeyboardInterrupt:
        is_running = False
        logging.info("用户中断，程序退出。")
