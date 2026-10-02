#!/usr/bin/env python3
"""从归档 PDF 复核台账结论。

默认只报告，不修改：
  python3 relabel.py [台账.json ...]

确认差异后显式应用（会同步更新台账、文件名和哈希）：
  python3 relabel.py --apply [台账.json ...]
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from evidence_core import (
    atomic_write_json,
    classify_pdf_text,
    file_sha256,
    result_family,
)

ROOT = os.path.dirname(os.path.abspath(__file__))
LABEL_RE = re.compile(
    r"-(?:无记录|有结果需核阅|疑模糊匹配需甄别|结果页需人工判定|无效证据页)"
    r"(?=_[0-9]{8}\.pdf$)"
)


def pdf_text(path: str) -> tuple[str | None, str | None]:
    proc = subprocess.run(
        ["pdftotext", "-layout", path, "-"],
        capture_output=True,
        text=True,
    )
    if proc.returncode:
        return None, proc.stderr.strip() or f"pdftotext 退出码 {proc.returncode}"
    return proc.stdout, None


def find_file(ledger_path: str, entry: dict) -> tuple[str | None, str | None]:
    """优先使用台账相对路径；模糊搜索出现多个同名文件时拒绝猜。"""
    base = os.path.dirname(os.path.abspath(ledger_path))
    raw = entry.get("path") or entry.get("file")
    if not raw:
        return None, "台账缺少 file/path"
    direct = raw if os.path.isabs(raw) else os.path.join(base, raw)
    if os.path.isfile(direct):
        return direct, None
    hits = glob.glob(os.path.join(base, "**", os.path.basename(raw)), recursive=True)
    hits = [p for p in hits if os.path.isfile(p)]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, f"发现 {len(hits)} 个同名文件，拒绝猜测归属"
    return None, "存档文件不存在"


def proposed_name(entry: dict, old_path: str, new_result: str) -> str | None:
    label = new_result.lstrip("★")
    prefix = entry.get("stem_prefix")
    date = entry.get("date")
    if prefix and date:
        return os.path.join(os.path.dirname(old_path), f"{prefix}-{label}_{date}.pdf")
    name = os.path.basename(old_path)
    replaced = LABEL_RE.sub(f"-{label}", name)
    if replaced == name:
        return None
    return os.path.join(os.path.dirname(old_path), replaced)


def apply_change(entry: dict, old_path: str, new_result: str, reason: str) -> tuple[bool, str]:
    dest = proposed_name(entry, old_path, new_result)
    if not dest:
        return False, "无法从旧文件名安全生成新文件名"
    if os.path.abspath(dest) != os.path.abspath(old_path):
        if os.path.exists(dest):
            return False, f"目标文件已存在，拒绝覆盖：{os.path.basename(dest)}"
        os.rename(old_path, dest)
    else:
        dest = old_path
    entry["file"] = os.path.relpath(dest, os.path.dirname(os.path.abspath(entry["_ledger"])))
    if not entry["file"].startswith("."):
        entry["file"] = entry["file"].replace(os.sep, "/")
    entry["result"] = new_result
    entry["reason"] = reason
    entry["sha256"] = file_sha256(dest)
    entry["复核"] = "已按归档PDF订正，文件名与台账同步"
    return True, os.path.basename(dest)


def main() -> int:
    apply = "--apply" in sys.argv
    logs = [a for a in sys.argv[1:] if a.endswith(".json")]
    if not logs:
        logs = [
            f for f in os.listdir(ROOT)
            if f.endswith(".json") and ("进度" in f or "ledger" in f.lower())
        ]
    if not logs:
        print("未找到台账文件；请显式传入台账.json")
        return 2

    totals = {"checked": 0, "substantive": 0, "review_drift": 0, "missing": 0, "invalid": 0}
    for raw_log in logs:
        ledger_path = raw_log if os.path.isabs(raw_log) else os.path.abspath(raw_log)
        if not os.path.exists(ledger_path):
            candidate = os.path.join(ROOT, raw_log)
            ledger_path = candidate if os.path.exists(candidate) else ledger_path
        if not os.path.exists(ledger_path):
            print(f"{raw_log}: 文件不存在")
            totals["missing"] += 1
            continue
        try:
            with open(ledger_path, encoding="utf-8") as f:
                done = json.load(f)
        except Exception as exc:
            print(f"{raw_log}: 读取失败 {exc}")
            totals["invalid"] += 1
            continue

        ledger_changed = False
        distribution: dict[str, int] = {}
        for key, entry in done.items():
            if not isinstance(entry, dict) or not (entry.get("file") or entry.get("path")):
                continue
            totals["checked"] += 1
            evidence_path, err = find_file(ledger_path, entry)
            if err:
                totals["missing"] += 1
                print(f"  ⚠ {key}: {err}")
                continue
            text, err = pdf_text(evidence_path)
            if err:
                totals["invalid"] += 1
                print(f"  ⚠ {key}: PDF 读取失败：{err}")
                continue
            new_result, reason = classify_pdf_text(
                text,
                keyword=entry.get("keyword") or entry.get("检索词"),
                result_region_pattern=entry.get("pdf_result_region_pattern"),
            )
            distribution[new_result] = distribution.get(new_result, 0) + 1
            old_result = entry.get("result", "")
            old_family = result_family(old_result)
            new_family = result_family(new_result)

            if new_family == "invalid":
                totals["invalid"] += 1
                print(f"  ⚠ {key}: 归档件现判为无效证据页（{reason}），不自动改写")
                continue
            if old_family != new_family:
                totals["substantive"] += 1
                print(f"  ✗ {key}: 「{old_result}」→「{new_result}」（{reason}）")
                if apply:
                    entry["_ledger"] = ledger_path
                    changed, message = apply_change(entry, evidence_path, new_result, reason)
                    entry.pop("_ledger", None)
                    if changed:
                        ledger_changed = True
                        print(f"      已同步：{message}")
                    else:
                        print(f"      未应用：{message}")
                continue
            if old_result != new_result:
                totals["review_drift"] += 1
                print(
                    f"  △ {key}: 非空结果细分漂移「{old_result}」→「{new_result}」；"
                    "均需人工核阅，不自动翻动"
                )
        if ledger_changed:
            atomic_write_json(ledger_path, done)
        print(
            f"{os.path.basename(ledger_path)}: 分布 {distribution}"
            + ("；已应用实质冲突" if apply else "；仅报告")
        )

    print(
        "\n合计：复核 {checked}，实质冲突 {substantive}，非空细分漂移 "
        "{review_drift}，缺档 {missing}，无效/不可读 {invalid}".format(**totals)
    )
    if not apply and totals["substantive"]:
        print("确认无误后加 --apply；工具会同步台账、PDF 文件名和 SHA-256。")
    return 1 if totals["substantive"] or totals["missing"] or totals["invalid"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
