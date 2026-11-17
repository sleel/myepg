#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
山西联通 IPTV 12 源容错聚合 EPG 生成器（独立公开仓库版 · Windows / Linux LXC / GitHub Actions 通用，零第三方依赖）

支持两种输入：
  1. 脱敏频道清单 JSON (-c channels_epg.json，默认，不含任何账号/AuthInfo/内网IP，适合公开仓库)
  2. 完整 M3U 文件   (-m atv_catchup.m3u，本地调试或私有仓库兼容模式)

12 源分级机制（默认仅启用前 5 个主力源；任意主力失败/空数据时按备选优先级自动唤醒其余 7 个备用源容错）：
  [主力启用 5 源]
  1. gsls_e9    : 主力全覆盖（过去7天 + 今明）
  2. erw        : 补全山西本省 4 套及文化精品/生活时尚/东方财经等 13 台完整 7 天
  3. epgpw_cn   : 独家补充 太原1 / 太原2 / 太原教育 / 优优宝贝
  4. tak_112114 : 独家补充 太原3（太原社教法制）
  5. zsdc       : 轻量兜底主力源
  [备用待命 7 源]
  6~12. gsls_e7 / gsls_e / tak_51zmte1 / tak_51zmte2 / tak_51zmt / epgpw_hk / epgpw_tw（加 --all-sources 可强制全拉）

生成裁剪规则：
  - 7天回看 (catchup-days="7") : 保留过去7天+今+明（若明日无真实预告则留空），过去7天缺日按 12×2h(精彩节目) 补满
  - 仅3~4h时移 (catchup-days="1"): 有真实节目则仅保留「今日+明日(无则空)」；全网无EPG则仅生成「今日 12×2h(精彩节目)」
  - 纯直播 (无 catchup)          : 仅保留真实「今日+明日(无则空)」，绝不造假占位
