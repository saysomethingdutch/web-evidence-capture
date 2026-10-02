#!/usr/bin/env python3
"""【技能样例】部门级缺口补跑 —— 直达 URL / 范围限定检索 / 两页原子取证 三种模式并存。

本文件是某公司项目 2026-09-01 的实例，改 T 任务表即可复用。三个可迁移的做法：
  · 直达 URL 模式：URL 自带检索词，页脚打印出来就是检索契约，比填框稳
  · 范围限定检索：部门无独立网站时用市府统一检索 + areaId/siteId，范围标签印在件上
  · AJ.OUT/AJ.PENDING 接管：复用别的模块的函数，要连它的模块级常量一起接管

原始说明：补 2026-09-01 缺口总表里「可跑」的那批。

三条来自实测的更正，都写在这里备查：

1. **HTTP 探针判的「403/502 站点不可用」有假。** 政府站普遍对非浏览器请求直接拒。
   用常驻 Chrome 重试：宾县 chinabx.gov.cn 从 502→200 且有检索框（救回 12 行）、
   青岛应急 ajj、青岛生态环境 mbee 从 403→200（救回 2 行）。
   **凡是要写进《查询说明》的「站点不可用」，必须是真实浏览器复验过的。**

2. **呼和浩特市门户是真挂了**（云防护返回「站点不存在，未接入云防护」，
   http/https/index.html/so 全部 422），但**各部门自有站点活着**。
   逐部门跑比走门户更贴合笔录口径（笔录第 43-54 行本来就是逐部门）。

3. **青岛统一检索不能顶替那 3 个 403 的部门站**：
   www.qingdao.gov.cn/unionsearch 的结果来源标签清一色「青岛政务网」，
   它只索引门户自身内容，不索引 dpc/police/zrzygh。拿它交差就是把「没查」写成「查了没有」。
"""
from __future__ import annotations
import datetime, importlib.util, json, os, sys, time, urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(ROOT)
sys.path.insert(0, "/Users/lestersun/.claude/skills/web-evidence-capture/scripts")
from playwright.sync_api import sync_playwright
from capture import connect, grab
from evidence_core import atomic_write_json

# 判定纪律的实现只此一份，从半自动脚本里 import，不复制
_s = importlib.util.spec_from_file_location("aj", os.path.join(ROOT, "半自动_地方AJAX.py"))
AJ = importlib.util.module_from_spec(_s); _s.loader.exec_module(AJ)

# **被 import 的模块用的是它自己的 OUT/PENDING 常量**——不改写就会把本批 PDF
# 全落进 留痕_地方AJAX，而本批台账指向 留痕_补缺口，落盘与台账对不上。
# 第一轮跑完「成功 17 项」而 留痕_补缺口 里一个文件都没有，就是这么来的。
# 复用别人的函数时，连同它依赖的模块级常量一起接管。
OUT = os.path.join(ROOT, "留痕_补缺口")
PENDING = os.path.join(OUT, ".pending")
LOG = os.path.join(ROOT, "进度_补缺口.json")
TODAY = datetime.date.today().strftime("%Y%m%d")
AJ.OUT, AJ.PENDING = OUT, PENDING       # 见上：必须接管，否则落盘落到别人家去

E12 = "哈尔滨某某某光电技术有限公司"
E21 = "内蒙古某晶科技有限公司"
E22 = "某某某光电（内蒙古）有限公司"
E9  = "青岛某某信息产业发展合伙企业（有限合伙）"
E17 = "某某（上海）数字科技有限公司"

# 任务 = (对象编号, 主体, 层级, 行政区, 部门, 首页, 覆盖矩阵行, 直达URL模板 或 None, 两页取证?)
# 直达 URL 模板里的 {kw} 会被 urlencode 后的主体名替换。**能直达就直达**：
# URL 自带检索词，页脚打印出来就是可复核的检索契约，比填框稳得多。
T = []
T.append(("E12", E12, "区县级", "哈尔滨市宾县", "宾县政府门户（覆盖全部区县级部门）",
          "http://www.chinabx.gov.cn/", list(range(85, 97)), None, False))

