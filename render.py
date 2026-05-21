#!/usr/bin/env python3
"""Render a markdown whitepaper to polished HTML and PDF.

The script intentionally uses only the Python standard library plus a local
Chromium binary for PDF printing. It supports the markdown constructs used by
this repository: headings, paragraphs, emphasis, links, inline code, fenced
code blocks, lists, and GitHub-style tables.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
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


@dataclass
class MermaidNode:
    node_id: str
    label: str


@dataclass
class MermaidEdge:
    source: str
    target: str
    label: str = ""


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


def highlight_code(code: str, language: str) -> str:
    normalized = language.lower()
    if normalized == "json":
        return highlight_json(code)
    if normalized in {"gherkin", "feature"}:
        return highlight_gherkin(code)
    return html.escape(code)


def highlight_json(code: str) -> str:
    token_re = re.compile(
        r'(?P<string>"(?:\\.|[^"\\])*")'
        r"|(?P<number>-?\b(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?\b)"
        r"|(?P<literal>\b(?:true|false|null)\b)"
        r"|(?P<punct>[{}\[\],:])"
    )
    parts: list[str] = []
    last = 0

    for match in token_re.finditer(code):
        parts.append(html.escape(code[last : match.start()]))
        token = match.group(0)
        if match.lastgroup == "string":
            after = code[match.end() :]
            class_name = "syntax-key" if re.match(r"\s*:", after) else "syntax-string"
        elif match.lastgroup == "number":
            class_name = "syntax-number"
        elif match.lastgroup == "literal":
            class_name = "syntax-literal"
        else:
            class_name = "syntax-punctuation"
        parts.append(f'<span class="{class_name}">{html.escape(token)}</span>')
        last = match.end()

    parts.append(html.escape(code[last:]))
    return "".join(parts)


def highlight_gherkin(code: str) -> str:
    keywords = (
        "Feature",
        "Rule",
        "Background",
        "Scenario Outline",
        "Scenario",
        "Examples",
        "Given",
        "When",
        "Then",
        "And",
        "But",
        "User",
    )
    keyword_re = re.compile(rf"^(\s*)({'|'.join(re.escape(keyword) for keyword in keywords)})(\b|:)", re.IGNORECASE)
    lines: list[str] = []

    for line in code.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            leading = line[: len(line) - len(stripped)]
            lines.append(f'{html.escape(leading)}<span class="syntax-comment">{html.escape(stripped)}</span>')
            continue
        if stripped.startswith("@"):
            lines.append(highlight_gherkin_tags(line))
            continue
        if stripped.startswith("|"):
            lines.append(highlight_gherkin_table(line))
            continue

        escaped = html.escape(line)
        match = keyword_re.match(line)
        if match:
            leading, keyword, suffix = match.groups()
            start = len(leading)
            end = start + len(keyword)
            escaped = (
                html.escape(line[:start])
                + f'<span class="syntax-gherkin-keyword">{html.escape(line[start:end])}</span>'
                + html.escape(line[end:])
            )
        escaped = highlight_gherkin_strings(escaped)
        lines.append(escaped)

    return "\n".join(lines)


def highlight_gherkin_tags(line: str) -> str:
    escaped = html.escape(line)
    return re.sub(r"(@[\w:-]+)", r'<span class="syntax-tag">\1</span>', escaped)


def highlight_gherkin_table(line: str) -> str:
    escaped = html.escape(line)
    return re.sub(r"(\|)", r'<span class="syntax-punctuation">\1</span>', escaped)


def highlight_gherkin_strings(escaped_line: str) -> str:
    return re.sub(r"(&quot;.*?&quot;)", r'<span class="syntax-string">\1</span>', escaped_line)


def parse_mermaid_node(expression: str, nodes: dict[str, MermaidNode]) -> str:
    expression = expression.strip().rstrip(";")
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*)\s*\[\s*\"(.+)\"\s*\]", expression)
    if match:
        node_id, label = match.groups()
        nodes[node_id] = MermaidNode(node_id, label)
        return node_id
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*)", expression)
    if match:
        node_id = match.group(1)
        nodes.setdefault(node_id, MermaidNode(node_id, node_id))
        return node_id
    node_id = f"node{len(nodes) + 1}"
    nodes[node_id] = MermaidNode(node_id, expression.strip('"'))
    return node_id


def parse_mermaid(source: str) -> tuple[dict[str, MermaidNode], list[MermaidEdge]]:
    nodes: dict[str, MermaidNode] = {}
    edges: list[MermaidEdge] = []

    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("%%") or line.startswith("flowchart"):
            continue
        if "-->" not in line:
            parse_mermaid_node(line, nodes)
            continue

        parts = re.split(r"\s*-->(?:\|\"([^\"]+)\"\|)?\s*", line)
        previous = parse_mermaid_node(parts[0], nodes)
        index = 1
        while index < len(parts):
            label = parts[index] or ""
            target_expression = parts[index + 1] if index + 1 < len(parts) else ""
            target = parse_mermaid_node(target_expression, nodes)
            edges.append(MermaidEdge(previous, target, label))
            previous = target
            index += 2

    return nodes, edges


def mermaid_label_lines(label: str, max_chars: int = 28) -> list[str]:
    lines: list[str] = []
    for segment in re.split(r"<br\s*/?>", label):
        words = html.unescape(segment).split()
        current: list[str] = []
        length = 0
        for word in words:
            proposed = length + len(word) + (1 if current else 0)
            if current and proposed > max_chars:
                lines.append(" ".join(current))
                current = [word]
                length = len(word)
            else:
                current.append(word)
                length = proposed
        if current:
            lines.append(" ".join(current))
    return lines or [label]


def render_mermaid_svg(source: str) -> str:
    nodes, edges = parse_mermaid(source)
    if not nodes:
        return ""

    indegree = {node_id: 0 for node_id in nodes}
    children: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for edge in edges:
        indegree[edge.target] = indegree.get(edge.target, 0) + 1
        children.setdefault(edge.source, []).append(edge.target)

    ranks: dict[str, int] = {node_id: 0 for node_id, count in indegree.items() if count == 0}
    pending = list(ranks)
    while pending:
        current = pending.pop(0)
        for child in children.get(current, []):
            next_rank = ranks[current] + 1
            if next_rank > ranks.get(child, -1):
                ranks[child] = next_rank
                pending.append(child)
    for node_id in nodes:
        ranks.setdefault(node_id, 0)

    grouped: dict[int, list[str]] = {}
    for node_id, rank in ranks.items():
        grouped.setdefault(rank, []).append(node_id)

    node_width = 300
    min_node_height = 54
    h_gap = 30
    v_gap = 24
    margin = 24
    row_heights: dict[int, int] = {}
    label_lines = {node_id: mermaid_label_lines(node.label, max_chars=36) for node_id, node in nodes.items()}

    for rank, node_ids in grouped.items():
        row_heights[rank] = max(min_node_height, max(30 + len(label_lines[node_id]) * 15 for node_id in node_ids))

    max_rank_width = max(len(node_ids) * node_width + (len(node_ids) - 1) * h_gap for node_ids in grouped.values())
    width = max(620, max_rank_width + margin * 2)
    y_by_rank: dict[int, int] = {}
    y = margin
    for rank in sorted(grouped):
        y_by_rank[rank] = y
        y += row_heights[rank] + v_gap
    height = y - v_gap + margin

    positions: dict[str, tuple[float, float, int, int]] = {}
    for rank in sorted(grouped):
        node_ids = grouped[rank]
        row_width = len(node_ids) * node_width + (len(node_ids) - 1) * h_gap
        x = (width - row_width) / 2
        for node_id in node_ids:
            node_height = row_heights[rank]
            positions[node_id] = (x, y_by_rank[rank], node_width, node_height)
            x += node_width + h_gap

    svg: list[str] = [
        f'<svg class="mermaid-svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="Mermaid diagram" xmlns="http://www.w3.org/2000/svg">',
        "<defs>",
        '<marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">',
        '<path d="M0,0 L0,6 L9,3 z" fill="#b83218" />',
        "</marker>",
        "</defs>",
    ]

    for edge in edges:
        sx, sy, sw, sh = positions[edge.source]
        tx, ty, tw, _ = positions[edge.target]
        start_x = sx + sw / 2
        start_y = sy + sh
        end_x = tx + tw / 2
        end_y = ty
        mid_y = start_y + max(20, (end_y - start_y) / 2)
        path = f"M {start_x:.1f} {start_y:.1f} C {start_x:.1f} {mid_y:.1f}, {end_x:.1f} {mid_y:.1f}, {end_x:.1f} {end_y - 8:.1f}"
        svg.append(f'<path class="mermaid-edge" d="{path}" marker-end="url(#arrow)" />')
        if edge.label:
            label_x = (start_x + end_x) / 2
            label_y = mid_y - 7
            svg.append(f'<text class="mermaid-edge-label" x="{label_x:.1f}" y="{label_y:.1f}" text-anchor="middle">{html.escape(edge.label)}</text>')

    for node_id, node in nodes.items():
        x, y_pos, w, h = positions[node_id]
        svg.append(f'<rect class="mermaid-node" x="{x:.1f}" y="{y_pos:.1f}" width="{w}" height="{h}" rx="8" />')
        lines = label_lines[node_id]
        text_y = y_pos + h / 2 - ((len(lines) - 1) * 8)
        svg.append(f'<text class="mermaid-node-label" x="{x + w / 2:.1f}" y="{text_y:.1f}" text-anchor="middle">')
        for index, line in enumerate(lines):
            dy = 0 if index == 0 else 17
            svg.append(f'<tspan x="{x + w / 2:.1f}" dy="{dy}">{html.escape(line)}</tspan>')
        svg.append("</text>")

    svg.append("</svg>")
    return "\n".join(svg)


def mermaid_chain_order(nodes: dict[str, MermaidNode], edges: list[MermaidEdge]) -> list[str] | None:
    if len(edges) != len(nodes) - 1:
        return None

    outgoing: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    incoming: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for edge in edges:
        outgoing[edge.source].append(edge.target)
        incoming[edge.target].append(edge.source)

    starts = [node_id for node_id, sources in incoming.items() if not sources]
    if len(starts) != 1:
        return None
    if any(len(targets) > 1 for targets in outgoing.values()):
        return None
    if any(len(sources) > 1 for sources in incoming.values()):
        return None

    order = [starts[0]]
    seen = {starts[0]}
    current = starts[0]
    while outgoing[current]:
        current = outgoing[current][0]
        if current in seen:
            return None
        seen.add(current)
        order.append(current)

    return order if len(order) == len(nodes) else None


def render_mermaid_chain_svg(nodes: dict[str, MermaidNode], edges: list[MermaidEdge], order: list[str]) -> str:
    columns = 2
    node_width = 270
    min_node_height = 58
    h_gap = 42
    v_gap = 34
    margin = 24
    label_lines = {node_id: mermaid_label_lines(nodes[node_id].label, max_chars=30) for node_id in order}
    row_count = (len(order) + columns - 1) // columns
    width = margin * 2 + columns * node_width + (columns - 1) * h_gap

    positions: dict[str, tuple[float, float, int, int]] = {}
    row_heights: list[int] = []
    for row in range(row_count):
        row_ids = order[row * columns : (row + 1) * columns]
        row_heights.append(max(min_node_height, max(32 + len(label_lines[node_id]) * 15 for node_id in row_ids)))

    y = margin
    for row in range(row_count):
        row_ids = order[row * columns : (row + 1) * columns]
        row_width = len(row_ids) * node_width + (len(row_ids) - 1) * h_gap
        x = (width - row_width) / 2
        for node_id in row_ids:
            positions[node_id] = (x, y, node_width, row_heights[row])
            x += node_width + h_gap
        y += row_heights[row] + v_gap
    height = y - v_gap + margin

    edge_lookup = {(edge.source, edge.target): edge for edge in edges}
    svg: list[str] = [
        f'<svg class="mermaid-svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="Mermaid diagram" xmlns="http://www.w3.org/2000/svg">',
        "<defs>",
        '<marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">',
        '<path d="M0,0 L0,6 L9,3 z" fill="#b83218" />',
        "</marker>",
        "</defs>",
    ]

    for index, source in enumerate(order[:-1]):
        target = order[index + 1]
        edge = edge_lookup.get((source, target), MermaidEdge(source, target))
        sx, sy, sw, sh = positions[source]
        tx, ty, tw, th = positions[target]
        source_row = index // columns
        target_row = (index + 1) // columns
        source_col = index % columns
        target_col = (index + 1) % columns

        if source_row == target_row:
            start_x = sx + sw
            start_y = sy + sh / 2
            end_x = tx - 8
            end_y = ty + th / 2
            path = f"M {start_x:.1f} {start_y:.1f} L {end_x:.1f} {end_y:.1f}"
            label_x = (start_x + end_x) / 2
            label_y = start_y - 8
        elif source_col == columns - 1:
            start_x = sx + sw / 2
            start_y = sy + sh
            end_x = tx + tw / 2
            end_y = ty - 8
            mid_y = start_y + (end_y - start_y) / 2
            path = f"M {start_x:.1f} {start_y:.1f} C {start_x:.1f} {mid_y:.1f}, {end_x:.1f} {mid_y:.1f}, {end_x:.1f} {end_y:.1f}"
            label_x = (start_x + end_x) / 2
            label_y = mid_y - 8
        else:
            start_x = sx + sw
            start_y = sy + sh / 2
            end_x = tx - 8
            end_y = ty + th / 2
            path = f"M {start_x:.1f} {start_y:.1f} L {end_x:.1f} {end_y:.1f}"
            label_x = (start_x + end_x) / 2
            label_y = start_y - 8

        svg.append(f'<path class="mermaid-edge" d="{path}" marker-end="url(#arrow)" />')
        if edge.label:
            svg.append(f'<text class="mermaid-edge-label" x="{label_x:.1f}" y="{label_y:.1f}" text-anchor="middle">{html.escape(edge.label)}</text>')

    for node_id in order:
        x, y_pos, w, h = positions[node_id]
        svg.append(f'<rect class="mermaid-node" x="{x:.1f}" y="{y_pos:.1f}" width="{w}" height="{h}" rx="8" />')
        lines = label_lines[node_id]
        text_y = y_pos + h / 2 - ((len(lines) - 1) * 8)
        svg.append(f'<text class="mermaid-node-label" x="{x + w / 2:.1f}" y="{text_y:.1f}" text-anchor="middle">')
        for line_index, line in enumerate(lines):
            dy = 0 if line_index == 0 else 16
            svg.append(f'<tspan x="{x + w / 2:.1f}" dy="{dy}">{html.escape(line)}</tspan>')
        svg.append("</text>")

    svg.append("</svg>")
    return "\n".join(svg)


def render_mermaid_block(source: str) -> str:
    svg = render_mermaid_with_mermaid_js(source)
    escaped_source = html.escape(source)
    return (
        '<figure class="mermaid-diagram">'
        f"{svg}"
        "<details>"
        "<summary>Mermaid source</summary>"
        f"<pre><code>{escaped_source}</code></pre>"
        "</details>"
        "</figure>"
    )


def find_mermaid_cli() -> str | None:
    local = Path("node_modules/.bin/mmdc")
    if local.exists():
        return str(local)
    return shutil.which("mmdc")


def render_mermaid_with_mermaid_js(source: str) -> str:
    mmdc = find_mermaid_cli()
    if not mmdc:
        raise RuntimeError("Mermaid CLI is not installed. Run `npm install` before rendering Mermaid diagrams.")

    with tempfile.TemporaryDirectory(prefix="finops-mermaid-") as tmp:
        tmp_dir = Path(tmp)
        input_path = tmp_dir / "diagram.mmd"
        output_path = tmp_dir / "diagram.svg"
        mermaid_config = tmp_dir / "mermaid-config.json"
        puppeteer_config = tmp_dir / "puppeteer-config.json"

        input_path.write_text(source, encoding="utf-8")
        mermaid_config.write_text(
            """{
  "theme": "base",
  "themeVariables": {
    "fontFamily": "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif",
    "primaryColor": "#ffffff",
    "primaryTextColor": "#0d3448",
    "primaryBorderColor": "#f2b199",
    "lineColor": "#b83218",
    "secondaryColor": "#fff0e8",
    "tertiaryColor": "#f7fbfc",
    "clusterBkg": "#f7fbfc",
    "clusterBorder": "#f2b199",
    "edgeLabelBackground": "#ffffff"
  },
  "flowchart": {
    "curve": "basis",
    "htmlLabels": true,
    "nodeSpacing": 48,
    "rankSpacing": 54
  }
}
""",
            encoding="utf-8",
        )
        puppeteer_config.write_text(
            f"""{{
  "executablePath": "{find_chromium(None) or '/usr/bin/chromium'}",
  "args": ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
}}
""",
            encoding="utf-8",
        )
        command = [
            mmdc,
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--configFile",
            str(mermaid_config),
            "--puppeteerConfigFile",
            str(puppeteer_config),
            "--backgroundColor",
            "transparent",
            "--svgId",
            "mermaid-" + hashlib.sha1(source.encode("utf-8")).hexdigest()[:12],
        ]
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "Mermaid CLI failed."
            raise RuntimeError(message)
        svg = output_path.read_text(encoding="utf-8")
        if 'class="' in svg[:300]:
            svg = re.sub(r'<svg([^>]*?)class="([^"]*)"', r'<svg\1class="mermaid-svg \2"', svg, count=1)
        else:
            svg = svg.replace("<svg ", '<svg class="mermaid-svg" ', 1)
        return svg


def parse_conversation(source: str) -> list[tuple[str, str]]:
    turns: list[tuple[str, str]] = []
    current_speaker = ""
    current_lines: list[str] = []

    for line in source.splitlines():
        speaker_match = re.fullmatch(r"(AI|User):", line.strip())
        if speaker_match:
            if current_speaker:
                turns.append((current_speaker, "\n".join(current_lines).strip()))
            current_speaker = speaker_match.group(1)
            current_lines = []
        else:
            current_lines.append(line)

    if current_speaker:
        turns.append((current_speaker, "\n".join(current_lines).strip()))
    return turns


def conversation_lines(text: str, max_chars: int = 52, max_lines: int = 7) -> list[str]:
    normalized = re.sub(r"\n\s*-\s*", "\n- ", text.strip())
    lines: list[str] = []
    for paragraph in normalized.splitlines():
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        words = paragraph.split()
        current: list[str] = []
        length = 0
        for word in words:
            proposed = length + len(word) + (1 if current else 0)
            if current and proposed > max_chars:
                lines.append(" ".join(current))
                current = [word]
                length = len(word)
            else:
                current.append(word)
                length = proposed
        if current:
            lines.append(" ".join(current))

    if len(lines) > max_lines:
        return lines[: max_lines - 1] + ["..."]
    return lines or [""]


def render_conversation_svg(source: str) -> str:
    turns = parse_conversation(source)
    if not turns:
        return ""

    width = 900
    margin = 28
    bubble_width = 610
    avatar_size = 44
    gap = 20
    y = margin
    bubbles: list[tuple[str, list[str], float, float, int]] = []

    for speaker, text in turns:
        lines = conversation_lines(text)
        height = max(70, 42 + len(lines) * 17)
        x = margin + avatar_size + 14 if speaker == "AI" else width - margin - avatar_size - 14 - bubble_width
        bubbles.append((speaker, lines, x, y, height))
        y += height + gap

    height = y - gap + margin
    svg: list[str] = [
        f'<figure class="conversation-diagram"><svg class="conversation-svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="Example user flow conversation" xmlns="http://www.w3.org/2000/svg">',
        '<rect class="conversation-bg" x="1" y="1" width="898" height="' + str(height - 2) + '" rx="14" />',
    ]

    for speaker, lines, x, y_pos, bubble_height in bubbles:
        is_ai = speaker == "AI"
        avatar_x = margin if is_ai else width - margin - avatar_size
        avatar_fill = "#165c7d" if is_ai else "#e4572e"
        bubble_class = "bubble-ai" if is_ai else "bubble-user"
        text_x = x + 22
        text_y = y_pos + 32
        tail = (
            f"M {x:.1f} {y_pos + 28:.1f} L {x - 12:.1f} {y_pos + 38:.1f} L {x:.1f} {y_pos + 48:.1f} Z"
            if is_ai
            else f"M {x + bubble_width:.1f} {y_pos + 28:.1f} L {x + bubble_width + 12:.1f} {y_pos + 38:.1f} L {x + bubble_width:.1f} {y_pos + 48:.1f} Z"
        )
        svg.append(f'<circle cx="{avatar_x + avatar_size / 2:.1f}" cy="{y_pos + 34:.1f}" r="{avatar_size / 2:.1f}" fill="{avatar_fill}" />')
        svg.append(f'<text class="avatar-label" x="{avatar_x + avatar_size / 2:.1f}" y="{y_pos + 40:.1f}" text-anchor="middle">{speaker}</text>')
        svg.append(f'<path class="{bubble_class}" d="{tail}" />')
        svg.append(f'<rect class="{bubble_class}" x="{x:.1f}" y="{y_pos:.1f}" width="{bubble_width}" height="{bubble_height}" rx="14" />')
        for index, line in enumerate(lines):
            svg.append(f'<text class="conversation-text" x="{text_x:.1f}" y="{text_y + index * 17:.1f}">{html.escape(line)}</text>')

    svg.append("</svg></figure>")
    return "\n".join(svg)


def render_security_architecture_svg(source: str) -> str:
    width = 1180
    height = 760

    def node(x: int, y: int, w: int, h: int, title: str, subtitle: str = "", class_name: str = "arch-node") -> str:
        title_y = y + 28 if subtitle else y + h / 2 + 5
        parts = [f'<rect class="{class_name}" x="{x}" y="{y}" width="{w}" height="{h}" rx="12" />']
        parts.append(f'<text class="arch-title" x="{x + w / 2}" y="{title_y:.1f}" text-anchor="middle">{html.escape(title)}</text>')
        if subtitle:
            for index, line in enumerate(mermaid_label_lines(subtitle, max_chars=24)):
                parts.append(f'<text class="arch-subtitle" x="{x + w / 2}" y="{y + 50 + index * 15}" text-anchor="middle">{html.escape(line)}</text>')
        return "\n".join(parts)

    def database(x: int, y: int, w: int, h: int, title: str, subtitle: str = "") -> str:
        parts = [
            f'<path class="arch-db" d="M{x},{y + 16} C{x},{y - 5} {x + w},{y - 5} {x + w},{y + 16} L{x + w},{y + h - 16} C{x + w},{y + h + 5} {x},{y + h + 5} {x},{y + h - 16} Z" />',
            f'<ellipse class="arch-db-top" cx="{x + w / 2}" cy="{y + 16}" rx="{w / 2}" ry="16" />',
            f'<text class="arch-title" x="{x + w / 2}" y="{y + 45}" text-anchor="middle">{html.escape(title)}</text>',
        ]
        if subtitle:
            for index, line in enumerate(mermaid_label_lines(subtitle, max_chars=25)):
                parts.append(f'<text class="arch-subtitle" x="{x + w / 2}" y="{y + 68 + index * 15}" text-anchor="middle">{html.escape(line)}</text>')
        return "\n".join(parts)

    def arrow(
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        label: str,
        bend: int = 0,
        label_x: int | None = None,
        label_y: int | None = None,
    ) -> str:
        if bend:
            mid_x = (x1 + x2) / 2
            path = f"M{x1},{y1} C{mid_x},{y1 + bend} {mid_x},{y2 - bend} {x2},{y2}"
            auto_label_x = mid_x
            auto_label_y = (y1 + y2) / 2 - 8
        else:
            path = f"M{x1},{y1} L{x2},{y2}"
            auto_label_x = (x1 + x2) / 2
            auto_label_y = (y1 + y2) / 2 - 8
        label_x = label_x if label_x is not None else int(auto_label_x)
        label_y = label_y if label_y is not None else int(auto_label_y)
        label_width = max(70, len(label) * 6 + 14)
        return (
            f'<path class="arch-arrow" d="{path}" marker-end="url(#arch-arrow)" />'
            f'<rect class="arch-label-bg" x="{label_x - label_width / 2:.1f}" y="{label_y - 14:.1f}" width="{label_width:.1f}" height="20" rx="5" />'
            f'<text class="arch-label" x="{label_x:.1f}" y="{label_y:.1f}" text-anchor="middle">{html.escape(label)}</text>'
        )

    return f"""
