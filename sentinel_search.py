#!/usr/bin/env python3
"""
SeerrSentinel Search — Automated Radarr/Sonarr searches.

Can be run directly or via:
    python3 seerr_sentinel.py search
"""

import os
import sys
import json
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Config centralisée (charge aussi le .env)
from seerr_sentinel import load_config

_cfg = load_config([
    "RADARR_API_KEY",
    "RADARR_URL",
    "SONARR_API_KEY",
    "SONARR_URL",
])

RADARR_API_KEY = _cfg["RADARR_API_KEY"]
RADARR_URL = _cfg["RADARR_URL"]
SONARR_API_KEY = _cfg["SONARR_API_KEY"]
SONARR_URL = _cfg["SONARR_URL"]
HISTORY_FILE = "/tmp/force_search_history.json"
COOLDOWN_REQUEST_MINUTES = 10
COOLDOWN_DOWNLOAD_MINUTES = 10


# Cycle Configuration
MOVIE_CYCLE_HOURS = 12
MOVIE_MAX_SEARCHES = 6

SEASON_CYCLE_HOURS = 12
SEASON_MAX_SEARCHES = 3

EPISODE_CYCLE_HOURS = 12
EPISODE_MAX_SEARCHES = 3


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}

def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=4)

def is_cooled_down(last_search_iso):
    if not last_search_iso:
        return True
    try:
        last_search = datetime.fromisoformat(last_search_iso)
        # Ensure UTC
        if last_search.tzinfo is None:
            last_search = last_search.replace(tzinfo=timezone.utc)
        
        now = datetime.now(timezone.utc)
        return (now - last_search) > timedelta(minutes=COOLDOWN_REQUEST_MINUTES)
    except ValueError:
        return True

def get_cycle_config(item_type):
    if item_type == "movie":
        return MOVIE_CYCLE_HOURS, MOVIE_MAX_SEARCHES
    elif item_type == "season":
        return SEASON_CYCLE_HOURS, SEASON_MAX_SEARCHES
    elif item_type == "episode":
        return EPISODE_CYCLE_HOURS, EPISODE_MAX_SEARCHES
    return 24, 1

def check_cycle_quota(key, item_type):
    """
    Checks if the search quota for the current cycle has been reached.
    Resets the cycle if the duration has passed.
    Returns True if search is allowed, False otherwise.
    """
    history = load_history()
    entry = history.get(key, {})
    
    # If legacy format (no cycle_start), treat as new
    if "cycle_start" not in entry:
        return True

    try:
        cycle_start = datetime.fromisoformat(entry["cycle_start"])
        if cycle_start.tzinfo is None:
            cycle_start = cycle_start.replace(tzinfo=timezone.utc)
        
        cycle_hours, max_searches = get_cycle_config(item_type)
        now = datetime.now(timezone.utc)
        
        # Check if cycle expired
        if (now - cycle_start) > timedelta(hours=cycle_hours):
            # Cycle expired -> Reset will happen on record_search, allow search now
            # Actually better to rely on record_search to reset/init, 
            # but we need to know if we CAN search.
            # If expired, we count as 0, so < max is True.
            return True
        
        # Check quota
        if entry.get("count", 0) >= max_searches:
            return False
            
        return True
    except ValueError:
        return True

def record_search(key, search_type, item_type="movie", title=None):
    history = load_history()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    
    if key not in history:
        history[key] = {}
        
    entry = history[key]
    
    # Init or Reset Cycle Logic
    cycle_hours, _ = get_cycle_config(item_type)
    reset_cycle = False
    
    if "cycle_start" not in entry:
        reset_cycle = True
    else:
        try:
            cycle_start = datetime.fromisoformat(entry["cycle_start"])
            if cycle_start.tzinfo is None:
                cycle_start = cycle_start.replace(tzinfo=timezone.utc)
            if (now - cycle_start) > timedelta(hours=cycle_hours):
                reset_cycle = True
        except ValueError:
            reset_cycle = True
            
    if reset_cycle:
        entry["cycle_start"] = now_iso
        entry["count"] = 1
    else:
        entry["count"] = entry.get("count", 0) + 1

    entry["last_search"] = now_iso
    entry["type"] = search_type # Store the specific search command name
    
    if title:
        entry["title"] = title

    save_history(history)
    print(f"Recorded {search_type} for {key} ({title}) - Cycle Count: {entry['count']}")

