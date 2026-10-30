# -*- coding: utf-8 -*-
"""
财经资讯日报生成器
数据源：新浪财经（美股/港股/商品/汇率）、雪球（A股日K）、东方财富（板块/涨停池/7×24快讯）
输出：reports/财经日报_YYYYMMDD.png
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from PIL import Image

try:
    import chinese_calendar as _cc
except ImportError:
    _cc = None

BASE = Path(__file__).resolve().parent
REPORT_DIR = BASE / "reports"
REPORT_DIR.mkdir(exist_ok=True)
CACHE_DIR = BASE / ".cache"
CACHE_DIR.mkdir(exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

RED = "#d0483e"
GREEN = "#0e9f6e"
GREY = "#8a97a5"
BG = "#f2f4f7"


# ---------------------------------------------------------------- 网络请求
class Http:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self._last = {}

    def get(self, url, params=None, headers=None, retries=3, gap=1.0, timeout=12, kind="json", host=None):
        h = dict(headers or {})
        for i in range(retries):
            # 同一域名限速，避免被风控
            key = host or url.split("/")[2]
            wait = gap - (time.time() - self._last.get(key, 0))
            if wait > 0:
                time.sleep(wait)
            self._last[key] = time.time()
            try:
                r = self.s.get(url, params=params, headers=h, timeout=timeout)
                if r.status_code != 200:
                    raise RuntimeError("HTTP %s" % r.status_code)
                return r.json() if kind == "json" else r.content
            except Exception:
                if i == retries - 1:
                    return None
                time.sleep(1.5 * (i + 1))
        return None


# ---------------------------------------------------------------- 行情
def fetch_sina(hx):
    """新浪批量行情：美股指数/中概/港股/商品/汇率"""
    codes = ["gb_$dji", "gb_ixic", "gb_inx", "gb_sox",
             "gb_baba", "gb_pdd", "gb_jd", "gb_bidu", "gb_nio",
             "rt_hkHSI", "rt_hkHSTECH", "hf_GC", "hf_CL",
             "DINIW", "fx_susdcnh"]
    raw = hx.get("https://hq.sinajs.cn/list=" + ",".join(codes),
                 headers={"Referer": "https://finance.sina.com.cn/"}, kind="text")
    out = {}
    if not raw:
        return out
    try:
        text = raw.decode("gbk", "ignore")
    except Exception:
        text = raw.decode("utf-8", "ignore")
    for line in text.split(";"):
        m = re.search(r'hq_str_([A-Za-z0-9_$]+)="([^"]*)"', line)
        if not m:
            continue
        out[m.group(1)] = m.group(2).split(",")
    return out


def xueqiu_kline(hx, symbol, count=30):
    """雪球日K：日期/开/高/低/收/涨跌幅/成交额"""
    hx.s.get("https://xueqiu.com/", headers={"Referer": "https://xueqiu.com/"}, timeout=12)
    j = hx.get("https://stock.xueqiu.com/v5/stock/chart/kline.json",
               params={"symbol": symbol, "begin": str(int(time.time() * 1000)), "period": "day",
                       "type": "before", "count": -count, "indicator": "kline"},
               headers={"Referer": "https://xueqiu.com/"}, host="xueqiu.kline")
    if not j or not j.get("data"):
        return []
    col = j["data"]["column"]
    rows = []
    for it in j["data"]["item"]:
        d = dict(zip(col, it))
        if "date" not in d and d.get("timestamp"):
            d["date"] = datetime.fromtimestamp(d["timestamp"] / 1000).strftime("%Y-%m-%d")
        rows.append(d)
    return rows


def em_quote(hx, secids, fields="f2,f3,f4,f6,f12,f14,f86,f104,f105"):
    j = hx.get("https://push2.eastmoney.com/api/qt/ulist.np/get",
               params={"secids": secids, "fields": fields + ",f12,f14", "fltt": 2, "invt": 2},
               headers={"Referer": "https://quote.eastmoney.com/"}, host="em.push2")
    if not j or not j.get("data"):
        return {}
    return {d["f12"]: d for d in j["data"]["diff"]}


def em_board_map(hx):
    j = hx.get("https://push2.eastmoney.com/api/qt/clist/get",
               params={"pn": 1, "pz": 200, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                       "fid": "f3", "fs": "m:90+t:2", "fields": "f3,f12,f14"},
               headers={"Referer": "https://quote.eastmoney.com/"}, host="em.push2")
    if not j or not j.get("data"):
        return {}
    return {d["f14"]: d["f12"] for d in j["data"]["diff"]}


def em_board_kline(hx, code, lmt=4):
    j = hx.get("https://push2his.eastmoney.com/api/qt/stock/kline/get",
               params={"secid": "90." + code, "fields1": "f1,f2,f3,f4,f5,f6",
                       "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                       "klt": 101, "fqt": 1, "end": "20500101", "lmt": lmt},
               headers={"Referer": "https://quote.eastmoney.com/"}, host="em.his", gap=2.0)
    if not j or not j.get("data"):
        return None, []
    bars = []
    for k in j["data"]["klines"]:
        p = k.split(",")
        bars.append({"date": p[0], "close": float(p[2]), "pct": float(p[8]), "amount": float(p[6])})
    return j["data"].get("name"), bars


def em_index_kline(hx, secid, lmt=30):
    """东财指数日K（secid 如 1.000001/0.399001）：日期/收/涨跌幅/成交额"""
    j = hx.get("https://push2his.eastmoney.com/api/qt/stock/kline/get",
               params={"secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
                       "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                       "klt": 101, "fqt": 1, "end": "20500101", "lmt": lmt},
               headers={"Referer": "https://quote.eastmoney.com/"}, host="em.his", gap=2.0)
    if not j or not j.get("data"):
        return []
    bars = []
    for k in j["data"]["klines"]:
        p = k.split(",")
        bars.append({"date": p[0], "close": float(p[2]), "pct": float(p[8]), "amount": float(p[6])})
    return bars


def sina_index_kline(hx, symbol, count=30):
    """新浪指数日K（symbol 如 sh000001）：日期/收盘/涨跌幅"""
    j = hx.get("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
               params={"symbol": symbol, "scale": 240, "ma": "no", "datalen": count},
               headers={"Referer": "https://finance.sina.com.cn/"}, host="sina.kline")
    if not j:
        return []
    bars = []
    for it in j:
        try:
            bars.append({"date": it["day"], "close": float(it["close"]),
                         "pct": None, "amount": 0.0})
        except Exception:
            continue
    return bars


def sina_a_quotes(hx):
    """新浪 A股指数快照：{code: {px, pct, amount}}，amount 单位万元"""
    raw = hx.get("https://hq.sinajs.cn/list=s_sh000001,s_sz399001,s_sz399006",
                 headers={"Referer": "https://finance.sina.com.cn/"}, kind="text", host="sina.hq")
    out = {}
    if not raw:
        return out
    text = raw.decode("gbk", "ignore")
    for line in text.split(";"):
        m = re.search(r'hq_str_s_([a-z0-9]+)="([^"]*)"', line)
        if not m:
            continue
        p = m.group(2).split(",")
        try:
            out[m.group(1)] = {"px": float(p[1]), "pct": float(p[3]),
                               "amount": float(p[5]) if len(p) > 5 else 0.0}
        except Exception:
            continue
    return out


def em_limit_up(hx, date_str):
    j = hx.get("https://push2ex.eastmoney.com/getTopicZTPool",
               params={"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
                       "Pageindex": 0, "pagesize": 300, "sort": "fbt:asc", "date": date_str},
               headers={"Referer": "https://data.eastmoney.com/"}, host="em.pool")
    if not j or not j.get("data"):
        return None
    pool = j["data"].get("pool") or []
    boards = {}
    for p in pool:
        b = p.get("hybk") or ""
        if b:
            boards[b] = boards.get(b, 0) + 1
    return {"count": j["data"].get("tc") or len(pool),
            "boards": sorted(boards.items(), key=lambda x: -x[1])[:4]}


# ---------------------------------------------------------------- 快讯
def fetch_news(hx, since):
    items, seen = [], set()
    for fid in [102, 101, 103, 109]:
        raw = hx.get("https://newsapi.eastmoney.com/kuaixun/v1/getlist_%d_ajaxResult_50_1_.html" % fid,
                     headers={"Referer": "https://kuaixun.eastmoney.com/"}, kind="text", host="em.news")
        if not raw:
            continue
        try:
            txt = raw.decode("utf-8", "ignore").strip()
            body = re.search(r"var ajaxResult=(\{.*\})", txt, re.S)
            data = json.loads(body.group(1)) if body else {}
        except Exception:
            continue
        for it in data.get("LivesList") or []:
            t = (it.get("simtitle") or it.get("title") or "").strip()
            d = (it.get("simdigest") or it.get("digest") or "").strip()
            st = it.get("showtime") or ""
            if not t:
                continue
            key = re.sub(r"[\s【】]", "", t)[:26]
            if key in seen:
                continue
            try:
                dt = datetime.strptime(st, "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if dt < since:
                continue
            seen.add(key)
            items.append({"time": dt, "title": t, "digest": d})
    return items


# ---------------------------------------------------------------- 研判规则
OVERSEAS = ["美联储", "鲍威尔", "美国", "美股", "纳指", "纳斯达克", "道指", "标普", "特朗普", "关税",
            "白宫", "欧盟", "欧央行", "日本", "日经", "英伟达", "苹果", "特斯拉", "微软", "谷歌",
            "Meta", "OPEC", "原油", "黄金", "美元", "美债", "非农", "CPI", "PCE", "议息", "中东",
            "乌克兰", "以色列", "欧洲", "韩国", "印度", "制裁"]
DOMESTIC = ["央行", "国务院", "发改委", "证监会", "财政部", "工信部", "国常会", "政策", "A股",
            "沪指", "两市", "人民币", "降准", "降息", "LPR", "MLF", "专项债", "楼市", "地产",
            "出口", "PMI", "社融", "IPO", "芯片", "半导体", "算力", "光伏", "储能", "医药",
            "汽车", "消费", "国企", "央企", "并购", "回购", "增持", "补贴"]
WEIGHT = {"美联储": 6, "降息": 5, "加息": 5, "关税": 5, "国务院": 6, "国常会": 6, "央行": 5,
          "证监会": 5, "财政部": 5, "发改委": 4, "非农": 4, "CPI": 4, "PCE": 4, "GDP": 4,
          "PMI": 4, "社融": 4, "英伟达": 5, "苹果": 4, "特斯拉": 4, "OPEC": 4, "制裁": 4,
          "冲突": 4, "超预期": 3, "跌破": 3, "创新高": 3, "获批": 3, "试点": 2, "反内卷": 3,
          "回购": 2, "增持": 2, "中标": 2, "订单": 2, "存储": 2, "光刻": 2, "算力": 2}
POS = ["降准", "降息", "宽松", "刺激", "补贴", "减税", "回购", "增持", "超预期", "创新高",
       "达成", "签署", "获批", "扩产", "中标", "订单", "上调", "突破", "增长"]
NEG = ["加息", "收紧", "制裁", "关税", "调查", "罚款", "下调", "低于预期", "暴跌", "停产",
       "召回", "违约", "退市", "监管", "亏损", "裁员", "下调评级"]
SECTOR = [("半导体", ["芯片", "半导体", "存储", "光刻", "晶圆", "台积电", "英伟达"]),
          ("AI算力", ["算力", "AI", "人工智能", "大模型", "数据中心", "服务器", "GPU"]),
          ("新能源", ["光伏", "锂", "储能", "风电", "电池", "新能源"]),
          ("医药", ["医药", "创新药", "集采", "疫苗", "医疗器械"]),
          ("地产", ["楼市", "地产", "房贷", "收储", "土地"]),
          ("汽车", ["汽车", "整车", "智驾", "车企", "新能源车"]),
          ("军工", ["军工", "国防", "航天", "导弹"]),
          ("消费", ["消费", "白酒", "零售", "免税", "餐饮"]),
          ("金融", ["券商", "保险", "银行", "IPO", "资本市场"]),
          ("资源", ["原油", "石油", "天然气", "黄金", "铜", "有色", "煤炭"])]


STRONG_OVERSEAS = ["美联储", "鲍威尔", "欧洲央行", "欧央行", "日本央行", "英国央行", "美国",
                   "特朗普", "关税", "白宫", "美股", "纳指", "道指", "标普", "英伟达", "苹果",
                   "特斯拉", "OPEC", "欧元区", "德国", "法国", "韩国", "印度", "俄罗斯",
                   "中东", "以色列", "乌克兰", "拉加德"]


def classify_news(items):
    for it in items:
        text = it["title"] + " " + it["digest"]
        if any(k in text for k in STRONG_OVERSEAS):
            it["overseas"] = True
        else:
            it["overseas"] = sum(1 for k in OVERSEAS if k in text) > sum(1 for k in DOMESTIC if k in text)
        it["score"] = sum(w for k, w in WEIGHT.items() if k in text)
        it["sector"] = next((s for s, kws in SECTOR if any(k in text for k in kws)), "")
        pos = sum(1 for k in POS if k in text)
        neg = sum(1 for k in NEG if k in text)
        it["tone"] = "偏多" if pos > neg else ("偏空" if neg > pos else "中性")
        it["dup"] = re.sub(r"[\s·、，。！？【】\"“”‘’：:（）()]", "", it["title"])[:10]
    items.sort(key=lambda x: (-x["score"], -x["time"].timestamp()))
    return items


def pick_news(items, n=6):
    picked, used = [], set()
    for it in items:
        if len(picked) >= n:
            break
        if it["dup"] in used:
            continue
        used.add(it["dup"])
        picked.append(it)
    picked.sort(key=lambda x: x["time"], reverse=True)
    return picked


# ---------------------------------------------------------------- 组装模型
def fmt_pct(v, sign=True):
    if v is None:
        return "—"
    return ("%+.2f%%" if sign else "%.2f%%") % v


def color_of(v):
    if v is None:
        return GREY
    return RED if v > 0 else (GREEN if v < 0 else GREY)


def collect():
    hx = Http()
    now = datetime.now()
    model = {"generated_at": now, "sources": []}

    # ---------- 隔夜美股 ----------
    sina = fetch_sina(hx)
    us = []
    mapping = [("gb_$dji", "道琼斯"), ("gb_ixic", "纳斯达克"), ("gb_inx", "标普500")]
    session_date = None
    for code, name in mapping:
        f = sina.get(code)
        if not f or len(f) < 5:
            continue
        px = float(f[1])
        pct = float(f[2])
        chg = float(f[4])
        us.append({"name": name, "px": px, "pct": pct, "chg": chg})
        try:
            t = datetime.strptime(f[3], "%Y-%m-%d %H:%M:%S")
            d = t.date() - timedelta(days=1) if t.hour < 8 else t.date()
            session_date = d
        except Exception:
            pass
    if not session_date:
        session_date = (now - timedelta(days=1)).date()
    model["us"] = {"session_date": session_date, "indices": us}
    # 三个美股指数涨跌幅同时为 0 时，判定为盘前/数据未更新，避免误导性地显示“收平”
    if us and all((u["pct"] == 0.0) for u in us):
        model["us"]["stale"] = True
        for u in us:
            u["pct"] = None
            u["chg"] = None

    sox = sina.get("gb_sox")
    model["us"]["sox"] = ({"name": "费城半导体", "px": float(sox[1]), "pct": float(sox[2])}
                          if sox and len(sox) > 2 else None)

    adrs = []
    for code, name in [("gb_baba", "阿里巴巴"), ("gb_pdd", "拼多多"), ("gb_jd", "京东"),
                       ("gb_bidu", "百度"), ("gb_nio", "蔚来")]:
        f = sina.get(code)
        if f and len(f) > 2:
            try:
                adrs.append({"name": name, "px": float(f[1]), "pct": float(f[2])})
            except Exception:
                pass
    model["us"]["adr"] = adrs

    hk = []
    for code, name in [("rt_hkHSI", "恒生指数"), ("rt_hkHSTECH", "恒生科技")]:
        f = sina.get(code)
        if f and len(f) > 8:
            try:
                hk.append({"name": name, "px": float(f[6]), "pct": float(f[8])})
            except Exception:
                pass
    model["hk"] = hk

    # ---------- 商品 / 汇率 / 美元 ----------
    other = []
    g = sina.get("hf_GC")
    if g and len(g) > 7:
        try:
            last, base = float(g[0]), float(g[7])
            other.append({"name": "COMEX黄金", "px": last, "pct": (last - base) / base * 100})
        except Exception:
            pass
    c = sina.get("hf_CL")
    if c and len(c) > 7:
        try:
            last, base = float(c[0]), float(c[7])
            other.append({"name": "WTI原油", "px": last, "pct": (last - base) / base * 100})
        except Exception:
            pass
    d = sina.get("DINIW")
    if d and len(d) > 5:
        try:
            last, base = float(d[1]), float(d[5])
            other.append({"name": "美元指数", "px": last, "pct": (last - base) / base * 100})
        except Exception:
            pass
    fx = sina.get("fx_susdcnh")
    if fx and len(fx) > 8:
        try:
            other.append({"name": "离岸人民币", "px": float(fx[1]), "pct": float(fx[11])})
        except Exception:
            pass
    model["other"] = other
    model["sources"].append("新浪财经")

    # ---------- A股上一交易日 ----------
    idx_spec = [("1.000001", "sh000001", "上证指数"),
                ("0.399001", "sz399001", "深证成指"),
                ("0.399006", "sz399006", "创业板指")]
    snap = sina_a_quotes(hx)
    idx = []
    target = None
    a_amount = 0.0
    a_amount_prev = 0.0
    spark = []
    em_ok = False
    for secid, ssym, name in idx_spec:
        bars = em_index_kline(hx, secid, 30)
        if bars:
            em_ok = True
        else:  # 东财日K不可用时退回新浪日K
            bars = sina_index_kline(hx, ssym, 30)
        if not bars:
            continue
        k = len(bars) - 1
        last = bars[k]
        if last["date"] == now.strftime("%Y-%m-%d") and now.hour < 15:
            k -= 1
        bar, prev = bars[k], bars[max(k - 1, 0)]
        target = bar["date"]
        chg = bar["close"] - prev["close"]
        q = snap.get(ssym) or {}
        pct = bar.get("pct")
        if pct is None:
            pct = q.get("pct")
        if pct is None:
            pct = chg / prev["close"] * 100
        amt = bar.get("amount") or 0
        if not amt and q.get("amount"):  # 新浪快照单位为万元，折算为元
            amt = q["amount"] * 1e4
        idx.append({"name": name, "px": bar["close"], "pct": float(pct), "chg": chg,
                    "amount": amt})
        if secid != "0.399006":  # 创业板指成交额已包含在深市中
            a_amount += amt
            a_amount_prev += prev.get("amount") or 0
        if secid == "1.000001":
            spark = [b["close"] for b in bars[: k + 1]][-24:]
    model["a"] = {"date": target, "indices": idx,
                  "amount": a_amount or None, "amount_prev": a_amount_prev or None, "spark": spark}
    if idx:
        model["sources"].append("东方财富A股日K" if em_ok else "新浪财经A股日K")

    # ---------- 涨停 / 板块 ----------
    if target:
        zt = em_limit_up(hx, target.replace("-", ""))
        model["a"]["limit_up"] = zt
        watch = [("银行", "BK0475"), ("证券", "BK0473"), ("半导体", "BK1036"), ("白酒", "BK0896"),
                 ("化学制药", "BK0465"), ("电池", "BK1033"), ("光伏设备", "BK1031"),
                 ("国防军工", "BK0490"), ("消费电子", "BK1037"), ("有色金属", "BK0478"),
                 ("煤炭", "BK0437"), ("房地产开发", "BK0451")]
        sectors, done = [], set()
        for attempt in range(2):  # 第二遍补抓失败板块
            for label, code in watch:
                if code in done:
                    continue
                real, bars = em_board_kline(hx, code)
                if not bars or not real:
                    continue
                if label[:2] not in real:  # 代码与名称校验，避免板块改名
                    done.add(code)
                    continue
                hit = next((b for b in reversed(bars) if b["date"] == target), None)
                if hit:
                    sectors.append({"name": label, "pct": hit["pct"], "code": code})
                    done.add(code)
            time.sleep(3)
        if sectors:
            sectors.sort(key=lambda x: -x["pct"])
            model["a"]["sectors"] = sectors
            model["sources"].append("东方财富")

    # 涨跌家数 / 汇率商品补充：仅在收盘后数据有效
    q = em_quote(hx, "1.000001,0.399001,100.UDI,133.USDCNH,101.GC00Y",
                 fields="f2,f3,f6,f86,f104,f105")
    try:
        ts = int(q.get("000001", {}).get("f86") or 0)
        qt = datetime.fromtimestamp(ts)
        if target and qt.strftime("%Y-%m-%d") == target and q.get("399001"):
            up = int(q["000001"]["f104"]) + int(q["399001"]["f104"])
            dn = int(q["000001"]["f105"]) + int(q["399001"]["f105"])
            model["a"]["breadth"] = {"up": up, "down": dn}
    except Exception:
        pass
    for key, name in [("UDI", "美元指数"), ("USDCNH", "离岸人民币"), ("GC00Y", "COMEX黄金")]:
        d = q.get(key) or {}
        if d.get("f2") not in (None, "-") and d.get("f3") not in (None, "-"):
            for o in model["other"]:
                if o["name"] == name:
                    o["px"], o["pct"] = float(d["f2"]), float(d["f3"])

    # ---------- 快讯 ----------
    since = datetime.combine((now - timedelta(days=1)).date(), datetime.min.time()) + timedelta(hours=15)
    if now.hour < 3:
        since -= timedelta(days=1)
    news = classify_news(fetch_news(hx, since))
    if len([n for n in news if n["score"] > 0]) < 8:
        news = classify_news(fetch_news(hx, since - timedelta(hours=12)))
    model["news"] = {
        "overseas": pick_news([n for n in news if n["overseas"]], 5),
        "domestic": pick_news([n for n in news if not n["overseas"]], 5),
    }
    if news:
        model["sources"].append("东方财富7×24")

    #
    build_view(model)
    return model


def build_view(m):
    us = {x["name"]: x for x in m["us"]["indices"]}
    sox = m["us"].get("sox") or {}
    adr = m["us"].get("adr") or []
    hk = {x["name"]: x for x in m.get("hk") or []}
    oth = {x["name"]: x for x in m.get("other") or []}

    spx = us.get("标普500", {}).get("pct")
    ixic = us.get("纳斯达克", {}).get("pct")
    dji = us.get("道琼斯", {}).get("pct")
    sx = sox.get("pct")
    tech = None
    if ixic is not None and sx is not None:
        tech = (ixic + sx) / 2
    elif ixic is not None:
        tech = ixic

    usd = oth.get("美元指数", {}).get("pct")
    cnh = oth.get("离岸人民币", {}).get("pct")
    gold = oth.get("COMEX黄金", {}).get("pct")
    oil = oth.get("WTI原油", {}).get("pct")
    hst = hk.get("恒生科技", {}).get("pct")
    adr_avg = sum(a["pct"] for a in adr) / len(adr) if adr else None

    # 联动行
    rows = []

    def tone(v, hi=0.6, lo=-0.6):
        if v is None:
            return "中性", GREY
        if v >= hi:
            return "偏多", RED
        if v <= lo:
            return "偏空", GREEN
        return "中性", GREY

    def txt_tech(v):
        if v is None:
            return "美股科技数据缺失，暂按中性对待"
        if v <= -1.0:
            return "外围科技股明显走弱，A股成长赛道开盘情绪承压，重点看低开后能否放量承接"
        if v <= -0.3:
            return "外围科技小幅回调，A股半导体、算力偏震荡，等待盘中量能与资金确认"
        if v >= 1.0:
            return "外围科技走强，A股半导体、算力、消费电子有望获得情绪提振"
        return "外围科技窄幅波动，对A股科技板块影响有限"

    t1, c1 = tone(tech)
    rows.append({"signal": "纳指 %s ｜ 费半 %s" % (fmt_pct(ixic), fmt_pct(sx)),
                 "map": "半导体 · AI算力 · 消费电子", "tone": t1, "color": c1,
                 "text": txt_tech(tech)})

    bc = None
    if spx is not None and dji is not None:
        bc = (spx + dji) / 2
    t2, c2 = tone(bc)
    rows.append({"signal": "标普500 %s ｜ 道指 %s" % (fmt_pct(spx), fmt_pct(dji)),
                 "map": "沪深300 · 金融 · 核心资产", "tone": t2, "color": c2,
                 "text": ("隔夜蓝筹指数走强，外资风险偏好回升，A股权重与核心资产相对占优"
                          if (bc or 0) >= 0.6 else
                          "隔夜蓝筹走弱，A股权重板块承压，指数或低开后寻求支撑"
                          if (bc or 0) <= -0.6 else
                          "外围蓝筹波动有限，A股大盘以内部因素主导，指数大概率区间震荡")})

    # 美元：走强 = 偏空
    fx_sig = usd
    t3, c3 = tone(-fx_sig if fx_sig is not None else None)
    rows.append({"signal": "美元指数 %s ｜ 离岸人民币 %s" % (fmt_pct(usd), fmt_pct(cnh)),
                 "map": "外资流向 · 出口链 · 航空造纸", "tone": t3, "color": c3,
                 "text": ("美元走强、人民币承压，外资风险偏好受抑，利好出口链、压制航空与进口依赖行业"
                          if (fx_sig or 0) >= 0.3 else
                          "美元走弱、人民币压力缓和，外资流入环境改善，利好核心资产与成长股"
                          if (fx_sig or 0) <= -0.3 else
                          "汇率波动有限，对A股资金面影响中性")})

    adr_t = adr_avg
    t4, c4 = tone(adr_t)
    rows.append({"signal": "中概股均值 %s ｜ 恒生科技 %s" % (fmt_pct(adr_t), fmt_pct(hst)),
                 "map": "港股互联网 · A股传媒/计算机", "tone": t4, "color": c4,
                 "text": ("中概与港股科技走弱，A股互联网、传媒、计算机情绪偏弱"
                          if (adr_t or 0) <= -0.6 else
                          "中概与港股科技回暖，A股成长风格与互联网方向有望同步修复"
                          if (adr_t or 0) >= 0.6 else
                          "中概与港股科技分化，A股相关板块跟随有限")})

    cmd = None
    vals = [x for x in (oil, gold) if x is not None]
    cmd = sum(vals) / len(vals) if vals else None
    t5, c5 = tone(cmd)
    if (oil or 0) >= 0.8 and (gold or 0) <= 0:
        cmd_txt = "原油走高、黄金回落，石油石化与油服受益，黄金股短线承压"
    elif (gold or 0) >= 0.8 and (oil or 0) <= 0:
        cmd_txt = "黄金走强、原油回落，避险情绪升温，关注黄金股与有色"
    elif (oil or 0) >= 0.8:
        cmd_txt = "原油走强，石油石化、油服与炼化链条具备短线催化"
    elif (gold or 0) >= 0.8:
        cmd_txt = "黄金走强，黄金股与贵金属方向具备短线催化"
    elif (oil or 0) <= -0.8 or (gold or 0) <= -0.8:
        cmd_txt = "大宗商品回落，资源股承压，资金或回流成长与消费"
    else:
        cmd_txt = "大宗商品波动有限，资源股以个股逻辑为主"
    rows.append({"signal": "WTI原油 %s ｜ COMEX黄金 %s" % (fmt_pct(oil), fmt_pct(gold)),
                 "map": "石油石化 · 黄金股 · 有色", "tone": t5, "color": c5,
                 "text": cmd_txt})

    # 风险偏好打分
    score = 0.0
    if spx is not None:
        score += 0.34 * spx
    if ixic is not None:
        score += 0.34 * ixic
    if hst is not None:
        score += 0.12 * hst
    if adr_avg is not None:
        score += 0.20 * adr_avg
    if usd is not None:
        score -= 0.15 * usd
    label = "风险偏好回落" if score <= -0.55 else ("风险偏好回暖" if score >= 0.55 else "多空拉锯")
    m["linkage"] = {"rows": rows, "score": score, "label": label}

    # 一句话导语
    a_idx = {x["name"]: x for x in m["a"]["indices"]}
    sh = a_idx.get("上证指数", {})
    amount = m["a"].get("amount")
    amount_prev = m["a"].get("amount_prev")
    vol_txt = ""
    if amount and amount_prev:
        dlt = amount / amount_prev - 1
        vol_txt = ("较前日缩量 %.1f%%" % abs(dlt * 100)) if dlt < 0 else ("较前日放量 %.1f%%" % (dlt * 100))
    zt = m["a"].get("limit_up")
    zt_txt = ("、涨停 %d 家" % zt["count"]) if zt else ""
    us_desc = "数据未更新" if m["us"].get("stale") else (
        "集体收跌" if all((x["pct"] or 0) < 0 for x in m["us"]["indices"]) else (
            "集体收涨" if all((x["pct"] or 0) > 0 for x in m["us"]["indices"]) else "涨跌互现"))
    cyb = a_idx.get("创业板指", {}).get("pct")
    a_desc = "上证 %s" % fmt_pct(sh.get("pct"))
    if cyb is not None and abs(cyb) >= 1.5:
        a_desc += "、创业板 %s" % fmt_pct(cyb)
    if score <= -0.55:
        tail = "外围偏空叠加内部走弱，今日A股或低开震荡，关注低开后的承接力度与政策对冲"
    elif score >= 0.55:
        tail = "外围环境偏暖，A股有望迎来情绪修复，但需防范高开后的兑现压力"
    else:
        tail = "外围多空交织，A股或以震荡消化为主，等待量能方向选择"
    if score <= -0.55 and (sh.get("pct") or 0) > 0:
        tail = "外围偏空但A股自身收红，今日或以震荡消化外围压力为主"
    headline = ("A股上一交易日%s，%s%s；隔夜美股%s，纳指 %s、费半 %s。%s。"
                % (a_desc, vol_txt or "量能平稳", zt_txt, us_desc, fmt_pct(ixic), fmt_pct(sx), tail))
    m["headline"] = headline

    # 研判要点
    bullets = []
    bullets.append({"k": "外围映射",
                    "v": ("纳指 %s、费半 %s，科技成长情绪%s；开盘关注半导体、算力能否低开高走。"
                          % (fmt_pct(ixic), fmt_pct(sx), "偏弱" if (tech or 0) < -0.3 else "中性偏暖"))})
    fx_txt = ("美元指数 %s、" % fmt_pct(usd)) + (
        "人民币承压，外资流入环境偏谨慎" if (fx_sig or 0) > 0.3 else
        ("人民币减压，外资流入环境改善" if (fx_sig or 0) < -0.3 else "汇率波动有限，对资金面影响中性"))
    bullets.append({"k": "汇率流动性", "v": fx_txt + "。"})
    a_txt = "上一交易日上证 %s、创业板 %s，两市成交 %s 亿元，%s。" % (
        fmt_pct(sh.get("pct")), fmt_pct(a_idx.get("创业板指", {}).get("pct")),
        ("%.0f" % (amount / 1e8)) if amount else "—", vol_txt or "量能平稳")
    if zt:
        a_txt += "涨停 %d 家，资金集中在%s。" % (zt["count"], "、".join(b for b, _ in zt["boards"][:2]))
    bullets.append({"k": "A股情绪", "v": a_txt})
    strat = ("外围风险偏好回落，早盘大概率承压，建议不追高，等回踩确认后再布局；"
             "优先内需、低估值与政策受益方向，科技成长待外围企稳" if score <= -0.55 else
             ("外围环境偏暖，可关注科技成长与顺周期的修复窗口，但需警惕高开后的兑现压力"
              if score >= 0.55 else
              "外围多空交织，指数或以震荡为主，控制仓位、均衡配置，等待量能方向选择"))
    bullets.append({"k": "今日策略", "v": strat})
    m["bullets"] = bullets


# ---------------------------------------------------------------- 渲染
CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{width:900px;background:#f4f3ee;font-family:"Microsoft YaHei","PingFang SC",sans-serif;
     color:#1c2b3a;-webkit-font-smoothing:antialiased}
.hero{background:linear-gradient(120deg,#0a1d33 0%,#123c63 55%,#0c274a 100%);color:#fff;
      padding:30px 32px 26px;position:relative;overflow:hidden}
.hero:before{content:"";position:absolute;right:-70px;top:-90px;width:320px;height:320px;
      border-radius:50%;background:radial-gradient(circle,rgba(240,180,41,.32),rgba(240,180,41,.08) 46%,transparent 70%)}
.hero:after{content:"";position:absolute;left:0;right:0;bottom:0;height:4px;
      background:linear-gradient(90deg,transparent,#f0b429 22%,#f6d365 50%,#f0b429 78%,transparent)}
.hero .top{display:flex;justify-content:space-between;align-items:flex-end;position:relative;z-index:1}
.hero h1{font-size:32px;letter-spacing:3px;font-weight:800}
.hero h1:before{content:"";display:inline-block;width:30px;height:30px;margin-right:14px;vertical-align:-4px;
      border-radius:50%;background:radial-gradient(circle at 35% 35%,#f7d878,#e8a33d 72%);
      box-shadow:0 0 0 6px rgba(240,180,41,.14),0 0 26px rgba(240,180,41,.4)}
.hero h1 span{color:#f0b429;font-size:14px;letter-spacing:3px;margin-left:12px;font-weight:500;
      padding-left:12px;border-left:1px solid rgba(255,255,255,.22)}
.hero .date{font-size:13px;color:#9fb6cf;line-height:1.8;text-align:right}
.tagline{margin-top:18px;font-size:14px;line-height:1.7;color:#eef4fb;position:relative;z-index:1;
      background:linear-gradient(90deg,rgba(240,180,41,.16),rgba(255,255,255,.03));
      border-left:3px solid #f0b429;border-radius:0 8px 8px 0;padding:11px 15px}
.wrap{padding:6px 24px 10px}
.sec{margin-top:22px}
.sec-hd{display:flex;align-items:baseline;gap:11px;padding-left:14px;position:relative;margin-bottom:12px}
.sec-hd:before{content:"";position:absolute;left:0;top:2px;bottom:2px;width:5px;border-radius:3px;
      background:linear-gradient(180deg,#f6d365,#e8a33d)}
.sec-hd h2{font-size:20px;letter-spacing:.5px;color:#0e2740;font-weight:700}
.sec-hd em{font-style:normal;font-size:12px;color:#8a97a5}
.grid3{display:flex;gap:12px}
.card{flex:1;background:#fff;border-radius:12px;padding:15px 16px 13px;
      box-shadow:0 4px 14px rgba(15,32,56,.07);position:relative;overflow:hidden}
.card:before{content:"";position:absolute;left:16px;right:16px;top:0;height:3px;border-radius:0 0 3px 3px;
      background:linear-gradient(90deg,#f6d365,#e8a33d)}
.card .n{font-size:13px;color:#5b6672;display:flex;justify-content:space-between}
.card .n i{font-style:normal;font-size:11px;color:#9aa6b4}
.card .p{font-size:26px;font-weight:800;margin:6px 0 3px;letter-spacing:-.5px}
.card .c{font-size:14px;font-weight:700}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.chip{background:#fff;border:1px solid #ece4d3;border-radius:8px;padding:8px 12px;font-size:12.5px;
      color:#3a4a5c;box-shadow:0 1px 4px rgba(15,32,56,.04);font-weight:600}
.chip b{font-weight:800;margin-left:6px}
.tab{background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 14px rgba(15,32,56,.06)}
.tr{display:flex;align-items:center;padding:11px 15px;border-bottom:1px solid #f0ede3;gap:10px}
.tr:last-child{border-bottom:0}
.tr .sig{width:214px;font-size:12.5px;color:#51606f;line-height:1.5}
.tr .map{width:186px;font-size:12.5px;color:#1c2b3a;font-weight:600}
.tr .tag{width:54px;text-align:center;font-size:11.5px;font-weight:700;border-radius:20px;padding:3px 0}
.tr .txt{flex:1;font-size:12px;color:#6b7a89;line-height:1.55}
.stats{display:flex;gap:10px;margin-top:12px}
.stat{flex:1;background:#fff;border-radius:10px;padding:11px 14px;box-shadow:0 3px 10px rgba(15,32,56,.05)}
.stat .k{font-size:11.5px;color:#8a97a5}
.stat .v{font-size:17px;font-weight:800;margin-top:3px;letter-spacing:-.3px}
.stat .s{font-size:11px;color:#9aa6b4;margin-top:2px}
.rows{background:#fff;border-radius:12px;padding:6px 15px 10px;box-shadow:0 4px 14px rgba(15,32,56,.06)}
.rows .rt{font-size:11.5px;color:#9aa6b4;margin:4px 0 6px}
.row{display:flex;align-items:center;gap:9px;padding:5px 0;font-size:12.5px}
.row .nm{width:66px;color:#3a4a5c;white-space:nowrap}
.row .bar{flex:1;height:13px;position:relative;background:#f2f0e9;border-radius:3px}
.row .bar span{position:absolute;top:0;height:13px;border-radius:3px}
.row .v{width:58px;text-align:right;font-weight:700;font-size:12px}
.spark{background:#fff;border-radius:12px;padding:12px 15px 6px;margin-top:12px;
      box-shadow:0 4px 14px rgba(15,32,56,.06);display:flex;align-items:center;gap:12px}
.spark .lbl{width:70px;font-size:12px;color:#5b6672;line-height:1.6}
.spark .lbl b{display:block;font-size:17px;color:#1c2b3a}
.news{display:flex;gap:12px}
.ncol{flex:1;background:#fff;border-radius:12px;padding:13px 15px;box-shadow:0 4px 14px rgba(15,32,56,.06)}
.ncol h3{font-size:13.5px;color:#0e2740;margin-bottom:9px;display:flex;align-items:center;gap:7px;font-weight:700}
.ncol h3:before{content:"";width:8px;height:8px;border-radius:50%;background:linear-gradient(135deg,#f6d365,#e8a33d)}
.ni{padding:8px 0;border-bottom:1px dashed #ece8dd}
.ni:last-child{border-bottom:0}
.ni .t{font-size:12.5px;line-height:1.55;color:#1c2b3a;display:-webkit-box;-webkit-line-clamp:2;
      -webkit-box-orient:vertical;overflow:hidden}
.ni .m{font-size:10.5px;color:#9aa6b4;margin-top:4px;display:flex;gap:8px}
.ni .m b{font-weight:600;color:#d0483e}
.view{background:linear-gradient(180deg,#fff,#fbfaf6);border-radius:12px;padding:14px 16px;
      box-shadow:0 4px 14px rgba(15,32,56,.06)}
.view .vt{display:flex;gap:10px;font-size:12.8px;line-height:1.7;padding:6px 0;color:#3a4a5c}
.view .vt b{width:72px;flex:none;color:#0e2740}
.foot{margin-top:18px;background:linear-gradient(120deg,#0a1d33,#123c63);border-radius:12px;
      padding:14px 16px;font-size:10.8px;color:#b9c8d9;line-height:1.8}
.foot b{color:#f0b429}
"""


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_html(m):
    now = m["generated_at"]
    wk = "星期" + "一二三四五六日"[now.weekday()]
    us_date = m["us"]["session_date"].strftime("%m月%d日")
    a_date_raw = m["a"].get("date") or ""
    a_date = a_date_raw.replace("-", "/")
    a_short = (datetime.strptime(a_date_raw, "%Y-%m-%d").strftime("%m月%d日") if a_date_raw else "")
    parts = []

    # 头部
    parts.append("""
<div class="hero">
  <div class="top">
    <h1>隔夜财经早报<span>OVERNIGHT BRIEF</span></h1>
    <div class="date">%s %s %s<br>数据：A股 %s 收盘 · 美股 %s 收盘</div>
  </div>
  <div class="tagline">%s</div>
</div>
""" % (now.strftime("%Y年%m月%d日"), wk, now.strftime("%H:%M"), a_short, us_date, esc(m["headline"])))

    # 隔夜美股
    cards = []
    for x in m["us"]["indices"]:
        c = color_of(x["pct"])
        chg = x.get("chg")
        if chg is None:
            prev_txt, chg_txt = "昨收 —", "—"
        else:
            prev_txt, chg_txt = "昨收 %.2f" % (x["px"] - chg), "%+.2f" % chg
        cards.append("""
<div class="card"><div class="n"><span>%s</span><i>%s</i></div>
<div class="p" style="color:%s">%s</div>
<div class="c" style="color:%s">%s（%s）</div></div>""" % (
            x["name"], prev_txt, c, "{:,.2f}".format(x["px"]), c,
            fmt_pct(x["pct"]), chg_txt))
    chips = []
    if m["us"].get("sox"):
        s = m["us"]["sox"]
        chips.append('<div class="chip">费城半导体<b style="color:%s">%s</b></div>'
                     % (color_of(s["pct"]), fmt_pct(s["pct"])))
    for a in m["us"].get("adr") or []:
        chips.append('<div class="chip">%s<b style="color:%s">%s</b></div>'
                     % (a["name"], color_of(a["pct"]), fmt_pct(a["pct"])))
    for h in m.get("hk") or []:
        chips.append('<div class="chip">%s %s<b style="color:%s">%s</b></div>'
                     % (h["name"], "{:,.2f}".format(h["px"]), color_of(h["pct"]), fmt_pct(h["pct"])))
    for o in m.get("other") or []:
        px = ("%.4f" % o["px"]) if "人民币" in o["name"] else (
            "{:,.2f}".format(o["px"]) if "指数" in o["name"] or "黄金" in o["name"] else "%.2f" % o["px"])
        chips.append('<div class="chip">%s %s<b style="color:%s">%s</b></div>'
                     % (o["name"], px, color_of(o["pct"]), fmt_pct(o["pct"])))
    sec_us = ('<div class="sec"><div class="sec-hd"><h2>二 · 隔夜美股速览</h2>'
              '<em>美股 %s 收盘 · 港股/商品/汇率为最新</em></div><div class="grid3">%s</div>'
              '<div class="chips">%s</div></div>' % (us_date, "".join(cards), "".join(chips)))

    # 联动
    rows = []
    for r in m["linkage"]["rows"]:
        rows.append("""
<div class="tr"><div class="sig">%s</div><div class="map">%s</div>
<div class="tag" style="color:%s;background:%s1a">%s</div>
<div class="txt">%s</div></div>""" % (esc(r["signal"]), esc(r["map"]), r["color"],
                                       r["color"], r["tone"], esc(r["text"])))
    sec_link = ('<div class="sec"><div class="sec-hd"><h2>三 · 美股 → A股 联动雷达</h2>'
                '<em>风险偏好：%s</em></div><div class="tab">%s</div></div>'
                % (m["linkage"]["label"], "".join(rows)))

    # A股复盘
    acards = []
    for x in m["a"]["indices"]:
        c = color_of(x["pct"])
        acards.append("""
<div class="card"><div class="n"><span>%s</span><i>%s</i></div>
<div class="p" style="color:%s">%s</div>
<div class="c" style="color:%s">%s（%+.2f）</div></div>""" % (
            x["name"], a_date[-5:], c, "{:,.2f}".format(x["px"]), c, fmt_pct(x["pct"]), x["chg"]))
    stats = []
    amount = m["a"].get("amount")
    prev = m["a"].get("amount_prev")
    s_txt = ""
    if amount and prev:
        dlt = amount / prev - 1
        s_txt = ("环比 %+.1f%%" % (dlt * 100))
    stats.append('<div class="stat"><div class="k">两市成交额</div><div class="v">%s</div>'
                 '<div class="s">%s</div></div>'
                 % (("%.2f 万亿" % (amount / 1e12)) if amount else "—", s_txt))
    zt = m["a"].get("limit_up")
    stats.append('<div class="stat"><div class="k">涨停家数</div><div class="v" style="color:%s">%s</div>'
                 '<div class="s">%s</div></div>'
                 % (RED, zt["count"] if zt else "—",
                    "主线：" + "、".join(b for b, _ in zt["boards"][:2]) if zt and zt["boards"] else "—"))
    br = m["a"].get("breadth")
    stats.append('<div class="stat"><div class="k">涨跌家数</div><div class="v">%s</div>'
                 '<div class="s">%s</div></div>'
                 % (("%d : %d" % (br["up"], br["down"])) if br else "—",
                    "赚钱效应%s" % ("偏弱" if br and br["down"] > br["up"] else "尚可") if br else "收盘后更新"))

    sec_rows = []
    sectors = m["a"].get("sectors") or []
    if sectors:
        mx = max(abs(s["pct"]) for s in sectors) or 1
        for s in sectors:
            w = abs(s["pct"]) / mx * 46
            c = color_of(s["pct"])
            left = "50%" if s["pct"] >= 0 else ("%.2f%%" % (50 - w))
            sec_rows.append("""
<div class="row"><div class="nm">%s</div><div class="bar">
<span style="left:%s;width:%.2f%%;background:%s"></span></div>
<div class="v" style="color:%s">%s</div></div>""" % (s["name"], left, w, c, c, fmt_pct(s["pct"])))
        sec_rows.insert(0, '<div class="row" style="color:#98a2b3;font-size:11px">'
                           '<div class="nm"></div><div class="bar" style="background:transparent">'
                           '<span style="left:50%;width:1px;background:#d0d5dd;height:13px"></span></div>'
                           '<div class="v"></div></div>')

    spark = ""
    if m["a"].get("spark"):
        pts = m["a"]["spark"]
        lo, hi = min(pts), max(pts)
        rng = (hi - lo) or 1
        W, H = 640, 96
        step = W / (len(pts) - 1)
        path = " ".join("%.1f,%.1f" % (i * step, H - (v - lo) / rng * (H - 12) - 6)
                        for i, v in enumerate(pts))
        up = pts[-1] >= pts[0]
        col = RED if up else GREEN
        chg20 = (pts[-1] / pts[0] - 1) * 100
        spark = """<div class="spark"><div class="lbl">上证指数<b>%s</b></div>
<svg width="%d" height="%d" viewBox="0 0 %d %d">
<polyline points="%s" fill="none" stroke="%s" stroke-width="1.8"/>
</svg><div class="lbl" style="text-align:right;width:96px">近%d个交易日<br>
<b style="color:%s;font-size:15px">%s</b></div></div>""" % (
            "{:,.2f}".format(pts[-1]), W, H, W, H, path, col,
            len(pts), col, fmt_pct(chg20))
    sec_a = ('<div class="sec"><div class="sec-hd"><h2>一 · 上一交易日A股复盘</h2>'
             '<em>%s 收盘 · 东方财富行业板块</em></div><div class="grid3">%s</div><div class="stats">%s</div>'
             '%s%s</div>' % (a_short, "".join(acards), "".join(stats),
                             ('<div class="rows" style="margin-top:10px">'
                              '<div class="rt">重点行业涨跌幅</div>%s</div>'
                              % "".join(sec_rows)) if sec_rows else "", spark))
    parts.extend([sec_a, sec_us, sec_link])

    # 要闻
    def news_col(title, items):
        rows = []
        for it in items:
            meta = it["time"].strftime("%H:%M")
            extra = ""
            if it["sector"]:
                extra = "<b>%s</b>" % it["sector"]
            rows.append('<div class="ni"><div class="t">%s</div><div class="m"><span>%s</span>%s'
                        '<span>%s</span></div></div>' % (esc(it["title"]), meta, extra, it["tone"]))
        return '<div class="ncol"><h3>%s</h3>%s</div>' % (title, "".join(rows))

    parts.append('<div class="sec"><div class="sec-hd"><h2>四 · 隔夜要闻精选</h2>'
                 '<em>东方财富 7×24 快讯 · 自动筛选+影响标注</em></div><div class="news">%s%s</div></div>'
                 % (news_col("海外市场", m["news"]["overseas"]),
                    news_col("国内市场与产业", m["news"]["domestic"])))

    # 研判
    vt = "".join('<div class="vt"><b>%s</b><span>%s</span></div>' % (b["k"], esc(b["v"]))
                 for b in m["bullets"])
    parts.append('<div class="sec"><div class="sec-hd"><h2>五 · 今日研判</h2><em>规则化推演 · 仅供参考</em>'
                 '</div><div class="view">%s</div></div>' % vt)

    src = "、".join(m["sources"])
    parts.append("""
<div class="wrap"><div class="foot">
<b>数据来源：</b>%s（行情为延时/收盘数据，仅供研究参考）。<b>免责声明：</b>本报告由程序自动生成，
所载信息来自公开渠道，可能存在延迟、遗漏或错误；其中“联动雷达”“今日研判”为基于规则与历史统计的
推演结论，不构成任何投资建议或收益承诺。市场有风险，投资需谨慎，据此操作风险自担。
</div></div>""" % src)

    return ('<!DOCTYPE html><html><head><meta charset="utf-8"><style>%s</style></head><body>'
            '<div class="hero-wrap">%s</div>%s</body></html>' % (CSS, parts[0], "".join(parts[1:])))


