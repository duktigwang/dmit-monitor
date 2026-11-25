#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DMIT LAX.AS3 (Pro.TINY & T1.TINY) 全天候三通道云端监控系统
-----------------------------------------------------------
通道 1 [主力实时]：HostMonit 实时库存引擎 JSON API（单次聚合查询，无视云机房 IP Cloudflare 403）
通道 2 [官网直连]：DMIT 官网直达加购页检测（非受限 IP 自动启用，受限云 IP 自动静默降级）
通道 3 [社区舆情]：DMIT 官方 Telegram (@dmitnews) + NodeSeek 论坛新帖 RSS 实时补货情报监听
推送端：iPhone Bark App（仅真实补货时触发 timeSensitive + alarm 强警报，点击直达对应套餐加购结算页）
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
from datetime import datetime, timedelta, timezone
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

DEFAULT_TARGETS = [
    {"name": "LAX.AS3.Pro.TINY", "pid": "253"},
    {"name": "LAX.AS3.T1.TINY", "pid": "271"},
]

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "20"))
ALERT_REPEAT_TIMES = int(os.environ.get("ALERT_REPEAT_TIMES", "3"))
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "300"))
SEND_STARTUP_NOTICE = os.environ.get("SEND_STARTUP_NOTICE", "0").strip() == "1"
PORT = int(os.environ.get("PORT", "8080"))
SELF_PING_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()

HOSTMONIT_SEARCH_API = "https://backend.stock.hostmonit.com/api/product/search"
TG_DMITNEWS_URL = "https://t.me/s/dmitnews"
NODESEEK_RSS_URL = "https://www.nodeseek.com/rss.xml"
DMIT_AS3_CATALOG_URL = "https://www.dmit.io/cart.php?region=los-angeles&generation=as3"
# =====================================================================

