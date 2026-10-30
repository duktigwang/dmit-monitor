#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DMIT LAX.AS3.Pro.TINY 全天候三通道云端监控系统
------------------------------------------------
通道 1 [主力实时]：HostMonit 实时库存引擎 JSON API（毫秒级响应，无视云机房 IP Cloudflare 403）
通道 2 [官网直连]：DMIT 官网直达加购页检测（非受限 IP 自动启用，受限云 IP 自动静默降级）
通道 3 [社区舆情]：DMIT 官方 Telegram (@dmitnews) + NodeSeek 论坛新帖 RSS 实时补货情报监听
推送端：iPhone Bark App（仅真实补货时触发 timeSensitive + alarm 强警报，点击直达加购结算页）
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    from curl_cffi import requests as cf_requests
    USE_CURL_CFFI = True
except ImportError:
    import requests as std_requests
    USE_CURL_CFFI = False

from bs4 import BeautifulSoup

# ==================== 配置读取（支持环境变量覆盖） ====================
BARK_KEY = os.environ.get("BARK_KEY", "").strip()
BARK_SERVER = os.environ.get("BARK_SERVER", "https://api.day.app").strip()

TARGET_PRODUCT = os.environ.get("TARGET_PRODUCT", "LAX.AS3.Pro.TINY").strip()
DEFAULT_PID = os.environ.get("TARGET_PID", "253").strip()

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "20"))
ALERT_REPEAT_TIMES = int(os.environ.get("ALERT_REPEAT_TIMES", "3"))
SEND_STARTUP_NOTICE = os.environ.get("SEND_STARTUP_NOTICE", "0").strip() == "1"
PORT = int(os.environ.get("PORT", "8080"))

HOSTMONIT_SEARCH_API = "https://backend.stock.hostmonit.com/api/product/search"
TG_DMITNEWS_URL = "https://t.me/s/dmitnews"
NODESEEK_RSS_URL = "https://www.nodeseek.com/rss.xml"
# =====================================================================

