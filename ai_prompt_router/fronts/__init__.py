"""Caller-facing transports (fronts) for the router.

Each front is an entry point that ends up calling `Router.forward_prompt()`; the
upstream side is always HTTP. Modules here are imported lazily by __main__ so the
router still starts when a front's optional dependency is missing.
"""
