import functools
import time
import asyncio
from typing import Any, Callable, Dict
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from rich.console import Console
from graph.state import AgentState
from utils.logger import Logger
import config

console = Console()

def resilient_node(phase_name: str):
    """
    LangGraph Node Decorator: 
    - Automatically handles setup of Logger.
    - Captures all exceptions and routes them to the state['error'].
    - Ensures the node returns a valid dictionary update.
    """
    def decorator(func: Callable):
        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(state: AgentState, *args, **kwargs) -> Dict[str, Any]:
                logger = Logger(config.DB_PATH, state.get("run_id", "unknown"))
                logger.info(phase_name, f"Starting phase: {phase_name}...")
                start_time = time.time()
                try:
                    result = await func(state, *args, **kwargs)
                    duration = time.time() - start_time
                    logger.info(phase_name, f"Completed in {duration:.2f}s")
                    return result
                except Exception as e:
                    logger.error(phase_name, f"Critical failure: {str(e)}")
                    return {"error": f"[{phase_name}] {str(e)}", "current_phase": "error"}
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(state: AgentState, *args, **kwargs) -> Dict[str, Any]:
                logger = Logger(config.DB_PATH, state.get("run_id", "unknown"))
                logger.info(phase_name, f"Starting phase: {phase_name}...")
                start_time = time.time()
                try:
                    result = func(state, *args, **kwargs)
                    duration = time.time() - start_time
                    logger.info(phase_name, f"Completed in {duration:.2f}s")
                    return result
                except Exception as e:
                    logger.error(phase_name, f"Critical failure: {str(e)}")
                    return {"error": f"[{phase_name}] {str(e)}", "current_phase": "error"}
            return sync_wrapper
    return decorator

def human_retry(attempts: int = 3):
    """
    Action Decorator:
    - Retries a function if it fails.
    - Perfect for network requests or LLM calls.
    - Uses exponential backoff (2s, 4s, 8s...).
    """
    return retry(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=2, min=2, max=10),
        before_sleep=lambda retry_state: console.print(
            f"[yellow][RETRY][/yellow] Attempt {retry_state.attempt_number} failed. Retrying in {retry_state.upcoming_sleep}s..."
        ),
        reraise=True
    )

def log_action(action_desc: str):
    """
    Utility Decorator:
    - Simply logs the start and end of high-level actions for better CLI monitoring.
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            console.print(f"[bold blue][ACTION][/bold blue] {action_desc}...")
            res = await func(*args, **kwargs)
            console.print(f"[bold green][SUCCESS][/bold green] Finished {action_desc}.")
            return res
        return wrapper
    return decorator
