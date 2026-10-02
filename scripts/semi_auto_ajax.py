#!/usr/bin/env python3
"""【技能样例】地方站内 AJAX 渠道 —— 能自动的自动，不能的停下来等人。

这 7 项的共同点：URL 带参数打开只会渲染首页，但**在页面上填框检索是能执行的**。
实测四种反应各不相同：
  · 郑州市门户    填框敲回车 → 原页 URL 变成 search.jspx?q=…      → 全自动
  · 深圳人社      点搜索**另开一页** /fzlm/cjhzthjs/?keywords=…   → 自动跟到新页
  · 北京发改委    点搜索**另开一页** /so/s?tab=all&siteCode=…     → 自动跟到新页
  · 赛罕区门户    结果页不回显检索词，qt= 被 JS 抹掉             → 两页一组原子取证

**「另开一页」是本脚本最容易犯的假阴性**：脚本盯着原页，原页从头到尾没动过，
三条判据全落空，于是把「结果就在隔壁那一页上」判成「检索未执行」。人眼看到有结果、
脚本看到空白页，两边说的根本不是同一页。见 resolve_result_page。

所以不做「全自动」或「全人工」的一刀切：脚本先自己试，试通了就按闸门校验后落盘；
试不通就把页面留在浏览器里、在终端等你操作，你确认结果出来后回车，脚本再打印归档。

用法：
  python3 半自动_地方AJAX.py              # 跑全部 7 项
  python3 半自动_地方AJAX.py --only=E21    # 只跑一个主体
  python3 半自动_地方AJAX.py --auto-only   # 只做能全自动的，不停下来等人
"""
from __future__ import annotations

import datetime, json, os, re, sys, time, urllib.parse

from playwright.sync_api import sync_playwright

sys.path.insert(0, "/Users/lestersun/.claude/skills/web-evidence-capture/scripts")
from capture import connect, grab, pdf_text
from evidence_core import (atomic_write_json, bad_page_reason, classify_pdf_text,
                           file_sha256, EMPTY_PHRASES, compact)

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(ROOT)
OUT = os.path.join(ROOT, "留痕_地方AJAX")
PENDING = os.path.join(OUT, ".pending")
LOG = os.path.join(ROOT, "进度_地方AJAX.json")
TODAY = datetime.date.today().strftime("%Y%m%d")

PLAN = json.load(open(os.path.join(PROJ, "地方逐部门跑批清单.json"), encoding="utf-8"))
TODO_KEYS = json.load(open(os.path.join(ROOT, "地方部门_需人工_20260901.json"), encoding="utf-8"))
UNIT = {f"{u['对象编号']}|{u['检索入口']}": u for u in PLAN["单元"]}

# 各站的检索框选择器（来自 entry_discover --forms 的实测结果）
BOX = {
    "hrss.sz.gov.cn":        "#search1, input[name=keywords]",
    "fgw.beijing.gov.cn":    "#page_search, input[name=qt]",
    "gaj.sh.gov.cn":         "#searchChar, input[name=searchChar]",
    "www.saihan.gov.cn":     "#qt, input[name=qt]",
    "www.zhengzhou.gov.cn":  "input[name=q]",
    "zzgjj.zhengzhou.gov.cn":"input[name=q]",
}
HOME = {h: f"https://{h}/" for h in BOX}
BTN = ("button[type=submit]", "input[type=submit]", "[class*=search-btn]",
       "[class*=searchBtn]", "[class*=btn-search]", "button:has-text('搜索')",
       "a:has-text('搜索')", "span:has-text('搜 索')", "i[class*=search]",
       # 有些站群的检索按钮**不长得像按钮**：内蒙古各厅局是
       # <div class="s-btn" onclick="btnonclick()"> 里套一张图，没有 form。
       # 上面那些选择器一个都匹配不上，于是报「自动触发失败」——
       # 其实站点好好的，只是按钮不按常规写。
       "div.s-btn", "[onclick*=btnonclick]", "[class~=s-btn]",
       "[class*=sousuo]", "[id*=searchBtn]", "[id*=btnSearch]")
