#!/usr/bin/env python3
"""入口甄别 —— 批量证明「这个 URL 真的执行了检索」，再拿它去跑证据。

批量留痕最贵的一类错，是拿一个**根本没检索**的入口跑完全批：页面上有"公示"
"公告""搜索"字样，PDF 打印得漂漂亮亮，判定器给出"无记录"，而它其实是首页。
这个错是静默的——不放对照组，复核时也看不出来。

判据：拿一个**杜撰的、不存在的主体名**去打每个入口，看返回什么。

  精确型  明确报空（「没有找到」「未能搜到」）→ 入口可信
  分词型  杜撰名竟然有结果 → 站点做分词匹配，非零结果一律不算命中
  待定    既不报空也无计数 → 疑参数被忽略后渲染首页，交 runner 的相似度闸
  报错型  404 / 空壳 / 403 → 入口本身错的，重新找，绝不能当"无记录"

对照组只读正文文本、不打印 PDF，杜撰名不会进底稿。

用法：
  python3 entry_triage.py entries.json                  # 甄别 + 分级
  python3 entry_triage.py entries.json --redo           # 忽略已有结果重跑
  python3 entry_triage.py entries.json --grade-only     # 只用已存数据重新分级

entries.json 支持两种写法：
  ["https://a.gov.cn/so/s?qt={kw}", ...]
  {"https://a.gov.cn/so/s?qt={kw}": {"备注": "某市门户"}, ...}
"""
from __future__ import annotations

import datetime, json, os, random, re, sys, time, urllib.parse

from capture import connect
from evidence_core import EMPTY_PHRASES, atomic_write_json, bad_page_reason, compact

# 杜撰的主体名。要求：字形合法、可被分词器切开、但组合起来全网不存在。
# 不要用真词或真公司的片段，否则命中真实结果，对照就失效了。
CONTROL = os.environ.get("EVIDENCE_CONTROL_NAME", "耒阳砯砼泫铘信息技术有限公司")

# 第二个对照词：只保留被查主体的**行业词尾**。全生僻字的对照词在 AND 型引擎上
# 必然 0 结果，测不出分词；用行业词尾一打就露馅——中国证监会 guestweb4 实测
# 「湖北某某光电科技有限公司」42 条，「光电科技有限公司」同样 42 条，
# 说明完整名里的区分部分一个字都没参与匹配。传 --suffix=光电科技有限公司 启用。
SUFFIX_CONTROL = None

# 检索界面标记。真正的「空结果页」一定还挂着检索控件——排序、范围、时间、
# 高级搜索这些。而一张 404 页可能同样写着「没有找到」：上海市消防救援总队的
# websearch 路径返回的正文是「…首页 机构 新闻 政策 公开 互动 加入我们
# 上海消防博物馆 404页面没有找到 主办单位…」，只有站点导航、没有任何检索控件。
# 光靠空结果措辞会把它判成「入口可信」，进而把一个坏入口的产出写成「无记录」。
# 这条判据同时服务于判定纪律第 8 条：检索字段本身就是证据契约的一部分。
SEARCH_UI = (
    # 结果区标题——最直接的证据，说明这块内容是检索产物
    "搜索结果", "检索结果", "查询结果", "结果列表",
    # 检索控件——排序、范围、时间筛选这些只会长在结果页上
    "高级搜索", "智能排序", "按相关度", "结果排序", "搜索位置", "排序：",
    "范围：", "在结果中搜索", "本站搜索", "站内检索", "全文检索",
    "检索词", "搜索历史", "热门搜索", "搜索热词", "时间不限", "全部时间",
)

# 前缀与数字之间要允许冒号/空格：七台河市政府门户写的是「相关结果:0个」，
# 少了 [:：\s]* 就匹配不到，一张真正的空结果页会被判成「待定」，
# 再被 runner 的相似度闸当成首页拦掉——把有效证据丢了，比漏判还难发现。
COUNT = re.compile(
    r"(?:共|找到|检索到|筛选到|相关结果)[:：\s]*(?:约)?([0-9,，]+)\s*(?:条|篇|个|项)")
