#!/usr/bin/env python3
"""网页合规留痕底座 —— Chrome 真实打印，页眉带查询时间、页脚带完整网址。

架构：浏览器常驻。start 开一次，之后所有脚本连上去干活，浏览器不关。
人工在这个窗口里登录/过验证码，脚本复用同一会话批量打印。

用法：
  python3 capture.py start                启动常驻浏览器（首次需人工登录各站）
  python3 capture.py one <URL> <文件名>    单页留痕
  python3 capture.py stop                 关闭常驻浏览器

其他脚本这样复用：
  from capture import connect, grab, PDF_OPTS
"""
import base64, os, sys, datetime, subprocess, time, urllib.request, re

ROOT = os.path.dirname(os.path.abspath(__file__))
# 浏览器配置是运行时状态，放 ~/.agents/state/ 而不放在 skill 目录里（与 pkulaw-browser 同一约定）：
# 3 万多个文件留在 skill 目录会让 Codex 扫描 ~/.agents/skills 时撞上遍历上限。2026-09-23 由 scripts/_chrome_profile 迁出。
PROF = os.environ.get("WEB_EVIDENCE_PROFILE",
                      os.path.expanduser("~/.agents/state/web-evidence-capture/chrome-profile"))  # 独立目录，见 pitfalls.md
OUT = os.environ.get("WEB_EVIDENCE_OUT", os.path.join(ROOT, "留痕"))
PORT = int(os.environ.get("WEB_EVIDENCE_PORT", "9222"))
CHROME = os.environ.get(
    "WEB_EVIDENCE_CHROME",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

# ── 打印参数：页眉右=查询时间，页脚左=完整网址，右=页码 ──────────────────
# date/url/pageNumber/totalPages 是 Chrome 打印内置占位符，自动填充。
# 这三样（时间/网址/页码）是留痕件可被复核的最低要求，截图拼接达不到。
HDR = ('<div style="font-size:9px;width:100%;padding:0 10px;display:flex;'
       'justify-content:space-between;"><span class="title"></span>'
       '<span class="date"></span></div>')
FTR = ('<div style="font-size:9px;width:100%;padding:0 10px;display:flex;'
       'justify-content:space-between;"><span class="url"></span>'
       '<span><span class="pageNumber"></span>/<span class="totalPages"></span></span></div>')

PDF_OPTS = dict(displayHeaderFooter=True, printBackground=True,
                headerTemplate=HDR, footerTemplate=FTR,
                paperWidth=8.27, paperHeight=11.69,      # A4
                marginTop=0.6, marginBottom=0.6, marginLeft=0.4, marginRight=0.4)

# 标题里出现这些词，说明拿到的多半不是有效结果页（登录墙/验证/错误页）。
# 这是最外层的粗筛；真正的判定要看正文，见 SKILL.md 判定纪律第 1、2 条。
BAD_TITLE_RE = (
    re.compile(r"^\s*(?:用户)?登录(?:\s*[-—_|].*)?$", re.I),
    re.compile(r"验证码|安全验证|人机验证|checking|robot", re.I),
    re.compile(r"(?:^|\D)(?:403|404|412|429)(?:\D|$)|访问受限|拒绝访问", re.I),
    re.compile(r"出错|系统异常|service unavailable", re.I),
)


def bad_title_hits(title):
    """结果页导航栏常含“登录”；只按明确的标题形态拦截，避免裸词误伤。"""
    return [p.pattern for p in BAD_TITLE_RE if p.search(title or "")]


def alive():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2)
        return True
    except Exception:
        return False