# 结果区文字标记。**但它只是三条判据之一，不能单用**——
# 很多站的结果页直接列条目，既没有计数行也没有「结果」标题，一个标记都匹配不上。
# 实测深圳人社：URL 已变成 /fzlm/cjhzthjs/?keywords=深圳市某某数字科技有限公司、
# 检索框有词、下面列着通知公告，却因为没有 MARK 词被判「拒绝落盘」——假阴性。
# 结果区标记 = 自己列的词 + evidence_core 认的全部空结果措辞。
# **词表要和判定层共用一份**，否则会出现台账自相矛盾：
# 赛罕区页面明写「未能搜到」，判定层认得（EMPTY_PHRASES 里有），
# 而这里的 MARK 没收录，于是台账一边写「结果计数明确为空」、
# 一边写「等满 25 秒仍无结果区标记」——同一份证据两句话打架，复核人只会更不信。
MARK = tuple(dict.fromkeys(
    ("相关结果", "搜索结果", "检索结果", "查询结果", "共有", "共检索到",
     "结果列表", "条结果", "找到", "找到记录") + tuple(EMPTY_PHRASES)))
SEARCH_URL = re.compile(r"(search|result|/so/|/ss\b|query|keywords?=|\bqt=|\bwd=)", re.I)


def search_executed(pg, kw, home, before_body):
    """判定纪律第 1 条的三选一，缺一不可地实现全：命中任一即认为检索已执行。

    返回 (是否执行, 依据)。只实现「结果区文字标记」一条是不够的——
    这正是本脚本第一版把 4 个有结果的页面判成「拒绝落盘」的原因。
    """
    body = pg.locator("body").inner_text(timeout=10000)
    enc = urllib.parse.quote(kw)
    # ① URL 变了，且带上了检索词或检索路径
    if pg.url != home:
        if enc in pg.url or kw in pg.url:
            return True, f"URL 携带检索词：…{pg.url[-70:]}"
        if SEARCH_URL.search(pg.url):
            return True, f"URL 变为检索路径：…{pg.url[-70:]}"
    # ② 正文出现结果区标记
    m = next((x for x in MARK if x in body), None)
    if m:
        return True, f"正文出现结果区标记「{m}」"
    # ③ 检索框仍留着检索词，且正文相对检索前有实质变化
    try:
        val = pg.evaluate("""()=>{const i=[...document.querySelectorAll('input')]
            .find(x=>x.value&&x.value.length>3);return i?i.value:''}""")
    except Exception:
        val = ""
    if re.sub(r"\s+", "", val or "") == re.sub(r"\s+", "", kw):
        d = abs(len(body) - len(before_body))
        if body != before_body and d > 80:
            return True, f"检索框保留检索词且正文较检索前变化 {d} 字符"
    return False, "URL 未变、无结果区标记、正文亦无实质变化 —— 检索疑未执行"


BEFORE = {}       # id(page) → 检索前的正文，供「正文是否有实质变化」判据使用
PAGE_READY = {}   # id(page) → settle 的结论，如实写进台账
WIDENED = {}      # id(page) → 检索框显示处理的披露文字


def resolve_result_page(ctx, pg, before_ids, kw):
    """检索结果另开一页时，把判定和打印切到新开的那一页上去。

    **这是本脚本第二类假阴性的根因。** 很多政府站的检索按钮是 target=_blank：
    北京发改委点「搜索」会新开 fgw.beijing.gov.cn/so/s?tab=all&siteCode=…，
    深圳人社新开 hrss.sz.gov.cn/fzlm/cjhzthjs/?keywords=…。
    脚本若一直盯着原来那页，原页从头到尾没动过——URL 没变、没有结果区标记、
    正文也没变化，三条判据全落空，于是把「结果就在隔壁那一页上」判成「检索未执行」。
    人眼看到的是有结果，脚本看到的是另一张空白页，两边说的根本不是同一页。

    返回 (应当用于判定和打印的页, 切换说明或 None)。
    """
    cands = [p for p in ctx.pages if id(p) not in before_ids and not p.is_closed()]
    ready = []
    for p in cands:
        try:
            p.wait_for_load_state("domcontentloaded", timeout=15000)
            p.wait_for_timeout(1500)
        except Exception:
            pass
        u = p.url or ""
        if u and not u.startswith("about:"):
            ready.append(p)
    if not ready:
        return pg, None
    enc = urllib.parse.quote(kw)
    def score(p):
        u = p.url or ""
        return 2 if (enc in u or kw in u) else (1 if SEARCH_URL.search(u) else 0)
    ready.sort(key=score, reverse=True)
    new = ready[0]
    try:
        new.bring_to_front()
        new.wait_for_timeout(1500)
    except Exception:
        pass
    BEFORE.setdefault(id(new), "")
    return new, f"结果另开新页 {(new.url or '')[:100]}"


