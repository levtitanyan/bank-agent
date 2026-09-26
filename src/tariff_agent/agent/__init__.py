"""The ADK agent: tools, session state, metrics, and the agent itself.

Everything the model can do lives behind the six functions in
:mod:`tariff_agent.agent.tools`. The model never receives a URL, a filesystem
path or a document's bytes - only ids minted by earlier tools, and summaries
small enough to reason about.
"""