IMPERSONATE_PROFILES = ["safari17_0", "safari17_2_ios", "chrome131"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("DMIT-Monitor")

BJT = timezone(timedelta(hours=8))


def now_beijing_str() -> str:
    return datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S (北京时间)")


def direct_add_url(pid: str) -> str:
    return f"https://www.dmit.io/cart.php?a=add&pid={pid}" if pid else DMIT_AS3_CATALOG_URL


def load_targets() -> list[dict[str, str]]:
    """支持通过 TARGET_PRODUCTS='NAME:PID,NAME2:PID2' 或兼容单品环境变量加载监控列表"""
    raw_multi = os.environ.get("TARGET_PRODUCTS", "").strip()
    if raw_multi:
        items = []
        for entry in raw_multi.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" in entry:
                name, pid = entry.split(":", 1)
                items.append({"name": name.strip(), "pid": pid.strip()})
            else:
                items.append({"name": entry, "pid": ""})
        if items:
            return items

    single_name = os.environ.get("TARGET_PRODUCT", "").strip()
    if single_name:
        single_pid = os.environ.get("TARGET_PID", "253").strip()
        return [{"name": single_name, "pid": single_pid}]

    return [dict(item) for item in DEFAULT_TARGETS]


TARGETS = load_targets()

STATE = {
    "status": "starting",
    "targets": [t["name"] for t in TARGETS],
    "started_at_beijing": now_beijing_str(),
    "active_channels": ["hostmonit_api", "dmit_official", "tg_dmitnews", "nodeseek_rss"],
    "dmit_direct_available": None,
    "last_check_beijing": None,
    "products": {
        t["name"]: {
            "pid": t["pid"],
            "buy_url": direct_add_url(t["pid"]),
            "in_stock": False,
            "last_result": None,
            "last_stock_change_time": None,
        }
        for t in TARGETS
    },
    "total_checks": 0,
    "consecutive_errors": 0,
    "bark_configured": bool(BARK_KEY),
}

SEEN_TG_POSTS: set[str] = set()
SEEN_NS_GUIDS: set[str] = set()
COMMUNITY_INITIALIZED = False


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


def _query_hostmonit_keyword(keyword: str, page_size: int = 50) -> tuple[int, list[dict]]:
    status, data = http_post_json(
        HOSTMONIT_SEARCH_API,
        payload={
            "provider": "dmit",
            "pageNo": 1,
            "pageSize": page_size,
            "keyword": keyword,
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
        return status, []
    return 200, (data.get("content") or [])


def check_hostmonit_batch() -> dict[str, tuple[bool | None, str]]:
    """通道 1：通过 HostMonit 实时库存接口聚合查询所有监控套餐"""
    results: dict[str, tuple[bool | None, str]] = {}
    by_title: dict[str, dict] = {}

    try:
        # 若监控列表均属于 LAX.AS3，仅需单次请求即可拿到全部状态；缺失项再按名称补查
        batch_kw = "LAX.AS3" if all("LAX.AS3" in t["name"].upper() for t in TARGETS) else ""
        if batch_kw:
            status, items = _query_hostmonit_keyword(batch_kw, page_size=50)
            if status == 200:
                for it in items:
                    title = (it.get("title") or "").strip().upper()
                    if title:
                        by_title[title] = it

        for t in TARGETS:
            name = t["name"]
            prod_state = STATE["products"][name]
            item = by_title.get(name.upper())

            if item is None:
                status, items = _query_hostmonit_keyword(name, page_size=24)
                if status != 200:
                    results[name] = (None, f"HostMonit API 响应异常 (HTTP {status})")
                    continue
                for it in items:
                    if (it.get("title") or "").strip().upper() == name.upper():
                        item = it
                        break
                if item is None and items:
                    item = items[0]

            if item is None:
                results[name] = (None, f"HostMonit 未查询到套餐 [{name}]")
                continue

            prod_url = item.get("productUrl") or ""
            m = re.search(r"pid=(\d+)", prod_url)
            if m and m.group(1) != prod_state["pid"]:
                logger.info(f"🔄 自动更新 {name} PID: {prod_state['pid']} -> {m.group(1)}")
                prod_state["pid"] = m.group(1)
                prod_state["buy_url"] = direct_add_url(m.group(1))

            mon_time = item.get("monitorTime")
            prod_state["last_stock_change_time"] = mon_time
            mon_status = int(item.get("monitorStatus") or 0)
            avail_raw = str(item.get("availableNum") or "0").strip()
            avail_num = int(avail_raw) if avail_raw.isdigit() else 0

            if mon_status == 1 or avail_num > 0:
                results[name] = (
                    True,
                    f"🎉 HostMonit 检测到有货！(PID={prod_state['pid']}, status={mon_status}, avail={avail_raw})",
                )
            else:
                results[name] = (
                    False,
                    f"缺货中 [HostMonit 实时源] (PID={prod_state['pid']}, 上次变动: {mon_time})",
                )
    except Exception as e:
        for t in TARGETS:
            if t["name"] not in results:
                results[t["name"]] = (None, f"HostMonit 检测异常: {e}")

    return results


def check_dmit_direct(name: str, pid: str) -> tuple[bool | None, str]:
    """通道 2：直查 DMIT 官网（若当前云机房 IP 被 Cloudflare 拦截则返回 None 交给通道 1）"""
    if not pid:
        return None, "未配置 PID"
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
        return False, f"缺货中 [DMIT 官网直连] ({name}, PID={pid})"
    if "confproduct" in final_url or "billing cycle" in lower_html or "configure" in lower_html:
        return True, f"🎉 DMIT 官网加购页已开放！({name}, PID={pid})"
    return None, "DMIT 页面状态未知"


def _pick_community_target_url(text: str) -> str:
    low = text.lower()
    has_pro = "pro" in low
    has_t1 = "t1" in low
    if has_t1 and not has_pro and "LAX.AS3.T1.TINY" in STATE["products"]:
        return STATE["products"]["LAX.AS3.T1.TINY"]["buy_url"]
    if has_pro and not has_t1 and "LAX.AS3.Pro.TINY" in STATE["products"]:
        return STATE["products"]["LAX.AS3.Pro.TINY"]["buy_url"]
    return DMIT_AS3_CATALOG_URL


def check_community_feeds() -> list[tuple[str, str, str]]:
    """
    通道 3：监听 DMIT 官方 TG 频道 (@dmitnews) 与 NodeSeek 社区最新帖
    返回新发现的补货情报列表: [(标题, 内容摘要, 跳转链接), ...]
    """
    global COMMUNITY_INITIALIZED
    alerts: list[tuple[str, str, str]] = []

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
                if any(k in low for k in ["as3.pro", "pro.tiny", "as3.t1", "t1.tiny", "lax.as3", "restock", "补货", "放货"]):
                    alerts.append(("📢 DMIT 官方频道新动态", text[:160], _pick_community_target_url(text)))
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
                is_target = any(k in low_t for k in ["as3", "tiny", "lax.pro", "pro.tiny", "t1.tiny", "lax.t1"])
                is_restock = any(k in low_t for k in ["补货", "放货", "有货", "上新", "开售", "restock"])
                is_trade = any(k in low_t for k in ["收", "出", "溢价", "求", "转让", "换"])
                if is_dmit and is_target and is_restock and not is_trade:
                    alerts.append(("📢 NodeSeek 社区补货快讯", f"新帖：{title}", _pick_community_target_url(title)))
    except Exception as e:
        logger.debug(f"NodeSeek RSS 巡检略过: {e}")

    COMMUNITY_INITIALIZED = True
    return alerts


def check_all_targets(cycle: int) -> dict[str, tuple[bool | None, str]]:
    """执行一轮综合库存检测"""
    if STATE["dmit_direct_available"] is not False or cycle % 90 == 0:
        direct_results: dict[str, tuple[bool | None, str]] = {}
        direct_ok = True
        for t in TARGETS:
            name = t["name"]
            pid = STATE["products"][name]["pid"]
            res, msg = check_dmit_direct(name, pid)
            if res is None:
                direct_ok = False
                break
            direct_results[name] = (res, msg)
        if direct_ok and len(direct_results) == len(TARGETS):
            return direct_results

    return check_hostmonit_batch()


def run_once() -> int:
    results = check_all_targets(1)
    for name, (in_stock, msg) in results.items():
        logger.info(f"[单次巡检] {name}: {msg}")
        if in_stock:
            buy_url = STATE["products"][name]["buy_url"]
            send_bark_notification(
                title=f"🚨 补货啦！DMIT {name}",
                body="云端巡检发现已开放加购！点击本通知直接加入购物车抢购！",
                target_url=buy_url,
                urgent=True,
            )
    return 0


def run_monitor():
    logger.info("=" * 64)
    logger.info("🚀 DMIT 三通道云端监控系统已启动！")
    logger.info(
        "🎯 目标套餐: " + ", ".join(f"{t['name']} (PID={STATE['products'][t['name']]['pid']})" for t in TARGETS)
    )
    logger.info(f"⏱️ 轮询间隔: 每 {CHECK_INTERVAL} 秒一次（社区舆情每 60 秒一次）")
    logger.info(f"🔑 Bark 推送: {'已就绪 ✅' if BARK_KEY else '未配置 ❌'}")
    logger.info("=" * 64)

    if BARK_KEY and SEND_STARTUP_NOTICE:
        send_bark_notification(
            title="✅ DMIT 三通道监控已就绪",
            body=f"正在云端 24 小时盯防 {' & '.join(STATE['targets'])}，有货立即响铃！",
            target_url=DMIT_AS3_CATALOG_URL,
            urgent=False,
        )

    check_community_feeds()
    STATE["status"] = "running"
    cycle = 0
    cooldown_until: dict[str, float] = {}

    while True:
        try:
            now_bjt = now_beijing_str()
            now_hms = datetime.now(BJT).strftime("%H:%M:%S")
            now_mono = time.monotonic()

            results = check_all_targets(cycle)

            # 每 24 轮（约 8 分钟）通过公网域名自唤醒一次，重置 Render 15 分钟休眠计时器
            if SELF_PING_URL and cycle % 24 == 0 and cycle > 0:
                try:
                    http_get(f"{SELF_PING_URL.rstrip('/')}/?keepalive=1", timeout=10)
                except Exception:
                    pass

            # 每 3 轮（约 60 秒）扫描一次官方 TG 频道与 NodeSeek 论坛新帖
            if cycle % 3 == 0 and cycle > 0:
                for news_title, news_body, news_url in check_community_feeds():
                    logger.warning(f"📢 捕获社区补货信号: {news_title} - {news_body}")
                    send_bark_notification(
                        title=news_title,
                        body=f"{news_body}\n点击立即前往 DMIT 加购页核实抢购！",
                        target_url=news_url,
                        urgent=True,
                    )

            cycle += 1
            STATE["total_checks"] += 1
            STATE["last_check_beijing"] = now_bjt
            STATE["bark_configured"] = bool(BARK_KEY)

            all_failed = all(res is None for res, _ in results.values())
            if all_failed:
                STATE["consecutive_errors"] += 1
            else:
                STATE["consecutive_errors"] = 0

            alerts_to_send: list[tuple[str, str]] = []
            for name, (in_stock, msg) in results.items():
                prod_state = STATE["products"][name]
                prod_state["last_result"] = msg
                if in_stock is None:
                    prod_state["in_stock"] = False
                    logger.warning(f"[{now_hms}] ⚠️ {name}: {msg}")
                elif in_stock:
                    prod_state["in_stock"] = True
                    logger.warning(f"🚨🚨🚨【补货警报】{name} 有货了！{msg}")
                    if now_mono >= cooldown_until.get(name, 0.0):
                        alerts_to_send.append((name, prod_state["buy_url"]))
                else:
                    prod_state["in_stock"] = False
                    cooldown_until.pop(name, None)
                    logger.info(f"[{now_hms}] {name}: {msg}")

            if alerts_to_send:
                for i in range(ALERT_REPEAT_TIMES):
                    for name, buy_url in alerts_to_send:
                        send_bark_notification(
                            title=f"🚨 紧急补货！DMIT {name}",
                            body=f"[{now_hms}] 监测到已有库存！手慢无，点击本通知直接加购结算！",
                            target_url=buy_url,
                            urgent=True,
                        )
                    if i + 1 < ALERT_REPEAT_TIMES:
                        time.sleep(10)

                next_allowed = time.monotonic() + COOLDOWN_SECONDS
                for name, _ in alerts_to_send:
                    cooldown_until[name] = next_allowed
                logger.info(f"{', '.join(n for n, _ in alerts_to_send)} 已完成连续报警，进入 {COOLDOWN_SECONDS} 秒独立冷却期（不影响其余套餐继续监控）")

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
