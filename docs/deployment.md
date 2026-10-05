# Deployment

## Deploying CodeAtlas on Render

Render is the current deployment target. `render.yaml` reuses the tested production
Docker image and configures the complete existing search stack. It does not invoke
Docker Compose or provision a reverse proxy. Nothing has been deployed to Render;
creating the Blueprint below is a manual action that creates paid resources.
Automatic Git deploys are disabled, although initial resource creation deploys.

### Service mapping

| Component | Render mapping | Persistence |
| --- | --- | --- |
| FastAPI | Docker Web Service, `1c-2g`, one instance/worker | 10 GB disk at `/app/data` |
| PostgreSQL | Managed Render Postgres 17, `0.1c-256mb` | Managed, 1 GB initial storage |
| Redis | Managed Render Key Value, `256mb`, `allkeys-lru` | Managed cache; no source of truth |
| Elasticsearch | Docker image private service, `1c-2g`, single node, version 9.4.3 | 10 GB disk at `/usr/share/elasticsearch/data` |
| HTTPS/routing | Render Web Service routing and managed HTTPS | Managed by Render |

Use the same region for all resources (the Blueprint selects Oregon). Postgres
and Key Value reject external connections; Elasticsearch has no public web route.
Its disabled security assumes trusted services within the Render private network.
Do not place untrusted applications in that network or expose port 9200 publicly.
Use the direct Postgres connection, not a transaction-pooled endpoint: writer
coordination uses session advisory locks.

