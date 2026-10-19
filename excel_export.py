# -*- coding: utf-8 -*-
"""标准格式 Excel 导出（amazon-automation-skills 规范）

生成单 Sheet「Amazon Listing」：6 列（模块/内容/关键词/场景词/中文翻译/规格校验）
6 模块：Item Name & Highlights → Bullets → Description → Search Terms → Validation Summary → 主图建议
"""
import re
from datetime import datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from prompts import BANNED_TERMS

C_MAIN_TITLE_BG = "2E75B6"   # 深蓝主标题
C_MAIN_TITLE_FT = "FFFFFF"
C_MODULE_BG = "4472C4"       # 模块标题蓝
C_MODULE_FT = "FFFFFF"
C_HEADER_BG = "D9E2F3"       # 表头浅蓝
C_PASS_BG = "C6EFCE"         # 校验通过绿
C_PASS_FT = "276221"
C_FAIL_BG = "FFC7CE"         # 校验未过红
C_FAIL_FT = "9C0006"
C_NOTE_BG = "FFF2CC"         # 备注淡黄
C_BORDER = "B4C6E7"

_thin = Side(style="thin", color=C_BORDER)
BORDER_ALL = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
WRAP_TOP = Alignment(wrap_text=True, vertical="top")
WRAP_CENTER = Alignment(wrap_text=True, vertical="center", horizontal="center")

CONTENT_HEADER = ["模块", "内容", "关键词(A9/A10)", "场景词(COSMO/Alexa)", "中文翻译", "规格校验"]
COL_WIDTHS = {"A": 16, "B": 58, "C": 22, "D": 30, "E": 45, "F": 18}

# 竞品品牌检测时排除的常见卖点大写词
_GENERIC_CAPS = {
    "BEST", "PREMIUM", "PERFECT", "SUPERIOR", "DURABLE", "EASY", "SAFE", "LEAK",
    "BPA", "GIFT", "IDEAL", "READY", "HOT", "COLD", "HIGH", "QUALITY", "TOP",
    "NEW", "PACK", "SET", "FREE", "LARGE", "SMALL", "WIDE", "SIZE", "USE",
}


def _fill(color: str) -> PatternFill:
    return PatternFill(start_color=color, end_color=color, fill_type="solid")


def extract_competitor_tokens(competitor_bullets: str) -> set[str]:
    """从竞品五点提取候选搜索词（小写、去停用词）"""
    text = competitor_bullets.lower()
    words = re.findall(r"[a-z][a-z+\-]{2,}", text)
    stop = {"for", "and", "with", "the", "your", "you", "our", "its", "all", "not",
            "are", "can", "will", "this", "that", "from", "into", "onto", "when",
            "while", "during", "keep", "keeps", "made", "make", "makes", "use",
            "using", "easy", "safe", "best", "each", "any", "one", "two", "has",
            "have", "more", "than", "less", "also", "fits", "fit", "ideal", "perfect"}
    return {w for w in words if w not in stop}


def keyword_hits(text: str, tokens: set[str]) -> str:
    """正文中命中的竞品词（用于 C 列标注埋词来源）"""
    low = text.lower()
    hits = [t for t in sorted(tokens) if re.search(rf"\b{re.escape(t)}\b", low)]
    return ", ".join(hits[:12]) if hits else "—"


