#!/usr/bin/env python3
"""
Circle (CRCL) 仪表盘数据刷新脚本 v2

v2 改进：
  - 用 BeautifulSoup 精准定位顶部 KPI 卡片，避免正则匹配失败
  - DefiLlama USDC fetch 支持多种返回结构
  - 详细日志：每个字段是否成功替换
"""

import re
import sys
import json
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from bs4 import BeautifulSoup

HTML_FILE = Path("index.html")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


# ---------- 数据源 ----------

def fetch_crcl_ohlcv():
    """Yahoo Finance v8：过去 6 个月 CRCL 日线"""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/CRCL"
    params = {"range": "6mo", "interval": "1d", "includePrePost": "false"}
    r = requests.get(url, params=params, headers=UA, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(ts):
        o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        if None in (o, h, l, c, v):
            continue
        d = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
        rows.append([d, round(o, 2), round(h, 2), round(l, 2), round(c, 2), int(v)])
    return rows


def fetch_usdc_circulation():
    """DefiLlama USDC 当前流通。兼容多种返回结构。"""
    # 优先用稳定币总览（更可靠）
    try:
        r = requests.get("https://stablecoins.llama.fi/stablecoins?includePrices=false",
                         headers=UA, timeout=20)
        r.raise_for_status()
        for a in r.json().get("peggedAssets", []):
            if a.get("symbol") == "USDC":
                circ = a.get("circulating", {}).get("peggedUSD") or a.get("circulatingPrevDay", {}).get("peggedUSD")
                if circ:
                    return float(circ)
    except Exception as e:
        print(f"  ⚠ DefiLlama stablecoins fail: {e}")
    # 备用：单 stablecoin 详细页
    try:
        r = requests.get("https://stablecoins.llama.fi/stablecoin/2", headers=UA, timeout=20)
        r.raise_for_status()
        d = r.json()
        circ = d.get("circulating", {}).get("peggedUSD") if isinstance(d.get("circulating"), dict) else None
        if not circ and d.get("tokens"):
            last = d["tokens"][-1]
            if isinstance(last, dict):
                circ = last.get("circulating", {}).get("peggedUSD")
        if circ:
            return float(circ)
    except Exception as e:
        print(f"  ⚠ DefiLlama stablecoin/2 fail: {e}")
    return None


def fetch_stable_market():
    """稳定币总市值 + 各币种"""
    try:
        r = requests.get("https://stablecoins.llama.fi/stablecoins?includePrices=false",
                         headers=UA, timeout=20)
        r.raise_for_status()
        assets = r.json().get("peggedAssets", [])
        out = {"total": 0, "usdt": 0, "usdc": 0}
        for a in assets:
            c = (a.get("circulating") or {}).get("peggedUSD", 0) or 0
            out["total"] += c
            if a.get("symbol") == "USDT":
                out["usdt"] = c
            elif a.get("symbol") == "USDC":
                out["usdc"] = c
        return out
    except Exception as e:
        print(f"  ⚠ Stable market fetch fail: {e}")
        return None


def fetch_btc_price():
    """CoinGecko BTC USD 现价"""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
            headers=UA, timeout=20)
        r.raise_for_status()
        return r.json().get("bitcoin", {}).get("usd")
    except Exception as e:
        print(f"  ⚠ BTC fetch fail: {e}")
        return None


# ---------- HTML 修改（BeautifulSoup 精准定位）----------

