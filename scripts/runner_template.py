#!/usr/bin/env python3
"""批量网页留痕 runner 骨架。

用法：
  python3 runner_template.py --config=项目配置.json [--site=S1]

配置文件可覆盖脚本内的 SITES/TARGETS/输出目录。runner 先验证结果页，再打印到
临时目录，最后从存档 PDF 判定并归档；错误页进入隔离区且不计完成。
"""
from __future__ import annotations

import datetime
import json
import os
import random
import re
import shutil
import sys
import time
import urllib.parse

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from capture import connect, grab, pdf_text
from evidence_core import (
    atomic_write_json,
    bad_page_reason,
    classify_pdf_text,
    file_sha256,
)

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(ROOT, "留痕")
DEFAULT_LOG = os.path.join(ROOT, "进度.json")
TODAY = datetime.date.today().strftime("%Y%m%d")
DEFAULT_GAP = (4.0, 9.0)

# 复制改造时替换这里，或使用 --config JSON。
SITES = {
    "S1": {
        "name": "示例URL检索站",
        "type": "url",
        "tmpl": "https://example.com/search?q={q}",
        "wait": "domcontentloaded",
        "result_url_regex": r"/search(?:[/?]|$)",
    },
    "S2": {
        "name": "示例表单检索站",
        "type": "form",
        "url": "https://example.org/",
        "input": "input[name=keyword]",
        # 复杂站点务必显式配置 button；不要让脚本在全页猜提交按钮。
        "button": "button.search",
        "wait": "domcontentloaded",
        "result_url_regex": r"/result(?:[/?]|$)",
    },
}

TARGETS = [
    ("E1", "示例公司", "示例科技股份有限公司"),
    ("E2", "示例子公司", "示例子公司名称"),
]

GENERIC_RESULT_MARK = (
    "为您找到", "搜索结果", "检索结果", "查询结果", "共检索到",
    "未找到相关结果", "没有符合条件", "暂无数据",
)


def safe_name(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|\r\n]+', "_", value).strip(" .")
    return value[:160] or "未命名"


def load_config(argv: list[str]) -> tuple[dict, list, str, str, tuple[float, float]]:
    cfg_path = next((a.split("=", 1)[1] for a in argv if a.startswith("--config=")), None)
    if not cfg_path:
        return SITES, TARGETS, DEFAULT_OUT, DEFAULT_LOG, DEFAULT_GAP
    cfg_path = os.path.abspath(cfg_path)
    with open(cfg_path, encoding="utf-8") as f:
        data = json.load(f)
    base = os.path.dirname(cfg_path)

    def resolved(value: str, default: str) -> str:
        value = value or default
        return value if os.path.isabs(value) else os.path.join(base, value)

    targets = []
    for row in data.get("targets", []):
        if isinstance(row, dict):
            targets.append((row["code"], row["short"], row["keyword"]))
        else:
            targets.append(tuple(row))
    gap = tuple(data.get("gap", DEFAULT_GAP))
    if len(gap) != 2:
        raise ValueError("gap 必须是 [最短秒数, 最长秒数]")
    return (
        data["sites"],
        targets,
        resolved(data.get("out"), DEFAULT_OUT),
        resolved(data.get("log"), DEFAULT_LOG),
        (float(gap[0]), float(gap[1])),
    )


