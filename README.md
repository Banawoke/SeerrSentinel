<p align="center">
  <img src=".logo.png" alt="SeerrSentinel Logo" width="100" height="100">
</p>
<h1 align="center">SeerrSentinel</h1>

Automation suite for managing **Seerr**, **Radarr**, and **Sonarr**. I was struggling to keep my library clean so i made this script. The goal of SeerrSentinel is to automate media discovery, library cleanup, and file imports for Seerr, Radarr, and Sonarr. It is inspired by [Huntarr.io](https://github.com/plexguide/Huntarr.io).

>[!IMPORTANT]
> This is very early software, any help will be verry welcome it work with my use case but may need some tweeks for yours. Report any bug or feature request 😊.

## Scripts

| File | Role |
|---|---|
| `seerr_sentinel.py` | **Main entry point** — orchestrator + config validation |
| `sentinel_search.py` | Automated searches for missing media |
| `sentinel_cleaner.py` | Detection and deletion of stalled/missing media |
| `sentinel_import.py` | Video file injection from the downloads folder |

## Installation

### 1. Configure the environment

```bash
cp .env.example .env
# Edit .env with your API keys and URLs
```

### 2. Check your configuration

```bash
python3 seerr_sentinel.py --check-env
```

### 3. Python dependencies

```bash
pip install requests python-dotenv
```

## Usage

```bash
# Check the .env before anything else
python3 seerr_sentinel.py --check-env

# Trigger Radarr/Sonarr searches for missing media
python3 seerr_sentinel.py search

# Cleanup (dry-run to test safely first)
python3 seerr_sentinel.py clean --dry-run
python3 seerr_sentinel.py clean

# Video file injection
python3 seerr_sentinel.py import
python3 seerr_sentinel.py import --sonarr --force-id 42

# Run all steps in sequence
python3 seerr_sentinel.py all --dry-run
```

Each sub-script can also be run directly:

```bash
python3 sentinel_cleaner.py --dry-run
python3 sentinel_search.py
python3 sentinel_import.py --radarr
```

## Configuration (`.env`)

| Variable | Required | Description |
|---|---|---|
| `JELLYSEER_API_KEY` | yes | Jellyseerr API key |
| `JELLYSEER_URL` | yes | Jellyseerr URL (`http://your-jellyseerr:5055`) |
| `TMDB_API_KEY` | yes | TMDB API key (register here [TMDB](https://www.themoviedb.org/settings/api))|
| `RADARR_API_KEY` | yes | Radarr API key |
| `RADARR_URL` | yes | Radarr URL (`http://your-radarr:7878`) |
| `SONARR_API_KEY` | yes | Sonarr API key |
| `SONARR_URL` | yes | Sonarr URL (`http://your-sonarr:8989`) |
| `DOWNLOADS_PATH` | yes | Path to the downloads folder |
| `PUID` | yes | User ID for chown on injected files |
| `PGID` | yes | Group ID for chown on injected files |
| `RELEASE_BUFFER_DAYS` | optional | Days after release before cleanup (default: `7`) |
| `DELETION_DELAY_DAYS` | optional | Grace period before deletion (default: `2`) |
| `KEEP_REQUESTS_OLDER_THAN_DAYS` | optional | Keep Jellyseerr requests older than N days (default: `14`) |

## Architecture

```
seerr_sentinel.py          ← orchestrator + load_config()
├── sentinel_search.py     ← Radarr/Sonarr MoviesSearch / SeasonSearch / EpisodeSearch
│   └── (imports sentinel_import for orphan maintenance)
├── sentinel_cleaner.py    ← missing media detection + Radarr/Sonarr/Jellyseerr deletion
└── sentinel_import.py     ← hard-link injection + Radarr/Sonarr rescan
```

### `sentinel_search` logic

1. Checks for active commands (global lock)
2. Every 30 min: runs `sentinel_import` to detect orphans
3. Looks for a missing Radarr candidate → triggers `MoviesSearch`
4. If nothing on Radarr side → looks at Sonarr → `SeasonSearch` or `EpisodeSearch`
5. Per-cycle quota (12h) to avoid flooding indexers

### `sentinel_cleaner` logic

1. Fetches all missing media from Radarr/Sonarr
2. Ignores recent releases (`RELEASE_BUFFER_DAYS`)
3. After `DELETION_DELAY_DAYS` days → deletes from Radarr/Sonarr and Jellyseerr
4. Keeps Jellyseerr requests older than `KEEP_REQUESTS_OLDER_THAN_DAYS` days

### `sentinel_import` logic

1. Scans the `DOWNLOADS_PATH` folder
2. Matches files against missing media using title tokens + TMDB aliases
3. Creates hard-links in Radarr/Sonarr media folders
4. Triggers a `RescanMovie` / `RescanSeries` and waits for confirmation

## Use case example

### Cronjob 

```cron
20 */4 * * *  python3 /seerr_sentinel/seerr_sentinel.py clean
*/10 * * * *  python3 /seerr_sentinel/seerr_sentinel.py search
*/30 * * * *  python3 /seerr_sentinel/seerr_sentinel.py import
```