"""

import argparse
import datetime
import gzip
import io
import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET

BJT = datetime.timezone(datetime.timedelta(hours=8))

# 全量 12 源配置：(源标识, 下载地址, 是否默认启用为主力源)
SOURCE_REGISTRY = [
    ("gsls_e9", "https://gitee.com/gsls200808/xmltvepg/raw/master/e9.xml.gz", True),
    ("erw", "https://gitee.com/taksssss/tv/raw/main/epg/erw.xml.gz", True),
    ("epgpw_cn", "https://gitee.com/taksssss/tv/raw/main/epg/epgpw_cn.xml.gz", True),
    ("tak_112114", "https://gitee.com/taksssss/tv/raw/main/epg/112114.xml.gz", True),
    ("zsdc", "https://epg.zsdc.eu.org/t.xml.gz", True),
    ("gsls_e7", "https://gitee.com/gsls200808/xmltvepg/raw/master/e7.xml.gz", False),
    ("gsls_e", "https://gitee.com/gsls200808/xmltvepg/raw/master/e.xml.gz", False),
    ("tak_51zmte1", "https://gitee.com/taksssss/tv/raw/main/epg/51zmte1.xml.gz", False),
    ("tak_51zmte2", "https://gitee.com/taksssss/tv/raw/main/epg/51zmte2.xml.gz", False),
    ("tak_51zmt", "https://gitee.com/taksssss/tv/raw/main/epg/51zmt.xml.gz", False),
    ("epgpw_hk", "https://gitee.com/taksssss/tv/raw/main/epg/epgpw_hk.xml.gz", False),
    ("epgpw_tw", "https://gitee.com/taksssss/tv/raw/main/epg/epgpw_tw.xml.gz", False),
]

PUBLIC_SOURCES = [(name, url) for name, url, enabled in SOURCE_REGISTRY if enabled]
BACKUP_SOURCES = [(name, url) for name, url, enabled in SOURCE_REGISTRY if not enabled]

# 各主力源失效时的优先替补顺序（其余未列出备用源随后补位）
FAILOVER_PREFS = {
    "gsls_e9": ["gsls_e7", "gsls_e", "tak_51zmte1", "tak_51zmt", "tak_51zmte2"],
    "erw": ["tak_51zmte1", "tak_51zmt", "tak_51zmte2", "gsls_e7"],
    "epgpw_cn": ["epgpw_hk", "epgpw_tw", "tak_51zmt", "tak_51zmte1"],
    "tak_112114": ["tak_51zmt", "tak_51zmte1", "tak_51zmte2", "gsls_e7"],
    "zsdc": ["gsls_e7", "tak_51zmt", "gsls_e"],
}

MANUAL_ALIASES = {
    "文化精品": ["文化精品", "CCTV央视文化精品", "央视文化精品", "央视精品"],
    "黄河卫视": ["黄河卫视", "黄河卫视高清", "黄河电视台", "黄河卫视标清"],
    "山西经济": ["山西经济", "山西经济与科技", "山西经济资讯"],
    "山西社会法治": ["山西社会法治", "山西社会与法治", "山西社会法制"],
    "山西卫视": ["山西卫视", "山西卫视高清", "山西卫视标清"],
    "山西文体生活": ["山西文体生活"],
    "山西影视": ["山西影视"],
    "国学频道": ["国学频道", "国学"],
    "梨园频道": ["梨园频道", "梨园", "河南梨园"],
    "书画频道": ["书画频道", "书画"],
    "中央新影老故事": ["中央新影老故事", "老故事"],
    "CETV1": ["CETV1", "CETV-1", "中国教育1台", "中国教育一套"],
    "CETV2": ["CETV2", "CETV-2", "中国教育2台"],
    "CETV4": ["CETV4", "CETV-4", "中国教育4台"],
    "CETV早期教育": ["CETV早期教育", "早期教育"],
    "CGTN英语": ["CGTN英语", "CGTN", "CGTN标清", "CGTN (中國環球電視網)英語頻道"],
    "CGTN纪录": ["CGTN纪录", "CGTNDocumentary", "CGTN (中國環球電視網)記錄頻道"],
    "CGTN西语": ["CGTN西语", "CGTN西班牙语"],
    "CGTN阿语": ["CGTN阿语", "CGTN阿拉伯语"],
    "太原1": ["太原1", "太原新闻综合", "太原一套"],
    "太原2": ["太原2", "太原百姓", "太原经济生活", "太原二套"],
    "太原3": ["太原3", "太原社教法制", "太原影视", "太原三套"],
    "太原教育": ["太原教育", "太原文体"],
    "央广购物": ["央广购物", "央广购物标清"],
    "优购物": ["优购物"],
    "聚鲨环球精选": ["聚鲨环球精选", "聚鲨环球精选标清"],
    "北京卫视4K": ["北京卫视4K", "北京卫视"],
    "东方卫视4K": ["东方卫视4K", "东方卫视"],
    "广东卫视4K": ["广东卫视4K", "广东卫视"],
    "湖南卫视4K": ["湖南卫视4K", "湖南卫视"],
    "江苏卫视4K": ["江苏卫视4K", "江苏卫视"],
    "山东卫视4K": ["山东卫视4K", "山东卫视"],
    "深圳卫视4K": ["深圳卫视4K", "深圳卫视"],
    "四川卫视4K": ["四川卫视4K", "四川卫视"],
    "浙江卫视4K": ["浙江卫视4K", "浙江卫视"],
    "凤凰中文HD": ["凤凰中文HD", "凤凰卫视中文台", "凤凰中文"],
    "凤凰资讯HD": ["凤凰资讯HD", "凤凰卫视资讯台", "凤凰资讯"],
    "凤凰香港HD": ["凤凰香港HD", "凤凰卫视香港台", "凤凰香港"],
    "新动漫": ["新动漫"],
}


def normalize_name(s: str) -> str:
    """规范化频道名以支持宽松模糊匹配。"""
    if not s:
        return ""
    n = s.upper()
    n = re.sub(r"[\s\-_（）\(\)\[\]【】]", "", n)
    n = re.sub(r"(超高清|高清|标清|HD|4K|频道|电视台|台)$", "", n)
    n = re.sub(r"[与及]", "", n)
    return n


def parse_m3u(m3u_path: str) -> dict:
    """解析 M3U，按唯一 tvg-name（若无则用频道标题）合并聚合元数据。"""
    channels = {}
    with open(m3u_path, "r", encoding="utf-8-sig", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("#EXTINF:"):
                continue
            m_tvg = re.search(r'tvg-name="([^"]*)"', line)
            m_grp = re.search(r'group-title="([^"]*)"', line)
            m_days = re.search(r'catchup-days="([^"]*)"', line)
            tvg_name = m_tvg.group(1).strip() if m_tvg else ""
            group = m_grp.group(1).strip() if m_grp else "其他"
            title = line.split(",")[-1].strip()
            cid = tvg_name or title
            if not cid:
                continue
            has_catchup = "catchup=" in line
            cdays = int(m_days.group(1)) if m_days else (7 if has_catchup else 0)
            if cid not in channels:
                channels[cid] = {
                    "id": cid,
                    "group": group,
                    "titles": [title],
                    "aliases": [cid],
                    "catchup": has_catchup,
                    "cdays": cdays,
                }
            else:
                if has_catchup:
                    channels[cid]["catchup"] = True
                if cdays > channels[cid]["cdays"]:
                    channels[cid]["cdays"] = cdays
                if title not in channels[cid]["titles"]:
                    channels[cid]["titles"].append(title)
            if title and title not in channels[cid]["aliases"]:
                channels[cid]["aliases"].append(title)
    return channels


def load_channels_config(config_path: str) -> dict:
    """加载脱敏 JSON 频道配置文件（按列表顺序还原字典）。"""
    with open(config_path, "r", encoding="utf-8") as f:
        items = json.load(f)
    return {item["id"]: item for item in items}


def export_channels_config(m3u_path: str, config_path: str) -> dict:
    """从已校验 M3U 提取零隐私频道清单并保存为 JSON。"""
    channels = parse_m3u(m3u_path)
    items = list(channels.values())
    parent = os.path.dirname(os.path.abspath(config_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(config_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    return channels


def fetch_source_xml(sname: str, surl: str, cache_dir: str = "", cache_hours: float = 0.0, timeout: int = 30) -> bytes:
    """抓取（或读取未过期的本地缓存）并自动解压 gzip XML。"""
    cache_file = os.path.join(cache_dir, f"{sname}.xml") if cache_dir else ""
    if cache_file and cache_hours > 0 and os.path.exists(cache_file):
        age_h = (time.time() - os.path.getmtime(cache_file)) / 3600.0
        if age_h < cache_hours and os.path.getsize(cache_file) > 1024:
            print(f"[{sname}] 使用本地缓存 ({age_h:.1f}h 前)")
            with open(cache_file, "rb") as f:
                return f.read()

    print(f"[{sname}] 正在下载 {surl} ...")
    last_err = None
    for _ in range(2):
        try:
            req = urllib.request.Request(surl, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            xml_bytes = gzip.decompress(raw) if len(raw) >= 2 and raw[0] == 0x1F and raw[1] == 0x8B else raw
            if cache_file and cache_hours > 0:
                os.makedirs(cache_dir, exist_ok=True)
                with open(cache_file, "wb") as f:
                    f.write(xml_bytes)
            return xml_bytes
        except Exception as exc:
            last_err = exc
            time.sleep(1.0)
    if cache_file and os.path.exists(cache_file):
        print(f"[{sname}] 下载失败 ({last_err})，回退使用旧缓存")
        with open(cache_file, "rb") as f:
            return f.read()
    raise last_err


def make_synthetic_day(dstr: str) -> list:
    """为缺失真实节目单的日期生成每天 12 个 2 小时时段（精彩节目）。"""
    dt = datetime.datetime.strptime(dstr, "%Y%m%d")
    next_dstr = (dt + datetime.timedelta(days=1)).strftime("%Y%m%d")
    slots = []
    for h in range(0, 24, 2):
        hend = h + 2
        start_s = f"{dstr}{h:02d}0000 +0800"
        stop_s = f"{dstr}{hend:02d}0000 +0800" if hend < 24 else f"{next_dstr}000000 +0800"
        slots.append({
            "start": start_s,
            "stop": stop_s,
            "title": "精彩节目",
            "desc": "",
        })
    return slots


def _index_source_xml(xml_bytes: bytes):
    """流式索引单个 XMLTV 源的频道别名与按日分组节目表。"""
    name_to_cids, norm_to_cids, cid_to_primary, progs_by_scid = {}, {}, {}, {}
    for _, elem in ET.iterparse(io.BytesIO(xml_bytes), events=("end",)):
        if elem.tag == "channel":
            scid = elem.attrib.get("id", "")
            for dn_el in elem.findall("display-name"):
                dn = (dn_el.text or "").strip()
                if dn:
                    cid_to_primary.setdefault(scid, dn)
                    if scid not in name_to_cids.setdefault(dn, []):
                        name_to_cids[dn].append(scid)
                    norm = normalize_name(dn)
                    if norm and scid not in norm_to_cids.setdefault(norm, []):
                        norm_to_cids[norm].append(scid)
            elem.clear()
        elif elem.tag == "programme":
            scid = elem.attrib.get("channel", "")
            st = re.sub(r"\+0000$", "+0800", elem.attrib.get("start", ""))
            sp = re.sub(r"\+0000$", "+0800", elem.attrib.get("stop", ""))
            if scid and len(st) >= 8:
                t_el = elem.find("title")
                d_el = elem.find("desc")
                title = (t_el.text or "").strip() if t_el is not None else "精彩节目"
                desc = (d_el.text or "").strip() if d_el is not None else ""
                progs_by_scid.setdefault(scid, {}).setdefault(st[:8], []).append({
                    "start": st,
                    "stop": sp,
                    "title": title or "精彩节目",
                    "desc": desc,
                })
            elem.clear()
    return name_to_cids, norm_to_cids, cid_to_primary, progs_by_scid


def _match_channel_ids(cid: str, aliases: list, name_to_cids: dict, norm_to_cids: dict, cid_to_primary: dict):
    """按 精确 -> 手工别名 -> 规范化模糊 三级匹配源站 channel id。"""
    mcids, mname, way = [], "", ""
    for al in aliases:
        if al in name_to_cids:
            for c in name_to_cids[al]:
                if c not in mcids:
                    mcids.append(c)
            if not way:
                mname, way = al, "精确"
    if not mcids:
        for ma in MANUAL_ALIASES.get(cid, []):
            if ma in name_to_cids:
                for c in name_to_cids[ma]:
                    if c not in mcids:
                        mcids.append(c)
                if not way:
                    mname, way = ma, "别名"
    if not mcids:
        norm = normalize_name(cid)
        if len(norm) >= 2 and norm in norm_to_cids:
            for c in norm_to_cids[norm]:
                if c not in mcids:
                    mcids.append(c)
                if not mname:
                    mname = cid_to_primary.get(c, norm)
            if not way:
                way = "模糊"
    return mcids, mname, way


def _apply_source_xml(
    sname: str,
    surl: str,
    channels: dict,
    data_store: dict,
    primary_hit: dict,
    sample_title: dict,
    min_7d_keep: str,
    today_s: str,
    tomorrow_s: str,
    cache_dir: str,
    cache_hours: float,
):
    """下载并解析单个 XMLTV 源，填入仍缺日的频道数据。"""
    xml_bytes = fetch_source_xml(sname, surl, cache_dir=cache_dir, cache_hours=cache_hours)
    name_to_cids, norm_to_cids, cid_to_primary, progs_by_scid = _index_source_xml(xml_bytes)
    if not progs_by_scid:
        raise ValueError(f"{sname} 返回空 XMLTV 节目数据")

    for cid, info in channels.items():
        allowed_min = min_7d_keep if (info["catchup"] and info["cdays"] >= 7) else today_s
        mcids, mname, way = _match_channel_ids(
            cid, info["aliases"], name_to_cids, norm_to_cids, cid_to_primary
        )
        added = False
        for scid in mcids:
            scid_days = progs_by_scid.get(scid, {})
            for dkey, plist in scid_days.items():
                if allowed_min <= dkey <= tomorrow_s and dkey not in data_store[cid] and plist:
                    data_store[cid][dkey] = plist
                    added = True
            if cid not in sample_title and today_s in scid_days:
                for p in scid_days[today_s]:
                    if len(p["start"]) >= 12 and p["start"][8:12] >= "1800":
                        sample_title[cid] = f"{p['start'][8:10]}:{p['start'][10:12]} {p['title']}"
                        break
                if cid not in sample_title and scid_days[today_s]:
                    p0 = scid_days[today_s][0]
                    sample_title[cid] = f"{p0['start'][8:10]}:{p0['start'][10:12]} {p0['title']}"
        if added and cid not in primary_hit:
            primary_hit[cid] = {"src": sname, "name": mname, "way": way}


def _build_failover_queue(failed_primaries: list) -> list:
    """根据失效的主力源名生成去重后的备用源激活队列（先对应替补，后剩余备用）。"""
    backup_map = dict(BACKUP_SOURCES)
    ordered = []
    seen = set()
    for p in failed_primaries:
        for bname in FAILOVER_PREFS.get(p, []):
            if bname in backup_map and bname not in seen:
                seen.add(bname)
                ordered.append((bname, backup_map[bname]))
    for bname, burl in BACKUP_SOURCES:
        if bname not in seen:
            seen.add(bname)
            ordered.append((bname, burl))
    return ordered


def run_build(
    channels: dict,
    out_xml: str,
    report_path: str = "",
    cache_dir: str = "",
    cache_hours: float = 0.0,
    all_sources: bool = False,
) -> dict:
    """执行 12 源分级聚合（默认仅拉主力 5 源，异常时自动唤醒备用 7 源），裁剪/补齐并输出 XMLTV。"""
    today = datetime.datetime.now(BJT).date()
    today_s = today.strftime("%Y%m%d")
    tomorrow_s = (today + datetime.timedelta(days=1)).strftime("%Y%m%d")
    window_7d = [(today + datetime.timedelta(days=d)).strftime("%Y%m%d") for d in range(-6, 1)]
    min_7d_keep = (today - datetime.timedelta(days=7)).strftime("%Y%m%d")

    data_store = {cid: {} for cid in channels}
    primary_hit = {}
    sample_title = {}

    active_sources = [(n, u) for n, u, _ in SOURCE_REGISTRY] if all_sources else PUBLIC_SOURCES
    failed_primaries = []
    for sname, surl in active_sources:
        try:
            _apply_source_xml(
                sname, surl, channels, data_store, primary_hit, sample_title,
                min_7d_keep, today_s, tomorrow_s, cache_dir, cache_hours,
            )
        except Exception as exc:
            print(f"[{sname}] 拉取/解析失败 ({exc})，已跳过")
            failed_primaries.append(sname)

    used_backups = []
    if failed_primaries and not all_sources:
        failover_queue = _build_failover_queue(failed_primaries)
        print(f"[EPG 容错] 主力源 {failed_primaries} 异常，自动唤醒 {len(failover_queue)} 个备用源补救...")
        for bname, burl in failover_queue:
            try:
                _apply_source_xml(
                    bname, burl, channels, data_store, primary_hit, sample_title,
                    min_7d_keep, today_s, tomorrow_s, cache_dir, cache_hours,
                )
                used_backups.append(bname)
            except Exception as exc:
                print(f"[{bname}] 备用源跳过 ({exc})")

    mode_desc = "全量 12 源" if all_sources else (
        f"主力 5 源（故障自动切换启用备用: {', '.join(used_backups)}）" if used_backups else "主力 5 源启用（7 源备用待命，全量共 12 源）"
    )
    epg_summary = {}
    report_lines = [
        f"# 山西联通 IPTV EPG 匹配结果（{mode_desc}）",
        "",
        "| # | 分组 | M3U 频道名 | `tvg-name` | 类型 | 主 EPG 源 | 源内名称 (`display-name`) | 匹配 | 7天实有 | 今日节目抽样 | 裁剪/补齐策略 |",
        "| :---: | :--- | :--- | :--- | :---: | :--- | :--- | :---: | :---: | :--- | :--- |",
    ]

    for idx, (cid, info) in enumerate(channels.items(), 1):
        real_in_7 = sum(1 for wd in window_7d if data_store[cid].get(wd))
        has_today_real = bool(data_store[cid].get(today_s))
        has_tomorrow_real = bool(data_store[cid].get(tomorrow_s))
        hit = primary_hit.get(cid, {"src": "-", "name": "-", "way": "无源"})
        smp = sample_title.get(cid, "-")

        if not info["catchup"]:
            mode_text = "纯直播"
            if has_today_real and has_tomorrow_real:
                strategy = "今日+明日真实"
            elif has_today_real:
                strategy = "仅今日真实(无明日预告)"
            else:
                strategy = "无源留空"
        elif info["cdays"] == 1:
            mode_text = "仅3~4h时移"
            if has_today_real:
                strategy = "今日+明日真实" if has_tomorrow_real else "今日真实(明日空白)"
            else:
                data_store[cid][today_s] = make_synthetic_day(today_s)
                strategy = "无源:仅生成今日12×2h(明日空白)"
        else:
            mode_text = "7天回看"
            for dstr in window_7d:
                if not data_store[cid].get(dstr):
                    data_store[cid][dstr] = make_synthetic_day(dstr)
            if real_in_7 >= 7:
                strategy = "完整7天真实" + ("+明日" if has_tomorrow_real else "")
            elif real_in_7 > 0:
                strategy = f"真实{real_in_7}天+缺{7 - real_in_7}天12×2h补齐"
            else:
                strategy = "全7天12×2h(精彩节目)补齐"

        epg_summary[cid] = {
            "src": hit["src"],
            "epg_name": hit["name"],
            "way": hit["way"],
            "real_days": real_in_7 if info["cdays"] >= 7 else (1 if has_today_real else 0) + (1 if has_tomorrow_real else 0),
            "sample": smp,
            "strategy": strategy,
        }
        titles_text = "/".join(info["titles"])
        report_lines.append(
            f"| {idx} | {info['group']} | {titles_text} | `{cid}` | {mode_text} | "
            f"`{hit['src']}` | `{hit['name']}` | {hit['way']} | {real_in_7}/7 | {smp} | {strategy} |"
        )

    if report_path:
        with open(report_path, "w", encoding="utf-8", newline="\n") as rf:
            rf.write("\n".join(report_lines) + "\n")

    out_dir = os.path.dirname(os.path.abspath(out_xml))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    root = ET.Element("tv", {"generator-info-name": "Shanxi-Unicom-EPG-12Src"})
    for cid, info in channels.items():
        ch_el = ET.SubElement(root, "channel", {"id": cid})
        for al in info["aliases"]:
            dn_el = ET.SubElement(ch_el, "display-name", {"lang": "zh"})
            dn_el.text = al

    total_progs = 0
    for cid in channels:
        for dkey in sorted(data_store[cid].keys()):
            for p in data_store[cid][dkey]:
                attrs = {"start": p["start"], "channel": cid}
                if p.get("stop"):
                    attrs["stop"] = p["stop"]
                p_el = ET.SubElement(root, "programme", attrs)
                t_el = ET.SubElement(p_el, "title", {"lang": "zh"})
                t_el.text = p["title"]
                if p.get("desc"):
                    d_el = ET.SubElement(p_el, "desc", {"lang": "zh"})
                    d_el.text = p["desc"]
                total_progs += 1

    ET.indent(root, space="  ")
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    with open(out_xml, "wb") as f:
        f.write(xml_bytes)
    with gzip.open(out_xml + ".gz", "wb") as f:
        f.write(xml_bytes)

    print(f"[EPG] 已生成 {out_xml} ({len(xml_bytes)} 字节, {total_progs} 条节目) 及 {out_xml}.gz")
    return {"channels": channels, "epg_summary": epg_summary, "total_progs": total_progs, "xml_bytes": len(xml_bytes)}


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="山西联通 IPTV 12 源容错聚合 EPG 生成器（默认仅启用 5 主力，异常时自动唤醒 7 备用）")
    parser.add_argument("-c", "--config", default=os.path.join(base_dir, "channels_epg.json"), help="脱敏 JSON 频道清单（默认）")
    parser.add_argument("-m", "--m3u", default="", help="可选：直接从 M3U 读取频道列表（若指定则覆盖 -c）")
    parser.add_argument("-o", "--output", default=os.path.join(base_dir, "epg.xml"), help="输出 XMLTV 路径")
    parser.add_argument("-r", "--report", default="epg_report.md", help="匹配报告路径")
    parser.add_argument("--no-report", action="store_true", help="不单独输出报告文件")
    parser.add_argument("--cache-dir", default=os.path.join(base_dir, ".epg_cache"), help="本地 XML 缓存目录")
    parser.add_argument("--cache-hours", type=float, default=0.0, help="缓存有效期（小时，默认 0 每次拉最新）")
    parser.add_argument("--all-sources", action="store_true", help="强制同时拉取全部 12 源（默认仅主力 5 源 + 异常自动容错备用 7 源）")
    args = parser.parse_args()

    channels = parse_m3u(args.m3u) if args.m3u else load_channels_config(args.config)
    run_build(channels, args.output, "" if args.no_report else args.report, args.cache_dir, args.cache_hours, args.all_sources)


if __name__ == "__main__":
    main()
