# 站点类型手册

接手一个新站点时，先花两分钟判断它属于哪一类，比直接写脚本快得多。

## 目录

- [判别流程](#判别流程)
- [A. URL 可拼参数型](#a-url-可拼参数型)
- [B. JS 触发型](#b-js-触发型)
- [C. SPA 异步渲染型](#c-spa-异步渲染型)
- [D. 新标签结果型](#d-新标签结果型)
- [E. 验证码 / 滑块型](#e-验证码--滑块型)
- [F. WAF 拦无头型](#f-waf-拦无头型)
- [G. 政务统一检索平台](#g-政务统一检索平台)
- [H. 商业查询平台](#h-商业查询平台)
- [I. 多入口同域名型](#i-多入口同域名型)
- [J. 站内页码与 URL 脱钩型](#j-站内页码与-url-脱钩型)

## 判别流程

1. **先试最省事的**：手工在浏览器里搜一次，看地址栏有没有把关键词带进 URL。有 → A 型，直接拼参数，最稳。
2. URL 不变或没有关键词 → 看搜索按钮的 HTML。有 `onclick=` → B 型。
3. 点了按钮页面没跳转但内容变了 → C 型，要等渲染。
4. 点了按钮开新窗口 → D 型，要捕获 popup。
5. 弹验证码/滑块 → E 型，人机配合。
6. 脚本被拒但手工浏览器正常 → F 型，改用有头真实浏览器。

## A. URL 可拼参数型

**特征**：`https://site/search?q=关键词` 这类，改 URL 就能直接出结果页。

**处理**：最简单也最可靠。直接 goto，等渲染，判定，打印。

```python
url = tmpl.format(q=urllib.parse.quote(keyword))
pg.goto(url, wait_until='networkidle', timeout=60000)
pg.wait_for_timeout(6000)
```

**注意**：`wait_until` 选 `networkidle` 还是 `domcontentloaded` 因站而异。政务站资源多、有的 networkidle 永远等不到，超时就改 domcontentloaded + 固定等待。

## B. JS 触发型

**特征**：搜索按钮长这样 —— `<a class="s-btn" onclick="submitsearch()">搜索</a>`，输入框不在 `<form>` 里。

**关键**：**回车无效**。填完搜索框按 Enter，页面纹丝不动，但搜索框里留着你的词——这时候如果只检查"关键词是否出现在页面上"，会把首页当结果页存下来（判定纪律第 1 条的经典事故）。

**处理**：必须点按钮。按钮选择器优先级：

```python
BTNS = ('a.s-btn', 'button[type=submit]', '.search-btn', '.searchBtn',
        '[onclick*=search]', '[onclick*=submit]',
        'button:has-text("搜索")', 'a:has-text("搜索")')
```

点不到时的兜底：直接执行页面函数 `pg.evaluate('submitsearch()')`，但要先确认函数名。

## C. SPA 异步渲染型

**特征**：单页应用，点搜索后 URL 可能不变，内容通过 AJAX 填充。登录弹窗、结果区都是动态渲染的。

**两个坑**：
1. **`<title>` 不变**：登录墙弹出来了但标题还是原来的，标题黑名单查不出来 → 必须查正文
2. **判定时机**：读 DOM 时还没渲染完，打印时渲染完了 → 结论与证据不一致

**处理**：轮询等结果标记出现，再打印，再从 PDF 判定。

```python
for _ in range(8):
    pg.wait_for_timeout(1000)
    body = pg.evaluate('()=>document.body?document.body.innerText:""')
    if any(m in body for m in RESULT_MARK):
        break
```

## D. 新标签结果型

**特征**：点搜索按钮后，结果在新窗口/新标签打开。政务站和商业查询平台很常见。

**后果**：不捕获的话，脚本还在对着旧页面打印，存下来的是搜索前的首页。

**处理**：点击前保存已有页面集合，点击后同时轮询新标签和原页面。不要只用一次
`expect_page(timeout=...)`：同页跳转会白等，新标签又可能先是 `about:blank`。

```python
before = set(ctx.pages)
btn.click()
for _ in range(40):
    fresh = [p for p in ctx.pages if p not in before and not p.is_closed()]
    if fresh and fresh[-1].url != "about:blank":
        dp = fresh[-1]
        break
    if result_signal_on(pg):        # 同页跳转兜底
        dp = pg
        break
    pg.wait_for_timeout(300)
```

打印前再验证 `dp` 的 URL、标题、结果区和检索条件。页面很多时，绝不能用
“最后一个标签”或“URL 看起来最像”的规则事后猜归属；这会把上一项结果打印到下一项。

## E. 验证码 / 滑块型

**特征**：极验滑块、图形验证码、短信验证、实名认证。

**原则**：**不要试图绕过**。既不可靠（识别率低、封号风险），也不合适（有的属于规避技术措施）。

**正确分工**：
1. 脚本打开站点、**填好检索词**（这一步能省人不少事）
2. 停下来，提示人过验证
3. 人过完验证、确认结果出来，回车
4. 脚本判定、打印、归档，进入下一项

```python
pg.fill(sel, keyword)
pg.bring_to_front()
print(f'[{i}/{n}] {code}  检索词：{keyword}')
if cid: print(f'          证件号：{cid}')     # 需要填证件号的站点，一并显示方便复制
print('   → 请过验证码并点检索，出结果后回车（s跳过 / q退出）：', end='', flush=True)
ans = input().strip().lower()
```

**会话级验证码**：很多站过一次后同会话内不再弹。这种只需在第一项停一次，后面全自动——脚本里用一个 `captcha_done` 标志控制。

**终端要显示完整信息**：人要手工填的字段（名称、证件号）都打印出来，别让人回去翻表。

## F. WAF 拦无头型

**特征**：脚本报 412 / 403 / 连接被拒，但手工浏览器访问完全正常。典型：`*.chinatax.gov.cn` 系。

**判别**：同一 URL，手工能开、脚本不能开 → WAF 在拦自动化特征。

**处理顺序**：
1. 用**有头真实 Chrome**（CDP 连常驻浏览器），别用无头
2. 加反检测：`--disable-blink-features=AutomationControlled` + `navigator.webdriver` 覆盖
3. 设真实 UA、viewport、locale、timezone
4. 还不行 → 转人工，别硬刚

```python
ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
```

## G. 政务统一检索平台

不少政府网站用同一套搜索后端，摸清一个就能套用一批：

| 平台形态 | URL 模式 |
|---|---|
| 智能云搜索 | `https://<域名>/so/s?qt=<关键词>` |
| so-gov 平台 | `https://bw.so-gov.cn/s?siteCode=<网站标识码>&tab=all&qt=<关键词>` |
| TRS WAS5 | `https://<域名>/was5/web/search?channelid=<频道号>&sw=<关键词>` |
| 部委自建 | `https://<域名>/search/index.html?q=<关键词>`（参数名各异） |

**先试 `/so/s?qt=`**，通用性最高。返回 404 就说明不是这套，再看具体站点。

### 入口必须从首页表单读出来，不能猜

**这是本类站点最容易出假证据的地方。** 一轮实跑里，四个部委站因为入口选错，
产出了看起来完全正常、实则无效的留痕件：

| 站点 | 错误做法与后果 | 实测正确入口（2026-08-31） |
|---|---|---|
| 应急管理部 | 直接 GET `/was5/web/search` 得到空的 `TRSWAS5.1` 页 | `?channelid=201309&sw=<词>&perpage=15&page=1` |
| 人社部 | 首页填框点通用按钮，存下的页面没等到结果渲染 | `/hsearch/?searchword=<词>` |
| 国家税务总局 | **参数里留着上次的 `searchWord=发票查询`**，查出 29 条无关结果 | `/search5/html/searchResult.html?siteCode=bm29000002&searchWord=<词>&left_right_index=0&searchSource=0` |
| 国家药品监督管理局 | `searchResult.html?searchword=` 被静默重定向回首页，存成空白页 | `/so/s?qt=<词>` |
| 自然资源部 | 带 `?searchword=` 打开就打印，**没点搜索按钮**，结果区从未渲染 | 同 URL + 点击「搜索」，等出现 `暂无数据` |

正确的探测顺序：

1. 打开**首页**，读 `<form>` 的 `action` 和 `method`，以及全部 hidden input 的
   `name`——参数名和频道号都在这里，比试参数快得多也可靠得多；
2. 用读到的参数拼 GET 试一次（TRS WAS5 表单虽是 POST，实测 GET 等效）；
3. 拿一个**确定应当无结果**的检索词跑，确认能拿到明确的空结果措辞；
4. 把这次实测看到的结果区文本写进站点配置作为回归基准（见下）。

### 站点配置里存 `exp` 回归基准

每个站点配置记录**实测时页面上实际出现的结果区文本**，脚本轮询到它才允许落盘：

```python
"C24": {"name": "应急管理部", "type": "url",
        "tmpl": "https://www.mem.gov.cn/was5/web/search?channelid=201309&sw={q}&perpage=15&page=1",
        "exp": ("全部网页结果共",)},          # 实测原文，不是猜的措辞
```

好处有两个：等不到就拒绝落盘、记入待人工，不会把没渲染完的页面存成证据；下次跑
突然等不到了，说明站点改版，要停下来重新探——而不是继续产出可疑件。

`exp` 必须抄实测原文。凭印象写"共X条"而实际页面是"全部网页结果共X条"，
属于 `pitfalls.md` 里那类正则漏字事故，会让整站长期静默失败。

**共性坑：拆词模糊匹配**。这类平台会把长公司名拆词匹配，返回一堆无关结果还显示"为您找到约N条"。必须按判定纪律第 5 条做三态分类，不能见到"找到N条"就判有记录。

## H. 商业查询平台

企查查、启信宝、天眼查这类。

**特征**：需登录、有反爬、数据结构化程度高、通常提供报告导出。

企业信用报告不是网页截图。需要报告时，先读
[`commercial-report-platforms.md`](commercial-report-platforms.md)，把流程拆成“报告产品列表 →
额度/预订单检查 → 创建订单 → 报告中心 → 下载”。只有创建订单会消耗免费次数或产生费用，
不能把这些步骤揉成一个会盲目重复点击的脚本。

**自动化优先级**：

1. 已登录浏览器通过 CDP 提供共享登录态的 request context 时，走已验证接口；
2. 普通浏览器扩展会话没有共享 request context 时，用固定 URL 和稳定定位器一次性回放，
   只返回小型状态摘要，不反复截图或输出整页 DOM；
3. 选择器漂移、验证码或关键确认信息无法读取时转人工。

DOM 控制不等于 Computer Use。点击本身几乎不占模型上下文；高 token 消耗来自反复截图、
整页 DOM 快照和逐步推理。已知路径应写成确定性动作，只在断言失败时取局部 DOM 诊断。

**自然人查询的正确姿势**：**从公司页的主要人员列表点进去**，不要搜姓名。

- 搜"李晓军"→ 5000+ 同名，无法认定
- 从公司页 → 主要人员 → 点人名 → `/people/info/<hash>` → 保证是本人，零重名

**报告导出**：这类平台的正式报告（企业信用报告/公共信用信息报告）比页面截图更有证明力，能导就导。导出按钮常是不起眼的元素，注意找准：

```python
btn = pg.query_selector('strong.download')   # 实际选择器要现场探
```

导出触发的是浏览器原生下载，用**下载目录差分**捕获最稳（比 Playwright 的 expect_download 兼容性好，尤其在 CDP 连真实 Chrome 时）：

```python
before = set(os.listdir(DL_DIR))
btn.click()
for _ in range(30):
    time.sleep(1)
    new = [f for f in set(os.listdir(DL_DIR)) - before if not f.endswith('.crdownload')]
    if new: break
```

## I. 多入口同域名型

**特征**：同一域名下有综合查询、状态查询、公告查询等多个入口，页面外观接近，
甚至结果页标题相似。中国商标网、部分知识产权官网属于这一型。

**风险**：脚本复用旧标签或仅按域名找 page，会把综合查询结果保存成状态查询，
文件名正确但正文属于另一渠道。

**处理**：

- 每种查询绑定精确入口 URL、页面标题/栏目标记和必需字段；
- 每个任务保存自己创建的 `Page` 引用，不在全局标签中猜；
- 打印前核对 URL、激活栏目、检索条件及结果区；
- 多字段检索要逐字段确认实际 value，缺一个就不点击查询；
- 结果入口或选择器改版时转人工，不让通用 submit 选择器遍历整页。

知识产权官网的具体模式见 `ip-official-sites.md`。

## J. 站内页码与 URL 脱钩型

**特征**：点击下一页后地址栏不变；当前页码和结果列表由前端状态维护；某页加载失败后继续点击，可能破坏后续分页状态；首页总量非零，但中间页可能暂时返回空列表或错误总量。

**风险**：把站内第 N 页误当成 PDF 第 N 页；因 URL 相同而重复保存同一页；把中间空页误归类为“无记录”；在已经损坏的分页状态上继续取证。

**处理**：

1. 首页记录基准总量、首条结果标识和页尺寸；
2. 翻页后同时验证当前页标识、列表非空性和首条结果变化；
3. 站内页码写入自定义页眉和台账，PDF 物理页码写入页脚；
4. “基准总量非零但当前页为空”标为 `pagination_anomaly`；
5. 原样保留空页，再从一次新的查询状态直接探测相邻后页；
6. 后页正常只能作为相邻验证，不能把空页改写为正常页；
7. 同一主体的多页材料全部验证后再原子归档。

| 首页总量 | 当前页列表 | 相邻后页 | 结论 |
|---:|---|---|---|
| 0 | 空 | 不需要 | 无结果候选，仍须通过无结果页验证 |
| >0 | 非空 | 不需要 | 正常结果页 |
| >0 | 空 | 非空 | 中间分页异常，保留空页与相邻验证页 |
| >0 | 空 | 空或不可达 | 分页、认证或风控异常，不能判定无记录 |

裁判文书网的具体选择器和实测边界见 `china-judgments-online.md`。

## 上交所（sse.com.cn）监管信息各模块：填框 + 触发

「监管措施 / 承诺履行情况 / 董监高及核心技术人员持股变动」三个模块 DOM 结构一致，
URL 不随关键词变化（站内异步查询）：

| 字段 | 选择器 | 填什么 |
|---|---|---|
| 证券代码或简称 | `#inputCode` | 公司全称 |
| 涉及对象 / 承诺主体名称 / 姓名 | `input.js_code3` | 自然人姓名 |

**触发方式实测**：点该输入框**同容器内**的 `span.search_btn` 有效，按 Enter 也有效；
**blur/change 无效**——只失焦不检索，页面停在默认全量列表上。

两个坑：

1. **不要用泛选择器找按钮**。`[class*=search-btn]` 会撞到页眉的全站搜索按钮，
   而循环里的 `break` 会跳过后备的 Enter。实测因此产出 19 份留痕，存的全是
   「退市海越、ST新华锦、珍宝岛、妙可蓝多」这类默认列表，与被查主体毫无关系。
2. **公司填「证券代码或简称」、自然人填「涉及对象」**。字段选错整批查空而看不出来。

因此这类模块必须加一道**结果表校验**：表里要么明确「暂无数据」，要么含被查主体名；
两者都不满足就是没触发检索，拒绝落盘。

```python
table = pg.evaluate("()=>[...document.querySelector('table').querySelectorAll('tr')]"
                    ".map(r=>[...r.cells].map(c=>c.innerText.trim()).join('\u0001'))")
body_rows = [r for r in table[1:] if r.strip()]
empty = any(w in "".join(table) for w in ("暂无数据", "无数据", "暂无记录"))
hit = compact(kw) in compact("".join(table))
if not empty and body_rows and not hit:
    return None, "结果表有行但不含主体名，且未显示「暂无数据」——疑填了框未触发检索"
```

另外「上交所全站」（`/home/search/index.shtml?webswd=`）是可寻址的，且默认
**模糊搜索**；页脚计数「共5000条」是显示上限而非真实计数，不能拿它当命中量。

## 结果另开一页的政务站（2026-09-01 实测）

这四个站点搜索按钮都是 `target=_blank`。判定与打印必须切到新页，否则原页永远没变化。

| 站点 | 检索框 | 触发 | 新页 URL | 引擎特性 |
|---|---|---|---|---|
| `fgw.beijing.gov.cn` 北京发改委 | `#page_search, input[name=qt]` | Enter | `/so/s?tab=all&siteCode=1100000011`（**不带检索词**） | **分词型**：全名 2227 条、只搜「数字科技有限公司」6117 条，主体名在结果区 0 次 |
| `hrss.sz.gov.cn` 深圳人社 | `#search1, input[name=keywords]` | Enter | `/fzlm/cjhzthjs/?keywords=<主体>` | 无计数行，条目直接列；结果由 `search.gd.gov.cn` jsonp **第 14 秒**才回填 |
| `gaj.sh.gov.cn` 上海公安 | `#searchChar, input[name=searchChar]` | Enter | `/shga/vQsAllSearch`（词在 **POST body** `searchChar=`） | **短语精确型**：以「上海」试检 9999 条证明引擎可用，故 0 条即真实无记录；措辞是「找到记录约 0 条」 |
| `www.saihan.gov.cn` 赛罕区 | `#qt, input[name=qt]` | Enter | `/so/s?qt=`（**qt 被 JS 抹空**） | 结果页不回显检索词 → 走两页一组原子取证；框宽只容 13 字，须放宽后再印 |
| `www.zhengzhou.gov.cn` 郑州门户 | `input[name=q]` | Enter | 原页跳 `search.jspx?q=<主体>` | 唯一不另开页的；措辞「相关结果 N 条」 |

判「无记录」前先做**引擎可用性反证**：拿一个必然有结果的常见词（如所在城市名）试检。
上海公安三个对照词全 0 条，看着像空壳站，用「上海」一试 9999 条，才敢把 0 条写成无记录。

## 市政府统一检索的范围参数（2026-09-01 实测）

部门无独立网站时用这个，**范围标签会印在留痕件上，可复核**。详见 pitfalls 同名条目。

| 城市 | URL 模板 | 范围参数 | 页面上的范围标签 |
|---|---|---|---|
| 青岛 | `www.qingdao.gov.cn/unionsearch/?searchWord={kw}&sitetype=all&areaId=N` | `areaId` 可枚举 | 页头「市公安局」等 |
| 上海 | `search.sh.gov.cn/search?siteId=<部门域名>&text={kw}` | `siteId=scjgj.sh.gov.cn` | 页头「市场监管局」 |
| 哈尔滨 | `www.harbin.gov.cn/so/xxgk/s?qt={kw}` | 无 URL 参数，靠结果页**发文机构分面** | 分面列出命中所属部门 |

青岛 areaId 实测表：14 发改委｜17 公安局｜20 人社局｜22 自然资源和规划局｜
23 生态环境局｜24 住建局｜35 应急局｜39 市场监管局｜52 公积金中心｜1 西海岸新区｜2 市南区。
枚举办法：`?searchWord=测&areaId=N` 打开后读正文第 2 行就是范围名。

## 其它本轮实测入口

| 站点 | 入口 | 备注 |
|---|---|---|
| `www.chinabx.gov.cn` 哈尔滨宾县 | 首页 `input.hd2-fr-d1` | 一次检索覆盖该区全部 12 个部门行 |
| `neimenggu.chinatax.gov.cn` | `/was5/search?channelid=286494&searchword={kw}` | 表单禁用回车，必须走 GET |
| `shzfgjj.cn` 上海公积金 | `/search/search.html?wd={kw}` | 首页框藏在隐藏手机浮层，走直达 |
| `sh.119.gov.cn` 上海消防总队 | 首页 `input[placeholder*=输入]` | 结果页措辞「"X"全部相关结果约0条／暂无数据」 |
| `*.huhhot.gov.cn` 呼和浩特各局 | 首页框 →「智能云搜索」`/so/s?qt=` | qt 被 JS 抹空、不回显 → 两页原子取证；市门户本身已停用 |


---

# 信用中国省级子站（2026-09-02 实测 10 省）

**先看清楚你在检索哪个数据集。** 这些子站首页那个框，多数是站内文章检索，
搜的是「信用动态／政策法规」栏目，不是企业信用记录。框的上方通常有一排切换：

```
信用信息 ｜ 统一社会信用代码 ｜ 站内文章
         ↑ 查企业信用记录走这个        ↑ 默认常常停在这个
```

搜出「共 0 条」而口径是「站内文章」，写进笔录就是把没查写成查了没有。

| 省 | 站点 | 查企业信用的入口 | 拦路的东西 |
|---|---|---|---|
| 安徽 | credit.ah.gov.cn | `#search_input`，切「信用信息」 | 无。结果页出「查询结果一共有 N 条记录」+ 统一社会信用代码，**可当标准样张** |
| 河南 | credit.henan.gov.cn | `#contentxy22` | 跳「全国一体化信用查询」页，结果要等加载 |
| 内蒙古 | fgw.nmg.gov.cn/xynmg/ | `#search_input` | 结果区慢，会停在「正在搜索，请稍后...」，**别急着打印** |
| 上海 | credit.fgw.sh.gov.cn | `#textfield01` | 敲回车不动，要点「查 询」按钮 |
| 新疆 | www.creditxj.gov.cn | `#search_input`，切「法人及其他组织」 | **验证码** |
| 广东 | credit.gd.gov.cn | `#key`，点「立即查询」 | **拖动滑块旋转图片** |
| 山东 | credit.shandong.gov.cn | 首页查询要过滑块；另有栏目页 `827.location.block.dhtml`（失信惩戒对象）、`834...`（行政许可与行政处罚公示） | **滑块** |
| 北京 | creditbj.jxj.beijing.gov.cn | 首页查询框 | 框无 placeholder，要在页面上确认不是站内文章 |
| 湖北 | credit.hubei.gov.cn | 首页框是文章检索；走栏目页 `gkgs/qgxywzythgs/xzcf/`（行政处罚）、`.../sxcjdx/`（严重失信主体名单） | — |
| 黑龙江 | credit.hlj.gov.cn | 首页框只能搜文章（`was5/web/search`，分类仅「信用动态/政策法规」） | 企业信用入口需在页面上另找 |

## 结果怎么读：认那一行分类计数，别在正文里搜关键词

详情页普遍有一行「计数 + 分类名」，**数字可能在分类名前面，也可能在后面**：

```
0 0 0 0 0 0 0 0   行政管理 诚实守信 严重失信主体名单 经营异常 信用承诺 信用评价 司法判决 其他   ← 山东、新疆
行政管理 诚实守信 严重失信主体名单 经营异常 …   1 0 0 0 5 0 0 0                              ← 河南
基础信息 行政许可 行政处罚 守信激励 失信惩戒 重点关注 资质/资格 风险提示 其他                  ← 上海（另一套分类名）
```

只往一个方向找会读错：河南那份往前找，读到的 3 和 35 来自上面那段说明
（「3.因篇幅有限…前 100 条」），实际是 1 和 5。**两个方向都试，取正好凑够个数的那边。**

在正文里搜「行政处罚」更不行 —— 每份报告的固定说明里都有
「如需对相关行政处罚信息进行信用修复，可按照…」。实测四份干净的报告
全被这句话判成「有结果需核阅」。要么按计数判，要么把
`信用修复｜异议申诉｜流程指引｜重要提示｜如需对｜可按照｜篇幅有限` 这些上下文排掉。

**「行政管理」这一栏既装行政许可也装行政处罚**，计数非零不等于有处罚，
要看它下面列的是「行政许可（新标准）第 1 条 准予设立/开业登记通知书」
还是处罚决定书。实测四份非零件全是开办登记。

---

# 内蒙古自治区厅局站群（nmg_dept_search）

自治区统一的部门检索组件，各厅局共用，只有 `siteid` 不同：

```
https://<部门域名>/nmg_dept_search/#/Door?keywords={kw}&siteTag=1&siteid=<N>
```

实测：住建厅 54、应急厅 71、药监局 101。检索词在**井号片段**里，
前端读不读要逐站证伪（见 pitfalls）。另外：

- 首页检索按钮是 `<div class="s-btn" onclick="btnonclick()">`，不是 button
- 公安厅首页的框是 `#wxf_search_new_input`，藏在折叠层里，
  **填了词打印出来还是 placeholder**，两页取证的第 1 页自证不了 ——
  这个站只能走片段 URL（已证伪前端确实读取）
- 片段型 SPA 要给足初始等待（实测 9 秒），`settle()` 救不了：
  它看正文长度是否稳定，而「加载中」那一屏本来就稳定

---

# 广东省统一检索（search.gd.gov.cn）

`https://search.gd.gov.cn/search/all/{siteId}?keywords={kw}`，
`siteId` 就是范围，**结果页顶部会把部门名印出来**，范围可在留痕件上复核。
这与「不带范围参数的门户全站检索」性质完全不同，后者是顶替。

实测 siteId：

| siteId | 范围（页首印的） |
|---|---|
| 122 | 广东省应急管理厅 |
| 147 | 广东省消防救援总队 |
| 206 | 广东省药品监督管理局 |
| 233 | 广东省住房和城乡建设厅 |
| 755014 / 755016 / 755028 | 深圳市生态环境局 / 规划和自然资源局 / 应急管理局 |
| 755039 | 深圳市福田区人民政府 |
| 769001 / 769035 | 东莞市人民政府 / 东莞市人力资源和社会保障局 |

省高院另有自己的检索域名 `search.gdcourts.gov.cn/?keywords={kw}`。

---

# 找省级部门站点：别猜子域名

`yjj` 在北京、上海、河南是**药品监督管理局**，在别处可能是**应急管理局**；
`hljfy.gov.cn` 打开是**抚远市人民政府**。只看 HTTP 200 不看页面标题，
就会把张三收成李四。

顺序：① 各省门户的站群导航 → ② 搜索引擎 → ③ **无论哪条路来的都要开页核标题**。

实测通用的域名规律（仍需逐个核标题）：

| 部门 | 常见形态 |
|---|---|
| 省级税务局 | `<省拼音>.chinatax.gov.cn`，返回 412 是云盾挑战页，标题已正确，等它跳转 |
| 消防救援总队 | `<两字母省码>.119.gov.cn` —— **中国消防的省码，不是拼音首字母**：黑龙江是 `hl` 不是 `hlj`，河南是 `ha`。黑龙江/内蒙古/安徽/湖北的总队站已无内容，消防信息在省应急管理厅发布 |
| 信用中国子站 | `credit.<省>.gov.cn` 居多，也有 `creditbj.jxj.beijing.gov.cn`、`credit.fgw.sh.gov.cn`、`fgw.nmg.gov.cn/xynmg/`、`www.creditxj.gov.cn` |
| 高级人民法院 | 无统一规律：`www.hljcourt.gov.cn`、`xjfy.xjcourt.gov.cn`、`gy.nmgfy.gov.cn`、`ahfy.ahcourt.gov.cn`、`www.hbfy.gov.cn`、`www.hshfy.sh.cn` |
| 住房公积金 | **实行设区市统筹，多数省份没有省级机构**，要落到设区市：`bh.hrbgjj.org.cn`、`dggjj.dg.gov.cn`、`gjj.wuhan.gov.cn` |

**`https` 打不开不代表站点没了，`http` 常常通**（`xj.119.gov.cn` 只有 http 通）。
两个协议都要试。
