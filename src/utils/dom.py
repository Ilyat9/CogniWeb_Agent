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
                "                    // Clear stale data-agent-id attributes from\n"
                "                    // previous observations BEFORE assigning fresh\n"
                "                    // ones. IDs restart at 0 every call, so any\n"
                "                    // element that drops out of the current\n"
                "                    // (visible) selection would otherwise keep a\n"
                "                    // stale id forever - and a later\n"
                '                    // [data-agent-id="N"] selector could match a\n'
                "                    // stale hidden element instead of (or in\n"
                "                    // addition to) the fresh one, breaking strict\n"
                "                    // mode or clicking the wrong node.\n"
                "                    document.querySelectorAll('[data-agent-id]')\n"
                "                        .forEach(el => el.removeAttribute('data-agent-id'));\n"
                "                    \n"
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