def get_last_search_timestamp(key):
    history = load_history()
    entry = history.get(key, {})
    return entry.get("last_search")


def is_released(item_type, item):
    """
    Determines if the media item is currently released based on available dates.
    Returns True if at least one release date is in the past.
    Returns False if all known future dates are in the future, or no dates are present (conservative).
    """
    now = datetime.now(timezone.utc)
    
    if item_type == "movie":
        # Check various release dates from Radarr API
        fields = ["digitalRelease", "physicalRelease", "releaseDate"]
        
        has_any_date = False
        
        for f in fields:
            date_str = item.get(f)
            if date_str:
                has_any_date = True
                try:
                    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    if dt <= now:
                        return True # Found a date in the past, it's released
                except ValueError:
                    pass
        
        if not has_any_date:
            # Fallback: if status is 'released', assume yes
            return item.get("status") == "released"
            
        return False # Has dates, but all are in the future

    elif item_type == "episode":
        air_date_str = item.get("airDateUtc")
        if air_date_str:
            try:
                dt = datetime.fromisoformat(air_date_str.replace("Z", "+00:00"))
                if dt > now:
                    return False # Future air date
            except ValueError:
                pass
        return True # Default to True if no date or date is past

    return True


# --- History Caches to prevent re-download of deleted media ---
_radarr_history_cache = {}
def is_movie_deleted_in_history(movie_id):
    if movie_id in _radarr_history_cache:
        return _radarr_history_cache[movie_id]

    try:
        url = f"{RADARR_URL}/api/v3/history/movie?movieId={movie_id}"
        resp = requests.get(url, headers={"X-Api-Key": RADARR_API_KEY})
        resp.raise_for_status()
        events = resp.json()

        for ev in events:
            if ev.get("eventType") == "movieFileDeleted":
                _radarr_history_cache[movie_id] = True
                return True

        _radarr_history_cache[movie_id] = False
        return False
    except Exception as e:
        print(f"Error checking Radarr history for movie {movie_id}: {e}")
        return False


_sonarr_history_cache = {}
def is_episode_deleted_in_history(series_id, episode_id):
    if series_id not in _sonarr_history_cache:
        try:
            url = f"{SONARR_URL}/api/v3/history/series?seriesId={series_id}"
            resp = requests.get(url, headers={"X-Api-Key": SONARR_API_KEY})
            resp.raise_for_status()
            events = resp.json()

            deleted_eps = set()
            for ev in events:
                if ev.get("eventType") == "episodeFileDeleted":
                    ep_id = ev.get("episodeId")
                    if ep_id:
                        deleted_eps.add(ep_id)
            _sonarr_history_cache[series_id] = deleted_eps
        except Exception as e:
            print(f"Error checking Sonarr history for series {series_id}: {e}")
            _sonarr_history_cache[series_id] = set()

    return episode_id in _sonarr_history_cache[series_id]
# -------------------------------------------------------------

def get_sonarr_series_map():
    if not SONARR_API_KEY or not SONARR_URL:
        return {}
    try:
        url = f"{SONARR_URL}/api/v3/series"
        resp = requests.get(url, headers={"X-Api-Key": SONARR_API_KEY})
        resp.raise_for_status()
        series_list = resp.json()
        return {s["id"]: s["title"] for s in series_list}
    except Exception as e:
        print(f"Error fetching Sonarr series map: {e}")
        return {}

