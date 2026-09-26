#!/usr/bin/env python3
"""
SeerrSentinel — Jellyseerr / Radarr / Sonarr Request Diagnostic and Debugging Tool

This script inspects media request statuses, detects "zombie / orphaned requests"
(requests approved in Jellyseerr but absent from Radarr/Sonarr that block users
with the 'Duplicate request for media blocked' error), and cleans up database inconsistencies.

Zero hardcoded API keys or URLs: all configuration is dynamically loaded from .env.

Usage:
    # Global audit of active requests and detection of zombie/duplicate blockers
    python3 test_debug_requests.py --audit

    # Inspect a specific media item by TMDB ID (e.g. 1433087)
    python3 test_debug_requests.py --inspect 1433087

    # Inspect and unblock/clean a specific media item
    python3 test_debug_requests.py --inspect 1433087 --fix

    # Clean all detected orphaned requests (dry-run simulation by default)
    python3 test_debug_requests.py --clean-orphans
    python3 test_debug_requests.py --clean-orphans --apply

    # Inspect and manage Radarr exclusions
    python3 test_debug_requests.py --exclusions
    python3 test_debug_requests.py --unexclude 1433087
"""

import os
import sys
import argparse
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration Loading (.env)
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_CANDIDATES = [
    os.path.join(_SCRIPT_DIR, ".env"),
    os.path.join(_SCRIPT_DIR, "..", ".env"),
    "/cronjob/.env",
]
for candidate in _ENV_CANDIDATES:
    if os.path.exists(candidate):
        load_dotenv(dotenv_path=candidate)

JELLYSEER_API_KEY = os.environ.get("JELLYSEER_API_KEY", "").strip()
JELLYSEER_URL = os.environ.get("JELLYSEER_URL", "").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "").strip()
RADARR_URL = os.environ.get("RADARR_URL", "").rstrip("/")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "").strip()
SONARR_URL = os.environ.get("SONARR_URL", "").rstrip("/")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()

# ---------------------------------------------------------------------------
# Official Jellyseerr Status Mappings
# ---------------------------------------------------------------------------
MEDIA_STATUS = {
    1: "UNKNOWN",
    2: "PENDING",
    3: "PROCESSING",
    4: "PARTIALLY_AVAILABLE",
    5: "AVAILABLE",
    6: "BLOCKLISTED",
    7: "DELETED",
}

REQUEST_STATUS = {
    1: "PENDING",
    2: "APPROVED",
    3: "DECLINED",
    4: "FAILED",
    5: "COMPLETED",
}


def _check_config(require_servarr=True):
    """Validate that required environment variables are present."""
    missing = []
    if not JELLYSEER_API_KEY:
        missing.append("JELLYSEER_API_KEY")
    if not JELLYSEER_URL:
        missing.append("JELLYSEER_URL")
    if require_servarr:
        if not RADARR_API_KEY:
            missing.append("RADARR_API_KEY")
        if not RADARR_URL:
            missing.append("RADARR_URL")
    if missing:
        print(f"❌ [ERROR] Missing environment variables in .env: {', '.join(missing)}")
        print("   Please check your .env file in the repository root or /cronjob/.env.")
        sys.exit(1)


def get_jellyseerr_headers():
    return {"X-Api-Key": JELLYSEER_API_KEY, "Content-Type": "application/json"}


def get_radarr_headers():
    return {"X-Api-Key": RADARR_API_KEY, "Content-Type": "application/json"}


