# JOB-CARD: Enterprise Unstructured Log & Text Enrichment Service

## Feature Specification

**Endpoint:** `POST /v1/enrich`  
**Tag:** `Enrichment`  
**Version:** v1  
**Status:** Design Complete — Ready for Implementation  

---

## Purpose

Provide a schema-validated, production-grade service that transforms unstructured text (logs, tickets, documents, chat transcripts) into structured enrichment records. The service must be deterministic, auditable, and resilient to upstream LLM failures.

---

## Input Contract

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `raw_text` | `string` | **Required**, 20–4000 chars | The unstructured text to enrich. |
| `source_url` | `string \| null` | Optional, max 2048 chars | Provenance URL for audit trail. |

**Example Request:**
```json
{
  "raw_text": "ERROR 2024-01-15 14:32:11 Connection timeout to database primary after 30s. Retrying replica...",
  "source_url": "https://logs.example.com/app/abc123"
}
```

---

## Output Contract

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `category` | `enum` | **Required**, one of: `tech`, `finance`, `healthcare`, `security`, `other` | Domain classification. |
| `summary` | `string` | **Required**, max 300 chars | Concise human-readable summary. |
| `quality_score` | `float` | **Required**, 0.0–1.0 | Confidence in the enrichment. |
| `needs_human_review` | `boolean` | **Required** | True when quality_score < 0.7 or ambiguous. |
| `reasoning` | `string` | **Required**, max 200 chars | Brief justification for the classification. |

**Example Response:**
```json
{
  "category": "tech",
  "summary": "Database connection timeout with fallback to replica initiated.",
  "quality_score": 0.92,
  "needs_human_review": false,
  "reasoning": "Clear infrastructure error with automatic remediation; standard ops pattern."
}
```

---

## Non-Negotiable Rules

1. **Never invent categories.** Output must be exactly one of the five defined enum values.
2. **Never output code fences, markdown, or conversational text.** The response must be a plain JSON object matching `EnrichResponse`.
3. **Deterministic fallback.** When input is ambiguous, contradictory, or the model fails validation after repair:
   - `category` = `"other"`
   - `quality_score` = `0.0`
   - `needs_human_review` = `true`
   - `reasoning` = `"Ambiguous or unclassifiable input; requires human review."`

---

## Fallback Logic

| Condition | Action |
|-----------|--------|
| Input length < 20 or > 4000 chars | Reject at schema validation (HTTP 422). |
| LLM returns invalid JSON / schema mismatch | **Exactly one** repair attempt with validation error fed back. |
| Repair attempt fails | Quarantine raw input, error, and model output → `logs/quarantine.jsonl`. Return HTTP 422 with fallback payload. |
| `LLM_ENABLED=0` (kill-switch) | Immediate HTTP 503 `{"error": "enrichment_unavailable", "message": "LLM enrichment is disabled."}` |
| `LLM_STUB=1` | Return `STUB_ENRICH_RESPONSE` without calling upstream. |
| Upstream timeout (>30.0s) | HTTP 504 `{"error": "timeout", "message": "Upstream LLM did not respond within 30s."}` |
| Upstream 429 / 5xx | Retry with exponential backoff (max 2 retries). |
| Upstream 400 / 401 / 403 | **Never retry**. Propagate as HTTP 502. |

---

## Resilience Requirements

| Property | Value |
|----------|-------|
| Upstream timeout | 30.0 seconds (hard) |
| Retry policy | Exponential backoff (base 1s, max 2 retries) on 429/5xx only |
| Retry exclusions | Never retry 400, 401, 403 |
| Validation & repair | Exactly one repair attempt; then quarantine |
| Quarantine destination | `logs/quarantine.jsonl` (JSON Lines, append-only) |

---

## Observability (Structured Logging)

Every request logs a single structured JSON line to stdout with:

| Field | Type | Description |
|-------|------|-------------|
| `duration_ms` | `int` | Wall-clock latency of the request. |
| `prompt_tokens` | `int` | Tokens sent to the model (prompt). |
| `completion_tokens` | `int` | Tokens received from the model (completion). |
| `model_id` | `string` | Model identifier used (e.g., `gpt-4o-mini`). |
| `repair_count` | `int` | 0 or 1 (number of repair attempts). |

---

## Feature Flags (Environment / Settings)

| Setting | Env Var | Default | Description |
|---------|---------|---------|-------------|
| `llm_enabled` | `LLM_ENABLED` | `true` | Kill-switch. When `false`, return 503 immediately. |
| `llm_stub` | `LLM_STUB` | `false` | When `true`, return `STUB_ENRICH_RESPONSE` without upstream call. |
| `llm_api_key` | `LLM_API_KEY` | *required if not stub* | Upstream API key. |
| `llm_base_url` | `LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint. |
| `llm_model` | `LLM_MODEL` | `gpt-4o-mini` | Model to invoke. |
| `llm_timeout_seconds` | `LLM_TIMEOUT_SECONDS` | `30.0` | Request timeout. |
| `llm_max_retries` | `LLM_MAX_RETRIES` | `2` | Max retries on transient errors. |

---

## Acceptance Criteria

1. `POST /v1/enrich` returns 200 with valid `EnrichResponse` for well-formed input.
2. Invalid input (length, missing fields) returns 422 with validation details.
3. `LLM_STUB=1` returns deterministic stub without network call.
4. `LLM_ENABLED=0` returns 503 immediately.
5. Upstream timeout returns 504.
6. Upstream 429/5xx triggers retry with backoff; 400/401/403 does not.
7. Validation failure triggers exactly one repair attempt.
8. Failed repair writes to `logs/quarantine.jsonl` and returns 422 with fallback.
9. Structured log line emitted for every request with required fields.
10. Evaluation suite (`eval/run_enrich_eval.py`) runs and prints score matrix.