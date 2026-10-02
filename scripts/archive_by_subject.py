#!/usr/bin/env python3
"""把散在各批的留痕件汇成一套按主体组织的底稿，并处理撞名。

`archive_manifest.py` 是 CSV 点名复制；这个是从台账批量汇总，重点在**撞名怎么办**。

主体归属一律来自**台账键或源目录名，绝不按文件名相似度猜** —— 按文件名认主体
翻过车：`公司名_自然人名_hash.pdf` 被认成公司，6 份互相覆盖。

撞名分三步，能按内容分开就绝不加来源后缀：

  第 1 步 去重。两种重复：
     ① 同一主体、**同一条检索 URL** 跑了两遍（分批跑难免）
     ② 同一部门换了入口跑两遍（URL 不同，问的是同一件事）→ 按 PREFER 定夺
     **挑哪份留：先看覆盖，再看名字好不好读。** 反过来会出事 —— 同一条 URL 在
     两批里各跑一次，名字好看那份（「市级 X市政府门户」）台账里没记覆盖行，
     名字难看那份（「市级 X市Y区 www.x.gov.cn」）带着 6 行覆盖；只按名字挑，
     底稿上那 6 行会当场从「已查」变成「未查询」。
     覆盖留最全那份，名字取最好读那份，两样都要；**被丢那份的覆盖行要并过来**。

  第 2 步 按内容改名：
     · 范围限定检索 → 读留痕件页首印着的**范围标签**当名字（「市级 深圳市应急管理局」）
     · 商业报告 vs 本所自查（是两样东西，都要留）→ 加内容后缀（「-网查报告」）

  第 3 步 还撞才退回加来源后缀。兜底，不是常态。
     加 `（本所-国家级）` 这类后缀能防覆盖，但读起来像在给自己留退路，
     而且没说清两份差在哪。

另外：**渠道名里不能有分号。** 查询笔录的「留痕载体」列是用「；」串文件名的，
渠道名自带分号（层级写成「市级；区县级」）会让那列读不出边界。

用法：改下面 CONFIG 后
  python3 archive_by_subject.py            # 预演
  python3 archive_by_subject.py --apply    # 复制并逐份核对 SHA-256
"""
from __future__ import annotations
import collections
import glob
import hashlib
import json
import os
import re
import shutil
import sys

# ── 改这里 ────────────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "归档")
# 主体编号 → (归档序号, 名称, 分类目录)
SUBJECTS: dict[str, tuple[int, str, str]] = {}
# 台账 → (留痕子目录, 渠道名怎么算, 来源标签)
LEDGERS: list[tuple[str, str, callable, str]] = []
# 同一域名挂多个部门时，URL 片段 → 页首印着的范围标签
SCOPE: dict[str, str] = {}
# 商业报告那一方加什么后缀
REPORT_SRC, REPORT_SFX = "", "-报告"
# 换入口跑重了的：渠道名 → 留哪个来源
PREFER: dict[str, str] = {}
LABELS = (r"无记录（经人工核阅）|无记录|有结果需核阅|疑模糊匹配需甄别|"
          r"同名结果待核阅|需人工确认|疑无结果需确认|待判定|站点不可用（已留证）")
# ─────────────────────────────────────────────────────────────────────


def safe(v) -> str:
    return (re.sub(r'[\\/:*?"<>|\r\n]+', "_", str(v)).strip(" .") or "x")[:120]


