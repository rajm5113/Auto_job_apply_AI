import json
import os
import re
from patchright.async_api import Page
from utils.llm_client import LLMClient
from utils.dom_simplifier import simplify_html
from rich.console import Console

console = Console()
PATCHES_FILE = "data/ui_patches.json"

class AdaptiveLocator:
    def __init__(self, page: Page, llm_client: LLMClient):
        self.page = page
        self.llm = llm_client
        self.patches = self._load_patches()

    def _load_patches(self) -> dict:
        if os.path.exists(PATCHES_FILE):
            with open(PATCHES_FILE, "r") as f:
                return json.load(f)
        return {}

    def _save_patch(self, action_id: str, new_selector: str):
        self.patches[action_id] = new_selector
        os.makedirs(os.path.dirname(PATCHES_FILE), exist_ok=True)
        with open(PATCHES_FILE, "w") as f:
            json.dump(self.patches, f, indent=2)

    async def get_selector(self, action_id: str, default_selector: str, hint_description: str) -> str:
        """
        Attempts to use the known selector (patched or default). 
        If it fails, it simplifies the DOM, asks the LLM for a new selector, and caches it.
        """
        active_selector = self.patches.get(action_id, default_selector)
        
        # Test if active selector currently exists on page
        element = await self.page.query_selector(active_selector)
        if element:
            return active_selector
        
        console.print(f"[yellow][ADAPTIVE] Selector '{active_selector}' for '{action_id}' failed. Asking LLM for a fallback...[/yellow]")
        
        # 1. Capture HTML
        raw_html = await self.page.content()
        clean_html = simplify_html(raw_html)
        
        # Prevent massive HTML from blowing up context window
        if len(clean_html) > 15000:
            clean_html = clean_html[:15000] + "..."

        # 2. Ask LLM
        prompt = f"""You are an expert web automation engineer. The UI has changed.
We need to find the CSS selector for a specific element.

Goal Description: {hint_description}

Here is the simplified DOM structure of the current page:
```html
{clean_html}
```

Analyze the DOM carefully. Return ONLY a valid JSON string mapping exactly to this structure:
{{
    "css_selector": "the strictly unique CSS selector you found"
}}
No other explanation, markdown, or chat text."""

        try:
            bot_response = self.llm.complete(prompt)
        except Exception as e:
            console.print(f"[red][ADAPTIVE] LLM fallback completely failed: {e}. Falling back to default selector.[/red]")
            return default_selector
        
        # 3. Clean up JSON response
        try:
            bot_response = bot_response.strip()
            if bot_response.startswith("```"):
                bot_response = bot_response.split("```")[1]
                if bot_response.startswith("json"):
                    bot_response = bot_response[4:]
            
            data = json.loads(bot_response.strip())
            new_selector = data.get("css_selector", "")
        except Exception as e:
            console.print(f"[red][ADAPTIVE] Failed to parse LLM JSON: {e}[/red]")
            return default_selector # Fallback to default and pray
        
        # 4. Cache and return
        if new_selector:
            console.print(f"[green][ADAPTIVE] Learned new selector for '{action_id}': {new_selector}[/green]")
            self._save_patch(action_id, new_selector)
            return new_selector
            
        return default_selector
