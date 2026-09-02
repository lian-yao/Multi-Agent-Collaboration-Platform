from typing import Annotated, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.config import AgentSettings, get_settings
from app.orchestration.llm import build_chat_model


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def build_langgraph_agent(
    llm: BaseChatModel | None = None,
    settings: AgentSettings | None = None,
):
    """构建单 Agent 问答图：START -> agent -> END。"""

    model = llm or build_chat_model(settings or get_settings())

    def agent_node(state: AgentState) -> dict:
        response = model.invoke(state["messages"])
        return {"messages": [response]}

    builder = StateGraph(AgentState)
    builder.add_node("agent", agent_node)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    return builder.compile()


def run_agent(
    user_input: str,
    llm: BaseChatModel | None = None,
    settings: AgentSettings | None = None,
) -> list[AnyMessage]:
    agent = build_langgraph_agent(llm=llm, settings=settings)
    result = agent.invoke({"messages": [HumanMessage(content=user_input)]})
    return result["messages"]
