"""The only part of the project permitted to open a network connection.

Everything downstream - discovery, document processing, RAG, extraction - works
on bytes this package has already declared safe: on an allow-listed host, over
https, within the size cap, and of the expected content type.

The LLM never passes a URL in here. Agent tools take ids produced by earlier
tools, and a URL reaches this layer only after discovery has checked it against
the allowlist.
"""
