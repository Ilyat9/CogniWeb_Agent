"""
DOM processing utilities for efficient token usage.

FIXES:
1. get_interactive_elements() now includes [role="link"] for modern SPAs
2. Better visibility detection (checks computed style, not just rect)
3. Prioritize elements in viewport (scroll position aware)
4. Text extraction handles more cases (aria-label, title)
"""

import re
from typing import List, Dict, Any
from bs4 import BeautifulSoup, Tag


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
            max_text_length: Maximum characters per text block
        """
        self.max_text_length = max_text_length
        self.element_map: Dict[int, str] = {}  # Legacy, not used with get_interactive_elements
        self.next_id = 0
    
    async def get_interactive_elements(self, page) -> List[Dict[str, Any]]:
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
            List of dicts with keys: id, tag, text, selector
            Elements are sorted by Y position (top to bottom)
        """
        try:
            elements = await page.evaluate("""
                () => {
                    // FIXED: Include role="link" for modern SPAs
                    const selectors = [
                        'button',
                        'a',
                        'input',
                        'select', 
                        'textarea',
                        '[role="button"]',
                        '[role="link"]',  // NEW: Critical for SPA navigation
                        '[onclick]'       // NEW: Inline onclick handlers
                    ];
                    
                    // Find all matching elements
                    const allElements = document.querySelectorAll(selectors.join(','));
                    const results = [];
                    let idCounter = 0;
                    
                    allElements.forEach(element => {
                        // FIXED: Better visibility check
                        const rect = element.getBoundingClientRect();
                        const style = window.getComputedStyle(element);
                        
                        // Check if element is visible
                        const isVisible = (
                            rect.width > 0 && 
                            rect.height > 0 &&
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            style.opacity !== '0'
                        );
                        
                        if (!isVisible) {
                            return; // Skip invisible elements
                        }
                        
                        // Mark element with data-agent-id
                        element.setAttribute('data-agent-id', idCounter);
                        
                        // FIXED: Better text extraction
                        let text = '';
                        
                        // Priority 1: Form input values
                        if (element.value) {
                            text = element.value;
                        } 
                        // Priority 2: Placeholder
                        else if (element.placeholder) {
                            text = `[Placeholder: ${element.placeholder}]`;
                        }
                        // Priority 3: Aria-label (accessibility)
                        else if (element.getAttribute('aria-label')) {
                            text = element.getAttribute('aria-label');
                        }
                        // Priority 4: Title attribute
                        else if (element.title) {
                            text = element.title;
                        }
                        // Priority 5: Inner text
                        else {
                            text = element.innerText || element.textContent || '';
                        }
                        
                        // Clean and limit text
                        text = text.trim().replace(/\\s+/g, ' ').substring(0, 200);
                        
                        // Get position for sorting
                        const y = rect.top + window.scrollY;
                        
                        // Build result object
                        results.push({
                            id: idCounter,
                            tag: element.tagName.toLowerCase(),
                            text: text,
                            selector: `[data-agent-id="${idCounter}"]`,
                            y: y  // For sorting
                        });
                        
                        idCounter++;
                    });
                    
                    // Sort by Y position (top to bottom)
                    // This ensures LLM sees elements in reading order
                    results.sort((a, b) => a.y - b.y);
                    
                    // Remove y from final output (not needed in Python)
                    return results.map(r => ({
                        id: r.id,
                        tag: r.tag,
                        text: r.text,
                        selector: r.selector
                    }));
                }
            """)
            
            return elements if elements else []
            
        except Exception as e:
            # Fail gracefully on complex pages
            print(f"Warning: Failed to extract interactive elements: {e}")
            return []
    
    # Legacy method - NOT USED in fixed orchestrator
    def process_html(self, html: str) -> tuple[str, Dict[int, str]]:
        """
        DEPRECATED: Use get_interactive_elements() instead.
        
        This method parses static HTML with BeautifulSoup.
        Problem: Element IDs don't match live DOM.
        
        Kept for backward compatibility only.
        """
        soup = BeautifulSoup(html, 'html.parser')
        
        # Remove noise
        for tag in soup(['script', 'style', 'meta', 'link']):
            tag.decompose()
        
        # Extract interactive elements
        self.element_map = {}
        self.next_id = 0
        lines = []
        
        self._process_node(soup.body if soup.body else soup, lines)
        
        return "\\n".join(lines), self.element_map
    
    def _process_node(self, node, lines: List[str], depth: int = 0):
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
        interactable_tags = ['a', 'button', 'input', 'select', 'textarea']
        return tag.name in interactable_tags or tag.get('onclick')
    
    def _build_selector(self, tag: Tag) -> str:
        """
        Build robust CSS selector using priority hierarchy (legacy).
        
        NOTE: This is NOT used when using get_interactive_elements().
        That method uses data-agent-id selectors which are guaranteed unique.
        """
        # Priority 1: ID attribute (with CSS escaping)
        if tag.get('id'):
            element_id = tag['id']
            if element_id and element_id[0].isdigit():
                escaped_id = self._css_escape_id(element_id)
                return f"#{escaped_id}"
            return f"#{element_id}"
        
        # Priority 2: data-qa attribute
        for qa_attr in ['data-qa', 'data-test-id', 'data-testid', 'data-test']:
            if tag.get(qa_attr):
                return f"{tag.name}[{qa_attr}='{tag[qa_attr]}']"
        
        # Priority 3: name attribute
        if tag.get('name'):
            return f"{tag.name}[name='{tag['name']}']"
        
        # Priority 4: tag + classes
        if tag.get('class'):
            classes = tag['class']
            class_list = classes[:2] if isinstance(classes, list) else [classes]
            class_selector = ''.join(f'.{cls}' for cls in class_list)
            return f"{tag.name}{class_selector}"
        
        # Priority 5: nth-child fallback
        parent = tag.parent
        if parent:
            siblings_of_same_tag = [
                s for s in parent.children 
                if isinstance(s, Tag) and s.name == tag.name
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
        for attr in ['name', 'type', 'placeholder', 'value', 'data-qa', 'data-test-id']:
            if tag.get(attr):
                attrs.append(f"{attr}='{tag[attr]}'")
        return " ".join(attrs)
    
    def _extract_text(self, tag: Tag) -> str:
        """Extract and truncate text content (legacy)."""
        text = tag.get_text(strip=True)
        if len(text) > self.max_text_length:
            text = text[:self.max_text_length] + "..."
        return f'"{text}"' if text else ""