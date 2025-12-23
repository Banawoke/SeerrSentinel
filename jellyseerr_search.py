import os
import sys
import json
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)

required_vars = {
    "JELLYSEER_API_KEY": os.environ.get("JELLYSEER_API_KEY"),
    "JELLYSEER_URL": os.environ.get("JELLYSEER_URL"),
    "RADARR_API_KEY": os.environ.get("RADARR_API_KEY"),
    "RADARR_URL": os.environ.get("RADARR_URL"),
    "SONARR_API_KEY": os.environ.get("SONARR_API_KEY"),
    "SONARR_URL": os.environ.get("SONARR_URL"),
}

missing_vars = [key for key, value in required_vars.items() if not value]

if missing_vars:
    print(f"Error: The following required environment variables are missing in .env: {', '.join(missing_vars)}")
    sys.exit(1)

RADARR_API_KEY = required_vars["RADARR_API_KEY"]
RADARR_URL = required_vars["RADARR_URL"]
SONARR_API_KEY = required_vars["SONARR_API_KEY"]
SONARR_URL = required_vars["SONARR_URL"]
HISTORY_FILE = "/tmp/force_search_history.json"
COOLDOWN_REQUEST_HOURS = 12
COOLDOWN_DOWNLOAD_MINUTES = 10


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
        return (now - last_search) > timedelta(hours=COOLDOWN_REQUEST_HOURS)
    except ValueError:
        return True

def record_search(key, search_type, title=None):
    history = load_history()
    now_iso = datetime.now(timezone.utc).isoformat()
    
    if key not in history:
        history[key] = {}
    
    entry = {
        "timestamp": now_iso,
    }
    if title:
        entry["title"] = title
    else:
        # Preserve existing title if updating timestamp
        current_entry = history[key].get(search_type)
        if isinstance(current_entry, dict):
             entry["title"] = current_entry.get("title")

    # Store simpler structure if we want compatibility, but user requested title.
    # New structure: history[key][search_type] = { "timestamp": "...", "title": "..." }
    # To maintain backward compat for reading timestamps, we need to handle the read side too
    # but let's just break compat since it's a dev script in /tmp
    
    history[key][search_type] = entry
    save_history(history)
    print(f"Recorded {search_type} for {key} ({title}) at {now_iso}")

def get_last_search_timestamp(key, search_type):
    history = load_history()
    entry = history.get(key, {}).get(search_type)
    if isinstance(entry, dict):
        return entry.get("timestamp")
    return entry # It was a string in previous version


def is_released(item_type, item):
    """
    Determines if the media item is currently released based on available dates.
    Returns True if at least one release date is in the past.
    Returns False if all known future dates are in the future, or no dates are present (conservative).
    """
    now = datetime.now(timezone.utc)
    
    if item_type == "movie":
        # Check various release dates
        dates_to_check = []
        
        # Mapping API fields to check
        fields = ["releaseDate"]
        
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
             
             # Filter unreleased
             released_movies = [m for m in movies if is_released("movie", m)]
             
             print(f"[Radarr] Found {len(released_movies)} released missing movies (out of {len(movies)} total missing):")
             for m in released_movies:
                 print(f"  - {m['title']} (ID: {m['id']}, Year: {m['year']})")
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
            filtered_count = 0
            
            for ep in episodes:
                if not is_released("episode", ep):
                    continue
                    
                filtered_count += 1
                s_id = ep.get("seriesId")
                # Try map first, then episode object, then fallback
                s_title = series_map.get(s_id) or ep.get("series", {}).get("title", f"Series {s_id}")
                
                if s_title not in series_stats:
                    series_stats[s_title] = 0
                series_stats[s_title] += 1
            
            print(f"[Sonarr] Found {filtered_count} released missing episodes (out of {len(episodes)} total missing) across {len(series_stats)} series:")
            for title, count in series_stats.items():
                print(f"  - {title}: {count} episodes missing")

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

