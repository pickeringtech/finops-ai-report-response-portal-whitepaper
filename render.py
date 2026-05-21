#!/usr/bin/env python3
"""Render a markdown whitepaper to polished HTML and PDF.

The script intentionally uses only the Python standard library plus a local
Chromium binary for PDF printing. It supports the markdown constructs used by
this repository: headings, paragraphs, emphasis, links, inline code, fenced
code blocks, lists, and GitHub-style tables.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import pathname2url


SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class Heading:
    level: int
    text: str
    slug: str


def slugify(text: str, used: dict[str, int]) -> str:
    base = SLUG_RE.sub("-", text.lower()).strip("-") or "section"
    count = used.get(base, 0)
    used[base] = count + 1
    return base if count == 0 else f"{base}-{count + 1}"


def strip_inline_markdown(text: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text.strip()


def inline_markdown(text: str) -> str:
    placeholders: list[str] = []

    def stash(value: str) -> str:
        placeholders.append(value)
        return f"\u0000{len(placeholders) - 1}\u0000"

    def code_repl(match: re.Match[str]) -> str:
        return stash(f"<code>{html.escape(match.group(1))}</code>")

    text = re.sub(r"`([^`]*)`", code_repl, text)
    escaped = html.escape(text)

    def link_repl(match: re.Match[str]) -> str:
        label = match.group(1)
        href = html.escape(match.group(2), quote=True)
        return f'<a href="{href}">{label}</a>'

    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link_repl, escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)

    def restore(match: re.Match[str]) -> str:
        return placeholders[int(match.group(1))]

    return re.sub("\u0000([0-9]+)\u0000", restore, escaped)


def split_table_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def is_table_separator(line: str) -> bool:
    cells = split_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def parse_alignments(separator: str) -> list[str]:
    alignments = []
    for cell in split_table_row(separator):
        left = cell.startswith(":")
        right = cell.endswith(":")
        if left and right:
            alignments.append("center")
        elif right:
            alignments.append("right")
        else:
            alignments.append("left")
    return alignments


def render_table(lines: list[str]) -> str:
    headers = split_table_row(lines[0])
    alignments = parse_alignments(lines[1])
    body = [split_table_row(line) for line in lines[2:]]

    out = ['<div class="table-wrap"><table>']
    out.append("<thead><tr>")
    for index, header in enumerate(headers):
        align = alignments[index] if index < len(alignments) else "left"
        out.append(f'<th class="align-{align}">{inline_markdown(header)}</th>')
    out.append("</tr></thead>")
    out.append("<tbody>")
    for row in body:
        out.append("<tr>")
        for index, cell in enumerate(row):
            align = alignments[index] if index < len(alignments) else "left"
            out.append(f'<td class="align-{align}">{inline_markdown(cell)}</td>')
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "\n".join(out)


def parse_markdown(markdown: str) -> tuple[str, list[Heading], str]:
    lines = markdown.splitlines()
    used_slugs: dict[str, int] = {}
    headings: list[Heading] = []
    blocks: list[str] = []
    paragraph: list[str] = []
    list_stack: list[str] = []

    def close_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            blocks.append(f"<p>{inline_markdown(' '.join(paragraph))}</p>")
            paragraph = []

    def close_lists(to_level: int = 0) -> None:
        while len(list_stack) > to_level:
            blocks.append(f"</{list_stack.pop()}>")

    i = 0
    title = "Untitled Document"

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            close_paragraph()
            close_lists()
            language = stripped[3:].strip() or "text"
            i += 1
            code_lines = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            blocks.append(
                '<figure class="code-block">'
                f'<figcaption>{html.escape(language)}</figcaption>'
                f'<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>'
                "</figure>"
            )
            i += 1
            continue

        if not stripped:
            close_paragraph()
            close_lists()
            i += 1
            continue

        if re.match(r"^\|.*\|$", stripped) and i + 1 < len(lines) and is_table_separator(lines[i + 1]):
            close_paragraph()
            close_lists()
            table_lines = [lines[i], lines[i + 1]]
            i += 2
            while i < len(lines) and re.match(r"^\|.*\|$", lines[i].strip()):
                table_lines.append(lines[i])
                i += 1
            blocks.append(render_table(table_lines))
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading_match:
            close_paragraph()
            close_lists()
            level = len(heading_match.group(1))
            text = strip_inline_markdown(heading_match.group(2))
            slug = slugify(text, used_slugs)
            if level == 1:
                title = text
            headings.append(Heading(level, text, slug))
            blocks.append(f'<h{level} id="{slug}">{inline_markdown(heading_match.group(2))}</h{level}>')
            i += 1
            continue

        list_match = re.match(r"^(\s*)([-*+]|\d+[.])\s+(.+)$", line)
        if list_match:
            close_paragraph()
            indent = len(list_match.group(1).replace("\t", "    "))
            target_level = indent // 2 + 1
            list_type = "ol" if re.match(r"\d+[.]", list_match.group(2)) else "ul"
            close_lists(max(0, target_level - 1))
            if len(list_stack) < target_level:
                blocks.append(f"<{list_type}>")
                list_stack.append(list_type)
            elif list_stack[-1] != list_type:
                close_lists(len(list_stack) - 1)
                blocks.append(f"<{list_type}>")
                list_stack.append(list_type)
            blocks.append(f"<li>{inline_markdown(list_match.group(3).strip())}</li>")
            i += 1
            continue

        close_lists()
        paragraph.append(stripped)
        i += 1

    close_paragraph()
    close_lists()
    return "\n".join(blocks), headings, title


def build_toc(headings: list[Heading]) -> str:
    visible = [heading for heading in headings if 2 <= heading.level <= 3]
    if not visible:
        return ""
    items = []
    for heading in visible:
        class_name = "toc-subitem" if heading.level == 3 else "toc-item"
        items.append(f'<li class="{class_name}"><a href="#{heading.slug}">{html.escape(heading.text)}</a></li>')
    return '<nav class="toc" aria-label="Table of contents"><h2>Contents</h2><ol>' + "\n".join(items) + "</ol></nav>"


def stylesheet() -> str:
    return r"""
