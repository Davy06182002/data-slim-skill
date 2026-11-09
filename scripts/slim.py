#!/usr/bin/env python3
"""data-slim 数据瘦身脚本

用法:
  python3 slim.py csv <file.csv> [-o OUT] [--keep a,b,c] [--drop a,b,c] [--head N]
  python3 slim.py pdf <file.pdf> [-o OUT] [--dpi 300] [--thresh 175]
  python3 slim.py ocr <file.pdf> [-o OUT] [--dpi 300] [--lang chi_sim+eng] [--psm 3] [--oem 3]
  python3 slim.py text <file.txt|md|...> [-o OUT] [--max-chars N]
  python3 slim.py json <file.json> [-o OUT]
  python3 slim.py estimate <file>
  python3 slim.py dashboard                 # 账本汇总(仅显示省 token, 不换算金额)
  python3 slim.py batch <dir> [--report out.json]  # [Pro] 批量瘦身整目录 + 导出用量报告
  python3 slim.py config set model <名称>   # 记录默认模型(金额换算已停用)

功能:
  csv   : 删空行/空列、全角转半角、去重、按需裁列、截取前 N 行 -> 紧凑 CSV/TSV
  pdf   : 黑白文档 PDF 压缩 (300dpi 灰度 -> 阈值二值化 -> 1-bit PNG -> 重建)
  text  : 压缩空白/连续空行; --max-chars 用「头+尾」保留策略截断
  json  : minify (去缩进去空格)
  index : 为目录生成摘要索引 INDEX.md (第2层按需加载: 对话只载索引, 按索引点名文件)
  estimate: 估算文件文本的 token 数 (启发式: ASCII/4 + CJK*1.2)
  dashboard: 读取 SQLite 账本, 汇总累计/本月省 token 与体积, 按类型/Top 文件拆分
  config   : 设置默认模型(金额换算已停用)

依赖: csv/text/json/estimate/dashboard 仅用标准库; pdf 需 pymupdf + pillow。
输出默认写到原文件同目录, 文件名加 .slim 后缀。每次瘦身自动记账到 ~/.data-slim/ledger.db。
"""
import argparse
import csv
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

# 标准全角区 FF01-FF5F -> 半角 21-7E（一次性覆盖 ．－＝ 等之前漏掉的字符）
_FW = "".join(chr(c) for c in range(0xFF01, 0xFF5F))
_HW = "".join(chr(c - 0xFEE0) for c in range(0xFF01, 0xFF5F))
# 额外：CJK 语境惯用标点（不在 FF01-FF5F 内，需单独补）
_FW += "　、。 【】“”‘’"          # 全角空格、顿号、句号、方头括号、弯引号
_HW += " ,. {}\"\"''"
FW2HW = str.maketrans(_FW, _HW)


def token_estimate(text: str) -> int:
    """启发式 token 估算: 通用分词器口径
    ASCII 4字符≈1token; CJK 1字≈1.2token; 全角字母/数字/标点(U+FF00-FFEF)≈1token。
    计入全角字符, 否则全角->半角归一化会被计成 0 而误报"负节省"。"""
    ascii_n = sum(1 for c in text if ord(c) < 128)
    cjk_n = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    fw_n = sum(1 for c in text if 0xFF00 <= ord(c) <= 0xFFEF)
    return int(ascii_n / 4 + cjk_n * 1.2 + fw_n)


def report(before_path: str, after_path: str, before_text: str, after_text: str) -> None:
    b_bytes, a_bytes = os.path.getsize(before_path), os.path.getsize(after_path)
    b_tok, a_tok = token_estimate(before_text), token_estimate(after_text)
    print(f"[data-slim] {os.path.basename(before_path)} -> {os.path.basename(after_path)}")
    print(f"  文件大小: {_human(b_bytes)} -> {_human(a_bytes)}  (省 {100 - a_bytes * 100 // max(b_bytes, 1)}%)")
    print(f"  token估算: ~{b_tok:,} -> ~{a_tok:,}  (省 {b_tok - a_tok:,}, {100 - a_tok * 100 // max(b_tok, 1)}%)")


# ---------------- 账本 (主指标 token, 金额不换算) ----------------
LEDGER = os.path.expanduser(os.environ.get("DATA_SLIM_LEDGER", "~/.data-slim/ledger.db"))
MILESTONE = int(os.environ.get("DATA_SLIM_MILESTONE", 10000))  # 累计省 token 每跨过该值自动报一次里程碑


