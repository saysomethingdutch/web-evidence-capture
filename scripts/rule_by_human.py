#!/usr/bin/env python3
"""记录人工认定，并留下可追溯的改判痕迹。

`relabel.py` 是机器重判：从 PDF 文本重新推结论。这个脚本是**人拍的板**——
律师调阅原件核过之后说「这条可以排除」，台账要如实记下来。两件事性质不同：
前者可以随时重跑，后者是一次法律判断，必须写清楚是谁、什么时候、凭什么。

三条规矩：

1. **只改台账标签和文件名，PDF 一个字节不动。** 改判改的是结论，不是证据。
2. **新标签保留「（经人工核阅）」字样。** 它跟机器判出的「无记录」不是一回事：
   后者靠页面上的空结果措辞或零计数，前者靠人看过原件。底稿里这两种结论
   份量不同，标签上就该分得开 —— 复核人一眼能看出哪些格子是人拍的板。
3. **同名排除尤其要写明「凭什么排除」**：靠案由和时间对不上，还是核对过身份证号。
   这句话将来是要顶事的，写「经核阅无关」等于没写。

用法：
  # 先看还有哪些待核阅
  python3 rule_by_human.py --list 台账.json [台账2.json ...]

  # 逐条认定：把个案理由写进 RULINGS，或用 --rulings 指向一个 json
  python3 rule_by_human.py 台账.json 留痕目录 --rulings=认定.json
  python3 rule_by_human.py 台账.json 留痕目录 --rulings=认定.json --apply

  # 成批认定：人已把全部★件核完，统一套用一句话（个案理由仍从 --rulings 取）
  python3 rule_by_human.py 台账.json 留痕目录 --bulk --apply
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import os
import sys

BULK_LABEL = "无记录（经人工核阅）"
BULK_WHY = ("经办人调阅留痕件原件逐份核阅，确认结果区内容与被查询主体无关，"
            "未发现被查询主体的负面信息。原机器判定为「{old}」，理由：{why}。")
BY = "经办人人工核阅认定"


def sha(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def cmd_list(ledgers: list[str]) -> int:
    n = 0
    for lg in ledgers:
        d = json.load(open(lg, encoding="utf-8"))
        for k, v in sorted(d.items()):
            if isinstance(v, dict) and str(v.get("result", "")).startswith("★"):
                n += 1
                print(f"\n{v['result']}　{k}")
                print(f"   {(v.get('reason') or '')[:140]}")
                print(f"   {v.get('file', '')}")
    print(f"\n待核阅 {n} 份。核完把认定写成 json：{{台账键: [新结论, 改判依据]}}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("args", nargs="*")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--bulk", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rulings", default=None)
    ap.add_argument("-h", "--help", action="store_true")
    o = ap.parse_args()
    if o.help or not o.args:
        print(__doc__)
        return 2
    if o.list:
        return cmd_list(o.args)
    if len(o.args) < 2:
        print("用法：rule_by_human.py 台账.json 留痕目录 [--rulings=…] [--bulk] [--apply]")
        return 2
    ledger, outdir = o.args[0], o.args[1]

    rulings = {}
    if o.rulings:
        rulings = {k: tuple(v) for k, v in
                   json.load(open(o.rulings, encoding="utf-8")).items()}
    d = json.load(open(ledger, encoding="utf-8"))
    if o.bulk:
        for k, v in d.items():
            if isinstance(v, dict) and str(v.get("result", "")).startswith("★") \
                    and k not in rulings:
                rulings[k] = (BULK_LABEL,
                              BULK_WHY.format(old=v["result"],
                                              why=(v.get("reason") or "")[:150]))
    if not rulings:
        print("没有要改判的。用 --list 看待核阅清单，或用 --rulings 传认定文件。")
        return 0

    now = datetime.datetime.now().astimezone().isoformat()
    n, changed = 0, False
    for key, (new, why) in rulings.items():
        v = d.get(key)
        if not v or v.get("result") == new:
            continue
        old_path = os.path.join(outdir, v["file"])
        if not os.path.exists(old_path):
            print(f"✗ {key}：文件不在 {old_path}")
            continue
        new_file = v["file"].replace("-" + v["result"].lstrip("★"),
                                     "-" + new.lstrip("★"))
        print(f"{'✓' if o.apply else '·'} {key}\n   {v['result']} → {new}"
              f"\n   {v['file']}\n   → {new_file}\n   依据：{why[:100]}…")
        if o.apply:
            dst = os.path.join(outdir, new_file)
            os.rename(old_path, dst)
            v.update({"原判定": v["result"], "原判定理由": v.get("reason"),
                      "result": new, "reason": why, "改判依据": why,
                      "认定人": BY, "改判时间": now,
                      "改判方法": "仅改台账标签与文件名，PDF 内容未作任何修改",
                      "file": new_file, "sha256": sha(dst)})
            changed = True
        n += 1
    if o.apply and changed:
        tmp = ledger + ".tmp"
        json.dump(d, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, ledger)
    print(f"\n{'已改判' if o.apply else '预演'} {n} 份"
          + ("" if o.apply else "\n（预演。加 --apply 执行）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
