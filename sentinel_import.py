#!/usr/bin/env python3
"""
SeerrSentinel Import — Video file injection into Radarr/Sonarr.

Can be run directly or via:
    python3 seerr_sentinel.py import [--radarr] [--sonarr] [--force-id N]
"""

import os
import sys
import requests
import re
import argparse
from pathlib import Path
import unicodedata
import time

# Config centralisée (charge aussi le .env)
from seerr_sentinel import load_config

_cfg = load_config([
    "DOWNLOADS_PATH",
    "TMDB_API_KEY",
    "PUID",
    "PGID",
    "RADARR_API_KEY",
    "RADARR_URL",
    "SONARR_API_KEY",
    "SONARR_URL",
])

RADARR_VARS = {
    "API_KEY": _cfg["RADARR_API_KEY"],
    "URL": _cfg["RADARR_URL"],
}
SONARR_VARS = {
    "API_KEY": _cfg["SONARR_API_KEY"],
    "URL": _cfg["SONARR_URL"],
}

REQUIRED_VARS = {
    "DOWNLOADS_PATH": _cfg["DOWNLOADS_PATH"],
    "TMDB_API_KEY": _cfg["TMDB_API_KEY"],
    "PUID": _cfg["PUID"],
    "PGID": _cfg["PGID"],
    "RADARR_API_KEY": _cfg["RADARR_API_KEY"],
    "SONARR_API_KEY": _cfg["SONARR_API_KEY"],
    "RADARR_URL": _cfg["RADARR_URL"],
    "SONARR_URL": _cfg["SONARR_URL"],
}