# --- Radarr Logic ---

def process_radarr():
    if not RADARR_API_KEY or not RADARR_URL:
        print("Radarr configuration missing, skipping.")
        return False

    print("Checking Radarr for missing movies...")
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
        movie_id = str(movie.get("id"))
        title = movie.get("title")
        
        # Check Release status
        if not is_released("movie", movie):
            # print(f"Skipping unreleased movie: {title}")
            continue

        last_search = get_last_search_timestamp(f"movie_{movie_id}", "MoviesSearch")
        
        if is_cooled_down(last_search):
            candidates.append({
                "movie": movie,
                "last_search": last_search
            })

    if not candidates:
         print("No eligible Radarr candidates (all recently searched, unreleased, or no missing movies).")
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
        record_search(f"movie_{movie_id}", "MoviesSearch", title=title)
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
            
        s_id = ep.get("seriesId")
        s_num = ep.get("seasonNumber")
        
        key = (s_id, s_num)
        if key not in season_groups:
            season_groups[key] = []
        season_groups[key].append(ep)

    # Collect Actions (Season or Episode) for ALL groups
    actions = []

    for key, episodes in season_groups.items():
        series_id, season_num = key
        
        # Determine Series Title
        series_title = series_map.get(series_id)
        if not series_title:
             # Fallback to first episode's embedded series title
             series_title = episodes[0].get("series", {}).get("title", f"Series {series_id}")

        # Check Season Search Cooldown
        last_search_time = get_last_search_timestamp(f"season_{series_id}_{season_num}", "SeasonSearch")
        
        if is_cooled_down(last_search_time):
            actions.append({
                "type": "SeasonSearch", # or "EpisodeSearch"
                "last_search": last_search_time,
                "series_id": series_id,
                "season_num": season_num,
                "title": f"{series_title} Season {season_num}",
                "print_title": f"{series_title} Season {season_num}"
            })
        else:
            # Fallback: Check individual episodes - STRICT SEQUENTIAL
            # Find the FIRST missing episode in this season.
            episodes.sort(key=lambda x: x.get("episodeNumber"))
            
            # STRICT: Only look at the very first episode. 
            # If it's cooled down -> Search it.
            # If it's NOT cooled down -> Do nothing (wait for it).
            # Do NOT skip to episode 2.
            
            first_ep = episodes[0]
            ep_id = first_ep.get("id")
            ep_num = first_ep.get("episodeNumber")
            ep_title = first_ep.get("title", "")
            
            last_ep_search = get_last_search_timestamp(f"episode_{ep_id}", "EpisodeSearch")
            
            if is_cooled_down(last_ep_search):
                    actions.append({
                    "type": "EpisodeSearch",
                    "last_search": last_ep_search,
                    "episode_id": ep_id,
                        "title": f"{series_title} S{season_num}E{ep_num} - {ep_title}",
                        "print_title": f"{series_title} S{season_num}E{ep_num}"
                    })
            else:
                 # Debug info if needed
                 # print(f"  Strict wait: {series_title} S{season_num}E{ep_num} is on cooldown.")
                 pass

    if not actions:
        print("No eligible Sonarr candidates (all recently searched).")
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
            record_search(f"season_{top['series_id']}_{top['season_num']}", "SeasonSearch", title=top["title"])
            return True
            
        elif top["type"] == "EpisodeSearch":
             payload = {
                "name": "EpisodeSearch",
                "episodeIds": [top["episode_id"]]
            }
             resp = requests.post(cmd_url, json=payload, headers=headers)
             resp.raise_for_status()
             print(f"Triggered EpisodeSearch for {top['print_title']}")
             record_search(f"episode_{top['episode_id']}", "EpisodeSearch", title=top["title"])
             return True
             
    except Exception as e:
        print(f"Error triggering Sonarr search: {e}")
        return False

if __name__ == "__main__":
    print(f"--- Force Search Run: {datetime.now()} ---")
    
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