def list_missing_content(series_map=None):
    print("\n--- Missing Content Summary ---")
    
    # Radarr Summary
    if RADARR_API_KEY and RADARR_URL:
        try:
             url = f"{RADARR_URL}/api/v3/wanted/missing?sortKey=airDateUtc&sortDirection=ascending&pageSize=1000"
             resp = requests.get(url, headers={"X-Api-Key": RADARR_API_KEY})
             movies = resp.json().get("records", [])
             
             # Filter unreleased and deleted
             released_movies = [m for m in movies if is_released("movie", m)]
             unreleased_movies = [m for m in movies if not is_released("movie", m)]
             
             eligible_movies = []
             deleted_history_movies = []
             for m in released_movies:
                 if is_movie_deleted_in_history(m.get("id")):
                     deleted_history_movies.append(m)
                 else:
                     eligible_movies.append(m)
             
             print(f"[Radarr] Found {len(eligible_movies)} eligible missing movies (out of {len(movies)} total missing):")
             for m in eligible_movies:
                 print(f"  - {m['title']} (ID: {m['id']}, Year: {m['year']})")
                 
             if deleted_history_movies:
                 print(f"[Radarr] Skipped {len(deleted_history_movies)} movies due to previous deletion history:")
                 for m in deleted_history_movies:
                     print(f"  ~ {m['title']} (File previously deleted)")
                     
             if unreleased_movies:
                 print(f"[Radarr] Skipped {len(unreleased_movies)} movies due to unreleased status:")
                 for m in unreleased_movies:
                     print(f"  ~ {m['title']} (Not released yet)")
        except Exception as e:
            print(f"[Radarr] Error fetching summary: {e}")
            
    # Sonarr Summary
    if SONARR_API_KEY and SONARR_URL:
        if series_map is None:
             series_map = get_sonarr_series_map()
             
        try:
            url = f"{SONARR_URL}/api/v3/wanted/missing?sortKey=airDateUtc&sortDirection=ascending&pageSize=1000"
            resp = requests.get(url, headers={"X-Api-Key": SONARR_API_KEY})
            episodes = resp.json().get("records", [])
            
            # Group for display
            series_stats = {}
            deleted_stats = {}
            unreleased_stats = {}
            filtered_count = 0
            
            for ep in episodes:
                s_id = ep.get("seriesId")
                s_title = series_map.get(s_id) or ep.get("series", {}).get("title", f"Series {s_id}")

                if not is_released("episode", ep):
                    if s_title not in unreleased_stats:
                        unreleased_stats[s_title] = 0
                    unreleased_stats[s_title] += 1
                    continue
                    
                # Check history cache
                if is_episode_deleted_in_history(s_id, ep.get("id")):
                    if s_title not in deleted_stats:
                        deleted_stats[s_title] = 0
                    deleted_stats[s_title] += 1
                    continue
                    
                filtered_count += 1
                if s_title not in series_stats:
                    series_stats[s_title] = 0
                series_stats[s_title] += 1
            
            print(f"[Sonarr] Found {filtered_count} released missing episodes (out of {len(episodes)} total missing) across {len(series_stats)} series:")
            for title, count in series_stats.items():
                print(f"  - {title}: {count} episodes missing")
                
            if deleted_stats:
                print(f"[Sonarr] Skipped episodes due to previous deletion history:")
                for title, count in deleted_stats.items():
                    print(f"  ~ {title}: {count} episodes skipped")
            
            if unreleased_stats:
                print(f"[Sonarr] Skipped episodes due to unreleased status (future air date):")
                for title, count in unreleased_stats.items():
                    print(f"  ~ {title}: {count} episodes skipped")

        except Exception as e:
             print(f"[Sonarr] Error fetching summary: {e}")
    print("-------------------------------\n")

def check_active_commands(base_url, api_key, command_names):
    """
    Checks if any of the specified commands are currently running or started < 10 mins ago.
    """
    try:
        url = f"{base_url}/api/v3/command"
        resp = requests.get(url, headers={"X-Api-Key": api_key})
        resp.raise_for_status()
        commands = resp.json()
        
        now = datetime.now(timezone.utc)
        
        for cmd in commands:
            if cmd.get("name") in command_names:
                started_str = cmd.get("started")
                if started_str:
                    # Parse timestamp (handle Z for UTC)
                    try:
                        started_dt = datetime.fromisoformat(started_str.replace("Z", "+00:00"))
                        if (now - started_dt) < timedelta(minutes=COOLDOWN_DOWNLOAD_MINUTES):
                            status = cmd.get("status", "unknown")
                            print(f"Skipping: Active/Recent {cmd['name']} detected (Status: {status}, Started: {started_str}).")
                            return True # Busy
                    except ValueError:
                        pass # Ignore parsing errors
    except Exception as e:
        print(f"Error checking active commands: {e}")
        
    return False # Not busy