class MediaImporter:
    def __init__(self):
        self.downloads_path = REQUIRED_VARS["DOWNLOADS_PATH"]
        self.tmdb_key = REQUIRED_VARS["TMDB_API_KEY"]
        
        self.puid = int(REQUIRED_VARS["PUID"]) if REQUIRED_VARS["PUID"] else None
        self.pgid = int(REQUIRED_VARS["PGID"]) if REQUIRED_VARS["PGID"] else None
        
        self.tmdb_cache = {}

    def normalize(self, text):
        if not text: return ""
        text = unicodedata.normalize('NFKD', text)
        text = "".join([c for c in text if not unicodedata.combining(c)])
        return re.sub(r'[^a-zA-Z0-9]', '', text).lower()

    # Noise tokens: resolutions, codecs, languages, containers
    NOISE_TOKENS = {'mkv', 'mp4', 'avi', '1080p', '720p', '480p', '2160p',
                    'h264', 'h265', 'x264', 'x265', 'web', 'bluray', 'hdtv',
                    'french', 'english', 'vostfr', 'multi', 'dl', 'hdr',
                    'remux', 'webrip', 'bdrip', 'proper', 'repack', 'amzn',
                    'nf', 'dsnp', 'atvp', 'hmax', 'complete'}

    def word_tokenize(self, text):
        """Split text into meaningful word tokens for matching."""
        if not text:
            return []
        text = unicodedata.normalize('NFKD', text)
        text = "".join([c for c in text if not unicodedata.combining(c)])
        tokens = re.findall(r'[a-zA-Z0-9]+', text.lower())
        return [t for t in tokens if t not in self.NOISE_TOKENS and len(t) > 1]

    def title_matches(self, title, item_name):
        """Check if a title meaningfully matches a filename using token matching."""
        title_tokens = self.word_tokenize(title)
        file_tokens = self.word_tokenize(item_name)
        if not title_tokens or not file_tokens:
            return False

        # All title tokens must appear as whole tokens in the filename
        matched = sum(1 for t in title_tokens if t in file_tokens)
        return matched == len(title_tokens)

    def get_tmdb_aliases(self, tmdb_id, media_type):
        """
        Fetch aliases from TMDB (alternative_titles + translations).
        media_type: 'tv' or 'movie'
        """
        if not tmdb_id or not self.tmdb_key:
            return set()
        
        cache_key = f"{media_type}_{tmdb_id}"
        if cache_key in self.tmdb_cache:
            return self.tmdb_cache[cache_key]

        aliases = set()
        try:
            # 1. Alternative Titles
            url_alt = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/alternative_titles?api_key={self.tmdb_key}"
            resp = requests.get(url_alt)
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", []) # TV
                if media_type == 'movie':
                    results = data.get("titles", []) # Movie uses 'titles'
                
                for item in results:
                    if item.get("title"): aliases.add(item["title"])
            
            # 2. Translations
            url_trans = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/translations?api_key={self.tmdb_key}"
            resp = requests.get(url_trans)
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("translations", []):
                    d = item.get("data", {})
                    if d.get("name"): aliases.add(d["name"])
                    if d.get("title"): aliases.add(d["title"])
            
            # 3. Base details 
            url_base = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}?api_key={self.tmdb_key}"
            resp = requests.get(url_base)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("title"): aliases.add(data["title"])
                if data.get("original_title"): aliases.add(data["original_title"])
                if data.get("name"): aliases.add(data["name"])
                if data.get("original_name"): aliases.add(data["original_name"])


        except Exception as e:
            print(f"  -> [WARN] Error fetching TMDB aliases for ID {tmdb_id}: {e}")

        self.tmdb_cache[cache_key] = aliases
        return aliases

    def is_released(self, item_type, item):
        """
        Determines if the media item is currently released based on available dates.
        Returns True if at least one release date is in the past.
        Returns False if all known future dates are in the future, or no dates are present.
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        
        if item_type == "movie":
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

    def get_downloads_content(self):
        content = []
        if os.path.exists(self.downloads_path):
            for root, dirs, files in os.walk(self.downloads_path):
                for d in dirs:
                    if 'sample' in d.lower(): continue
                    content.append({
                        "path": os.path.join(root, d),
                        "name": d,
                        "normalized": self.normalize(d),
                        "type": "dir"
                    })
                for f in files:
                    if f.lower().endswith(('.nfo', '.part', '.txt', '.srt', '.sfv', '.jpg', '.png', '.url')): continue
                    if 'sample' in f.lower(): continue
                    
                    content.append({
                        "path": os.path.join(root, f),
                        "name": f,
                        "normalized": self.normalize(f),
                        "type": "file"
                    })
        return content

    def check_inode_match(self, source_path, dest_dir):
        """Checks if files match by inode."""
        source_inodes = set()
        if os.path.isfile(source_path):
            source_inodes.add(os.stat(source_path).st_ino)
        elif os.path.isdir(source_path):
            for root, _, files in os.walk(source_path):
                for f in files:
                    if f.lower().endswith(('.mkv', '.mp4', '.avi')):
                        try: source_inodes.add(os.stat(os.path.join(root, f)).st_ino)
                        except: pass
        
        if not source_inodes or not os.path.exists(dest_dir):
            return False

        for root, _, files in os.walk(dest_dir):
            for f in files:
                if f.lower().endswith(('.mkv', '.mp4', '.avi')):
                    try:
                        if os.stat(os.path.join(root, f)).st_ino in source_inodes:
                            return True
                    except: pass
        return False

    def link_file(self, source, target):
        if os.path.exists(target): return False
        try:
            os.link(source, target)
            os.chmod(target, 0o777)
            self.ensure_ownership(target)
            return True
        except OSError as e:
            print(f"  -> Link failed: {e}")
            return False

    def run(self) -> None:
        raise NotImplementedError

    def wait_for_command(self, url, api_key, command_id, timeout=120):
        """Wait for a command to complete by polling its status."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                cmd_url = f"{url}/api/v3/command/{command_id}"
                resp = requests.get(cmd_url, headers={"X-Api-Key": api_key})
                if resp.status_code == 200:
                    cmd_data = resp.json()
                    status = cmd_data.get("status")
                    if status in ["completed", "failed"]:
                        return status == "completed"
                time.sleep(2)
            except Exception:
                time.sleep(2)
        return False

    def ensure_ownership(self, path):
        """Chown path to PUID:PGID if set."""
        if self.puid is not None and self.pgid is not None:
            try:
                os.chown(path, self.puid, self.pgid)
            except OSError as e:
                print(f"  -> [WARN] Failed to chown {path}: {e}")

    def clear_queue_for_item(self, url, api_key, item_id, id_field_name):
        """Remove any stuck queue entries for this item after successful import."""
        try:
            q_url = f"{url}/api/v3/queue"
            resp = requests.get(q_url, headers={"X-Api-Key": api_key})
            if resp.status_code != 200:
                return
            records = resp.json().get("records", [])
            for r in records:
                if r.get(id_field_name) == item_id:
                    queue_id = r.get("id")
                    title = r.get("title")
                    print(f"  -> [CLEANUP] Removing stuck queue item '{title}' as media is now imported.")
                    del_url = f"{url}/api/v3/queue/{queue_id}"
                    requests.delete(del_url, params={"removeFromClient": "true", "blocklist": "false"}, headers={"X-Api-Key": api_key})
        except Exception as e:
            print(f"  -> [WARN] Error cleaning queue for {id_field_name}={item_id}: {e}")

