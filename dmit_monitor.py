#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DMIT LAX.AS3.Pro.TINY 自动库存监控脚本（云端实测验证版）
--------------------------------------------------------
- 监控目标：DMIT 洛杉矶 Premium AS3 Pro TINY (PID=253)
- 推送目标：iPhone (Bark App，点击通知直接加购进入结算页)
- 防风控：使用 curl_cffi 原生 Safari TLS/HTTP2 指纹穿透 Cloudflare
- 混合检测：
    1) 高频轻量检测 (17KB)：每 20 秒直查 cart.php?a=add&pid=253，省流量且极速
    2) 目录校验同步：启动时及每 15 轮校验商品列表页 .cart-products-box，防止官方更换 PID
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    from curl_cffi import requests as cf_requests
    USE_CURL_CFFI = True
except ImportError:
    import requests as std_requests
    USE_CURL_CFFI = False

from bs4 import BeautifulSoup

# ==================== 配置读取（优先从云平台环境变量读取） ====================
BARK_KEY = os.environ.get("BARK_KEY", "").strip()
BARK_SERVER = os.environ.get("BARK_SERVER", "https://api.day.app").strip()

TARGET_PRODUCT = os.environ.get("TARGET_PRODUCT", "LAX.AS3.Pro.TINY").strip()
DEFAULT_PID = os.environ.get("TARGET_PID", "253").strip()

DMIT_CATALOG_URL = (
    "https://www.dmit.io/cart.php?region=los-angeles&network=premium&generation=as3"
)
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "20"))
ALERT_REPEAT_TIMES = int(os.environ.get("ALERT_REPEAT_TIMES", "3"))
PORT = int(os.environ.get("PORT", "8080"))
# ==============================================================================

