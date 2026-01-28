#!/usr/bin/env python3
"""
Example usage scripts for the Autonomous Browser Agent.
Demonstrates common tasks and patterns.
"""

import os
from main import Agent, Config

# Ensure API key is set
if not os.getenv("OPENAI_API_KEY") and not os.getenv("LLM_API_KEY"):
    print("Error: Please set OPENAI_API_KEY or LLM_API_KEY environment variable")
    exit(1)


def example_1_google_search():
    """Example: Simple Google search."""
    print("\n" + "="*60)
    print("EXAMPLE 1: Google Search")
    print("="*60 + "\n")
    
    config = Config.from_env()
    agent = Agent(config)
    
    success = agent.run(
        task="Search for 'Playwright Python documentation' and click the first result",
        starting_url="https://www.google.com"
    )
    
    print(f"\nResult: {'✓ Success' if success else '✗ Failed'}")
    return success


def example_2_wikipedia_navigation():
    """Example: Navigate Wikipedia and extract information."""
    print("\n" + "="*60)
    print("EXAMPLE 2: Wikipedia Navigation")
    print("="*60 + "\n")
    
    config = Config.from_env()
    agent = Agent(config)
    
    success = agent.run(
        task="Go to the Wikipedia page for Python (programming language) and click on 'History' section",
        starting_url="https://en.wikipedia.org/wiki/Main_Page"
    )
    
    print(f"\nResult: {'✓ Success' if success else '✗ Failed'}")
    return success


def example_3_form_filling():
    """Example: Fill out a contact form."""
    print("\n" + "="*60)
    print("EXAMPLE 3: Form Filling")
    print("="*60 + "\n")
    
    config = Config.from_env()
    agent = Agent(config)
    
    success = agent.run(
        task="""
        Fill out the contact form with the following information:
        - Name: John Doe
        - Email: john.doe@example.com
        - Message: I'm interested in your services
        Then submit the form.
        """,
        starting_url="https://www.example.com/contact"  # Replace with actual form
    )
    
    print(f"\nResult: {'✓ Success' if success else '✗ Failed'}")
    return success


def example_4_job_search():
    """Example: Search for jobs (requires manual login first)."""
    print("\n" + "="*60)
    print("EXAMPLE 4: Job Search on HH.ru")
    print("="*60 + "\n")
    
    print("NOTE: You may need to login manually first.")
    print("The agent will reuse your session from ./browser_data\n")
    
    config = Config.from_env()
    config.headless = False  # Show browser for login
    agent = Agent(config)
    
    success = agent.run(
        task="""
        Search for 'Python developer' jobs in Moscow.
        Find the first three job listings and note their titles.
        When done, mark the task as complete.
        """,
        starting_url="https://hh.ru"
    )
    
    print(f"\nResult: {'✓ Success' if success else '✗ Failed'}")
    return success


def example_5_github_navigation():
    """Example: Navigate GitHub repository."""
    print("\n" + "="*60)
    print("EXAMPLE 5: GitHub Repository Navigation")
    print("="*60 + "\n")
    
    config = Config.from_env()
    agent = Agent(config)
    
    success = agent.run(
        task="""
        Go to the Playwright Python GitHub repository.
        Click on the 'README.md' file to view it.
        Then navigate back to the main repository page.
        """,
        starting_url="https://github.com/microsoft/playwright-python"
    )
    
    print(f"\nResult: {'✓ Success' if success else '✗ Failed'}")
    return success


def example_6_product_comparison():
    """Example: Compare products on e-commerce site."""
    print("\n" + "="*60)
    print("EXAMPLE 6: Product Comparison")
    print("="*60 + "\n")
    
    config = Config.from_env()
    agent = Agent(config)
    
    success = agent.run(
        task="""
        Search for 'laptop' on the site.
        Look at the first three results and note their prices.
        Then mark the task as complete.
        """,
        starting_url="https://www.amazon.com"  # Or any e-commerce site
    )
    
    print(f"\nResult: {'✓ Success' if success else '✗ Failed'}")
    return success


def example_7_social_media_interaction():
    """Example: Interact with social media (requires login)."""
    print("\n" + "="*60)
    print("EXAMPLE 7: Social Media Interaction")
    print("="*60 + "\n")
    
    print("NOTE: You need to login manually first.")
    print("Run this with headless=false and login, then rerun.\n")
    
    config = Config.from_env()
    config.headless = False
    agent = Agent(config)
    
    success = agent.run(
        task="""
        Go to your Twitter/X feed.
        Scroll down to see more posts.
        Then mark the task as complete.
        """,
        starting_url="https://twitter.com/home"
    )
    
    print(f"\nResult: {'✓ Success' if success else '✗ Failed'}")
    return success


def run_custom_task():
    """Run a custom task from user input."""
    print("\n" + "="*60)
    print("CUSTOM TASK")
    print("="*60 + "\n")
    
    task = input("Enter task description: ").strip()
    if not task:
        print("No task provided, skipping.")
        return False
    
    starting_url = input("Enter starting URL (optional): ").strip() or None
    
    config = Config.from_env()
    agent = Agent(config)
    
    success = agent.run(task=task, starting_url=starting_url)
    
    print(f"\nResult: {'✓ Success' if success else '✗ Failed'}")
    return success


def main():
    """Main menu for examples."""
    examples = {
        '1': ('Google Search', example_1_google_search),
        '2': ('Wikipedia Navigation', example_2_wikipedia_navigation),
        '3': ('Form Filling', example_3_form_filling),
        '4': ('Job Search (HH.ru)', example_4_job_search),
        '5': ('GitHub Navigation', example_5_github_navigation),
        '6': ('Product Comparison', example_6_product_comparison),
        '7': ('Social Media', example_7_social_media_interaction),
        '8': ('Custom Task', run_custom_task),
    }
    
    print("\n" + "="*60)
    print("AUTONOMOUS BROWSER AGENT - EXAMPLES")
    print("="*60)
    print("\nAvailable examples:")
    
    for key, (name, _) in examples.items():
        print(f"  {key}. {name}")
    
    print("  0. Exit")
    
    while True:
        choice = input("\nSelect example (0-8): ").strip()
        
        if choice == '0':
            print("Goodbye!")
            break
        
        if choice in examples:
            name, func = examples[choice]
            try:
                func()
            except KeyboardInterrupt:
                print("\n\nTask interrupted by user")
            except Exception as e:
                print(f"\nError running example: {e}")
        else:
            print("Invalid choice, please try again.")


if __name__ == "__main__":
    main()
