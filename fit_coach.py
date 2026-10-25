#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦门私教 mcd-fit-coach —— 在麦当劳吃出热量缺口
基于麦当劳官方 MCP 的减脂/健身配餐 Skill，零第三方依赖，单文件实现。

用法:
  python fit_coach.py --mock                          # 离线演示（无需 Token）
  python fit_coach.py --list-tools                    # 打印麦当劳 MCP 服务端工具清单
  python fit_coach.py --height 175 --weight 82 --age 30 --sex m --goal cut
  环境变量 MCD_MCP_TOKEN 或 --token 传入 MCP Token；MCD_MCP_URL 可覆盖服务端地址。

仅供学习与参赛演示；营养与价格数据以麦当劳官方渠道实时结果为准。
"""

import argparse
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from datetime import date

DEFAULT_MCP_URL = "https://mcp.mcd.cn"
USER_AGENT = "mcd-fit-coach/1.0.0"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# MCP 客户端（Streamable HTTP / JSON-RPC 2.0，兼容 JSON 与 SSE 两种响应）
# ---------------------------------------------------------------------------

class McpClient:
    def __init__(self, url, token, timeout=25):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.session_id = None
        self._next_id = 1

    def _headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _post(self, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, headers=self._headers(), method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError("MCP HTTP %s: %s" % (e.code, body)) from None
        except urllib.error.URLError as e:
            raise RuntimeError("无法连接 MCP 服务端 (%s): %s" % (self.url, e.reason)) from None
        sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        ctype = (resp.headers.get("Content-Type") or "").lower()
        raw = resp.read().decode("utf-8", errors="replace")
        if "text/event-stream" in ctype:
            return self._parse_sse(raw)
        return json.loads(raw) if raw.strip() else None

    @staticmethod
    def _parse_sse(raw):
        """SSE 响应里每个 data: 行是一段 JSON，取最后一条可解析的。"""
        result = None
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                chunk = line[5:].strip()
                try:
                    result = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
        return result

    def initialize(self):
        resp = self._post({
            "jsonrpc": "2.0", "id": self._next_id, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "mcd-fit-coach", "version": "1.0.0"},
            },
        })
        if resp is None:
            raise RuntimeError("MCP initialize 无响应")
        if "error" in resp:
            raise RuntimeError("MCP initialize 失败: %s" % resp["error"])
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return resp.get("result", {})

    def list_tools(self):
        resp = self._post({"jsonrpc": "2.0", "id": self._next_id, "method": "tools/list"})
        return (resp or {}).get("result", {}).get("tools", [])

    def call(self, tool, arguments=None):
        self._next_id += 1
        resp = self._post({
            "jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        })
        if resp is None:
            raise RuntimeError("MCP tools/call 无响应")
        if "error" in resp:
            raise RuntimeError("MCP 工具 %s 调用失败: %s" % (tool, resp["error"]))
        result = resp.get("result", {})
        if result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict)]
            raise RuntimeError("MCP 工具 %s 返回错误: %s" % (tool, " ".join(texts)[:300]))
        texts = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict)]
        return "\n".join(t for t in texts if t)


def parse_tool_payload(text):
    """工具返回可能是 JSON 字符串或裹在 JSON 里的字符串，逐层解开。"""
    for _ in range(3):
        if not isinstance(text, str):
            break
        stripped = text.strip()
        if stripped.startswith(("{", "[")):
            try:
                text = json.loads(stripped)
            except json.JSONDecodeError:
                break
        else:
            break
    return text


# ---------------------------------------------------------------------------
# 营养数据获取：优先真实 MCP，字段名做容错提取
# ---------------------------------------------------------------------------

CALORIE_KEYS = ("energykcal", "calorie", "calories", "cal", "kcal", "energy", "热量", "卡路里", "千卡", "能量")
PROTEIN_KEYS = ("protein", "proteincontent", "蛋白质", "蛋白")
NAME_KEYS = ("name", "foodname", "food_name", "title", "名称", "餐品名")


def _pick(d, keys):
    if not isinstance(d, dict):
        return None
    lower = {str(k).lower(): v for k, v in d.items()}
    for key in keys:
        if key in lower:
            return lower[key]
        for k, v in lower.items():
            if key in k:
                return v
    return None


def unwrap_api_response(text):
    """拆开 MCP 工具返回的 markdown 信封：定位 '## Original Response' 后的 JSON 对象。"""
    marker = text.lower().find("original response")
    if marker == -1:
        return None
    tail = text[marker:]
    i, j = tail.find("{"), tail.rfind("}")
    if i == -1 or j <= i:
        return None
    try:
        return json.loads(tail[i:j + 1])
    except json.JSONDecodeError:
        return None


def parse_custom_table(text):
    """解析 MCP 自定义表格式：首行 `[行数]{col1,col2,...}:`，随后每行为逗号分隔记录。"""
    lines = text.strip().splitlines()
    cols = None
    start = None
    for idx, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and "{" in s and "}" in s and s.rstrip().endswith(":"):
            cols = [c.strip() for c in s[s.index("{") + 1:s.index("}")].split(",")]
            start = idx + 1
            break
    if not cols:
        return None
    rows = []
    for line in lines[start:]:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = [p.strip() for p in s.split(",")]
        if len(parts) != len(cols):
            continue
        rows.append({c: (None if p in ("", "null") else p) for c, p in zip(cols, parts)})
    return rows or None


def extract_items(payload):
    """从工具返回中尽力提取 [{name, kcal, protein}]，兼容 JSON 列表与自定义表格式。"""
    if isinstance(payload, str):
        parsed = parse_tool_payload(payload)
        if parsed is None or isinstance(parsed, str):
            parsed = unwrap_api_response(payload)
        if parsed is not None:
            payload = parsed
    if isinstance(payload, str):
        rows = parse_custom_table(payload)
        payload = rows if rows is not None else payload
    if isinstance(payload, dict):
        for key in ("data", "items", "foods", "list", "records", "result", "menu"):
            if key in payload:
                payload = payload[key]
                break
    if isinstance(payload, str):
        rows = parse_custom_table(payload)
        payload = rows if rows is not None else payload
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise ValueError("营养数据格式无法识别：不是列表结构")

    items = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        name = _pick(row, NAME_KEYS)
        kcal = _pick(row, CALORIE_KEYS)
        protein = _pick(row, PROTEIN_KEYS)
        if name is None or kcal is None:
            continue
        try:
            kcal = float(str(kcal).replace("千卡", "").replace("kcal", "").strip())
            protein = float(str(protein).replace("克", "").replace("g", "").strip()) if protein is not None else 0.0
        except ValueError:
            continue
        items.append({"name": str(name), "kcal": kcal, "protein": protein})
    if not items:
        raise ValueError("营养数据中未提取到可用条目（缺少名称或热量字段）")
    # 单位纠偏：若整体数值呈千焦量级（中位数远超正常 kcal 范围），按 1 kcal = 4.184 kJ 换算
    kcals = sorted(i["kcal"] for i in items)
    median = kcals[len(kcals) // 2]
    if median > 1500:
        for i in items:
            i["kcal"] = round(i["kcal"] / 4.184, 1)
    return items


MOCK_MENU = [
    {"name": "巨无霸", "kcal": 557, "protein": 25},
    {"name": "麦辣鸡腿堡", "kcal": 549, "protein": 22},
    {"name": "板烧鸡腿堡", "kcal": 517, "protein": 26},
    {"name": "双层吉士堡", "kcal": 445, "protein": 24},
    {"name": "麦香鱼", "kcal": 330, "protein": 15},
    {"name": "吉士蛋麦满分", "kcal": 283, "protein": 15},
    {"name": "猪柳蛋麦满分", "kcal": 371, "protein": 17},
    {"name": "脆薯饼", "kcal": 150, "protein": 2},
    {"name": "麦乐鸡(5块)", "kcal": 235, "protein": 12},
    {"name": "板烧鸡腿(原味)", "kcal": 300, "protein": 30},
    {"name": "蔬菜色拉", "kcal": 60, "protein": 3},
    {"name": "玉米杯", "kcal": 90, "protein": 3},
    {"name": "0度泡泡啤风味饮", "kcal": 5, "protein": 0},
    {"name": "无糖可乐", "kcal": 1, "protein": 0},
    {"name": "鲜煮咖啡", "kcal": 8, "protein": 0},
    {"name": "脱脂牛奶", "kcal": 95, "protein": 8},
    {"name": "草莓圣代(迷你)", "kcal": 150, "protein": 4},
]


def fetch_menu_live(client):
    """优先调用官方文档确认的 list-nutrition-foods；失败则按名称模糊匹配兜底。"""
    tools = client.list_tools()
    if not tools:
        raise RuntimeError("服务端 tools/list 为空")
    names = {t.get("name") for t in tools}
    tried = []
    if "list-nutrition-foods" in names:
        tried.append("list-nutrition-foods")
        try:
            raw = client.call("list-nutrition-foods", {})
            return extract_items(parse_tool_payload(raw)), "list-nutrition-foods"
        except Exception as e:  # noqa: BLE001 - 继续模糊匹配兜底
            print("警告：list-nutrition-foods 调用失败（%s），尝试模糊匹配" % e)
    scored = []
    for t in tools:
        name = (t.get("name") or "").lower()
        desc = (t.get("description") or "").lower()
        score = 0
        if "nutrition" in name or "food" in name:
            score += 4
        if "menu" in name or "list" in name:
            score += 1
        if "nutrition" in desc or "营养" in desc:
            score += 3
        if "menu" in desc or "菜单" in desc or "餐品" in desc:
            score += 2
        if score and t.get("name") not in tried:
            scored.append((score, t))
    if not scored:
        raise RuntimeError("服务端工具中未找到营养/菜单类工具")
    scored.sort(key=lambda x: -x[0])
    errors = list(tried)
    for _, t in scored[:3]:
        try:
            raw = client.call(t["name"], {})
            return extract_items(parse_tool_payload(raw)), t["name"]
        except Exception as e:  # noqa: BLE001 - 逐个尝试，记录原因
            errors.append("%s: %s" % (t.get("name"), e))
    raise RuntimeError("营养工具调用均失败 -> " + "; ".join(errors))


def fetch_perks_live(client):
    """尽力获取优惠券/活动信息，失败不阻断主流程。"""
    notes = []
    try:
        tools = client.list_tools()
        names = {t.get("name") for t in tools}
        for tool in ("available-coupons", "query-my-coupons"):
            if tool in names:
                raw = client.call(tool, {})
                data = parse_tool_payload(raw)
                text = json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data
                notes.append("优惠券数据已获取（%s，%d 字符）" % (tool, len(text)))
                break
        else:
            for t in tools:
                name = (t.get("name") or "").lower()
                if "coupon" in name or "voucher" in name or "优惠" in name:
                    raw = client.call(t["name"], {})
                    data = parse_tool_payload(raw)
                    text = json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data
                    notes.append("优惠券数据已获取（%s，%d 字符）" % (t["name"], len(text)))
                    break
    except Exception as e:  # noqa: BLE001
        notes.append("优惠券获取失败：%s" % e)
    return notes


# ---------------------------------------------------------------------------
# 热量与配餐
# ---------------------------------------------------------------------------

ACTIVITY_LEVELS = {
    "sedentary": 1.2, "light": 1.375, "moderate": 1.55, "active": 1.725,
}

MEAL_SLOTS = [
    ("早餐", ["麦满分", "吉士蛋", "猪柳", "咖啡", "牛奶", "薯饼"]),
    ("午餐", ["堡", "霸", "鸡", "卷", "辣"]),
    ("晚餐", ["堡", "霸", "鱼", "鸡", "色拉", "玉米"]),
]


def compute_targets(height, weight, age, sex, activity, goal):
    bmr = 10 * weight + 6.25 * height - 5 * age + (5 if sex == "m" else -161)
    tdee = bmr * ACTIVITY_LEVELS.get(activity, 1.375)
    delta = {"cut": -400, "maintain": 0, "bulk": 300}[goal]
    budget = max(tdee + delta, 1200 if sex == "f" else 1500)
    protein_per_kg = {"cut": 1.8, "maintain": 1.4, "bulk": 1.6}[goal]
    return {
        "bmr": round(bmr), "tdee": round(tdee),
        "budget": round(budget),
        "protein": round(weight * protein_per_kg),
    }


def categorize(items):
    """返回 槽位 -> 全局索引列表；关键词按槽位顺序首配即停。"""
    buckets = {slot: [] for slot, _ in MEAL_SLOTS}
    buckets["其他"] = []
    for gi, item in enumerate(items):
        for slot, keywords in MEAL_SLOTS:
            if any(k in item["name"] for k in keywords):
                buckets[slot].append(gi)
                break
        else:
            buckets["其他"].append(gi)
    return buckets


def build_plan(items, targets):
    buckets = categorize(items)
    split = [("早餐", 0.3), ("午餐", 0.4), ("晚餐", 0.3)]
    plan = []
    total_kcal = 0.0
    total_protein = 0.0
    used = set()

    def pick(slot, ratio, pool):
        nonlocal total_kcal, total_protein
        slot_kcal = targets["budget"] * ratio
        best, best_score = None, None
        for gi in pool:
            if gi in used:
                continue
            item = items[gi]
            score = abs(item["kcal"] - slot_kcal) - item["protein"] * 6
            if best_score is None or score < best_score:
                best, best_score = gi, score
        if best is None:
            return
        used.add(best)
        plan.append({"slot": slot, **items[best]})
        total_kcal += items[best]["kcal"]
        total_protein += items[best]["protein"]

    for slot, ratio in split:
        pool = buckets.get(slot) or buckets["其他"] or list(range(len(items)))
        pick(slot, ratio, pool)

    # 补齐轮：用小食/饮品/加餐把总热量逼近预算（±8% 内），优先高蛋白
    low, high = targets["budget"] * 0.92, targets["budget"] * 1.08
    for _ in range(8):
        if total_kcal >= low:
            break
        best, best_score = None, None
        for gi in range(len(items)):
            if gi in used:
                continue
            item = items[gi]
            if total_kcal + item["kcal"] > high:
                continue
            left = targets["budget"] - total_kcal
            score = abs(left - item["kcal"]) - item["protein"] * 4
            if best_score is None or score < best_score:
                best, best_score = gi, score
        if best is None:
            break
        used.add(best)
        plan.append({"slot": "加餐", **items[best]})
        total_kcal += items[best]["kcal"]
        total_protein += items[best]["protein"]
    return plan, total_kcal, total_protein


# ---------------------------------------------------------------------------
# 传播卡片（单文件 HTML，无外部资源，移动端宽度，可直接截图发小红书）
# ---------------------------------------------------------------------------

MEME_LINES = [
    "教练，我想吃巨无霸。——可以，先把缺口走完。",
    "三分练，七分吃，麦门帮你算到嘴。",
    "麦门永不吃亏，也不吃超标。",
    "别的私教收课时费，麦门私教只求一颗 Star。",
    "今天也是离马甲线最近的一天。",
]

RED = "#da291c"
YELLOW = "#ffc72c"


def build_card(profile, targets, plan, total_kcal, total_protein, mock, extra_notes):
    today = date.today().strftime("%Y-%m-%d")
    goal_label = {"cut": "减脂", "maintain": "维持", "bulk": "增肌"}[profile["goal"]]
    kcal_pct = min(100, round(total_kcal / targets["budget"] * 100)) if targets["budget"] else 0
    protein_pct = min(100, round(total_protein / targets["protein"] * 100)) if targets["protein"] else 0
    meme = MEME_LINES[date.today().timetuple().tm_yday % len(MEME_LINES)]

    meals_html = ""
    for i, meal in enumerate(plan, 1):
        meals_html += (
            '<div class="meal"><div class="meal-head"><span class="meal-slot">%s</span>'
            '<span class="meal-kcal">%d kcal</span></div>'
            '<div class="meal-name">%s</div>'
            '<div class="meal-protein">蛋白质 %sg</div></div>'
            % (meal["slot"], meal["kcal"], meal["name"], meal["protein"])
        )

    badges = ""
    for note in extra_notes:
        badges += '<div class="note">%s</div>' % note

    watermark = ('<div class="mode-tag">演示数据 · MOCK</div>' if mock
                 else '<div class="mode-tag">MCP 实时数据</div>')
    source = "演示数据" if mock else "麦当劳 MCP 实时数据"

    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>麦门私教 · 今日减脂餐单</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#f5f1e8; font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
         display:flex; justify-content:center; padding:24px 12px; }
  .card { width:390px; background:#fff; border-radius:20px; overflow:hidden;
          box-shadow:0 12px 40px rgba(0,0,0,.14); }
  .head { background:linear-gradient(135deg,%(red)s,#b71c12); color:#fff; padding:26px 22px 44px;
          position:relative; }
  .arch { width:64px; height:40px; border:6px solid %(yellow)s; border-bottom:none;
          border-radius:32px 32px 0 0; margin-bottom:12px; }
  .head h1 { font-size:26px; letter-spacing:1px; }
  .head .sub { opacity:.85; font-size:12px; margin-top:6px; max-width:75%%; line-height:1.6; }
  .goal-tag { position:absolute; right:18px; top:22px; background:%(yellow)s; color:#7a1208;
              font-weight:700; padding:5px 12px; border-radius:999px; font-size:13px; }
  .mode-tag { position:absolute; right:18px; bottom:16px; font-size:11px; padding:3px 10px;
              border-radius:999px; background:rgba(255,255,255,.16); border:1px solid rgba(255,255,255,.45); }
  .body { padding:20px 22px; }
  .metrics { display:flex; gap:10px; margin-bottom:16px; }
  .metric { flex:1; background:#faf6ee; border-radius:12px; padding:10px 6px; text-align:center; }
  .metric b { display:block; font-size:20px; color:%(red)s; }
  .metric span { font-size:11px; color:#8a8272; }
  .section-title { font-size:13px; color:#8a8272; margin:14px 0 8px; }
  .bar { background:#f0e9da; border-radius:999px; height:14px; overflow:hidden; }
  .bar i { display:block; height:100%%; border-radius:999px;
           background:linear-gradient(90deg,%(yellow)s,%(red)s); }
  .meals { display:flex; flex-direction:column; gap:10px; margin-top:6px; }
  .meal { border:1px solid #f0e6d2; border-radius:14px; padding:12px 14px; background:#fffdf8; }
  .meal-head { display:flex; justify-content:space-between; align-items:baseline; }
  .meal-slot { font-weight:700; color:%(red)s; }
  .meal-kcal { font-size:13px; color:#6b6455; }
  .meal-name { font-size:17px; font-weight:600; margin-top:4px; }
  .meal-protein { font-size:12px; color:#8a8272; margin-top:2px; }
  .note { background:#fff8e6; border-left:3px solid %(yellow)s; padding:8px 10px;
          font-size:12px; color:#7a6a33; border-radius:0 8px 8px 0; margin-top:8px; }
  .meme { margin-top:18px; padding:14px; background:#faf6ee; border-radius:14px;
          font-size:13px; color:#6b6455; text-align:center; line-height:1.7; }
  .foot { padding:14px 22px 22px; text-align:center; font-size:11px; color:#a89f8d; }
  .foot b { color:%(red)s; }
</style></head>
<body><div class="card">
  <div class="head">
    <div class="arch"></div>
    <h1>麦门私教</h1>
    <div class="sub">在麦当劳吃出热量缺口 · %(today)s · 数据源：%(source)s</div>
    <div class="goal-tag">今日目标：%(goal_label)s</div>
    %(watermark)s
  </div>
  <div class="body">
    <div class="metrics">
      <div class="metric"><b>%(tdee)s</b><span>TDEE (kcal)</span></div>
      <div class="metric"><b>%(budget)s</b><span>今日预算 (kcal)</span></div>
      <div class="metric"><b>%(protein)s g</b><span>蛋白目标</span></div>
    </div>
    <div class="section-title">热量进度 · 已配 %(total_kcal)s / %(budget)s kcal</div>
    <div class="bar"><i style="width:%(kcal_pct)s%%"></i></div>
    <div class="section-title">蛋白质进度 · %(total_protein)s / %(protein)s g</div>
    <div class="bar"><i style="width:%(protein_pct)s%%"></i></div>
    <div class="section-title">麦门减脂餐单</div>
    <div class="meals">%(meals)s</div>
    %(badges)s
    <div class="meme">%(meme)s</div>
  </div>
  <div class="foot">Powered by 麦当劳 MCP · <b>mcd-fit-coach</b><br>营养与价格以麦当劳官方渠道实时结果为准</div>
</div></body></html>""" % {
        "red": RED, "yellow": YELLOW, "today": today, "source": source,
        "goal_label": goal_label, "watermark": watermark,
        "tdee": targets["tdee"], "budget": targets["budget"], "protein": targets["protein"],
        "total_kcal": round(total_kcal), "kcal_pct": kcal_pct,
        "total_protein": round(total_protein), "protein_pct": protein_pct,
        "meals": meals_html, "badges": badges, "meme": meme,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="麦门私教 —— 基于麦当劳 MCP 的减脂配餐 Skill")
    parser.add_argument("--height", type=float, default=170, help="身高 cm")
    parser.add_argument("--weight", type=float, default=70, help="体重 kg")
    parser.add_argument("--age", type=int, default=28, help="年龄")
    parser.add_argument("--sex", choices=["m", "f"], default="m", help="生理性别")
    parser.add_argument("--activity", choices=list(ACTIVITY_LEVELS), default="light",
                        help="活动水平 sedentary/light/moderate/active")
    parser.add_argument("--goal", choices=["cut", "maintain", "bulk"], default="cut",
                        help="目标 cut=减脂 maintain=维持 bulk=增肌")
    parser.add_argument("--mock", action="store_true", help="离线演示模式，使用内置演示菜单")
    parser.add_argument("--list-tools", action="store_true", help="打印服务端工具清单后退出")
    parser.add_argument("--token", default=None, help="MCP Token（默认读环境变量 MCD_MCP_TOKEN）")
    parser.add_argument("--url", default=os.environ.get("MCD_MCP_URL", DEFAULT_MCP_URL),
                        help="MCP 服务端地址")
    parser.add_argument("--out", default="maimen_fit_card.html", help="卡片输出路径")
    args = parser.parse_args()

    profile = {"height": args.height, "weight": args.weight, "age": args.age,
               "sex": args.sex, "activity": args.activity, "goal": args.goal}
    targets = compute_targets(args.height, args.weight, args.age, args.sex,
                              args.activity, args.goal)
    print("[麦门私教] BMR %d kcal · TDEE %d kcal · 今日预算 %d kcal · 蛋白目标 %d g"
          % (targets["bmr"], targets["tdee"], targets["budget"], targets["protein"]))

    notes = []
    if args.mock:
        items = list(MOCK_MENU)
        print("[模式] 离线演示：使用内置演示菜单（%d 项）" % len(items))
        mock = True
    else:
        token = args.token or os.environ.get("MCD_MCP_TOKEN")
        if not token:
            print("错误：未提供 Token。请设置环境变量 MCD_MCP_TOKEN 或使用 --token；"
                  "首次体验可用 --mock。")
            sys.exit(2)
        client = McpClient(args.url, token)
        print("[MCP] 连接 %s ..." % args.url)
        client.initialize()
        if args.list_tools:
            tools = client.list_tools()
            print("[MCP] 服务端共 %d 个工具：" % len(tools))
            for t in tools:
                print("  - %s : %s" % (t.get("name"), (t.get("description") or "")[:80]))
            return
        try:
            items, tool_name = fetch_menu_live(client)
            print("[MCP] 营养数据来自工具 %s（%d 项）" % (tool_name, len(items)))
            notes += fetch_perks_live(client)
        except Exception as e:  # noqa: BLE001
            print("警告：真实数据获取失败（%s）。本次回退为演示数据；"
                  "如需真实配餐请检查 Token/网络。" % e)
            items, mock = list(MOCK_MENU), True
            notes.append("本次为演示数据：MCP 调用未成功")
        else:
            mock = False

    plan, total_kcal, total_protein = build_plan(items, targets)
    if not plan:
        print("错误：未能生成餐单")
        sys.exit(1)

    print("\n今日麦门减脂餐单：")
    for meal in plan:
        print("  %s：%s（%d kcal / 蛋白 %s g）"
              % (meal["slot"], meal["name"], meal["kcal"], meal["protein"]))
    print("  合计 %d kcal / 蛋白 %d g（预算 %d kcal / 目标 %d g）"
          % (round(total_kcal), round(total_protein), targets["budget"], targets["protein"]))

    html = build_card(profile, targets, plan, total_kcal, total_protein, mock, notes)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print("\n传播卡片已生成：%s（浏览器打开即可截图）" % args.out)


if __name__ == "__main__":
    main()