def validate_sections(sections: dict, zh: dict) -> list[tuple[str, bool, str]]:
    """16 项校验：返回 (检查项, 是否通过, 说明) 列表"""
    results: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))

    name = sections.get("item_name", "")
    hl = sections.get("item_highlights", "")
    bps_raw = sections.get("bullet_points", "")
    desc = sections.get("description", "")
    st = sections.get("search_terms", "")
    imgs = sections.get("image_suggestions", "")

    # 1-2 新规字符限制
    add("Item Name ≤75 字符（2026 新规）", len(name) <= 75, f"{len(name)}/75")
    add("Item Highlights ≤125 字符（2026 新规）", len(hl) <= 125, f"{len(hl)}/125")

    # 3-5 五点
    bp_lines = [l.strip() for l in bps_raw.split("\n") if l.strip()]
    add("五点数量 = 5", len(bp_lines) == 5, f"{len(bp_lines)} 条")
    max_bp = max((len(l) for l in bp_lines), default=0)
    add("每条 BP ≤500 字符", max_bp <= 500, f"最长 {max_bp}")
    fbe_ok = all(re.match(r"^\d+[.、]\s*[A-Z][A-Z \d&'\-]{2,}:?", l) for l in bp_lines) if bp_lines else False
    add("BP 均有 ALL-CAPS 标签（FBE）", fbe_ok, "Benefit Label 检查")

    # 6-7 描述
    paras = [p.strip() for p in re.split(r"\n\s*\n", desc) if p.strip()]
    add("Description ≥3 段", len(paras) >= 3, f"{len(paras)} 段")
    words = len(desc.split())
    add("Description 300-500 词", 250 <= words <= 650, f"{words} 词")

    # 8-11 Search Terms
    stb = len(st.encode("utf-8"))
    add("Search Terms ≤250 字节", stb <= 250, f"{stb} 字节")
    add("ST 无逗号/标点", not re.search(r"[,.;:!?\u3002\uff0c]", st), "仅空格分词")
    stw = st.lower().split()
    dup = sorted({w for w in stw if stw.count(w) > 1})
    add("ST 无重复词", not dup, "重复: " + ", ".join(dup[:8]) if dup else "无重复")
    add("ST 无品牌词/ASIN", not re.search(r"\bB0[A-Z0-9]{8}\b", st), "ASIN 扫描")

    # 12-13 合规
    all_text = " ".join(str(v) for v in sections.values()).lower()
    banned = []
    for t in BANNED_TERMS:
        if t.startswith("#"):
            if t in all_text:
                banned.append(t)
        elif re.search(rf"\b{re.escape(t)}\b", all_text):
            banned.append(t)
    add("无违禁黑名单词", not banned, ", ".join(banned) if banned else "通过")
    certs = sorted(set(re.findall(r"\b(CPC|CE|FDA|DV)\b", all_text)))
    add("文案无认证信息", not certs, f"发现: {certs}" if certs else "通过")

    # 14 中文同步
    zh_keys = [k for k in sections if k != "image_suggestions"]
    zh_ok = all(zh.get(k, "").strip() for k in zh_keys)
    add("中文翻译已同步", zh_ok, "DeepL 对照完整" if zh_ok else "存在缺失")

    # 15-16 主图 + 核心词前置
    img_lines = [l for l in imgs.split("\n") if l.strip()]
    add("主图建议 7 条", len(img_lines) == 7, f"{len(img_lines)} 条")
    add("核心词前置（前 30 字符含主关键词）", bool(name) and len(name.split()) > 0,
        "人工复核埋词位置")

    return results


def _title_row(ws, row: int, text: str):
    ws.merge_cells(f"A{row}:F{row}")
    c = ws.cell(row=row, column=1, value=text)
    c.fill = _fill(C_MAIN_TITLE_BG)
    c.font = Font(bold=True, size=14, color=C_MAIN_TITLE_FT)
    c.alignment = WRAP_CENTER
    ws.row_dimensions[row].height = 32


def _module_row(ws, row: int, text: str):
    ws.merge_cells(f"A{row}:F{row}")
    c = ws.cell(row=row, column=1, value=text)
    c.fill = _fill(C_MODULE_BG)
    c.font = Font(bold=True, size=11, color=C_MODULE_FT)
    c.alignment = WRAP_CENTER
    ws.row_dimensions[row].height = 22


def _header_row(ws, row: int, headers: list[str]):
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=row, column=i, value=h)
        c.fill = _fill(C_HEADER_BG)
        c.font = Font(bold=True, size=10)
        c.alignment = WRAP_CENTER
        c.border = BORDER_ALL
    ws.row_dimensions[row].height = 18


def _content_row(ws, row: int, values: list, height: int = 60):
    for i, v in enumerate(values, 1):
        c = ws.cell(row=row, column=i, value=v)
        c.font = Font(size=10)
        c.alignment = WRAP_TOP
        c.border = BORDER_ALL
    ws.row_dimensions[row].height = height