def opened_since(ctx, before_ids):
    return [p for p in ctx.pages if id(p) not in before_ids and not p.is_closed()]


def log(m):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {m}", flush=True)


def safe(v):
    return (re.sub(r'[\\/:*?"<>|\r\n]+', "_", str(v)).strip(" .") or "x")[:110]


def vis(pg, sel, n=6):
    l = pg.locator(sel)
    for i in range(min(l.count(), n)):
        c = l.nth(i)
        try:
            if c.is_visible() and c.is_enabled():
                return c
        except Exception:
            pass
    return None


def settle(pg, max_wait=25, gap=1200):
    """等结果区异步回填完再打印。**打印时机本身就是证据质量的一部分。**

    第一版拿「正文不再变化」当收工条件，错得很彻底：
    深圳人社实测第 0~9 秒正文稳定停在 326 字的空壳，第 14 秒才涨到 4237 字。
    「不再变化」≠「已加载」——空壳自己就很稳定，于是脚本第 3 秒就收工，
    印下一张什么结果都没有的页，落盘时还理直气壮写「未见结果」。
    这比漏检更糟：漏检只是没查，假空页是**把没查完写成了查了没有**。

    改为等**阳性信号**：结果区标记 / 计数行 / 明确空结果措辞出现，或正文明显长出来。
    都等不到就按超时收，并把「等满 N 秒仍无结果区」如实回给调用方写进台账。
    返回 (是否等到阳性信号, 说明)。
    """
    waited, base, last, stable = 0.0, None, -1, 0
    while waited < max_wait:
        try:
            flat = re.sub(r"\s+", "", pg.locator("body").inner_text(timeout=8000))
        except Exception:
            return False, "正文读取失败"
        if base is None:
            base = len(flat)
        m = next((x for x in MARK if x in flat), None)
        if m:
            return True, f"结果区已就绪（出现「{m}」，等待 {waited:.0f} 秒）"
        if len(flat) > base * 1.5 + 200:          # 正文明显长出来 = 结果回填了
            if len(flat) == last:
                stable += 1
                if stable >= 2:
                    return True, f"结果区已回填（正文 {base}→{len(flat)} 字，等待 {waited:.0f} 秒）"
            else:
                stable = 0
            last = len(flat)
        pg.wait_for_timeout(gap)
        waited += gap / 1000
    return False, f"等满 {max_wait:.0f} 秒仍无结果区标记、正文亦无明显增长（{base} 字）"


def fill_box(pg, box, kw):
    """填检索框。点不动就退回 JS 聚焦——赛罕区的 #qt 被浮层盖住，
    click 会 8 秒超时，但 focus + 派发 input 事件是能填进去的。"""
    try:
        box.click(timeout=8000)
    except Exception:
        try:
            box.evaluate("e=>e.focus()")
        except Exception:
            pass
    try:
        box.fill(kw, timeout=8000)
    except Exception:
        box.evaluate("(e,v)=>{e.value=v;"
                     "e.dispatchEvent(new Event('input',{bubbles:true}));"
                     "e.dispatchEvent(new Event('change',{bubbles:true}))}", kw)
    pg.wait_for_timeout(400)