# 中文错误措辞可以在正文里认；**数字状态码只能在标题/URL 里认**。
# 正文里的 404/403 极可能是正常内容：上海市消防救援总队的检索结果页导航里有
# 「上海消防博物馆404」之类的条目，一份明确报空的正常结果页被判成了报错型。
# 同类事故见 pitfalls.md：新闻链接哈希 …bbdc429dad33… 里的 429。
ERR_BODY = ("请求参数错误", "页面无法访问", "系统繁忙")
ERR_HEAD = ("404", "403", "not found", "bad gateway", "服务不可用")
# ssp / aisp_ssp 系统：URL 带关键词打开后不会自动检索，必须点「搜一下」
SSP_CLICK = ("button:has-text('搜一下')", "a:has-text('搜一下')",
             "input[value*='搜']", "[class*=search-btn]")


def log(m):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {m}", flush=True)


def flat(t: str) -> str:
    """只去空白，数字全保留。分级要靠「共 N 条」里的 N，不能先把它抹掉。"""
    return re.sub(r"\s+", "", t or "")


def norm(t: str) -> str:
    """相似度比对专用：再把长数字串抹成 #。

    时间戳/浏览量每次加载都变，不抹掉同一页面自己跟自己都不相等。但**分级绝不
    能用这份文本**：四位以上的结果计数会一起被抹掉，「相关结果 1234 条」变成
    「相关结果#条」，一个明摆着的分词型站点就被错判成「待定」放行了。
    归一化是为相似度服务的，不是通用清洗。
    """
    return re.sub(r"\d{4,}", "#", flat(t))


def first_visible(pg, sel, maxn=8):
    loc = pg.locator(sel)
    for i in range(min(loc.count(), maxn)):
        c = loc.nth(i)
        try:
            if c.is_visible() and c.is_enabled():
                return c
        except Exception:
            continue
    return None


def load_entry(ctx, url_tmpl: str, kw: str):
    """打开入口，返回 (最终URL, 标题, 正文)。"""
    pg = ctx.new_page()
    try:
        pg.goto(url_tmpl.replace("{kw}", urllib.parse.quote(kw)),
                wait_until="domcontentloaded", timeout=50000)
        pg.wait_for_timeout(3500)
        if "ssp/main" in url_tmpl or "aisp_ssp" in url_tmpl:
            btn = next((b for b in (first_visible(pg, s) for s in SSP_CLICK) if b), None)
            if btn is not None:
                try:
                    btn.click(timeout=5000)
                    pg.wait_for_timeout(3000)
                except Exception:
                    pass
        pg.wait_for_timeout(1500)
        return pg.url, pg.title(), pg.locator("body").inner_text(timeout=15000)
    finally:
        if not pg.is_closed():
            pg.close()


def grade(rec: dict) -> tuple[str, str]:
    """离线分级。只读 rec 里存的对照正文原文，可反复重算，不联网。"""
    body = rec.get("对照正文_原文")
    if body is None:
        return "待重跑", "只存了归一化文本，计数可能已被抹掉，需重跑对照组"
    f = compact(body)
    if len(f) < 15:
        return "报错型", "对照页正文为空"
    if rec.get("错误"):
        return "报错型", rec["错误"]
    title = rec.get("标题") or ""
    head = compact(title + " " + (rec.get("最终URL") or ""))
    if any(compact(e) in head for e in ERR_HEAD):
        return "报错型", f"标题/URL 提示错误：{title}"
    hit = next((e for e in ERR_BODY if compact(e) in f), None)
    if hit:
        return "报错型", f"正文提示错误：{hit}"
    r = bad_page_reason(body, title, rec.get("最终URL", ""))
    if r:
        return "报错型", r

    empty = next((p for p in EMPTY_PHRASES if compact(p) in f), None)
    has_ui = any(compact(u) in f for u in SEARCH_UI)
    if empty and not has_ui:
        return "报错型", f"写着「{empty}」但页面上没有任何检索控件 —— 疑 404/空壳页冒充空结果"
    nums = [int(x.replace(",", "").replace("，", "")) for x in COUNT.findall(f)]
    pos = [n for n in nums if n > 0]
    if empty and not pos:
        return "精确型", f"对照页明确报空：{empty}"
    if pos:
        return "分词型", f"杜撰主体名仍返回 {max(pos)} 条 → 站点做分词/模糊匹配"
    # A 组报空不代表不分词：全生僻字在 AND 型引擎上必然 0 条。
    # 若词尾对照返回了结果，说明只要词尾就能命中，整站按分词型处置。
    if rec.get("词尾对照计数"):
        return "分词型", (f"生僻字对照报空，但只搜行业词尾「{rec.get('词尾对照词')}」"
                          f"返回 {rec['词尾对照计数']} 条 → 区分部分未参与匹配")
    if empty:
        return "精确型", f"对照页报空（另有计数 {nums}）：{empty}"
    if any(n == 0 for n in nums):
        return "精确型", "对照页计数为 0"
    return "待定", "既无报空也无计数，疑参数被忽略后渲染首页；交 runner 相似度闸现场判"