def shoot(html_path, png_path, css_height=4200):
    if Path(EDGE).exists():  # Windows：本机 Edge 无头截图
        cmd = [EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
               "--force-device-scale-factor=2", "--window-size=900,%d" % css_height,
               "--screenshot=%s" % str(png_path), html_path.as_uri()]
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
    else:  # Linux / 云端：Playwright 无头 Chromium
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(args=["--no-sandbox"])
            page = b.new_page(viewport={"width": 900, "height": css_height},
                              device_scale_factor=2)
            page.goto(html_path.as_uri())
            page.wait_for_timeout(800)
            page.screenshot(path=str(png_path), full_page=True)
            b.close()
    im = Image.open(png_path).convert("RGB")
    # 裁掉底部空白
    bg = im.getpixel((5, im.height - 5))
    bottom = im.height - 1
    for y in range(im.height - 1, im.height // 3, -1):
        row = im.crop((0, y, im.width, y + 1)).getdata()
        if any(abs(p[0] - bg[0]) + abs(p[1] - bg[1]) + abs(p[2] - bg[2]) > 12 for p in row):
            bottom = y
            break
    bottom = min(im.height, bottom + 56)
    im.crop((0, 0, im.width, bottom)).save(png_path)
    return png_path


PUB_TMPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{TITLE}}</title>
<style>
body{margin:0;background:#f4f3ee;color:#1c2b3a;
  font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:14px}
