# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CogniWeb_Agent is a production-ready autonomous web browser agent built with Python. It uses Playwright for browser automation and LLMs (via OpenRouter API) for decision-making, implementing the ReAct (Reasoning + Acting) pattern to autonomously navigate and interact with websites.

**Tech Stack**: Python 3.10+, Playwright, Pydantic v2, OpenAI SDK, OpenRouter API

## Development Commands

### Setup
```bash
# Install dependencies
pip install -r requirements.txt

# Install browser binaries
playwright install chromium

# Create .env from template
cp .env.example .env
```

### Running
```bash
# Main entry point with signal handling
python main.py

# Alternative entry point (generator-based)
python comprehensive_completion.py
```

## Architecture

### Layered Structure
```
src/
├── config/              # Pydantic Settings, validation
├── core/                # Domain models, exceptions
├── infrastructure/      # BrowserService, LLMService (I/O layer)
├── agent/               # AgentOrchestrator (ReAct loop)
└── utils/               # DOMProcessor
```

### Key Design Patterns

**Dependency Injection**: All services receive dependencies via constructor injection. This enables easy mocking for tests.

**Context Managers**: BrowserService and LLMService use `async __aenter__`/`__aexit__` for guaranteed cleanup.

**Async/Await**: All I/O operations are async throughout the codebase.

**Pydantic Validation**: Config uses BaseSettings for type-safe configuration. LLM outputs are validated via Pydantic models.

### Entry Point (`main.py`)
- Signal handling (SIGINT/SIGTERM) via GracefulShutdown class
- Prevents zombie browser processes on exit
- Uses `asyncio.shield()` for cleanup safety

### AgentOrchestrator (`src/agent/orchestrator.py`)
- Implements ReAct loop: Observe → Think → Act
- Smart loop detection tracks `(action + target + success)` triples (not just observations)
- Tracks conversation history for context window management
- Detects CAPTCHAs and raises `CaptchaDetectedError`
- Stores data in `context_data` for multi-step tasks

### BrowserService (`src/infrastructure/browser.py`)
- Context manager pattern for resource cleanup
- Stealth mode via `playwright-stealth` package
- Human-like typing with random jitter (TYPING_SPEED_MIN to MAX)
- Auto-snapshots (screenshot + HTML dump) on critical failures
- Retry logic with exponential backoff
- Strict mode handling: `.first` fallback for non-unique selectors

### LLMService (`src/infrastructure/llm.py`)
- AsyncOpenAI client configured for OpenRouter
- Retry with tenacity library
- JSON extraction from LLM responses (handles code blocks, malformed JSON)
- Rate limiting: 15s between requests (configurable in orchestrator)
- Token tracking for cost monitoring

### DOMProcessor (`src/utils/dom.py`)
- Uses JavaScript to inject `data-agent-id` attributes for element tracking
- Returns sorted elements by viewport position
- Prioritizes visible, interactable elements
- Handles aria-label, placeholder, title attributes

## Configuration

### Required .env Variables
- `OPENAI_API_KEY`: OpenRouter API key (validated against placeholder values)
- `API_BASE_URL`: OpenRouter endpoint
- `MODEL_NAME`: Model to use (e.g., `upstage/solar-pro-3:free`)

### Browser Settings
- `HEADLESS`: Run without GUI (true for production)
- `SLOW_MO`: Milliseconds delay between actions
- `ENABLE_STEALTH_MODE` (alias `ENABLE_STEALTH`): stealth browser profile (default TRUE - the one feature flag that is on by default; set false only for debugging the raw Playwright profile)
- `STEALTH_USER_AGENT` / `STEALTH_LOCALE` / `STEALTH_TIMEZONE` / `STEALTH_VIEWPORT_WIDTH/HEIGHT`: consistent stealth fingerprint profile (all applied together)

### Agent Settings
- `MAX_STEPS`: Maximum reasoning-action steps (default: 50)
- `TEMPERATURE`: LLM temperature (lower = more deterministic, default: 0.1)
- `MAX_TOKENS`: Max tokens in LLM response

### DOM Processing
- `TEXT_BLOCK_MAX_LENGTH`: Max characters per text block (default: 200)
- `DOM_MAX_TOKENS_ESTIMATE`: Token budget for DOM (default: 10000)

### Loop Detection
- `LOOP_DETECTION_WINDOW`: States to check for loops
- `MAX_IDENTICAL_STATES`: Threshold for intervention

## The ReAct Toolset