<figure class="architecture-diagram">
<svg class="architecture-svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="Security-centred architecture view" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <marker id="arch-arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">
      <path d="M0,0 L0,6 L9,3 z" fill="#b83218" />
    </marker>
  </defs>
  <rect class="arch-bg" x="1" y="1" width="1178" height="758" rx="18" />
  <rect class="arch-zone arch-zone-app" x="240" y="72" width="270" height="610" rx="16" />
  <text class="arch-zone-title" x="375" y="86" text-anchor="middle">Application Boundary</text>
  <rect class="arch-zone arch-zone-data" x="900" y="72" width="230" height="610" rx="16" />
  <text class="arch-zone-title" x="1015" y="86" text-anchor="middle">S3-backed Storage</text>

  {node(48, 362, 130, 72, "User", "enterprise SSO", "arch-node arch-user")}
  {node(300, 338, 150, 88, "Internal Web App / API", "authz, validation, write control", "arch-node arch-app")}
  {node(565, 110, 150, 78, "Amazon Bedrock Runtime", "approved model invocation", "arch-node arch-ai")}
  {node(770, 110, 150, 78, "Guardrail Boundary", "model constrained; no direct writes", "arch-node arch-guardrail")}

  {database(930, 180, 170, 88, "S3 Case Files", "narrow issue context")}
  {database(930, 320, 170, 88, "S3 Response Store", "validated state")}
  {database(930, 460, 170, 88, "S3 Dashboard Export", "approved prefix")}
  {database(930, 600, 170, 88, "S3 Audit Logs", "views, submissions, events")}
  {node(300, 555, 150, 72, "Technology Catalogue", "read assignment metadata", "arch-node arch-catalogue")}

  {arrow(178, 398, 300, 382, "SSO-authenticated HTTPS", 0, 240, 370)}
  {arrow(450, 356, 930, 222, "read S3 case context", -38, 760, 270)}
  {arrow(450, 376, 930, 362, "write response state", 0, 695, 342)}
  {arrow(450, 396, 930, 502, "publish dashboard export", 42, 678, 520)}
  {arrow(430, 426, 930, 642, "append audit events", 70, 710, 682)}
  {arrow(375, 555, 375, 426, "read metadata", 0, 445, 500)}
  {arrow(450, 348, 565, 149, "invoke approved model", -26, 526, 245)}
  {arrow(715, 149, 770, 149, "guardrails apply", 0, 742, 103)}