class RadarrImporter(MediaImporter):
    def __init__(self):
        super().__init__()
        if not RADARR_VARS["API_KEY"] or not RADARR_VARS["URL"]:
            print("Radarr logic disabled (Env vars missing)")
            self.enabled = False
        else:
            self.enabled = True
            self.api_key = RADARR_VARS["API_KEY"]
            self.url = RADARR_VARS["URL"]

    def run(self):
        if not self.enabled: return
        print("\n--- Radarr Movie Import ---")
        self.find_orphans()

    def get_movies(self):
        try:
            url = f"{self.url}/api/v3/movie?pageSize=10000"
            resp = requests.get(url, headers={"X-Api-Key": self.api_key})
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"Error fetching movies: {e}")
            return []

    def verify_import(self, movie_id):
        """Wait a bit then verify the movie has been imported."""
        time.sleep(5)  # Brief pause for filesystem consistency
        try:
            url = f"{self.url}/api/v3/movie/{movie_id}"
            resp = requests.get(url, headers={"X-Api-Key": self.api_key})
            m = resp.json()
            if m.get("hasFile"):
                print(f"  -> [SUCCESS] Movie '{m['title']}' is now imported!")
                self.clear_queue_for_item(self.url, self.api_key, movie_id, "movieId")
                return True
            else:
                print(f"  -> [FAILURE] Movie '{m['title']}' still missing file.")
                return False
        except: return False

    def force_injection(self, movie_id, source_path, dest_dir):
        if not os.path.exists(dest_dir):
            try: 
                os.makedirs(dest_dir, mode=0o777, exist_ok=True)
                self.ensure_ownership(dest_dir)
            except OSError: return False

        source_file = None
        if os.path.isdir(source_path):
            files = []
            for root, _, fs in os.walk(source_path):
                for f in fs:
                    if f.lower().endswith(('.mkv', '.mp4', '.avi')):
                        files.append(os.path.join(root, f))
            if files: source_file = max(files, key=os.path.getsize)
        else:
            source_file = source_path
        
        if not source_file: return False

        target_file = os.path.join(dest_dir, os.path.basename(source_file))
        if self.link_file(source_file, target_file):
            print(f"  -> Linked {os.path.basename(source_file)}")
            # Rescan and wait for completion
            cmd_url = f"{self.url}/api/v3/command"
            resp = requests.post(cmd_url, json={"name": "RescanMovie", "movieId": movie_id}, headers={"X-Api-Key": self.api_key})
            if resp.status_code == 201:
                cmd_id = resp.json().get("id")
                if cmd_id:
                    print(f"  -> Waiting for RescanMovie to complete...")
                    self.wait_for_command(self.url, self.api_key, cmd_id)
            return True
        return False

    def find_orphans(self):
        movies = self.get_movies()
        missing_movies = []
        for m in movies:
            if not m['hasFile']:
                missing_movies.append(m)
            else:
                # Ghost detection: API says hasFile=True but sizeOnDisk is 0
                size_on_disk = m.get("sizeOnDisk", 0) or m.get("movieFile", {}).get("size", 0)
                if size_on_disk == 0:
                    print(f"  [GHOST] {m['title']} (TMDB {m.get('tmdbId', '?')}): hasFile=True but sizeOnDisk=0!")
                    missing_movies.append(m)

        print(f"Found {len(missing_movies)} missing movies (including ghost entries).")
        
        if not missing_movies: return

        released_movies = []
        for m in missing_movies:
            if not self.is_released("movie", m):
                print(f"  ~ {m['title']} -> Skipped (Not released yet)")
            else:
                released_movies.append(m)
                print(f"  - {m['title']} (ID: {m.get('tmdbId', '?')}, Year: {m.get('year', '?')})")

        if not released_movies: return

        disk_items = self.get_downloads_content()
        video_files = [i["path"] for i in disk_items if i["type"] == "file" and i["name"].lower().endswith(('.mkv', '.mp4', '.avi'))]

        processed_ids = set()

        for m in released_movies:
            if m['id'] in processed_ids: continue

            # Build Check List
            titles = {m["title"], m.get("originalTitle"), m.get("cleanTitle")}
            
            # Remove year
            no_year = re.sub(r'\(\d{4}\)', '', m["title"]).strip()
            if len(no_year) > 3: titles.add(no_year)
            
            if ":" in m["title"]: titles.add(m["title"].split(":")[0].strip())

            # TMDB
            tmdb_id = m.get("tmdbId")
            if tmdb_id:
                aliases = self.get_tmdb_aliases(tmdb_id, 'movie')
                titles.update(aliases)

            # Filter out very short titles that cause false positives
            titles = {t for t in titles if t and len(self.normalize(t)) > 3}

            # Search
            match = None
            for item in disk_items:
                if item["path"].startswith(m["path"]): continue # Skip self
                
                is_match = False
                for t in titles:
                    if self.title_matches(t, item["name"]):
                        is_match = True
                        break
                
                if is_match:
                    # Video Check
                    has_video = False
                    if item["type"] == "file":
                        if item["name"].lower().endswith(('.mkv', '.mp4', '.avi')): has_video = True
                    else:
                        for v in video_files:
                            if v.startswith(item["path"]): 
                                has_video = True
                                break
                    
                    if has_video:
                        # Year check
                        movie_year = m.get('year')
                        file_year_match = re.search(r'\b(\d{4})\b', item['name'])
                        if movie_year and file_year_match:
                            file_year = int(file_year_match.group(1))
                            if abs(file_year - movie_year) > 1: continue

                        match = item
                        break
            
            if match:
                print(f"{m['title']} (TMDB ID: {m['tmdbId']}) -> {match['path']}")
                processed_ids.add(m['id'])
                
                dest_path = m['path']
                if self.check_inode_match(match['path'], dest_path):
                    print("  -> Already linked (Inode). Skipping.")
                    continue

                if self.force_injection(m['id'], match['path'], dest_path):
                    self.verify_import(m['id'])
            else:
                print(f"{m['title']} (TMDB ID: {m.get('tmdbId', '?')}) -> No match found on disk")

