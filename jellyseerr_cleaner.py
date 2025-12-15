import argparse
import requests
import os
import json
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from pathlib import Path
import sys

env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)

required_vars = {
    "JELLYSEER_API_KEY": os.environ.get("JELLYSEER_API_KEY"),
    "JELLYSEER_URL": os.environ.get("JELLYSEER_URL"),
    "TMDB_API_KEY": os.environ.get("TMDB_API_KEY"),
    "RADARR_API_KEY": os.environ.get("RADARR_API_KEY"),
    "RADARR_URL": os.environ.get("RADARR_URL"),
    "SONARR_API_KEY": os.environ.get("SONARR_API_KEY"),
    "SONARR_URL": os.environ.get("SONARR_URL"),
}

missing_vars = [key for key, value in required_vars.items() if not value]

if missing_vars:
    print(f"Error: The following required environment variables are missing in .env: {', '.join(missing_vars)}")
    sys.exit(1)

JELLYSEER_API_KEY = required_vars["JELLYSEER_API_KEY"]
JELLYSEER_URL = required_vars["JELLYSEER_URL"]
TMDB_API_KEY = required_vars["TMDB_API_KEY"]
RADARR_API_KEY = required_vars["RADARR_API_KEY"]
RADARR_URL = required_vars["RADARR_URL"]
SONARR_API_KEY = required_vars["SONARR_API_KEY"]
SONARR_URL = required_vars["SONARR_URL"]
TARGET_TMDB_IDS = []
TMDB_TITLE_CACHE = {}
RELEASE_BUFFER_DAYS = int(os.environ.get("RELEASE_BUFFER_DAYS", "7"))
DELETION_DELAY_DAYS = int(os.environ.get("DELETION_DELAY_DAYS", "2"))
TEMP_FILE = "/tmp/jellyseerr_deletions.json"
PENDING_FILE = "/tmp/jellyseerr_pending_deletions.json"


def normalize_media_type(media_type, default="movie"):  # pragma: no cover - simple helper
    if not media_type:
        return default
    normalized = media_type.lower()
    if normalized in {"tv", "series", "tv_show"}:
        return "tv"
    return "movie"


def normalize_tmdb_id(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def parse_iso_datetime(input_str):
    if not input_str:
        return None
    try:
        normalized = input_str.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        try:
            return datetime.fromisoformat(input_str)
        except ValueError:
            return None


def is_release_due(release_date_str, buffer_days=7):
    release_dt = parse_iso_datetime(release_date_str)
    if not release_dt:
        return True
    now = datetime.now(timezone.utc)
    release_dt_utc = release_dt.astimezone(timezone.utc)
    return release_dt_utc <= now - timedelta(days=buffer_days)


def get_sonarr_next_airing(series):
    next_airing = series.get("nextAiring")
    if next_airing:
        return next_airing

    next_episode = series.get("nextAiringEpisode") or {}
    if next_episode:
        return next_episode.get("airDateUtc")

    for season in series.get("seasons", []):
        stats = season.get("statistics", {})
        candidate = stats.get("nextAiring") or stats.get("nextAiringEpisode")
        if candidate:
            return candidate

    return None

def resolve_media_title(media, tmdb_api_key, media_type_hint=None):
    tmdb_id = media.get("tmdbId")
    title = media.get("title") or media.get("name")
    media_type = normalize_media_type(media_type_hint or media.get("mediaType"))

    if not title and tmdb_id:
        try:
            title = get_tmdb_title(tmdb_id, tmdb_api_key, media_type=media_type)
        except Exception:
            title = None

    return title

def friendly_radarr_title(movie):
    title = resolve_media_title(movie, TMDB_API_KEY, media_type_hint="movie")
    if title:
        return title
    tmdb_id = movie.get("tmdbId")
    if tmdb_id:
        return f"TMDB {tmdb_id}"
    return "Unknown Radarr Movie"

def friendly_sonarr_title(series):
    title = resolve_media_title(series, TMDB_API_KEY, media_type_hint="tv")
    if title:
        return title
    tmdb_id = series.get("tmdbId")
    if tmdb_id:
        return f"TMDB {tmdb_id}"
    return "Unknown Sonarr Series"

def write_to_temp_file(data):
    with open(TEMP_FILE, "w") as f:
        json.dump(data, f, indent=4)

def read_from_temp_file():
    if os.path.exists(TEMP_FILE):
        with open(TEMP_FILE, "r") as f:
            return json.load(f)
    return {"radarr": [], "sonarr": [], "jellyseerr": []}


def load_pending_deletions():
    if os.path.exists(PENDING_FILE):
        try:
            with open(PENDING_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}

def save_pending_deletions(data):
    with open(PENDING_FILE, "w") as f:
        json.dump(data, f, indent=4)


def parse_command_line_arguments():
    parser = argparse.ArgumentParser("Jellyseerr cleaner", description="Syncs Radarr/Sonarr with Jellyseerr requests")
    parser.add_argument(
        "-d",
        "--dry-run",
        action="store_true",
        help="Only output what would change without modifying any service",
    )
    return parser.parse_args()

def get_tmdb_title(tmdb_id, tmdb_api_key, media_type="movie"):
    cache_key = (tmdb_id, media_type)
    if cache_key in TMDB_TITLE_CACHE:
        return TMDB_TITLE_CACHE[cache_key]

    tmdb_url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}?api_key={tmdb_api_key}"
    response = requests.get(tmdb_url)
    response.raise_for_status()
    data = response.json()
    title = data.get("title") or data.get("name")
    TMDB_TITLE_CACHE[cache_key] = title
    return title