def sha(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def clean_channel(c: str) -> str:
    """渠道名的表层规整。**只做可逆的**，不合并语义。"""
    c = re.sub(r"^[A-Z]\d+[a-z]?[_ ]", "", c)          # 剥内部编号前缀
    c = c.replace("_", "-")
    c = c.replace("；", "、").replace(";", "、")        # 见文件头：分号会破坏笔录那一列
    return re.sub(r"\s{2,}", " ", c).strip()


def strip_tail(fn: str) -> str:
    """从留痕文件名里剥掉结论标签和日期，只留渠道描述。"""
    s = fn[:-4] if fn.lower().endswith(".pdf") else fn
    s = re.sub(r"_\d{8}$", "", s)
    return re.sub(rf"-({LABELS})$", "", s)


def ledger_rows(root: str) -> dict[str, set]:
    """留痕文件名 → 它覆盖的行号。撞名取舍要看这个，不能只看名字好不好读。"""
    out = {}
    for lg in glob.glob(os.path.join(root, "进度_*.json")):
        try:
            d = json.load(open(lg, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        for v in d.values():
            if isinstance(v, dict) and v.get("file"):
                rs = set(v.get("覆盖矩阵行") or []) | set(v.get("模板行") or [])
                if rs:
                    out[v["file"]] = rs
    return out


def scope_of(rec: dict) -> str | None:
    for k, v in SCOPE.items():
        if k in (rec.get("url") or ""):
            return v
    return None


def folder(oid: str) -> str:
    n, nm, cat = SUBJECTS[oid]
    return os.path.join(OUT, cat, f"{n} {safe(nm)}")


def collect() -> list[dict]:
    R = []
    for lg, sub, chan, tag in LEDGERS:
        p = os.path.join(ROOT, lg)
        if not os.path.exists(p):
            continue
        for k, v in json.load(open(p, encoding="utf-8")).items():
            oid = k.split("|")[0]
            src = os.path.join(ROOT, sub, v.get("file", ""))
            if oid not in SUBJECTS or not os.path.exists(src):
                continue
            R.append({"oid": oid, "渠道": clean_channel(chan(k, v)),
                      "结论": v.get("result", ""), "来源": tag,
                      "src": src, "url": v.get("url", "")})
    return R


def resolve(R: list[dict]) -> tuple[list[dict], list[dict], dict]:
    cover = ledger_rows(ROOT)
    nrow = lambda r: len(cover.get(os.path.basename(r["src"]), ()))
    ugly = lambda r: bool(re.search(r"\.(gov\.)?cn", r["渠道"]))
    merged, dropped, keep = {}, [], []

    byurl = collections.defaultdict(list)
    for r in R:
        (byurl[(r["oid"], (r.get("url") or "").strip())] if r.get("url") else keep).append(r)
    for _, v in byurl.items():
        if len(v) == 1:
            keep.extend(v)
            continue
        v.sort(key=lambda x: (-nrow(x), ugly(x), len(x["渠道"])))     # 覆盖优先
        best = v[0]
        best["渠道"] = sorted(v, key=lambda x: (ugly(x), len(x["渠道"])))[0]["渠道"]
        merged[os.path.basename(best["src"])] = sorted(
            set().union(*(cover.get(os.path.basename(x["src"]), set()) for x in v)))
        keep.append(best)
        dropped += v[1:]

    bykey = collections.defaultdict(list)
    for r in keep:
        bykey[(r["oid"], r["渠道"])].append(r)
    keep2 = []
    for (_, ch), v in bykey.items():
        want = PREFER.get(ch)
        if want and len(v) > 1 and any(x["来源"] == want for x in v):
            for x in v:
                (keep2 if x["来源"] == want else dropped).append(x)
        else:
            keep2.extend(v)
    R = keep2

    for r in R:                                                        # 第 2 步
        sc = scope_of(r)
        if sc and sc not in r["渠道"]:
            r["渠道"] = (f"{r['渠道'].split(' ')[0]} {sc}"
                        if r["渠道"].startswith(("市级", "区县级", "省级")) else sc)
    bykey = collections.defaultdict(list)
    for r in R:
        bykey[(r["oid"], r["渠道"])].append(r)
    for _, v in bykey.items():
        if len(v) > 1 and REPORT_SRC and any(x["来源"] == REPORT_SRC for x in v):
            for x in v:
                if x["来源"] == REPORT_SRC:
                    x["渠道"] += REPORT_SFX
    for r in R:
        r["dst"] = os.path.join(folder(r["oid"]), f"{safe(r['渠道'])}.pdf")

    dup = {d for d, n in collections.Counter(r["dst"] for r in R).items() if n > 1}
    if dup:                                                            # 第 3 步：兜底
        print(f"\n仍有 {len(dup)} 个去向撞名，退回加来源后缀区分：")
        for d in sorted(dup)[:5]:
            print("  ", os.path.relpath(d, OUT))
    seen = collections.Counter()
    for r in R:
        if r["dst"] in dup:
            seen[(r["dst"], r["来源"])] += 1
            n = seen[(r["dst"], r["来源"])]
            r["渠道"] += f"（{r['来源']}{'' if n == 1 else n}）"
            r["dst"] = os.path.join(folder(r["oid"]), f"{safe(r['渠道'])}.pdf")
    return R, dropped, merged


def main() -> int:
    if not SUBJECTS or not LEDGERS:
        print(__doc__)
        print("!! 先在文件头把 SUBJECTS / LEDGERS 配好。")
        return 2
    apply_ = "--apply" in sys.argv
    R, dropped, merged = resolve(collect())
    if dropped:
        print(f"\n同站同检索跑重了，各留一份（另 {len(dropped)} 份留在留痕目录，不进底稿）：")
        for x in dropped[:5]:
            print(f"  略过 {x['oid']} {x['渠道'][:40]}（{x['来源']}）")
    print(f"\n共 {len(R)} 份；去向重名 "
          f"{len(R) - len({r['dst'] for r in R})}")
    for k, v in collections.Counter(r["来源"] for r in R).most_common():
        print(f"  {k:<20}{v:>6}")
    if not apply_:
        print("\n【预演】加 --apply 执行。")
        return 0
    bad = 0
    for r in R:
        os.makedirs(os.path.dirname(r["dst"]), exist_ok=True)
        shutil.copy2(r["src"], r["dst"])
        if sha(r["src"]) != sha(r["dst"]):
            bad += 1
            print(f"★ SHA 不一致：{r['dst']}")
    json.dump(R, open(os.path.join(ROOT, "归档_记录.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, default=str)
    json.dump(merged, open(os.path.join(ROOT, "归档_覆盖合并.json"), "w",
                           encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n已归档 {len(R)} 份；SHA-256 不一致 {bad}\n→ {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
