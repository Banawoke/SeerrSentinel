#!/usr/bin/env python3
"""
SeerrSentinel Cleaner — Detection and cleanup of missing/stalled media.

Can be run directly or via:
    python3 seerr_sentinel.py clean [--dry-run]
"""

import argparse
import requests
import os
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

# Config centralisée (charge aussi le .env)
from seerr_sentinel import load_config

_cfg = load_config([
    "JELLYSEER_API_KEY",
    "JELLYSEER_URL",
    "TMDB_API_KEY",
    "RADARR_API_KEY",
    "RADARR_URL",
    "SONARR_API_KEY",
    "SONARR_URL",
])

JELLYSEER_API_KEY = _cfg["JELLYSEER_API_KEY"]
JELLYSEER_URL = _cfg["JELLYSEER_URL"]
TMDB_API_KEY = _cfg["TMDB_API_KEY"]
RADARR_API_KEY = _cfg["RADARR_API_KEY"]
RADARR_URL = _cfg["RADARR_URL"]
SONARR_API_KEY = _cfg["SONARR_API_KEY"]
SONARR_URL = _cfg["SONARR_URL"]
TARGET_TMDB_IDS = []
TMDB_TITLE_CACHE = {}
RELEASE_BUFFER_DAYS = int(os.environ.get("RELEASE_BUFFER_DAYS", "7"))
DELETION_DELAY_DAYS = int(os.environ.get("DELETION_DELAY_DAYS", "2"))
KEEP_REQUESTS_OLDER_THAN_DAYS = int(os.environ.get("KEEP_REQUESTS_OLDER_THAN_DAYS", "14"))
STUCK_DOWNLOAD_MINUTES = float(os.environ.get("STUCK_DOWNLOAD_MINUTES", "20.0"))
MAX_DOWNLOAD_HOURS = float(os.environ.get("MAX_DOWNLOAD_HOURS", "6.0"))

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
            
            # Extract new fields
            created_at = item.get("createdAt")
            requested_by = item.get("requestedBy", {})
            requester = requested_by.get("displayName") or requested_by.get("email") or requested_by.get("username") or "Unknown"

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
                    "request_date": created_at,
                    "requested_by": requester,
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

        # If API says file exists, but sizeOnDisk is 0, the file is gone
        if has_file is True:
            size_on_disk = movie.get("sizeOnDisk", 0) or movie.get("movieFile", {}).get("size", 0)
            if size_on_disk == 0:
                title = movie.get("title") or get_tmdb_title(tmdb_id, TMDB_API_KEY, media_type="movie")
                print(f"  [GHOST] {title} (TMDB {tmdb_id}): hasFile=True but sizeOnDisk=0!")
                has_file = False

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

        # Series-level ghost detection via sizeOnDisk
        series_stats = series.get("statistics", {})
        series_size_on_disk = series_stats.get("sizeOnDisk", 0)
        api_total_files = series_stats.get("episodeFileCount", 0)
        is_ghost_series = api_total_files > 0 and series_size_on_disk == 0

        if is_ghost_series:
            title = series.get("title") or f"TMDB {tmdb_id}"
            print(f"  [GHOST] {title} (TMDB {tmdb_id}): API says {api_total_files} files but sizeOnDisk=0!")

        # Now check seasons
        seasons = series.get("seasons", [])
        monitored_seasons_count = 0
        actionable_seasons = [] # Seasons with 0 files (to be unmonitored)
        report_seasons = []     # All seasons with missing files (for logging)
        
        for season in seasons:
            if not season.get("monitored"):
                continue
            monitored_seasons_count += 1
            
            stats = season.get("statistics", {})
            file_count = stats.get("episodeFileCount", 0)
            total_count = stats.get("episodeCount", 0)
            season_size = stats.get("sizeOnDisk", 0)

            # Ghost detection: API says files exist but sizeOnDisk is 0
            if file_count > 0 and season_size == 0:
                print(f"  [GHOST] Season {season.get('seasonNumber')}: {file_count} files but sizeOnDisk=0")
                file_count = 0
            
            # If ANY files are missing, we report it
            if file_count < total_count and total_count > 0:
                report_seasons.append(season.get("seasonNumber"))
                
                # We only take ACTION (unmonitor/delete) if 0 files exist
                if file_count == 0:
                    actionable_seasons.append(season.get("seasonNumber"))
        
        # Empty series detection: all monitored seasons have 0 episodes AND 0 files
        # This catches series like "The Hunting Wives" where Sonarr has no episode data
        if not report_seasons and monitored_seasons_count > 0:
            total_episodes = series_stats.get("episodeCount", 0)
            total_files = series_stats.get("episodeFileCount", 0)
            if total_episodes == 0 and total_files == 0:
                title = series.get("title") or f"TMDB {tmdb_id}"
                print(f"  [EMPTY] {title} (TMDB {tmdb_id}): {monitored_seasons_count} monitored season(s) but 0 episodes tracked")
                report_seasons = [s.get("seasonNumber") for s in seasons if s.get("monitored")]
                actionable_seasons = report_seasons[:]

        if not report_seasons:
            continue
            
        title = series.get("title") or get_tmdb_title(tmdb_id, TMDB_API_KEY, media_type="tv")
        
        # Determine action based on ACTIONABLE seasons
        if actionable_seasons and len(actionable_seasons) == monitored_seasons_count:
             action = "delete_series"
        elif actionable_seasons:
             action = "unmonitor_seasons"
        else:
             action = "report_only"
             
        missing_items[tmdb_id] = {
            "title": title or f"TMDB {tmdb_id}",
            "tmdb_id": tmdb_id,
            "series_id": series.get("id"),
            "action": action,
            "seasons": actionable_seasons, # For action logic
            "report_seasons": report_seasons # For display logic
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

def clean_stuck_downloads(api_key, base_url, app_name, dry_run=False):
    headers = {"X-Api-Key": api_key}
    url = f"{base_url}/api/v3/queue"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    records = data.get("records", [])
    
    stuck_records = []
    now = datetime.now(timezone.utc)
    
    print(f"\nEvaluating downloads in {app_name}...")
    for r in records:
        size = r.get("size", 0)
        sizeleft = r.get("sizeleft", 0)
        title = r.get("title", "Unknown")
        
        if size > 0:
            progress = 1.0 - (sizeleft / float(size))
        else:
            progress = 0.0
            
        added_str = r.get("added")
        added_dt = parse_iso_datetime(added_str)
        if not added_dt:
            print(f"  [?] Skipping {title} (No added date)")
            continue
            
        if added_dt.tzinfo is None:
            added_dt = added_dt.replace(tzinfo=timezone.utc)
            
        age_hours = (now - added_dt).total_seconds() / 3600.0
        age_minutes = (now - added_dt).total_seconds() / 60.0
        
        reason = None
        if progress <= 0.05 and age_minutes >= STUCK_DOWNLOAD_MINUTES:
            reason = f"Progress is low ({progress*100:.2f}% <= 5%) after {age_minutes:.1f} minutes (limit: {STUCK_DOWNLOAD_MINUTES}m)."
        elif age_hours >= MAX_DOWNLOAD_HOURS:
            reason = f"Download taking too long ({age_hours:.1f}h >= {MAX_DOWNLOAD_HOURS}h), currently at {progress*100:.2f}%."
            
        if reason:
            print(f" Mark for removal: {title}")
            print(f" Reason: {reason}")
            stuck_records.append((r, reason))
        else:
            print(f"Keep: {title} | Progress: {progress*100:.2f}% | Age: {age_minutes:.1f} minutes | Status: {r.get('status')}")
            
    if not stuck_records:
        return

    print(f"\n--- Removing {len(stuck_records)} stuck downloads in {app_name} ---")
    for r, reason in stuck_records:
        title = r.get("title", "Unknown")
        record_id = r.get("id")
        
        if not dry_run:
            delete_url = f"{base_url}/api/v3/queue/{record_id}"
            params = {"removeFromClient": "true", "blocklist": "true"}
            try:
                delete_response = requests.delete(delete_url, headers=headers, params=params)
                delete_response.raise_for_status()
                print(f"  -> Successfully removed and blocklisted: {title}")
            except Exception as e:
                print(f"  -> Failed to remove {title}: {e}")
        else:
            print(f"  -> [DRY RUN] Would remove and blocklist: {title}")



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

def perform_deletions_list(radarr_api_key, radarr_url, sonarr_api_key, sonarr_url, deletion_data):
    if deletion_data["radarr"]:
        print(f"Deleting movies from Radarr: {deletion_data['radarr']}")
        for tmdb_id in deletion_data["radarr"]:
            delete_radarr_movie(radarr_api_key, radarr_url, tmdb_id)
    else:
        print("Nothing to delete from Radarr.")

    if deletion_data["sonarr"]:
        print(f"Processing series in Sonarr: {deletion_data['sonarr']}")
        for item in deletion_data["sonarr"]:
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
                 delete_sonarr_series(sonarr_api_key, sonarr_url, item)
    else:
        print("Nothing to delete from Sonarr.")

    jellyseerr_requests_to_delete = deletion_data.get("jellyseerr", []) or []
    if jellyseerr_requests_to_delete:
        print("Deleting requested media from Jellyseerr:")
        requests_to_clean = []
        for request in jellyseerr_requests_to_delete:
            requests_to_clean.append(request)
            
        # Previously we did a resolution step here, but now we pass full request objects
        # We can just call delete_jellyseerr_requests directly if we passed the right structure
        delete_jellyseerr_requests(JELLYSEER_API_KEY, JELLYSEER_URL, requests_to_clean)
    else:
        print("Nothing to delete from Jellyseerr.")



def get_jellyseerr_media_info(api_key, base_url, tmdb_id, media_type):
    """
    Lookup media information in Jellyseerr by TMDB ID.
    Returns the JSON response which includes mediaInfo and requests if they exist.
    """
    headers = {"X-Api-Key": api_key}
    url = f"{base_url}/api/v1/{media_type}/{tmdb_id}"
    response = requests.get(url, headers=headers)
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    return response.json()

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
            seasons = info.get('report_seasons', info['seasons'])
            print(f"- {info['title']} (TMDB ID: {tmdb_id}) -> Action: {action}, Missing Seasons: {seasons}")
    else:
        print("No Sonarr series considered missing.")

    # Identify all current candidates for deletion
    # Candidates are items missing in Radarr/Sonarr. 
    # We look up their status in Jellyseerr directly to find request info.
    
    current_candidates_tmdb_ids = set(radarr_missing) | set(sonarr_missing)
    
    now = datetime.now(timezone.utc)
    updated_pending = {}
    
    ready_radarr = []
    ready_sonarr = []
    ready_jellyseerr = []
    
    # Process current candidates
    for tmdb_id in current_candidates_tmdb_ids:
        # Determine media type for lookup
        if tmdb_id in radarr_missing:
            media_type = "movie"
        else:
            media_type = "tv"

        # Lookup media in Jellyseerr directly
        try:
             jellyseerr_data = get_jellyseerr_media_info(JELLYSEER_API_KEY, JELLYSEER_URL, tmdb_id, media_type)
        except Exception as e:
             print(f"Error looking up TMDB ID {tmdb_id} in Jellyseerr: {e}")
             continue
             
        media_info = jellyseerr_data.get("mediaInfo")
        if not media_info:
            # Not in Jellyseerr or no media info, skip
            print(f"SKIPPING: TMDB ID {tmdb_id} (Not managed by Jellyseerr or no media info found)")
            continue
            
        requests_list = media_info.get("requests", [])
        
        if requests_list:
            # Sort requests by date descending to get the latest
            requests_list.sort(key=lambda x: x.get("createdAt"), reverse=True)
            latest_request = requests_list[0]
            request_date_str = latest_request.get("createdAt")
            requested_by_user = latest_request.get("requestedBy", {})
            requester = requested_by_user.get("displayName") or requested_by_user.get("email") or "Unknown"
        else:
            # Fallback to media creation date if no requests exist
            request_date_str = media_info.get("createdAt")
            if not request_date_str:
                print(f"SKIPPING: TMDB ID {tmdb_id} (Managed by Jellyseerr but no request or creation date found)")
                continue
            requester = "Media-Only (No Request)"
        
        media_id = media_info.get("id")
        title = jellyseerr_data.get("title") or jellyseerr_data.get("name") or f"TMDB {tmdb_id}"

        request_date = parse_iso_datetime(request_date_str)
        
        # Entry for pending file (store based on latest request)
        tmdb_id_str = str(tmdb_id)
        entry = {
            "tmdb_id": tmdb_id,
            "title": title,
            "request_date": request_date_str,
            "requested_by": requester,
            "media_id": media_id
        }
        
        # Add to pending list
        updated_pending[tmdb_id_str] = entry
        
        # Check if ready to delete
        if request_date:
            # Ensure request_date is UTC aware if 'now' is
            if request_date.tzinfo is None:
                request_date = request_date.replace(tzinfo=timezone.utc)
            
            age_days = (now - request_date).days
            if age_days >= DELETION_DELAY_DAYS:

                # Skip report_only sonarr entries early — they are incomplete but should NOT be deleted
                if tmdb_id in sonarr_missing and sonarr_missing[tmdb_id].get("action") == "report_only":
                    print(f"SKIPPING: {title} (Age: {age_days} days, Requested by: {requester}) — some episodes missing but series has files")
                    continue

                # Ready for deletion of MEDIA
                print(f"READY TO DELETE MEDIA: {title} (Age: {age_days} days, Requested by: {requester})")
                
                # Add to execution lists
                if tmdb_id in radarr_missing:
                    ready_radarr.append(tmdb_id)
                elif tmdb_id in sonarr_missing:
                    ready_sonarr.append(sonarr_missing[tmdb_id])
                 
                # Check if we should delete the Jellyseerr request
                if age_days < KEEP_REQUESTS_OLDER_THAN_DAYS:
                    # Prepare jellyseerr deletion object
                    req_obj = {
                        "title": title,
                        "media_id": media_id,
                        "tmdb_id": tmdb_id,
                        "media_type": media_type,
                        "request_date": request_date_str,
                        "requested_by": requester,  
                    }
                    ready_jellyseerr.append(req_obj)
                    print(f"  -> Will also delete Jellyseerr request (Age {age_days} < {KEEP_REQUESTS_OLDER_THAN_DAYS} days)")
                else:
                    print(f"  -> Keeping Jellyseerr request (Age {age_days} >= {KEEP_REQUESTS_OLDER_THAN_DAYS} days)")
            else:
                print(f"PENDING: {title} (Age: {age_days} days < {DELETION_DELAY_DAYS} days delay)")
        else:
             print(f"SKIPPING: {title} (No request date found)")

    # Save updated pending state
    save_pending_deletions(updated_pending)

    # Perform Deletions
    if dry_run:
        print("\n--- DRY RUN SUMMARY ---")
        print(f"Would delete {len(ready_radarr)} movies from Radarr")
        print(f"Would delete {len(ready_sonarr)} series from Sonarr")
        print(f"Would delete {len(ready_jellyseerr)} requests from Jellyseerr")
        if ready_jellyseerr:
             for r in ready_jellyseerr:
                 print(f"  - {r['title']} (Requested by {r['requested_by']} on {r['request_date']})")
    else:
        if ready_radarr or ready_sonarr or ready_jellyseerr:
            print("\nPerforming deletions...")
            deletion_data = {
                "radarr": ready_radarr,
                "sonarr": ready_sonarr,
                "jellyseerr": ready_jellyseerr
            }
            perform_deletions_list(RADARR_API_KEY, RADARR_URL, SONARR_API_KEY, SONARR_URL, deletion_data)
        else:
            print("\nNo media ready for deletion.")

if __name__ == "__main__":
    args = parse_command_line_arguments()
    
    print("--- Cleaning Stuck Downloads ---")
    clean_stuck_downloads(RADARR_API_KEY, RADARR_URL, "Radarr", dry_run=args.dry_run)
    clean_stuck_downloads(SONARR_API_KEY, SONARR_URL, "Sonarr", dry_run=args.dry_run)
    
    generate_missing_media_report(dry_run=args.dry_run)