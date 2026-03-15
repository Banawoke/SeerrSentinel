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

It is highly recommended to use **Docker** or **Docker Compose**. A continuously updated image is available.

### Using Docker Compose (Recommended)

1. Create a `docker-compose.yml` file:
```yaml
services:
  seerr-sentinel:
    image: ghcr.io/banawoke/seerrsentinel:latest
    container_name: seerr-sentinel
    restart: unless-stopped
    environment:
      - JELLYSEER_API_KEY=your_api_key
      - JELLYSEER_URL=http://your-jellyseerr:5055
      - TMDB_API_KEY=your_api_key
      - RADARR_API_KEY=your_api_key
      - RADARR_URL=http://your-radarr:7878
      - SONARR_API_KEY=your_api_key
      - SONARR_URL=http://your-sonarr:8989
      - DOWNLOADS_PATH=/downloads
      - PUID=1000
      - PGID=1000
      # - RELEASE_BUFFER_DAYS=7
      # - DELETION_DELAY_DAYS=2
      # - KEEP_REQUESTS_OLDER_THAN_DAYS=14
      # - STUCK_DOWNLOAD_MINUTES=20.0
      # - MAX_DOWNLOAD_HOURS=6.0
      # - DAEMON_INTERVAL_SECONDS=60
      # - SEARCH_INTERVAL_MINUTES=15
      # - IMPORT_INTERVAL_MINUTES=30
      # - CLEAN_INTERVAL_MINUTES=240
    volumes:
      - /path/to/your/downloads:/downloads
```
2. Start the container:
```bash
docker compose up -d
```

### Using Docker CLI

```bash
docker run -d \
  --name seerr-sentinel \
  --restart unless-stopped \
  -e JELLYSEER_API_KEY=your_api_key \
  -e JELLYSEER_URL=http://your-jellyseerr:5055 \
  -e TMDB_API_KEY=your_api_key \
  -e RADARR_API_KEY=your_api_key \
  -e RADARR_URL=http://your-radarr:7878 \
  -e SONARR_API_KEY=your_api_key \
  -e SONARR_URL=http://your-sonarr:8989 \
  -e DOWNLOADS_PATH=/downloads \
  -e PUID=1000 \
  -e PGID=1000 \
  # Other optional variables
  -v /path/to/your/downloads:/downloads \
  ghcr.io/banawoke/seerrsentinel:latest
```

### Manual Installation (Python)

If you prefer to run the scripts manually:

1. **Configure the environment**
```bash
cp .env.example .env
# Edit .env with your API keys and URLs
```

2. **Python dependencies**
```bash
pip install -r requirements.txt
```

3. **Check your configuration**
```bash
python3 seerr_sentinel.py --health-check
```

## Usage

### Docker (Daemon Mode)

When using the Docker image, the script automatically runs in `daemon` mode. It stays alive in the background and handles its own schedule.
These intervals are customizable via environment variables (defaults shown below):
- **Search**: every 15 minutes (`SEARCH_INTERVAL_MINUTES`)
- **Import**: every 30 minutes (`IMPORT_INTERVAL_MINUTES`)
- **Clean**: every 4 hours (`CLEAN_INTERVAL_MINUTES`)

The daemon checks the timers every 60 seconds (`DAEMON_INTERVAL_SECONDS`).

You can check everything it does in real-time by reading the logs:
```bash
docker logs -f seerr-sentinel
```

Alternatively, you can manually trigger operations inside the container:
```bash
docker exec -it seerr-sentinel python3 seerr_sentinel.py clean --dry-run
docker exec -it seerr-sentinel python3 seerr_sentinel.py search
```

### Manual Usage (Python)

If you are running the scripts manually:

#### Simple

```bash
# Check your environment setup before anything else
python3 seerr_sentinel.py --health-check

# Run everything in one go (dry-run to test safely first)
python3 seerr_sentinel.py all --dry-run

# Run everything in one go
python3 seerr_sentinel.py all
```

### Step by step

```bash
# Trigger Radarr/Sonarr searches for missing media
python3 seerr_sentinel.py search

# Cleanup (dry-run to test safely first)
python3 seerr_sentinel.py clean --dry-run
python3 seerr_sentinel.py clean

# Video file injection
python3 seerr_sentinel.py import
python3 seerr_sentinel.py import --sonarr --force-id 42
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
| `STUCK_DOWNLOAD_MINUTES` | optional | Minutes to wait before removing a download with <= 5% progress (default: `20.0`) |
| `MAX_DOWNLOAD_HOURS` | optional | Maximum hours before a download is removed regardless of progress (default: `6.0`) |
| `DAEMON_INTERVAL_SECONDS` | optional | How often the background daemon checks the timers (default: `60`) |
| `SEARCH_INTERVAL_MINUTES` | optional | How frequently the search module runs (default: `15`) |
| `IMPORT_INTERVAL_MINUTES` | optional | How frequently the import module runs (default: `30`) |
| `CLEAN_INTERVAL_MINUTES` | optional | How frequently the clean module runs (default: `240`) |

## Architecture

```
seerr_sentinel.py          ← orchestrator + load_config() + scheduling
├── sentinel_search.py     ← Radarr/Sonarr MoviesSearch / SeasonSearch / EpisodeSearch
├── sentinel_cleaner.py    ← missing media detection + Radarr/Sonarr/Jellyseerr deletion
└── sentinel_import.py     ← hard-link injection + Radarr/Sonarr rescan
```

### `seerr_sentinel.py all` and `daemon` logic

When running the `all` command (or the `daemon` mode), the script manages its own sub-intervals via a lightweight local JSON cache. By default these are the timers (which can be overriden via `.env` variables):
- **Search**: Executes only every 15 min.
- **Import**: Executes only if 30 minutes have passed since the last run.
- **Clean**: Executes only if 4 hours have passed since the last run.

### `sentinel_search` logic

1. Checks for active commands (global lock)
3. Looks for a missing Radarr candidate → triggers `MoviesSearch`
4. If nothing on Radarr side → looks at Sonarr → `SeasonSearch` or `EpisodeSearch`
5. Per-cycle quota (12h) to avoid flooding indexers

### `sentinel_cleaner` logic

1. Fetches all missing media from Radarr/Sonarr
2. Ignores recent releases (`RELEASE_BUFFER_DAYS`)
3. After `DELETION_DELAY_DAYS` days → deletes from Radarr/Sonarr and Jellyseerr
4. Keeps Jellyseerr requests older than `KEEP_REQUESTS_OLDER_THAN_DAYS` days
5. Detects stuck downloads in Radarr/Sonarr queues (<= 5% progress after `STUCK_DOWNLOAD_MINUTES` or any progress after `MAX_DOWNLOAD_HOURS`) and blocklists them

### `sentinel_import` logic

1. Scans the `DOWNLOADS_PATH` folder
2. Matches files against missing media using title tokens + TMDB aliases
3. Creates hard-links in Radarr/Sonarr media folders
4. Triggers a `RescanMovie` / `RescanSeries` and waits for confirmation