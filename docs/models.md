# Model connections, and the knobs on them

A **connection** is a provider plus a model plus a credential: `openai/gpt-5.6-luna`,
`ollama/qwen3.8:27b`, `anthropic/claude-sonnet-5`. Personas bind to one. Nothing else in
the product holds a provider key — `credential_ref` points into sealed storage, never at
the key itself.

## Where a value comes from

Four layers, most specific first:

```
the request ->  the persona ->  the connection ->  the platform default
```

An explicit request field always wins. Below that, a persona's own params override its
connection's, and the connection overrides whatever the deployment set. A platform default
is the floor, never the ceiling — a connection that names `num_ctx` means it.

## What you can set

Anything the provider accepts goes through, so this is not a fixed list. The ones that
earn their keep:

| Key | Why you would set it |
|---|---|
| `temperature` | The main lever on voice. See below. |
| `presence_penalty`, `frequency_penalty` | Punish re-emitting what is already in the transcript. |
| `seed` | Reproducibility, where the provider honours it. |
| `max_tokens` | Completion budget. Also what the empty-generation retry multiplies, so setting it gives that repair something to work with. |
| `reasoning_effort` | On reasoning models. Set it deliberately and the platform stops trying to manage it for you. |
| `num_ctx` | Ollama only, and important: its factory default of 4096 makes real prompts return **empty generations silently**. The platform forces 16384 unless you say otherwise. |
| `history_char_budget` | How much transcript this model is given. A local 8B on a laptop and a hosted frontier model do not want the same number. |
| `knowledge_token_budget` | How much retrieved knowledge this model is given per turn, overriding both the flow's number and the platform's guess from the model's context window. Set it when you have measured your own model, or when you run an OpenAI-compatible endpoint whose model name nothing recognises. |

A few keys are dropped at call time because the platform manages them: `model`, `messages`, `tools`,
`api_key`, `api_base`, `stream`, `response_format`, `n`. Letting a convenience knob
reroute a call would make it something else entirely.

## Distinct voices on one connection

The reason per-persona params exist. Five personas sharing one model converge: by round
three they trade each other's sentences verbatim, because they are one model reading one
transcript. Spread them:

```
Referee temperature 0.4 cool, deterministic
Suspect A temperature 0.9 presence_penalty 0.4 blusters, won't repeat itself
Suspect B temperature 0.6 presence_penalty 0.5 tight-lipped, penalised hardest for echoes
```

`presence_penalty` is doing the targeted work there — it directly punishes re-emitting
tokens already in the context, which is the mechanism behind one character's line
migrating into another's mouth.

Set them in the persona editor (**Generation overrides**, JSON) or on the connection for
everyone who shares it. They travel in `.pyr`, so a bundle carries its cast's voices.

## When a model refuses something

Providers disagree about which parameters exist, and the adapter repairs what it can
rather than failing a turn:

- a parameter the endpoint refuses **while naming a replacement** is **renamed**, and its
  value carried over. OpenAI's newer chat-completions models answer `max_tokens` with
  *"Unsupported parameter: 'max_tokens' … Use 'max_completion_tokens' instead"*; the
  budget moves to the name they asked for rather than being thrown away, because usage
  metering, cost ceilings and the empty-generation retry all read it;
- a parameter the endpoint names as unsupported with no replacement is **dropped**, and
  the call retried;
- an endpoint that refuses function tools while a reasoning effort is set is retried with
  `reasoning_effort: "none"` — unless you chose an effort yourself, which is treated as
  deliberate;
- an empty generation (a reasoning model spending its whole budget on thinking) is retried
  with a **larger** budget at the **same** reasoning level, so a high-effort experiment
  stays high-effort.

Repairs chain: a request carrying both an unsupported penalty and a reasoning effort is
refused twice, and repaired twice. What the endpoint accepted is then reused — a retry
starts from the repaired call, not the original, so one rejected request per turn is not
paid twice.

A repair costs one rejected request the first time a connection is used in a process;
the adapter learns from the endpoint rather than from a list of model names, which is
what lets a model released after this version still work.

## Which model runs what

Not everything uses a persona's connection:

- **Turns** use the acting persona's connection.
- **The disclosure gate** uses the tenant's chosen gate connection, falling back to the
  acting persona's. It is a strict-JSON classifier over gists — it wants schema
  discipline, not the persona's weight class, and pointing it away from a big local model
  keeps a ~1s judgement from queueing behind a 7B on one GPU.
- **Moderation** resolves per workspace, then tenant (`moderation_model`). Unset
  everywhere means content is not screened.
- **The workspace assistant** has its own connection, created empty and filled in on
  the "Assistant model" profile in the personas UI.
- **The admin assistant** uses a connection on the reserved admin organization, so console
  questions are not billed to a tenant.
- **Embedding and reranking** are deployment-wide by necessity: every tenant's vectors sit
  in one column of one width. Admin → Models.