def report(data: dict) -> dict:
    out, tally = {}, {}
    for url, rec in data.items():
        g, why = grade(rec)
        out[url] = {"分级": g, "依据": why, "备注": rec.get("备注"),
                    "标题": rec.get("标题"), "对照正文长度": rec.get("对照正文长度")}
        tally[g] = tally.get(g, 0) + 1
    for g in ("精确型", "分词型", "待定", "报错型", "待重跑"):
        rows = [(u, v) for u, v in out.items() if v["分级"] == g]
        if not rows:
            continue
        print(f"\n{'='*92}\n{g}（{len(rows)}）\n{'='*92}")
        for u, v in rows:
            print(f"  {u[:80]}\n      {v['依据']}")
    print(f"\n合计 {len(out)}：" +
          " / ".join(f"{g} {n}" for g, n in tally.items()))
    if tally.get("分词型") or tally.get("报错型") or tally.get("待定"):
        print("\n分词型：非零结果不得判命中，必须在结果区核到主体全名。")
        print("报错型：重新找入口，绝不能拿来当「无记录」。")
        print("待定：交 runner 的相似度闸（真实页 vs 对照页）现场判。")
    return out


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    src = argv[0]
    redo = "--redo" in argv
    grade_only = "--grade-only" in argv
    global SUFFIX_CONTROL
    SUFFIX_CONTROL = next((a.split("=", 1)[1] for a in argv if a.startswith("--suffix=")), None)

    raw = json.load(open(src, encoding="utf-8"))
    entries = ({u: {} for u in raw} if isinstance(raw, list) else raw)
    out_p = os.path.splitext(src)[0] + "-对照组.json"
    grade_p = os.path.splitext(src)[0] + "-分级.json"
    done = {} if redo or not os.path.exists(out_p) else json.load(open(out_p, encoding="utf-8"))

    if not grade_only:
        todo = [u for u in entries if u not in done or "对照正文_原文" not in done[u]]
        print(f"\n对照关键词：{CONTROL}\n待甄别 {len(todo)} / {len(entries)} 个入口\n")
        if todo:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                ctx = connect(p).contexts[0]
                for url in todo:
                    rec = dict(entries[url])
                    try:
                        final, title, body = load_entry(ctx, url, CONTROL)
                        rec.update({"最终URL": final, "标题": title,
                                    "对照正文_原文": flat(body)[:20000],
                                    "对照正文_归一化": norm(body)[:20000],
                                    "对照正文长度": len(flat(body)),
                                    "甄别时间": datetime.datetime.now().astimezone().isoformat()})
                    except Exception as e:
                        rec["错误"] = f"{type(e).__name__}: {str(e)[:120]}"
                    done[url] = rec
                    atomic_write_json(out_p, done)
                    g, _ = grade(rec)
                    log(f"{'✓' if g == '精确型' else '·'} {g:5s} {url[:66]}")
                    time.sleep(random.uniform(2.5, 4.5))

    # 词尾对照：同一入口再打一次「行业词尾」，与 A 组计数比对
    if SUFFIX_CONTROL and not grade_only:
        from playwright.sync_api import sync_playwright
        print(f"\n词尾对照：{SUFFIX_CONTROL}")
        with sync_playwright() as p:
            ctx = connect(p).contexts[0]
            for url in entries:
                rec = done.get(url) or {}
                if "词尾对照计数" in rec:
                    continue
                try:
                    _f, _t, body = load_entry(ctx, url, SUFFIX_CONTROL)
                    nums = [int(x.replace(",", "").replace("，", ""))
                            for x in COUNT.findall(compact(body))]
                    rec["词尾对照词"] = SUFFIX_CONTROL
                    rec["词尾对照计数"] = max(nums) if nums else 0
                except Exception as e:
                    rec["词尾对照计数"] = None
                    rec["词尾对照错误"] = f"{type(e).__name__}: {str(e)[:80]}"
                done[url] = rec
                atomic_write_json(out_p, done)
                log(f"  词尾 {rec.get('词尾对照计数')}  {url[:60]}")
                time.sleep(random.uniform(2.5, 4.5))

    g = report(done)
    atomic_write_json(grade_p, g)
    print(f"\n对照组 → {out_p}\n分级   → {grade_p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
