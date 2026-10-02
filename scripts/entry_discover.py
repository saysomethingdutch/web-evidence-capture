#!/usr/bin/env python3
"""入口发现 —— 探针说「这站没有站内搜索」时，用三级救援把入口挖出来。

无头探针（只读首页 <form>）在两类站上会漏判：SPA 站的检索控件不在 form 里；
WAF 对无头 UA 直接 403。实测 21 个被判「无可用检索入口」的政府站点，用下面
三级救回 9 个——都是本来要转人工的。

  1 级 同族模式：同一站群格式一致。`beijing.gov.cn` 系一律 `/so/s?qt=`，
       一次救回 3 个。省局站群也算同族：市局 404 时试省局。
  2 级 读首页真实控件（--forms）：把 <form> 的 action/param、检索框的
       name/id、按钮的 onclick 全挖出来，据此拼入口。
  3 级 实操（--drive）：填词 → 敲回车 → 不行就点按钮 → 看站点自己跳到哪个
       URL，把关键词换回 {kw} 就是模板。

第 3 级两个必踩的坑：**回车常常没绑事件**，必须点按钮；**同一站群可能并存两种
路径**（`websearch.html#search/query=` 与 `/websearch/#search/query=`），
只试一种就下"不可用"结论是试少了。

挖出来的入口仍要过 entry_triage.py 的对照组才算数。

用法：
  python3 entry_discover.py --forms hosts.json      # 2 级：勘察首页控件
  python3 entry_discover.py --drive targets.json    # 3 级：实操取 URL

hosts.json:    ["https://a.gov.cn/", ...]  或  {"https://a.gov.cn/": "某局", ...}
targets.json:  [{"home": "https://a.gov.cn/", "desc": "某局",
                 "box": "#q, input[name=q]"}, ...]      box 来自 --forms 的产出
"""
from __future__ import annotations

import json, os, random, sys, time, urllib.parse

from capture import connect
from evidence_core import atomic_write_json

CONTROL = os.environ.get("EVIDENCE_CONTROL_NAME", "耒阳砯砼泫铘信息技术有限公司")
EMPTY = ("未找到", "没有找到", "暂无数据", "暂无相关", "未查询到", "无相关",
         "没有符合", "未能搜到", "很抱歉", "抱歉", "无结果", "未检索到")
BTN = ("a:has-text('检索')", "div:has-text('检索')", "button:has-text('搜索')",
       "a:has-text('搜索')", "span:has-text('搜索')", "div:has-text('搜索')",
       "a:has-text('查询')", "a:has-text('搜一下')", "[class*=search-btn]",
       "[class*=searchBtn]", "[class*=btn-search]")

# 已知站群模式，1 级救援直接照抄。新发现的站群请补进来。
FAMILY = {
    "beijing.gov.cn": "{scheme}://{host}/so/s?qt={{kw}}",
    "chinatax.gov.cn": "{scheme}://{host}/search5/html/searchResult.html?searchWord={{kw}}",
    "sh.gov.cn": "{scheme}://{host}/websearch.html#search/query={{kw}}",
}

JS_FORMS = r"""
() => {
  const abs = u => { try { return new URL(u, location.href).href } catch { return u } };
  const forms = [...document.querySelectorAll('form')].map(f => ({
    action: abs(f.getAttribute('action') || ''),
    method: (f.getAttribute('method') || 'GET').toUpperCase(),
    inputs: [...f.querySelectorAll('input,select,textarea')].map(i => ({
      name: i.name || null, type: i.type || null,
      placeholder: i.placeholder || null, value: i.value ? i.value.slice(0,40) : null,
    })).filter(i => i.name || i.placeholder),
  })).filter(f => f.inputs.length);
  const KEY = /search|\/so\/|\/ss\b|query|websearch|jsearch|retrieval|sousuo/i;
  const links = [...new Set([...document.querySelectorAll('a[href]')]
    .map(a => abs(a.getAttribute('href'))).filter(h => KEY.test(h)))].slice(0, 25);
  const boxes = [...document.querySelectorAll('input')]
    .filter(i => /搜索|检索|查询|关键|输入|search/i.test(
        (i.placeholder||'')+' '+(i.name||'')+' '+(i.id||'')+' '+(i.className||'')))
    .map(i => ({ name: i.name||null, id: i.id||null, cls: i.className||null,
                 placeholder: i.placeholder||null,
                 formAction: i.form ? abs(i.form.getAttribute('action')||'') : null }));
  const btns = [...document.querySelectorAll('button,a,input[type=button],input[type=submit],span,div')]
    .filter(b => /^(搜索|检索|搜一下|查询|搜)$/.test((b.innerText||b.value||'').trim()))
    .slice(0, 8).map(b => ({ tag: b.tagName, text: (b.innerText||b.value||'').trim(),
                 onclick: (b.getAttribute('onclick')||'').slice(0,200),
                 href: b.getAttribute('href') ? abs(b.getAttribute('href')) : null }));
  return { url: location.href, title: document.title, forms, links, boxes, btns };
}
"""


def suggest_box(rec):
    """从勘察结果里给出 --drive 用的检索框选择器建议。"""
    sels = []
    for b in rec.get("boxes", []):
        if b.get("id"):
            sels.append(f"#{b['id']}")
        if b.get("name"):
            sels.append(f"input[name={b['name']}]")
    for f in rec.get("forms", []):
        for i in f["inputs"]:
            if i.get("name") and i.get("type") in (None, "text", "search"):
                sels.append(f"input[name={i['name']}]")
    return ", ".join(dict.fromkeys(sels)) or "input[placeholder*=搜索], input[placeholder*=关键]"


