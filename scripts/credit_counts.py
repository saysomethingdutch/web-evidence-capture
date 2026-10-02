#!/usr/bin/env python3
"""读「信用中国」省级子站详情页那一行分类计数，判有没有风险记录。

为什么不能在正文里搜「行政处罚」：**每份报告的固定说明里都有这四个字**——
「如需对相关行政处罚信息进行信用修复，可按照…」。实测四份干净的报告
全被这句套话判成「有结果需核阅」。

详情页普遍有一行「计数 + 分类名」，**数字可能在分类名前面，也可能在后面**：

    0 0 0 0 0 0 0 0   行政管理 诚实守信 严重失信主体名单 经营异常 …     ← 山东、新疆
    行政管理 诚实守信 严重失信主体名单 经营异常 …   1 0 0 0 5 0 0 0     ← 河南
    基础信息 行政许可 行政处罚 守信激励 失信惩戒 重点关注 …            ← 上海（另一套分类名）

只往一个方向找会读错：河南那份往前找，读到的 3 和 35 来自上面那段说明
（「3.因篇幅有限…前 100 条」），实际是 1 和 5。两个方向都试，取正好凑够个数的那边。

**「行政管理」这一栏既装行政许可也装行政处罚**，计数非零不等于有处罚，
要看它下面列的是「行政许可（新标准）第 1 条 准予设立/开业登记通知书」还是处罚决定书。
实测四份非零件全是开办登记。

**没有分类计数行的，一律判「疑模糊需甄别」，不判「无记录」。** 这是故意的：
信用中国子站的**站内文章检索**页也会给出「检索到 相关信息共 0 条」，但它搜的是
「信用动态／政策法规」栏目，不是企业信用记录（见 SKILL.md 判定纪律第 11 条）。
拿那个 0 条判「无记录」，等于把没查写成查了没有。实测 27 份里有 13 份是这种页面，
本脚本一律拒绝给它们「无记录」。

用法：
  python3 credit_counts.py 留痕件.pdf [更多.pdf ...]
  from credit_counts import counts, classify
"""
from __future__ import annotations
import re
import subprocess
import sys

# 两套分类名：多数子站用「行政管理/诚实守信/…」，上海用「基础信息/行政许可/行政处罚/…」
CATS = ("行政管理", "诚实守信", "严重失信主体名单", "严重失信", "经营异常",
        "信用承诺", "信用评价", "司法判决", "其他",
        "基础信息", "行政许可", "行政处罚", "守信激励", "失信惩戒",
        "重点关注", "资质/资格", "风险提示")
RISKY = ("严重失信主体名单", "严重失信", "经营异常", "司法判决",
         "行政处罚", "失信惩戒", "重点关注", "风险提示")
# 「行政管理」栏里列的到底是许可还是处罚，看这两组词。
# 注意报告正文写的是「行政许可 第1条」而不是「行政许可 1」，`\s*\d` 匹配不上，
# 要允许中间夹一个「第」。许可类别、许可证书名称这些字段名同样只出现在许可条目里。
LICENSE = re.compile(r"行政许可(（新标准）)?\s*第?\s*\d|准予(设立|开业|变更)|"
                     r"登记通知书|注册登记许可|许可类别|许可证书名称|许可决定日期|"
                     r"商事(变更|设立)登记")
PUNISH = re.compile(r"行政处罚\s*第?\s*\d|处罚决定书|违法行为类型|罚款金额|没收违法所得|"
                    r"处罚依据|处罚机关")
# 页面自己说「查询期内无相关记录」这类，比任何计数都直接
EMPTY = re.compile(r"查询期内无相关记录|无相关记录|暂无相关记录|未查询到相关|"
                   r"暂无数据|没有找到|未找到相关|无符合条件")
# 这些上下文里出现「行政处罚」只是页面固定说明，不是这家企业有处罚
BOILER = re.compile(r"信用修复|异议申诉|流程指引|操作指引|重要提示|如需对|"
                    r"可按照|请查看|申请通过后|仅依现有数据|篇幅有限")
