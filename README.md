# Jellyseerr Utils Script

This repository contains utility scripts for managing and cleaning up your Jellyseerr, Radarr, and Sonarr instances.

## Jellyseerr Cleaner (`jellyseerr_cleaner.py`)

The `jellyseerr_cleaner.py` script is designed to keep your media libraries clean by synchronizing Jellyseerr requests with the actual status of media in Radarr and Sonarr. It automatically identifies and removes media that has been requested but is considered "stalled" or "missing" (e.g., movies listed but never downloaded, or series with missing episodes) after a configurable buffer period.

### Features

*   **Stalled Media Detection**: Identifies movies in Radarr with no files and series in Sonarr with missing episode files.
*   **Release Buffer**: waits for a specified number of days (`RELEASE_BUFFER_DAYS`) after a media's release date before considering it for removal, giving your download clients time to find releases.
*   **Deletion Grace Period**: Implements a "safety net" (`DELETION_DELAY_DAYS`). Items identified for deletion are first tracked in a pending list. They are only deleted if they remain in the "missing" state for the duration of the grace period (default 2 days).
*   **Jellyseerr Sync**: When media is deleted from Radarr/Sonarr, the corresponding request is also removed from Jellyseerr.
*   **Two-Step Deletion**: To prevent accidental mass deletions, the script operates in a two-step "confirm" mode:
    1.  **First Run**: Generates a report of candidates. If items are ready for deletion (passed grace period), they are written to a temporary file.
    2.  **Second Run**: If a temporary file from the previous run exists, the script executes the deletions.
*   **Dry Run Mode**: Allows you to simulate the process and see what would be marked for deletion without actually modifying your services.

### Prerequisites

*   Python 3
*   `requests` library
*   `python-dotenv` library

### Installation

1.  Clone this repository.
2.  Install dependencies:
    ```bash
    pip install requests python-dotenv
    ```
3.  Create a `.env` file in the same directory as the script (or ensure the script can locate it).

### Configuration (`.env`)

Configure the following variables in your `.env` file:

```dotenv
# Jellyseerr
JELLYSEER_API_KEY="your_jellyseerr_api_key"
JELLYSEER_URL="http://ip:port"

# TMDB (Used for title resolution)
TMDB_API_KEY="your_tmdb_api_key"

# Radarr
RADARR_API_KEY="your_radarr_api_key"
RADARR_URL="http://ip:port"

# Sonarr
SONARR_API_KEY="your_sonarr_api_key"
SONARR_URL="http://ip:port"

# Configuration (Optional)
RELEASE_BUFFER_DAYS=7   # Days to wait after release before checking (Default: 7)
DELETION_DELAY_DAYS=2   # Days to wait in "pending" state before deletion (Default: 2)
```

### Usage

**1. Standard Run (Report & Identify)**
Run the script to check for missing media and update the pending deletion list.
```bash
python3 jellyseerr_cleaner.py
```
*   This will print a report of missing movies/series.
*   It tracks "first seen" times for new candidates.
*   If candidates have passed their grace period (`DELETION_DELAY_DAYS`), they are staged for deletion (written to a temp file).

**2. Execute Deletions**
If the previous run staged items for deletion, running the command again will execute the deletions.
```bash
python3 jellyseerr_cleaner.py
```
*   The script detects the temporary file created by the previous run.
*   It proceeds to delete the staged items from Radarr, Sonarr, and Jellyseerr.
*   The temporary file is automatically cleared after deletions.

**3. Dry Run**
To see what the script *would* report or stage without risk of triggering a deletion phase on the next run (effectively resets the temp file):
```bash
python3 jellyseerr_cleaner.py --dry-run
```

### Logic Flow

1.  **Check**: The script queries Radarr and Sonarr for items that are missing files.
2.  **Filter**: It ignores items released recently (within `RELEASE_BUFFER_DAYS`).
3.  **Track**: "Missing" items are added to a pending list (`/tmp/jellyseerr_pending_deletions.json`).
4.  **Grace Period**: The script checks how long an item has been pending.
5.  **Stage**: If an item has been pending longer than `DELETION_DELAY_DAYS`, it is added to a "ready to delete" list (`/tmp/jellyseerr_deletions.json`).
6.  **Execute**: If the script is run and finds the "ready to delete" list, it performs the removal.

## Jellyseerr Search Automation (`jellyseerr_search.py`)

The `jellyseerr_search.py` script automates the process of searching for "missing" content in Radarr and Sonarr. It helps trigger searches for items that are monitored but haven't been grabbed yet, applying smart filtering and cooldown logic to avoid API rate limits and redundant searches.

### Features

*   **Smart Detection**: Identifies movies in Radarr and episodes in Sonarr that are monitored, missing, and *released* (available).
*   **Release Date Filtering**: Ignores unreleased content (future release dates) to prevent useless searches.
*   **Cooldown System**:
    *   **Request Cooldown**: Prevents searching for the same item more than once every 12 hours by default.
    *   **Activity Cooldown**: Checks if the download client or indexer is currently busy (e.g., a search started by default < 10 mins ago) to avoid overloading the system.
*   **Sonarr Optimization**:
    *   Prioritizes "Season Search" if an entire season is missing or has multiple missing episodes.
    *   Falls back to "Episode Search" for individual missing episodes.
    *   Executes searches sequentially to prevent flooding. (first episode, then second, then third, etc.)
*   **Mutual Exclusion**: Ensures that Radarr and Sonarr search processes do not run simultaneously (global lock mechanism) to manage system load.

### Logic Flow

1.  **Global Lock Check**: Checks if Radarr or Sonarr are currently running a search command. If so, it aborts to prevent conflicts.
2.  **Radarr Check**:
    *   Lists missing movies.
    *   Filters out unreleased movies.
    *   Checks the local history file for the last search time.
    *   If a candidate is found and "cool" (not searched recently), triggers a `MoviesSearch` command.
3.  **Sonarr Check** (Only if Radarr didn't take action):
    *   Lists missing episodes.
    *   Groups them by Series and Season.
    *   Prioritizes triggering a `SeasonSearch` if justifiable.
    *   Otherwise, triggers an `EpisodeSearch` for the first missing episode.
4.  **History Tracking**: timestamps of searches are recorded in `/tmp/force_search_history.json` to manage cooldowns.

### Usage

This script is intended to be run via a cron job (e.g., every 5-15 minutes).

```bash
python3 jellyseerr_search.py
```

*   **No arguments**: The script runs its logic, performs at most *one* search action (Radarr OR Sonarr), and exits. This "slow and steady" approach ensures your indexers are not hammered.

### Configuration

The script uses the same `.env` file as the cleaner script. Ensure the following are set:
*   `RADARR_API_KEY`, `RADARR_URL`
*   `SONARR_API_KEY`, `SONARR_URL`