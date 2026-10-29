#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DMIT LAX.AS3.Pro.TINY 自动库存监控脚本（云端托管优化版）
------------------------------------------------------
- 监控目标：DMIT 洛杉矶 Premium AS3 Pro TINY 套餐
- 推送目标：iPhone (Bark App)
- 云端适配：内置健康检查 HTTP 端口，完美适配 Zeabur / Render / Koyeb / Docker 24小时托管
"""

import os
import sys
import time
import logging
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# 优先使用 curl_cffi 模拟 Chrome TLS 指纹穿透 Cloudflare
try:
    from curl_cffi import requests
    USE_CURL_CFFI = True
except ImportError:
    import requests
    USE_CURL_CFFI = False

from bs4 import BeautifulSoup

# ==================== 配置读取（优先从云平台环境变量读取） ====================
# 1. 你的 iPhone Bark Key（在云平台配置环境变量 BARK_KEY，或在此处填入默认值）
BARK_KEY = os.environ.get("BARK_KEY", "").strip()

# 2. 备用 Bark 服务器
BARK_SERVER = os.environ.get("BARK_SERVER", "https://api.day.app").strip()

# 3. 监控的目标套餐
TARGET_PRODUCT = os.environ.get("TARGET_PRODUCT", "LAX.AS3.Pro.TINY").strip()

# 4. DMIT 订购页面
DMIT_CART_URL = "https://www.dmit.io/cart.php?region=los-angeles&network=premium&generation=as3"

# 5. 检测轮询间隔（秒），建议 20 秒
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "20"))

# 6. 发现有货后的连续提醒次数
ALERT_REPEAT_TIMES = int(os.environ.get("ALERT_REPEAT_TIMES", "3"))

# 7. 云平台监听端口（如 Render/Zeabur 自动分配的 PORT）
PORT = int(os.environ.get("PORT", "8080"))
# ==============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("DMIT-Monitor")


class HealthCheckHandler(BaseHTTPRequestHandler):
    """提供轻量 HTTP 接口，让云平台（Render/Zeabur等）检测服务存活，防止休眠"""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        response_body = (
            f'{{"status":"running","target":"{TARGET_PRODUCT}",'
            f'"bark_configured":{str(bool(BARK_KEY)).lower()}}}'
        )
        self.wfile.write(response_body.encode("utf-8"))

    def log_message(self, format, *args):
        pass  # 忽略常规 http 日志


def start_health_server(port: int):
    """后台线程启动健康检查服务器"""
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        logger.info(f"🌐 云端健康检查端口已启动: http://0.0.0.0:{port}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"健康检查服务启动失败: {e}")


def send_bark_notification(title: str, body: str, target_url: str = DMIT_CART_URL):
    """推送通知到 iPhone"""
    if not BARK_KEY:
        logger.warning("⚠️ 未配置 BARK_KEY，跳过通知发送！请在环境变量中设置 BARK_KEY。")
        return False

    push_url = f"{BARK_SERVER.rstrip('/')}/{BARK_KEY}"
    payload = {
        "title": title,
        "body": body,
        "url": target_url,               # 点击通知直接打开 DMIT 购买页
        "sound": "alarm.caf",             # 警报铃声
        "level": "timeSensitive",         # 时效性通知，突破勿扰模式
        "group": "DMIT库存监控",
        "icon": "https://www.dmit.io/favicon.ico"
    }

    try:
        resp = requests.post(push_url, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info("📲 Bark 推送成功！已发送至 iPhone")
            return True
        else:
            logger.error(f"Bark 推送失败，HTTP {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Bark 发送异常: {e}")
    return False


def get_html_content(url: str) -> str:
    """获取目标页面 HTML"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8",
    }

    if USE_CURL_CFFI:
        resp = requests.get(url, headers=headers, impersonate="chrome124", timeout=15)
    else:
        resp = requests.get(url, headers=headers, timeout=15)

    if resp.status_code != 200:
        logger.warning(f"请求失败，HTTP 状态码: {resp.status_code}")
        return ""

    return resp.text


def check_stock_status(html: str) -> tuple[bool, str]:
    """检测目标套餐库存"""
    if not html:
        return False, "页面内容为空"

    if TARGET_PRODUCT not in html:
        return False, f"页面未包含目标套餐 [{TARGET_PRODUCT}]"

    soup = BeautifulSoup(html, "html.parser")

    target_node = None
    for text_item in soup.find_all(string=True):
        if TARGET_PRODUCT in text_item:
            target_node = text_item
            break

    if not target_node:
        return False, "未定位到套餐卡片"

    # 向上寻找商品卡片容器
    card = target_node.parent
    for _ in range(6):
        if card and card.parent:
            card = card.parent
            card_text = card.get_text()

            if "Out of Stock" in card_text or "缺货" in card_text:
                return False, "缺货中 (Out of Stock)"

            if ("Order Now" in card_text or "Configure" in card_text or
                "立即订购" in card_text or "cart.php?a=add" in str(card)):
                return True, "🎉 发现有货！存在可订购按钮！"

    card_text = card.get_text() if card else ""
    if "Out of Stock" not in card_text:
        return True, "🎉 状态变动！未检测到缺货标识！"

    return False, "缺货中 (Out of Stock)"


def run_monitor():
    logger.info("=" * 60)
    logger.info("🚀 DMIT 云端库存监控已启动！")
    logger.info(f"🎯 监控目标: {TARGET_PRODUCT}")
    logger.info(f"⏱️ 轮询频率: 每 {CHECK_INTERVAL} 秒一次")
    logger.info(f"🔑 BARK_KEY 状态: {'已配置 ✅' if BARK_KEY else '未配置 ❌ (请设置环境变量 BARK_KEY)'}")
    logger.info(f"🛡️ 驱动引擎: {'curl_cffi (Chrome TLS 指纹)' if USE_CURL_CFFI else 'requests'}")
    logger.info("=" * 60)

    # 发送启动握手消息
    if BARK_KEY:
        send_bark_notification(
            title="☁️ DMIT 云端监控已上线",
            body=f"监控目标: {TARGET_PRODUCT}，轮询间隔: {CHECK_INTERVAL}秒。一旦补货将第一时间报警！"
        )

    consecutive_errors = 0

    while True:
        try:
            now_str = datetime.now().strftime("%H:%M:%S")
            html = get_html_content(DMIT_CART_URL)
            in_stock, msg = check_stock_status(html)

            if in_stock:
                logger.warning(f"🚨🚨🚨【重要警报】{TARGET_PRODUCT} 补货了！！！")
                # 连续推送提醒
                for _ in range(ALERT_REPEAT_TIMES):
                    send_bark_notification(
                        title=f"🚨 紧急补货！DMIT {TARGET_PRODUCT}",
                        body=f"[{now_str}] 监测到已有库存！手慢无，点击本通知立即前往官网抢购！"
                    )
                    time.sleep(10)
                # 冷却 300 秒避免刷屏
                logger.info("报警完成，进入 5 分钟冷却期...")
                time.sleep(300)
            else:
                logger.info(f"[{now_str}] 状态: {msg}")
                consecutive_errors = 0

        except Exception as e:
            consecutive_errors += 1
            logger.error(f"检测异常: {e}")
            if consecutive_errors >= 10:
                logger.critical("连续 10 次发生错误，请检查网络或 Cloudflare 状态")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    # 在独立后台线程启动 Web 健康检查服务
    t = threading.Thread(target=start_health_server, args=(PORT,), daemon=True)
    t.start()

    # 主线程运行监控逻辑
    run_monitor()
