"""How an agent says "I can't resolve this": it calls a tool.

A text marker at the end of the reply was tried first, but the model added it even to good
answers. A tool call is more deliberate, and the `reason` goes to the human.
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
