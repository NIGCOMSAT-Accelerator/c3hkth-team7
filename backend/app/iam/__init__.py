"""IAM — identity, onboarding, and API keys for both account kinds.

`models.py`    account/key contracts. Individual vs commercial is a security boundary.
`security.py`  Argon2id passwords, JWT sessions, API-key material. Pure functions.
`store.py`     MongoDB Atlas. Separate from Postgres so a database compromise there
               cannot authenticate as anyone.
`deps.py`      the three guards: portal session, aggregator API key, scope check.
`mailer.py`    onboarding email over the same SMTP relay the alert channel uses.
`routes.py`    the HTTP surface.
"""

from __future__ import annotations
