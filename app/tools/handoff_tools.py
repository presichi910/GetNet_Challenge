"""The agents' way to say "I cannot resolve this": an explicit tool call.

An earlier design used a text marker appended to the reply. Measured on the evaluation set it was
over-applied (the model appended it even after a perfect answer, e.g. a sourced exchange rate),
because a trailing marker reads like a mandatory output format. A tool call is a deliberate action,
and its `reason` argument travels to the human operator.
"""
from langchain_core.tools import tool

ESCALATE_TOOL = "escalate_to_human"


@tool
def escalate_to_human(reason: str) -> str:
    """Hand this case to a human agent. Call it ONLY after trying your other tools and still being
    unable to address the request (the information does not exist in your sources, the account was
    not found, or tools keep failing). Do NOT call it when you answered the question, even partially
    or with caveats. `reason`: one short sentence for the human operator."""
    return ("Escalation registered: a human agent will take over this case. Briefly tell the user you "
            "could not resolve it yourself. Do not state any unverified information.")
