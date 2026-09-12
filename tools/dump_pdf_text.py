"""dump_pdf_text.py — 调试辅助：按 (y, x) 全局排序的阅读顺序导出 PDF 文本行。

适用于 306 西综真题版式：整页通栏 + 选项 2x2 网格（A 左上 B 右上 C 左下 D 右下）。
用法: python tools/dump_pdf_text.py <pdf> <起始页> <结束页>
"""
import sys

import fitz


def page_reading_lines(page: fitz.Page) -> list[str]:
    items: list[tuple[float, float, str]] = []
    for b in page.get_text("blocks"):
        if b[6] != 0:  # 仅文本块
            continue
        for ln in b[4].splitlines():
            ln = ln.strip()
            if ln:
                items.append((b[1], b[0], ln))
    items.sort(key=lambda t: (t[0], t[1]))
    return [ln for _, _, ln in items]


def main() -> None:
    pdf, p0, p1 = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    doc = fitz.open(pdf)
    for pno in range(p0 - 1, min(p1, doc.page_count)):
        print(f"\n===== PAGE {pno + 1} =====")
        for ln in page_reading_lines(doc[pno]):
            print(ln)


if __name__ == "__main__":
    main()