class SonarrImporter(MediaImporter):
    def __init__(self):
        super().__init__()
        if not SONARR_VARS["API_KEY"] or not SONARR_VARS["URL"]:
            print("Sonarr logic disabled (Env vars missing)")
            self.enabled = False
        else:
            self.enabled = True
            self.api_key = SONARR_VARS["API_KEY"]
            self.url = SONARR_VARS["URL"]

    def run(self, force_id=None):
        if not self.enabled: return
        print("\n--- Sonarr Series Import ---")
        self.find_orphans(force_id)

    def get_series(self):
        try:
            resp = requests.get(f"{self.url}/api/v3/series", headers={"X-Api-Key": self.api_key})
            resp.raise_for_status()
            return resp.json()
        except: return []

    def get_missing_series_ids(self):
        ids = set()
        page = 1
        while True:
            try:
                res = requests.get(f"{self.url}/api/v3/wanted/missing?page={page}&pageSize=1000", headers={"X-Api-Key": self.api_key})
                data = res.json()
                records = data.get('records', [])
                if not records: break
                
                for r in records:
                    ids.add(r['seriesId'])
                
                if len(records) < 1000: break
                page += 1
            except Exception as e:
                print(f"Error fetching wanted/missing: {e}")
                break
        return ids

    def get_episodes(self, series_id):
        try:
            ep = requests.get(f"{self.url}/api/v3/episode?seriesId={series_id}", headers={"X-Api-Key": self.api_key}).json()
            ef = requests.get(f"{self.url}/api/v3/episodefile?seriesId={series_id}", headers={"X-Api-Key": self.api_key}).json()
            file_paths = {f['id']: f['path'] for f in ef}
            
            existing = set()
            for e in ep:
                if e.get('hasFile'):
                     fp = file_paths.get(e.get('episodeFileId'))
                     if fp and os.path.exists(fp):
                         existing.add((e.get('seasonNumber'), e.get('episodeNumber')))
            return existing
        except: return set()

    def verify_import(self, series_id, injected_episodes):
        """Wait a bit then verify episodes have been imported."""
        if not injected_episodes: return False
        time.sleep(5)  # Brief pause for filesystem consistency
        
        try:
            # Check specific episodes
            all_eps = requests.get(f"{self.url}/api/v3/episode?seriesId={series_id}", headers={"X-Api-Key": self.api_key}).json()
            
            success_count = 0
            for e in all_eps:
                key = (e.get('seasonNumber'), e.get('episodeNumber'))
                if key in injected_episodes:
                    if e.get('hasFile'):
                        success_count += 1
                    else:
                        print(f"  -> [WARN] S{key[0]:02d}E{key[1]:02d} still missing file.")
            
            if success_count > 0:
                print(f"  -> [SUCCESS] {success_count}/{len(injected_episodes)} injected episodes are now imported!")
                self.clear_queue_for_item(self.url, self.api_key, series_id, "seriesId")
                return True
            return False
            
        except Exception as e:
            print(f"  -> Verify failed: {e}")
            return False

    def force_injection(self, series_id, source_path, dest_dir, existing_episodes):
        if not os.path.exists(dest_dir):
            try: 
                os.makedirs(dest_dir, mode=0o777, exist_ok=True)
                self.ensure_ownership(dest_dir)
            except: return []

        files_to_link = []
        if os.path.isdir(source_path):
            for root, dirs, fs in os.walk(source_path):
                 dirs[:] = [d for d in dirs if d.lower() not in ['sample', 'subs', 'extras']]
                 for f in fs:
                     if f.lower().endswith(('.mkv', '.mp4', '.avi')):
                         files_to_link.append(os.path.join(root, f))
        elif source_path.lower().endswith(('.mkv', '.mp4', '.avi')):
            files_to_link.append(source_path)

        injected = []
        linked_count = 0
        skipped_existing = 0
        
        for src in files_to_link:
            filename = os.path.basename(src)
            # Season detection
            match_s = re.search(r'S(\d+)', os.path.basename(src), re.IGNORECASE)
            s_num = int(match_s.group(1)) if match_s else 1
            
            # Episode detection
            e_num = None
            temp = re.sub(r'S(\d+)\s*[-_]\s*(\d+)', r'S\1E\2', filename, flags=re.IGNORECASE)
            match_se = re.search(r'S(\d+)E(\d+)', temp, re.IGNORECASE)
            if match_se:
                e_num = int(match_se.group(2))
            else:
                 parts = re.findall(r'(?:^|[._\-\s\[])(\d{1,3})(?:$|[._\-\s\]])', filename)
                 for p in parts:
                     val = int(p)
                     if val not in [1080, 720, 264, 265, 480]:
                         e_num = val
                         break
            
            if e_num is None:
                # print(f"  -> [SKIP] Could not determine episode number for {filename}")
                continue

            if (s_num, e_num) in existing_episodes:
                skipped_existing += 1
                continue
            
            season_dir = os.path.join(dest_dir, f"Season {s_num}")
            if not os.path.exists(season_dir):
                try: 
                    os.makedirs(season_dir, mode=0o777, exist_ok=True)
                    self.ensure_ownership(season_dir)
                except: pass
            
            target_name = temp if match_se else f"S{s_num:02d}E{e_num:02d}{os.path.splitext(filename)[1]}"
            target_file = os.path.join(season_dir, target_name)
            
            if self.link_file(src, target_file):
                linked_count += 1
                if e_num is not None: injected.append((s_num, e_num))
        
        if linked_count > 0:
            print(f"  -> Injected {linked_count} new files ({skipped_existing} already existed).")
            # Trigger rescan and wait for completion
            resp = requests.post(f"{self.url}/api/v3/command", json={"name": "RescanSeries", "seriesId": series_id}, headers={"X-Api-Key": self.api_key})
            if resp.status_code == 201:
                cmd_id = resp.json().get("id")
                if cmd_id:
                    print(f"  -> Waiting for RescanSeries to complete...")
                    self.wait_for_command(self.url, self.api_key, cmd_id)
            self.verify_import(series_id, injected)
        
        return injected

    def find_orphans(self, force_id=None):
        series_all = self.get_series()
        
        if force_id:
            missing = [s for s in series_all if str(s['id']) == str(force_id)]
            print(f"Forced check for Series ID {force_id}. Found {len(missing)} series.")
        else:
            wanted_ids = self.get_missing_series_ids()
            missing = [s for s in series_all if s['id'] in wanted_ids]
            
            # Ghost detection: series NOT in wanted/missing
            # but API says files exist while sizeOnDisk is 0
            for s in series_all:
                if s['id'] in wanted_ids:
                    continue  # Already in missing list
                stats = s.get('statistics', {})
                file_count = stats.get('episodeFileCount', 0)
                size_on_disk = stats.get('sizeOnDisk', 0)
                if file_count > 0 and size_on_disk == 0:
                    print(f"  [GHOST] {s['title']} (TMDB {s.get('tmdbId', '?')}): {file_count} files but sizeOnDisk=0!")
                    missing.append(s)
            
            print(f"Found {len(missing)} series with wanted missing episodes (including ghost entries).")
        
        if not missing: return

        disk_items = self.get_downloads_content()
        video_files = [i["path"] for i in disk_items if i["type"] == "file" and i["name"].lower().endswith(('.mkv', '.mp4', '.avi'))]

        for s in missing:
            # Series/Episode level checks are not robust enough here right now, 
            # and TMDB caused false negatives. Removed TMDB skip for Sonarr.

            titles = {s["title"], s.get("cleanTitle")}
            if ":" in s["title"]: titles.add(s["title"].split(":")[0].strip())
            no_year = re.sub(r'\(\d{4}\)', '', s["title"]).strip()
            if len(no_year) > 3: titles.add(no_year)

            if 'alternateTitles' in s:
                for alt in s['alternateTitles']: 
                    if 'title' in alt: titles.add(alt['title'])

            tmdb_id = s.get("tmdbId")
            if tmdb_id:
                aliases = self.get_tmdb_aliases(tmdb_id, 'tv')
                titles.update(aliases)
            
            # Filter out very short titles that cause false positives
            titles = {t for t in titles if t and len(self.normalize(t)) > 3}

            matches = []
            for item in disk_items:
                if item["path"].startswith(s["path"]): continue
                
                is_match = False
                for t in titles:
                    if self.title_matches(t, item["name"]):
                        is_match = True
                        break
                
                if is_match:
                    has_video = False
                    if item["type"] == "dir":
                        for v in video_files:
                            if v.startswith(item["path"]): 
                                has_video = True
                                break
                    else: has_video = True # basic assumption if file matched
                    
                    if has_video: matches.append(item)
            
            if matches:
                print(f"{s['title']} (TMDB ID: {s['tmdbId']}) - {len(matches)} sources")
                dest_path = s['path']
                
                already_linked = False
                for m in matches:
                     if self.check_inode_match(m['path'], dest_path):
                         already_linked = True
                
                if already_linked:
                    print("  -> Some files already linked (Inode). Triggering consistency scan.")
                    resp = requests.post(f"{self.url}/api/v3/command", json={"name": "RescanSeries", "seriesId": s['id']}, headers={"X-Api-Key": self.api_key})
                    if resp.status_code == 201:
                        cmd_id = resp.json().get("id")
                        if cmd_id:
                            print(f"  -> Waiting for consistency scan to complete...")
                            self.wait_for_command(self.url, self.api_key, cmd_id)
                
                # Injection
                existing = self.get_episodes(s['id'])
                total_injected = 0
                total_skipped = 0
                for m in matches:
                    result = self.force_injection(s['id'], m['path'], dest_path, existing)
                    total_injected += len(result)
                
                if total_injected == 0:
                    print(f"  -> All matched episodes already imported ({len(existing)} existing). Nothing to inject.")
            else:
                print(f"{s['title']} (TMDB ID: {s.get('tmdbId', '?')}) -> No match found on disk")

if __name__ == "__main__":
    if not os.environ.get("_SEERRSENTINEL_INTERNAL"):
        print("Error: This script cannot be run directly.")
        print("Use:  python3 seerr_sentinel.py import [--radarr] [--sonarr] [--force-id N]")
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument("--radarr", action="store_true")
    parser.add_argument("--sonarr", action="store_true")
    parser.add_argument("--force-id", type=int, help="Force check for specific Series/Movie ID")
    args = parser.parse_args()

    run_all = not args.radarr and not args.sonarr

    if run_all or args.radarr:
        RadarrImporter().run()
    
    if run_all or args.sonarr:
        SonarrImporter().run(force_id=args.force_id)
