
# StoryAudit Backend Runtime Architecture v1.0

## Storage layers

### Immutable corpus
`Source -> Document -> Block -> Proposition`

These tables are append-only. PostgreSQL triggers reject UPDATE/DELETE.

### Versioned analysis
LLM outputs are stored in `annotations` with:

- methodology_version
- schema_version
- prompt_version
- model_version
- supersedes_annotation_id
- is_current

Reanalysis creates a replacement annotation instead of overwriting history.

### Recalculable outputs
`Metric -> Capital -> Index -> Finding -> Recommendation`

They may be regenerated when formulas or weights change.

## Queue

`jobs` is the canonical queue table. Workers call:

```sql
SELECT * FROM claim_next_job('worker-01');
```

The function uses `FOR UPDATE SKIP LOCKED`.

Use idempotency keys:
- initial: `{scan}:{agent}:{scopeHash}:{promptVersion}`
- reanalysis: `{scan}:{agent}:{objectType}:{objectId}:{issueId}:{promptVersion}`

## Dependency graph

`A0 -> A1 -> [A2,A3,A4] -> A5 -> SourceProfileBuilder -> A6 -> A7 -> A8a -> A8b`

A2/A3/A4 are parallelizable.

A8a is deterministic code.

## Targeted reanalysis

One ambiguous proposition should not replay the entire scan.

Example:

`A3 conflict(prop_17)`
-> enqueue A3 only for prop_17
-> insert replacement role annotation
-> rebuild the affected source profile
-> invalidate/rerun A6
-> A7
-> A8

Downstream invalidation:
- A1 -> A2,A3,A4,A5,A6,A7,A8
- A2/A3/A4 -> A5,A6,A7,A8
- A5 -> A6,A7,A8
- A6 -> A7,A8
- A7 -> A8

## Metric to Evidence

`GET /scans/{scanId}/metrics/{metricId}` returns:

1. current metric result;
2. feature/source breakdown;
3. findings;
4. exact evidence propositions;
5. block/document/source provenance.

Frontend navigation becomes:

`Metric -> Finding -> Quote -> Document -> Source`

## Source Profile Builder

It is deterministic, not an LLM agent.

Aggregation uses:
- confidence-weighted mode for customer/problem/object/promise;
- confidence-weighted hero and brand-role mode;
- top-N higher-order meanings;
- weighted Before/After transformation labels.

This minimizes additional synthesis hallucination before A6.

## Production extensions not included here

- authentication / multi-tenancy / RLS;
- billing;
- provider-specific LLM adapters;
- crawler implementation;
- object storage for files;
- distributed event bus.

Those should wrap this runtime rather than change the methodology core.