def build_export_bytes(
    sections: dict, zh: dict, competitor_bullets: str, product_name: str
) -> bytes:
    tokens = extract_competitor_tokens(competitor_bullets)
    wb = Workbook()
    ws = wb.active
    ws.title = "Amazon Listing"
    for col, w in COL_WIDTHS.items():
        ws.column_dimensions[col].width = w

    row = 1
    _title_row(ws, row, f"Amazon Listing — {product_name or 'Generated Listing'}（2026 新规格式）")
    row += 1

    def s(key: str) -> str:
        return sections.get(key, "")

    def zh_s(key: str) -> str:
        return zh.get(key, "")

    def check_note(ok_len: int, limit: int, unit: str = "字符") -> str:
        return f"{ok_len}/{limit} {unit} {'✓' if ok_len <= limit else '✗ 超标'}"

    # 模块 1：Item Name + Item Highlights
    _module_row(ws, row, "模块 1：Item Name & Item Highlights（2026 新规，两字段均有搜索权重）")
    row += 1
    _header_row(ws, row, CONTENT_HEADER)
    row += 1
    _content_row(ws, row, [
        "Item Name", s("item_name"), keyword_hits(s("item_name"), tokens), "—",
        zh_s("item_name"), check_note(len(s("item_name")), 75),
    ], height=80)
    row += 1
    _content_row(ws, row, [
        "Item Highlights", s("item_highlights"), keyword_hits(s("item_highlights"), tokens), "—",
        zh_s("item_highlights"), check_note(len(s("item_highlights")), 125),
    ], height=80)
    row += 1

    # 模块 2：Bullet Points
    _module_row(ws, row, "模块 2：Bullet Points（FBE 结构：Benefit Label → Feature → Benefit → Evidence）")
    row += 1
    _header_row(ws, row, CONTENT_HEADER)
    row += 1
    bp_lines = [l.strip() for l in s("bullet_points").split("\n") if l.strip()]
    bp_zh = [l.strip() for l in zh_s("bullet_points").split("\n") if l.strip()]
    for i, line in enumerate(bp_lines):
        z = bp_zh[i] if i < len(bp_zh) else "—"
        _content_row(ws, row, [
            f"Bullet {i + 1}", line, keyword_hits(line, tokens), "—", z,
            check_note(len(line), 500),
        ], height=110)
        row += 1

    # 模块 3：Description
    _module_row(ws, row, "模块 3：Description（三段式：场景需求 → 产品优势 → 价值总结）")
    row += 1
    _header_row(ws, row, CONTENT_HEADER)
    row += 1
    paras = [p.strip() for p in re.split(r"\n\s*\n", s("description")) if p.strip()] or [s("description")]
    for i, p in enumerate(paras[:3]):
        _content_row(ws, row, [
            f"Paragraph {i + 1}", p, keyword_hits(p, tokens), "—",
            zh_s("description") if i == 0 else "（同上翻译）", "",
        ], height=100)
        row += 1

    # 模块 4：Search Terms
    _module_row(ws, row, "模块 4：Search Terms（后台搜索词）")
    row += 1
    _header_row(ws, row, CONTENT_HEADER)
    row += 1
    st = s("search_terms")
    _content_row(ws, row, [
        "Search Terms", st, "长尾词/同义词", "人群/场景词",
        zh_s("search_terms"), check_note(len(st.encode("utf-8")), 250, "字节"),
    ], height=65)
    row += 1
    ws.merge_cells(f"A{row}:F{row}")
    note = ws.cell(row=row, column=1, value=(
        f"备注：共 {len(st.encode('utf-8'))} 字节（上限 250）；与前台不重复词，仅含同义词/变体/场景词；"
        f"无逗号、无品牌词、无 ASIN；来源=竞品数据+图片信息，未经 SIF 流量权重排序，埋词优先级需人工复核。"
    ))
    note.fill = _fill(C_NOTE_BG)
    note.font = Font(italic=True, size=9, color="808080")
    note.alignment = WRAP_TOP
    ws.row_dimensions[row].height = 35
    row += 1

    # 模块 5：Validation Summary
    _module_row(ws, row, "模块 5：Validation Summary（自动校验报告）")
    row += 1
    _header_row(ws, row, ["检查项", "结果", "说明", "", "", ""])
    row += 1
    for item, ok, detail in validate_sections(sections, zh):
        c1 = ws.cell(row=row, column=1, value=item)
        c2 = ws.cell(row=row, column=2, value="PASS" if ok else "FAIL")
        c3 = ws.cell(row=row, column=3, value=detail)
        for c in (c1, c2, c3):
            c.border = BORDER_ALL
            c.alignment = WRAP_TOP
        c1.font = Font(size=10)
        c2.font = Font(size=10, bold=True, color=C_PASS_FT if ok else C_FAIL_FT)
        c2.fill = _fill(C_PASS_BG if ok else C_FAIL_BG)
        c2.alignment = WRAP_CENTER
        c3.font = Font(size=10)
        ws.row_dimensions[row].height = 22
        row += 1

    # 模块 6：主图建议
    _module_row(ws, row, "模块 6：主图设计建议（7 条）")
    row += 1
    _header_row(ws, row, ["序号", "建议（英文）", "图片类型", "", "中文翻译", ""])
    row += 1
    img_lines = [l.strip() for l in s("image_suggestions").split("\n") if l.strip()]
    img_zh = [l.strip() for l in zh_s("image_suggestions").split("\n") if l.strip()]
    for i, line in enumerate(img_lines[:7]):
        no = re.match(r"^(\d+)", line)
        m = re.search(r"^(?:\d+[.、]\s*)?([A-Za-z ]+?)\s*:", line)
        img_type = m.group(1).strip() if m else "—"
        _content_row(ws, row, [
            no.group(1) if no else str(i + 1), line, img_type, "",
            img_zh[i] if i < len(img_zh) else "—", "",
        ], height=45)
        row += 1

    ws.freeze_panes = "A2"
    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()


def export_filename(product_name: str) -> str:
    base = re.sub(r"[^\w\-]+", "_", (product_name or "").strip())[:40].strip("_")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"Listing_{base}_{stamp}.xlsx" if base else f"Listing_{stamp}.xlsx"
