# Pepper — LLM Strategy

## Core Principle

The model is a commodity. The context is the moat.

LLM capabilities are converging toward zero cost. The irreplaceable investment is the accumulated personal context — years of your life, decisions, patterns, and relationships that no external model ever sees.

**Design rule**: the model layer is always swappable. Pepper never depends on a specific model.

---

## Two-Tier Architecture

### Tier 1: Local (Ollama) — Default for all personal data

| Property | Value |
| --- | --- |
| Runs on | Your machine — Apple Silicon |
| Data policy | Raw personal data never leaves the machine |
| Cost | Zero after hardware |
| Latency | Seconds per response on M3/M4 Max |
| Use for | iMessage reading, email summarization, health data, finance, routine retrieval, background agents, continuous monitoring |

**Current recommended models** (update as better models release):

- `hermes-4.3-36b-tools:latest` — deep reasoning model for background agents and complex local tasks
- `llama3.3:70b` — strong alternative, very capable
- `hermes-4.3-36b-tools:latest` — specifically fine-tuned for agentic tool calling; preferred for Pepper Core orchestration
- `nomic-embed-text` — local embeddings for vector search

**Hardware baseline**:

- M3 Max / M4 Max with 64GB+ unified memory: runs 70B models at usable speed
- M2 Max with 32GB: runs 30B models well, 70B possible but slower
- M3 Pro / M4 Pro with 36GB: runs 32B models comfortably

As hardware improves (M5, dedicated AI chips), local inference quality increases for free.

---

### Tier 2: Frontier API (Claude) — For reasoning quality

| Property | Value |
| --- | --- |
| Provider | Anthropic Claude |
| Data policy | **Summaries and structured outputs only** — never raw personal data |
| Cost | ~$20-50/month for personal EA use |
| Latency | Subsecond for most responses |
| Use for | Complex family conversations, difficult decision analysis, high-quality drafting, nuanced advice |

**The data contract**: before any call to the Claude API, the data pipeline produces a structured summary:

```text
Raw iMessage thread → local LLM → "3 recent conversations about X, tone positive, open item Y"
                                   ↑
                              This is what Claude sees
```

Claude reasons about summaries, not raw personal data. This preserves privacy while accessing frontier reasoning quality.

**Fallback**: if offline or API unavailable, Tier 1 handles everything at reduced quality. Pepper degrades gracefully.

---

## Model Selection Logic

```python
def select_model(task_type: str, data_sensitivity: str) -> str:
    if data_sensitivity == "raw_personal":
        # iMessage, email bodies, health metrics, financial transactions — never leave the machine
        return f"local/{DEFAULT_LOCAL_MODEL}"

    if task_type in ["family_conversation", "difficult_decision", "high_stakes_draft"]:
        # Frontier reasoning; caller must ensure only summaries are sent
        return DEFAULT_FRONTIER_MODEL

    if task_type in ["routine_retrieval", "scheduling", "reminders"]:
        return f"local/{DEFAULT_LOCAL_MODEL}"

    if task_type == "background_agent":
        # Deep reasoning via frontier model (always local per DEFAULT_FRONTIER_MODEL)
        return DEFAULT_FRONTIER_MODEL

    # Default: local
    return f"local/{DEFAULT_LOCAL_MODEL}"
```

---

## Abstraction Layer

All model calls go through a single unified interface. Pepper never calls Ollama or Anthropic directly:

```python
class ModelClient:
    def chat(self, messages, tools=None, model=None):
        model = model or config.default_model
        
        if model.startswith("local/"):
            return self._ollama_chat(model, messages, tools)
        elif model.startswith("claude"):
            return self._anthropic_chat(model, messages, tools)
    
    def embed(self, text):
        # Always local — embeddings never go to API
        return self._local_embed(text)
```

Swapping from `hermes-4.3-36b-tools:latest` to a future `hermes4:100b` is one config line. The rest of Pepper doesn't change.

---

## Embedding Strategy

Embeddings are always local. No text is ever sent to an external embedding API.

- **Model**: `nomic-embed-text` via Ollama (384 or 768 dimensions)
- **Storage**: pgvector with HNSW index
- **What gets embedded**: conversation summaries, notes, decisions, life context sections, contact profiles
- **Re-embedding**: when a better local embedding model becomes available, maintenance agents re-index

---

## Model Upgrade Process

Maintenance agents handle model evaluation and upgrades:

1. **Weekly check**: query Ollama registry for new model releases
2. **Benchmark**: run a standardized set of test prompts against new model
3. **Compare**: score against current model on: tool calling accuracy, reasoning quality, response format adherence
4. **Propose**: if new model scores better, open a PR updating `config.yaml`
5. **Review**: human reviews the PR (single config line change)
6. **Deploy**: merge triggers a model pull via Ollama; zero downtime

This is the "upgrades to the next best thing" loop — managed by agents, approved by humans.

---

## Cost Management

At typical personal EA usage (morning brief, a few queries daily, pre-meeting briefs):

- **Local tier**: zero marginal cost
- **Frontier tier**: ~$10-30/month for Claude API calls on summaries

Cost controls:

- Route to local by default; only escalate to frontier when reasoning quality matters
- Cache frontier API responses for identical or near-identical queries
- Background agents (security, maintenance, monitoring) always use local models
- Hard monthly spend limit in config: if exceeded, fall back to local for everything

---

## The Long View

**12-18 months**: 70B local models reach quality parity with today's frontier models for most personal assistant tasks. Frontier API usage becomes optional rather than necessary.

**2-3 years**: Local multimodal models. Pepper can see your screen, understand voice, process images — all locally. The "ambient Pepper" becomes feasible without cloud dependency.

**5 years**: The gap between local and frontier is narrow enough that the privacy trade-off almost never makes sense. Pepper becomes a fully local system.

The architecture built today is designed to ride this curve without structural changes.
