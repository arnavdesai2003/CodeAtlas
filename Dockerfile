FROM python:3.13-slim-bookworm AS parser-builder
# The pinned ARM64 binary binding crashes during parse in this Linux image.
# Build the same version with Bookworm's compiler; retain the grammar wheels.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && python -m pip wheel --no-cache-dir --no-binary tree-sitter --no-deps \
       --wheel-dir /wheels tree-sitter==0.25.0

FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/.cache/huggingface

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
COPY --from=parser-builder /wheels /wheels
# CPU wheels avoid bundling unused CUDA libraries on ordinary Linux servers.
RUN python -m pip install --index-url https://download.pytorch.org/whl/cpu torch==2.13.0 \
    && python -m pip install --no-index --find-links /wheels tree-sitter==0.25.0 \
    && python -m pip install -r requirements.txt \
    && rm -rf /wheels
RUN useradd --uid 10001 --create-home codeatlas \
    && mkdir -p /app/data/repos /app/.cache/huggingface \
    && chown -R codeatlas:codeatlas /app
COPY --chown=codeatlas:codeatlas app ./app
COPY --chown=codeatlas:codeatlas scripts ./scripts
COPY --chown=codeatlas:codeatlas tests ./tests
COPY --chown=codeatlas:codeatlas Dockerfile compose.production.yaml render.yaml .dockerignore ./
USER codeatlas
EXPOSE 8000
CMD ["python", "-B", "-m", "scripts.start_api"]
