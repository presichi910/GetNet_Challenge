from datetime import date

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.data.repository import CustomerRepository


@pytest.fixture
def repo(tmp_path):
    return CustomerRepository(tickets_file=tmp_path / "tickets.jsonl", today=date(2026, 1, 15))


class ToolFakeModel(GenericFakeChatModel):
    """Fake chat model that accepts `bind_tools`."""

    def bind_tools(self, tools, **kwargs):
        return self


def scripted(*messages: AIMessage) -> ToolFakeModel:
    return ToolFakeModel(messages=iter(messages))
