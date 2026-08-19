#!/usr/bin/env python3
"""설계 문서 HTML을 에이전트가 읽을 Markdown으로 변환한다.

HTML 문서는 사람이 읽고 공유하기 좋지만 에이전트 입력으로는 낭비가 크다.
세 문서를 합치면 147KB인데 실제 본문은 42%뿐이고 나머지는 CSS와 태그다.
워커가 문서를 읽을 때마다 그 비용을 치르면 컨텍스트가 빨리 차고 compact가 잦아진다.

HTML은 사람용으로 그대로 두고, 같은 내용의 Markdown을 나란히 둔다.
문서를 고치면 이 스크립트를 다시 돌린다.

    python scripts/html2md.py            # docs/*.html → docs/*.md
    python scripts/html2md.py --check    # 최신 여부만 확인 (CI용)
"""

from __future__ import annotations

import argparse
import re
import sys
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"

# 본문만 변환한다. 좌측 목차는 Markdown에서 불필요하다.
MAIN_RE = re.compile(r"<main>(.*)</main>", re.S)

VOID = {"br", "hr", "img", "link", "meta", "input", "source"}
SKIP = {"style", "script", "aside", "nav"}


class Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node | str] = []

    def cls(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def text(self) -> str:
        out = []
        for c in self.children:
            out.append(c if isinstance(c, str) else c.text())
        return "".join(out)


class Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {})
        self.stack = [self.root]
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if self.skip_depth:
            if tag not in VOID:
                self.skip_depth += 1
            return
        if tag in SKIP:
            self.skip_depth = 1
            return
        node = Node(tag, {k: (v or "") for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if not self.skip_depth:
            self.stack[-1].children.append(data)


# ── 인라인 ───────────────────────────────────────────────
def inline(node: Node | str) -> str:
    if isinstance(node, str):
        return re.sub(r"\s+", " ", node)
    t = node.tag
    inner = "".join(inline(c) for c in node.children)
    if t == "code":
        return f"`{inner.strip()}`"
    if t in ("strong", "b"):
        return f"**{inner.strip()}**"
    if t in ("em", "i"):
        return f"*{inner.strip()}*"
    if t == "a":
        href = node.attrs.get("href", "")
        return f"[{inner.strip()}]({href})" if href else inner
    if t == "br":
        return "  \n"
    return inner


def clean(s: str) -> str:
    return re.sub(r"[ \t]+", " ", s).strip()


# ── 블록 ─────────────────────────────────────────────────
def fenced(body: str, info: str = "") -> str:
    return f"```{info}\n{body.rstrip()}\n```"


def render_table(node: Node) -> str:
    rows: list[tuple[list[str], bool]] = []
    for tr in iter_tags(node, "tr"):
        cells, head = [], False
        for cell in node_children(tr):
            if cell.tag in ("th", "td"):
                cells.append(clean(inline(cell)).replace("|", "\\|") or " ")
                head = head or cell.tag == "th"
        if cells:
            rows.append((cells, head))
    if not rows:
        return ""
    width = max(len(c) for c, _ in rows)
    out = []
    header_done = False
    for cells, head in rows:
        cells = cells + [" "] * (width - len(cells))
        out.append("| " + " | ".join(cells) + " |")
        if head and not header_done:
            out.append("|" + "|".join([" --- "] * width) + "|")
            header_done = True
    if not header_done:  # 헤더 없는 표도 Markdown은 헤더 행을 요구한다
        out.insert(1, "|" + "|".join([" --- "] * width) + "|")
    return "\n".join(out)


def node_children(n: Node):
    return [c for c in n.children if isinstance(c, Node)]


def iter_tags(n: Node, tag: str):
    for c in node_children(n):
        if c.tag == tag:
            yield c
        else:
            yield from iter_tags(c, tag)


def find_cls(n: Node, name: str) -> Node | None:
    for c in node_children(n):
        if name in c.cls():
            return c
        r = find_cls(c, name)
        if r:
            return r
    return None


def render_list(node: Node, ordered: bool, depth: int = 0) -> str:
    out = []
    for i, li in enumerate([c for c in node_children(node) if c.tag == "li"], 1):
        nested = [c for c in node_children(li) if c.tag in ("ul", "ol")]
        body = clean(
            "".join(inline(c) for c in li.children if not (isinstance(c, Node) and c.tag in ("ul", "ol")))
        )
        marker = f"{i}." if ordered else "-"
        out.append("  " * depth + f"{marker} {body}")
        for sub in nested:
            out.append(render_list(sub, sub.tag == "ol", depth + 1))
    return "\n".join(out)


def render(node: Node | str) -> str:
    if isinstance(node, str):
        return "" if not node.strip() else clean(node)

    t, cls = node.tag, node.cls()

    # 특수 블록 — 클래스로 판별
    if "eq" in cls:
        name = find_cls(node, "eq-name")
        line = find_cls(node, "eq-line")
        where = find_cls(node, "eq-where")
        body = unescape(line.text()).rstrip() if line else ""
        parts = [fenced(body, "")]
        if name:
            parts.insert(0, f"**{clean(name.text())}**\n")
        if where:
            parts.append(f"> {clean(inline(where))}")
        return "\n".join(parts)

    if "prompt" in cls:
        name = find_cls(node, "prompt-name")
        body = find_cls(node, "prompt-body")
        title = f"**{clean(name.text())}**\n\n" if name else ""
        return title + fenced(unescape(body.text()).rstrip() if body else "")

    if "figure" in cls:
        return fenced(unescape(node.text()).rstrip())

    if "rule-box" in cls:
        label = find_cls(node, "label")
        lines = []
        if label:
            lines.append(f"**{clean(label.text())}**")
        for p in node_children(node):
            if p.tag == "p":
                lines.append(clean(inline(p)))
        return "\n".join(f"> {ln}" for ln in lines if ln)

    if "turn" in cls:
        tag_el = find_cls(node, "turn-tag")
        who = clean(tag_el.text()) if tag_el else ""
        body = clean(
            "".join(
                inline(c)
                for c in node.children
                if not (isinstance(c, Node) and "turn-tag" in c.cls())
            )
        )
        return f"> **{who}**  \n> {body}" if who else f"> {body}"

    if "ep-head" in cls:
        verb = next((c for c in node_children(node) if "verb" in c.cls()), None)
        path = next((c for c in node_children(node) if "path" in c.cls()), None)
        tags = [clean(c.text()) for c in node_children(node) if "tag" in c.cls()]
        line = f"**{clean(verb.text())}** `{clean(path.text())}`" if verb and path else ""
        if tags:
            line += "  —  " + " · ".join(tags)
        return line

    if "meta-strip" in cls:
        return " · ".join(clean(inline(c)) for c in node_children(node) if c.tag == "span")

    # 일반 태그
    if t in ("h1", "h2", "h3", "h4", "h5"):
        # 절 번호는 별도 span이라 그대로 이으면 "§3Risk"처럼 붙는다.
        parts = []
        for c in node.children:
            frag = inline(c)
            if isinstance(c, Node) and "num" in c.cls():
                frag = frag.strip() + " "
            parts.append(frag)
        return "#" * int(t[1]) + " " + clean("".join(parts))
    if t == "p":
        return clean(inline(node))
    if t == "pre":
        return fenced(unescape(node.text()).rstrip())
    if t == "table":
        return render_table(node)
    if t in ("ul", "ol"):
        return render_list(node, t == "ol")
    if t == "hr":
        return "---"
    if t == "footer":
        return "---\n\n" + clean(inline(node))

    # 컨테이너 — 자식을 이어 붙인다
    return "\n\n".join(p for p in (render(c) for c in node.children) if p.strip())


def convert(html: str) -> str:
    m = MAIN_RE.search(html)
    body = m.group(1) if m else html
    b = Builder()
    b.feed(body)
    md = render(b.root)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="설계 문서 HTML → Markdown")
    ap.add_argument("--check", action="store_true", help="변환 결과가 최신인지만 확인")
    args = ap.parse_args()

    stale = []
    for src in sorted(DOCS.glob("*.html")):
        dst = src.with_suffix(".md")
        md = convert(src.read_text(encoding="utf-8"))
        if args.check:
            if not dst.exists() or dst.read_text(encoding="utf-8") != md:
                stale.append(dst.name)
            continue
        dst.write_text(md, encoding="utf-8")
        before, after = len(src.read_bytes()), len(md.encode())
        print(f"  {src.name:24} {before:>7,}B → {dst.name:22} {after:>7,}B  ({after*100//before}%)")

    if args.check and stale:
        print("최신이 아님:", ", ".join(stale), file=sys.stderr)
        print("python scripts/html2md.py 를 실행할 것", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