def widen_box(box):
    """放宽检索框，让**已经填在里面的**检索词完整显示出来。

    赛罕区实测：框宽只容 13 个字，「某某某光电（内蒙古）有限公司」被裁掉头一个字，
    打印出来是「某某光电（内蒙古）有限公司」——差一个字就是另一家公司，
    这份「检索条件页」当场作废，两页一组的取证也就白做了。

    只改宽度、字号和横向滚动位置，**绝不碰 value**：
    把检索词写回框里是伪造站点回显；把本来就在框里的词露出来不是。
    这项改动会写进台账，并与台账里那条 qt=<主体全称> 的真实请求互相印证。
    """
    try:
        # 只放宽 input 本身不够：它常被外层 flex/固定宽容器卡着，改了也撑不开。
        # 实测呼和浩特市发改委页头「搜本站」框，放宽 input 后仍印出「内蒙古某晶科」——
        # 裁在第 6 个字。**要连同上溯三层祖先一起解除宽度约束。**
        box.evaluate("""e=>{
          const relax=(n)=>{n.style.setProperty('width','680px','important');
            n.style.setProperty('min-width','680px','important');
            n.style.setProperty('max-width','none','important');
            n.style.setProperty('flex','0 0 auto','important');
            n.style.setProperty('overflow','visible','important');};
          relax(e);
          e.style.setProperty('font-size','12px','important');
          e.style.setProperty('letter-spacing','0','important');
          e.style.setProperty('text-overflow','clip','important');
          let n=e.parentElement;
          for(let i=0;i<3&&n;i++){relax(n);n=n.parentElement;}
          e.scrollLeft=0;
          try{e.setSelectionRange(0,0)}catch(_){}}""")
        return "打印前放宽检索框及其外层容器，以完整显示已填入的检索词（仅改宽度字号，未改动其值）"
    except Exception:
        return None


def try_search(ctx, pg, box, kw, home, before=None):
    """回车 → 各种按钮，逐个试。返回 (触发方式, 结果页)；失败为 (None, 原页)。

    每次触发后都要重新确认「结果到底落在哪一页」——见 resolve_result_page。
    """
    if before is None:
        before = pg.locator("body").inner_text(timeout=8000)
    ids = {id(p) for p in ctx.pages}
    fill_box(pg, box, kw)
    for how in ["enter"] + list(BTN):
        try:
            if how == "enter":
                box.press("Enter")
            else:
                b = vis(pg, how, 3)
                if b is None:
                    continue
                b.click(timeout=4000)
            pg.wait_for_timeout(4000)
            tgt, moved = resolve_result_page(ctx, pg, ids, kw)
            if moved:
                ok, _why = search_executed(tgt, kw, home, "")
                if ok:
                    return f"{how}（{moved}）", tgt
                ids.add(id(tgt))     # 这页不是结果页，别再重复评估
                if len(ids) - len(ctx.pages) > 3:
                    break                # 已开出 3 个无关新页，别再乱点了
            ok, _why = search_executed(pg, kw, home, before)
            if ok:
                return how, pg
        except Exception:
            continue
    # 兜底：JS 直接点那个「不像按钮的按钮」。
    # 顺序是**先按名字全页找、再按位置找**，不能反过来 —— 打印前 widen_box()
    # 把检索框及其外层放宽了，按钮早被挤出「框右侧 140px」的范围，
    # 单靠位置判据会失效。
    try:
        clicked = pg.evaluate("""(sel)=>{
            const named=[...document.querySelectorAll('[onclick],div,span,a,button,i')]
              .filter(e=>{const q=e.getBoundingClientRect();
                return q.width>6&&q.height>6&&
                  /btnonclick|s-btn|search-?btn|btn-?search|sousuo|searchbutton/i
                    .test((e.getAttribute('onclick')||'')+(e.className||'')+(e.id||''))});
            let c=named[0];
            if(!c){
              const i=document.querySelector(sel)||document.activeElement; if(!i) return null;
              const r=i.getBoundingClientRect();
              c=[...document.querySelectorAll('div,span,a,img,button,i')].find(e=>{
                const q=e.getBoundingClientRect();
                return q.width>8&&q.height>8&&q.left>=r.right-12&&q.left<r.right+140
                       &&Math.abs(q.top-r.top)<70
                       &&(e.onclick||e.getAttribute('onclick'));});
            }
            if(!c) return null; c.click();
            return (c.tagName+'.'+(c.className||'')+'#'+(c.id||'')).slice(0,48);}""",
            "input[type=text]")
        if clicked:
            pg.wait_for_timeout(5000)
            tgt, moved = resolve_result_page(ctx, pg, ids, kw)
            cand = tgt if moved else pg
            ok, _why = search_executed(cand, kw, home, "" if moved else before)
            if ok:
                return f"JS 点击检索框右侧控件（{clicked}）" + (f"（{moved}）" if moved else ""), cand
    except Exception:
        pass
    return None, pg