def first_visible(pg, sel, maxn=10):
    loc = pg.locator(sel)
    for i in range(min(loc.count(), maxn)):
        c = loc.nth(i)
        try:
            if c.is_visible() and c.is_enabled():
                return c
        except Exception:
            continue
    return None


def templatize(url, kw):
    for f in (urllib.parse.quote(kw), urllib.parse.quote_plus(kw), kw):
        if f in url:
            return url.replace(f, "{kw}")
    return None


def cmd_forms(src):
    raw = json.load(open(src, encoding="utf-8"))
    hosts = ({h: "" for h in raw} if isinstance(raw, list) else raw)
    out_p = os.path.splitext(src)[0] + "-首页勘察.json"
    done = json.load(open(out_p, encoding="utf-8")) if os.path.exists(out_p) else {}
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = connect(p).contexts[0]
        for host, desc in hosts.items():
            if host in done:
                continue
            pg = ctx.new_page()
            try:
                pg.goto(host, wait_until="domcontentloaded", timeout=45000)
                pg.wait_for_timeout(4000)
                r = pg.evaluate(JS_FORMS)
                r["说明"] = desc
                r["建议box选择器"] = suggest_box(r)
                print(f"  {desc[:24]:26s} form={len(r['forms'])} "
                      f"框={len(r['boxes'])} 链接={len(r['links'])}  «{r['title'][:26]}»")
                print(f"      box: {r['建议box选择器'][:88]}")
            except Exception as e:
                r = {"说明": desc, "错误": f"{type(e).__name__}: {str(e)[:110]}"}
                print(f"  {desc[:24]:26s} ✗ {r['错误'][:66]}")
            finally:
                if not pg.is_closed():
                    pg.close()
            done[host] = r
            atomic_write_json(out_p, done)
            time.sleep(random.uniform(2, 4))
    print(f"\n→ {out_p}\n把「建议box选择器」填进 targets.json 的 box 字段，再跑 --drive")
    return 0


def cmd_drive(src):
    targets = json.load(open(src, encoding="utf-8"))
    out_p = os.path.splitext(src)[0] + "-实操.json"
    done = json.load(open(out_p, encoding="utf-8")) if os.path.exists(out_p) else {}
    from playwright.sync_api import sync_playwright
    print(f"\n关键词：{CONTROL}\n")
    with sync_playwright() as p:
        ctx = connect(p).contexts[0]
        for t in targets:
            home, desc, sel = t["home"], t.get("desc", t["home"]), t["box"]
            if done.get(home, {}).get("入口模板"):
                continue
            pg = ctx.new_page()
            rec = {"说明": desc, "首页": home}
            try:
                pg.goto(home, wait_until="domcontentloaded", timeout=45000)
                pg.wait_for_timeout(3500)
                box = first_visible(pg, sel)
                if box is None:
                    rec["结论"] = "首页上找不到可见的检索框"
                else:
                    box.click(timeout=8000)
                    box.fill(CONTROL, timeout=8000)
                    pg.wait_for_timeout(500)
                    before = pg.url
                    # 先敲回车；很多政务站没绑 Enter，再逐个试按钮
                    triggers = [("Enter", None)] + [(None, b) for b in BTN]
                    for key, bsel in triggers:
                        try:
                            el = box if key else first_visible(pg, bsel, maxn=4)
                            if el is None:
                                continue
                            try:
                                with ctx.expect_page(timeout=8000) as pi:
                                    el.press(key) if key else el.click(timeout=5000)
                                tgt = pi.value
                                tgt.wait_for_load_state("domcontentloaded", timeout=20000)
                            except Exception:
                                tgt = pg
                            pg.wait_for_timeout(4000)
                            body = tgt.locator("body").inner_text(timeout=12000)
                            rec.update({"最终URL": tgt.url, "标题": tgt.title(),
                                        "正文长度": len(body),
                                        "报空": next((w for w in EMPTY if w in body), None),
                                        "触发方式": key or bsel,
                                        "入口模板": templatize(tgt.url, CONTROL)})
                            if tgt is not pg and not tgt.is_closed():
                                tgt.close()
                            if rec.get("入口模板") or tgt.url != before:
                                break
                        except Exception:
                            continue
                    rec["结论"] = ("取到结果页 URL" if rec.get("入口模板")
                                   else "触发了但 URL 没变（站内 AJAX，无独立结果页 URL）")
            except Exception as e:
                rec["结论"] = f"{type(e).__name__}: {str(e)[:110]}"
            finally:
                if not pg.is_closed():
                    pg.close()
            done[home] = rec
            atomic_write_json(out_p, done)
            print(f"  {'✓' if rec.get('入口模板') else '·'} {desc[:24]:26s} "
                  f"{rec['结论'][:40]:42s} {(rec.get('入口模板') or '')[:60]}")
            time.sleep(random.uniform(2, 4))
    got = [v for v in done.values() if v.get("入口模板")]
    print(f"\n取到 {len(got)}/{len(done)} 个入口模板：")
    for v in got:
        print(f"  ✓ {v['说明'][:24]:26s} {v['入口模板']}")
    print(f"\n→ {out_p}\n这些入口仍须过 entry_triage.py 的对照组才能进跑批。")
    return 0


def main():
    argv = sys.argv[1:]
    if "--forms" in argv:
        return cmd_forms(argv[argv.index("--forms") + 1])
    if "--drive" in argv:
        return cmd_drive(argv[argv.index("--drive") + 1])
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