def fetch_jellyseerr_requests(api_key, base_url):
    take = 10
    skip = 0
    headers = {"X-Api-Key": api_key}
    requests_list = []
    seen_tmdb = set()

    url = f"{base_url}/api/v1/request?sort=added&sortDirection=desc&skip={skip}&take={take}"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()

    if data.get("results"):
        for item in data["results"]:
            media = item.get("media", {})
            tmdb_id = media.get("tmdbId")
            if not tmdb_id or tmdb_id in seen_tmdb:
                continue
            media_type = normalize_media_type(media.get("mediaType"))
            title = media.get("title") or media.get("name")
            media_id = media.get("id")
            if not title and tmdb_id:
                try:
                    title = get_tmdb_title(tmdb_id, TMDB_API_KEY, media_type=media_type)
                except Exception:
                    title = "Unknown Title"
            seen_tmdb.add(tmdb_id)
            requests_list.append(
                {
                    "title": title or "Unknown Title",
                    "media_id": media_id,
                    "tmdb_id": tmdb_id,
                    "media_type": media_type,
                }
            )

    return requests_list

def delete_jellyseerr_requests(api_key, base_url, requests_to_delete):
    if not requests_to_delete:
        print("No Jellyseerr requests to delete.")
        return 0

    headers = {"X-Api-Key": api_key}
    deleted_count = 0
    for request in requests_to_delete:
        media_id = request.get("media_id")
        tmdb_id = request.get("tmdb_id")
        title = request.get("title") or (f"TMDB {tmdb_id}" if tmdb_id else "Unknown Jellyseerr Media")
        print(f"Deleting Jellyseerr request {title} (ID: {media_id}, TMDB ID: {tmdb_id})")
        delete_url = f"{base_url}/api/v1/media/{media_id}"
        delete_response = requests.delete(delete_url, headers=headers)
        delete_response.raise_for_status()
        print(f"Successfully deleted Jellyseerr request {title} (media ID: {media_id})")
        deleted_count += 1

    print(f"Deleted {deleted_count} Jellyseerr request(s).")
    return deleted_count

def resolve_jellyseerr_delete_requests(jellyseerr_entries, api_key, base_url):
    if not jellyseerr_entries:
        return []

    if all(isinstance(entry, dict) for entry in jellyseerr_entries):
        return jellyseerr_entries

    request_candidates = {
        req.get("tmdb_id"): req
        for req in fetch_jellyseerr_requests(api_key, base_url)
        if req.get("tmdb_id")
    }

    normalized_requests = []
    for entry in jellyseerr_entries:
        tmdb_id = entry
        if isinstance(entry, dict):
            tmdb_id = entry.get("tmdb_id")
        if tmdb_id is None:
            continue
        try:
            tmdb_id = int(tmdb_id)
        except (TypeError, ValueError):
            pass
        request = request_candidates.get(tmdb_id)
        if request:
            normalized_requests.append(request)
        else:
            print(f"Warning: Jellyseerr request for TMDB ID {tmdb_id} not found in current requests.")

    return normalized_requests

