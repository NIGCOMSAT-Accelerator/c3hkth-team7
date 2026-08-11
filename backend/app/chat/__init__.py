"""Herald's conversational surface.

A subscriber who receives "31% of your cropland is under standing water, harvest
low-lying plots first" reasonably asks *why*, *how sure are you*, and *what does
waterlogging actually do to rice*. This answers that.

Lives beside the Herald rather than inside `HeraldAgent` because the contracts
differ: `HeraldAgent.run()` is a queue-consuming batch stage that must not raise,
while chat is request-response with a per-turn budget and a session. Same domain,
different shape.
"""

from app.chat.service import answer, get_or_create_session, history

__all__ = ["answer", "get_or_create_session", "history"]