:root {
  color-scheme: light;
  --ink: #17202a;
  --muted: #5b6574;
  --line: #d9e0e8;
  --soft: #f5f7f8;
  --panel: #ffffff;
  --brand: #165c7d;
  --brand-dark: #0d3448;
  --accent: #e4572e;
  --accent-strong: #b83218;
  --accent-soft: #fff0e8;
  --accent-line: #f2b199;
  --code-bg: #132330;
  --code-line: #304252;
  --shadow: 0 22px 70px rgba(25, 38, 52, 0.11);
}

* { box-sizing: border-box; }

html {
  scroll-behavior: smooth;
  font-size: 16px;
}

body {
  margin: 0;
  background:
    linear-gradient(90deg, rgba(22, 92, 125, 0.055) 0, transparent 28rem),
    linear-gradient(180deg, #f7f9fb 0, #eef3f5 38rem, #f7f9fb 100%);
  color: var(--ink);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.62;
}

.page {
  width: min(1180px, calc(100% - 44px));
  margin: 32px auto 56px;
  background: var(--panel);
  border: 1px solid rgba(23, 32, 42, 0.08);
  box-shadow: var(--shadow);
}

.cover {
  position: relative;
  min-height: 360px;
  padding: 62px 72px 54px;
  overflow: hidden;
  color: #fff;
  background:
    linear-gradient(135deg, rgba(13, 52, 72, 0.97), rgba(22, 92, 125, 0.91) 56%, rgba(228, 87, 46, 0.86)),
    repeating-linear-gradient(90deg, transparent 0 56px, rgba(255, 255, 255, 0.05) 56px 57px);
}

.cover::after {
  content: "";
  position: absolute;
  right: -120px;
  top: -130px;
  width: 430px;
  height: 430px;
  border: 1px solid rgba(255, 214, 194, 0.34);
  transform: rotate(25deg);
}

.eyebrow {
  margin: 0 0 26px;
  color: rgba(255,255,255,0.78);
  font-size: 0.78rem;
  font-weight: 720;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

.eyebrow::before {
  content: "";
  display: inline-block;
  width: 38px;
  height: 3px;
  margin-right: 12px;
  vertical-align: middle;
  background: #ff8a3d;
}

.cover h1 {
  position: relative;
  max-width: 880px;
  margin: 0;
  color: #fff;
  font-size: clamp(2.5rem, 6vw, 4.9rem);
  line-height: 0.95;
  letter-spacing: 0;
}

.layout {
  display: grid;
  grid-template-columns: 270px minmax(0, 1fr);
  gap: 42px;
  padding: 42px 58px 64px;
}

.toc {
  align-self: start;
  position: sticky;
  top: 24px;
  padding-right: 22px;
  border-right: 1px solid var(--line);
}

.toc h2 {
  margin: 0 0 14px;
  color: var(--accent-strong);
  font-size: 0.82rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.toc ol {
  margin: 0;
  padding: 0;
  list-style: none;
}

.toc li {
  margin: 0;
}

.toc a {
  display: block;
  padding: 5px 0;
  color: var(--muted);
  font-size: 0.88rem;
  line-height: 1.35;
  text-decoration: none;
}

.toc .toc-subitem a {
  padding-left: 13px;
  font-size: 0.82rem;
}

.toc a:hover { color: var(--accent-strong); }

main {
  min-width: 0;
}

main > h1 {
  display: none;
}

h1, h2, h3, h4, h5, h6 {
  color: var(--brand-dark);
  line-height: 1.18;
  letter-spacing: 0;
  break-after: avoid;
}

h2 {
  margin: 2.3rem 0 0.85rem;
  padding-top: 0.95rem;
  border-top: 1px solid var(--accent-line);
  font-size: 1.75rem;
}

h2:first-child {
  margin-top: 0;
  border-top: 0;
  padding-top: 0;
}

h3 {
  margin: 1.8rem 0 0.65rem;
  font-size: 1.22rem;
}

h4 {
  margin: 1.4rem 0 0.5rem;
  font-size: 1.03rem;
}

p {
  margin: 0 0 1rem;
}

a {
  color: var(--accent-strong);
  text-underline-offset: 0.17em;
}

strong {
  color: #101820;
  font-weight: 720;
}

ul, ol {
  margin: 0.25rem 0 1.15rem 1.45rem;
  padding: 0;
}

li {
  margin: 0.22rem 0;
  padding-left: 0.2rem;
}

code {
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  font-size: 0.88em;
  color: var(--accent-strong);
  background: var(--accent-soft);
  border: 1px solid var(--accent-line);
  padding: 0.08rem 0.28rem;
  border-radius: 4px;
}

.code-block {
  margin: 1.35rem 0;
  overflow: hidden;
  background: var(--code-bg);
  border: 1px solid var(--code-line);
  box-shadow: 0 10px 28px rgba(19, 35, 48, 0.12);
  break-inside: avoid;
}

.code-block figcaption {
  padding: 8px 14px;
  color: #b9c7d4;
  background: rgba(255,255,255,0.045);
  border-bottom: 1px solid var(--code-line);
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

pre {
  margin: 0;
  padding: 17px 18px;
  overflow-x: auto;
  color: #eef7fb;
  font-size: 0.84rem;
  line-height: 1.55;
  white-space: pre-wrap;
}

pre code {
  color: inherit;
  background: transparent;
  border: 0;
  padding: 0;
}

.table-wrap {
  width: 100%;
  margin: 1.2rem 0 1.55rem;
  overflow-x: auto;
  border: 1px solid var(--line);
  box-shadow: 0 8px 24px rgba(25, 38, 52, 0.06);
}

table {
  width: 100%;
  min-width: 620px;
  border-collapse: collapse;
  font-size: 0.88rem;
  line-height: 1.42;
}

thead {
  display: table-header-group;
}

th {
  background: linear-gradient(180deg, #fff4ee, #f7e2d8);
  color: var(--brand-dark);
  font-weight: 760;
  text-align: left;
}

th, td {
  padding: 10px 12px;
  vertical-align: top;
  border-bottom: 1px solid var(--line);
  border-right: 1px solid var(--line);
}

tr:last-child td {
  border-bottom: 0;
}

th:last-child, td:last-child {
  border-right: 0;
}

tbody tr:nth-child(even) {
  background: var(--soft);
}

.align-right { text-align: right; }
.align-center { text-align: center; }

.footer {
  display: flex;
  justify-content: space-between;
  gap: 20px;
  padding: 20px 58px;
  color: var(--brand-dark);
  background: linear-gradient(90deg, #fff0e8, #eef5f7);
  border-top: 2px solid var(--accent-line);
  font-size: 0.82rem;
}

@media (max-width: 900px) {
  .page {
    width: min(100%, calc(100% - 20px));
    margin-top: 10px;
  }

  .cover {
    min-height: 300px;
    padding: 42px 28px;
  }

  .layout {
    display: block;
    padding: 28px 24px 42px;
  }

  .toc {
    position: static;
    margin: 0 0 30px;
    padding: 0 0 20px;
    border-right: 0;
    border-bottom: 1px solid var(--line);
  }

  .footer {
    display: block;
    padding: 18px 24px;
  }

  table {
    min-width: 560px;
  }
}

@page {
  size: A4;
  margin: 16mm 15mm 18mm;
}

@media print {
  html {
    font-size: 13px;
  }

  body {
    background: #fff;
  }

  .page {
    width: auto;
    margin: 0;
    border: 0;
    box-shadow: none;
  }

  .cover {
    min-height: 255mm;
    padding: 45mm 24mm 28mm;
    break-after: page;
    print-color-adjust: exact;
    -webkit-print-color-adjust: exact;
  }

  .cover h1 {
    font-size: 48pt;
  }

  .layout {
    display: block;
    padding: 0;
  }

  .toc {
    position: static;
    break-after: page;
    padding: 0;
    border: 0;
  }

  .toc h2 {
    font-size: 16pt;
  }

  .toc a {
    color: var(--ink);
  }

  h2 {
    font-size: 20pt;
    margin-top: 1.35rem;
    break-after: avoid;
  }

  h3 {
    font-size: 14pt;
  }

  p, li, table, pre {
    break-inside: avoid;
  }

  .table-wrap {
    overflow: visible;
    box-shadow: none;
  }

  table {
    min-width: 0;
    font-size: 8.8pt;
  }

  th, td {
    padding: 6px 7px;
  }

  pre {
    white-space: pre-wrap;
    font-size: 8.6pt;
  }

  .footer {
    display: none;
  }
}
"""


def build_html(markdown: str, source: Path) -> str:
    body, headings, title = parse_markdown(markdown)
    toc = build_toc(headings)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>{stylesheet()}</style>
</head>
<body>
  <article class="page">
    <header class="cover">
      <p class="eyebrow">Whitepaper</p>
      <h1>{html.escape(title)}</h1>
    </header>
    <div class="layout">
      {toc}
      <main>
        {body}
      </main>
    </div>
    <footer class="footer">
      <span>{html.escape(title)}</span>
    </footer>
  </article>
</body>
</html>
"""


def render_pdf(html_path: Path, pdf_path: Path, chromium: str) -> None:
    url = urljoin("file:", pathname2url(str(html_path.resolve())))
    raw_pdf_path = pdf_path.with_suffix(f"{pdf_path.suffix}.raw")
    command = [
        chromium,
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-crash-reporter",
        "--disable-crashpad",
        "--disable-dev-shm-usage",
        "--run-all-compositor-stages-before-draw",
        f"--print-to-pdf={raw_pdf_path}",
        "--no-pdf-header-footer",
        "--print-to-pdf-no-header",
        url,
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "Chromium PDF export failed."
        raise RuntimeError(message)
    scrub_pdf_metadata(raw_pdf_path, pdf_path)
    raw_pdf_path.unlink(missing_ok=True)


def scrub_pdf_metadata(source_pdf: Path, target_pdf: Path) -> None:
    qpdf = shutil.which("qpdf")
    if not qpdf:
        source_pdf.replace(target_pdf)
        return

    command = [
        qpdf,
        "--remove-info",
        "--remove-metadata",
        str(source_pdf),
        str(target_pdf),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "qpdf metadata removal failed."
        raise RuntimeError(message)
    remove_pdf_date_entries(target_pdf)


def remove_pdf_date_entries(pdf_path: Path) -> None:
    data = pdf_path.read_bytes()

    def blank(match: re.Match[bytes]) -> bytes:
        return b" " * len(match.group(0))

    data = re.sub(rb"/(?:CreationDate|ModDate)\s*\([^)]*\)", blank, data)
    pdf_path.write_bytes(data)


def find_chromium(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render a markdown whitepaper to HTML and PDF.")
    parser.add_argument("source", nargs="?", default="adviser.md", help="Markdown source file.")
    parser.add_argument("-o", "--out-dir", default="dist", help="Output directory.")
    parser.add_argument("--html-name", help="HTML output filename. Defaults to the source stem.")
    parser.add_argument("--pdf-name", help="PDF output filename. Defaults to the source stem.")
    parser.add_argument("--no-pdf", action="store_true", help="Only render HTML.")
    parser.add_argument("--chromium", help="Path to Chromium/Chrome binary for PDF export.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = Path(args.source)
    if not source.exists():
        print(f"Source file not found: {source}", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = source.stem
    html_path = out_dir / (args.html_name or f"{stem}.html")
    pdf_path = out_dir / (args.pdf_name or f"{stem}.pdf")

    markdown = source.read_text(encoding="utf-8")
    html_document = build_html(markdown, source)
    html_path.write_text(html_document, encoding="utf-8")
    print(f"Wrote HTML: {html_path}")

    if args.no_pdf:
        return 0

    chromium = find_chromium(args.chromium)
    if not chromium:
        print("Chromium/Chrome was not found; HTML was rendered but PDF was skipped.", file=sys.stderr)
        return 2

    try:
        render_pdf(html_path, pdf_path, chromium)
    except RuntimeError as exc:
        print(f"PDF export failed: {exc}", file=sys.stderr)
        return 3

    size_mb = pdf_path.stat().st_size / (1024 * 1024)
    print(f"Wrote PDF: {pdf_path} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