def get_all_radarr_movies_with_status(api_key, base_url):
    headers = {"X-Api-Key": api_key}
    url = f"{base_url}/api/v3/movie"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    movies = response.json()
    return {movie.get("tmdbId"): movie for movie in movies if "tmdbId" in movie}

def get_all_sonarr_series_with_status(api_key, base_url):
    headers = {"X-Api-Key": api_key}
    url = f"{base_url}/api/v3/series"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    series_list = response.json()
    return {series.get("tmdbId"): series for series in series_list if "tmdbId" in series}

def get_radarr_missing_movies(api_key, base_url):
    all_movies = get_all_radarr_movies_with_status(api_key, base_url)
    missing_movies = {}
    for tmdb_id, movie in all_movies.items():
        if tmdb_id is None:
            continue
        has_file = movie.get("hasFile")
        if has_file is False:
            release_date = movie.get("releaseDate") or movie.get("inCinemas")
            if not is_release_due(release_date):
                continue
            title = movie.get("title") or get_tmdb_title(tmdb_id, TMDB_API_KEY, media_type="movie")
            missing_movies[tmdb_id] = title or f"TMDB {tmdb_id}"
    return missing_movies

def get_sonarr_missing_episodes(api_key, base_url):
    all_series = get_all_sonarr_series_with_status(api_key, base_url)
    missing_items = {}
    for tmdb_id, series in all_series.items():
        if tmdb_id is None:
            continue
        
        # Check series-level safeguards first
        next_air_date = get_sonarr_next_airing(series)
        if not is_release_due(next_air_date, RELEASE_BUFFER_DAYS):
            continue
        
        previous_airing = series.get("previousAiring")
        if previous_airing and not is_release_due(previous_airing, RELEASE_BUFFER_DAYS):
            continue

        # Now check seasons
        seasons = series.get("seasons", [])
        monitored_seasons_count = 0
        problematic_seasons = []
        
        for season in seasons:
            if not season.get("monitored"):
                continue
            monitored_seasons_count += 1
            
            stats = season.get("statistics", {})
            file_count = stats.get("episodeFileCount", 0)
            total_count = stats.get("episodeCount", 0)
            
            if file_count < total_count:
                problematic_seasons.append(season.get("seasonNumber"))
        
        if not problematic_seasons:
            continue
            
        title = series.get("title") or get_tmdb_title(tmdb_id, TMDB_API_KEY, media_type="tv")
        
        if len(problematic_seasons) == monitored_seasons_count:
             action = "delete_series"
        else:
             action = "unmonitor_seasons"
             
        missing_items[tmdb_id] = {
            "title": title or f"TMDB {tmdb_id}",
            "tmdb_id": tmdb_id,
            "series_id": series.get("id"),
            "action": action,
            "seasons": problematic_seasons
        }
            
    return missing_items

def delete_radarr_movie(api_key, base_url, tmdb_id):
    headers = {"X-Api-Key": api_key}
    # First, find the movie in Radarr by tmdbId
    lookup_url = f"{base_url}/api/v3/movie?tmdbId={tmdb_id}"
    response = requests.get(lookup_url, headers=headers)
    response.raise_for_status()
    movies = response.json()

    if not movies:
        print(f"Movie with TMDB ID {tmdb_id} not found in Radarr.")
        return

    # Assuming the first result is the correct one
    movie_id = movies[0]["id"]
    delete_url = f"{base_url}/api/v3/movie/{movie_id}"
    # Set deleteFiles to true to remove associated files and add exclusion to prevent re-adding
    params = {"deleteFiles": "true", "addExclusion": "true"}
    delete_response = requests.delete(delete_url, headers=headers, params=params)
    delete_response.raise_for_status()
    print(f"Successfully deleted movie with TMDB ID {tmdb_id} from Radarr.")

def unmonitor_sonarr_seasons(api_key, base_url, series_id, seasons_to_unmonitor):
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    
    # Get series first to have current state
    get_url = f"{base_url}/api/v3/series/{series_id}"
    resp = requests.get(get_url, headers=headers)
    resp.raise_for_status()
    series_data = resp.json()
    
    updated = False
    for season in series_data.get("seasons", []):
        if season["seasonNumber"] in seasons_to_unmonitor:
            if season["monitored"]:
                season["monitored"] = False
                updated = True
    
    if updated:
        put_url = f"{base_url}/api/v3/series/{series_id}"
        resp = requests.put(put_url, headers=headers, json=series_data)
        resp.raise_for_status()
        print(f"Unmonitored seasons {seasons_to_unmonitor} for series {series_data.get('title')}")
    else:
        print(f"Seasons {seasons_to_unmonitor} were already unmonitored for {series_data.get('title')}")

