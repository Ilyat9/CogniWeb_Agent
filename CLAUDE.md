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
- `ENABLE_STEALTH`: Enable playwright-stealth

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

Agent can call 11 tools via `AgentAction` model:
1. `navigate(url)` - Navigate to URL
2. `click_element(element_id)` - Click element
3. `type_text(element_id, text, press_enter)` - Type text
4. `select_option(element_id, value)` - Select dropdown option
5. `scroll_page(direction)` - Scroll up/down
6. `take_screenshot()` - Capture screenshot
7. `wait(seconds)` - Wait for page update
8. `go_back()` - Navigate back
9. `query_dom(query)` - Search page for text
10. `store_context(key, value)` or `store_context(field1=value1, field2=value2)` - Store data
11. `done(summary)` - Complete task

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
