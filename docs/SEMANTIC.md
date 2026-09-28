# Semantic reading — the embedding backend

Register (warmth, moralising, distancing, refusal and expressed agency) is read two ways:

* **The lexicon** matches fixed phrases. It is precise, but its recall on natural prose is
  poor. A warm reply that never says "let's" reads as neutral, and a novel refusal that never
  says "I can't" is missed.
* **A semantic embedding** reads meaning. Each dimension is an axis between exemplar
  sentences (`corpus/register_anchors.toml`), and a reply is placed by what it means, not by
  the words it uses.

With nothing chosen, the Explorer reads with a stdlib fallback. The fallback is a bag of
surface forms: honest, lexical, and **never trusted**. Choosing a real backend is what lets
drift alerts, the Conversations triage and the Live view see register shifts that use no
canonical phrase.

## Choosing one

In the app, open **Semantic reading**. You can reach it from the *register* chip on the
Overview, from ⌘K → *Semantic reading*, or from the "Register read via the lexicon" item in
*Needs a look*. In a terminal:

```bash
explorer embed                          # what is available here, and what is active
explorer embed pull bge-m3              # download a model into a running Ollama
explorer embed test ollama:bge-m3       # run the controls; change nothing
explorer embed use ollama:bge-m3        # make it active (only if it passes)
explorer embed key voyage               # store a cloud API key (owner-only file)
```

| Backend | Spec | Where the text goes | Notes |
|---|---|---|---|
| **Ollama** (recommended) | `ollama:bge-m3` | nowhere — this computer | Free and private. `bge-m3` covers 100+ languages and long inputs; `nomic-embed-text` and `mxbai-embed-large` are English. Install [Ollama](https://ollama.com/download); the panel downloads the model with one click. |
| LM Studio | `lmstudio:<model>` | nowhere — this computer | Any embedding model loaded in LM Studio's local server. |
| OpenAI | `openai:text-embedding-3-large` | OpenAI | Needs `OPENAI_API_KEY` or a key saved in the panel. |
| Voyage AI | `voyage:voyage-3.5` | Voyage AI | The embeddings provider Anthropic recommends. Needs `VOYAGE_API_KEY` or a saved key. |
| Any OpenAI-compatible server | `compat:<model>@<base url>` | that server | llama.cpp, vLLM, hosted providers. Optional `EXPLORER_COMPAT_API_KEY`. |
| sentence-transformers | `minilm` | nowhere | Source installs with `pip install '.[embeddings]'`. PyTorch is too large to ship inside the app. |

None of these needs a Python package: they talk to the model over HTTP with the standard
library. The self-updating app therefore gets them as an ordinary code update, with no new
download.

The choice is saved in `embedding.json` beside the database. `EXPLORER_EMBED_BACKEND`, when
set, still overrides it.

## Tested before it is used — per language

A backend's own claim to be "semantic" is never taken on trust. Before a backend becomes
active, the register controls run on it:

* **Exemplar coherence:** a leave-one-out check that the exemplars separate under this backend.
* **Generalization:** held-out probe sentences that share **no content words** with the
  exemplars must land on the correct side of every axis, by a margin of at least 0.05. A
  backend that only matches surface forms scores near zero, and fails.
* **Generalization in French, Spanish and Japanese** (the corpus's languages): the same
  probes, translated one for one, are projected onto the **English** axes. A multilingual
  model places them correctly. An English-only model does not, and it is **not trusted in
  that language**. A Japanese conversation is then read by nothing rather than by a reading
  nobody validated.

A backend that fails in English cannot be made active (the panel's "use anyway" keeps its
readings marked untrusted). The languages a backend passed are saved with the choice, and
every reading carries `trustworthy` for the language of the text it read.

Imported conversations are tagged with their language when they arrive. In a language with
no validated lexicon, a conversation's drift runs on the embedding reading alone, provided
the backend is trusted in that language.

## What happens when you switch

* Every reading uses the new backend at once; no restart is needed.
* Every conversation's triage (the flags in Conversations) is re-read in the background. The
  cached triage is versioned by the backend and the languages it is trusted in.
* Vectors are cached in `embeddings.sqlite` beside the database, keyed by model and text. So
  nothing is embedded twice, re-reading a history is cheap, and switching back to a backend
  used before costs nothing. Deleting the file loses only time.

## If the backend goes away

If Ollama is stopped, the network drops or a key is revoked, the Explorer does not hang or
crash. Readings fall back to the untrusted lexicon, the panel says why, and the chosen
backend is retried every minute. Conversations read in the meantime are marked so they are
re-read once it is back.

## Privacy

Local backends (Ollama, LM Studio) keep everything on this computer. Cloud backends send
the text of each reply being read to the provider named in the panel. That destination is
shown before you choose, and again beside the test result. API keys live in `secrets.json`
beside the database, readable only by you (mode 0600). They are never sent back to the page,
and a chat site can never reach these settings.
