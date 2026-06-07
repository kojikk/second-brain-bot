"""
Render lightweight Markdown (as produced by the agent / LLM) into
Telegram-safe HTML for parse_mode="HTML".

Telegram HTML supports: <b> <i> <u> <s> <code> <pre> <a> <blockquote>.
It does NOT support Markdown headers (#), so we render them as bold —
which means raw "##"/"###" from the model can never leak into the chat.
"""
import html
import re

_FENCE_RE   = re.compile(r"```[ \t]*([a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)
_HEADER_RE  = re.compile(r"^\s{0,3}(#{1,6})\s+(.*\S)\s*$")
_HR_RE      = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_BULLET_RE  = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUM_RE     = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_QUOTE_RE   = re.compile(r"^\s*>\s?(.*)$")
_LINK_RE    = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE    = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_ITALIC_US_RE   = re.compile(r"(?<![\w*])_(?!\s)(.+?)(?<!\s)_(?![\w*])")
_CODE_RE    = re.compile(r"`([^`]+)`")

DIVIDER = "\u2500" * 12  # ────────────


def _inline(text: str) -> str:
    """Apply inline formatting. `text` MUST already be HTML-escaped."""
    code_spans: list[str] = []

    def _stash(m: re.Match) -> str:
        code_spans.append(m.group(1))
        return f"\x00{len(code_spans) - 1}\x00"

    text = _CODE_RE.sub(_stash, text)
    text = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC_STAR_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _ITALIC_US_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{code_spans[int(m.group(1))]}</code>", text)
    return text


def to_telegram_html(md: str) -> str:
    """Render Markdown-ish text to Telegram-safe HTML."""
    if not md:
        return ""

    blocks: list[str] = []

    def _stash_block(m: re.Match) -> str:
        code = html.escape(m.group(2).rstrip("\n"))
        blocks.append(f"<pre>{code}</pre>")
        return f"\x01{len(blocks) - 1}\x01"

    md = _FENCE_RE.sub(_stash_block, md)

    out: list[str] = []
    quote_buf: list[str] = []

    def _flush_quote() -> None:
        if quote_buf:
            out.append("<blockquote>" + "\n".join(quote_buf) + "</blockquote>")
            quote_buf.clear()

    for line in md.split("\n"):
        block_m = re.fullmatch(r"\s*\x01(\d+)\x01\s*", line)
        if block_m:
            _flush_quote()
            out.append(blocks[int(block_m.group(1))])
            continue

        q = _QUOTE_RE.match(line)
        if q:
            quote_buf.append(_inline(html.escape(q.group(1))))
            continue
        _flush_quote()

        if _HR_RE.match(line):
            out.append(DIVIDER)
            continue

        h = _HEADER_RE.match(line)
        if h:
            out.append(f"<b>{_inline(html.escape(h.group(2)))}</b>")
            continue

        b = _BULLET_RE.match(line)
        if b:
            indent = "   " * (len(b.group(1)) // 2)
            out.append(f"{indent}\u2022 {_inline(html.escape(b.group(2)))}")
            continue

        n = _NUM_RE.match(line)
        if n:
            out.append(f"{n.group(1)}{n.group(2)}. {_inline(html.escape(n.group(3)))}")
            continue

        out.append(_inline(html.escape(line)))

    _flush_quote()

    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