def load_ledger(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError("台账顶层必须是 JSON object")
    return value


def _visible(locator) -> bool:
    try:
        return locator.count() > 0 and locator.first.is_visible() and locator.first.is_enabled()
    except Exception:
        return False


def _first_visible(page, selector: str):
    """兼容不同 Playwright 版本：逐个检查，不依赖较新的 filter(visible=True)。"""
    locator = page.locator(selector)
    for index in range(locator.count()):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible() and candidate.is_enabled():
                return candidate
        except Exception:
            continue
    return None


def _has_result_signal(pg, cfg: dict, home_url: str) -> bool:
    try:
        if cfg.get("result_selector") and pg.locator(cfg["result_selector"]).count():
            return True
        if cfg.get("result_url_regex") and re.search(cfg["result_url_regex"], pg.url, re.I):
            return True
        body = pg.locator("body").inner_text(timeout=2000)
        marks = tuple(cfg.get("result_marks", GENERIC_RESULT_MARK))
        return any(mark in body for mark in marks) and pg.url != home_url
    except Exception:
        return False


def _wait_result(ctx, source_pg, before_pages: set, home_url: str, cfg: dict):
    """同时观察 popup 和同页结果；popup 的 about:blank 要等到真实 URL 出现。"""
    deadline = time.time() + float(cfg.get("result_timeout", 12))
    while time.time() < deadline:
        new_pages = [p for p in ctx.pages if p not in before_pages and not p.is_closed()]
        if new_pages:
            candidate = new_pages[-1]
            try:
                if candidate.url == "about:blank":
                    candidate.wait_for_timeout(300)
                if candidate.url != "about:blank":
                    try:
                        candidate.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:
                        pass
                    candidate.wait_for_timeout(int(cfg.get("settle_ms", 2500)))
                    return candidate, True
            except Exception:
                pass
        if _has_result_signal(source_pg, cfg, home_url):
            source_pg.wait_for_timeout(int(cfg.get("settle_ms", 2500)))
            return source_pg, False
        source_pg.wait_for_timeout(300)
    return None, False


def _button_candidates(cfg: dict) -> list[str]:
    configured = cfg.get("button")
    candidates = [configured] if configured else []
    # 语义明确的搜索按钮优先；全页通用 submit 最后，避免误点登录/筛选表单。
    candidates.extend([
        'button:has-text("查询")',
        'button:has-text("搜索")',
        'input[type=button][value*="查询"]',
        'input[type=button][value*="搜索"]',
        'input[type=submit][value*="查询"]',
        'input[type=submit][value*="搜索"]',
        ".search-btn", ".searchBtn", ".header_search_btn", "a.s-btn",
        '[onclick*="search" i]', '[onclick*="submit" i]',
        "button[type=submit]",
    ])
    out = []
    for item in candidates:
        if item and item not in out:
            out.append(item)
    return out


def do_form_search(ctx, pg, cfg: dict, keyword: str):
    """填表并只触发一次搜索；返回结果 page 与是否 popup。

    一旦某个按钮成功点击，就不再继续尝试其他按钮，防止连续点击多个提交控件。
    """
    input_sel = cfg["input"]
    try:
        box = _first_visible(pg, input_sel)
        if box is None:
            raise RuntimeError("选择器没有可见且可用的匹配项")
        box.wait_for(state="visible", timeout=10000)
        box.fill(keyword)
    except Exception as exc:
        return None, False, f"未找到可见且唯一的检索输入框：{exc}"

    home_url = pg.url
    before_pages = set(ctx.pages)
    clicked = False
    for selector in _button_candidates(cfg):
        try:
            button = pg.locator(selector)
            if not _visible(button):
                continue
            button.first.click(timeout=4000)
            clicked = True
            result_pg, is_popup = _wait_result(ctx, pg, before_pages, home_url, cfg)
            if result_pg:
                return result_pg, is_popup, None
            return None, False, f"已点击 {selector}，但未观察到结果页信号"
        except Exception:
            continue

    if not clicked and cfg.get("allow_enter", True):
        try:
            box = _first_visible(pg, input_sel)
            if box is None:
                raise RuntimeError("检索输入框已不可用")
            box.press("Enter")
            result_pg, is_popup = _wait_result(ctx, pg, before_pages, home_url, cfg)
            if result_pg:
                return result_pg, is_popup, None
        except Exception:
            pass
    return None, False, "未找到可用搜索按钮，回车也未触发结果页"


def valid_result_page(pg, cfg: dict, keyword: str) -> tuple[bool, str]:
    try:
        title = pg.title()
        body = pg.locator("body").inner_text(timeout=5000)
    except Exception as exc:
        return False, f"无法读取页面：{exc}"
    reason = bad_page_reason(body, title, pg.url)
    if reason:
        return False, reason
    if cfg.get("result_selector") and pg.locator(cfg["result_selector"]).count():
        return True, "命中配置的结果区选择器"
    if cfg.get("result_url_regex") and re.search(cfg["result_url_regex"], pg.url, re.I):
        return True, "URL 命中配置的结果页规则"
    marks = tuple(cfg.get("result_marks", GENERIC_RESULT_MARK))
    if any(mark in body for mark in marks):
        return True, "正文命中结果页标记"
    decoded_url = urllib.parse.unquote(pg.url)
    if keyword in decoded_url and pg.url != cfg.get("url"):
        return True, "URL 含检索词且已离开入口页"
    return False, "未找到可复核的结果页结构信号"


def quarantine(pending_path: str, out_dir: str, filename: str) -> str:
    qdir = os.path.join(out_dir, "异常页_不得作为底稿")
    os.makedirs(qdir, exist_ok=True)
    dest = os.path.join(qdir, filename)
    if os.path.exists(dest):
        stem, ext = os.path.splitext(filename)
        dest = os.path.join(qdir, f"{stem}-{int(time.time())}{ext}")
    os.replace(pending_path, dest)
    return dest


def main() -> int:
    only = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--site=")), None)
    sites, targets, out_dir, log_path, gap = load_config(sys.argv[1:])
    os.makedirs(out_dir, exist_ok=True)
    pending_dir = os.path.join(out_dir, ".pending")
    os.makedirs(pending_dir, exist_ok=True)
    done = load_ledger(log_path)
    counts = {"★有结果需核阅": 0, "无记录": 0, "★疑模糊匹配需甄别": 0}
    manual_list: list[str] = []

    with sync_playwright() as p:
        browser = connect(p)
        ctx = browser.contexts[0]
        for sid, cfg in sites.items():
            if only and sid != only:
                continue
            for code, short, keyword in targets:
                key = f"{code}|{sid}"
                if key in done:
                    continue
                pg = ctx.new_page()
                result_pg = None
                is_popup = False
                try:
                    if cfg["type"] == "url":
                        url = cfg["tmpl"].format(q=urllib.parse.quote(keyword))
                        pg.goto(url, wait_until=cfg.get("wait", "domcontentloaded"), timeout=60000)
                        pg.wait_for_timeout(int(cfg.get("settle_ms", 3500)))
                        result_pg = pg
                    elif cfg["type"] == "form":
                        pg.goto(
                            cfg["url"],
                            wait_until=cfg.get("wait", "domcontentloaded"),
                            timeout=60000,
                        )
                        pg.wait_for_timeout(int(cfg.get("entry_settle_ms", 2000)))
                        result_pg, is_popup, error = do_form_search(ctx, pg, cfg, keyword)
                        if not result_pg:
                            manual_list.append(f"{sid} {code}：{error}")
                            continue
                    else:
                        manual_list.append(f"{sid} {code}：未知站点类型 {cfg['type']}")
                        continue

                    ok, proof = valid_result_page(result_pg, cfg, keyword)
                    if not ok:
                        manual_list.append(f"{sid} {code}：{proof}")
                        continue

                    prefix = "_".join(map(safe_name, (code, short, sid, cfg["name"])))
                    pending_name = f"{prefix}-待判定_{TODAY}.pdf"
                    path, title, hit = grab(
                        ctx,
                        result_pg,
                        pending_name,
                        out_dir=pending_dir,
                        force=False,
                        pdf_opts=cfg.get("pdf_options"),
                    )
                    if not path:
                        manual_list.append(f"{sid} {code}：标题疑似认证/错误页 {title} {hit}")
                        continue

                    text = pdf_text(path)
                    result, reason = classify_pdf_text(
                        text,
                        keyword=keyword,
                        result_region_pattern=cfg.get("pdf_result_region_pattern"),
                    )
                    if "无效证据页" in result:
                        qpath = quarantine(path, out_dir, pending_name)
                        manual_list.append(
                            f"{sid} {code}：打印件无效（{reason}），已隔离 {os.path.basename(qpath)}"
                        )
                        continue

                    label = result.lstrip("★")
                    final_name = f"{prefix}-{safe_name(label)}_{TODAY}.pdf"
                    final_path = os.path.join(out_dir, final_name)
                    if os.path.exists(final_path):
                        raise FileExistsError(f"目标文件已存在，拒绝覆盖：{final_name}")
                    os.replace(path, final_path)
                    ledger_file = os.path.relpath(
                        final_path, os.path.dirname(os.path.abspath(log_path))
                    ).replace(os.sep, "/")
                    done[key] = {
                        "file": ledger_file,
                        "result": result,
                        "keyword": keyword,
                        "site": sid,
                        "url": result_pg.url,
                        "title": title,
                        "proof": proof,
                        "reason": reason,
                        "判定依据": "存档PDF",
                        "sha256": file_sha256(final_path),
                        "captured_at": datetime.datetime.now().astimezone().isoformat(),
                        "stem_prefix": prefix,
                        "date": TODAY,
                        "pdf_result_region_pattern": cfg.get("pdf_result_region_pattern"),
                    }
                    atomic_write_json(log_path, done)
                    counts[result] = counts.get(result, 0) + 1
                    print(f"{sid} {cfg['name']} / {code} {short}: ✓ {result}（{reason}）")
                except Exception as exc:
                    manual_list.append(f"{sid} {code}：{type(exc).__name__}: {str(exc)[:120]}")
                finally:
                    if is_popup and result_pg is not None and not result_pg.is_closed():
                        result_pg.close()
                    if not pg.is_closed():
                        pg.close()
                time.sleep(random.uniform(*gap))

    try:
        if not os.listdir(pending_dir):
            os.rmdir(pending_dir)
    except OSError:
        pass
    print(
        "\n本轮："
        + " / ".join(f"{k} {v}" for k, v in counts.items())
        + f" / 需人工 {len(manual_list)}；台账累计 {len(done)} 项"
    )
    if manual_list:
        print("\n需人工清单：")
        for item in manual_list:
            print("  -", item)
    print("\n下一步先运行 relabel.py（默认只报告；确认后再加 --apply）。")
    return 0 if not manual_list else 2


if __name__ == "__main__":
    raise SystemExit(main())