IMPERSONATE_PROFILES = ["safari17_0", "safari17_2_ios", "safari15_5", "chrome131"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("DMIT-Monitor")

# 运行态状态（供 /health 接口实时查看云端是否被 403 或正常运行）
STATE = {
    "status": "starting",
    "target": TARGET_PRODUCT,
    "current_pid": DEFAULT_PID,
    "impersonate": IMPERSONATE_PROFILES[0] if USE_CURL_CFFI else "requests",
    "last_check_utc": None,
    "last_result": None,
    "in_stock": False,
    "total_checks": 0,
    "consecutive_errors": 0,
    "bark_configured": bool(BARK_KEY),
}


def direct_add_url(pid: str) -> str:
    return f"https://www.dmit.io/cart.php?a=add&pid={pid}"


class HealthCheckHandler(BaseHTTPRequestHandler):
    """轻量健康检查接口，供云平台存活探针与防休眠定时任务调用"""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(STATE, ensure_ascii=False).encode("utf-8"))

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def start_health_server(port: int):
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        logger.info(f"🌐 健康检查端口已启动: http://0.0.0.0:{port}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"健康检查服务启动失败: {e}")


def send_bark_notification(title: str, body: str, target_url: str):
    """通过 Bark 推送强提醒通知到 iPhone"""
    if not BARK_KEY:
        logger.warning("⚠️ 未配置 BARK_KEY，跳过推送。请在环境变量中设置 BARK_KEY。")
        return False

    push_url = f"{BARK_SERVER.rstrip('/')}/{BARK_KEY}"
    payload = {
        "title": title,
        "body": body,
        "url": target_url,
        "sound": "alarm.caf",
        "level": "timeSensitive",
        "group": "DMIT库存监控",
        "icon": "https://www.dmit.io/favicon.ico",
    }

    try:
        if USE_CURL_CFFI:
            resp = cf_requests.post(push_url, json=payload, timeout=10)
        else:
            resp = std_requests.post(push_url, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info("📲 Bark 推送成功！已发送至 iPhone")
            return True
        logger.error(f"Bark 推送失败，HTTP {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Bark 发送异常: {e}")
    return False


def fetch_url(url: str) -> tuple[int, str, str]:
    """
    请求 DMIT 页面，自动轮换可用的浏览器 TLS 指纹（不覆盖 User-Agent 以保证指纹一致）
    返回: (status_code, final_url, html_text)
    """
    if not USE_CURL_CFFI:
        resp = std_requests.get(url, timeout=20, allow_redirects=True)
        return resp.status_code, resp.url, resp.text

    # 优先使用上次成功的指纹
    preferred = STATE["impersonate"]
    profiles = [preferred] + [p for p in IMPERSONATE_PROFILES if p != preferred]

    last_status = 0
    for prof in profiles:
        try:
            resp = cf_requests.get(
                url,
                impersonate=prof,
                timeout=20,
                allow_redirects=True,
            )
            last_status = resp.status_code
            if resp.status_code == 200 and "Attention Required! | Cloudflare" not in resp.text[:1500]:
                if STATE["impersonate"] != prof:
                    logger.info(f"🔄 切换可用 TLS 指纹为: {prof}")
                    STATE["impersonate"] = prof
                return resp.status_code, str(resp.url), resp.text
        except Exception as e:
            logger.warning(f"指纹 {prof} 请求异常: {e}")

    return last_status, url, ""


def check_catalog_and_sync_pid() -> tuple[bool | None, str]:
    """
    从商品目录页精确定位 .cart-products-box，校验库存并同步最新 PID
    返回: (in_stock 或 None 表示请求失败, 说明文本)
    """
    status, _, html = fetch_url(DMIT_CATALOG_URL)
    if status != 200 or not html:
        return None, f"目录页请求失败 (HTTP {status})"

    soup = BeautifulSoup(html, "html.parser")
    box = None
    for title_div in soup.select(".cart-products-title"):
        if title_div.get_text(strip=True) == TARGET_PRODUCT:
            box = title_div.find_parent(class_="cart-products-box")
            break

    if box is None:
        return None, f"目录页未找到商品卡片 [{TARGET_PRODUCT}]"

    pid = (box.get("pid") or "").strip()
    if pid and pid != STATE["current_pid"]:
        logger.info(f"🔄 检测到 {TARGET_PRODUCT} PID 更新: {STATE['current_pid']} -> {pid}")
        STATE["current_pid"] = pid

    classes = box.get("class") or []
    qty_el = box.select_one(".cart-products-qty")
    qty_text = qty_el.get_text(strip=True) if qty_el else ""

    if "none-stock" in classes or "out of stock" in qty_text.lower() or "缺货" in qty_text:
        return False, f"目录页确认缺货 (PID={STATE['current_pid']}, {qty_text or 'none-stock'})"

    return True, f"🎉 目录页检测到有货！(PID={STATE['current_pid']})"


def check_by_pid(pid: str) -> tuple[bool | None, str]:
    """
    轻量直查 cart.php?a=add&pid=<pid>（缺货时仅 17KB，有货时 302 跳转至 confproduct）
    返回: (in_stock 或 None 表示请求失败, 说明文本)
    """
    url = direct_add_url(pid)
    status, final_url, html = fetch_url(url)
    if status != 200 or not html:
        return None, f"直达页请求失败 (HTTP {status})"

    lower_html = html.lower()
    if "out of stock" in lower_html or "缺货" in lower_html:
        return False, f"缺货中 (PID={pid})"

    if "confproduct" in final_url or "billing cycle" in lower_html or "configure" in lower_html:
        return True, f"🎉 直达加购页已开放！(PID={pid} -> {final_url})"

    # 状态不明时回退目录页二次确认，杜绝误报
    return check_catalog_and_sync_pid()


def run_monitor():
    logger.info("=" * 60)
    logger.info("🚀 DMIT 云端库存监控已启动！")
    logger.info(f"🎯 监控目标: {TARGET_PRODUCT} (初始 PID={STATE['current_pid']})")
    logger.info(f"⏱️ 轮询频率: 每 {CHECK_INTERVAL} 秒一次")
    logger.info(f"🔑 BARK_KEY: {'已配置 ✅' if BARK_KEY else '未配置 ❌'}")
    logger.info("=" * 60)

    if BARK_KEY:
        send_bark_notification(
            title="☁️ DMIT 云端监控已上线",
            body=f"正在每 {CHECK_INTERVAL} 秒盯防 {TARGET_PRODUCT}，补货将立即拉响警报，点通知直达结算！",
            target_url=direct_add_url(STATE["current_pid"]),
        )

    STATE["status"] = "running"
    cycle = 0

    while True:
        try:
            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            now_hms = datetime.now().strftime("%H:%M:%S")

            # 启动首轮及每 15 轮（约 5 分钟）同步一次目录页，其余轮次走 17KB 极速 PID 检测
            if cycle % 15 == 0:
                in_stock, msg = check_catalog_and_sync_pid()
            else:
                in_stock, msg = check_by_pid(STATE["current_pid"])

            cycle += 1
            STATE["total_checks"] += 1
            STATE["last_check_utc"] = now_utc
            STATE["last_result"] = msg
            STATE["bark_configured"] = bool(BARK_KEY)

            if in_stock is None:
                STATE["consecutive_errors"] += 1
                logger.warning(f"[{now_hms}] ⚠️ {msg} (连续失败 {STATE['consecutive_errors']} 次)")
            elif in_stock:
                STATE["in_stock"] = True
                STATE["consecutive_errors"] = 0
                buy_url = direct_add_url(STATE["current_pid"])
                logger.warning(f"🚨🚨🚨【补货警报】{TARGET_PRODUCT} 有货了！{msg}")

                for i in range(ALERT_REPEAT_TIMES):
                    send_bark_notification(
                        title=f"🚨 补货啦！DMIT {TARGET_PRODUCT}",
                        body=f"[{now_hms}] 已开放加购！点击本通知直接加入购物车抢购！",
                        target_url=buy_url,
                    )
                    if i + 1 < ALERT_REPEAT_TIMES:
                        time.sleep(10)

                logger.info("已完成连续报警，冷却 300 秒避免重复刷屏...")
                time.sleep(300)
            else:
                STATE["in_stock"] = False
                STATE["consecutive_errors"] = 0
                logger.info(f"[{now_hms}] {msg}")

        except KeyboardInterrupt:
            logger.info("手动停止监控。")
            sys.exit(0)
        except Exception as e:
            STATE["consecutive_errors"] += 1
            logger.error(f"监控循环异常: {e}")

        time.sleep(CHECK_INTERVAL)


def run_once() -> int:
    """单次检测模式（供 GitHub Actions 保活与双保险备份检测使用）"""
    in_stock, msg = check_by_pid(STATE["current_pid"])
    logger.info(f"[单次检测] {msg}")
    if in_stock:
        buy_url = direct_add_url(STATE["current_pid"])
        send_bark_notification(
            title=f"🚨 补货啦！DMIT {TARGET_PRODUCT}",
            body="GitHub 云端备份巡检发现已开放加购！点击直接加入购物车抢购！",
            target_url=buy_url,
        )
    return 0


if __name__ == "__main__":
    if "--once" in sys.argv:
        sys.exit(run_once())
    t = threading.Thread(target=start_health_server, args=(PORT,), daemon=True)
    t.start()
    run_monitor()