def delete_sonarr_series(api_key, base_url, tmdb_id):
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    # First, find the series in Sonarr by tmdbId
    lookup_url = f"{base_url}/api/v3/series?tmdbId={tmdb_id}"
    response = requests.get(lookup_url, headers={"X-Api-Key": api_key})
    response.raise_for_status()
    series_list = response.json()

    if not series_list:
        print(f"Series with TMDB ID {tmdb_id} not found in Sonarr.")
        return
    target_tmdb = normalize_tmdb_id(tmdb_id)
    matching_series = next(
        (
            series
            for series in series_list
            if normalize_tmdb_id(series.get("tmdbId")) == target_tmdb
        ),
        None,
    )
    if not matching_series:
        print(
            f"Series with TMDB ID {tmdb_id} was not among the lookup results for Sonarr; skipping."
        )
        return
    series_id = matching_series["id"]
    delete_url = f"{base_url}/api/v3/series/editor"
    payload = {
        "seriesIds": [series_id],
        "moveFiles": True,
        "deleteFiles": True,
        "addImportListExclusion": True,
    }

    delete_response = requests.delete(delete_url, headers=headers, json=payload)
    delete_response.raise_for_status()
    print(f"Successfully deleted series with TMDB ID {tmdb_id} from Sonarr.")



def get_all_radarr_movies(api_key, base_url):
    headers = {"X-Api-Key": api_key}
    url = f"{base_url}/api/v3/movie"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    movies = response.json()
    # Filter for movies that are 'missing'
    missing_movies = {}
    for movie in movies:
        tmdb_id = movie.get("tmdbId")
        has_file = movie.get("hasFile")
        title = movie.get("title") or (get_tmdb_title(tmdb_id, TMDB_API_KEY) if tmdb_id else None)
        print(f"Radarr Movie: {title}, Status: {movie.get('status')}, HasFile: {has_file}, TMDB ID: {tmdb_id}")
        if tmdb_id and has_file is False:
            missing_movies[tmdb_id] = title or f"TMDB {tmdb_id}"
    return missing_movies

def get_all_sonarr_series(api_key, base_url):
    headers = {"X-Api-Key": api_key}
    url = f"{base_url}/api/v3/series"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    series_list = response.json()
    missing_series = {}
    for series in series_list:
        tmdb_id = series.get("tmdbId")
        stats = series.get("statistics", {})
        episode_file_count = stats.get("episodeFileCount", 0)
        episode_count = stats.get("episodeCount", 0)
        title = series.get("title") or (get_tmdb_title(tmdb_id, TMDB_API_KEY, media_type="tv") if tmdb_id else None)
        print(f"Sonarr Series: {title}, Status: {series.get('status')}, EpisodeFileCount: {episode_file_count}/{episode_count}, TMDB ID: {tmdb_id}")
        if tmdb_id and episode_file_count < episode_count:
            missing_series[tmdb_id] = title or f"TMDB {tmdb_id}"
    return missing_series

def get_jellyseerr_library_media(api_key, base_url):
    headers = {"X-Api-Key": api_key}
    url = f"{base_url}/api/v1/media?"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    
    library_media = {}
    for item in data["results"]:
        tmdb_id = item.get("tmdbId")
        if not tmdb_id:
            continue
        title = resolve_media_title(item, TMDB_API_KEY, media_type_hint=item.get("mediaType"))
        library_media[tmdb_id] = title or f"TMDB {tmdb_id}"
    return library_media

