#!/usr/bin/env python3
"""站点库冒烟测试 —— 接手项目、浏览器升级或站点改版后，先跑这个。

用一个**确定不存在**的主体名去检索每个 verified 站点：正常站点应当返回明确的
空结果措辞。这样既验证入口还通，又验证 exp 基准还对得上，而且不会污染真实项目
的判定——因为查的是个假名字。

跑不通的站点在这里暴露，比在批量留痕时产出几十份可疑件要便宜得多。

用法：
  python3 smoke_sites.py                    # 测全部 verified 站点
  python3 smoke_sites.py --site=C24         # 只测一个
  python3 smoke_sites.py --include-probed   # 连 status=probed 的一起测
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from capture import connect  # noqa: E402
from evidence_core import bad_page_reason  # noqa: E402

# 不存在的主体名。正常站点必须返回空结果——若返回非零，说明该站在做拆词匹配，
# 这本身就是要记下来的站点特性（见 SKILL.md 判定纪律第 5 条）。
PROBE_NAME = "测试无此主体壹贰叁光电科技有限公司"


def first_visible(pg, sel):
    loc = pg.locator(sel)
    for i in range(min(loc.count(), 8)):
        c = loc.nth(i)
        try:
            if c.is_visible() and c.is_enabled():
                return c
        except Exception:
            continue
    return None


def check(ctx, sid, cfg):
    exp = tuple(cfg.get("exp") or ())
    if not exp:
        return "SKIP", "配置未记录 exp 基准，无法自动判定"
    pg = ctx.new_page()
    try:
        # 站点可指定自己的冒烟词（搜索引擎类用常用词，见 verified_sites.json）
        q = urllib.parse.quote(cfg.get("smoke_probe") or PROBE_NAME)
        if cfg["type"] in ("url", "url_click"):
            pg.goto(cfg["tmpl"].format(q=q), wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(2500)
            if cfg["type"] == "url_click":
                for sel in cfg.get("click", []):
                    btn = first_visible(pg, sel)
                    if btn is not None:
                        try:
                            btn.click(timeout=5000)
                            pg.wait_for_timeout(2500)
                            break
                        except Exception:
                            continue
        else:
            return "SKIP", f"类型 {cfg['type']} 需人工冒烟"

        body = ""
        for _ in range(10):
            pg.wait_for_timeout(1300)
            try:
                body = pg.locator("body").inner_text(timeout=5000)
            except Exception:
                continue
            if any(k in body for k in exp):
                break
        else:
            return "FAIL", f"10 轮未等到 exp {exp} —— 站点可能已改版，重新探入口"

        reason = bad_page_reason(body, pg.title(), pg.url)
        if reason:
            return "FAIL", f"命中错误/认证页：{reason}"

        line = next((l.strip() for l in body.split("\n")
                     if any(k in l for k in exp) and len(l.strip()) < 80), "")
        return "OK", line or "已命中 exp"
    except Exception as e:
        return "FAIL", f"{type(e).__name__}: {str(e)[:110]}"
    finally:
        if not pg.is_closed():
            pg.close()


def main():
    only = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--site=")), None)
    inc_probed = "--include-probed" in sys.argv

    with open(os.path.join(ROOT, "verified_sites.json"), encoding="utf-8") as f:
        sites = json.load(f)["sites"]

    wanted = {"verified"} | ({"probed"} if inc_probed else set())
    rows, nfail = [], 0
    with sync_playwright() as p:
        ctx = connect(p).contexts[0]
        for sid, cfg in sites.items():
            if only and sid != only:
                continue
            st = cfg.get("status")
            if st not in wanted:
                if not only:
                    rows.append((sid, cfg["name"], st.upper(),
                                 cfg.get("blocker") or cfg.get("reason") or "未纳入自动冒烟"))
                    continue
            res, detail = check(ctx, sid, cfg)
            nfail += res == "FAIL"
            rows.append((sid, cfg["name"], res, detail))
            print(f"{sid:5s} {cfg['name'][:22]:24s} {res:5s} {detail[:60]}", flush=True)

    print("\n" + "=" * 70)
    for tag in ("FAIL", "SKIP"):
        hit = [r for r in rows if r[2] == tag]
        if hit:
            print(f"\n{tag}（{len(hit)}）：")
            for sid, name, _, d in hit:
                print(f"  {sid} {name}: {d}")
    print(f"\n合计 {len(rows)} 项，FAIL {nfail} 项。")
    if nfail:
        print("FAIL 的站点在重新探明入口并更新 verified_sites.json 之前，不要用于批量留痕。")
    return 1 if nfail else 0


if __name__ == "__main__":
    raise SystemExit(main())
