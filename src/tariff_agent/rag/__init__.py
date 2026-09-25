"""The retrieval layer: finding the passages that state a tariff field.

Chunks official documents, indexes them lexically and (when a key is available)
semantically, and answers one question per tariff field: *which passages state
this, and are any of them actually relevant?*

The second half of that question matters as much as the first. A retriever that
always returns its nearest neighbour will supply plausible evidence for a field
the bank does not offer, and the extraction step will dutifully quote it.
"""