Agent can call 25 tools via `AgentAction` model:
1. `navigate(url)` - Navigate to URL
2. `click_element(element_id)` - Click element
3. `type_text(element_id, text, press_enter)` - Type text
4. `upload_file(element_id, file_path)` - Upload a file (path must stay inside UPLOAD_ALLOWED_DIR)
5. `select_option(element_id, value)` - Select dropdown option
6. `hover_element(element_id)` - Hover (menus, tooltips)
7. `press_key(key)` - Keyboard event/combination ('Enter', 'Control+a')
8. `scroll_page(direction)` - Scroll up/down
9. `take_screenshot()` - Capture screenshot
10. `wait(seconds)` - Wait fixed seconds
11. `wait_for_element(element_id|selector, state, timeout_ms)` - Condition-based wait (preferred over blind wait)
12. `go_back()` - Navigate back
13. `go_forward()` - Navigate forward
14. `query_dom(query)` - Search page for text
15. `find_element_by_text(text, tag)` - Semantic search over the live DOM; registers fresh element_ids
16. `extract_page_content()` - Cleaned Markdown of the page (ENABLE_MARKDOWN_EXTRACTION, default off)
17. `extract_structured_data(key, selector)` - Extract table data into context_data[key]
18. `list_tabs()` - List open tabs
19. `switch_tab(index)` - Switch to another open tab
20. `download_file(element_id)` - Click and save the download into DOWNLOAD_ALLOWED_DIR
21. `assert_page_state(expect_text_present=... | expect_url_contains=... | expect_element_visible=...)` - cheap no-LLM page check; failure = ActionResult, not an exception
22. `set_variable(name, value)` / `get_variable(name)` - intermediate scratch memory (self.scratch_memory), deliberately NOT part of TaskResult.context_data
23. `store_context(key, value)` or `store_context(field1=value1, field2=value2)` - Store data for the FINAL result
24. `done(summary)` - Complete task

NOTE: every tool name must appear in the system prompt (tests/test_hardening.py enforces it), and any tool returning page text (query_dom, extract_page_content, extract_structured_data, find_element_by_text) has its result wrapped in `<untrusted_page_content>` before entering conversation history (prompt-injection guard, orchestrator._format_action_result).

New tools are validated in `src/core/models.py` (AgentAction.validators), implemented in `BrowserService` (`src/infrastructure/browser.py`) and dispatched in `AgentOrchestrator._execute_action`. All new tools have unit tests (`tests/test_new_tools.py`).

## Web UI (Phase 4)

`make install-ui && make run-ui` serves the static SPA (src/api/static/index.html) plus the API on :8000.
Key endpoints added in `src/api/app.py`: `GET /tasks`, `GET /task/{id}/steps`, `GET /task/{id}/screenshot`, `POST /task/{id}/stop` (per-task graceful stop via the same shutdown_check pattern), `WS /ws/task/{id}` (live step stream; the orchestrator publishes via the optional `event_sink` constructor arg), `GET /config` (secrets masked), `GET /reports[/{run_id}]`. `GET /task/{id}` additionally serves `current_step`/`last_tool` DURING a run via the `on_step` orchestrator hook.

Access control: `API_BIND_HOST` (default 127.0.0.1 - never 0.0.0.0 by default) and optional `API_AUTH_TOKEN` (bearer auth on all /task* + /config + /reports + WS `?token=`; `/health` stays open). File endpoints resolve+validate paths against SCREENSHOT_DIR/REPORTS_DIR.

## Visual fallback & markdown extraction (Phase 4)

- `ENABLE_VISUAL_FALLBACK` (alias `ENABLE_VISION_FALLBACK`, default FALSE) + `MODEL_SUPPORTS_VISION`: switch a step to an annotated screenshot (set-of-marks, numbers = element_id) when DOM extraction is empty/failed/noisy OR after `VISUAL_FALLBACK_ERROR_STREAK` (default 2) consecutive InvalidElementId steps.
- `ENABLE_MARKDOWN_EXTRACTION` (default FALSE): `extract_page_content` - crawl4ai as OPTIONAL converter (requirements-tools.txt, lazy import, never launches its own browser) with a built-in stdlib-only fallback cleaner (`src/utils/extract.py`).
- Optional extras live in `requirements-tools.txt` (playwright-stealth, crawl4ai) and `requirements-ui.txt` (fastapi+uvicorn+websockets) - never in base requirements.txt.

## Loop Detection Logic

The system distinguishes between:
- **Real loops**: Same action on same target failing repeatedly → raises `LoopDetectedError`
- **Validation errors**: Invalid element ID when page changed → treated as error, not loop

Loop signature tracked: `(tool, element_id, success)`

## Error Handling

Exception hierarchy in `src/core/exceptions.py`:
```
AgentBaseException
├── ConfigurationError
├── NetworkError
├── BrowserError
├── SelectorError
├── ActionError
├── ValidationError
├── LLMError
├── LoopDetectedError
├── CaptchaDetectedError
├── TimeoutError
└── AgentCriticalError  # Triggers auto-snapshots
```

## Testing Strategy

All components use dependency injection (easy to mock):
- Consider adding pytest with:
  - Unit tests for orchestrator (mock browser/llm)
  - Integration tests with real browser
  - Mock tests for JSON extraction

## Important Notes

- Validation in settings prevents placeholder API keys
- URLs are validated to block javascript:, data:, file: protocols
- Anti-fingerprinting: Stealth mode + human-like typing + slow motion
- Persistent browser session in `./browser_data` (cookies, localStorage)
- Error snapshots saved to `./screenshots/`
- Logging to `agent.log`
