# 商业查询平台正式报告

本文件处理企查查、启信宝的正式企业信用报告。它们与“把结果页打印成证据 PDF”是两类材料：
前者是平台生成的报告文件，后者证明检索时间、检索字段和当时页面状态。项目口径需要时，两者分别归档，不能互相冒充。

以下接口和页面行为于 **2026-08-31** 在用户本人已登录、正常有权访问的浏览器中验证。
前端接口可能改版；请求字段或断言不匹配时停止并重新探测，不能猜参数或循环试单。

## 一、共同纪律

### 1. 不用 Computer Use 的低 token 路径

优先用站点自己的报告接口和报告中心文件地址。若当前浏览器是带 CDP 的 Playwright 会话，
可用同一 browser context 的 request context 共享登录态，直接调用接口；不要导出 Cookie，
不要绕过验证码、登录或站点权限。

若当前是用户普通 Chrome 的浏览器扩展会话，而扩展没有提供共享 Cookie 的 request context，
则无法可靠地从本地脚本直接复用登录态做零 DOM 请求。此时仍不需要 Computer Use：
用固定 URL、稳定定位器和一次性动作脚本回放，只输出下列小型摘要，不输出整页 DOM 或截图：

```json
{
  "site": "qcc|qixin",
  "entity_name": "...",
  "entity_id": "keyNo|eid",
  "report_type": "企业信用报告-专业版",
  "existing_completed_order": true,
  "needs_create": false,
  "download_url_present": true,
  "state": "ready|generating|manual_required"
}
```

DOM 操作的 token 成本主要来自反复截图、全页快照和逐步观察，不来自 `locator.click()` 本身。
只有断言失败时才读取弹窗或目标行的局部文本。不得用页面内 `fetch()` 发送有副作用的请求，
也不得为了“方便”复制浏览器 Cookie 或本地存储。

### 2. 生成与下载必须分开

报告流程至少分为五步：

1. 按统一社会信用代码或商业登记号确认唯一主体；
2. 读取可用报告产品和额度；
3. 查询报告中心是否已有同主体、同类型、可下载的有效报告；
4. 仅在不存在可复用报告且用户明确授权时创建订单；
5. 等生成完成后从报告中心下载。

第 4 步可能消耗免费次数或产生费用，是有副作用操作。自动批处理必须默认
`allow_create=false`；没有明确授权时只做探测和下载既有报告。请求超时后不得直接重发创建订单，
应先回报告中心按主体、报告类型和时间检查是否已经成功。

### 3. 下载后的核验

至少记录平台、主体名称、统一社会信用代码/商业登记号、平台主体 ID、报告类型、订单号、
生成状态、下载时间、文件大小和 SHA-256。PDF 还要检查魔数、页数、首页报告标题和主体名称。
登录页、错误页、空白文件、HTML 冒充 PDF 或主体不一致的文件一律隔离。

**核验取多少页**：主体名在封面就有，但**统一社会信用代码在「工商信息」章节，
实测落在第 5–8 页**（前几页是封面、免责声明和目录）。用 `pdftotext -l 3` 做核验会把
**全部**报告判成"信用代码不匹配"——批量归档时这表现为 0/20 全部拒绝，看起来像文件有问题，
其实是核验窗口太窄。取前 12 页比较稳妥：

```bash
pdftotext -layout -l 12 report.pdf -    # 覆盖封面+目录+工商信息
```

两项都要查：只查主体名会漏掉同名主体，只查信用代码会漏掉张冠李戴。核验用的文本要
先去掉空格和换行再匹配——报告排版会在字段值中间插入空白。

**报告与检索留痕分开归档**：平台报告放 `企查查报告/`，检索结果 PDF 放 `留痕/`，
各自一本台账。两者证明的东西不同（前者是平台出具的结论性材料，后者证明某时点
按某关键词检索到了什么），笔录里也是分列的两项，不能互相顶替。

## 二、企查查（QCC）

### 1. 主体和报告产品

公司详情页使用：

```text
https://www.qcc.com/firm/<keyNo>.html
```

不得只按公司名取第一条搜索结果；先核对详情页统一社会信用代码。报告弹窗的产品列表：

```http
POST /api/report/getReportListNew
Content-Type: application/json

{"scene":"company-detail","keyNo":"<keyNo>","countryCode":"<页面返回值或空字符串>"}
```

从返回值按显示名称选择 `企业信用报告-专业版`，不要把 `goodsId` 永久写死；产品编号可能改版。

### 2. 创建报告——有副作用

当前前端创建订单调用：

```http
POST /api/report/order
Content-Type: application/json

{
  "goodsId": "<产品列表返回的 goodsId>",
  "goods": [{"company_keyno":"<keyNo>","company_name":"<精确公司名>"}],
  "reportCode": "<产品列表返回的 reportCode>"
}
```

该请求可能消耗当日/当月报告额度。调用前必须再次查询报告中心并要求
`allow_create=true`。接口超时、返回不明或浏览器跳转异常时，先查订单，不能盲目重试。

### 3. 报告中心和下载——不新耗生成额度

报告中心页面：

```text
https://www.qcc.com/web/user/download?type=2
```

订单列表及生成进度：

