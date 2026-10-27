# webshare-arr

Bridge between [Webshare.cz](https://webshare.cz) and Radarr/Sonarr.

- `/api` – Newznab indexer: searches Webshare, scores results 0–100 (title match, year, container, size, votes) and drops junk.
- `/sabnzbd/api` – SABnzbd-compatible download client: downloads the chosen file from Webshare (resumable, one at a time).

## Setup
1. `cp .env.example .env` and fill it in (never commit `.env`).
2. `docker build -t webshare-arr:local . && docker compose up -d` (listens on :9710, host network).
3. In Radarr/Sonarr add a Newznab indexer (`http://localhost:9710`, API path `/api`) and a SABnzbd download client (`localhost:9710`, URL base `/sabnzbd`) with the same API key. Restrict both with a tag and auto-tag the quality profile you want served from Webshare.

Tunables (env): `MIN_SCORE` (55), `MIN_MB` (150), `MAX_GB` (40), `LANG_TAG` (CZECH).

## Real audio tracks and resolution

For movie/TV searches by id, the top `FILEINFO_MAX` (60) hits are looked up with Webshare's `file_info` (cached forever in
`data/fileinfo.json`). The release title is then built from facts: Radarr/Sonarr's own title, the measured resolution,
source tags from the file name, the original-language track's audio format (`TrueHD 7.1`, `DTS 5.1`, `DDP 5.1`, ...) and the
languages of the audio tracks (`CZ ENGLISH` = multi, `CZ` = dub only). Without track languages the file name decides, and a
name with Czech/Slovak diacritics not found in the original title counts as a dub.

## Extra titles (`data/aliases.json`)
Series have no Czech/Slovak titles in the metadata Sonarr uses, so add them by hand (TVDB id for series, TMDB id for movies); changes are picked up without a restart:
```json
{"tv": {"74584": ["Gordon Ramsay Noční můry kuchyně"]}, "movie": {"60672": ["Purpurové řeky 2"]}}
```
Movies get Czech/Slovak titles automatically from Radarr's metadata server.

## TMDB (optional)
Set `TMDB_TOKEN` (a TMDB "API Read Access Token") in `.env` to also search series under their Czech/Slovak TMDB names.

## Rate limit
All Webshare API calls go through one limiter (`WS_MIN_INTERVAL`, default 0.25 s) and search results are cached for 30 minutes, so bulk "search missing/upgrade" runs by Huntarr/Newtarr cannot hammer Webshare.

## VIP-aware preference
With `RADARR_*`/`SONARR_*` set, the service checks the Webshare account every `VIP_CHECK_MINUTES` (60). While VIP is active
the delay profiles prefer usenet (= Webshare) and the Webshare indexer gets priority 25; without VIP they prefer torrents
and Webshare drops to priority 40. Current state: `GET /health`.
