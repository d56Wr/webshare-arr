# webshare-arr

Bridge between [Webshare.cz](https://webshare.cz) and Radarr/Sonarr.

- `/api` – Newznab indexer: searches Webshare, scores results 0–100 (title match, year, container, size, votes) and drops junk.
- `/sabnzbd/api` – SABnzbd-compatible download client: downloads the chosen file from Webshare (resumable, one at a time).

## Setup
1. `cp .env.example .env` and fill it in (never commit `.env`).
2. `docker build -t webshare-arr:local . && docker compose up -d` (listens on :9710, host network).
3. In Radarr/Sonarr add a Newznab indexer (`http://localhost:9710`, API path `/api`) and a SABnzbd download client (`localhost:9710`, URL base `/sabnzbd`) with the same API key. Restrict both with a tag and auto-tag the quality profile you want served from Webshare.

Tunables (env): `MIN_SCORE` (55), `MIN_MB` (150), `MAX_GB` (40), `LANG_TAG` (CZECH).
