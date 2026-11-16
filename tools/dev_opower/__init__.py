"""Dev-only stand-in for the Opower integration (tools/seed_dev.py).

Recreates the source instance's Opower sensors, with their unique IDs, from
``opower_seed.json`` in the config directory, so they get entity registry entries owned
by the ``opower`` platform the way the real integration's do.
"""
