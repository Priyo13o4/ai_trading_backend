# GitHub Actions + GHCR CI/CD Guide

Date: 2026-04-13

## Purpose
Move production image builds off the VM and into GitHub Actions, publish to GHCR, and deploy via pull + no-build restarts.

## 1. Auth Model and Prerequisites

1. GitHub Actions push authentication:
   - The workflow uses `${{ secrets.GITHUB_TOKEN }}`.
   - No custom PAT is required inside GitHub Actions.
   - Workflow permissions must include `packages: write`.

2. VM pull authentication:
   - A PAT is optional only if the VM is already authenticated to GHCR and can pull private packages successfully.
   - For your first-time setup (or if `docker pull ghcr.io/...` fails with auth errors), create a classic PAT with `read:packages` and log in once:

```bash
echo "<PAT_WITH_read:packages>" | docker login ghcr.io -u <github-username-lowercase> --password-stdin
```

## 2. Workflow Implemented in This Repo

File: `.github/workflows/build-prod.yml`

Behavior:
1. Triggers on push to `main` for relevant backend/build paths.
2. Lowercases the owner string in a dedicated step because `github.repository_owner` is not guaranteed lowercase.
3. Builds and pushes these images:
   - `ai_trading_bot-postgres` from `Dockerfile.postgres` with context `.`
   - `ai_trading_bot-api-web` from `api-web/Dockerfile` with context `.`
   - `ai_trading_bot-api-worker` from `api-worker/Dockerfile` with context `.`
   - `ai_trading_bot-scrapling` from `scrapling-api/Dockerfile` with context `scrapling-api/`
4. Pushes tags:
   - `<short_sha>` (12-char commit SHA) as immutable build output

Tagging strategy recommendation:
1. Deploy using a pinned `IMAGE_TAG=<short_sha>` across all services.
2. Keep at least one known-good prior SHA for fast rollback.

## 3. Production Compose Wiring

File: `docker-compose.prod.yml`

The production override now sets GHCR image references for:
1. `postgres`
2. `api-web`
3. `api-sse` (reuses `ai_trading_bot-api-web` image)
4. `api-worker`
5. `scrapling`

It uses `ghcr.io/${GHCR_IMAGE_OWNER}/...` and a shared `${IMAGE_TAG}` so you can deploy a single, consistent release tag across all services.

`GHCR_IMAGE_OWNER` and `IMAGE_TAG` are required and fail fast if missing.

Set these in `.env.prod`:

```dotenv
GHCR_IMAGE_OWNER=<github-account-or-org-lowercase>
IMAGE_TAG=<12-char-commit-sha-from-actions>
```

Network/environment compatibility notes:
1. Existing `env_file` wiring in `docker-compose.prod.yml` is preserved.
2. Redis and n8n service topology still comes from base `docker-compose.yml`:
   - `redis-queue`, `redis-app`, `redis-sessions`
   - `n8n`, `n8n-worker`
3. Only image source for app/postgres/scrapling changes; service names and inter-service DNS remain unchanged.

## 4. Deployment Commands (No Build on VM)

Use this exact production flow:

```bash
cd "/opt/pipfactor/ai_trading_bot"

# Optional preflight: confirm VM GHCR auth works for one target image
docker pull ghcr.io/${GHCR_IMAGE_OWNER}/ai_trading_bot-api-web:${IMAGE_TAG}

# Pull newest images from GHCR
docker compose --env-file .env.prod -f docker-compose.yml -f docker-compose.prod.yml pull \
  postgres api-web api-sse api-worker scrapling

# Restart stack without building on the VM
docker compose --env-file .env.prod -f docker-compose.yml -f docker-compose.prod.yml up -d --no-build \
  postgres redis-queue redis-app redis-sessions n8n n8n-worker api-web api-sse api-worker scrapling
```

### Helper: Auto-set IMAGE_TAG from latest successful CI run

File: `scripts/set_image_tag_from_actions.sh`

This helper fetches the latest successful run of `build-prod.yml` on `main`, takes the run commit SHA, truncates it to 12 chars, and writes `IMAGE_TAG` into `.env.prod`.

Requirements:
1. `GITHUB_TOKEN` or `GH_TOKEN` exported in your shell (must be able to read Actions metadata).
2. Run from repo root (`ai_trading_bot`).

```bash
cd "/opt/pipfactor/ai_trading_bot"
chmod +x scripts/set_image_tag_from_actions.sh
export GITHUB_TOKEN="<token_with_repo_or_actions_read_access>"
scripts/set_image_tag_from_actions.sh --workflow build-prod.yml --branch main --env-file .env.prod
```

Then deploy as usual:

```bash
docker compose --env-file .env.prod -f docker-compose.yml -f docker-compose.prod.yml pull \
   postgres api-web api-sse api-worker scrapling

docker compose --env-file .env.prod -f docker-compose.yml -f docker-compose.prod.yml up -d --no-build \
   postgres redis-queue redis-app redis-sessions n8n n8n-worker api-web api-sse api-worker scrapling
```

Important safety rule:
1. Always deploy with `--no-build` in production.
2. Production override explicitly disables `build` for GHCR-managed services.

## 5. GHCR Storage Bloat and Retention Policy

Without cleanup, GHCR grows quickly because every commit produces new immutable tags.

Recommended policy:
1. Keep recent SHA tags only (for example last 30-60 versions).
2. Delete older SHA-tagged package versions regularly using GitHub package retention settings or a scheduled cleanup workflow.
3. Before deleting old versions, ensure at least one known-good rollback SHA per image remains available.

Operational cadence:
1. Weekly: verify package growth and delete stale versions.
2. Monthly: validate rollback by pulling and starting one pinned SHA in a staging/safe environment.

## 6. Quick Validation Checklist

1. Commit and push to `main`.
2. Confirm workflow success in Actions.
3. Verify packages in GHCR for all four images with SHA tags.
4. Ensure VM has `GHCR_IMAGE_OWNER` + `IMAGE_TAG` in `.env.prod` and valid GHCR login.
5. Run `pull` then `up -d --no-build`.