# 呼和浩特：市门户已停用（云防护「站点不存在」），改逐部门。各局用同一套「智能云搜索」，
# 结果页 URL 的 qt= 被 JS 抹空、页面也不回显检索词 → 一律走两页一组原子取证。
# sthjj（生态环境）、zfcxjsj（住建）两个域名同样返回「站点不存在」，不可达，不跑。
HHHT = [("人力资源和社会保障局", "http://rsj.huhhot.gov.cn/",   [242]),
        # 呼和浩特市不单设药品监督管理局，药品/医疗器械/化妆品监管职能由市场监督管理局承担
        # （留痕件正文可见「食品药品许可 医疗器械经营许可」）。故本件同时覆盖第 251 行。
        # 这是按**实际履职部门**归位，不是拿别的部门顶替；若律师口径不同，删掉 251 即可。
        ("市场监督管理局（兼承担药品监督管理职能）", "http://scjgj.huhhot.gov.cn/", [244, 251]),
        ("应急管理局",          "http://yjglj.huhhot.gov.cn/", [245]),
        ("公安局",             "http://gaj.huhhot.gov.cn/",   [246]),
        ("自然资源局",          "http://zrzyj.huhhot.gov.cn/", [248]),
        ("发展和改革委员会",     "http://fgw.huhhot.gov.cn/",   [250])]
for oid, kw in (("E21", E21), ("E22", E22)):
    for dept, home, rows in HHHT:
        T.append((oid, kw, "市级", "呼和浩特市赛罕区", dept, home, rows, None, True))
    # 税务系统垂直管理，呼和浩特市局无独立域名，其公告发布在自治区局网站。
    # 该站是 TRS WAS5，且表单 onkeydown 明确拦掉回车（keyCode==13 return false），
    # 填框敲回车永远不动 —— 改走 WAS5 的 GET 直达。
    T.append((oid, kw, "市级", "呼和浩特市赛罕区", "税务局（自治区局，市局无独立域名）",
              "https://neimenggu.chinatax.gov.cn/",
              [241], "https://neimenggu.chinatax.gov.cn/was5/search?channelid=286494&searchword={kw}", False))

# 青岛：dpc/police/zrzygh 三个部门站真实浏览器下仍 403。改走**青岛市政府自己的**
# 「政府网站信息统一检索」并用 areaId 限定到该部门，页头会显示「市公安局」等范围标签，
# 范围可在留痕件上复核。areaId 由枚举实测：14 发改委 / 17 公安局 / 22 自然资源和规划局 /
# 23 生态环境局 / 35 应急局。注意不限定 areaId 时只搜到门户自身内容（来源标签清一色
# 「青岛政务网」），那样才是顶替；限定后搜的就是该部门站点的内容。
QD = "http://www.qingdao.gov.cn/unionsearch/?searchWord={kw}&sitetype=all&areaId=%s"
for dept, aid, rows in [("生态环境局", 23, [27]), ("应急管理局", 35, [29]),
                        ("公安局", 17, [30]), ("自然资源和规划局", 22, [32]),
                        ("发展和改革委员会", 14, [34])]:
    T.append(("E9", E9, "市级", "青岛市黄岛区", f"{dept}（青岛统一检索 areaId={aid}）",
              "http://www.qingdao.gov.cn/", rows, QD % aid, False))

# 哈尔滨：市级 6 个部门（市场监管/药监/人社/生态环境/自然资源/住建）**没有独立网站**，
# 只有市门户下的「部门政府信息公开」专页。此前判「该页无站内检索框」是错的——
# 页面上有 #ipt，它通向 https://www.harbin.gov.cn/so/xxgk/s?qt=  政府信息公开平台检索，
# 检的正是各部门的政府信息公开内容，且结果页带**发文机构分面**（市市场监管局、市生态环境局…），
# 复核人能在留痕件上看出命中来自哪个局。
# **这不是顶替**：它就是那几个部门专页自己提供的检索入口，比市门户全站检索（混新闻/办事）更贴笔录口径。
# 分面按结果集动态生成，没有独立 URL 参数，所以按主体各跑一次，一份覆盖该主体的 6 个部门行。
HRB_XXGK = "https://www.harbin.gov.cn/so/xxgk/s?qt={kw}"
for oid, kw2, region, rows in [
        ("E1",  "某某某光电股份有限公司",       "哈尔滨市松北区",           [2, 3, 4, 8, 9, 11]),
        ("E16", "哈尔滨某甲光电科技有限公司",   "哈尔滨市松北区",           [2, 3, 4, 8, 9, 11]),
        ("E12", "哈尔滨某某某光电技术有限公司", "哈尔滨市宾县",             [74, 75, 76, 80, 81, 83]),
        ("E13", "哈尔滨某乙光电技术有限公司",   "哈尔滨市？高新技术产业开发区", [98, 99, 100, 104, 105, 107]),
        ("E23", "哈尔滨某丙光电科技有限公司",   "哈尔滨市？高新技术产业开发区", [98, 99, 100, 104, 105, 107]),
        ("E24", "哈尔滨某丁半导体科技有限公司", "哈尔滨市？高新技术产业开发区", [98, 99, 100, 104, 105, 107])]:
    T.append((oid, kw2, "市级", region,
              "市场监管/药监/人社/生态环境/自然资源/住建（政府信息公开平台检索）",
              "https://www.harbin.gov.cn/", rows, HRB_XXGK, False))