# 这些站检索确实执行了，但**结果页既不回显检索词、URL 里的 qt= 也被 JS 抹掉**
# （赛罕区实测：加载约 1.2 秒后 qt= 被清空，输入框同时清空；真实数据走
#  POST api.so-gov.cn/s，检索词在请求体里）。单印结果页证明不了搜的是谁，
# 违反判定纪律第 8 条「检索字段是证据契约的一部分」。
# 对这类站改为**两页一组的原子取证**：
#   第 1 页：检索框里填着检索词——这是页面自己渲染的，不是事后写回去的
#   第 2 页：该次检索返回的结果
# 两页时间戳相差数秒，合成一份 PDF，不可拆分；台账另记承载检索词的真实请求。
# 绝不在结果页上把检索词写回输入框再打印——那是伪造站点回显。
NO_ECHO = {"www.saihan.gov.cn"}

# 站点级对照组结论（2026-09-01 实测）。**计数非零 ≠ 命中主体**，
# 分词型站点必须在站点层面打标，不能靠逐页阈值——见 SKILL.md 判定纪律第 5 条。
CONTROL = {
    "fgw.beijing.gov.cn": {
        "分词型": True,
        "对照A_全生僻字": "耒阳砯砼泫铘信息技术有限公司 → 未进结果页（返回首页）",
        "对照B_行业词尾": "数字科技有限公司 → 6117 条",
        "被查主体": "北京某某数字科技有限公司 → 2227 条，但主体名在结果区 0 次",
        "结论": "分词型：2227 条系「北京」「科技有限公司」等词命中，非主体命中",
    },
    "gaj.sh.gov.cn": {
        "分词型": False,
        "对照A_全生僻字": "耒阳砯砼泫铘信息技术有限公司 → 找到记录约 0 条",
        "对照B_行业词尾": "数字科技有限公司 → 找到记录约 0 条",
        "引擎可用性": "以「上海」试检 → 找到记录约 9999 条，证明引擎工作正常",
        "检索词送达": "POST /shga/vQsAllSearch  body=searchChar=<主体全称>，结果页 searchChar 回显",
        "结论": "短语精确型：0 条即真实无记录",
    },
    "hrss.sz.gov.cn": {
        "分词型": None,
        "备注": "结果页无计数行，条目直接列出；结果由 search.gd.gov.cn jsonp 异步回填，"
                "须等正文稳定后再打印（见 settle）",
    },
}


def merge(a, b, out):
    import subprocess
    subprocess.run(["pdfunite", a, b, out], check=True)
    for x in (a, b):
        if os.path.exists(x):
            os.remove(x)


