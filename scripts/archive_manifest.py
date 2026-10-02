#!/usr/bin/env python3
"""按 CSV 清单批量归档 Downloads 文件；默认只预演，绝不猜归属。

CSV 列：source,target_dir,target_name

  python3 archive_manifest.py 归档清单.csv
  python3 archive_manifest.py 归档清单.csv --apply
  python3 archive_manifest.py 归档清单.csv --apply --move

source 相对路径默认相对于 ~/Downloads；target_dir 可用绝对路径或相对当前目录。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_source(raw: str, source_root: str) -> str:
    return os.path.abspath(raw if os.path.isabs(raw) else os.path.join(source_root, raw))


def resolve_target(raw_dir: str, raw_name: str) -> str:
    directory = os.path.abspath(raw_dir)
    name = os.path.basename(raw_name)
    if name != raw_name or not name:
        raise ValueError(f"target_name 必须是纯文件名：{raw_name}")
    return os.path.join(directory, name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    parser.add_argument("--source-root", default=os.path.expanduser("~/Downloads"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--move", action="store_true", help="校验复制成功后删除源文件；须与 --apply 同用")
    parser.add_argument("--log", default="archive_audit.jsonl")
    args = parser.parse_args()
    if args.move and not args.apply:
        parser.error("--move 必须与 --apply 同用")

    with open(args.manifest, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    required = {"source", "target_dir", "target_name"}
    if not rows or not required.issubset(rows[0]):
        print("清单为空或缺少列：source,target_dir,target_name")
        return 2

    plan = []
    errors = []
    for i, row in enumerate(rows, 2):
        try:
            src = resolve_source(row["source"].strip(), os.path.abspath(args.source_root))
            dst = resolve_target(row["target_dir"].strip(), row["target_name"].strip())
            if not os.path.isfile(src):
                raise FileNotFoundError(f"源文件不存在：{src}")
            if os.path.exists(dst):
                same = os.path.isfile(dst) and sha256(src) == sha256(dst)
                if same:
                    plan.append((i, src, dst, "已归档（哈希一致）"))
                    continue
                raise FileExistsError(f"目标已存在且内容不同：{dst}")
            plan.append((i, src, dst, "待移动" if args.move else "待复制"))
        except Exception as exc:
            errors.append((i, str(exc)))

    for i, src, dst, status in plan:
        print(f"第{i}行 [{status}] {src} -> {dst}")
    for i, error in errors:
        print(f"第{i}行 [错误] {error}")
    if errors:
        print("存在错误，整批未执行。")
        return 2
    if not args.apply:
        print(f"预演完成：{len(plan)} 项。确认后加 --apply。")
        return 0

    audit = []
    for i, src, dst, status in plan:
        if status.startswith("已归档"):
            digest = sha256(dst)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            digest = sha256(src)
            if digest != sha256(dst):
                os.unlink(dst)
                raise IOError(f"复制后哈希不一致，已撤回目标：{dst}")
            if args.move:
                os.unlink(src)
        audit.append({
            "row": i,
            "source": src,
            "target": dst,
            "sha256": digest,
            "action": (
                "already_present"
                if status.startswith("已归档")
                else ("move" if args.move else "copy")
            ),
            "at": datetime.now().astimezone().isoformat(),
        })

    with open(args.log, "a", encoding="utf-8") as f:
        for record in audit:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"执行完成：{len(audit)} 项；审计日志：{os.path.abspath(args.log)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
