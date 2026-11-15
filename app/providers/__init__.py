"""Provider ports and adapters.

Domain code imports the ports from `app.providers.base`; only wiring imports an
adapter. Image and Video stay mock-only until a real adapter exists, so nothing
here advertises a model whose provider is not configured.
"""
