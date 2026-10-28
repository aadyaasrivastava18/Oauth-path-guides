# OAuth path guides

**Live app:** `<PASTE URL>`  |  **Repo:** `https://github.com/aadyaasrivastava18/Oauth-path-guides`  |  **Verification notes:** [VERIFICATION.md](VERIFICATION.md)

A developer building an AI agent needs their users to connect an app. First, they may have to register an OAuth app with the provider and get it approved, and today nobody can tell whether that is an hour or weeks. This tool answers that: look up an app (or type any app name or website) and get a shareable guide that says **how hard it is, how long it takes, what can block you, and what an agent can do versus what you must do**. Every statement is labelled with how well it is supported.

## What it does

- **Library:** pre-built guides across the difficulty range (`data/guides/`).
- **Live generation:** type any app name or URL. An agent finds the provider's docs, reads them, tests the OAuth endpoints, and streams progress while it builds a guide you can share by link. Works for apps outside the library.
- **Honest degradation:** no OAuth, no readable docs, or not enough evidence all produce a clear guide saying so, not a confident guess.

## How a guide is built

```
app name / URL
  -> resolve + search (Tavily)            find candidate docs, rank official developer pages first
  -> fetch (Jina Reader, direct fallback) read pages; long pages cut down to OAuth-relevant passages
  -> ONE Gemini call                      extract sourced facts + draft suggestions as JSON
  -> verify                               every model quote must appear in a fetched page, else it is downgraded
  -> probe                                1 request per endpoint with an invalid client (no credentials)
  -> classify                             fixed rules turn verified facts into a grade 1-4
  -> assemble + save                      Pydantic schema -> JSON -> rendered page
```

## Key decisions

1. **Setup effort and approval wait are separate numbers.** They are different risks (your work vs. their queue). Conflating them is the pain the brief describes.
2. **Evidence labels are first-class.** `Tested` (we observed it), `Docs` (quote confirmed in a fetched page), `Inferred` (model synthesis or unconfirmed quote), `Unverified` (heuristic). Click any tag to see the quote or note.
3. **The model extracts facts; code decides the grade.** `agent/classify.py` is rule-based, so the same facts always give the same grade and every reason is traceable. Grade 0 ("Not enough evidence") exists so the tool can decline to guess.
4. **Quote checking instead of trust.** The model must quote the page for each fact. If the quote is not found in the page we fetched, the fact is shown as `Inferred`. This catches fabricated console steps.
5. **"Do you need your own app?" comes first**, answered by a live Composio lookup (`toolkits.get(slug).composio_managed_auth_schemes`, per the managed-auth docs). If no Composio key is configured the guide says "unknown" and links the directory.
6. **Credential-free probes only.** One request per endpoint, invalid client, and only to URLs that literally appear in the docs we read. This produces `Tested` evidence without touching anyone's account.
7. **One model call per guide**, with model fallback (`GEMINI_MODELS=a,b`, quotas are per model), caching, a per-IP limit and a daily cap, because the free tier is small.
8. **The agent never logs in, creates apps or submits anything.** Each guide lists what the agent can do and where you take over.

## Setup

Python 3.11+ (tested on 3.12).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # then fill in keys (never commit .env)
python -m scripts.list_models   # pick models for GEMINI_MODELS
python -m scripts.check_setup   # one tiny request to each service; fix any FAIL
python -m scripts.generate_library   # builds guides for data/seed_apps.txt (about 1 model call per app)
uvicorn app.main:create_app --factory --reload   # http://localhost:8000
pytest                          # 40 tests, no network or keys needed
```

Keys: Gemini (aistudio.google.com), Tavily (tavily.com), optional Composio (free account) for the managed-auth check, optional Jina key for higher fetch limits.

## Deploy (Render free tier)

Push to GitHub, create a Web Service from the repo (`render.yaml` is included), and set the secret env vars in the dashboard. Set `TRUST_PROXY=true` (already in `render.yaml`). The free tier sleeps when idle, so the first request after a pause is slow; open the site once before a demo.

Live-generated guides are saved to local disk, which Render's free tier wipes on redeploy. For share links that survive, create a free Supabase project, run the SQL below, and set `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` (service key stays server-side, never in the repo):

```sql
create table guides (slug text primary key, data jsonb not null, created_at timestamptz not null default now());
alter table guides enable row level security;  -- no policies: only the service key can read/write
```

## Limitations (read these)

- **Time ranges are heuristics.** Providers rarely publish approval times. The ranges are rules of thumb per grade, labelled `Unverified`, unless the provider states a time (then it is shown separately, with its quote).
- **Docs-only evidence for most claims.** Only endpoint liveness, discovery documents and the managed-auth lookup are `Tested`. We cannot log in to consoles, so console steps are as good as the docs.
- **Generated guides are not hand-verified.** Only guides marked "Hand-verified" were checked by a person; see VERIFICATION.md.
- **JavaScript-heavy docs** may read poorly. Jina Reader helps; some pages still fail and are listed as unreadable in Sources.
- **Free-tier quotas** limit throughput. Limits are in-memory and per process; the daily cap is the real backstop (client IPs behind a proxy can be spoofed).
- **SSRF protection** checks DNS before each request but cannot fully prevent DNS-rebinding races.
- **Untested against live services in the build environment:** Gemini, Tavily, Jina, Supabase and the Composio call were exercised with fakes or by inspecting the SDK, not against the real services. `scripts.check_setup` tests all of them in one command.

## Layout

```
schema/models.py      the guide shape (Pydantic) and evidence labels
agent/                llm, search, fetch, resolve, probe, managed, classify, assemble, pipeline
app/                  FastAPI app, templates, store, rate limiter
scripts/              generate_library, check_setup, list_models
data/guides/          pre-built guides (JSON)      data/seed_apps.txt  apps to generate
tests/                40 tests
```