Render supports [private-service Elasticsearch](https://render.com/docs/deploy-elasticsearch).
This project needs BM25, dense-vector kNN, aliases, index copying and lifecycle
APIs. Keeping Elasticsearch preserves those features and recovery protocols.
The Blueprint disables mmap to avoid requiring host sysctl access and uses a
1 GB heap within 2 GB RAM. The same version is used locally. Render is not managing
this search database: you remain responsible for upgrades, snapshots and disk
capacity. Initial resources are a small deployment estimate, not tested Render
capacity; monitor memory during model loading, indexing and evaluation.

There is no viable fully free deployment of the complete current workflow.
Elasticsearch is unsuitable for 512 MB services, and private services and persistent
disks require paid plans. API clones must survive redeploys; free web services lose
local files. Free Postgres expires after 30 days. See [free-plan limits](https://render.com/docs/free).

If you prefer managed search operations, the best alternative is **Elastic Cloud
Hosted Elasticsearch 9.x**, with an HTTPS endpoint and a scoped API key. Choose a
region near Render, confirm index/alias/reindex/tasks permissions and validate the
full publication protocol against that deployment. Replace the private search
service and the API's `ELASTICSEARCH_URL` reference with secret dashboard values
`ELASTICSEARCH_URL=https://...` and `ELASTICSEARCH_API_KEY`; the client keeps normal
TLS verification. Remove the unused private service from the Blueprint before
creating resources. Elastic pricing is region/topology dependent and must be
quoted separately. Serverless Elasticsearch, OpenSearch, SQLite and pgvector are
not validated substitutes for these lifecycle APIs; no search functionality was
removed or rewritten. See [Elastic Cloud Hosted](https://www.elastic.co/docs/deploy-manage/deploy/elastic-cloud/cloud-hosted).

Keep Redis as Render Key Value. The current health endpoint and strict publication
invalidation depend on it, despite retrieval fallback during Redis outages.
A free Key Value instance is a possible cheaper demo option (25 MB, no persistence),
with eviction/cold-cache limitations. The supplied Blueprint uses paid `256mb`.
An external Redis URL, including `rediss://`, is also supported, but adds another
provider and network path without a present need. Do not flush live cache keys.

### Connect GitHub and create the resources

1. Sign into the Render Dashboard and connect your GitHub account. Grant the
   Render GitHub app access to `arnavdesai2003/CodeAtlas`.
2. Select **New → Blueprint**, choose that repository and its current branch
   (`main`), and use the root `render.yaml` file. Review the four resources,
   paid plans and two disks before you click **Deploy Blueprint**. Do not create
   a second set of resources manually if you use this path.
3. Supply independent random values for the prompted `API_KEY` and
   `GITHUB_WEBHOOK_SECRET` (`sync: false`). Use a password manager's generator;
   keep values private. Do not use the old `.env.production` initializer, which
   belongs to the preserved self-hosted package. Managed database credentials
   are wired automatically through Blueprint references.
4. Wait for Postgres, Key Value and Elasticsearch to become available. If the
   API's initial health check fails while stores start, use **Manual Deploy →
   Deploy latest commit** after the stores are ready. No Compose orchestration
   or dependency-start ordering is assumed.
5. In `codeatlas-api`, confirm **Settings → Health Check Path** is `/health`,
   Dockerfile is `./Dockerfile`, context is the repo root and Docker Command is
   blank (uses the image CMD). Render supplies `PORT`; the launcher binds
   `0.0.0.0:$PORT` and replaces itself with Uvicorn for proper shutdown.
6. Confirm the disk exists at `/app/data` and the public `onrender.com` URL
   appears near the top of the Web Service page. No custom domain is required.
   Render provides [HTTPS and routing](https://render.com/docs/web-services).

For manual creation instead of a Blueprint, create **New → Postgres** with version
17, database/user `codeatlas`, plan `0.1c-256mb`, 1 GB storage and no external
allowlist entries. Create **New → Key Value** with plan `256mb`, `allkeys-lru` and
no external allowlist entries. Create a **Private Service → Existing Image** using
`docker.elastic.co/elasticsearch/elasticsearch:9.4.3`, plan `1c-2g`, 10 GB disk at
`/usr/share/elasticsearch/data`, and exactly the search environment settings in
`render.yaml`. Then create **New → Web Service**, connect the GitHub repo/current
branch, select Docker/`1c-2g`, add the 10 GB `/app/data` disk and the following API
environment values. Disable auto-deploys. All four resources must share a region.

### Environment variables

| Variable | API value/source |
| --- | --- |
| `DATABASE_URL` | Postgres **Internal Database URL**; standard `postgresql://` and legacy `postgres://` normalize to installed psycopg 3 |
| `REDIS_URL` | Key Value **Internal Connection URL**, `redis://...` (external TLS `rediss://...` also supported) |
| `ELASTICSEARCH_URL` | Search private hostname:9200 from `fromService.hostport`, normalized to HTTP; or full external HTTPS URL |
| `API_KEY` | Private random key; required in production for search/repository routes |
| `GITHUB_WEBHOOK_SECRET` | Separate private random signing secret |
| `APP_ENV` | `production` |
| `PORT` | Supplied automatically by Render; local Docker default is 8000 |
| `EMBEDDING_DEVICE` | `cpu` |
| `HF_HOME` | `/app/data/model-cache` |
| `BENCHMARK_CACHE_BYPASS_ENABLED` | `false` |
| `ELASTICSEARCH_CLOSE_SEARCH_CONNECTIONS` | `false` |
| `ELASTICSEARCH_API_KEY` | Only for an authenticated external Elasticsearch endpoint; optional for the private service |

There is no `SECRET_KEY` setting in this repository. Optional `TORCH_NUM_THREADS`
is deliberately unset; choose it only after measuring the target hardware.
Existing environment-based configuration remains compatible with development
and the preserved Compose package. No production connection defaults to localhost
or a Compose hostname. Render's private service reference supplies the hostname;
explicit URLs retain their credentials, query parameters and TLS scheme.

### Persistent state and indexing

Postgres stores metadata/recovery journals, Elasticsearch stores searchable
symbols, and Key Value stores the disposable fenced cache. Git clones remain on
the API's Render disk at `/app/data/repos` because synchronization and recovery
use real Git working directories. Model downloads use `/app/data/model-cache`.
Moving clones to object storage would require a new checkout/recovery design;
removing them would break existing workflows. Never mount over `/app` (application
code). The image runs as UID 10001; confirm the mounted disk is writable in the
Render **Shell** before ingestion. Only files below the disk mount persist.

A Render disk is attached to one instance and unavailable to build/pre-deploy
commands or separate jobs. Run the following in the Web Service's dashboard
**Shell**, after its first healthy start, not in the Docker build or a pre-deploy
hook. Disk-backed redeploys have downtime and do not support horizontal scaling;
see [persistent disk limitations](https://render.com/docs/disks). Keep clones,
metadata and search backups consistent; managed storage is not a complete restore
plan. Do not restore a running Elasticsearch database from a disk snapshot;
use Elasticsearch-native snapshots and a separately configured backup repository.

First verify disk access and warm the models (needs outbound GitHub/Hugging Face
access; do not enable offline model flags until downloads finish):

```sh
cd /app
python -B -c 'from pathlib import Path; p=Path("/app/data/.write-check"); p.write_text("ok"); p.unlink()'
python -B -c 'from app.search.embeddings import get_embedding_model; from app.search.reranker import get_reranker; get_embedding_model(); get_reranker()'
```

A healthy fresh service has no searchable corpus. These commands ingest a **fresh
empty database**; do not repeat ingestion blindly after a partial failure:

```sh
python -B - <<'PY'
from app.db.database import SessionLocal
from app.indexer.repository import ingest_repository
with SessionLocal() as db:
    ingest_repository(db, 'https://github.com/karpathy/micrograd.git')
print('micrograd ingestion completed')
PY
python -B -m scripts.batch_ingest
python -B -m scripts.index_all_symbols
python -B -m scripts.index_all_elasticsearch
python -B -m scripts.verify_project --live --timeout 900
```

Full indexing uses the existing journals, advisory locks and atomic alias
publication. Stop other writers while indexing. New upstream commits can change
counts/evaluation cases; inherited 4,340-symbol metrics are not guarantees.
The release gate requires valid evaluation cases and hybrid Recall@10 >= .880.
Its offline unit child uses isolated test credentials/authentication; live checks
retain the deployed connection URLs and API key.
Investigate failures; do not adjust ranking to force success. If evaluating while
serving loaded models exhausts 2 GB RAM, pause traffic and increase the plan
before retrying. Indexing must finish before advertising search availability.
A migration of existing data requires coordinated database/search/clone backups,
not automatic bootstrap or copying active volume files.

### Check the deployed API and obtain its URL

Copy the displayed URL, for example `https://codeatlas-api-....onrender.com`.
Open `/health`: healthy dependencies return 200; unavailable stores return 503
without credentials or raw connection details. Health checks do not load models
or establish corpus readiness. In an HTTP client, POST `/search` with
`{"query":"compute gradients", "limit":10}` and the private
`X-CodeAtlas-API-Key` header. Expect 401 without the key and actual results with
it after indexing. Do not put this shared key in public browser JavaScript.
Configure the signed GitHub webhook using [webhook instructions](webhooks.md).

Use **Manual Deploy** for later releases and confirm clone availability, `/health`
and authenticated search after restart/redeploy. All writer processes must use
the same protocol version. Inspect pending jobs rather than deleting journals;
see [atomic publication](atomic-publication.md) and [retention](index-generation-retention.md).
No rollback or retention cleanup is scheduled by this Blueprint.

### Budget

Estimated baseline at the currently published [Render prices](https://render.com/pricing):

| Resource | Monthly estimate |
| --- | ---: |
| API `1c-2g` | $25.00 |
| Private Elasticsearch `1c-2g` | $25.00 |
| Managed Postgres `0.1c-256mb` | $6.00 |
| Postgres 1 GB storage | $0.30 |
| Key Value `256mb` | $10.00 |
| Two 10 GB persistent disks ($0.25/GB) | $5.00 |
| Hobby workspace / managed HTTPS | $0.00 |
| Blueprint baseline total | **$71.30** |

Using free Key Value reduces this estimate to **$61.30**, with 25 MB/no persistence.
Free Postgres is an expiring demo resource, not the durable database in this plan.
There is no $0 full-stack deployment preserving the current workflow. Estimates
exclude taxes, bandwidth/build overages, backup storage and larger plans; verify
the Dashboard quote before creation. Managed Elastic Cloud adds its own quote
instead of the private search service and disk. Nothing has been purchased.

### Existing package and validation

The Dockerfile's CPU dependencies, native parser source-build fix, nonroot user
and secret-excluding build context are retained. The previous self-hosted files
remain available for compatibility; their instructions are archived in
[deployment-selfhosted.md](deployment-selfhosted.md). Render does not use Caddy,
Compose orchestration, SSH access, server IPs or certificate scripts.

Local validation results are recorded in AGENTS.md. Local tests and Docker startup
are not a claim of an actual Render launch. Manual resource creation, initial
indexing, public HTTPS/authenticated search, disk permission/restart persistence
and backup restoration must be verified on Render.

Validation on 2026-10-05: all 469 host tests passed; the native Linux ARM64 image
passed its complete offline release gate under one CPU/2 GB with production-style
parent settings. Both ARM64 and AMD64 Docker builds passed; the Blueprint passed
Render's public JSON schema. Isolated local startup verified port 10000, all-store
health, API authentication, nonroot parsing and writable persistent files, including
restart persistence. The stack was stopped and development services left intact.

The additional AMD64 suite under emulation had 26 child-process deadline failures
(all 15-second recovery fixture deadlines; no memory-limit events). Three
representative tests passed using a temporary 60-second diagnostic deadline;
children took about 33 seconds. Repository tests retain their original deadlines.
Native AMD64 execution on Render remains a launch check; this is not a claim of a
passing standard emulated suite or validated Render capacity.