h1{font-size:18px;line-height:1.5;margin:6px 0 4px}
.tip{font-size:12px;color:#8a97a5;margin:6px 0 10px}
.imgbox{background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 14px rgba(15,32,56,.08)}
.imgbox img{width:100%;display:block}
.btn{display:inline-block;background:#e8a33d;color:#fff;border:0;border-radius:8px;
  padding:9px 14px;font-size:14px;font-weight:600;text-decoration:none;margin:10px 8px 0 0;cursor:pointer}
.sec{background:#fff;border-radius:12px;padding:14px;margin-top:14px;box-shadow:0 4px 14px rgba(15,32,56,.06)}
.lbl{font-size:12px;color:#8a97a5;margin-bottom:6px}
.val{font-size:15px;font-weight:700;line-height:1.6}
pre{white-space:pre-wrap;word-break:break-word;font-size:13.5px;line-height:1.75;margin:0;
  font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.copy{background:#0e2740}
</style></head><body><div class="wrap">
<h1>{{TITLE}}</h1>
<div class="tip">长按下方图片保存到手机相册；也可点“下载早报图”。</div>
<div class="imgbox"><img src="brief.png" alt="隔夜财经早报"></div>
<a class="btn" href="brief.png" download>下载早报图</a>
<div class="sec"><div class="lbl">公众号 · 标题</div><div class="val" id="t">{{TITLE}}</div>
<button class="btn copy" onclick="cp('t')">复制标题</button></div>
<div class="sec"><div class="lbl">公众号 · 摘要</div><div class="val" id="d">{{DIGEST}}</div>
<button class="btn copy" onclick="cp('d')">复制摘要</button></div>
<div class="sec"><div class="lbl">公众号 · 正文（粘贴到订阅号助手）</div><pre id="b">{{BODY}}</pre>
<button class="btn copy" onclick="cp('b')">复制正文</button></div>
<script>
function cp(id){var el=document.getElementById(id);var t=el.innerText||el.textContent;
 if(navigator.clipboard&&navigator.clipboard.writeText){
   navigator.clipboard.writeText(t).then(function(){alert('已复制，去订阅号助手粘贴')},
   function(){fb(t)});}else{fb(t)}}
function fb(t){var a=document.createElement('textarea');a.value=t;a.style.position='fixed';
 a.style.opacity='0';document.body.appendChild(a);a.focus();a.select();
 try{document.execCommand('copy');alert('已复制，去订阅号助手粘贴')}catch(e){alert('复制失败，请手动选择复制')}
 document.body.removeChild(a)}
</script>
</div></body></html>"""


def build_article(m):
    """从数据模型生成公众号发布素材：标题 / 摘要 / 正文。"""
    a, us = m["a"], m["us"]
    a_date = a.get("date") or ""
    a_short = datetime.strptime(a_date, "%Y-%m-%d").strftime("%m月%d日") if a_date else ""
    sh = next((x for x in a["indices"] if "上证" in x["name"]), None)
    if sh and sh.get("pct") is not None:
        title = "%s隔夜财经早报｜A股%s 上证%s" % (
            a_short, "收涨" if sh["pct"] >= 0 else "收跌", fmt_pct(sh["pct"]))
    else:
        title = "%s隔夜财经早报" % a_short
    title = title[:64]
    digest = (m.get("headline") or "")[:118]

    L = []
    L.append("【上一交易日 A股复盘 · %s】" % a_short)
    for x in a["indices"]:
        px = "{:,.2f}".format(x["px"]) if x.get("px") else "-"
        L.append("· %s %s 点，%s" % (x["name"], px, fmt_pct(x.get("pct"))))
    L.append("")
    L.append("【隔夜美股 · %s 收盘】" % us["session_date"].strftime("%m月%d日"))
    for x in us["indices"]:
        px = "{:,.2f}".format(x["px"]) if x.get("px") else "-"
        L.append("· %s %s 点，%s" % (x["name"], px, fmt_pct(x.get("pct"))))
    extra = []
    if us.get("sox"):
        extra.append("费城半导体 %s" % fmt_pct(us["sox"]["pct"]))
    for o in (m.get("other") or []):
        extra.append("%s %s" % (o["name"], fmt_pct(o["pct"])))
    if extra:
        L.append("· " + "，".join(extra))
    L.append("")
    L.append("【美股 → A股 联动】")
    for r in m["linkage"]["rows"]:
        L.append("· %s（%s）：%s" % (r["map"], r["tone"], r["text"]))
    L.append("")
    L.append("【隔夜要闻】")
    for it in (m["news"]["overseas"][:3] + m["news"]["domestic"][:3]):
        L.append("· " + it["title"])
    L.append("")
    L.append("【今日研判】")
    for b in m["bullets"]:
        L.append("· %s：%s" % (b["k"], b["v"]))
    L.append("")
    L.append("【数据与免责】")
    L.append("数据来源：%s（延时/收盘数据）。" % "、".join(m["sources"]))
    L.append("本报告由程序自动生成，仅供研究参考，不构成投资建议；市场有风险，投资需谨慎。")
    return {"title": title, "digest": digest, "body": "\n".join(L)}


def write_publish_page(png_path, art, out_dir):
    """生成手机端发布页：内嵌早报图 + 可一键复制的公众号文案。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(png_path, out_dir / "brief.png")
    html = (PUB_TMPL
            .replace("{{TITLE}}", esc(art["title"]))
            .replace("{{DIGEST}}", esc(art["digest"]))
            .replace("{{BODY}}", esc(art["body"])))
    page = out_dir / "index.html"
    page.write_text(html, encoding="utf-8")
    return page


OFF_TMPL = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>隔夜财经早报</title>
<style>body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:#0a1d33;color:#eef4fb;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.box{text-align:center;padding:24px}.t{font-size:22px;font-weight:800;letter-spacing:2px;color:#f0b429}
.s{margin-top:12px;font-size:14px;color:#9fb6cf;line-height:1.8}</style></head>
<body><div class="box"><div class="t">隔夜财经早报</div>
<div class="s">%s 为非交易日，今日休市，暂无早报</div></div></body></html>"""


def _write_offline_page(out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    d = datetime.now().strftime("%Y年%m月%d日")
    (out_dir / "index.html").write_text(OFF_TMPL % d, encoding="utf-8")


def should_generate(now=None):
    """判断今天是否需要生成：周一~周五交易日生成；周五是交易日则周六也生成。"""
    now = now or datetime.now()
    today = now.date()
    wd = today.weekday()  # 0=周一 ... 5=周六 6=周日
    if wd == 6:
        return False
    if _cc is None:
        return True  # 无节假日库时退化为周一~周六均生成
    try:
        if wd == 5:  # 周六：看周五是否交易日
            return _cc.is_workday(today - timedelta(days=1))
        return _cc.is_workday(today)  # 周一~周五
    except Exception:
        return True  # 节假日数据缺失年份时退化为周一~周六均生成


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html-only", action="store_true")
    ap.add_argument("--tag", default=datetime.now().strftime("%Y%m%d"))
    args = ap.parse_args()

    if not should_generate():
        print("[skip] 今日非A股交易日，跳过生成")
        _write_offline_page(BASE / "public")
        return

    m = collect()
    stamp = m["generated_at"].strftime("%Y%m%d_%H%M")
    (REPORT_DIR / ("model_%s.json" % stamp)).write_text(
        json.dumps(m, ensure_ascii=False, default=str, indent=1), encoding="utf-8")
    html = build_html(m)
    hp = REPORT_DIR / ("report_%s.html" % stamp)
    hp.write_text(html, encoding="utf-8")
    if args.html_only:
        print(hp)
        return
    png = REPORT_DIR / ("财经日报_%s.png" % m["generated_at"].strftime("%Y%m%d"))
    shoot(hp, png)
    print(png)

    # 公众号发布素材 + 手机端发布页
    art = build_article(m)
    page = write_publish_page(png, art, BASE / "public")
    (BASE / "public" / "article.txt").write_text(
        "标题：%s\n\n摘要：%s\n\n%s" % (art["title"], art["digest"], art["body"]),
        encoding="utf-8")
    print(page)


if __name__ == "__main__":
    main()