def get_sonarr_headers():
    return {"X-Api-Key": SONARR_API_KEY, "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Jellyseerr API Functions
# ---------------------------------------------------------------------------
def fetch_jellyseerr_movie(tmdb_id):
    """Fetch movie details and mediaInfo state from Jellyseerr."""
    url = f"{JELLYSEER_URL}/api/v1/movie/{tmdb_id}"
    try:
        resp = requests.get(url, headers=get_jellyseerr_headers(), timeout=10)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 404:
            return None
        else:
            print(f"⚠️ Jellyseerr HTTP {resp.status_code} error for TMDB ID {tmdb_id}")
            return None
    except Exception as e:
        print(f"⚠️ Exception during Jellyseerr API call: {e}")
        return None


def fetch_jellyseerr_tv(tmdb_id):
    """Fetch TV series details and mediaInfo state from Jellyseerr."""
    url = f"{JELLYSEER_URL}/api/v1/tv/{tmdb_id}"
    try:
        resp = requests.get(url, headers=get_jellyseerr_headers(), timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception:
        return None


def fetch_all_jellyseerr_requests(filter_type="all", limit=500):
    """Fetch paginated requests from Jellyseerr."""
    requests_list = []
    skip = 0
    take = 50

    while skip < limit:
        url = f"{JELLYSEER_URL}/api/v1/request?filter={filter_type}&sort=added&sortDirection=desc&skip={skip}&take={take}"
        try:
            resp = requests.get(url, headers=get_jellyseerr_headers(), timeout=10)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            if not results:
                break
            requests_list.extend(results)
            total = data.get("pageInfo", {}).get("results", 0)
            skip += take
            if skip >= total:
                break
        except Exception as e:
            print(f"⚠️ Error fetching Jellyseerr requests: {e}")
            break

    return requests_list


def delete_jellyseerr_request(request_id):
    """Delete a request in Jellyseerr by its ID."""
    url = f"{JELLYSEER_URL}/api/v1/request/{request_id}"
    try:
        resp = requests.delete(url, headers=get_jellyseerr_headers(), timeout=10)
        return resp.status_code in (200, 204)
    except Exception as e:
        print(f"⚠️ Error deleting request #{request_id}: {e}")
        return False


def delete_jellyseerr_media(media_id):
    """Delete an orphaned media entry in Jellyseerr."""
    url = f"{JELLYSEER_URL}/api/v1/media/{media_id}"
    try:
        resp = requests.delete(url, headers=get_jellyseerr_headers(), timeout=10)
        return resp.status_code in (200, 204)
    except Exception as e:
        print(f"⚠️ Error deleting media #{media_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# Radarr & Sonarr API Functions
# ---------------------------------------------------------------------------
def check_radarr_movie(tmdb_id):
    """Check if a movie exists in Radarr by its TMDB ID."""
    if not RADARR_URL or not RADARR_API_KEY:
        return None
    url = f"{RADARR_URL}/api/v3/movie?tmdbId={tmdb_id}"
    try:
        resp = requests.get(url, headers=get_radarr_headers(), timeout=10)
        if resp.status_code == 200:
            movies = resp.json()
            return movies[0] if movies else None
        return None
    except Exception as e:
        print(f"⚠️ Radarr error: {e}")
        return None


def get_radarr_exclusions():
    """Retrieve the list of configured exclusions from Radarr."""
    if not RADARR_URL or not RADARR_API_KEY:
        return []
    url = f"{RADARR_URL}/api/v3/exclusions"
    try:
        resp = requests.get(url, headers=get_radarr_headers(), timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return []
    except Exception as e:
        print(f"⚠️ Error fetching Radarr exclusions: {e}")
        return []


def remove_radarr_exclusion(exclusion_id):
    """Remove an exclusion in Radarr."""
    url = f"{RADARR_URL}/api/v3/exclusions/{exclusion_id}"
    try:
        resp = requests.delete(url, headers=get_radarr_headers(), timeout=10)
        return resp.status_code in (200, 204)
    except Exception as e:
        print(f"⚠️ Error removing exclusion #{exclusion_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# Main Diagnostic & Cleanup Commands
# ---------------------------------------------------------------------------
def cmd_inspect(tmdb_id, fix=False, media_type="movie"):
    """Deep inspection of a specific media item to diagnose request blocking."""
    print("=" * 70)
    print(f"  🔍 DETAILED MEDIA DIAGNOSTIC FOR TMDB ID: {tmdb_id}")
    print("=" * 70)

    # 1. Jellyseerr lookup
    js_data = fetch_jellyseerr_movie(tmdb_id) if media_type == "movie" else fetch_jellyseerr_tv(tmdb_id)
    if not js_data:
        print(f"ℹ️ No media entry found in Jellyseerr for TMDB ID {tmdb_id}.")
        return

    title = js_data.get("title") or js_data.get("name") or "Unknown Title"
    release_date = js_data.get("releaseDate") or js_data.get("firstAirDate") or "Unknown Date"
    media_info = js_data.get("mediaInfo")

    print(f"🎬 Title             : {title} ({release_date})")

    if not media_info:
        print("✅ Jellyseerr State  : Clean (no media record in DB).")
        print("   👉 The 'Request' button will work normally without any duplicate error.")
        return

    m_id = media_info.get("id")
    m_status_num = media_info.get("status", 1)
    m_status_name = MEDIA_STATUS.get(m_status_num, f"UNKNOWN({m_status_num})")
    ext_id = media_info.get("externalServiceId")
    requests_list = media_info.get("requests", [])

    print(f"📦 Media DB ID       : #{m_id}")
    print(f"📊 Media Status      : {m_status_name} (code: {m_status_num})")
    print(f"🔗 External Arr ID   : {ext_id or 'None'}")
    print(f"📋 Linked Requests   : {len(requests_list)}")

    has_blocking_request = False
    for r in requests_list:
        r_id = r.get("id")
        r_status_num = r.get("status")
        r_status_name = REQUEST_STATUS.get(r_status_num, f"UNKNOWN({r_status_num})")
        r_created = r.get("createdAt")
        user = r.get("requestedBy", {})
        u_name = user.get("displayName") or user.get("email") or user.get("username") or "Unknown"

        print(f"   ↳ Request #{r_id} | Status: {r_status_name} | By: {u_name} | Date: {r_created}")

        # Jellyseerr validation rule: if not DECLINED (3) and not COMPLETED (5) => BLOCKING
        if r_status_num not in (3, 5):
            has_blocking_request = True

    # 2. Radarr lookup
    radarr_item = check_radarr_movie(tmdb_id)
    if radarr_item:
        has_file = radarr_item.get("hasFile", False)
        print(f"📡 Radarr            : Present (ID: {radarr_item.get('id')}, File: {'✅ Yes' if has_file else '❌ No'}, Monitored: {radarr_item.get('monitored')})")
    else:
        print("📡 Radarr            : ❌ Not found in library")

    # 3. Radarr exclusions lookup
    exclusions = get_radarr_exclusions()
    matched_ex = [e for e in exclusions if e.get("tmdbId") == int(tmdb_id)]
    if matched_ex:
        print(f"⛔ Radarr Exclusions : ⚠️ Movie is present in the exclusion list (ID: {matched_ex[0].get('id')})")
    else:
        print("⛔ Radarr Exclusions : Not excluded")

    # 4. Conflict / blocking diagnostic
    print("\n--- Diagnostic ---")
    if has_blocking_request:
        if radarr_item:
            has_file = radarr_item.get("hasFile", False)
            if has_file:
                print("✅ Media is downloaded and available on disk.")
            else:
                print("ℹ️ Legitimate in-flight request:")
                print(f"   The movie is properly registered in Radarr (ID {radarr_item.get('id')}) and monitored.")
                print("   It is actively waiting for indexer search / download completion.")
                print("   ➡️ Additional 'Request' clicks will be blocked as duplicate (expected behavior while in-progress).")
        else:
            # Absent from Radarr but approved in Jellyseerr = REAL ZOMBIE REQUEST
            print("🚨 CONFIRMED ZOMBIE / ORPHANED REQUEST!")
            print("   An active request (APPROVED) exists in Jellyseerr, but the movie is COMPLETELY ABSENT from Radarr.")
            print("   ➡️ This movie will never download automatically, and users cannot re-request it.")
            print("   ➡️ Any click on 'Request' triggers: [warn][Media Request]: Duplicate request for media blocked")

            if fix:
                print("\n🔧 [APPLYING FIX]")
                for r in requests_list:
                    req_id = r.get("id")
                    print(f"   Deleting request #{req_id}...", end=" ")
                    ok = delete_jellyseerr_request(req_id)
                    print("✅" if ok else "❌")
                if m_id:
                    print(f"   Deleting media entry #{m_id}...", end=" ")
                    ok = delete_jellyseerr_media(m_id)
                    print("✅" if ok else "❌")
                if matched_ex:
                    print(f"   Removing Radarr exclusion #{matched_ex[0].get('id')}...", end=" ")
                    ok = remove_radarr_exclusion(matched_ex[0].get("id"))
                    print("✅" if ok else "❌")
                print("\n✅ Media record successfully cleaned! Users can now request it cleanly.")
            else:
                print("\n💡 To unblock this movie, run this command with --fix:")
                print(f"   python3 test_debug_requests.py --inspect {tmdb_id} --fix")
    else:
        print("✅ No blocking conflicts found on this media (clean state or completed request).")


def cmd_audit():
    """Scan all active Jellyseerr requests and detect orphaned/blocking entries."""
    print("=" * 70)
    print("  📋 GLOBAL JELLYSEERR REQUEST AUDIT & DUPLICATE BLOCK CHECK")
    print("=" * 70)

    requests_all = fetch_all_jellyseerr_requests(filter_type="all")
    print(f"ℹ️ {len(requests_all)} requests retrieved from Jellyseerr.\n")

    zombies = []

    for req in requests_all:
        req_id = req.get("id")
        req_status = req.get("status")
        media = req.get("media") or {}
        tmdb_id = media.get("tmdbId")
        media_id = media.get("id")
        media_status = media.get("status", 1)
        media_type = media.get("mediaType", "movie")

        # Looking for APPROVED (2) requests where media is UNKNOWN (1) or DELETED (7)
        if req_status == 2 and media_status in (1, 7):
            in_radarr = False
            if media_type == "movie" and tmdb_id:
                radarr_obj = check_radarr_movie(tmdb_id)
                in_radarr = bool(radarr_obj)

            if not in_radarr:
                user = req.get("requestedBy", {})
                u_name = user.get("displayName") or user.get("email") or "Unknown"
                zombies.append({
                    "req_id": req_id,
                    "media_id": media_id,
                    "tmdb_id": tmdb_id,
                    "req_status": REQUEST_STATUS.get(req_status, req_status),
                    "media_status": MEDIA_STATUS.get(media_status, media_status),
                    "user": u_name,
                    "created": req.get("createdAt", "")[:10],
                    "type": media_type,
                })

    if not zombies:
        print("✅ NO ZOMBIE REQUESTS DETECTED.")
        print("   All recorded requests are consistent with Radarr/Sonarr.")
    else:
        print(f"⚠️ {len(zombies)} ORPHANED / ZOMBIE REQUEST(S) DETECTED:")
        print("-" * 75)
        print(f"{'TMDB ID':<10} | {'REQ ID':<8} | {'MEDIA ID':<9} | {'REQ STATUS':<10} | {'MEDIA STATUS':<12} | {'REQUESTER'}")
        print("-" * 75)
        for z in zombies:
            print(f"{z['tmdb_id']:<10} | #{z['req_id']:<7} | #{z['media_id']:<8} | {z['req_status']:<10} | {z['media_status']:<12} | {z['user']}")
        print("-" * 75)
        print("\n💡 These requests will block future request attempts with 'Duplicate request for media blocked'.")
        print("   To purge them automatically:")
        print("   python3 test_debug_requests.py --clean-orphans --apply")


def cmd_clean_orphans(apply_changes=False):
    """Clean all detected orphaned requests."""
    print("=" * 70)
    mode_str = "REAL (--apply)" if apply_changes else "DRY-RUN (Simulation)"
    print(f"  🧹 CLEANING ORPHANED REQUESTS — MODE: {mode_str}")
    print("=" * 70)

    requests_all = fetch_all_jellyseerr_requests(filter_type="all")
    zombies = []

    for req in requests_all:
        req_id = req.get("id")
        req_status = req.get("status")
        media = req.get("media") or {}
        tmdb_id = media.get("tmdbId")
        media_id = media.get("id")
        media_status = media.get("status", 1)

        if req_status == 2 and media_status in (1, 7):
            radarr_obj = check_radarr_movie(tmdb_id) if tmdb_id else None
            if not radarr_obj:
                zombies.append((req_id, media_id, tmdb_id))

    if not zombies:
        print("✅ Nothing to clean: Jellyseerr database is already consistent.")
        return

    print(f"Found {len(zombies)} orphaned element(s) to process.")

    for req_id, media_id, tmdb_id in zombies:
        if apply_changes:
            print(f"🗑 Deleting Request #{req_id} and Media #{media_id} (TMDB {tmdb_id})...", end=" ")
            ok1 = delete_jellyseerr_request(req_id)
            ok2 = delete_jellyseerr_media(media_id)
            if ok1 and ok2:
                print("✅ Success")
            else:
                print(f"⚠️ Partial (Request: {ok1}, Media: {ok2})")
        else:
            print(f"  [DRY-RUN] Would delete Request #{req_id} and Media #{media_id} (TMDB {tmdb_id})")

    if not apply_changes:
        print("\n💡 To execute these deletions for real, add --apply:")
        print("   python3 test_debug_requests.py --clean-orphans --apply")
    else:
        print("\n✅ Cleanup completed successfully.")


def cmd_list_exclusions():
    """List all excluded movies in Radarr."""
    print("=" * 70)
    print("  ⛔ RADARR EXCLUSION LIST")
    print("=" * 70)
    exclusions = get_radarr_exclusions()
    if not exclusions:
        print("✅ No exclusions found in Radarr.")
        return

    print(f"Found {len(exclusions)} exclusion(s) in Radarr:\n")
    print(f"{'ID':<6} | {'TMDB ID':<10} | {'TITLE':<40} | {'YEAR'}")
    print("-" * 65)
    for ex in exclusions:
        ex_id = ex.get("id")
        tmdb = ex.get("tmdbId")
        title = ex.get("movieTitle", "Unknown")[:38]
        year = ex.get("movieYear", "N/A")
        print(f"{ex_id:<6} | {tmdb:<10} | {title:<40} | {year}")


def cmd_unexclude(tmdb_id):
    """Remove a Radarr exclusion by its TMDB ID."""
    exclusions = get_radarr_exclusions()
    matches = [e for e in exclusions if str(e.get("tmdbId")) == str(tmdb_id)]
    if not matches:
        print(f"ℹ️ No exclusion found in Radarr for TMDB ID {tmdb_id}.")
        return

    for ex in matches:
        ex_id = ex.get("id")
        title = ex.get("movieTitle")
        print(f"Removing exclusion #{ex_id} for '{title}' (TMDB {tmdb_id})...", end=" ")
        if remove_radarr_exclusion(ex_id):
            print("✅ Success")
        else:
            print("❌ Failed")


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Diagnostic and cleanup tool for Jellyseerr / Radarr / Sonarr requests"
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Scan all active requests and identify zombie/orphaned entries",
    )
    parser.add_argument(
        "--inspect",
        "-i",
        type=int,
        metavar="TMDB_ID",
        help="Inspect detailed state of a movie or series via its TMDB ID",
    )
    parser.add_argument(
        "--type",
        choices=["movie", "tv"],
        default="movie",
        help="Media type for inspection (movie or tv, default: movie)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="When combined with --inspect, unblocks and cleans up the targeted media",
    )
    parser.add_argument(
        "--clean-orphans",
        action="store_true",
        help="Clean all identified orphaned requests",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply deletions for real (defaults to dry-run simulation)",
    )
    parser.add_argument(
        "--exclusions",
        action="store_true",
        help="List all exclusions registered in Radarr",
    )
    parser.add_argument(
        "--unexclude",
        type=int,
        metavar="TMDB_ID",
        help="Remove a Radarr exclusion by TMDB ID",
    )

    args = parser.parse_args()
    _check_config()

    if args.inspect:
        cmd_inspect(args.inspect, fix=args.fix, media_type=args.type)
    elif args.clean_orphans:
        cmd_clean_orphans(apply_changes=args.apply)
    elif args.exclusions:
        cmd_list_exclusions()
    elif args.unexclude:
        cmd_unexclude(args.unexclude)
    else:
        # Default action: full audit
        cmd_audit()


if __name__ == "__main__":
    main()
