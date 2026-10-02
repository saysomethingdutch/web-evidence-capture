#!/usr/bin/env python3
"""复核已落盘的留痕件，专抓「看着成功、其实没检索」的假阳性。

判定纪律拦的是落盘那一刻；这个脚本拦的是**落盘之后**。实测一批 151 份省级留痕件，
判定层全部放行、标签也都写好了，这里仍抓出 29 份不能进底稿的：

  ① 结果页停在站点首页 —— URL 没变、正文是新闻列表，检索压根没触发
  ② 结果页空白 / 留着加载态 —— 前端还在渲染就打印了
  ③ 出现验证码、滑块 —— 结果区是假的
  ④ 单页直达件里找不到检索词 —— 证明不了检索的是本主体
  ⑤ 检索词只挂在 URL 的井号片段里 —— 前端可能根本不读

最危险的不是被标了★的那些，而是**标着「无记录」却根本没查**的那几份：
它们会一路走进查询笔录，变成白纸黑字的「已查、未发现」。

用法：
  python3 audit_evidence.py 进度_xxx.json 留痕目录
  python3 audit_evidence.py 进度_xxx.json 留痕目录 --json 复核结果.json
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import urllib.parse

CAPTCHA = re.compile(r"验证码|滑块|拖动.{0,4}完成|人机验证|安全验证|captcha", re.I)
LOADING = re.compile(r"正在搜索|正在加载|正在查询|加载中|请稍[后候]|loading", re.I)
RESULT = re.compile(r"相关结果|搜索结果|检索结果|查询结果|共\s*\d|检索到|找到|条结果|"
                    r"未能搜到|没有找到|暂无数据|未查询到|无符合条件|结果共")

# 已实测证伪「前端不读井号片段」的站点：同一主体名，分别开首页和开片段 URL，
# 两者返回不同（首页是新闻列表、片段 URL 给出明确计数）才算证伪。
# 没证伪过的站，片段里的检索词不能当作检索契约。
FRAGMENT_OK = set()


def pdf_text(path: str) -> str:
    return subprocess.run(["pdftotext", "-layout", path, "-"],
                          capture_output=True, text=True, timeout=180).stdout


def strip_furniture(text: str) -> str:
    """去掉每页首行（页眉：标题+时间）和末行（页脚：网址+页码）。

    页眉印的是 document.title，单页应用常年停在「加载中」；
    不剥掉就会把正文完好的空结果件判成半成品。
    """
    out = []
    for page in text.split("\f"):
        lines = [x for x in page.splitlines() if x.strip()]
        if len(lines) > 2:
            lines = lines[1:-1]
        out.append("\n".join(lines))
    return "\n".join(out)


def result_region(text: str, two_page: bool) -> str:
    """结果区 = 除第 1 页（检索条件页）外的全部页面。

    **不是「第 2 页」，也不是「最后一页」。** 只看第 2 页会漏掉三页件的真结果页；
    只看末页会把页脚导航当结果区 —— 六页件的「未能搜到」在第 2 页，末页是备案号。
    """
    pages = [x for x in text.split("\f") if x.strip()]
    return "\n".join(pages[1:] if (two_page and len(pages) > 1) else pages)


def audit_one(rec: dict, path: str) -> list[str]:
    """→ 可疑理由列表；空列表表示通过。"""
    if not os.path.exists(path):
        return ["文件不在磁盘上"]
    text = pdf_text(path)
    two = str(rec.get("取证方式", "")).startswith("两页一组")
    body = " ".join(result_region(text, two).split())
    kw = rec.get("keyword") or ""
    url = rec.get("url") or ""
    why = []

    if len(body) < 120:
        why.append(f"结果页近乎空白（{len(body)} 字）—— 多半是前端没渲染完就打印了")
    m = LOADING.search(strip_furniture(text))
    if m:
        why.append(f"正文留着加载态「{m.group(0)}」")
    m = CAPTCHA.search(text)
    if m:
        why.append(f"出现验证词「{m.group(0)}」")
    if len(body) >= 120 and not RESULT.search(body):
        why.append("结果区无任何计数/空结果措辞，看不出检索是否执行")

    u = urllib.parse.urlparse(url)
    if u.path in ("", "/") and not u.query and not u.fragment:
        why.append(f"结果页 URL 仍是站点首页（{url}）—— 检索未触发")
    # 检索词可能以百分号编码出现在页脚网址里；只找中文会漏判
    seen = (re.sub(r"\s+", "", kw) in re.sub(r"\s+", "", text)
            or (kw and urllib.parse.quote(kw) in text))
    if not two and kw and not seen:
        why.append("单页直达件，打印件正文和页脚网址里都找不到检索词 —— 无法证明检索的是本主体")
    if u.fragment and "keyword" in (u.fragment + u.query) and u.netloc not in FRAGMENT_OK:
        why.append(f"检索词挂在 URL 的井号片段里（#{u.fragment[:40]}…），"
                   f"未验证前端是否读取，需确认结果区确实按词过滤过")
    return why


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        print(__doc__)
        return 2
    ledger, outdir = args[0], args[1]
    dump = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--json=")), None)

    d = json.load(open(ledger, encoding="utf-8"))
    bad, ok = [], 0
    for key, rec in sorted(d.items()):
        if not isinstance(rec, dict) or not rec.get("file"):
            continue
        why = audit_one(rec, os.path.join(outdir, rec["file"]))
        if why:
            bad.append({"key": key, "问题": "；".join(why), "file": rec["file"],
                        "result": rec.get("result")})
        else:
            ok += 1
    print(f"\n复核 {ok + len(bad)} 份：通过 {ok}，可疑 {len(bad)}\n" + "=" * 96)
    for x in bad:
        flag = "  ★危险" if not str(x["result"]).startswith("★") else ""
        print(f"\n✗ {x['key']}  【{x['result']}】{flag}\n   {x['问题']}\n   {x['file']}")
    if bad:
        print("\n" + "=" * 96)
        print("标着「无记录」却被判可疑的，优先处理 —— 它们会直接走进查询笔录。")
    if dump:
        json.dump(bad, open(dump, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n→ {dump}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
