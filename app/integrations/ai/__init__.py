"""AI provider adapters.

One module per provider, each implementing ``app.core.ai.LLMProvider``.
Application code never imports one of these directly -- it calls ``get_llm()``,
which selects an adapter from ``LLM_PROVIDER``. Importing an adapter by name
reintroduces exactly the coupling the port exists to remove, and
``tests/test_ai.py`` fails the build if any module outside this package does it.

    stub.py               deterministic, no network -- the default
    foundry.py            Microsoft Foundry
    openai_compatible.py  any OpenAI-compatible endpoint -- the alternate
    local_forecast.py     in-process Croston/SBA, behind the Forecaster port
"""