```http
POST /api/order/getOrderList
{"type":"2","pageIndex":1,"searchKey":"<可选：主体名称>"}

POST /api/report/getOrderProgress
{"orderNoList":["<reportList[].orderNo>"]}

POST /api/report/getReportProcessLog
{"orderNos":["<orderCode>"]}
```

分页字段以当期响应为准。目标行必须同时匹配主体、报告类型和成功状态。PDF 地址位于
`reportList[].pdfUrl`；Word/Excel 分别看 `wordUrl`、`excelUrl`。当前网页下载 PDF 时会把：

```text
yj-files.oss-cn-hangzhou.aliyuncs.com
```

替换为：

```text
report.qichacha.com
```

并强制 HTTPS，随后添加：

```text
?response-content-type=application/force-download
```

若原 URL 已有查询参数，要用 `&` 而不是第二个 `?`。下载既有成功报告不会再次创建订单。
CDP 连接真实 Chrome 时，浏览器下载事件有时不回传；用下载目录差分和 `.crdownload` 完成标记判断更稳。

企查查 MCP 的企业信息工具适合主体校验和结构化字段查询，但截至上述验证日期，未提供生成、
查询订单或下载品牌报告的能力，不能用它替代报告链。

### 4. 普通浏览器扩展会话的确定性回放

没有共享 request context 时：打开精确详情页 → 断言名称和统一社会信用代码 → 点击“报告” →
在弹窗局部按完整文本选择“企业信用报告-专业版” → 先检查报告中心是否已有可下载项 →
仅在授权后点击一次“下载/生成” → 转报告中心按精确主体行下载。

不要每一步截图；每个阶段只返回 URL、主体断言、按钮唯一性、额度文本、订单状态和文件名。

## 三、启信宝（Qixin）

### 1. 主体和报告产品

公司详情页使用：

```text
https://www.qixin.com/company/<eid>
```

先核对统一社会信用代码。报告弹窗产品列表：

```http
POST /webc/products/getReportListNew
Content-Type: application/json

{"eid":"<eid>","pid":"","reportType":null}
```

省略无值字段也可，具体以当期页面请求为准。专业版应按返回项 `title` 精确匹配
`企业信用报告-专业版`；验证时其 `_id` 为 `562b47719ecb94ee5941790c`，但自动化仍应以标题和
当期返回值为准，不盲目写死。

报告弹窗的“下载报告”首先进入订单确认页，并不会仅因打开链接就完成生成：

```text
/pay/order/report?eid=<eid>&reportId=<产品 _id>
```

### 2. 预订单和创建报告

订单确认页读取：

```http
POST /webc/products/getPreOrderInfoByProductId
{"eid":"<eid>","productId":"<reportId>"}
```

响应决定格式、免费次数、抵扣和实付金额。真正提交订单的是：

```http
POST /webc/userorder/createUserOrder
```

其请求体由预订单结果和页面选择共同生成，含 `eid`、`ename`、`productId`、`format`、
`paidType`、邮箱及可能的抵扣字段。不要手工猜完整请求体；先读取预订单并断言主体、报告类型、
格式、剩余额度和应付金额。只有用户明确授权且金额符合授权范围时才提交。该请求可能消耗额度，
超时后先查报告中心，不能直接重试。

### 3. 报告中心和既有文件下载

报告中心：

```text
https://www.qixin.com/app-center/order?type=report
```

列表请求：

```http
POST /v4/internal/userorder/getOrderCenterList
Content-Type: application/json

{
  "start": 0,
  "hit": 10,
  "orderType": "DATA_REPORT",
  "productType": ["professionalCreditReport"],
  "searchKeyword": "<可选：主体名或订单号>"
}
```

“全部”筛选时可省略 `productType`；团队报告会增加 `isTeam:1`，不得混入个人报告。目标订单须为
`生成成功`，并精确匹配主体名、报告类型和订单号。文件位于
`items[].orderProducts.items[].fileUrl`。

当前前端对 `productType` 为 `1` 或 `3` 的文件使用同域下载代理：

```text
/api-proxy/common/download?url=<encodeURIComponent(fileUrl)>
```

其他类型可能直接打开 `fileUrl`。不要自行改变签名参数；优先使用列表返回的完整地址。
下载既有成功报告不会新建订单或再次扣减免费次数。

### 4. 普通浏览器扩展会话的确定性回放

没有共享 request context 时：打开精确公司页 → 局部断言主体 → 点击“报告” → 从弹窗读取报告项
及其 `href` → 进入专业版订单确认页 → 断言免费次数、应付金额和格式 → 授权后只提交一次 →
进入“我的报告”，按主体/订单号定位“生成成功”的行并下载。

若只需下载已有报告，直接从“我的报告”开始，不必回公司页，更不能先点击生成。

## 四、改版后的再探测

接口失效时先只读分析当期页面已公开加载的前端模块和 XHR URL，定位“产品列表、预订单、
创建订单、订单列表、文件下载”五类动作；先在报告中心用历史成功订单验证只读和下载路径，
最后才在用户明确授权下做一次创建实验。

探测结果写入项目日志时，标明验证日期、站点前端版本（能读取时）、接口方法、必填字段、
副作用级别和验证状态。只从静态代码看到但未在登录会话或真实页面核对的接口，标为
`静态发现/未实跑`，不能写成已验证能力。