def archive_two_page(ctx, pg, oid, kw, region, lvl, host, how, rows, pre, api,
                     forced=False, mark=None):
    """pre = 已打印好的「检索框含检索词」页；这里补打结果页并合成一份。"""
    body = pg.locator("body").inner_text(timeout=10000)
    r = bad_page_reason(body, pg.title(), pg.url)
    if r:
        return None, r
    if not forced:
        ok, why_exec = search_executed(pg, kw, HOME.get(host, ""), BEFORE.get(id(pg), ""))
        if not ok:
            return None, why_exec
    # **同一 host 下的多个渠道必须靠 mark 区分**，否则文件名相同 → 静默互相覆盖，
    # 台账 N 条、磁盘 N-1 份。实测青岛 5 个部门都走 www.qingdao.gov.cn 的统一检索
    # （仅 areaId 不同），5 条记录只落下 1 个文件，另 4 份被悄悄盖掉。
    prefix = "_".join(map(safe, (oid, kw[:18], lvl, region, host)))
    if mark:
        prefix += "_" + safe(mark)[:28]
    post, title, hit = grab(ctx, pg, f"{prefix}-结果页_{TODAY}.pdf", out_dir=PENDING, force=False)
    if not post:
        return None, f"标题疑认证/错误页：{title} {hit}"
    tmp = os.path.join(PENDING, f"{prefix}-合成_{TODAY}.pdf")
    merge(pre, post, tmp)
    text = pdf_text(tmp)
    label, why = classify_pdf_text(text, keyword=kw)
    if "无效证据页" in label:
        qd = os.path.join(OUT, "异常页_不得作为底稿")
        os.makedirs(qd, exist_ok=True)
        os.replace(tmp, os.path.join(qd, os.path.basename(tmp)))
        return None, f"打印件无效：{why}"
    # 合成件第 1 页必然含检索词（检索框回显），所以回显阈值要相应提高：
    # 出现 ≤2 次 = 只有两页的检索框，结果区里没有。
    flat = re.sub(r"\s+", "", text)
    cnt = flat.count(re.sub(r"\s+", "", kw))
    if label.startswith("★有结果") and cnt <= 2:
        label = "★疑模糊匹配需甄别"
        why += f"；但主体名全件仅出现 {cnt} 次（均为检索框回显），结果区不含主体名"
    final = os.path.join(OUT, f"{prefix}-{safe(label.lstrip('★'))}_{TODAY}.pdf")
    if os.path.exists(final):
        os.remove(final)
    os.replace(tmp, final)
    return {"file": os.path.basename(final), "result": label, "reason": why, "keyword": kw,
            "层级": lvl, "行政区": region, "站点": host, "触发方式": how,
            "取证方式": "两页一组：第1页检索框含检索词，第2页结果页",
            "承载检索词的真实请求": api, "主体名出现次数": cnt, "覆盖矩阵行": rows,
            "页面就绪": PAGE_READY.get(id(pg)), "检索框显示处理": WIDENED.get(id(pg)),
            "url": pg.url, "title": title, "sha256": file_sha256(final),
            "captured_at": datetime.datetime.now().astimezone().isoformat(),
            "判定依据": "存档PDF"}, None


def archive(ctx, pg, oid, kw, region, lvl, host, how, rows, forced=False, mark=None):
    body = pg.locator("body").inner_text(timeout=10000)
    r = bad_page_reason(body, pg.title(), pg.url)
    if r:
        return None, r
    # forced = 律师已在屏幕上确认结果出来了。闸门只能拦一次，不能在这里再拦一遍——
    # 第一版就是人工放行后被这里二次驳回，「强制落盘」成了摆设，什么都没写下去。
    if not forced:
        ok, why_exec = search_executed(pg, kw, HOME.get(host, ""), BEFORE.get(id(pg), ""))
        if not ok:
            return None, why_exec
    # **同一 host 下的多个渠道必须靠 mark 区分**，否则文件名相同 → 静默互相覆盖，
    # 台账 N 条、磁盘 N-1 份。实测青岛 5 个部门都走 www.qingdao.gov.cn 的统一检索
    # （仅 areaId 不同），5 条记录只落下 1 个文件，另 4 份被悄悄盖掉。
    prefix = "_".join(map(safe, (oid, kw[:18], lvl, region, host)))
    if mark:
        prefix += "_" + safe(mark)[:28]
    path, title, hit = grab(ctx, pg, f"{prefix}-待判定_{TODAY}.pdf", out_dir=PENDING, force=False)
    if not path:
        return None, f"标题疑认证/错误页：{title} {hit}"
    text = pdf_text(path)
    label, why = classify_pdf_text(text, keyword=kw)
    if "无效证据页" in label:
        qd = os.path.join(OUT, "异常页_不得作为底稿")
        os.makedirs(qd, exist_ok=True)
        os.replace(path, os.path.join(qd, os.path.basename(path)))
        return None, f"打印件无效：{why}"
    # 回显判据：主体名全页只出现 ≤1 次 = 只有检索框回显
    if label.startswith("★有结果"):
        flat = re.sub(r"\s+", "", text)
        if flat.count(re.sub(r"\s+", "", kw)) <= 1:
            label = "★疑模糊匹配需甄别"
            why += "；但主体名全页仅出现≤1次（检索框回显），结果区不含主体名"
        elif (CONTROL.get(host) or {}).get("分词型"):
            label = "★疑模糊匹配需甄别"
            why += f"；该站经对照组实测为分词型（{CONTROL[host]['对照B_行业词尾']}），计数非零不等于命中主体"
    final = os.path.join(OUT, f"{prefix}-{safe(label.lstrip('★'))}_{TODAY}.pdf")
    if os.path.exists(final):
        os.remove(final)
    os.replace(path, final)
    return {"file": os.path.basename(final), "result": label, "reason": why, "keyword": kw,
            "层级": lvl, "行政区": region, "站点": host, "触发方式": how,
            "覆盖矩阵行": rows, "url": pg.url, "title": title,
            "对照组": CONTROL.get(host), "页面就绪": PAGE_READY.get(id(pg)),
            "sha256": file_sha256(final),
            "captured_at": datetime.datetime.now().astimezone().isoformat(),
            "判定依据": "存档PDF"}, None


