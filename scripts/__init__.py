"""Operational / one-off scripts for AI Shuffle.

Modules here are runnable with ``python -m scripts.<name>`` from the repo root.
They are NOT imported by the application at runtime — they bootstrap the DB
pool themselves (``app.db.base.init_pool``) and are meant to be run manually,
coordinated with a human operator.
"""