IMPERSONATE_PROFILES = ["safari17_0", "safari17_2_ios", "chrome131"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("DMIT-Monitor")

STATE = {
    "status": "starting",
    "target": TARGET_PRODUCT,
    "current_pid": DEFAULT_PID,
    "active_channels": ["hostmonit_api", "dmit_official", "tg_dmitnews", "nodeseek_rss"],
    "dmit_direct_available": None,
    "last_check_utc": None,
    "last_result": None,
    "last_stock_change_time": None,
    "in_stock": False,
    "total_checks": 0,
    "consecutive_errors": 0,
    "bark_configured": bool(BARK_KEY),
}

SEEN_TG_POSTS: set[str] = set()
SEEN_NS_GUIDS: set[str] = set()
COMMUNITY_INITIALIZED = False


def direct_add_url(pid: str) -> str:
    return f"https://www.dmit.io/cart.php?a=add&pid={pid}"


class HealthCheckHandler(BaseHTTPRequestHandler):
    """健康检查与实时运行面板 JSON 接口"""

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
        logger.info(f"🌐 健康检查服务已启动: http://0.0.0.0:{port}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"健康检查服务启动失败: {e}")


def http_get(url: str, timeout: int = 15) -> tuple[int, str, str]:
    if USE_CURL_CFFI:
        for prof in IMPERSONATE_PROFILES:
            try:
                r = cf_requests.get(url, impersonate=prof, timeout=timeout, allow_redirects=True)
                if r.status_code == 200 and "Attention Required! | Cloudflare" not in r.text[:1500]:
                    return r.status_code, str(r.url), r.text
                if r.status_code == 403:
                    return 403, str(r.url), r.text
            except Exception:
                continue
        return 0, url, ""
    r = std_requests.get(url, timeout=timeout, allow_redirects=True)
    return r.status_code, r.url, r.text


def http_post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 15) -> tuple[int, dict]:
    hdrs = headers or {}
    if USE_CURL_CFFI:
        r = cf_requests.post(url, json=payload, headers=hdrs, impersonate="safari17_0", timeout=timeout)
    else:
        r = std_requests.post(url, json=payload, headers=hdrs, timeout=timeout)
    if r.status_code == 200:
        return 200, r.json()
    return r.status_code, {}


def send_bark_notification(title: str, body: str, target_url: str, urgent: bool = True) -> bool:
    """推送通知到 iPhone（urgent=True 启用警报音与穿透勿扰模式；普通通知使用默认轻提示音）"""
    if not BARK_KEY:
        logger.warning("⚠️ 未配置 BARK_KEY，跳过推送。")
        return False

    push_url = f"{BARK_SERVER.rstrip('/')}/{BARK_KEY}"
    payload = {
        "title": title,
        "body": body,
        "url": target_url,
        "group": "DMIT库存监控",
        "icon": "https://www.dmit.io/favicon.ico",
    }
    if urgent:
        payload["sound"] = "alarm.caf"
        payload["level"] = "timeSensitive"
    else:
        payload["level"] = "active"

    try:
        if USE_CURL_CFFI:
            resp = cf_requests.post(push_url, json=payload, timeout=10)
        else:
            resp = std_requests.post(push_url, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info(f"📲 Bark 推送成功: {title}")
            return True
        logger.error(f"Bark 推送失败 HTTP {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Bark 推送异常: {e}")
    return False


def check_hostmonit_api() -> tuple[bool | None, str]:
    """通道 1：通过 HostMonit 实时库存接口查询目标套餐状态"""
    try:
        status, data = http_post_json(
            HOSTMONIT_SEARCH_API,
            payload={
                "provider": "dmit",
                "pageNo": 1,
                "pageSize": 24,
                "keyword": TARGET_PRODUCT,
                "tags": [],
                "region": "",
                "sortBy": "default",
                "levelFilter": "all",
            },
            headers={
                "Origin": "https://stock.hostmonit.com",
                "Referer": "https://stock.hostmonit.com/dmit",
            },
        )
        if status != 200 or not data:
            return None, f"HostMonit API 响应异常 (HTTP {status})"

        items = data.get("content") or []
        target_item = None
        for it in items:
            if (it.get("title") or "").strip().upper() == TARGET_PRODUCT.upper():
                target_item = it
                break
        if target_item is None and items:
            target_item = items[0]
        if target_item is None:
            return None, f"HostMonit 未查询到套餐 [{TARGET_PRODUCT}]"

        # 自动同步最新 PID
        prod_url = target_item.get("productUrl") or ""
        m = re.search(r"pid=(\d+)", prod_url)
        if m and m.group(1) != STATE["current_pid"]:
            logger.info(f"🔄 自动更新 PID: {STATE['current_pid']} -> {m.group(1)}")
            STATE["current_pid"] = m.group(1)

        mon_time = target_item.get("monitorTime")
        STATE["last_stock_change_time"] = mon_time
        mon_status = int(target_item.get("monitorStatus") or 0)
        avail_raw = str(target_item.get("availableNum") or "0").strip()
        avail_num = int(avail_raw) if avail_raw.isdigit() else 0

        if mon_status == 1 or avail_num > 0:
            return True, f"🎉 HostMonit 检测到有货！(PID={STATE['current_pid']}, status={mon_status}, avail={avail_raw})"

        return False, f"缺货中 [HostMonit 实时源] (PID={STATE['current_pid']}, 上次变动: {mon_time})"
    except Exception as e:
        return None, f"HostMonit 检测异常: {e}"


def check_dmit_direct(pid: str) -> tuple[bool | None, str]:
    """通道 2：直查 DMIT 官网（若当前云机房 IP 被 Cloudflare 拦截则返回 None 交给通道 1）"""
    url = direct_add_url(pid)
    status, final_url, html = http_get(url, timeout=15)
    if status == 403:
        STATE["dmit_direct_available"] = False
        return None, "DMIT 官网对当前云机房 IP 开启了 Cloudflare 质询，已自动使用 HostMonit + 社区多源通道"
    if status != 200 or not html:
        return None, f"DMIT 直连失败 (HTTP {status})"

    STATE["dmit_direct_available"] = True
    lower_html = html.lower()
    if "out of stock" in lower_html or "缺货" in lower_html:
        return False, f"缺货中 [DMIT 官网直连] (PID={pid})"
    if "confproduct" in final_url or "billing cycle" in lower_html or "configure" in lower_html:
        return True, f"🎉 DMIT 官网加购页已开放！(PID={pid})"
    return None, "DMIT 页面状态未知"


def check_community_feeds() -> list[tuple[str, str]]:
    """
    通道 3：监听 DMIT 官方 TG 频道 (@dmitnews) 与 NodeSeek 社区最新帖
    返回新发现的补货情报列表: [(标题, 内容摘要), ...]
    """
    global COMMUNITY_INITIALIZED
    alerts: list[tuple[str, str]] = []

    # 3A. DMIT 官方 TG 公告频道 (@dmitnews)
    try:
        st, _, html = http_get(TG_DMITNEWS_URL, timeout=15)
        if st == 200 and html:
            soup = BeautifulSoup(html, "html.parser")
            for msg_div in soup.select(".tgme_widget_message"):
                post_id = msg_div.get("data-post") or ""
                if not post_id or post_id in SEEN_TG_POSTS:
                    continue
                SEEN_TG_POSTS.add(post_id)
                if not COMMUNITY_INITIALIZED:
                    continue
                txt_el = msg_div.select_one(".tgme_widget_message_text")
                text = txt_el.get_text(" ", strip=True) if txt_el else ""
                low = text.lower()
                if any(k in low for k in ["as3.pro", "pro.tiny", "lax.as3", "restock", "补货", "放货"]):
                    alerts.append(("📢 DMIT 官方频道新动态", text[:160]))
    except Exception as e:
        logger.debug(f"TG 频道巡检略过: {e}")

    # 3B. NodeSeek 论坛新帖 RSS
    try:
        st, _, xml_text = http_get(NODESEEK_RSS_URL, timeout=15)
        if st == 200 and xml_text:
            root = ET.fromstring(xml_text.encode("utf-8"))
            for item in root.findall("./channel/item"):
                guid = (item.findtext("guid") or item.findtext("link") or "").strip()
                title = (item.findtext("title") or "").strip()
                if not guid or guid in SEEN_NS_GUIDS:
                    continue
                SEEN_NS_GUIDS.add(guid)
                if not COMMUNITY_INITIALIZED:
                    continue
                low_t = title.lower()
                is_dmit = ("dmit" in low_t) or ("大妈" in low_t)
                is_target = any(k in low_t for k in ["as3", "tiny", "lax.pro", "pro.tiny"])
                is_restock = any(k in low_t for k in ["补货", "放货", "有货", "上新", "开售", "restock"])
                is_trade = any(k in low_t for k in ["收", "出", "溢价", "求", "转让", "换"])
                if is_dmit and is_target and is_restock and not is_trade:
                    alerts.append(("📢 NodeSeek 社区补货快讯", f"新帖：{title}"))
    except Exception as e:
        logger.debug(f"NodeSeek RSS 巡检略过: {e}")

    COMMUNITY_INITIALIZED = True
    return alerts


def single_check_cycle(cycle: int) -> tuple[bool | None, str]:
    """执行一轮综合库存检测"""
    # 若尚未探测或直连可用，或每 90 轮（约 30 分钟）探测一次官网直连
    if STATE["dmit_direct_available"] is not False or cycle % 90 == 0:
        direct_res, direct_msg = check_dmit_direct(STATE["current_pid"])
        if direct_res is not None:
            return direct_res, direct_msg

    # 主力云端无感检测：HostMonit 实时库存 API
    return check_hostmonit_api()


def run_once() -> int:
    in_stock, msg = single_check_cycle(1)
    logger.info(f"[单次巡检] {msg}")
    if in_stock:
        buy_url = direct_add_url(STATE["current_pid"])
        send_bark_notification(
            title=f"🚨 补货啦！DMIT {TARGET_PRODUCT}",
            body="云端巡检发现已开放加购！点击本通知直接加入购物车抢购！",
            target_url=buy_url,
            urgent=True,
        )
    return 0


def run_monitor():
    logger.info("=" * 64)
    logger.info("🚀 DMIT 三通道云端监控系统已启动！")
    logger.info(f"🎯 目标套餐: {TARGET_PRODUCT} (PID={STATE['current_pid']})")
    logger.info(f"⏱️ 轮询间隔: 每 {CHECK_INTERVAL} 秒一次（社区舆情每 60 秒一次）")
    logger.info(f"🔑 Bark 推送: {'已就绪 ✅' if BARK_KEY else '未配置 ❌'}")
    logger.info("=" * 64)

    if BARK_KEY and SEND_STARTUP_NOTICE:
        send_bark_notification(
            title="✅ DMIT 三通道监控已就绪",
            body=f"正在云端 24 小时盯防 {TARGET_PRODUCT}（库存源 + 官方TG + NodeSeek），有货立即响铃！",
            target_url=direct_add_url(STATE["current_pid"]),
            urgent=False,
        )

    # 初始化社区基线（不推送历史旧帖）
    check_community_feeds()
    STATE["status"] = "running"
    cycle = 0

    while True:
        try:
            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            now_hms = datetime.now().strftime("%H:%M:%S")

            in_stock, msg = single_check_cycle(cycle)

            # 每 3 轮（约 60 秒）扫描一次官方 TG 频道与 NodeSeek 论坛新帖
            if cycle % 3 == 0 and cycle > 0:
                news_list = check_community_feeds()
                for news_title, news_body in news_list:
                    logger.warning(f"📢 捕获社区补货信号: {news_title} - {news_body}")
                    send_bark_notification(
                        title=news_title,
                        body=f"{news_body}\n点击立即前往 DMIT 加购页核实抢购！",
                        target_url=direct_add_url(STATE["current_pid"]),
                        urgent=True,
                    )

            cycle += 1
            STATE["total_checks"] += 1
            STATE["last_check_utc"] = now_utc
            STATE["last_result"] = msg
            STATE["bark_configured"] = bool(BARK_KEY)

            if in_stock is None:
                STATE["consecutive_errors"] += 1
                logger.warning(f"[{now_hms}] ⚠️ {msg} (连续异常 {STATE['consecutive_errors']} 次)")
            elif in_stock:
                STATE["in_stock"] = True
                STATE["consecutive_errors"] = 0
                buy_url = direct_add_url(STATE["current_pid"])
                logger.warning(f"🚨🚨🚨【补货警报】{TARGET_PRODUCT} 有货了！{msg}")

                for i in range(ALERT_REPEAT_TIMES):
                    send_bark_notification(
                        title=f"🚨 紧急补货！DMIT {TARGET_PRODUCT}",
                        body=f"[{now_hms}] 监测到已有库存！手慢无，点击本通知直接加购结算！",
                        target_url=buy_url,
                        urgent=True,
                    )
                    if i + 1 < ALERT_REPEAT_TIMES:
                        time.sleep(10)

                logger.info("报警推送完毕，进入 300 秒冷却期避免重复轰炸...")
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
            logger.error(f"主循环异常: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    if "--once" in sys.argv:
        sys.exit(run_once())
    t = threading.Thread(target=start_health_server, args=(PORT,), daemon=True)
    t.start()
    run_monitor()
