# adapters/models/ollama/ — intentionally empty

Ollama is reached through LiteLLM's `ollama/<model>` prefix (see
`adapters/models/litellm/provider.py`), not a separate client. There is nothing
Ollama-specific to implement here unless LiteLLM's Ollama support proves insufficient.
