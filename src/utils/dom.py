"""
DOM processing utilities for efficient token usage.

FIXES:
1. get_interactive_elements() now includes [role="link"] for modern SPAs
2. Better visibility detection (checks computed style, not just rect)
3. Prioritize elements in viewport (scroll position aware)
4. Text extraction handles more cases (aria-label, title)

TASK 2 (generalize beyond hh.ru): the selector list and visibility checks
below were generic already, but the set of *interaction patterns* covered
was limited to what actually showed up on hh.ru (ARIA links, duplicate
desktop/mobile markup). This module now also:
- covers a broader set of common ARIA interaction roles (menuitem, tab,
  checkbox, radio, option) and contenteditable regions, not just
  button/link/onclick, so more SPA/component-library patterns get picked
  up;
- annotates elements that share identical visible text with an ordinal
  hint ("Apply (#2 of 5 similar)"), since several visually distinct list
  items with identical text is a common failure mode the LLM has no other
  signal to disambiguate.
"""

import logging
from collections import defaultdict
from typing import Any

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)


class DOMProcessor:
    """
    Process HTML DOM into minimal, token-efficient representation.

    CRITICAL METHOD: get_interactive_elements()
    - This is what orchestrator should use (not process_html)
    - Marks live DOM with data-agent-id attributes
    - Returns list of dicts ready for browser.element_map
    """

    def __init__(self, settings=None, max_text_length: int = 200):
        """
        Initialize DOM processor.

        Args:
            settings: Optional settings object
            max_text_length: Maximum characters per text block (overridden
                by settings.text_block_max_length when settings is provided)
        """
        # FIX (Docs vs Code Drift #6): TEXT_BLOCK_MAX_LENGTH was documented
        # as configurable in .env.example / README, but production code
        # never read it - the JS below hardcoded substring(0, 200)
        # regardless of this setting. Now actually wired through.
        if settings is not None and hasattr(settings, "text_block_max_length"):
            self.max_text_length = settings.text_block_max_length
        else:
            self.max_text_length = max_text_length
        self.element_map: dict[int, str] = {}  # Legacy, not used with get_interactive_elements
        self.next_id = 0

    async def get_interactive_elements(self, page) -> tuple[list[dict[str, Any]], str | None]:
        """
        FIXED: Extract interactive elements from live page using JavaScript.

        IMPROVEMENTS:
        1. Added [role="link"] for SPA navigation (critical for hh.ru)
        2. Better visibility check (computed style + rect)
        3. Viewport-aware prioritization
        4. Handles aria-label and title attributes for better text extraction

        Args:
            page: Playwright page object

        Returns:
            Tuple of:
              - List of dicts with keys: id, tag, text, selector (sorted
                top-to-bottom by Y position)
              - An error message string if extraction failed, else None.
                FIX (4.3 Minor): previously a failed extraction silently
                returned [], indistinguishable from "page has genuinely no
                interactive elements" - the LLM would likely conclude the
                page was empty rather than that something went wrong.
                Callers (orchestrator._get_observation) now surface this
                explicitly instead of guessing.
        """
        try:
            elements = await page.evaluate(
                "(maxTextLength) => {\n"
                '                    // FIXED: Include role="link" for modern SPAs\n'
                "                    const selectors = [\n"
                "                        'button',\n"
                "                        'a',\n"
                "                        'input',\n"
                "                        'select', \n"
                "                        'textarea',\n"
                "                        '[role=\"button\"]',\n"
                "                        '[role=\"link\"]',  // Critical for SPA navigation\n"
                "                        '[onclick]',      // Inline onclick handlers\n"
                "                        '[role=\"menuitem\"]', // Dropdown/nav menus\n"
                "                        '[role=\"tab\"]',      // Tabbed interfaces\n"
                "                        '[role=\"checkbox\"]', // Custom (non-<input>) checkboxes\n"
                "                        '[role=\"radio\"]',    // Custom radio buttons\n"
                "                        '[role=\"option\"]',   // Custom dropdown/listbox options\n"
                "                        '[contenteditable=\"true\"]', // Rich-text editable regions\n"
                "                        'summary'         // <details>/<summary> disclosure widgets\n"
                "                    ];\n"
                "                    \n"
                "                    // Find all matching elements\n"
                "                    const allElements = document.querySelectorAll(selectors.join(','));\n"
                "                    const results = [];\n"
                "                    let idCounter = 0;\n"
                "                    \n"
                "                    allElements.forEach(element => {\n"
                "                        // FIXED: Better visibility check\n"
                "                        const rect = element.getBoundingClientRect();\n"
                "                        const style = window.getComputedStyle(element);\n"
                "                        \n"
                "                        // Check if element is visible\n"
                "                        const isVisible = (\n"
                "                            rect.width > 0 && \n"
                "                            rect.height > 0 &&\n"
                "                            style.display !== 'none' &&\n"
                "                            style.visibility !== 'hidden' &&\n"
                "                            style.opacity !== '0'\n"
                "                        );\n"
                "                        \n"
                "                        if (!isVisible) {\n"
                "                            return; // Skip invisible elements\n"
                "                        }\n"
                "                        \n"
                "                        // Mark element with data-agent-id\n"
                "                        element.setAttribute('data-agent-id', idCounter);\n"
                "                        \n"
                "                        // FIXED: Better text extraction\n"
                "                        let text = '';\n"
                "                        \n"
                "                        // Priority 1: Form input values\n"
                "                        if (element.value) {\n"
                "                            text = element.value;\n"
                "                        } \n"
                "                        // Priority 2: Placeholder\n"
                "                        else if (element.placeholder) {\n"
                "                            text = `[Placeholder: ${element.placeholder}]`;\n"
                "                        }\n"
                "                        // Priority 3: Aria-label (accessibility)\n"
                "                        else if (element.getAttribute('aria-label')) {\n"
                "                            text = element.getAttribute('aria-label');\n"
                "                        }\n"
                "                        // Priority 4: Title attribute\n"
                "                        else if (element.title) {\n"
                "                            text = element.title;\n"
                "                        }\n"
                "                        // Priority 5: Inner text\n"
                "                        else {\n"
                "                            text = element.innerText || element.textContent || '';\n"
                "                        }\n"
                "                        \n"
                "                        // Clean and limit text (FIX: use configurable\n"
                "                        // maxTextLength instead of a hardcoded 200)\n"
                "                        text = text.trim().replace(/\\s+/g, ' ').substring(0, maxTextLength);\n"
                "                        \n"
                "                        // Get position for sorting\n"
                "                        const y = rect.top + window.scrollY;\n"
                "                        \n"
                "                        // Build result object\n"
                "                        results.push({\n"
                "                            id: idCounter,\n"
                "                            tag: element.tagName.toLowerCase(),\n"
                "                            text: text,\n"
                '                            selector: `[data-agent-id="${idCounter}"]`,\n'
                "                            y: y  // For sorting\n"
                "                        });\n"
                "                        \n"
                "                        idCounter++;\n"
                "                    });\n"
                "                    \n"
                "                    // Sort by Y position (top to bottom)\n"
                "                    // This ensures LLM sees elements in reading order\n"
                "                    results.sort((a, b) => a.y - b.y);\n"
                "                    \n"
                "                    // Remove y from final output (not needed in Python)\n"
                "                    return results.map(r => ({\n"
                "                        id: r.id,\n"
                "                        tag: r.tag,\n"
                "                        text: r.text,\n"
                "                        selector: r.selector\n"
                "                    }));\n"
                "                }",
                self.max_text_length,
            )

            elements = elements if elements else []
            elements = self._annotate_duplicate_text(elements)
            return (elements, None)

        except Exception as e:
            # Fail gracefully on complex pages, but tell the caller WHY -
            # see the FIX note in the docstring above.
            logger.warning(f"Failed to extract interactive elements: {e}")
            return ([], str(e))

    def _annotate_duplicate_text(self, elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        TASK 2 (generalize beyond hh.ru): pages routinely render several
        visually distinct rows/cards that share identical or near-identical
        text - e.g. an "Apply" button on every row of a job board, "Add to
        cart" on every product card. Text alone gives the LLM no signal for
        which one to pick, and unlike hh.ru's desktop/mobile duplication
        (already handled by the .first-locator fallback in browser.py),
        this isn't a selector-uniqueness problem - each element already has
        its own distinct element_id/data-agent-id. It's purely a "which of
        these N identical-looking options is the right one" problem.

        This appends an ordinal hint to the text of every element that
        shares an exact, non-empty text with at least one sibling, e.g.
        "Apply (#2 of 5 similar)", so the model has at least a
        relative-position signal to reason with when picking an element_id.
        Elements are otherwise left completely untouched (unique text,
        empty text, and text shared with only one other but at different
        positions all keep their original ordinal by DOM/Y-order, which
        get_interactive_elements() already sorts by).
        """
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for elem in elements:
            text = (elem.get("text") or "").strip()
            if text:
                groups[text].append(elem)

        for text, group in groups.items():
            if len(group) > 1:
                for i, elem in enumerate(group, start=1):
                    elem["text"] = f"{text} (#{i} of {len(group)} similar)"

        return elements

    # Legacy method - NOT USED in fixed orchestrator
    def process_html(self, html: str) -> tuple[str, dict[int, str]]:
        """
        DEPRECATED: Use get_interactive_elements() instead.

        This method parses static HTML with BeautifulSoup.
        Problem: Element IDs don't match live DOM.

        Kept for backward compatibility only.
        """
        soup = BeautifulSoup(html, "html.parser")

        # Remove noise
        for tag in soup(["script", "style", "meta", "link"]):
            tag.decompose()

        # Extract interactive elements
        self.element_map = {}
        self.next_id = 0
        lines = []

        self._process_node(soup.body if soup.body else soup, lines)

        return "\\n".join(lines), self.element_map

    def _process_node(self, node, lines: list[str], depth: int = 0):
        """Recursively process DOM nodes (legacy)."""
        if isinstance(node, Tag):
            # Check if interactable
            if self._is_interactable(node):
                element_id = self.next_id
                self.next_id += 1

                selector = self._build_selector(node)
                self.element_map[element_id] = selector

                # Format line
                indent = "  " * depth
                tag_name = node.name.upper()
                attrs = self._format_attrs(node)
                text = self._extract_text(node)

                line = f"{indent}[{element_id}] {tag_name} {attrs} {text}"
                lines.append(line)

            # Recurse children
            for child in node.children:
                self._process_node(child, lines, depth + 1)

    def _is_interactable(self, tag: Tag) -> bool:
        """Check if element is interactable (legacy)."""
        interactable_tags = ["a", "button", "input", "select", "textarea"]
        return tag.name in interactable_tags or tag.get("onclick")

    def _build_selector(self, tag: Tag) -> str:
        """
        Build robust CSS selector using priority hierarchy (legacy).

        NOTE: This is NOT used when using get_interactive_elements().
        That method uses data-agent-id selectors which are guaranteed unique.
        """
        # Priority 1: ID attribute (with CSS escaping)
        if tag.get("id"):
            element_id = tag["id"]
            if element_id and element_id[0].isdigit():
                escaped_id = self._css_escape_id(element_id)
                return f"#{escaped_id}"
            return f"#{element_id}"

        # Priority 2: data-qa attribute
        for qa_attr in ["data-qa", "data-test-id", "data-testid", "data-test"]:
            if tag.get(qa_attr):
                return f"{tag.name}[{qa_attr}='{tag[qa_attr]}']"

        # Priority 3: name attribute
        if tag.get("name"):
            return f"{tag.name}[name='{tag['name']}']"

        # Priority 4: tag + classes
        if tag.get("class"):
            classes = tag["class"]
            class_list = classes[:2] if isinstance(classes, list) else [classes]
            class_selector = "".join(f".{cls}" for cls in class_list)
            return f"{tag.name}{class_selector}"

        # Priority 5: nth-child fallback
        parent = tag.parent
        if parent:
            siblings_of_same_tag = [
                s for s in parent.children if isinstance(s, Tag) and s.name == tag.name
            ]
            if len(siblings_of_same_tag) > 1:
                try:
                    index = siblings_of_same_tag.index(tag) + 1
                    return f"{tag.name}:nth-child({index})"
                except ValueError:
                    pass

        return tag.name

    def _css_escape_id(self, element_id: str) -> str:
        """Escape ID for CSS selector if it starts with digit."""
        if not element_id:
            return element_id

        if element_id[0].isdigit():
            hex_code = hex(ord(element_id[0]))[2:]
            return f"\\\\{hex_code} {element_id[1:]}"

        return element_id

    def _format_attrs(self, tag: Tag) -> str:
        """Format relevant attributes for display (legacy)."""
        attrs = []
        for attr in ["name", "type", "placeholder", "value", "data-qa", "data-test-id"]:
            if tag.get(attr):
                attrs.append(f"{attr}='{tag[attr]}'")
        return " ".join(attrs)

    def _extract_text(self, tag: Tag) -> str:
        """Extract and truncate text content (legacy)."""
        text = tag.get_text(strip=True)
        if len(text) > self.max_text_length:
            text = text[: self.max_text_length] + "..."
        return f'"{text}"' if text else ""