_NUM = re.compile(r"(?<![\d.%])\d{1,5}(?![\d.%])")


def counts(text: str) -> dict[str, int] | None:
    """→ {分类: 条数}，页面没有这种计数行时返回 None。"""
    flat = re.sub(r"[ \t]+", " ", text)
    pat = r"(?:(?:" + "|".join(map(re.escape, CATS)) + r")\s+){4,}"
    for m in re.finditer(pat, flat):
        labels = [x for x in re.split(r"\s+", m.group(0).strip()) if x]
        tail = _NUM.findall(flat[m.end():m.end() + 40 + 6 * len(labels)])
        if len(tail) >= len(labels):
            return dict(zip(labels, (int(x) for x in tail[:len(labels)])))
        head = _NUM.findall(flat[max(0, m.start() - 40 - 6 * len(labels)):m.start()])
        if len(head) >= len(labels):
            return dict(zip(labels, (int(x) for x in head[-len(labels):])))
    # 还有一种排版：分类名和条数**贴在一起成对出现**，分类名后面常缀「信息」——
    # 广东、北京的《公共信用信息报告》长这样：
    #   「其他失信主体名单信息 0条　经营异常信息 0条　资质资格信息 0条」
    # 上面按「连排的分类名」定位的路子在这里找不到锚点，得单独认。
    pairs = re.findall(r"(" + "|".join(map(re.escape, CATS)) + r")(?:信息)?\s*(\d{1,5})\s*条",
                       flat)
    if len(pairs) >= 3:
        out = {}
        for k, n in pairs:
            out[k] = max(out.get(k, 0), int(n))
        return out
    return None


def classify(text: str) -> tuple[str, str]:
    """→ (结论, 理由)。有计数行就以计数为准，没有才退回关键词（并排掉套话）。"""
    # **顺序要紧：先看分类计数，再看空结果措辞。**
    # 反过来会出事 —— 一份报告完全可能一边某个风险栏非零、
    # 另一个栏位写着「查询期内无相关记录」，让空结果措辞先命中就把整份判成无记录了。
    # 计数是结构化的、覆盖全部分类；措辞只说明某一处为空。
    c = counts(text)
    if c is None:
        m = EMPTY.search(text)
        if m:
            return "无记录", f"页面明确显示「{m.group(0)}」"
    if c is not None:
        shown = "、".join(f"{k} {v} 条" for k, v in c.items())
        hot = {k: v for k, v in c.items() if k in RISKY and v}
        if hot:
            return "★有结果需核阅", (f"信用信息分类计数：{shown}；其中 "
                                + "、".join(f"{k} {v} 条" for k, v in hot.items())
                                + " 非零，需人工核阅")
        adm = c.get("行政管理", 0)
        if adm:
            lic, pun = bool(LICENSE.search(text)), bool(PUNISH.search(text))
            if lic and not pun:
                return "无记录", (f"信用信息分类计数：{shown}；风险类均为 0；"
                                f"「行政管理」{adm} 条经查为行政许可（设立/开业登记），非行政处罚")
            return "★有结果需核阅", (f"信用信息分类计数：{shown}；风险类均为 0，"
                                 f"但「行政管理」{adm} 条未能判明是许可还是处罚，需人工核阅")
        return "无记录", f"信用信息分类计数全部为 0：{shown}"

    hits = [m for m in re.finditer(r"行政处罚|失信被执行|严重违法|经营异常", text)
            if not BOILER.search(text[max(0, m.start() - 60):m.end() + 40])]
    if hits:
        return "★有结果需核阅", f"结果区出现「{hits[0].group(0)}」且非页面固定说明，需人工核阅"
    return "★疑模糊匹配需甄别", "页面无分类计数行，也无可复核的空结果措辞，需人工确认"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    for p in sys.argv[1:]:
        t = subprocess.run(["pdftotext", "-layout", p, "-"],
                           capture_output=True, text=True).stdout
        lab, why = classify(t)
        print(f"\n■ {p}\n   【{lab}】{why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