def check_queue(base_url, api_key):
    """
    Fetches the current queue from Sonarr or Radarr.
    Returns a set of IDs (movie IDs or episode IDs) that are currently in the queue.
    """
    active_ids = set()
    try:
        url = f"{base_url}/api/v3/queue"
        resp = requests.get(url, headers={"X-Api-Key": api_key})
        resp.raise_for_status()
        records = resp.json().get("records", [])
        
        for item in records:
            # Radarr uses movieId, Sonarr uses episodeId
            if "movieId" in item:
                active_ids.add(item["movieId"])
            if "episodeId" in item:
                active_ids.add(item["episodeId"])
                
    except Exception as e:
        print(f"Error checking queue for {base_url}: {e}")
        
    return active_ids

# --- Radarr Logic ---

def process_radarr():
    if not RADARR_API_KEY or not RADARR_URL:
        print("Radarr configuration missing, skipping.")
        return False

    print("Checking Radarr for missing movies...")
    
    # Check Queue
    queued_ids = check_queue(RADARR_URL, RADARR_API_KEY)
    
    try:
        url = f"{RADARR_URL}/api/v3/wanted/missing?sortKey=airDateUtc&sortDirection=ascending&pageSize=1000"
        resp = requests.get(url, headers={"X-Api-Key": RADARR_API_KEY})
        resp.raise_for_status()
        missing = resp.json().get("records", [])
    except Exception as e:
        print(f"Error fetching Radarr missing movies: {e}")
        return False

    # Collect all valid candidates
    candidates = []
    
    for movie in missing:
        movie_id = movie.get("id") # int
        title = movie.get("title")
        key = f"movie_{movie_id}"
        
        # Check Release status
        if not is_released("movie", movie):
            continue

        # Check Queue presence
        if movie_id in queued_ids:
            print(f"Skipping {title}: Already in queue.")
            continue
            
        # Check History for intentional deletion
        if is_movie_deleted_in_history(movie_id):
            print(f"Skipping {title}: Movie file was previously deleted.")
            continue

        # Check Cycle Quota
        if not check_cycle_quota(key, "movie"):
            print(f"Skipping {title}: Cycle quota reached.")
            continue

        last_search = get_last_search_timestamp(key)
        
        # We reuse check_cycle_quota for the "permission" to search,
        # but we also want to respect the short COOLDOWN_REQUEST_MINUTES (flood protection)
        # Assuming is_cooled_down logic should still apply to last_search
        
        if is_cooled_down(last_search):
            candidates.append({
                "movie": movie,
                "last_search": last_search
            })
        else:
            print(f"Skipping {title}: Cooldown active.")

    if not candidates:
         print("No eligible Radarr candidates (queue active, cycle limits, or unreleased).")
         return False

    # Sort candidates by last_search timestamp (None/Oldest first)
    def sort_key(c):
        ts = c["last_search"]
        if ts is None:
            return datetime.min.replace(tzinfo=timezone.utc).isoformat()
        return ts
        
    candidates.sort(key=sort_key)
    
    # Pick top candidate
    top = candidates[0]["movie"]
    movie_id = top.get("id")
    title = top.get("title")
    
    print(f"Radarr Candidate: {title} (ID: {movie_id})")
    
    # Trigger Search
    try:
        cmd_url = f"{RADARR_URL}/api/v3/command"
        payload = {"name": "MoviesSearch", "movieIds": [movie_id]}
        headers = {"X-Api-Key": RADARR_API_KEY, "Content-Type": "application/json"}
        resp = requests.post(cmd_url, json=payload, headers=headers)
        resp.raise_for_status()
        print(f"Triggered MoviesSearch for {title}")
        record_search(f"movie_{movie_id}", "MoviesSearch", item_type="movie", title=title)
        return True # Action Taken
    except Exception as e:
        print(f"Error triggering Radarr search: {e}")
        return False