def main():
    argv = sys.argv[1:]
    only = next((a.split("=", 1)[1] for a in argv if a.startswith("--only=")), None)
    auto_only = "--auto-only" in argv
    os.makedirs(PENDING, exist_ok=True)
    done = json.load(open(LOG, encoding="utf-8")) if os.path.exists(LOG) else {}

    todo = []
    for x in TODO_KEYS:
        oid = x["key"].split("|")[0]
        host = urllib.parse.urlparse(x["检索入口"]).netloc
        u = UNIT.get(x["key"], {})
        k = f"{oid}|{host}"
        if k in done or (only and oid != only) or host not in BOX:
            continue
        todo.append((oid, u.get("主体", oid), u.get("行政区", ""), u.get("层级", ""),
                     host, x["覆盖矩阵行"]))
    print(f"\n待办 {len(todo)} 项（台账已有 {len(done)} 项跳过）")
    print("能自动的自动跑；试不通的会把页面留在浏览器里，在终端等你操作。\n")

    n_ok, manual = 0, []
    with sync_playwright() as p:
        ctx = connect(p).contexts[0]
        for oid, kw, region, lvl, host, rows in todo:
            base_ids = {id(x) for x in ctx.pages}   # 本项开工前就在的标签页，收尾时不能碰
            pg = ctx.new_page()
            listener = None
            try:
                pg.goto(HOME[host], wait_until="domcontentloaded", timeout=50000)
                pg.wait_for_timeout(4000)
                box = vis(pg, BOX[host])
                if box is None:
                    manual.append(f"{oid} {kw[:16]} / {host}：首页找不到检索框")
                    continue
                BEFORE[id(pg)] = pg.locator("body").inner_text(timeout=8000)
                two, pre, api, hits = host in NO_ECHO, None, None, []
                widened = None
                if two:
                    fill_box(pg, box, kw)
                    widened = widen_box(box)      # 先让检索词完整可见，再打印
                    pg.wait_for_timeout(600)
                    _pfx = "_".join(map(safe, (oid, kw[:18], lvl, region, host)))
                    pre, _t, _h = grab(ctx, pg, f"{_pfx}-检索条件页_{TODAY}.pdf",
                                       out_dir=PENDING, force=True)
                    if not pre:
                        manual.append(f"{oid} {kw[:16]} / {host}：检索条件页打印失败")
                        continue
                    enc = urllib.parse.quote(kw)
                    # 监听挂在 context 上而不是单个页面上：结果另开一页时，
                    # 承载检索词的那个请求发生在新页，挂在原页上会漏掉。
                    def listener(r, _e=enc, _k=kw, _h=hits):
                        try:
                            if (_e in r.url or _k in r.url
                                    or _e in (r.post_data or "") or _k in (r.post_data or "")):
                                _h.append((r.method, r.url))
                        except Exception:
                            pass
                    ctx.on("request", listener)
                how, tgt = try_search(ctx, pg, box, kw, HOME[host], BEFORE[id(pg)])
                forced = False
                if how is None:
                    if auto_only:
                        manual.append(f"{oid} {kw[:16]} / {host}：自动触发失败（--auto-only 跳过）")
                        continue
                    print(f"\n{'─'*70}")
                    print(f"  需要你操作：{oid} {kw} / {region} / {host}")
                    print(f"  浏览器里已打开该站并填好检索词。请点页面上的检索按钮，")
                    print(f"  确认结果出来后，回到这里按回车；跳过本项输入 s 再回车。")
                    print(f"  （结果若另开了一页也没关系，脚本会自己找过去。）")
                    print(f"{'─'*70}")
                    if input("  > ").strip().lower() == "s":
                        manual.append(f"{oid} {kw[:16]} / {host}：人工跳过")
                        continue
                    how = "人工点击"
                    # 你手点的按钮同样可能是 target=_blank，先把结果页找出来再判定
                    tgt, moved = resolve_result_page(ctx, pg, {id(pg)} | base_ids, kw)
                    if moved:
                        how = f"人工点击（{moved}）"
                    passed, why_exec = search_executed(
                        tgt, kw, HOME[host], BEFORE.get(id(tgt), ""))
                    if not passed:
                        print(f"  ⚠ 仍未检出检索已执行：{why_exec}")
                        print(f"    当前判定用的是：{(tgt.url or '')[:100]}")
                        print(f"    若你确认这一页上结果已出来，输入 y 强制落盘；否则回车跳过。")
                        if input("  > ").strip().lower() != "y":
                            manual.append(f"{oid} {kw[:16]} / {host}：{why_exec}")
                            continue
                        forced, how = True, how + "｜律师现场确认结果已出"
                filled, fill_why = settle(tgt)   # 等结果真出来再打印
                PAGE_READY[id(tgt)] = fill_why
                WIDENED[id(pg)] = widened
                WIDENED.setdefault(id(tgt), widened)
                if not filled:
                    log(f"  ⚠ {oid}/{host}：{fill_why}")
                if two:
                    api = next((f"{m} {u[:130]}" for m, u in hits), None)
                    rec, err = archive_two_page(ctx, tgt, oid, kw, region, lvl,
                                                host, how, rows, pre, api, forced=forced)
                else:
                    rec, err = archive(ctx, tgt, oid, kw, region, lvl, host, how, rows,
                                       forced=forced)
                if err:
                    manual.append(f"{oid} {kw[:16]} / {host}：{err}")
                    log(f"✗ {oid} {kw[:14]} / {host} — {err[:60]}")
                else:
                    if forced:
                        rec["检索已执行的依据"] = "自动判据未命中，由律师在页面上现场确认"
                    done[f"{oid}|{host}"] = rec
                    atomic_write_json(LOG, done)
                    n_ok += 1
                    log(f"✓ {oid} {kw[:14]} / {host}: {rec['result']}（{how}）")
            except Exception as e:
                manual.append(f"{oid} {kw[:16]} / {host}：{type(e).__name__}: {str(e)[:80]}")
            finally:
                if listener is not None:
                    try:
                        ctx.remove_listener("request", listener)
                    except Exception:
                        pass
                for extra in opened_since(ctx, base_ids):   # 含 pg 本身与检索新开的页
                    try:
                        extra.close()
                    except Exception:
                        pass
                time.sleep(1.5)

    print(f"\n本轮成功 {n_ok} / 需人工 {len(manual)}；台账累计 {len(done)}")
    for m in manual:
        print("  -", m)
    covered = set()
    for v in done.values():
        covered.update(v.get("覆盖矩阵行") or [])
    print(f"本批已覆盖矩阵行 {len(covered)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