def perform_deletions(radarr_api_key, radarr_url, sonarr_api_key, sonarr_url):
    data_to_delete = read_from_temp_file()

    if data_to_delete["radarr"]:
        print(f"Deleting movies from Radarr: {data_to_delete['radarr']}")
        for tmdb_id in data_to_delete["radarr"]:
            delete_radarr_movie(radarr_api_key, radarr_url, tmdb_id)
    else:
        print("Nothing to delete from Radarr.")

    if data_to_delete["sonarr"]:
        print(f"Processing series in Sonarr: {data_to_delete['sonarr']}")
        for item in data_to_delete["sonarr"]:
            # Handle both old format (ID only) and new format (dict) for backward compat slightly, though new file structure overrides
            if isinstance(item, dict):
                tmdb_id = item["tmdb_id"]
                action = item.get("action", "delete_series")
                if action == "delete_series":
                    delete_sonarr_series(sonarr_api_key, sonarr_url, tmdb_id)
                elif action == "unmonitor_seasons":
                    series_id = item.get("series_id")
                    seasons = item.get("seasons", [])
                    if series_id and seasons:
                        unmonitor_sonarr_seasons(sonarr_api_key, sonarr_url, series_id, seasons)
            else:
                 # Fallback for simple ID list
                 delete_sonarr_series(sonarr_api_key, sonarr_url, item)
    else:
        print("Nothing to delete from Sonarr.")

    jellyseerr_requests_to_delete = data_to_delete.get("jellyseerr", []) or []
    if jellyseerr_requests_to_delete:
        print("Deleting requested media from Jellyseerr:")
        for request in jellyseerr_requests_to_delete:
            if isinstance(request, dict):
                title = request.get("title") or f"TMDB {request.get('tmdb_id')}"
            else:
                title = f"TMDB {request}"
            media_id = request.get("media_id") if isinstance(request, dict) else None
            tmdb_id = request.get("tmdb_id") if isinstance(request, dict) else request
            print(f"- {title} (ID: {media_id}, TMDB ID: {tmdb_id})")
        jellyseerr_requests_to_delete = resolve_jellyseerr_delete_requests(
            jellyseerr_requests_to_delete, JELLYSEER_API_KEY, JELLYSEER_URL
        )
        delete_jellyseerr_requests(JELLYSEER_API_KEY, JELLYSEER_URL, jellyseerr_requests_to_delete)
    else:
        print("Nothing to delete from Jellyseerr.")

    # Clear the temp file after deletions
    if os.path.exists(TEMP_FILE):
        os.remove(TEMP_FILE)
        print("Temp file cleared after deletions.")


