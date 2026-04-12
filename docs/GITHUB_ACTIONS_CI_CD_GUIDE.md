# GitHub Actions & GHCR deployment Guide

Date: 2026-04-12

## Purpose
This document provides a comprehensive, end-to-end plan for moving the `ai_trading_bot` build process from your production VM over to GitHub Actions using the GitHub Container Registry (GHCR). 

This solves the 100% CPU lockup and RAM exhaustion issue encountered when running `docker compose up --build` on a 4GB/2CPU production server.

---

## 1. Prerequisites and Preparation

To push images to the GitHub Container Registry, your GitHub Action pipeline and your production server will need permission.

1. **Create a Personal Access Token (PAT) for the VM:**
   - Go to GitHub -> **Settings** -> **Developer Settings** -> **Personal Access Tokens (Classic)**
   - Click **Generate new token (classic)**
   - Name it `Prod VM GHCR Access`
   - Select the `read:packages` scope (do NOT give it repo scope, for security).
   - Copy this token. You will only use it once on the server.

2. **Authenticate your VM to GHCR:**
   - SSH into your production VM.
   - Run the following command (replace `<YOUR_GITHUB_USERNAME>` and the `<TOKEN>`):
     ```bash
     cat token.txt | docker login ghcr.io -u <YOUR_GITHUB_USERNAME_LOWERCASE> --password-stdin
     rm token.txt
     ```

*Note: Your GitHub Action workflow does **not** need a custom PAT. GitHub provides a dynamic, short-lived `${{ secrets.GITHUB_TOKEN }}` inside the pipeline automatically that has permissions to push to your repository's registry.*

---

## 2. Setting up the GitHub Action Workflow

Create a new file in your repository: `.github/workflows/build-prod.yml`.

This workflow triggers when you push to the `main` branch, but only if the relevant files actually change. It builds each container using the project root `.` as the context so that your local modules like `common/trading_common` are accessible.

```yaml
name: Build and Push Docker Images to GHCR

on:
  push:
    branches:
      - main
    paths:
      - 'ai_trading_bot/api-web/**'
      - 'ai_trading_bot/api-worker/**'
      - 'ai_trading_bot/common/**'
      - 'ai_trading_bot/scrapling-api/**'
      - 'ai_trading_bot/Dockerfile.postgres'

env:
  REGISTRY: ghcr.io
  # Converts your github username/repo to lowercase automatically
  IMAGE_PREFIX: ghcr.io/${{ github.repository_owner }}

jobs:
  build-and-push:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write # Critical: allows pushing to GHCR

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Log in to the Container registry
        uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      # 1. Build Custom Postgres
      - name: Build Postgres
        run: |
          docker build -t ${{ env.IMAGE_PREFIX }}/ai_trading_bot-postgres:latest -f ai_trading_bot/Dockerfile.postgres ai_trading_bot/
          docker push ${{ env.IMAGE_PREFIX }}/ai_trading_bot-postgres:latest

      # 2. Build API Web & SSE (They share api-web/Dockerfile)
      - name: Build API Web
        run: |
          docker build -t ${{ env.IMAGE_PREFIX }}/ai_trading_bot-api-web:latest -f ai_trading_bot/api-web/Dockerfile ai_trading_bot/
          docker push ${{ env.IMAGE_PREFIX }}/ai_trading_bot-api-web:latest

      # 3. Build API Worker
      - name: Build API Worker
        run: |
          docker build -t ${{ env.IMAGE_PREFIX }}/ai_trading_bot-api-worker:latest -f ai_trading_bot/api-worker/Dockerfile ai_trading_bot/
          docker push ${{ env.IMAGE_PREFIX }}/ai_trading_bot-api-worker:latest

      # 4. Build Scrapling
      - name: Build Scrapling API
        run: |
          docker build -t ${{ env.IMAGE_PREFIX }}/ai_trading_bot-scrapling:latest ai_trading_bot/scrapling-api/
          docker push ${{ env.IMAGE_PREFIX }}/ai_trading_bot-scrapling:latest
```

---

## 3. Adapting `docker-compose.prod.yml`

Locally, `docker-compose.yml` uses `build:` to assemble images. On production, you want Docker to download the pre-compiled images instead. Note down the image paths below (replace `YOURORG` with your actual GitHub username all lowercase).

Edit `ai_trading_bot/docker-compose.prod.yml` and explicitly define the `image` tags for the services you built:

```yaml
services:
  postgres:
    image: ghcr.io/<YOURORG>/ai_trading_bot-postgres:latest

  api-web:
    image: ghcr.io/<YOURORG>/ai_trading_bot-api-web:latest

  api-sse:
    image: ghcr.io/<YOURORG>/ai_trading_bot-api-web:latest

  api-worker:
    image: ghcr.io/<YOURORG>/ai_trading_bot-api-worker:latest

  scrapling:
    image: ghcr.io/<YOURORG>/ai_trading_bot-scrapling:latest
```

When Docker sees `image` and `build` definitions simultaneously, passing the right deploy commands will force it to use `image`.

---

## 4. Redefining your Deployment Commands

Once the Action runs and the containers are stored in GHCR, you simply SSH into the production server and run the updated pull/publish routine.

Replace your current deployment block in `some_commands.txt` / runbook with this zero-compilation workflow:

```bash
cd "/opt/pipfactor/ai_trading_bot" # (Or wherever your root is on the VM)

# 1. Download the pre-built images from GHCR
docker compose --env-file .env.prod -f docker-compose.yml -f docker-compose.prod.yml pull postgres api-web api-worker scrapling

# 2. Restart only the containers whose images have changed
docker compose --env-file .env.prod -f docker-compose.yml -f docker-compose.prod.yml up -d --no-build \
  postgres redis-queue redis-app redis-sessions n8n n8n-worker api-web api-sse api-worker scrapling
```

**Notice what is missing here:** `--build` has been explicitly eliminated. Your server will process this cutover in roughly 10 seconds with negligible CPU spikes.

---

## 5. Connecting the Dots (Summary Checklist)

1. [ ] Create a read-only GHCR PAT and log the production VM into Docker.
2. [ ] Commit the `.github/workflows/build-prod.yml` file to the root of your repo.
3. [ ] Check the Actions tab in GitHub to ensure the workflow turns green and publishes packages.
4. [ ] Go to your GitHub profile -> "Packages". You should see `ai_trading_bot-api-web`, etc. Ensure their visibility is set appropriately (Private is fine, since the VM is authenticated!).
5. [ ] Update `docker-compose.prod.yml` with the hardcoded image paths.
6. [ ] Pull and restart on the VM using the updated commands.