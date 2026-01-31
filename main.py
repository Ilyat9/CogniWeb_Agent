#!/usr/bin/env python3
"""
Battle-Ready Browser Agent - Main Entry Point

Production-grade autonomous browser agent with:
- Async/await architecture
- Dependency injection
- Graceful shutdown handling
- Comprehensive error handling
- Signal management for clean termination
"""

import asyncio
import signal
import sys
import logging
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from src.config import load_settings
from src.core.exceptions import ConfigurationError, AgentCriticalError
from src.infrastructure import BrowserService, LLMService
from src.agent import AgentOrchestrator

# Configure basic logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('agent.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class GracefulShutdown:
    """
    Handle shutdown signals gracefully.
    
    Why needed?
    - SIGINT (Ctrl+C) and SIGTERM must trigger clean browser shutdown
    - Prevents zombie browser processes
    - Ensures resources are released properly
    """
    
    def __init__(self):
        self.shutdown_requested = False
    
    def request_shutdown(self, signum, frame):
        """Signal handler."""
        logger.warning("Shutdown requested... cleaning up")
        print("\n⚠️  Shutdown requested... cleaning up")
        self.shutdown_requested = True


async def main() -> int:
    """
    Main async entry point.
    
    Returns:
        Exit code (0 = success, 1 = failure)
    """
    print("\n" + "="*70)
    print("   BATTLE-READY BROWSER AGENT v4.2")
    print("   Modular Monolith Architecture")
    print("="*70 + "\n")
    
    # Setup signal handling
    shutdown = GracefulShutdown()
    signal.signal(signal.SIGINT, shutdown.request_shutdown)
    signal.signal(signal.SIGTERM, shutdown.request_shutdown)
    
    try:
        # Load and validate configuration
        settings = load_settings()
        logger.info(f"Configuration loaded - Model: {settings.model_name}, Max Steps: {settings.max_steps}")
        print("✅ Configuration loaded")
        print(f"   Model: {settings.model_name}")
        print(f"   Max Steps: {settings.max_steps}")
        print(f"   Stealth: {'Enabled' if settings.enable_stealth else 'Disabled'}")
        
    except ConfigurationError as e:
        logger.error(f"Configuration Error: {e}")
        print(f"❌ Configuration Error: {e}")
        return 1
    
    # Get task from user
    print("\n" + "-"*70)
    task = input("📝 Enter task: ").strip()
    if not task:
        print("No task provided")
        return 1
    
    starting_url = input("🌐 Starting URL (optional): ").strip() or None
    print("-"*70 + "\n")
    
    # Create services with dependency injection
    browser = BrowserService(settings)
    llm = LLMService(settings)
    
    try:
        # Use context managers for guaranteed cleanup
        async with browser, llm:
            logger.info("Browser and LLM services initialized")
            print("✅ Browser launched\n")
            
            # Create orchestrator
            orchestrator = AgentOrchestrator(settings, browser, llm)
            
            # Run task
            result = await orchestrator.run(task, starting_url)
            
            # Display result
            print("\n" + "="*70)
            if result.success:
                logger.info(f"Task completed successfully in {result.steps_taken} steps")
                print("✅ TASK COMPLETED SUCCESSFULLY!")
            else:
                logger.warning(f"Task failed: {result.summary}")
                print("❌ TASK FAILED")
            print("="*70)
            print(f"Summary: {result.summary}")
            print(f"Steps: {result.steps_taken}")
            print(f"Duration: {result.total_duration_seconds:.1f}s")
            if result.final_url:
                print(f"Final URL: {result.final_url}")
            
            return 0 if result.success else 1
    
    except AgentCriticalError as e:
        logger.critical(f"Critical error: {e}", exc_info=True)
        print(f"\n❌ CRITICAL ERROR: {e}")
        if e.context.get("screenshot_path"):
            print(f"Screenshot saved: {e.context['screenshot_path']}")
        if e.context.get("html_dump_path"):
            print(f"HTML dump saved: {e.context['html_dump_path']}")
        return 1
    
    except Exception as e:
        logger.exception("Unexpected error")
        print(f"\n❌ UNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    finally:
        print("\n👋 Cleanup complete")


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
