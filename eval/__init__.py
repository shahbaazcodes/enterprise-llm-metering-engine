"""Offline evaluation suite for the LLM metering & billing engine.

The suite is driven by ``eval/eval_set.json`` - the *scenario matrix*.  It
declares the tenants, the price cards, the token-count shapes and the probe
list that the tests are expected to cover.

Every probe is deterministic and hermetic:

* the database is a throw-away SQLite file created from the ORM metadata,
* Redis is replaced by ``fakeredis`` running the **real** Lua interpreter
  (``lupa``), so the atomic scripts behave exactly as they do in production,
* the tenants, plans and price cards are seeded from the scenario matrix.

No Docker daemon, no PostgreSQL instance and no network access are needed -
``pytest`` alone is enough::

    pytest                    # run every probe
    pytest -k "P4 or quota"   # run a subset
"""
