"""Web search, for verification and chat context.

Deliberately NOT part of the Scout → Analyst → Oracle path. Those stages consume
typed geospatial and meteorological adapters whose numbers are reproducible; web
results are unattributed prose of unknown recency, and feeding them into a risk
score would break both `test_oracle.py` and the "never invent data" rule the whole
pipeline is built on.

Two callers, both outside that path:

* `agents/fahis.py`   — after-the-fact verification. Writes to agent_memory.
* `chat/`             — explains an alert a subscriber already received.
"""

from app.search.client import SearchResult, SearchUnavailable, available, search

__all__ = ["SearchResult", "SearchUnavailable", "available", "search"]
