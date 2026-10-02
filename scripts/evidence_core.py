#!/usr/bin/env python3
"""网页留痕的共享判定与台账工具。

runner 与 relabel 必须共用这一层，否则两边的词表或优先级一旦漂移，
同一份 PDF 会被判成两个结论。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from typing import Any


EMPTY_PHRASES = (
    "未找到相关结果", "没有找到相关", "未找到与", "暂无数据", "暂无相关",
    "未查询到", "没有符合条件", "无符合条件记录", "没有找到", "未能搜到",
)

ERROR_PHRASES = (
    # 「已撤稿或删除」是内容不存在页，不是空结果页。实测呼和浩特市自然资源局
    # 对带全角括号的公司名返回此页，却被当成结果页存进底稿并判了「疑模糊匹配」——
    # 判定纪律第 2 条「站点报错 ≠ 检索无结果」在这里被破了。
    "页面已撤稿或删除", "已撤稿或删除", "您访问的页面不存在", "页面不存在或已删除",
    "对不起，您访问的页面", "站点不存在", "源站不可达",

    "出了点小问题", "系统出现异常", "请稍后重试", "请返回重试", "网络不给力",
    "服务异常", "访问过于频繁", "请求被阻断", "拒绝访问", "页面不存在",
    "not found", "service unavailable", "too many requests",

    # 云加速节点的网关错误页。实测北京法院网检索时结果页最后一页是
    # 「Error 502 网关错误 / 云节点 / 源服务器」，前面几页却是正常首页内容 ——
    # 单看首页会以为检索成功了，其实检索请求根本没到源站。
    "网关错误", "网关超时", "bad gateway", "gateway timeout", "源服务器",
)

# 前端还在渲染就打印 = 存下一张半成品。这几个词出现在正文里，说明结果区没定型。
# 实测信用中国（内蒙古）结果页只有一行「正在搜索，请稍后...」，
# 新疆法院网留着「请稍候」，都被当成有效结果页存了下来。
LOADING_PHRASES = (
    "正在搜索", "正在加载", "正在查询", "加载中", "请稍后", "请稍候", "数据加载中",
)

AUTH_PHRASES = (
    "请登录后继续", "登录后方可", "尚未登录", "请先登录", "扫码登录",
    "请输入验证码", "安全验证", "人机验证", "拖动滑块",
)

COUNT_PATTERNS = (
    re.compile(r"共(?:检索到|查询到|找到|有)?(?:约)?([0-9,，]+)(?:条|篇|个|项)"),
    re.compile(r"为您找到(?:约)?([0-9,，]+)(?:条|篇|个|项)"),
    re.compile(r"找到(?:约)?([0-9,，]+)(?:条|篇|个|项)"),
    # 「找到记录约 0 条，耗时 0.16 秒」——上海市公安局的措辞，动词和数字之间夹了名词。
    # 正则贴着「找到」写会漏掉它，于是一张回显了主体全称、明写 0 条的标准无记录页，
    # 被判成「未出现可复核计数、需人工确认」。**动宾之间要允许夹一个短名词。**
    re.compile(r"(?:找到|检索到|查询到|搜索到)[^0-9]{0,6}?(?:约)?([0-9,，]+)\s*(?:条|篇|个|项)"),
    # 「为您筛选到 379 条结果」——青岛市政府网站统一检索用的措辞，不含「找到」。
    # 漏掉它会让一个明明返回了结果的页面被判成「未出现可复核计数」。
    re.compile(r"筛选到(?:约)?([0-9,，]+)(?:条|篇|个|项)"),
    re.compile(r"相关结果[:：]?\s*(?:约)?([0-9,，]+)\s*(?:条|篇|个|项)"),
)


def compact(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).lower()


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_json(path: str, payload: Any) -> None:
    """同进程崩溃时不留下半截 JSON；多进程并发仍应使用独立台账。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ledger-", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _counts(flat: str) -> list[int]:
    nums: list[int] = []
    for pattern in COUNT_PATTERNS:
        for raw in pattern.findall(flat):
            try:
                nums.append(int(raw.replace(",", "").replace("，", "")))
            except ValueError:
                pass
    return nums


