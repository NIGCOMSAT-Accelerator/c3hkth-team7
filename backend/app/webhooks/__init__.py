"""Webhook subscription engine — the business-integration surface.

`engine.py`     signing, filtering, retry policy. Pure functions, no I/O.
`store.py`      subscription CRUD and the delivery ledger.
`publisher.py`  fan-out on a pipeline event, and the retry sweep.

Separate from `app/dispatch/webhook.py`, which delivers one alert to one farmer's
chosen channel with no retry. See `engine.py` for why both exist.
"""

from __future__ import annotations
