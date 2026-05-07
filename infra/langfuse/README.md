# Langfuse self-host

Why self-host: prompts to the agents contain PHI fragments (intake, injury freetext, pain notes). Langfuse Cloud / LangSmith SaaS would mean PHI egress to a third party. Self-hosting keeps all observability data on infra Andre controls.

## Local dev

```bash
docker compose -f infra/langfuse/docker-compose.yml up -d
```

Visit `http://localhost:3000`, create an account (first signup is admin), create a project, copy the public + secret keys.

In `.env`:

```
LANGFUSE_ENABLED=true
LANGFUSE_HOST=http://localhost:3000
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

Run the FastAPI backend, hit `/patient/interact` with a real test patient, watch traces appear in the Langfuse UI.

Kill-switch: set `LANGFUSE_ENABLED=false` (or unset). All `langfuse_client` functions become no-ops; agents run unchanged.

## Production deploy (recommended)

Render + Hetzner + Backblaze. ~$25-40/mo:

| Component | Where | Cost |
|---|---|---|
| Langfuse web | Render Web Service (Starter, $7/mo) | $7 |
| Langfuse worker | Render Background Worker (Starter, $7/mo) | $7 |
| Postgres | Render Postgres (Starter, $7/mo) | $7 |
| Redis | Render Redis (Starter, $10/mo) | $10 |
| ClickHouse | Hetzner CX22 (single VM, self-managed) | $5 |
| Blob storage | Backblaze B2 | ~$1 |

**Why not Vercel:** Langfuse needs ClickHouse + long-lived containers; Vercel Functions don't fit.

**Why not Kubernetes:** ops burden vs ~50 user scale doesn't justify it.

**Why not Langfuse Cloud:** PHI boundary, plus self-host costs less at this volume.

### Steps

1. Spin up Render Postgres + Redis from the Render dashboard. Note the connection strings.
2. Spin up a Hetzner CX22, install Docker, run only the `langfuse-clickhouse` service from the compose file with persistent volumes. Bind to a private network or firewall to Render's egress IPs.
3. Create a Backblaze B2 bucket, generate access keys.
4. Deploy `langfuse-web` and `langfuse-worker` as two Render Web/Worker services pointing at the above.
5. Set `LANGFUSE_HOST` in Vercel project env to the Render web service URL. Add `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_ENABLED=true`.
6. Smoke test: hit one `/patient/interact` against the prod backend; watch for a trace in the Render-hosted Langfuse UI within ~5 seconds.

## PHI handling (live in code)

`backend/langfuse_client.py:_mask` rewrites user-role message content to `[redacted user content, N chars]`. So:

- **Stored in Langfuse**: system prompts (versioned, non-PHI), tool inputs/outputs (structured), assistant outputs (model's own text), latencies, token counts, errors.
- **NOT stored**: raw intake text, injury freetext, pain notes, patient names that show up inside user messages.

`session_id` in Langfuse is `SHA-256(auth.uid())[:16]` — same hashing convention as the existing `pipeline_runs` writer. Two patient records remain unlinkable across systems even by an internal observer who has access to both Langfuse and Postgres.

## Joining with `pipeline_runs`

Both layers share `request_id` (set by `observability.set_run_context` in middleware). `pipeline_runs.request_id` is queryable in the admin dashboard; in Langfuse the same value appears as `metadata.request_id` on the trace. To audit one patient interaction end-to-end:

1. Find the row in `pipeline_runs` for the agent step.
2. Copy its `request_id`.
3. In Langfuse: search by metadata `request_id == <that uuid>`.

That's it. Langfuse provides prompt/response inspection; `pipeline_runs` provides aggregate decision audit. They answer different questions.