def _db():
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    conn = sqlite3.connect(LEDGER)
    conn.execute("CREATE TABLE IF NOT EXISTS records(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, op TEXT, file TEXT, before_tok INT, after_tok INT, saved_tok INT, before_bytes INT, after_bytes INT, model TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
    conn.commit()
    return conn


def kv_get(k):
    try:
        conn = _db(); r = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone(); conn.close()
        return r[0] if r else None
    except Exception:
        return None


def kv_set(k, v):
    try:
        conn = _db(); conn.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=?", (k, v, v)); conn.commit(); conn.close()
    except Exception:
        pass


def get_model(args):
    """agent 触发时 --model 自动注入并记录; 独立 CLI 复用上次模型。"""
    m = getattr(args, "model", None)
    if m:
        kv_set("last_model", m)
        return m
    return kv_get("last_model")


def _milestone_check():
    '''累计省 token 跨过 MILESTONE 整数倍时, 在输出里报一次里程碑。'''
    try:
        conn = _db()
        total = conn.execute('SELECT COALESCE(SUM(saved_tok),0) FROM records').fetchone()[0]
        conn.close()
        n = total // MILESTONE
        last = kv_get('last_milestone')
        if last is None:
            # 首次启用: 对齐已有累计, 不补报历史里程碑
            if n > 0:
                kv_set('last_milestone', str(n))
            return
        last = int(last)
        if n > last:
            kv_set('last_milestone', str(n))
            print(f"[data-slim] 🎉 里程碑: 累计已省 ~{total:,} token，突破第 {n} 个 {MILESTONE:,} 门槛")
            # 金额不换算: 仅显示 token, 不折人民币
    except Exception:
        pass


def record(op, before_path, after_path, before_text, after_text, model=None):
    """瘦身完成后自动记账; 任何异常都不影响主流程。"""
    try:
        bt = token_estimate(before_text); at = token_estimate(after_text)
        bb = os.path.getsize(before_path); ab = os.path.getsize(after_path)
        conn = _db()
        conn.execute(
            "INSERT INTO records(ts,op,file,before_tok,after_tok,saved_tok,before_bytes,after_bytes,model) VALUES(?,?,?,?,?,?,?,?,?)",
            (datetime.datetime.now().isoformat(timespec="seconds"), op, os.path.basename(before_path), bt, at, bt - at, bb, ab, model),
        )
        conn.commit(); conn.close(); _milestone_check()
    except Exception as e:
        print(f"[data-slim] 记账跳过: {e}")


def cmd_dashboard(args) -> None:
    try:
        conn = _db()
        rows = conn.execute("SELECT ts,op,file,before_tok,after_tok,saved_tok,before_bytes,after_bytes,model FROM records ORDER BY ts").fetchall()
        conn.close()
    except Exception as e:
        print(f"[data-slim] 读取账本失败: {e}"); return
    if not rows:
        print("[data-slim] 账本为空。先跑一次 csv/pdf/text/json，记录会自动写入。")
        return
    from collections import Counter
    total_saved = sum(r[5] for r in rows)
    total_bytes = sum(r[6] - r[7] for r in rows)
    month = datetime.datetime.now().strftime("%Y-%m")
    m_rows = [r for r in rows if r[0][:7] == month]
    m_saved = sum(r[5] for r in m_rows)
    m_bytes = sum(r[6] - r[7] for r in m_rows)
    print("[data-slim] 账本汇总（主指标: 省 token）")
    print(f"  累计: 省 ~{total_saved:,} token | 省 {_human(total_bytes)}")
    print(f"  本月({month}): 省 ~{m_saved:,} token | 省 {_human(m_bytes)}")
    print("  (金额不换算: 仅显示省 token, 不折人民币)")
    op_n = Counter(r[1] for r in rows); op_t = Counter(); op_b = Counter()
    for r in rows:
        op_t[r[1]] += r[5]
        op_b[r[1]] += (r[6] - r[7])
    print("  按类型:")
    for op in op_n:
        tag = " (估算)" if op == "ocr" else ""
        print(f"    {op}{tag}: {op_n[op]} 次, 省 ~{op_t[op]:,} token | 省 {_human(op_b[op])}")
    top = sorted(rows, key=lambda r: r[5], reverse=True)[:5]
    print("  Top5 省 token 文件:")
    for r in top:
        print(f"    {r[2]}: 省 ~{r[5]:,} token" + (f"  [{r[8]}]" if r[8] else ""))


def cmd_record(args) -> None:
    """L4 语义压缩记账: agent 生成收束版后, 记录省 token (仅 token, 不折算金额)。"""
    before = read_text(args.infile)
    after = read_text(args.outfile)
    record("l4", args.infile, args.outfile, before, after, args.model)
    bt, at = token_estimate(before), token_estimate(after)
    saved = bt - at
    pct = round(saved * 100 / max(bt, 1))
    print(f"[data-slim] L4 语义压缩记账完成: ~{bt:,} -> ~{at:,} token, 省 ~{saved:,} ({pct}%)")


def cmd_pro(args) -> None:
    """Pro 功能占位：验证 License Key，打印等级与有效期。
    当前 data-slim 全部免费；此命令仅就位校验链路，不锁定任何现有功能。
    校验纯本地（Ed25519 验签），零联网、不泄露用户信息。"""
    key = args.key or os.environ.get("DATA_SLIM_LICENSE", "")
    if not key:
        print("[data-slim] 未提供 License Key（--key 或环境变量 DATA_SLIM_LICENSE）。")
        print("  当前所有功能均免费，无需 License；Pro 规划见 SKILL.md「免费 / Pro 规划」章节。")
        return
    try:
        from slim_license import verify, LicenseError
    except ImportError:
        raise SystemExit("[data-slim] License 校验需要 cryptography: pip install cryptography")
    try:
        p = verify(key)
        print(f"[data-slim] License 有效 | tier={p['tier']} | email={p.get('email')} | 到期={p['exp']}")
        if p["tier"] in ("pro", "team"):
            print("  Pro 功能已解锁：`batch <dir>` 批量处理已可用（需 Pro License）；L5 超强语义压缩 / 团队协作 / 用量看板 规划中。")
        else:
            print("  当前为免费版；Pro 高级功能需对应 License。")
    except LicenseError as e:
        print(f"[data-slim] License 无效: {e}")
        sys.exit(2)


def require_pro() -> str:
    """Pro 功能入口网关：返回 tier（'pro'/'team'）；非授权直接 SystemExit 告知升级。

    校验纯本地（Ed25519 验签），零联网、不泄露用户信息（符合整改合规）。
    未授权时明确报错并引导 SKILL.md CTA，绝不偷偷降级到错误结果、绝不静默联网。
    """
    key = os.environ.get("DATA_SLIM_LICENSE", "")
    if not key:
        raise SystemExit(
            "[data-slim] 该功能为 Pro 专属，需要 License Key。\n"
            "  设置环境变量 DATA_SLIM_LICENSE=<你的Key> 后重试。\n"
            "  免费版可用全部 L1–L4 功能；Pro 升级见 SKILL.md「免费 / Pro 规划」章节。"
        )
    try:
        from slim_license import verify, LicenseError
    except ImportError:
        raise SystemExit("[data-slim] Pro 校验需要 cryptography: pip install cryptography")
    try:
        p = verify(key)
    except LicenseError as e:
        raise SystemExit(
            f"[data-slim] Pro 功能需要有效 License Key: {e}\n"
            "  升级见 SKILL.md「免费 / Pro 规划」章节。"
        )
    if p["tier"] in ("pro", "team"):
        return p["tier"]
    raise SystemExit(
        "[data-slim] 当前 License 为免费版，该功能需 Pro 授权。\n"
        "  升级见 SKILL.md「免费 / Pro 规划」章节。"
    )


def cmd_config(args) -> None:
    if args.action == "set" and args.key == "price":
        print("[data-slim] 金额换算已停用: 报告仅显示省 token, 不折人民币。该设置已忽略。")
    elif args.action == "set" and args.key == "model":
        kv_set("last_model", args.value)
        print(f"[data-slim] 已记默认模型: {args.value}")
    elif args.action == "get":
        print(f"  {args.key} = {kv_get(args.key)}")
    else:
        print("[data-slim] 用法: config set model <名称> | config get <key>")


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n}MB"


