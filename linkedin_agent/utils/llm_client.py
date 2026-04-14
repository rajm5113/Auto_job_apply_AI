"""
Multi-Model LLM Client — Role-based model routing.

Each agent role gets its own optimized model chain:
  scorer      → 8B (fast, cheap)   → 20B → 70B
  form_filler → 70B (reliable JSON)→ 120B → 20B
  writer      → 120B (best quality)→ 70B  → 20B
  parser      → 70B (reliable JSON)→ 120B → 20B
  locator     → 8B (fast)          → 20B
  default     → 70B → 20B → 8B
"""

from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage
from utils.decorators import human_retry
from rich.console import Console
import config

console = Console()


class LLMError(Exception):
    pass


# ── Model definitions ────────────────────────────────────────────────────────
# Each tuple: (model_id, max_prompt_chars, display_name)
MODELS = {
    "8b":   ("llama-3.1-8b-instant",       6000,  "Llama 8B"),
    "20b":  ("openai/gpt-oss-20b",         10000, "GPT-OSS 20B"),
    "70b":  ("llama-3.3-70b-versatile",    12000, "Llama 70B"),
    "120b": ("openai/gpt-oss-120b",        14000, "GPT-OSS 120B"),
}

# ── Role → model chain mapping ───────────────────────────────────────────────
ROLE_CHAINS = {
    "scorer":      ["8b", "20b", "70b"],
    "form_filler": ["70b", "120b", "20b"],
    "writer":      ["120b", "70b", "20b"],
    "parser":      ["70b", "120b", "20b"],
    "locator":     ["8b", "20b"],
    "default":     ["70b", "20b", "8b"],
}


class LLMClient:
    """
    Role-aware LLM client. Each role has its own fallback chain.

    Usage:
        llm = LLMClient(role="scorer")
        result = llm.complete("Score this job...")
    """

    # Class-level cache for instantiated ChatGroq objects (shared across roles)
    _groq_cache: dict = {}

    # Optional Gemini — only used when USE_GEMINI=true in .env
    _gemini_instance = None
    _gemini_exhausted: bool = False

    def __init__(self, role: str = "default", logger=None):
        self.role = role
        self.chain = ROLE_CHAINS.get(role, ROLE_CHAINS["default"])
        self.logger = logger

    def _get_groq(self, key: str) -> ChatGroq:
        """Get or create a cached ChatGroq instance for a model key."""
        if key not in LLMClient._groq_cache:
            model_id, _, display = MODELS[key]
            LLMClient._groq_cache[key] = ChatGroq(
                model=model_id,
                api_key=config.GROQ_API_KEY,
            )
        return LLMClient._groq_cache[key]

    def _get_gemini(self):
        """Lazy-init Gemini (only if USE_GEMINI is enabled)."""
        if LLMClient._gemini_instance is None:
            from langchain_google_genai import ChatGoogleGenerativeAI
            LLMClient._gemini_instance = ChatGoogleGenerativeAI(
                model="gemini-2.5-flash",
                google_api_key=config.GEMINI_API_KEY,
            )
        return LLMClient._gemini_instance

    @human_retry(attempts=2)
    def complete(self, prompt: str) -> str:
        """
        Execute the prompt against this role's model chain.
        Tries each model in order; skips on rate-limit or error.
        """
        # ── Optional Gemini (only if user opted in) ──────────────────────
        if getattr(config, "USE_GEMINI", False) and not LLMClient._gemini_exhausted:
            try:
                result = self._get_gemini().invoke(
                    [HumanMessage(content=prompt)]
                ).content
                return result
            except Exception as e:
                err = str(e).lower()
                if "429" in err or "quota" in err or "resource_exhausted" in err:
                    LLMClient._gemini_exhausted = True
                    console.print(
                        "[yellow]Gemini quota exhausted — using Groq models.[/yellow]"
                    )
                # Fall through to Groq chain

        # ── Groq model chain (role-specific) ─────────────────────────────
        last_error = None
        for model_key in self.chain:
            model_id, max_chars, display = MODELS[model_key]

            # Truncate prompt to model's safe limit
            truncated = prompt[:max_chars] if len(prompt) > max_chars else prompt
            messages = [HumanMessage(content=truncated)]

            try:
                result = self._get_groq(model_key).invoke(messages).content
                return result
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                if "429" in err_str or "rate" in err_str:
                    console.print(
                        f"[yellow]{display} rate-limited — trying next model.[/yellow]"
                    )
                else:
                    console.print(
                        f"[yellow]{display} failed ({str(e)[:80]}) — trying next.[/yellow]"
                    )
                continue

        # All models exhausted
        console.print("[red]All LLM models failed for this call.[/red]")
        raise LLMError(
            f"All models exhausted for role '{self.role}'. "
            f"Last error: {str(last_error)}"
        )