def generate_missing_media_report(dry_run=False):
    radarr_missing = get_radarr_missing_movies(RADARR_API_KEY, RADARR_URL)
    sonarr_missing = get_sonarr_missing_episodes(SONARR_API_KEY, SONARR_URL)
    jellyseerr_requests = fetch_jellyseerr_requests(JELLYSEER_API_KEY, JELLYSEER_URL)
    jellyseerr_library = get_jellyseerr_library_media(JELLYSEER_API_KEY, JELLYSEER_URL)

    jellyseerr_request_map = {
        movie.get("tmdb_id"): movie
        for movie in jellyseerr_requests
        if movie.get("tmdb_id") and movie.get("media_id")
    }

    all_radarr_movies = get_all_radarr_movies_with_status(RADARR_API_KEY, RADARR_URL)
    all_sonarr_series = get_all_sonarr_series_with_status(SONARR_API_KEY, SONARR_URL)

    print("\n--- Radarr Movies ---")
    for tmdb_id, movie in all_radarr_movies.items():
        title = friendly_radarr_title(movie)
        release_date = movie.get("releaseDate") or movie.get("inCinemas") or "Unknown"
        print(
            f"{title} (TMDB ID: {tmdb_id}), Has File: {movie.get('hasFile')}, Release Date: {release_date}"
        )

    print("\n--- Sonarr Series ---")
    for tmdb_id, series in all_sonarr_series.items():
        title = friendly_sonarr_title(series)
        episode_count = series.get("statistics", {}).get("episodeFileCount")
        next_air_date = get_sonarr_next_airing(series) or "Unknown"
        print(
            f"{title} (TMDB ID: {tmdb_id}), Episode File Count: {episode_count}, Next Airing: {next_air_date}"
        )

    print("--- Jellyseerr Media ---")
    print("Recent media found in requests:")
    if jellyseerr_requests:
        for movie in jellyseerr_requests:
            print(f"- {movie['title']} (ID: {movie['media_id']}, TMDB ID: {movie['tmdb_id']})")
    else:
        print("No media found in requests.")

    print(f"Library contains {len(jellyseerr_library)} entries.")

    if radarr_missing:
        print("\nRadarr movies to be considered missing:")
        for tmdb_id, title in radarr_missing.items():
            print(f"- {title} (TMDB ID: {tmdb_id})")
    else:
        print("\nNo Radarr movies considered missing.")

    if sonarr_missing:
        print("Sonarr series to be considered missing/incomplete:")
        for tmdb_id, info in sonarr_missing.items():
            action = info['action']
            seasons = info['seasons']
            print(f"- {info['title']} (TMDB ID: {tmdb_id}) -> Action: {action}, Seasons: {seasons}")
    else:
        print("No Sonarr series considered missing.")

    # Identify all current candidates for deletion
    current_candidates_tmdb_ids = set(radarr_missing) | set(sonarr_missing)
    
    # Load previously pending deletions
    pending_deletions = load_pending_deletions()
    
    # Update pending list
    now = datetime.now(timezone.utc)
    updated_pending = {}
    
    ready_to_delete_jellyseerr = []
    
    # Process current candidates
    for tmdb_id in current_candidates_tmdb_ids:
        tmdb_id_str = str(tmdb_id)
        if tmdb_id_str in pending_deletions:
            # Keeps exisiting entry
            entry = pending_deletions[tmdb_id_str]
        else:
            # New candidate
            entry = {
                "first_seen": now.isoformat(),
                "tmdb_id": tmdb_id
            }
        
        updated_pending[tmdb_id_str] = entry
        
        # Check grace period
        first_seen = datetime.fromisoformat(entry["first_seen"])
        if (now - first_seen).days >= DELETION_DELAY_DAYS:
            # Ready for deletion
             if tmdb_id in jellyseerr_request_map:
                request = jellyseerr_request_map[tmdb_id]
                ready_to_delete_jellyseerr.append({
                    "title": request.get("title") or f"TMDB {tmdb_id}",
                    "media_id": request["media_id"],
                    "tmdb_id": tmdb_id,
                })
        else:
             print(f"Deferring deletion for TMDB ID {tmdb_id} (In grace period, first seen {entry['first_seen']})")

    # Save updated pending state
    save_pending_deletions(updated_pending)

    if ready_to_delete_jellyseerr:
        print("\nJellyseerr requests READY to delete (passed grace period):")
        for request in ready_to_delete_jellyseerr:
            print(
                f"- {request['title']} (ID: {request['media_id']}, TMDB ID: {request['tmdb_id']})"
            )
    else:
        print("\nNo Jellyseerr media ready to delete.")

    radarr_ready = [
        tmdb_id for tmdb_id in radarr_missing 
        if str(tmdb_id) in updated_pending and 
        (now - datetime.fromisoformat(updated_pending[str(tmdb_id)]["first_seen"])).days >= DELETION_DELAY_DAYS
    ]
    
    sonarr_ready = []
    for tmdb_id_str, entry in updated_pending.items():
        tmdb_id = int(tmdb_id_str)
        if tmdb_id in sonarr_missing:
            # Check grace period
             if (now - datetime.fromisoformat(entry["first_seen"])).days >= DELETION_DELAY_DAYS:
                 sonarr_ready.append(sonarr_missing[tmdb_id])

    temp_data = {
        "radarr": radarr_ready,
        "sonarr": sonarr_ready,
        "jellyseerr": ready_to_delete_jellyseerr,
    }
    
    if any(temp_data.values()):
        write_to_temp_file(temp_data)
        if dry_run:
            print("\nDry run mode enabled. Ready deletions written to temp file (overwritten). Run without -d to perform deletions.")
        else:
            print("\nTemp file created with READY deletions. Run the script again to perform deletions.")
    else:
        print("\nNo media ready for deletion detected. Skipping temp file creation (or creating empty one).")
        if os.path.exists(TEMP_FILE):
            os.remove(TEMP_FILE)

if __name__ == "__main__":
    args = parse_command_line_arguments()
    if args.dry_run:
        if os.path.exists(TEMP_FILE):
            os.remove(TEMP_FILE)
            print("Dry run flag provided: existing temp file removed before reporting.")
        generate_missing_media_report(dry_run=True)
    else:
        if os.path.exists(TEMP_FILE):
            print("Temp file found. Performing deletions...")
            perform_deletions(RADARR_API_KEY, RADARR_URL, SONARR_API_KEY, SONARR_URL)
        else:
            generate_missing_media_report()