def _outpath(src: str, out: str | None, suffix: str) -> str:
    if out:
        return out
    base, _ = os.path.splitext(src)
    return f"{base}.slim{suffix}"


def read_text(src: str) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            with open(src, encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"[data-slim] 无法识别编码: {src}")


# ---------------- csv ----------------
def cmd_csv(args) -> None:
    rows = list(csv.reader(read_text(args.file).splitlines()))
    if not rows:
        raise SystemExit("[data-slim] 空文件")
    keep_cols = set(args.keep.split(",")) if args.keep else None
    drop_cols = set(args.drop.split(",")) if args.drop else set()

    # 找空列 (整列为空 -> 删)
    n_cols = max(len(r) for r in rows)
    empty_cols = set()
    for c in range(n_cols):
        if all(c >= len(r) or not r[c].strip() for r in rows[1:]):
            empty_cols.add(c)
    header = rows[0]

    # 列选择优先级: --keep > --drop > 自动删空列
    if keep_cols:
        keep_idx = [i for i, h in enumerate(header) if h.strip() in keep_cols]
    elif drop_cols:
        keep_idx = [i for i, h in enumerate(header) if h.strip() not in drop_cols]
    else:
        keep_idx = [i for i in range(n_cols) if i not in empty_cols]

    # 全角转半角 + 去首尾空白
    def clean_cell(v: str) -> str:
        return v.translate(FW2HW).strip()

    out_rows = [[clean_cell(r[i]) if i < len(r) else "" for i in keep_idx] for r in rows]

    # 删空行 (数据区整行为空) + 去重
    content_rows = [r for r in out_rows[1:] if any(r)]
    body = content_rows
    dup_count = 0
    if not args.keep_dup:
        deduped = list(dict.fromkeys(map(tuple, content_rows)))
        dup_count = len(content_rows) - len(deduped)
        body = deduped
    if args.head:
        body = body[: args.head]

    # --dry-run: 仅预览将执行的裁列/去重/截行, 不写文件 (契合"先审计后落地")
    empty_headers = [rows[0][c] for c in sorted(empty_cols) if c < len(rows[0])]
    plan = [
        "[data-slim] dry-run 预览(不写文件):",
        f"  原始: {len(out_rows) - 1} 数据行 × {n_cols} 列",
        f"  将删空列: {len(empty_headers)} 个 -> {empty_headers if empty_headers else '无'}",
        f"  将保留列: {len(out_rows[0])} 列",
        "  全角转半角: 是",
        (f"  将去重: {dup_count} 行" if not args.keep_dup else "  去重: 跳过(--keep-dup)"),
        (f"  将截行: 前 {args.head} 行" if args.head else "  截行: 无"),
        f"  预计输出: {len(body)} 行 × {len(out_rows[0])} 列",
    ]
    if args.dry_run:
        print("\n".join(plan))
        return

    out = _outpath(args.file, args.out, ".csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(out_rows[0])
        w.writerows(body)
    before = read_text(args.file)
    after = open(out, encoding="utf-8").read()
    print(f"[data-slim] 行数: {len(rows) - 1} -> {len(body)}; 列数: {n_cols} -> {len(out_rows[0])}")
    report(args.file, out, before, after)
    record("csv", args.file, out, before, after, get_model(args))


# ---------------- pdf ----------------
def cmd_pdf(args) -> None:
    try:
        import fitz  # PyMuPDF
        from PIL import Image
        import io
    except ImportError as e:
        raise SystemExit(f"[data-slim] PDF 压缩需要依赖: pip install pymupdf pillow ({e})")

    src_doc = fitz.open(args.file)
    out_doc = fitz.open()
    for page in src_doc:
        pix = page.get_pixmap(dpi=args.dpi, colorspace=fitz.csGRAY)
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
        img = img.point(lambda p: 0 if p < args.thresh else 255, mode="1")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        new_page = out_doc.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(new_page.rect, stream=buf.getvalue())
    out = _outpath(args.file, args.out, ".pdf")
    out_doc.save(out, garbage=4, deflate=True)

    # —— 来源检测 (item6): 文本层 / 彩色, 给出正确用法提示 ——
    try:
        btext = "\n".join(p.get_text() for p in src_doc)
    except Exception:
        btext = ""
    has_text = len(btext.strip()) >= 100
    if has_text:
        print(f"[data-slim][警告] 源 PDF 含文本层(~{len(btext.strip())}字符)。pdf 光栅化会把文本烧成图像并丢失文本层"
              f" → 模型后续将改按图读(更费 token)。若目标是省 token, 请改用 `ocr` 或直接按文本读; 确认只压体积再继续。")
    has_color = False
    for pg in src_doc:
        for im in pg.get_images(full=True):
            try:
                info = src_doc.extract_image(im[0])
                cs = str(info.get("colorspace", ""))
                if any(k in cs for k in ("RGB", "CMYK", "LAB", "Indexed")):
                    has_color = True
            except Exception:
                pass
            if has_color:
                break
        if has_color:
            break
    if has_color:
        print("[data-slim][警告] 源 PDF 含彩色图像, 二值化(阈值)会丢色彩信息; 如需保留颜色可降 --dpi 或改用 `ocr` 抽文本层。")

    # —— 主指标①: 省体积 (传输/存储/同步/摄取可靠性) ——
    print(f"[data-slim] 页数: {len(src_doc)}; dpi={args.dpi} 阈值={args.thresh}")
    b, a = os.path.getsize(args.file), os.path.getsize(out)
    pct = 100 - a * 100 // max(b, 1)
    print(f"  [省体积] {_human(b)} -> {_human(a)}  (省 {pct}%)")
    # —— 主指标②: 省 token (仅当存在文本层才有意义) ——
    try:
        atext = "\n".join(p.get_text() for p in fitz.open(out))
    except Exception:
        atext = ""
    bt, at = token_estimate(btext), token_estimate(atext)
    if has_text:
        print(f"  [省token] ~{bt:,} -> ~{at:,}  (省 {bt - at:,}, {100 - at * 100 // max(bt, 1)}%)  ← 含文本层, 真省上下文")
    else:
        print(f"  [省token] ~0  (转曲/纯图像 PDF 无文本层; token 由分辨率定, 与体积无关)")
        print(f"           → 本瘦身省的是「体积」而非「token」; 要省 token 先跑 `ocr` 抽文本层")
    if a >= b:
        print("  [提示] 输出比原文件更大(小体积矢量 PDF 光栅化会膨胀), 此时直接用原文件。")
    record("pdf", args.file, out, btext, atext, get_model(args))


def record_ocr(op, src, out_md, img_tok, text_tok, model):
    """ocr 专有用: 记录「按图像读(估算) -> 按文本读(精确)」的 token 差。"""
    try:
        bb = os.path.getsize(src); ab = os.path.getsize(out_md)
        conn = _db()
        conn.execute(
            "INSERT INTO records(ts,op,file,before_tok,after_tok,saved_tok,before_bytes,after_bytes,model) VALUES(?,?,?,?,?,?,?,?,?)",
            (datetime.datetime.now().isoformat(timespec="seconds"), op, os.path.basename(src), img_tok, text_tok, img_tok - text_tok, bb, ab, model),
        )
        conn.commit(); conn.close(); _milestone_check()
    except Exception as e:
        print(f"[data-slim] 记账跳过: {e}")


def _ocr_page(doc_path, i, dpi, lang, psm, oem, tmpdir):
    """单页: 渲染 -> tesseract -> 文本。供线程池并行(每线程独立打开文档, 避免跨线程共享)。"""
    import fitz as _fitz
    doc = _fitz.open(doc_path)
    page = doc[i]
    pix = page.get_pixmap(dpi=dpi)
    img_tok = math.ceil(pix.width / 1024) * math.ceil(pix.height / 1024) * 255
    img = os.path.join(tmpdir, f"p{i}.png")
    pix.save(img)
    tmpout = os.path.join(tmpdir, f"p{i}")
    cmd = ["tesseract", img, tmpout, "-l", lang, "--psm", str(psm)]
    if oem:
        cmd += ["--oem", str(oem)]
    subprocess.run(cmd, capture_output=True)
    txt = open(tmpout + ".txt", encoding="utf-8", errors="replace").read().strip()
    try:
        os.remove(img); os.remove(tmpout + ".txt")
    except OSError:
        pass
    return i, txt, img_tok


def cmd_ocr(args) -> None:
    """OCR 转曲/纯图像 PDF: 渲染每页 -> tesseract 识别 -> 产出 <名>.slim.md 文本层。
    价值: 原 PDF 无文本层, 模型按图像读(上下文 token 由分辨率决定, 与字节无关);
    OCR 后改按文本读, 真正减少进上下文的 token。
    调优: --psm 控制版面分割(密集多栏报价单可试 4/6, 稀疏小图试 11/12); --oem 选引擎。"""
    if not shutil.which("tesseract"):
        raise SystemExit("[data-slim] 未找到 tesseract。\n  安装: brew install tesseract tesseract-lang\n  (Intel Mac 新版无预编译 bottle 且 github 受限时, 可用 micromamba: micromamba install -c conda-forge tesseract)")
    try:
        import fitz
    except ImportError:
        raise SystemExit("[data-slim] OCR 需要 PyMuPDF: pip install pymupdf")
    tmpdir = os.path.join(os.path.expanduser("~"), ".data-slim", "ocr_tmp")
    os.makedirs(tmpdir, exist_ok=True)
    doc = fitz.open(args.file)
    pages = len(doc)
    dpi = args.dpi
    lang = args.lang
    psm = args.psm
    oem = args.oem
    doc.close()  # 关闭主文档, 各工作线程独立打开, 避免跨线程共享
    print(f"[data-slim] OCR 开始: {pages} 页 | dpi={dpi} | 并行处理中...")
    tasks = [(args.file, i, dpi, lang, psm, oem, tmpdir) for i in range(pages)]
    results = {}
    done = 0
    with ThreadPoolExecutor(max_workers=min(4, max(1, pages))) as ex:
        futs = {ex.submit(_ocr_page, *t): t[1] for t in tasks}
        for fut in as_completed(futs):
            i, txt, itok = fut.result()
            results[i] = (txt, itok)
            done += 1
            print(f"  进度: {done}/{pages} 页", end="\r", flush=True)
    print()
    img_tok_total = sum(results[i][1] for i in range(pages))
    blocks = [f"## 第 {i+1} 页\n\n{results[i][0]}" for i in range(pages)]
    out_md = _outpath(args.file, args.out, ".md")
    header = (
        f"# OCR 文本层 · {os.path.basename(args.file)}\n\n"
        f"> 来源: {args.file}\n> 页数: {pages} | 语言: {lang} | dpi: {dpi} | psm: {psm}" + (f" | oem: {args.oem}" if args.oem else "") + "\n"
        f"> 由 data-slim `ocr` 生成, 供模型按文本读取以省上下文 token。\n\n---\n\n"
    )
    md = header + "\n\n".join(blocks) + "\n"
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(md)
    text_tok = token_estimate(md)
    saved = img_tok_total - text_tok
    pct = 100 - text_tok * 100 // max(img_tok_total, 1)
    print(f"[data-slim] OCR 完成: {pages} 页 -> {out_md}")
    b, a = os.path.getsize(args.file), os.path.getsize(out_md)
    print(f"  文件大小: {_human(b)} -> {_human(a)}")
    print(f"  OCR 文本 token(精确): ~{text_tok:,}")
    print(f"  原PDF按图像读 token(估算, 取决于VLM): ~{img_tok_total:,}")
    print(f"  => 按文本读省 ~{saved:,} token (约 {pct}%)")
    print("  [注意] 图像 token 为常见 VLM 估算(每1024px tile≈255 token), 真实值取决于你用的模型与分辨率策略。")
    record_ocr("ocr", args.file, out_md, img_tok_total, text_tok, get_model(args))


# ---------------- text ----------------
def cmd_text(args) -> None:
    text = read_text(args.file)
    # 压缩空白: 行尾空白、3+ 连续空行 -> 1 空行、行内多空格 -> 1
    text = re.sub(r"[ \t]+$", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    truncated = False
    if args.max_chars and len(text) > args.max_chars:
        head = args.max_chars * 3 // 4
        tail = args.max_chars - head
        text = (
            text[:head]
            + f"\n\n...[已截断: 省略中间 {len(text) - args.max_chars} 字符]...\n\n"
            + text[-tail:]
        )
        truncated = True
    out = _outpath(args.file, args.out, ".txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    if truncated:
        print("[data-slim] 已启用头75%+尾25%截断")
    report(args.file, out, read_text(args.file), text)
    record("text", args.file, out, read_text(args.file), text, get_model(args))


# ---------------- json ----------------
def cmd_json(args) -> None:
    text = read_text(args.file)
    data = json.loads(text)
    out = _outpath(args.file, args.out, ".json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    report(args.file, out, text, open(out, encoding="utf-8").read())
    record("json", args.file, out, text, open(out, encoding="utf-8").read(), get_model(args))


# ---------------- estimate ----------------
# ---------------- index (第2层: 按需加载) ----------------
SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def _walk(root: str, depth: int, files_per_dir: int, collapse: int) -> tuple[list[str], int, int]:
    """返回索引行列表、总文件数、总字节数。depth=目录展开层数"""
    lines: list[str] = []
    total_files = total_bytes = 0

    def size_of(p: str) -> int:
        try:
            return os.path.getsize(p)
        except OSError:
            return 0

    def _skip(e: str) -> bool:
        return e in SKIP_NAMES or e.startswith(".") or e.startswith("~$")

    def emit(path: str, rel: str, level: int) -> None:
        nonlocal total_files, total_bytes
        indent = "  " * level
        if os.path.isdir(path):
            entries = [e for e in sorted(os.listdir(path)) if not _skip(e)]
            files = [e for e in entries if os.path.isfile(os.path.join(path, e))]
            subdirs = [e for e in entries if os.path.isdir(os.path.join(path, e))]
            all_files = sum(len(fn) for _, dn, fn in os.walk(path) if not SKIP_NAMES.intersection(fn))
            sz = sum(size_of(os.path.join(dp, f)) for dp, _, fs in os.walk(path) for f in fs)
            total_files += all_files
            total_bytes += sz
            # 子目录过多 -> 折叠: 只给样例名, 不递归 (防索引爆炸)
            if len(subdirs) > collapse:
                sample = "、".join(subdirs[:3])
                lines.append(f"{indent}- {os.path.basename(path) or rel}/ 含{len(subdirs)}个子目录({all_files}文件, {_human(int(sz))}), 未展开, 样例: {sample} 等")
                return
            names = "、".join(files[: files_per_dir])
            more = f" 等{len(files)}个" if len(files) > files_per_dir else ""
            subs = f" 含{len(subdirs)}个子目录 " if subdirs else " "
            tail = f"{names}{more}" if names else ""
            lines.append(f"{indent}- {os.path.basename(path) or rel}/{subs}({all_files}文件, {_human(int(sz))})" + (f": {tail}" if tail else ""))
            if level < depth:
                for sd in subdirs:
                    emit(os.path.join(path, sd), os.path.join(rel, sd), level + 1)
        else:
            sz = size_of(path)
            total_files += 1
            total_bytes += sz
            lines.append(f"{indent}- {os.path.basename(path)} ({_human(sz)})")

    emit(root, root, 0)
    return lines, total_files, total_bytes


def cmd_index(args) -> None:
    root = os.path.abspath(args.dir)
    if not os.path.isdir(root):
        raise SystemExit(f"[data-slim] 目录不存在: {root}")
    lines, n_files, n_bytes = _walk(root, args.depth, args.files_per_dir, args.collapse)
    head = [
        f"# 资料索引 · {os.path.basename(root)}",
        "",
        f"共 {n_files} 个文件 / {_human(n_bytes)}。本文件是目录页: 先在此定位, 再按需点名具体文件, 不要整目录载入。",
        "",
    ]
    out = args.out or os.path.join(root, "INDEX.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(head + lines) + "\n")
    text = "\n".join(head + lines)
    print(f"[data-slim] 索引 -> {out}")
    print(f"  覆盖: {n_files} 文件 / {_human(n_bytes)}; 索引行 {len(lines)} 条")
    print(f"  token估算: ~{token_estimate(text):,} (载入索引用它, 原始资料不进上下文)")


def cmd_estimate(args) -> None:
    if args.file.lower().endswith(".pdf"):
        try:
            import fitz
        except ImportError:
            raise SystemExit("[data-slim] 需要 pip install pymupdf")
        doc = fitz.open(args.file)
        text = "\n".join(p.get_text() for p in doc)
        print(f"[data-slim] PDF {len(doc)} 页, 提取文本 {len(text):,} 字符")
    else:
        text = read_text(args.file)
    n = token_estimate(text)
    print(f"  字符数: {len(text):,} (CJK {sum(1 for c in text if chr(0x4E00) <= c <= chr(0x9FFF)):,})")
    print(f"  token估算(通用分词): ~{n:,}")
    print(f"  token估算(中文优化模型如DeepSeek/Qwen): ~{int(n * 0.6):,}")


def _slim_chat(text: str) -> str:
    """对话瘦身(精简回复): 去客套框架句 + 压缩空白 + 去行尾空格。用于对话瘦身记账的近似。"""
    import re
    patterns = [
        r"^好的[，,。]?\s*[^。\n]*[。！!]?\n",
        r"^好嘞[，,。]?\s*[^。\n]*[。！!]?\n",
        r"^没问题[，,。]?\s*[^。\n]*[。！!]?\n",
        r"^当然[，,。]?\s*[^。\n]*[。！!]?\n",
        r"希望(以上|这)(内容|回答|说明)?对您有?帮助[。！!\n]?",
        r"如有(任何)?疑问(，|,)?随时(告诉|问|联系)我[。！!\n]?",
        r"让我(为您|帮你|来帮您|为你).*?[。\n]",
        r"以下是(关于|针对).*?(的)?(回答|说明|介绍|解读)[：:]\n",
        r"I hope this helps[.\n]?",
        r"Let me (help you|assist you|know)[.\n]?",
        r"Feel free to (ask|let me know)[.\n]?",
    ]
    for p in patterns:
        text = re.sub(p, "", text, flags=re.M | re.I)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()


def _slim_chat_struct(text: str) -> str:
    """精简回复 + 结论先行速览(规则启发式, 非 LLM)。
    做法: 先剥客套/压空白(精简回复), 再对每段取首句组成『速览』块置于文首, 实现结论先行。
    局限: 纯规则, 不真正理解语义; 仅适合"每段首句即要点"的回复。"""
    base = _slim_chat(text)
    paras = [p.strip() for p in re.split(r"\n{2,}", base) if p.strip()]
    leads = []
    for p in paras:
        m = re.split(r"(?<=[。！？!?])", p, maxsplit=1)
        lead = m[0].strip()
        if lead:
            leads.append(lead)
    if len(leads) < 2:
        return base
    summary = "\n".join(f"- {l}" for l in leads)
    return f"【速览】\n{summary}\n\n---\n\n{base}"


def cmd_chat(args) -> None:
    """对话瘦身记账: 对一段 AI 回复做精简(去客套/压缩空白), 估算省下的 token。
    价值: 让对话瘦身「可见」——输出收束版与省 token 数, 呼应对话侧原本无 ledger 的问题。
    注意: 这是剔除客套/冗余的近似估算, --structural 仅加规则版"结论先行"速览, 非 LLM 重写。"""
    if args.file == "-":
        text = sys.stdin.read()
    else:
        text = read_text(args.file)
    slim = _slim_chat_struct(text) if args.structural else _slim_chat(text)
    bt, at = token_estimate(text), token_estimate(slim)
    saved = bt - at
    pct = round(saved * 100 / max(bt, 1))
    mode = "精简回复" + (" + 结论先行" if args.structural else "")
    print(f"[data-slim] 对话瘦身 · 估算省 token（{mode}）")
    print(f"  原始 token: ~{bt:,}  ->  收束后: ~{at:,}")
    print(f"  省 ~{saved:,} token ({pct}%)  [剔除客套/压缩空白" + (" + 结论先行速览" if args.structural else "") + " 的近似估算]")
    tmp = os.path.join(os.path.expanduser("~"), ".data-slim")
    os.makedirs(tmp, exist_ok=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(slim)
        print(f"  收束版已写入: {args.out}")
        bp = args.file if args.file != "-" else os.path.join(tmp, "_chat_in.txt")
        if args.file == "-":
            with open(bp, "w", encoding="utf-8") as f:
                f.write(text)
        ap = args.out
    else:
        bp = os.path.join(tmp, "_chat_in.txt"); ap = os.path.join(tmp, "_chat_out.txt")
        with open(bp, "w", encoding="utf-8") as f: f.write(text)
        with open(ap, "w", encoding="utf-8") as f: f.write(slim)
        print("  (未指定 --out, 收束版未落盘; 仅记账)")
    record("chat", bp, ap, text, slim, get_model(args))


# ---------------- auto (按类型自动路由瘦身) ----------------
def _auto_kind(path: str):
    """按扩展名/内容判定文件类型与应调用的瘦身子命令。PDF 需判定有无文本层。"""
    name = os.path.basename(path)
    if ".slim." in name:   # 已是瘦身产物, 跳过(防叠罗汉 .slim.slim.*)
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return "csv"
    if ext == ".json":
        return "json"
    if ext in (".md", ".txt", ".text"):
        return "text"
    if ext == ".pdf":
        try:
            import fitz
            doc = fitz.open(path)
            txt = "".join(p.get_text() for p in doc)
            doc.close()
            return "ocr" if len(txt.strip()) < 100 else "pdf"
        except Exception:
            return "ocr"  # 依赖缺失/异常时保守按无文本层处理
    return None


def _already_slimmed(path: str) -> bool:
    """同目录已存在任意 <名>.slim.* 产物则视为已处理。
    注意各子命令产物扩展名不同: csv/json -> .slim.csv/.slim.json,
    pdf -> .slim.pdf, ocr -> .slim.md, text -> .slim.txt。
    仅查 .slim{ext} 会让 PDF(走 ocr 产出 .slim.md) 被反复重跑, 故扩展候选集。"""
    d, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    candidates = [f"{stem}.slim{ext}", f"{stem}.slim.md", f"{stem}.slim.txt"]
    return any(os.path.exists(os.path.join(d, c)) for c in candidates)


def _collect_auto_targets(path: str, recursive: bool) -> list:
    if os.path.isfile(path):
        k = _auto_kind(path)
        return [(path, k)] if k else []
    out = []
    for root, dirs, files in os.walk(path):
        if not recursive:
            dirs[:] = []
        for fn in files:
            fp = os.path.join(root, fn)
            k = _auto_kind(fp)
            if k:
                out.append((fp, k))
    return out


def cmd_auto(args) -> None:
    """自动瘦身引擎: 扫描路径下数据文件, 按类型自动调用对应子命令。
    这是『帮我瘦身』启用后对用户点名数据文件自动瘦身的能力载体。
    支持一次传入多个文件/目录(用户一条消息可能带多个附件), 全部自动处理。"""
    if os.environ.get("DATA_SLIM_AUTO") == "0":
        print("[data-slim] 自动瘦身已通过 DATA_SLIM_AUTO=0 关闭, 跳过。")
        return
    paths = args.path if isinstance(args.path, list) else [args.path]
    targets = []
    for p in paths:
        targets.extend(_collect_auto_targets(p, getattr(args, "recursive", False)))
    if not targets:
        print("[data-slim] 自动瘦身: 未发现可处理的数据文件。")
        return
    print(f"[data-slim] 自动瘦身: 发现 {len(targets)} 个数据文件, 开始处理...")
    processed = skipped = 0
    for f, kind in targets:
        if _already_slimmed(f):
            print(f"  ↳ 跳过(已瘦身): {os.path.basename(f)}")
            skipped += 1
            continue
        a = argparse.Namespace(
            file=f, out=None, model=getattr(args, "model", None),
            keep=None, drop=None, head=None, keep_dup=True, dry_run=False,
            dpi=300, thresh=175, lang="chi_sim+eng", psm=3, oem=3,
            max_chars=None, structural=False,
        )
        try:
            {"csv": cmd_csv, "json": cmd_json, "text": cmd_text,
             "ocr": cmd_ocr, "pdf": cmd_pdf}[kind](a)
            processed += 1
        except SystemExit as e:
            print(f"  ↳ 跳过(依赖缺失/错误): {os.path.basename(f)} -> {e}")
    print(f"[data-slim] 自动瘦身完成: 处理 {processed} 个, 跳过 {skipped} 个(已瘦身)。累计省 token 见 dashboard。")


# ---------------- batch (Pro: 批量处理 + 用量报告导出) ----------------
_BATCH_SUFFIX = {"csv": ".csv", "json": ".json", "text": ".txt", "ocr": ".md", "pdf": ".pdf"}
_BATCH_SUB = {"csv": "csv", "json": "json", "text": "text", "ocr": "ocr", "pdf": "pdf"}


def _export_batch_report(path: str, rows: list) -> None:
    """rows: list of (fp, kind, 'ok', bt, bb, at, ab) —— 导出 Pro 用量报告。"""
    import csv as _csv
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        data = [{
            "file": os.path.basename(r[0]), "type": r[1],
            "before_tok": r[3], "after_tok": r[5], "saved_tok": r[3] - r[5],
            "before_bytes": r[4], "after_bytes": r[6],
            "saved_pct": round((r[3] - r[5]) * 100 / max(r[3], 1)),
        } for r in rows]
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "files": data,
                "total_saved_tok": sum(r[3] - r[5] for r in rows),
                "total_saved_bytes": sum(r[4] - r[6] for r in rows),
            }, f, ensure_ascii=False, indent=2)
    else:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = _csv.writer(f)
            w.writerow(["file", "type", "before_tok", "after_tok", "saved_tok", "before_bytes", "after_bytes", "saved_pct"])
            for r in rows:
                w.writerow([os.path.basename(r[0]), r[1], r[3], r[5], r[3] - r[5], r[4], r[6],
                            round((r[3] - r[5]) * 100 / max(r[3], 1))])
    print(f"[data-slim Pro] 用量报告已导出: {path}")


def cmd_batch(args) -> None:
    """Pro 专属：批量瘦身整目录 + 导出用量报告（CSV/JSON）。

    相对免费版 auto（单线程、无导出），batch 支持并行处理（--workers）+ 汇总报告导出（--report）。
    入口经 require_pro() 授权校验（本地 Ed25519 验签，零联网）。
    每个文件仍走真实瘦身子命令（csv/json/text/ocr/pdf），结果计入账本；batch 仅负责并发编排与汇总。
    """
    tier = require_pro()
    if not os.path.isdir(args.dir):
        raise SystemExit(f"[data-slim Pro] 目录不存在: {args.dir}")
    targets = _collect_auto_targets(args.dir, True)  # batch 默认递归
    if not args.force:
        targets = [(fp, k) for fp, k in targets if not _already_slimmed(fp)]
    if not targets:
        print("[data-slim Pro] 批量处理: 无待处理文件（或已全部瘦身）。用 --force 强制重跑。")
        return
    print(f"[data-slim Pro] 批量处理({tier}): 发现 {len(targets)} 个文件, 并行度={args.workers}...")

    def run_one(fp_k):
        fp, kind = fp_k
        try:
            bt = token_estimate(read_text(fp)); bb = os.path.getsize(fp)
        except Exception:
            return (fp, kind, "skip", 0, 0, 0, 0)
        cmd = [sys.executable, os.path.abspath(__file__), _BATCH_SUB[kind], fp]
        if args.model:
            cmd += ["--model", args.model]
        r = subprocess.run(cmd, capture_output=True, text=True)
        out = _outpath(fp, None, _BATCH_SUFFIX[kind])
        try:
            at = token_estimate(read_text(out)); ab = os.path.getsize(out)
        except Exception:
            return (fp, kind, "fail", bt, bb, 0, 0)
        return (fp, kind, "ok", bt, bb, at, ab)

    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for res in ex.map(run_one, targets):
            rows.append(res)
            fp, kind, status, bt, bb, at, ab = res
            name = os.path.basename(fp)
            if status == "ok":
                print(f"  ✓ {name} [{kind}] 省 ~{bt - at:,} token, {_human(bb)}→{_human(ab)}")
            elif status == "fail":
                print(f"  ✗ {name} [{kind}] 处理失败")
            else:
                print(f"  - {name} [{kind}] 跳过(读取失败)")
    ok = [r for r in rows if r[2] == "ok"]
    total_saved = sum(r[3] - r[5] for r in ok)
    total_bytes = sum(r[4] - r[6] for r in ok)
    print(f"[data-slim Pro] 批量完成: {len(ok)} 成功 / {len(rows)} 总计 | 本次省 ~{total_saved:,} token | 省 {_human(total_bytes)}")
    if args.report:
        _export_batch_report(args.report, ok)


def main() -> None:
    p = argparse.ArgumentParser(description="data-slim 数据瘦身")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("csv")
    c.add_argument("file")
    c.add_argument("-o", "--out")
    c.add_argument("--keep", help="仅保留这些列, 逗号分隔")
    c.add_argument("--drop", help="删除这些列, 逗号分隔")
    c.add_argument("--head", type=int, help="仅保留前 N 行数据")
    c.add_argument("--keep-dup", action="store_true", help="不去重")
    c.add_argument("--dry-run", action="store_true", help="仅预览将执行的裁列/去重/截行, 不写文件")
    c.add_argument("--model", help="当前模型名(agent 自动注入, 用于记账/倍率)")
    c.set_defaults(func=cmd_csv)

    d = sub.add_parser("pdf")
    d.add_argument("file")
    d.add_argument("-o", "--out")
    d.add_argument("--dpi", type=int, default=300)
    d.add_argument("--thresh", type=int, default=175)
    d.add_argument("--model", help="当前模型名(agent 自动注入, 用于记账/倍率)")
    d.set_defaults(func=cmd_pdf)

    o = sub.add_parser("ocr", help="OCR 转曲/纯图像 PDF -> 文本层(真省上下文 token)")
    o.add_argument("file")
    o.add_argument("-o", "--out")
    o.add_argument("--dpi", type=int, default=300)
    o.add_argument("--lang", default="chi_sim+eng", help="tesseract 语言, 默认 chi_sim+eng")
    o.add_argument("--psm", type=int, default=3, help="页面分割模式(0-13), 默认3=全自动。密集多栏/报价单可试 4(单列变字号)/6(单均匀块); 稀疏小图用 11/12")
    o.add_argument("--oem", type=int, default=3, help="OCR 引擎模式(0=传统,1=LSTM,2=两者,3=自动/LSTM优先, 默认3)")
    o.add_argument("--model", help="当前模型名(agent 自动注入, 用于记账)")
    o.set_defaults(func=cmd_ocr)

    c = sub.add_parser("chat", help="对话瘦身: 对一段回复精简(去客套/压缩空白)并估算省 token")
    c.add_argument("file", help="AI 回复文本文件, 或 - 读 stdin")
    c.add_argument("-o", "--out", help="收束版输出路径")
    c.add_argument("--structural", action="store_true", help="在精简基础上加结论先行速览(规则启发式, 非LLM)")
    c.add_argument("--model", help="当前模型名(agent 自动注入, 用于记账)")
    c.set_defaults(func=cmd_chat)

    t = sub.add_parser("text")
    t.add_argument("file")
    t.add_argument("-o", "--out")
    t.add_argument("--max-chars", type=int, help="超过则头75%%+尾25%%截断")
    t.add_argument("--model", help="当前模型名(agent 自动注入, 用于记账/倍率)")
    t.set_defaults(func=cmd_text)

    j = sub.add_parser("json")
    j.add_argument("file")
    j.add_argument("-o", "--out")
    j.add_argument("--model", help="当前模型名(agent 自动注入, 用于记账/倍率)")
    j.set_defaults(func=cmd_json)

    e = sub.add_parser("estimate")
    e.add_argument("file")
    e.set_defaults(func=cmd_estimate)

    x = sub.add_parser("index")
    x.add_argument("dir")
    x.add_argument("-o", "--out")
    x.add_argument("--depth", type=int, default=2, help="目录展开层数, 默认2")
    x.add_argument("--files-per-dir", type=int, default=8, help="每目录最多列出文件名数, 默认8")
    x.add_argument("--collapse", type=int, default=20, help="子目录超过此数则折叠不展开, 默认20")
    x.set_defaults(func=cmd_index)

    dsh = sub.add_parser("dashboard", help="账本汇总(仅显示省 token, 不换算金额)")
    dsh.set_defaults(func=cmd_dashboard)

    a = sub.add_parser("auto", help="自动瘦身: 扫描用户指定路径下数据文件并自动调用对应子命令")
    a.add_argument("path", nargs="+", help="一个或多个文件/目录(用户一条消息可带多个附件, 全部自动处理)")
    a.add_argument("-r", "--recursive", action="store_true", help="递归扫描子目录")
    a.add_argument("--model", help="当前模型名(agent 自动注入)")
    a.set_defaults(func=cmd_auto)

    r = sub.add_parser("record", help="L4 语义压缩记账: 记录 agent 生成的收束版省 token(仅 token, 不折算金额)")
    r.add_argument("--in", dest="infile", required=True, help="原文件路径")
    r.add_argument("--out", dest="outfile", required=True, help="收束版 .slim.md 路径")
    r.add_argument("--model", help="当前模型名(agent 自动注入)")
    r.set_defaults(func=cmd_record)

    pj = sub.add_parser("pro", help="验证 License Key 并查看等级/有效期（Pro 规划见 SKILL.md）")
    pj.add_argument("--key", help="License Key（缺省读环境变量 DATA_SLIM_LICENSE）")
    pj.set_defaults(func=cmd_pro)

    b = sub.add_parser("batch", help="[Pro] 批量瘦身整目录 + 导出用量报告(CSV/JSON)")
    b.add_argument("dir", help="待批处理的目录")
    b.add_argument("--workers", type=int, default=4, help="并行度, 默认4")
    b.add_argument("--force", action="store_true", help="强制重跑已瘦身的文件")
    b.add_argument("--report", help="导出用量报告到 .json 或 .csv 路径(Pro)")
    b.add_argument("--model", help="当前模型名(agent 自动注入, 用于记账)")
    b.set_defaults(func=cmd_batch)

    cfg = sub.add_parser("config", help="设置默认模型(金额换算已停用)")
    cfg.add_argument("action", choices=["set", "get"])
    cfg.add_argument("key", help="price | model")
    cfg.add_argument("value", type=str, nargs="?", help="set model 时填模型名称")
    cfg.set_defaults(func=cmd_config)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