def bad_page_reason(text: str, title: str = "", url: str = "") -> str | None:
    """只匹配明确的错误/认证语句；不要用裸“登录”二字误伤正常结果页导航栏。"""
    flat = compact("\n".join((title, url, text[:4000])))
    if not flat:
        return "页面正文为空"
    for phrase in ERROR_PHRASES:
        if compact(phrase) in flat:
            return f"错误/风控页：{phrase}"
    for phrase in AUTH_PHRASES:
        if compact(phrase) in flat:
            return f"认证前置页：{phrase}"
    # 加载态**只看正文，不看标题**。单页应用常常把 <title> 一直留在「加载中」，
    # 正文却早已渲染完 —— 内蒙古公安厅的检索页正文明写「相关结果约 0 个／
    # 很抱歉！未能搜到您想要的内容」，标题却还是「加载中」，
    # 把标题算进来就会把一份完好的空结果件判成半成品。
    body_flat = compact(text[:4000])
    for phrase in LOADING_PHRASES:
        if compact(phrase) in body_flat:
            return f"页面未渲染完：正文留着「{phrase}」"
    # 状态码只在标题里认，不能在正文里搜。
    # 正文里的 403/412/429 极可能是正常内容：实测克拉玛依市政府检索结果页被判成
    # 「HTTP/WAF 拦截页」，元凶是新闻链接里的一段哈希 …bbdc429dad33…，其中含 429。
    # 正常证据被误扔进隔离区，比漏判一个拦截页更糟——拦截页还有正文空、
    # ERROR_PHRASES 等多重兜底，而误伤是静默的。
    head = compact("\n".join((title, url)))
    if re.search(r"(?:^|[^0-9])(?:403|412|429)(?:[^0-9]|$)", head):
        return "HTTP/WAF 拦截页"
    return None


def _extract_result_region(text: str, result_region_pattern: str | None) -> str | None:
    if not result_region_pattern:
        return None
    try:
        m = re.search(result_region_pattern, text, flags=re.S | re.I)
    except re.error:
        return None
    if not m:
        return None
    return m.group(1) if m.lastindex else m.group(0)


def _strip_print_furniture(text: str) -> str:
    """去掉每页的首行（页眉：标题+时间）和末行（页脚：网址+页码）。"""
    out = []
    for page in text.split("\f"):
        lines = [x for x in page.splitlines() if x.strip()]
        if len(lines) > 2:
            lines = lines[1:-1]
        out.append("\n".join(lines))
    return "\n".join(out)


def classify_pdf_text(
    text: str,
    keyword: str | None = None,
    result_region_pattern: str | None = None,
) -> tuple[str, str]:
    """从最终 PDF 文本判定证据状态。

    通用解析器不会用“全文出现检索词”证明命中，因为搜索框、标题和面包屑都会
    回显检索词。只有站点配置提供了可复核的结果区正则时，才在结果区内判断
    “真命中/疑拆词”；否则非零结果统一交人工核阅。
    """
    flat = compact(text)
    if len(flat) < 20:
        return "⚠无效证据页", "PDF 无可用文本或正文过短"

    # **判定前先剥掉打印页眉页脚。** 页眉印的是 document.title，单页应用常年停在
    # 「加载中」；页脚印的是完整网址。这两行是打印器加的，不是页面内容 ——
    # 拿它们去判「页面未渲染完」会把一份正文完好的空结果件判成半成品
    # （内蒙古公安厅：正文写着「相关结果约 0 个」，页眉写着「加载中」）。
    # 页眉页脚里的异常另有两道关拦：grab() 的标题粗筛、archive() 对实时正文的判定。
    reason = bad_page_reason(_strip_print_furniture(text))
    if reason:
        return "⚠无效证据页", reason

    counts = _counts(flat)
    positive_counts = [n for n in counts if n > 0]
    zero_count = any(n == 0 for n in counts)
    empty_hit = next((p for p in EMPTY_PHRASES if compact(p) in flat), None)

    # 同一页既写非零计数又写“暂无”时，后者常来自侧栏/空模块；不能直接判无记录。
    if positive_counts:
        region = _extract_result_region(text, result_region_pattern)
        if region is not None and keyword:
            core = compact(keyword)
            if core and core not in compact(region):
                return "★疑模糊匹配需甄别", (
                    f"结果计数为非零（{max(positive_counts)}），但配置的结果区不含完整检索词"
                )
        return "★有结果需核阅", f"结果计数为非零（{max(positive_counts)}）"

    if zero_count:
        return "无记录", "结果计数明确为 0"
    if empty_hit:
        return "无记录", f"页面明确显示“{empty_hit}”"

    # 没有可复核的零结果或非零计数，不能靠关键词回显猜结论。
    return "★疑模糊匹配需甄别", "结果页未出现可复核计数或明确空结果，需人工确认"


def result_family(result: str) -> str:
    if result == "无记录" or "无记录" in result:
        return "empty"
    if "无效证据页" in result:
        return "invalid"
    return "nonempty"
