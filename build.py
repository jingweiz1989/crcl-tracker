#!/usr/bin/env python3
"""
Circle (CRCL) 仪表盘数据刷新脚本

工作流：
  1. 从 Yahoo Finance v8 拉取最近 6 个月 CRCL 日线 OHLCV
  2. 从 DefiLlama 拉取 USDC 当前流通量
  3. 从 CoinGecko 拉取 BTC 当前价格
  4. 把数据写回 index.html 的 const ohlcv = [...] 等数组，更新顶部 KPI 和日期戳

GitHub Actions 每天自动执行 → commit & push → GitHub Pages 自动重新部署。
本地手动跑：python build.py
"""

import re
import sys
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

HTML_FILE = Path("index.html")

# ---------- 数据源 ----------

def fetch_crcl_ohlcv():
    """Yahoo Finance v8：拉取过去 6 个月 CRCL 日线"""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/CRCL"
    params = {"range": "6mo", "interval": "1d", "includePrePost": "false"}
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    result = data["chart"]["result"][0]
    timestamps = result["timestamp"]
    q = result["indicators"]["quote"][0]

    rows = []
    for i, ts in enumerate(timestamps):
        o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        if None in (o, h, l, c, v):
            continue
        date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        rows.append([date_str, round(o, 2), round(h, 2), round(l, 2), round(c, 2), int(v)])
    return rows


def fetch_usdc_circulation():
    """DefiLlama USDC 当前流通量（USD）"""
    url = "https://stablecoins.llama.fi/stablecoin/2"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    circ = data.get("circulating", {}).get("peggedUSD")
    return circ


def fetch_btc_price():
    """CoinGecko BTC USD 现价"""
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_market_cap=true"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json().get("bitcoin", {})


def fetch_stable_total():
    """DefiLlama 稳定币总市值 + USDT/USDC 拆分"""
    url = "https://stablecoins.llama.fi/stablecoins?includePrices=false"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    assets = r.json().get("peggedAssets", [])
    total = sum(a.get("circulating", {}).get("peggedUSD", 0) or 0 for a in assets)
    usdt = next((a for a in assets if a.get("symbol") == "USDT"), {})
    usdc = next((a for a in assets if a.get("symbol") == "USDC"), {})
    return {
        "total": total,
        "usdt": usdt.get("circulating", {}).get("peggedUSD", 0),
        "usdc": usdc.get("circulating", {}).get("peggedUSD", 0),
    }


# ---------- HTML 替换 ----------

def replace_ohlcv_array(html, rows):
    """替换 const ohlcv = [...]; 块"""
    lines = []
    for r in rows:
        lines.append(f'  ["{r[0]}",{r[1]},{r[2]},{r[3]},{r[4]},{r[5]}],')
    new_block = "const ohlcv = [\n" + "\n".join(lines).rstrip(",") + "\n];"
    pattern = re.compile(r"const ohlcv = \[.*?\];", re.DOTALL)
    return pattern.sub(new_block, html, count=1)


def replace_header_price(html, latest_close, prev_close, latest_date):
    """更新顶部 KPI: 当前股价 + 涨跌幅 + 日期"""
    change = latest_close - prev_close
    pct = (change / prev_close) * 100 if prev_close else 0
    arrow_class = "delta-up" if change >= 0 else "delta-down"
    sign = "+" if change >= 0 else ""
    new_html = re.sub(
        r'(<h3>当前股价</h3>\s*<div class="kpi">)\$[\d.]+\s*(<span[^>]*>)[^<]*(</span></div>\s*<div class="kpi-sub">)[^<]*',
        lambda m: f'{m.group(1)}${latest_close:.2f} <span class="{arrow_class}" style="font-size:14px;">{sign}{pct:.2f}%</span></div>\n    <div class="kpi-sub">{latest_date} 收盘 · 自动更新',
        html, count=1
    )
    return new_html


def replace_date_stamp(html, today_str):
    """更新顶部日期戳"""
    return re.sub(
        r'(USDC 发行方 · 截至 )[\d\-]+',
        rf'\g<1>{today_str}',
        html
    )


def replace_usdc_snapshot(html, usdc_b):
    """更新顶部 USDC 流通 KPI 静态值（live 接口也会再刷一次）"""
    if not usdc_b:
        return html
    return re.sub(
        r'(<div class="kpi" id="usdcLive">)\$[\d.]+ B(</div>)',
        rf'\g<1>${usdc_b:.1f} B\g<2>',
        html
    )


def replace_btc_snapshot(html, btc_price):
    """更新顶部 BTC 静态值"""
    if not btc_price:
        return html
    k = btc_price / 1000
    return re.sub(
        r'(<h3>BTC 现价</h3>\s*<div class="kpi">)~\$[\d.]+K(</div>)',
        rf'\g<1>~${k:.1f}K\g<2>',
        html
    )


# ---------- Main ----------

def main():
    if not HTML_FILE.exists():
        print(f"ERROR: {HTML_FILE} not found", file=sys.stderr)
        sys.exit(1)

    html = HTML_FILE.read_text(encoding="utf-8")
    today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    print(f"[{today}] 开始刷新数据...")

    # 1. CRCL OHLCV
    try:
        rows = fetch_crcl_ohlcv()
        if rows and len(rows) > 5:
            html = replace_ohlcv_array(html, rows)
            latest = rows[-1]
            prev = rows[-2] if len(rows) >= 2 else None
            html = replace_header_price(
                html,
                latest_close=latest[4],
                prev_close=prev[4] if prev else latest[4],
                latest_date=latest[0],
            )
            print(f"  ✓ CRCL: {len(rows)} 个交易日，最新 {latest[0]} 收 ${latest[4]:.2f}")
    except Exception as e:
        print(f"  ⚠ CRCL 拉取失败: {e}")

    # 2. USDC live
    try:
        circ = fetch_usdc_circulation()
        if circ:
            usdc_b = circ / 1e9
            html = replace_usdc_snapshot(html, usdc_b)
            print(f"  ✓ USDC 流通: ${usdc_b:.2f} B")
    except Exception as e:
        print(f"  ⚠ USDC 拉取失败: {e}")

    # 3. BTC price
    try:
        btc = fetch_btc_price()
        if btc.get("usd"):
            html = replace_btc_snapshot(html, btc["usd"])
            print(f"  ✓ BTC 现价: ${btc['usd']:,.0f}")
    except Exception as e:
        print(f"  ⚠ BTC 拉取失败: {e}")

    # 4. 日期戳
    html = replace_date_stamp(html, today)

    # 5. 写回
    HTML_FILE.write_text(html, encoding="utf-8")
    print(f"[{today}] 完成。写回 {HTML_FILE} ({len(html)/1024:.1f} KB)")


if __name__ == "__main__":
    main()
