from __future__ import annotations

import html
from pathlib import Path

from ops_dashboard.layout import html_page


def markdown_to_html(markdown: str) -> str:
    lines = markdown.splitlines()
    out: list[str] = []
    in_code = False
    code_lines: list[str] = []
    for line in lines:
        if line.startswith("```"):
            if in_code:
                out.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if line.startswith("# "):
            out.append(f"<h1>{html.escape(line[2:].strip())}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{html.escape(line[3:].strip())}</h2>")
        elif line.strip():
            text = html.escape(line.strip()).replace("`", "")
            out.append(f"<p>{text}</p>")
    return "\n".join(out)


def render(_request_ctx: object = None) -> str:
    path = Path(__file__).resolve().parent.parent / "HELP.md"
    body = f"<section>{markdown_to_html(path.read_text(encoding='utf-8'))}</section>"
    return html_page("Help", body, active_section="help")