# --- Sonarr Logic ---

def process_sonarr(series_map=None):
    if not SONARR_API_KEY or not SONARR_URL:
        print("Sonarr configuration missing, skipping.")
        return False

    print("Checking Sonarr for missing episodes...")
    
    # Check Queue
    queued_ids = check_queue(SONARR_URL, SONARR_API_KEY) # Contains episodeIds
    
    try:
        url = f"{SONARR_URL}/api/v3/wanted/missing?sortKey=airDateUtc&sortDirection=ascending&pageSize=1000"
        resp = requests.get(url, headers={"X-Api-Key": SONARR_API_KEY})
        resp.raise_for_status()
        missing_episodes = resp.json().get("records", [])
    except Exception as e:
        print(f"Error fetching Sonarr missing episodes: {e}")
        return False
        
    if not missing_episodes:
        print("No missing episodes found in Sonarr.")
        return False

    if series_map is None:
        series_map = get_sonarr_series_map()

    # Group by Series -> Season
    season_groups = {}
    
    for ep in missing_episodes:
        if not is_released("episode", ep):
            continue
            
        # Check if this specific episode is in queue
        if ep.get("id") in queued_ids:
            # print(f"Skipping Sonarr Episode {ep.get('id')}: Already in queue.")
            continue
            
        s_id = ep.get("seriesId")
        
        # Check History for intentional deletion
        if is_episode_deleted_in_history(s_id, ep.get("id")):
            # print(f"Skipping Sonarr Episode {ep.get('id')}: Episode file was previously deleted.")
            continue
            
        s_num = ep.get("seasonNumber")
        
        key = (s_id, s_num)
        if key not in season_groups:
            season_groups[key] = []
        season_groups[key].append(ep)

    # Collect Actions
    actions = []

    for key, episodes in season_groups.items():
        series_id, season_num = key
        
        # Determine Series Title
        series_title = series_map.get(series_id)
        if not series_title:
             series_title = episodes[0].get("series", {}).get("title", f"Series {series_id}")

        # Prepare Keys and Last Search Times
        season_key = f"season_{series_id}_{season_num}"
        season_last_search_iso = get_last_search_timestamp(season_key)
        
        # Identify Candidate Episode (First missing)
        episodes.sort(key=lambda x: x.get("episodeNumber"))
        first_ep = episodes[0]
        ep_id = first_ep.get("id")
        ep_num = first_ep.get("episodeNumber")
        ep_title = first_ep.get("title", "")
        ep_key = f"episode_{ep_id}"
        ep_last_search_iso = get_last_search_timestamp(ep_key)

        # Parse Timestamps (Treat None as Min/Old)
        def parse_ts(iso_str):
            if not iso_str:
                return datetime.min.replace(tzinfo=timezone.utc)
            try:
                dt = datetime.fromisoformat(iso_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                return datetime.min.replace(tzinfo=timezone.utc)

        season_dt = parse_ts(season_last_search_iso)
        ep_dt = parse_ts(ep_last_search_iso)
        
        # Decision Logic: Prefer action that was performed LEAST recently (Oldest timestamp)
        # If timestamps are equal (e.g. both None/Never), default to SeasonSearch
        prefer_season = season_dt <= ep_dt
        
        candidates_checks = []
        
        # Define check routines
        def check_season():
            if not check_cycle_quota(season_key, "season"):
                print(f"Skipping {series_title} Season {season_num}: Cycle quota reached.")
                return None
            if not is_cooled_down(season_last_search_iso):
                print(f"Skipping {series_title} Season {season_num}: Cooldown active.")
                return None
            return {
                "type": "SeasonSearch",
                "last_search": season_last_search_iso,
                "series_id": series_id,
                "season_num": season_num,
                "title": f"{series_title} Season {season_num}",
                "print_title": f"{series_title} Season {season_num}"
            }

        def check_episode():
             if not check_cycle_quota(ep_key, "episode"):
                 print(f"Skipping {series_title} S{season_num}E{ep_num}: Cycle quota reached.")
                 return None
             if not is_cooled_down(ep_last_search_iso):
                 print(f"Skipping {series_title} S{season_num}E{ep_num}: Cooldown active.")
                 return None
             return {
                 "type": "EpisodeSearch",
                 "last_search": ep_last_search_iso,
                 "episode_id": ep_id,
                 "title": f"{series_title} S{season_num}E{ep_num} - {ep_title}",
                 "print_title": f"{series_title} S{season_num}E{ep_num}"
             }

        # Execute decision
        if prefer_season:
            # Try Season first
            res = check_season()
            if res:
                actions.append(res)
            else:
                # Fallback to Episode if Season blocked
                res_ep = check_episode()
                if res_ep:
                    actions.append(res_ep)
        else:
            # Try Episode first
            res = check_episode()
            if res:
                actions.append(res)
            else:
                # Fallback to Season if Episode blocked
                res_s = check_season()
                if res_s:
                    actions.append(res_s)

    if not actions:
        print("No eligible Sonarr candidates (queue active or cycle limits reached).")
        return False

    # Sort actions by Priority (Season < Episode) then Last Search Timestamp (None/Oldest first)
    def sort_key(a):
        # Priority: SeasonSearch (0) comes before EpisodeSearch (1)
        priority = 0 if a["type"] == "SeasonSearch" else 1
        
        ts = a["last_search"]
        if ts is None:
            ts_val = datetime.min.replace(tzinfo=timezone.utc).isoformat()
        else:
            ts_val = ts
            
        return (priority, ts_val)
        
    actions.sort(key=sort_key)
    
    # Execute Top Action
    top = actions[0]
    
    print(f"Sonarr Candidate ({top['type']}): {top['print_title']}")
    
    try:
        cmd_url = f"{SONARR_URL}/api/v3/command"
        headers = {"X-Api-Key": SONARR_API_KEY, "Content-Type": "application/json"}
        
        if top["type"] == "SeasonSearch":
            payload = {
                "name": "SeasonSearch",
                "seriesId": top["series_id"],
                "seasonNumber": top["season_num"]
            }
            resp = requests.post(cmd_url, json=payload, headers=headers)
            resp.raise_for_status()
            print(f"Triggered SeasonSearch for {top['print_title']}")
            record_search(f"season_{top['series_id']}_{top['season_num']}", "SeasonSearch", item_type="season", title=top["title"])
            return True
            
        elif top["type"] == "EpisodeSearch":
             payload = {
                "name": "EpisodeSearch",
                "episodeIds": [top["episode_id"]]
            }
             resp = requests.post(cmd_url, json=payload, headers=headers)
             resp.raise_for_status()
             print(f"Triggered EpisodeSearch for {top['print_title']}")
             record_search(f"episode_{top['episode_id']}", "EpisodeSearch", item_type="episode", title=top["title"])
             return True
             
    except Exception as e:
        print(f"Error triggering Sonarr search: {e}")
        return False

if __name__ == "__main__":
    if not os.environ.get("_SEERRSENTINEL_INTERNAL"):
        print("Error: This script cannot be run directly.")
        print("Use:  python3 seerr_sentinel.py search")
        sys.exit(1)

    print(f"--- SeerrSentinel Search Run: {datetime.now()} ---")
    
    # Global Lock Check
    radarr_busy = check_active_commands(RADARR_URL, RADARR_API_KEY, ["MoviesSearch"])
    sonarr_busy = check_active_commands(SONARR_URL, SONARR_API_KEY, ["SeasonSearch", "EpisodeSearch"])
    
    if radarr_busy or sonarr_busy:
        print("Global Lock: One or more services are busy performing a search. Aborting run.")
        sys.exit(0)

    s_map = get_sonarr_series_map()
    list_missing_content(s_map)
    
    # Exclusive Execution: If Radarr runs, Sonarr waits.
    if process_radarr():
        print("Action taken by Radarr. Stopping run to respect mutual exclusion.")
        sys.exit(0)
        
    process_sonarr(s_map)