# 上海：市场监督管理局站内确无检索入口（首页零个检索控件，websearch/search5 各变体均 404），
# 记为无入口，不跑。公积金的 #nn1 藏在隐藏的手机版浮层里，真正触发是 <a onclick="a()">，
# 点不动 —— 同样改走直达 URL。
# 上海市市场监督管理局站内无检索入口，但**上海市政府网站统一检索支持 siteId 限定站点**：
# https://search.sh.gov.cn/search?siteId=scjgj.sh.gov.cn&text={kw}
# 页头会显示范围标签「市场监管局」，结果条目来源标注 scjgj.sh.gov.cn，范围在留痕件上可复核。
# 与青岛 areaId 同理：这是市政府自己对该部门站点的索引，不是拿门户内容顶替。
T.append(("E17", E17, "市级", "上海市奉贤区", "市场监督管理局（上海统一检索 siteId=scjgj）",
          "https://www.shanghai.gov.cn/", [172],
          "https://search.sh.gov.cn/search?siteId=scjgj.sh.gov.cn&text={kw}", False))

T += [("E17", E17, "市级", "上海市奉贤区", "消防救援总队", "https://sh.119.gov.cn/home", [175], None, False),
      ("E17", E17, "市级", "上海市奉贤区", "规划和自然资源局", "https://ghzyj.sh.gov.cn/", [176], None, False),
      ("E17", E17, "市级", "上海市奉贤区", "药品监督管理局", "https://yjj.sh.gov.cn/", [179], None, False),
      ("E17", E17, "市级；区县级", "上海市奉贤区", "住房公积金管理中心",
       "https://www.shzfgjj.cn/", [180, 192],
       "https://shzfgjj.cn/search/search.html?wd={kw}", False)]

# 实测到的检索框选择器；没列的走通用发现
BOX = {
 "www.chinabx.gov.cn":       "input.hd2-fr-d1, input[placeholder*=搜索]",
 "ajj.qingdao.gov.cn":       "#searchWord, input[name=searchWord]",
 "mbee.qingdao.gov.cn":      "#searchValue, input[name='搜索输入框']",
 "neimenggu.chinatax.gov.cn":"#sinpo, input[name=searchword]",
 "sh.119.gov.cn":            "input[placeholder*=输入]",
}
GENERIC = ("input[type=search]", "input[name*=key i]", "input[name*=word i]",
           "input[name=q]", "input[name=qt]", "input[name=wd]",
           "input[id*=search i]", "input[class*=search i]",
           "input[placeholder*=搜索]", "input[placeholder*=检索]",
           "input[placeholder*=关键]", "input[placeholder*=输入]", "input[type=text]")


def find_box(pg, host):
    if host in BOX:
        b = AJ.vis(pg, BOX[host])
        if b is not None:
            return b
    for s in GENERIC:
        b = AJ.vis(pg, s, 4)
        if b is not None:
            return b
    return None


