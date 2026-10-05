# Self-hosted deployment (preserved alternative)

These are the prior server instructions, retained for compatibility. The current
target is Render; follow [deployment.md](deployment.md) instead for that platform.

The standalone `compose.production.yaml` runs the API, PostgreSQL, Elasticsearch,
Redis and Caddy on one Linux Docker host. Only Caddy publishes ports 80/443;
stores use an internal network. The API uses one worker, CPU embeddings,
mandatory API authentication and disabled benchmark bypass. This is a
single-server deployment, without replication or automatic failover. It serves
an API, not a browser frontend.

## Host and DNS

Use a Linux server with Docker Engine and Compose, Git and Python 3. A starting
allocation of 4 CPUs, 8 GB RAM and 40 GB disk is a planning estimate, not a
measured capacity guarantee. Allow inbound TCP 80/443 and restrict SSH to your
administration addresses. Point a hostname's A record (and AAAA only if IPv6
works) at that server. Caddy obtains HTTPS certificates when DNS and inbound
ports are reachable; see [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https).

Configure Elasticsearch's host requirements, including `vm.max_map_count` and
file descriptor limits, according to [Elastic's system configuration guide](https://www.elastic.co/docs/deploy-manage/deploy/self-managed/important-system-configuration).
The compose file sets the container file descriptor limit to 65,535 and a 1 GB
Elasticsearch heap. Disk sizing must include models, clones and retained index
generations. Do not schedule automatic generation deletion.

## Initial launch

On the target server:

```sh
git clone https://github.com/arnavdesai2003/CodeAtlas.git
cd CodeAtlas
python3 -m scripts.init_production_env --domain YOUR_REAL_API_HOSTNAME
docker compose --env-file .env.production -f compose.production.yaml config --quiet
docker compose --env-file .env.production -f compose.production.yaml up -d --build
docker compose --env-file .env.production -f compose.production.yaml ps
```

Replace the hostname before running. The initializer creates a mode-600
`.env.production` with three independent random credentials and never overwrites
an existing file. Keep it private and back it up securely. Do not commit it or
print full rendered Compose configuration. Do not merge this file with the
development compose file. Volumes and container names use the separate
`codeatlas-production` project.

The image excludes local secrets, clones and model caches. It runs as UID 10001.
The pinned Tree-sitter binding is built from source in a separate compiler stage:
the distributed Linux ARM64 wheel segfaulted on minimal parsing in local testing,
while a source build of the same version passed that probe. Grammar versions and
application parsing logic remain unchanged. CPU PyTorch avoids CUDA libraries.

Health alone does not prove search readiness: a fresh deployment has no corpus
and no cached models. Download the embedding and optional evaluation/reranking
model into the persistent model volume:

```sh
docker compose --env-file .env.production -f compose.production.yaml exec -T api python -B -c 'from app.search.embeddings import get_embedding_model; from app.search.reranker import get_reranker; get_embedding_model(); get_reranker()'
```

## Fresh corpus

Use these commands only on a fresh database. Ingestion is not idempotent; after a
partial failure inspect registered repositories and clone state before retrying.

```sh
docker compose --env-file .env.production -f compose.production.yaml exec -T api python -B - <<'PY'
import os
import httpx
r = httpx.post('http://127.0.0.1:8000/repositories',
    headers={'X-CodeAtlas-API-Key': os.environ['API_KEY']},
    json={'clone_url': 'https://github.com/karpathy/micrograd.git'}, timeout=180)
r.raise_for_status()
print('micrograd ingestion completed')
PY
docker compose --env-file .env.production -f compose.production.yaml exec -T api python -B -m scripts.batch_ingest
docker compose --env-file .env.production -f compose.production.yaml exec -T api python -B -m scripts.index_all_symbols
docker compose --env-file .env.production -f compose.production.yaml exec -T api python -B -m scripts.index_all_elasticsearch
docker compose --env-file .env.production -f compose.production.yaml exec -T api python -B -m scripts.verify_project --live --timeout 900
```

The release gate requires all evaluation cases to remain valid and hybrid
Recall@10 >= .880. Fresh clones follow current upstream commits; inherited
4,340-symbol counts and retrieval metrics are not guaranteed. Investigate any
gate failure rather than changing ranking to force it to pass. The verification
uses isolated scratch artifacts and requires previously downloaded models.

## Existing data, access and operations

Migrating the development corpus requires a coordinated maintenance window and
backups of PostgreSQL, Elasticsearch, repository clones and model caches; do not
copy active Docker volume files. A fresh corpus launch does not transfer local
history or recovery journals. Decide migration versus fresh indexing before
launching against existing data.

Use the private API key in `X-CodeAtlas-API-Key` for `/search` and repository
routes. `/health` is public. Never embed the shared key in a public browser app;
a multiuser product needs a separate authentication design. Configure GitHub's
push webhook using the generated webhook secret and the route documented in
[webhooks](webhooks.md). HTTPS success, rejection of unauthenticated search,
authenticated search results and restart persistence must be verified on the
actual target before calling it deployed.

Stop with `docker compose --env-file .env.production -f compose.production.yaml stop`;
this preserves data. Do not use `down -v`. Back up all persistent stores and
credentials, and test restoration separately. Recovery journals, retained
indices and caches are not backups. Stop all cooperating writers before upgrades;
use the documented recovery commands for pending work. See [atomic publication](atomic-publication.md)
and [generation retention](index-generation-retention.md). Do not roll back an
image across schema/protocol changes without a reviewed migration plan.

No public host, DNS, certificate issuance, cloud sizing or restore workflow is
validated by local packaging tests. Cloud launch requires a server hostname/IP,
SSH username and existing key access, plus the public API domain.

## Local packaging evidence

On 2026-10-05 the image built on Linux ARM64 and all 462 offline tests passed
both there and on the macOS host. Compose and Caddy configuration validated.
An isolated stack with no published ports initialized fresh metadata and passed
all-store health, unauthenticated rejection (401) and authenticated empty
repository listing (200). That stack was stopped; its test volumes were retained.
No production models/corpus were downloaded and no public certificate was issued.
AMD64 and actual cloud indexing/search/restart/restore still need target validation.
