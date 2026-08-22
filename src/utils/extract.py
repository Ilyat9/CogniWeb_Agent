"""
Task 3 (Crawl4AI approach, implemented as an optional wrapper): convert the
CURRENT page's HTML into cleaned Markdown/text with navigation/script/style/
boilerplate filtered out, at a fraction of the tokens of a raw DOM snapshot.

Design decisions (per the accepted architecture decision):
- crawl4ai is an OPTIONAL extra (requirements-tools.txt), imported lazily
  inside _crawl4ai_markdown() - never a hard dependency. Same lazy-import
  pattern as TOKEN_COUNTER_MODE=tiktoken (one warning per process when the
  package is missing, silent fallback afterwards).
- We NEVER let crawl4ai launch its own browser runtime: the HTML is taken
  from the already-open Playwright page (page.content()) and only the
  HTML->Markdown conversion is used. If the installed crawl4ai version's
  API doesn't cooperate, we fall back to the built-in heuristic cleaner -
  the local fallback is the primary, always-available implementation and
  the optional package is an upgrade, not a requirement.
- The fallback cleaner is dependency-free (stdlib only): block-level tag
  removal + heading/list/link conversion + entity unescaping + whitespace
  collapsing. It is deliberately simple - the goal is "readable, cheap
  text", not a pixel-perfect Markdown render.

The entry point is async because crawl4ai's converter is async and the
caller (the orchestrator tool handler) already runs inside the event loop.
"""

import html as html_module
import logging
import re
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

# One warning per process when the optional package is missing, mirroring
# the tiktoken fallback pattern (see orchestrator._estimate_tokens).
_CRAWL4AI_STATE = {"warned": False, "available": None}

# Tags whose entire subtree is noise for content extraction.
_DROP_BLOCKS = [
    "script",
    "style",
    "noscript",
    "svg",
    "canvas",
    "template",
    "iframe",
    "form",
    "nav",
    "footer",
    "aside",
    "header",
]

_HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6"]


async def _crawl4ai_markdown(page_html: str, base_url: str) -> str | None:
    """
    Try to convert HTML to Markdown via the optional crawl4ai package.

    Returns None (caller falls back to the heuristic cleaner) when the
    package is not installed or its API fails in ANY way - a failing
    optional upgrade must never break the tool.
    """
    if _CRAWL4AI_STATE["available"] is False:
        return None
    try:
        # Lazy import by design (optional dependency).
        from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
    except ImportError:
        _CRAWL4AI_STATE["available"] = False
        if not _CRAWL4AI_STATE["warned"]:
            _CRAWL4AI_STATE["warned"] = True
            logger.warning(
                "crawl4ai is not installed - extract_page_content uses the "
                "built-in heuristic cleaner (install requirements-tools.txt "
                "for the higher-quality converter). This warning logs once."
            )
        return None

    try:
        generator = DefaultMarkdownGenerator()
        result = await generator.generate_markdown(url=base_url or "", html=page_html)
        _CRAWL4AI_STATE["available"] = True
        # API shape differs slightly across crawl4ai versions.
        if isinstance(result, str):
            return result
        return getattr(result, "raw_markdown", None) or str(result)
    except Exception as e:  # noqa: BLE001 - optional path must never raise
        logger.debug(f"crawl4ai markdown conversion failed, using fallback: {e}")
        return None


def _strip_blocks(html: str, tags: list[str]) -> str:
    """Remove <tag ...>...</tag> subtrees (case-insensitive, multiline)."""
    for tag in tags:
        html = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        # Unclosed/void variants (e.g. <script src=... />)
        html = re.sub(rf"<{tag}\b[^>]*/>", " ", html, flags=re.IGNORECASE)
    return html


def heuristic_html_to_markdown(page_html: str, base_url: str = "", max_length: int = 20000) -> str:
    """
    Dependency-free HTML -> cleaned Markdown heuristic (the always-available
    fallback / baseline implementation).

    Keeps: page title, headings (as #-prefixes), paragraphs, list items,
    links (as [text](absolute-url)), table cells (pipe-joined).
    Drops: everything in _DROP_BLOCKS plus HTML comments and remaining tags.
    """
    html = _strip_blocks(page_html, _DROP_BLOCKS)
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)

    title = ""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if title_match:
        title = html_module.unescape(title_match.group(1)).strip()

    # Links -> [text](url) before generic tag stripping. Non-greedy per <a>.
    def _link_repl(match: re.Match) -> str:
        inner = re.sub(r"<[^>]+>", "", match.group(2))
        text = html_module.unescape(inner).strip()
        href = html_module.unescape(match.group(1) or "").strip()
        if not text:
            return ""
        if href and base_url:
            href = urljoin(base_url, href)
        return f"[{text}]({href})" if href else text

    html = re.sub(
        r"<a\b[^>]*?href=[\"']([^\"']*)[\"'][^>]*>(.*?)</a>",
        _link_repl,
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Headings -> Markdown prefix.
    for level, tag in enumerate(_HEADING_TAGS, start=1):
        html = re.sub(
            rf"<{tag}\b[^>]*>(.*?)</{tag}>",
            lambda m, lvl=level: "\n\n"
            + "#" * lvl
            + " "
            + re.sub(r"<[^>]+>", "", m.group(1)).strip()
            + "\n",
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )

    # List items.
    html = re.sub(
        r"<li\b[^>]*>(.*?)</li>",
        lambda m: "\n- " + re.sub(r"<[^>]+>", " ", m.group(1)).strip(),
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Table cells -> pipe-joined rows (tables often hold the actual data).
    html = re.sub(
        r"<t[dh]\b[^>]*>(.*?)</t[dh]>",
        lambda m: " " + re.sub(r"<[^>]+>", " ", m.group(1)).strip() + " |",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Structural breaks.
    html = re.sub(r"<(br|hr)\b[^>]*/?>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(
        r"</(p|div|section|article|tr|table|ul|ol|blockquote)>", "\n", html, flags=re.IGNORECASE
    )

    # Strip remaining tags, unescape entities.
    text = re.sub(r"<[^>]+>", " ", html)
    text = html_module.unescape(text)

    # Collapse whitespace but keep paragraph breaks.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)

    if title:
        text = f"# {title}\n\n{text}"

    if len(text) > max_length:
        text = text[:max_length] + "\n\n[... truncated]"
    return text.strip()


async def html_to_markdown(page_html: str, base_url: str = "", max_length: int = 20000) -> str:
    """
    HTML -> cleaned Markdown. crawl4ai (if installed) first, heuristic
    fallback always available. See module docstring for the rationale.
    """
    if not page_html:
        return ""
    via_crawl4ai = await _crawl4ai_markdown(page_html, base_url)
    if via_crawl4ai and via_crawl4ai.strip():
        if len(via_crawl4ai) > max_length:
            return via_crawl4ai[:max_length] + "\n\n[... truncated]"
        return via_crawl4ai.strip()
    return heuristic_html_to_markdown(page_html, base_url=base_url, max_length=max_length)