</svg>
</figure>
"""


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
            code = chr(10).join(code_lines)
            if language == "mermaid":
                blocks.append(render_mermaid_block(code))
                i += 1
                continue
            if language == "conversation":
                blocks.append(render_conversation_svg(code))
                i += 1
                continue
            if language == "security-architecture":
                blocks.append(render_security_architecture_svg(code))
                i += 1
                continue
            blocks.append(
                '<figure class="code-block">'
                f'<figcaption>{html.escape(language)}</figcaption>'
                f'<pre><code class="language-{html.escape(language)}">{highlight_code(code, language)}</code></pre>'
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
  max-height: calc(100vh - 48px);
  padding: 18px 18px 18px 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-width: thin;
  scrollbar-color: var(--accent-line) transparent;
  border-right: 1px solid var(--line);
}

.toc::-webkit-scrollbar {
  width: 8px;
}

.toc::-webkit-scrollbar-thumb {
  background: var(--accent-line);
  border-radius: 999px;
}

.toc::-webkit-scrollbar-track {
  background: transparent;
}

.toc h2 {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 0 18px;
  color: var(--brand-dark);
  font-size: 1.05rem;
  letter-spacing: 0;
}

.toc h2::before {
  content: "";
  width: 28px;
  height: 3px;
  background: linear-gradient(90deg, var(--accent), #ff8a3d);
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
  margin: 0 0 4px;
  padding: 7px 10px;
  border-left: 3px solid transparent;
  color: var(--muted);
  background: transparent;
  border-radius: 0 5px 5px 0;
  font-size: 0.86rem;
  line-height: 1.28;
  text-decoration: none;
}

.toc .toc-item a {
  font-weight: 680;
  color: #344253;
}

.toc .toc-subitem a {
  margin-left: 10px;
  padding: 5px 8px;
  color: var(--muted);
  font-size: 0.79rem;
}

.toc a:hover {
  color: var(--accent-strong);
  background: var(--accent-soft);
  border-left-color: var(--accent);
}

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

.syntax-key {
  color: #8fd6ff;
}

.syntax-string {
  color: #ffd082;
}

.syntax-number {
  color: #9de8b6;
}

.syntax-literal {
  color: #ffb0a0;
  font-weight: 720;
}

.syntax-gherkin-keyword {
  color: #8fd6ff;
  font-weight: 780;
}

.syntax-tag {
  color: #ffb0a0;
  font-weight: 720;
}

.syntax-comment {
  color: #7f93a3;
  font-style: italic;
}

.syntax-punctuation {
  color: #b9c7d4;
}

.mermaid-diagram {
  margin: 1.45rem 0 1.7rem;
  padding: 16px;
  overflow-x: auto;
  background: linear-gradient(180deg, #fffaf7, #f7fbfc);
  border: 1px solid var(--accent-line);
  box-shadow: 0 10px 28px rgba(25, 38, 52, 0.08);
  break-inside: avoid;
}

.mermaid-svg {
  display: block;
  width: auto;
  max-width: 100%;
  max-height: 760px;
  height: auto;
  margin: 0 auto;
}

.mermaid-node {
  fill: #ffffff;
  stroke: var(--accent-line);
  stroke-width: 1.4;
  filter: drop-shadow(0 5px 12px rgba(25, 38, 52, 0.12));
}

.mermaid-node-label {
  fill: var(--brand-dark);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
  font-weight: 700;
}

.mermaid-edge {
  fill: none;
  stroke: var(--accent-strong);
  stroke-width: 2.2;
}

.mermaid-edge-label {
  fill: var(--accent-strong);
  paint-order: stroke;
  stroke: #fffaf7;
  stroke-width: 5;
  stroke-linejoin: round;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 12px;
  font-weight: 760;
}

.mermaid-diagram details {
  margin-top: 12px;
  color: var(--muted);
  font-size: 0.8rem;
}

.mermaid-diagram summary {
  cursor: pointer;
  color: var(--accent-strong);
  font-weight: 720;
}

.mermaid-diagram details pre {
  margin-top: 8px;
  max-height: 240px;
  background: var(--code-bg);
  color: #eef7fb;
  border: 1px solid var(--code-line);
}

.conversation-diagram {
  margin: 1.45rem 0 1.75rem;
  overflow-x: auto;
  break-inside: avoid;
}

.conversation-svg {
  display: block;
  width: auto;
  max-width: 100%;
  height: auto;
  margin: 0 auto;
}

.conversation-bg {
  fill: #f7fbfc;
  stroke: var(--accent-line);
  stroke-width: 1.5;
}

.bubble-ai {
  fill: #ffffff;
  stroke: #bfd3dd;
  stroke-width: 1.3;
}

.bubble-user {
  fill: var(--accent-soft);
  stroke: var(--accent-line);
  stroke-width: 1.3;
}

.avatar-label {
  fill: #ffffff;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 13px;
  font-weight: 800;
}

.conversation-text {
  fill: var(--ink);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
  font-weight: 560;
}

.architecture-diagram {
  margin: 1.45rem 0 1.75rem;
  overflow-x: auto;
  break-inside: avoid;
}

.architecture-svg {
  display: block;
  width: auto;
  max-width: 100%;
  height: auto;
  margin: 0 auto;
}

.arch-bg {
  fill: #f7fbfc;
  stroke: var(--accent-line);
  stroke-width: 1.5;
}

.arch-zone {
  fill: rgba(255, 255, 255, 0.64);
  stroke-width: 1.2;
  stroke-dasharray: 6 5;
}

.arch-zone-app {
  stroke: #9fbdca;
}

.arch-zone-data {
  stroke: var(--accent-line);
}

.arch-zone-title {
  fill: var(--muted);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 13px;
  font-weight: 800;
  text-transform: uppercase;
}

.arch-node {
  fill: #ffffff;
  stroke: #bfd3dd;
  stroke-width: 1.4;
  filter: drop-shadow(0 5px 12px rgba(25, 38, 52, 0.10));
}

.arch-user {
  stroke: var(--accent-line);
}

.arch-app {
  stroke: var(--brand);
  stroke-width: 1.8;
}

.arch-ai {
  fill: #eef7fb;
}

.arch-guardrail {
  fill: var(--accent-soft);
  stroke: var(--accent-line);
}

.arch-catalogue {
  fill: #fbfdff;
}

.arch-db {
  fill: #ffffff;
  stroke: var(--accent-line);
  stroke-width: 1.5;
  filter: drop-shadow(0 5px 12px rgba(25, 38, 52, 0.10));
}

.arch-db-top {
  fill: #fff8f4;
  stroke: var(--accent-line);
  stroke-width: 1.5;
}

.arch-title {
  fill: var(--brand-dark);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
  font-weight: 820;
}

.arch-subtitle {
  fill: var(--muted);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 11px;
  font-weight: 620;
}

.arch-arrow {
  fill: none;
  stroke: var(--accent-strong);
  stroke-width: 2;
}

.arch-label-bg {
  fill: rgba(255, 255, 255, 0.92);
  stroke: rgba(242, 177, 153, 0.9);
  stroke-width: 1;
}

.arch-label {
  fill: var(--accent-strong);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 11px;
  font-weight: 780;
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
    max-height: none;
    overflow: visible;
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
    max-height: none;
    overflow: visible;
    padding: 0;
    border: 0;
  }

  .toc h2 {
    margin-bottom: 12mm;
    color: var(--brand-dark);
    font-size: 18pt;
  }

  .toc h2::before {
    width: 34px;
    height: 4px;
    print-color-adjust: exact;
    -webkit-print-color-adjust: exact;
  }

  .toc ol {
    column-count: 2;
    column-gap: 12mm;
    column-rule: 1px solid var(--accent-line);
  }

  .toc a {
    margin: 0 0 1.5mm;
    padding: 0;
    border: 0;
    border-radius: 0;
    color: var(--ink);
    background: transparent;
    font-size: 9.6pt;
    line-height: 1.22;
    break-inside: avoid;
  }

  .toc .toc-item a {
    color: var(--brand-dark);
    font-weight: 720;
  }

  .toc .toc-subitem a {
    margin-left: 4mm;
    padding: 0;
    color: var(--muted);
    font-size: 8.4pt;
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

  .mermaid-diagram {
    padding: 0;
    overflow: visible;
    background: transparent;
    border: 0;
    box-shadow: none;
    print-color-adjust: exact;
    -webkit-print-color-adjust: exact;
  }

  .mermaid-svg {
    width: auto;
    max-width: 100%;
    max-height: 168mm;
  }

  .mermaid-diagram details {
    display: none;
  }

  .conversation-diagram {
    overflow: visible;
  }

  .conversation-svg {
    width: auto;
    max-width: 100%;
    max-height: 185mm;
  }

  .architecture-diagram {
    overflow: visible;
  }

  .architecture-svg {
    width: auto;
    max-width: 100%;
    max-height: 170mm;
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
        remove_pdf_date_entries(target_pdf)
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
        source_pdf.replace(target_pdf)
        remove_pdf_date_entries(target_pdf)
        return
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
    for env_name in ("CHROME_PATH", "CHROME_BIN"):
        configured = os.environ.get(env_name)
        if configured:
            return configured
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