def cmd_start():
    if alive():
        print(f"常驻浏览器已在运行（端口 {PORT}）")
        return
    os.makedirs(PROF, exist_ok=True)
    args = [CHROME, f"--remote-debugging-port={PORT}",
            f"--user-data-dir={PROF}", "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled"]
    # WEB_EVIDENCE_HEADLESS=1 → 用 --headless=new 跑批，不抢屏幕焦点。
    # 用 new headless 而不是老的 --headless：老版是另一套精简实现，
    # 打印路径和字体都可能跟有头不一致；new headless 就是同一个浏览器去掉窗口，
    # printToPDF 出来的页眉页脚和排版与有头一致（换模式后要抽一份对拍确认）。
    # **需要人工登录/过验证码的站不能用**，无头下没人能点。
    if os.environ.get("WEB_EVIDENCE_HEADLESS", "").lower() in ("1", "true", "yes"):
        args += ["--headless=new", "--window-size=1440,900",
                 "--disable-gpu", "--hide-scrollbars"]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        time.sleep(1)
        if alive():
            print(f"常驻浏览器已启动（端口 {PORT}）")
            print("请在窗口中完成需要登录/实名的站点认证，之后脚本会复用该会话。")
            return
    print("启动超时。若报「正在现有的浏览器会话中打开」，见 references/pitfalls.md")


def cmd_stop():
    # macOS/BSD pkill 可能把以 -- 开头的匹配串当成选项；按专用 profile 路径匹配。
    subprocess.run(["pkill", "-f", PROF])
    print("已关闭常驻浏览器")


def connect(p):
    """p 是 sync_playwright() 的返回值。连接常驻浏览器。"""
    if not alive():
        print("常驻浏览器未运行，请先执行：python3 capture.py start")
        sys.exit(1)
    endpoint = f"http://127.0.0.1:{PORT}"
    # 新版 Chrome 的默认上下文可能拒绝 Browser.setDownloadBehavior。
    # 本项目使用的 Playwright 提供 no_defaults=True，可跳过这项连接默认设置；
    # 旧版 Playwright 没有该参数时再回退到标准连接。
    try:
        return p.chromium.connect_over_cdp(endpoint, no_defaults=True)
    except TypeError:
        return p.chromium.connect_over_cdp(endpoint)


def grab(ctx, pg, filename, out_dir=None, force=False, pdf_opts=None):
    """打印当前页为 PDF。

    返回 (路径, 标题, 命中的可疑词列表)。标题异常且未 force 时返回 (None, 标题, 命中词)，
    调用方应据此拒绝落盘——避免登录页/验证页混入底稿。

    注意：标题检查只是粗筛。SPA 站点的登录弹窗往往不改变 <title>，
    必须另外检查正文，见 SKILL.md 判定纪律第 1 条。
    """
    title = pg.title()
    hit = bad_title_hits(title)
    if hit and not force:
        return None, title, hit
    # force 只允许保存到诊断隔离区，不能把明知可疑的页面混入正式底稿。
    base = out_dir or OUT
    d = os.path.join(base, "异常页_不得作为底稿") if force else base
    os.makedirs(d, exist_ok=True)
    cdp = ctx.new_cdp_session(pg)
    options = dict(PDF_OPTS)
    options.update(pdf_opts or {})
    r = cdp.send("Page.printToPDF", options)
    path = os.path.join(d, filename)
    with open(path, "wb") as f:
        f.write(base64.b64decode(r["data"]))
        f.flush()
        os.fsync(f.fileno())
    return path, title, []


def pdf_text(path):
    """读取存档 PDF 的文本。判定结论应基于这个，而不是打印前的 DOM。"""
    return subprocess.run(["pdftotext", "-layout", path, "-"],
                          capture_output=True, text=True).stdout


def cmd_one(url, filename, force=False):
    from playwright.sync_api import sync_playwright
    filename = filename.replace("YYYYMMDD", datetime.date.today().strftime("%Y%m%d"))
    with sync_playwright() as p:
        br = connect(p)
        ctx = br.contexts[0]
        pg = ctx.new_page()
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(3500)
            path, title, hit = grab(ctx, pg, filename, force=force)
            if path is None:
                print(f"[跳过] 标题「{title}」含 {hit} —— 疑为登录/验证页，未落盘")
                print("       请在浏览器窗口处理后重跑；--force 仅会保存到异常页隔离目录")
                return
            print(f"标题: {title}")
            print(f"已保存: {os.path.basename(path)}  ({os.path.getsize(path)} bytes)")
        finally:
            if not pg.is_closed():
                pg.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    c = sys.argv[1]
    if c == "start":
        cmd_start()
    elif c == "stop":
        cmd_stop()
    elif c == "one":
        cmd_one(sys.argv[2], sys.argv[3], "--force" in sys.argv)
    else:
        print(__doc__)