def main():
    argv = sys.argv[1:]
    only = next((a.split("=", 1)[1] for a in argv if a.startswith("--only=")), None)
    os.makedirs(PENDING, exist_ok=True)
    done = json.load(open(LOG, encoding="utf-8")) if os.path.exists(LOG) else {}
    todo = [t for t in T
            if f"{t[0]}|{urllib.parse.urlparse(t[5]).netloc}|{t[6][0]}" not in done
            and (not only or t[0] == only)]
    print(f"\n待办 {len(todo)} 项（台账已有 {len(done)} 项跳过）\n")

    n_ok, manual = 0, []
    with sync_playwright() as p:
        ctx = connect(p).contexts[0]
        for oid, kw, lvl, region, dept, home, rows, tmpl, two in todo:
            host = urllib.parse.urlparse(home).netloc
            key = f"{oid}|{host}|{rows[0]}"
            base_ids = {id(x) for x in ctx.pages}
            pg = ctx.new_page()
            pre, api, hits = None, None, []
            listener = None
            try:
                if tmpl:                       # ── 直达：URL 自带检索词 ──
                    url = tmpl.replace("{kw}", urllib.parse.quote(kw))
                    pg.goto(url, wait_until="domcontentloaded", timeout=50000)
                    pg.wait_for_timeout(3000)
                    AJ.BEFORE[id(pg)] = ""
                    how, tgt = "URL 直达（检索词在网址里，页脚可复核）", pg
                else:                          # ── 填框 ──
                    pg.goto(home, wait_until="domcontentloaded", timeout=50000)
                    pg.wait_for_timeout(3500)
                    box = find_box(pg, host)
                    if box is None:
                        manual.append(f"{oid} {dept} / {host}：找不到检索框"); continue
                    AJ.BEFORE[id(pg)] = pg.locator("body").inner_text(timeout=8000)
                    if two:
                        AJ.fill_box(pg, box, kw)
                        widened = AJ.widen_box(box)
                        pg.wait_for_timeout(600)
                        _pfx = "_".join(map(AJ.safe, (oid, kw[:18], lvl, region, host)))
                        pre, _t, _h = grab(ctx, pg, f"{_pfx}-检索条件页_{TODAY}.pdf",
                                           out_dir=PENDING, force=True)
                        if not pre:
                            manual.append(f"{oid} {dept} / {host}：检索条件页打印失败"); continue
                        AJ.WIDENED[id(pg)] = widened
                        enc = urllib.parse.quote(kw)
                        def listener(r, _e=enc, _k=kw, _h=hits):
                            try:
                                if (_e in r.url or _k in r.url
                                        or _e in (r.post_data or "") or _k in (r.post_data or "")):
                                    _h.append((r.method, r.url))
                            except Exception:
                                pass
                        ctx.on("request", listener)
                    how, tgt = AJ.try_search(ctx, pg, box, kw, home, AJ.BEFORE[id(pg)])
                    if how is None:
                        manual.append(f"{oid} {dept} / {host}：自动触发失败"); continue
                filled, why = AJ.settle(tgt)
                AJ.PAGE_READY[id(tgt)] = why
                AJ.WIDENED.setdefault(id(tgt), AJ.WIDENED.get(id(pg)))
                if not filled:
                    AJ.log(f"  ⚠ {oid}/{host}：{why}")
                if two and pre:
                    api = next((f"{m} {u[:130]}" for m, u in hits), None)
                    rec, err = AJ.archive_two_page(ctx, tgt, oid, kw, region, lvl,
                                                   host, how, rows, pre, api, mark=dept)
                else:
                    # 文件名带上部门：同一 host 挂多个渠道时（青岛统一检索的 5 个 areaId）
                    # 不带区分位会静默互相覆盖
                    rec, err = AJ.archive(ctx, tgt, oid, kw, region, lvl, host, how, rows, mark=dept)
                if err:
                    manual.append(f"{oid} {dept} / {host}：{err}")
                    AJ.log(f"✗ {oid} {dept[:10]} / {host} — {err[:56]}")
                else:
                    rec["部门"] = dept
                    done[key] = rec
                    atomic_write_json(LOG, done); n_ok += 1
                    AJ.log(f"✓ {oid} {dept[:14]} / {host}: {rec['result']}")
            except Exception as e:
                manual.append(f"{oid} {dept} / {host}：{type(e).__name__}: {str(e)[:70]}")
            finally:
                if listener is not None:
                    try: ctx.remove_listener("request", listener)
                    except Exception: pass
                for x in AJ.opened_since(ctx, base_ids):
                    try: x.close()
                    except Exception: pass
                time.sleep(1.2)

    print(f"\n本轮成功 {n_ok} / 需人工 {len(manual)}；台账累计 {len(done)}")
    for m in manual: print("  -", m)
    cov = {r for v in done.values() for r in v.get("覆盖矩阵行") or []}
    print(f"本批覆盖矩阵行 {len(cov)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
