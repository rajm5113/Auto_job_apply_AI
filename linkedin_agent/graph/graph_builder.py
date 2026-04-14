from langgraph.graph import StateGraph, END
from graph.state import AgentState
from agents.resume_agent import resume_node, domain_confirm_node
from agents.scraper_agent import scraper_node
from agents.scorer_agent import scorer_node
from agents.applier_agent import applier_node
from agents.logger_agent import logger_node


def error_node(state: AgentState) -> dict:
    from rich.console import Console
    Console().print(
        f"\n[red bold][ERROR] Pipeline stopped: {state.get('error')}[/red bold]"
    )
    return {}


def after_resume(state: AgentState) -> str:
    return "error_handler" if state.get("error") else "domain_confirm"

def after_scrape(state: AgentState) -> str:
    return "error_handler" if state.get("error") else "scorer"

def after_score(state: AgentState) -> str:
    return "error_handler" if state.get("error") else "applier"

def after_apply(state: AgentState) -> str:
    # Always proceed to logger even if some applications failed
    # Logger must run to persist results to Sheets
    if state.get("error"):
        return "error_handler"
    return "logger_node"

def after_log(state: AgentState) -> str:
    return END


def build_graph():
    graph = StateGraph(AgentState)

    # All five phases wired in
    graph.add_node("resume_parser",  resume_node)
    graph.add_node("domain_confirm", domain_confirm_node)
    graph.add_node("scraper",        scraper_node)
    graph.add_node("scorer",         scorer_node)
    graph.add_node("applier",        applier_node)
    graph.add_node("logger_node",    logger_node)
    graph.add_node("error_handler",  error_node)

    # Entry point
    graph.set_entry_point("resume_parser")

    # Edges
    graph.add_conditional_edges(
        "resume_parser", after_resume,
        {"error_handler": "error_handler", "domain_confirm": "domain_confirm"}
    )
    graph.add_edge("domain_confirm", "scraper")
    graph.add_conditional_edges(
        "scraper", after_scrape,
        {"error_handler": "error_handler", "scorer": "scorer"}
    )
    graph.add_conditional_edges(
        "scorer", after_score,
        {"error_handler": "error_handler", "applier": "applier"}
    )
    graph.add_conditional_edges(
        "applier", after_apply,
        {"error_handler": "error_handler", "logger_node": "logger_node"}
    )
    graph.add_conditional_edges(
        "logger_node", after_log,
        {END: END}
    )

    return graph.compile()