def update_html(html, ohlcv=None, usdc_b=None, btc=None, total_b=None, usdt_b=None, today=None):
    """精准更新 HTML 的多个动态字段，返回 (新HTML, 改动报告dict)"""
    report = {}

    # 1. OHLCV array（正则替换 `const ohlcv = [...]`）
    if ohlcv and len(ohlcv) > 5:
        lines = [f'  ["{r[0]}",{r[1]},{r[2]},{r[3]},{r[4]},{r[5]}]' for r in ohlcv]
        new_block = "const ohlcv = [\n" + ",\n".join(lines) + "\n];"
        new_html, n = re.subn(r"const ohlcv = \[.*?\];", new_block, html, count=1, flags=re.DOTALL)
        if n:
            report["ohlcv"] = f"OK · {len(ohlcv)} bars · 最新 {ohlcv[-1][0]} 收 ${ohlcv[-1][4]:.2f}"
            html = new_html
        else:
            report["ohlcv"] = "❌ 正则未匹配 const ohlcv 数组"

    # 2-5. 顶部 KPI（用 BeautifulSoup 定位）
    soup = BeautifulSoup(html, "html.parser")

    def find_kpi_card(label):
        """根据 <h3>标签内容定位 KPI 卡片"""
        for h3 in soup.find_all("h3"):
            txt = h3.get_text(strip=True).replace(" ", "")
            if label in txt:
                return h3.find_parent("div", class_="card")
        return None

    # 2. 当前股价 KPI
    if ohlcv and len(ohlcv) >= 2:
        latest = ohlcv[-1]
        prev = ohlcv[-2]
        latest_close = latest[4]
        chg = latest_close - prev[4]
        pct = (chg / prev[4]) * 100 if prev[4] else 0
        arrow = "delta-up" if chg >= 0 else "delta-down"
        sign = "+" if chg >= 0 else ""

        card = find_kpi_card("当前股价")
        if card:
            kpi = card.find("div", class_="kpi")
            sub = card.find("div", class_="kpi-sub")
            if kpi:
                kpi.clear()
                kpi.append(f"${latest_close:.2f} ")
                span = soup.new_tag("span", attrs={"class": arrow, "style": "font-size:14px;"})
                span.string = f"{sign}{pct:.2f}%"
                kpi.append(span)
            if sub:
                sub.clear()
                sub.string = f"{latest[0]} 收盘 · 自动更新 {today}"
            report["price_kpi"] = f"OK · ${latest_close:.2f} ({sign}{pct:.2f}%)"
        else:
            report["price_kpi"] = "❌ 未找到 当前股价 卡片"

    # 3. USDC 流通 KPI
    if usdc_b:
        for kid in ["usdcLive", "usdcLive2"]:
            el = soup.find("div", id=kid)
            if el:
                # 保留 <span class="live">LIVE</span> 不变（在 h3 里）
                el.clear()
                el.string = f"${usdc_b:.1f} B"
        # 也找名字含 USDC 流通 的卡片
        card = find_kpi_card("USDC流通")
        if card:
            sub = card.find("div", class_="kpi-sub")
            if sub and "Q1" in (sub.get_text() or ""):
                sub.clear()
                sub.string = f"DefiLlama 实时 · 自动刷新 {today}"
        report["usdc_kpi"] = f"OK · ${usdc_b:.1f}B"

    # 4. 稳定币总市场 KPI
    if total_b:
        for kid in ["totalStableLive", "totalStableLive2"]:
            el = soup.find("div", id=kid)
            if el:
                el.clear()
                el.string = f"${total_b:.0f} B"
        if usdc_b and usdt_b:
            sub_el = soup.find("div", id="totalStableSub")
            if sub_el:
                sub_el.clear()
                sub_el.string = f"USDC {usdc_b/total_b*100:.1f}% · USDT {usdt_b/total_b*100:.1f}%"
        report["total_kpi"] = f"OK · ${total_b:.0f}B"
        if usdt_b:
            el = soup.find("div", id="usdtLive")
            if el:
                el.clear()
                el.string = f"${usdt_b:.0f} B"

    # 5. BTC 现价 KPI（在市场环境 Tab）
    if btc:
        card = find_kpi_card("BTC现价")
        if card:
            kpi = card.find("div", class_="kpi")
            if kpi:
                kpi.clear()
                kpi.string = f"~${btc/1000:.1f}K"
        report["btc_kpi"] = f"OK · ${btc:,.0f}"

    # 6. 顶部副标题日期戳
    if today:
        for sub in soup.find_all("div", class_="sub"):
            if sub.string and "截至" in sub.string:
                sub.string = f"USDC 发行方 · 自动刷新 {today}"
                report["date_stamp"] = f"OK · {today}"
                break

    return str(soup), report


# ---------- Main ----------

def main():
    if not HTML_FILE.exists():
        print(f"ERROR: {HTML_FILE} not found", file=sys.stderr); sys.exit(1)

    today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    print(f"[{today}] 开始刷新数据...")

    rows = None
    try:
        rows = fetch_crcl_ohlcv()
        print(f"  ✓ CRCL Yahoo: {len(rows)} 个交易日，最新 {rows[-1][0]} 收 ${rows[-1][4]:.2f}")
    except Exception as e:
        print(f"  ⚠ CRCL fetch fail: {e}")

    usdc_circ = fetch_usdc_circulation()
    if usdc_circ:
        print(f"  ✓ USDC 流通: ${usdc_circ/1e9:.2f}B")
    else:
        print(f"  ⚠ USDC 拉取失败")

    mkt = fetch_stable_market()
    if mkt:
        print(f"  ✓ 稳定币市场总: ${mkt['total']/1e9:.0f}B · USDT ${mkt['usdt']/1e9:.0f}B · USDC ${mkt['usdc']/1e9:.0f}B")

    btc = fetch_btc_price()
    if btc:
        print(f"  ✓ BTC 现价: ${btc:,.0f}")

    # 写入 HTML
    html = HTML_FILE.read_text(encoding="utf-8")
    new_html, report = update_html(
        html,
        ohlcv=rows,
        usdc_b=(usdc_circ / 1e9) if usdc_circ else (mkt["usdc"] / 1e9 if mkt and mkt.get("usdc") else None),
        btc=btc,
        total_b=(mkt["total"] / 1e9) if mkt and mkt.get("total") else None,
        usdt_b=(mkt["usdt"] / 1e9) if mkt and mkt.get("usdt") else None,
        today=today.split()[0],
    )

    print("\n  替换报告:")
    for k, v in report.items():
        print(f"    {k}: {v}")

    if new_html != html:
        HTML_FILE.write_text(new_html, encoding="utf-8")
        diff = len(new_html) - len(html)
        print(f"\n[{today}] 完成。HTML 大小 {len(new_html)/1024:.1f} KB（{'+' if diff>=0 else ''}{diff} 字节）")
    else:
        print(f"\n[{today}] 完成。HTML 无变化")


if __name__ == "__main__":
    main()
