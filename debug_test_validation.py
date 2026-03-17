#!/usr/bin/env python3
"""
SeerrSentinel - Debug & Validation Suite
This script contains unit tests for the core logic of sentinel_search.py
and helpers to simulate real-world scenarios (cooldown, fails, quotas).
"""
import sys
import os
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add current directory to path to import local modules
_SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPT_DIR.absolute()))

# Mock environment variables for testing logic without calling real APIs
os.environ.setdefault('RADARR_API_KEY', 'dummy_key')
os.environ.setdefault('RADARR_URL', 'http://localhost:7878')
os.environ.setdefault('SONARR_API_KEY', 'dummy_key')
os.environ.setdefault('SONARR_URL', 'http://localhost:8989')

try:
    import sentinel_search as ss
except ImportError:
    print("❌ Error: sentinel_search.py not found in the current directory.")
    sys.exit(1)

# Use a temporary history file for tests to avoid corrupting real history
TEST_HIST = tempfile.mktemp(suffix='_sentinel_test.json')
ss.HISTORY_FILE = TEST_HIST

passed = 0
failed = 0

def check(name, condition, msg=""):
    global passed, failed
    if condition:
        print(f"  ✅ {name}")
        passed += 1
    else:
        print(f"  ❌ {name}: {msg}")
        failed += 1

def run_unit_tests():
    global passed, failed
    print("\n" + "="*50)
    print("  UNIT TESTS: CORE LOGIC")
    print("="*50 + "\n")

    # --- 1. Edge cases: empty / unknown keys ---
    h = ss.load_history()
    check("load_history (missing file) -> empty dict", h == {})
    check("get_fail_count (unknown key) -> 0", ss.get_fail_count('nonexistent') == 0)
    _, rem = ss.get_next_search_time('nonexistent')
    check("get_next_search_time (unknown key) -> 0 remaining", rem == 0)

    # --- 2. Failure counting logic ---
    ss.record_search('test_item', 'MoviesSearch', item_type='movie', title='Test Movie')
    check("fail_count immediate after search", ss.get_fail_count('test_item') == 0)
    
    ss.mark_failed_if_previous_search('test_item', title='Test Movie')
    check("fail_count -> 1 after mark_failed", ss.get_fail_count('test_item') == 1)
    
    ss.mark_failed_if_previous_search('test_item', title='Test Movie')
    check("fail_count -> 2 after 2nd mark_failed", ss.get_fail_count('test_item') == 2)

    # --- 3. Cycle & Quota management ---
    ss.MOVIE_MAX_SEARCHES = 2
    ss.MOVIE_CYCLE_HOURS = 12
    # Reset for this test
    if os.path.exists(TEST_HIST): os.remove(TEST_HIST)
    
    key = 'quota_test'
    # 1st search
    check("Search #1 allowed", ss.check_cycle_quota(key, 'movie') == True)
    ss.record_search(key, 'MoviesSearch', item_type='movie', title='Quota Test')
    # 2nd search
    check("Search #2 allowed", ss.check_cycle_quota(key, 'movie') == True)
    ss.record_search(key, 'MoviesSearch', item_type='movie', title='Quota Test')
    # 3rd search
    check("Search #3 blocked (quota=2 reached)", ss.check_cycle_quota(key, 'movie') == False)

    # --- 4. Cycle Reset ---
    h = ss.load_history()
    # Manually move cycle_start to 13 hours ago (cycle is 12h)
    h[key]['cycle_start'] = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()
    ss.save_history(h)
    check("Cycle reset allowed after 13h (>12h)", ss.check_cycle_quota(key, 'movie') == True)
    ss.record_search(key, 'MoviesSearch', item_type='movie', title='Quota Test')
    check("Fail count reset on new cycle", ss.get_fail_count(key) == 0)

    # --- 5. Cooldown logic ---
    ss.COOLDOWN_REQUEST_MINUTES = 15
    now_iso = datetime.now(timezone.utc).isoformat()
    check("is_cooled_down (now) -> False", ss.is_cooled_down(now_iso) == False)
    
    old_iso = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    check("is_cooled_down (16min ago) -> True", ss.is_cooled_down(old_iso) == True)

    # --- 6. Season Search priority logic ---
    # Scenario: 0 files on disk, but only 2 missing in API out of 12 total
    stats_pack = {"totalEpisodeCount": 12, "episodeFileCount": 0}
    missing_count = 2
    should_pack = (stats_pack["episodeFileCount"] == 0 and stats_pack["totalEpisodeCount"] > 0)
    check("Decision: 0 files found -> Force Pack Search", should_pack == True)

    stats_individual = {"totalEpisodeCount": 12, "episodeFileCount": 10}
    should_pack_2 = (stats_individual["episodeFileCount"] == 0 and stats_individual["totalEpisodeCount"] > 0)
    check("Decision: 10 files found -> Individual Episode Search", should_pack_2 == False)

def run_integration_helper():
    """
    Utility to rewind history for manual testing.
    Usage: python3 debug_test_validation.py rewind
    """
    REAL_HIST = os.environ.get('SENTINEL_HISTORY', '/tmp/force_search_history.json')
    if not os.path.exists(REAL_HIST):
        print(f"File {REAL_HIST} not found.")
        return

    print(f"Rewinding timestamps in {REAL_HIST} by 20 minutes...")
    with open(REAL_HIST, 'r') as f:
        h = json.load(f)

    past = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    for k in h:
        h[k]['next_search'] = past
        h[k]['last_search'] = past

    with open(REAL_HIST, 'w') as f:
        json.dump(h, f, indent=4)
    print("Done. Cooldowns are now expired for all items.")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "rewind":
        run_integration_helper()
    else:
        run_unit_tests()
        print(f"\n" + "="*50)
        print(f"  FINAL RESULTS: {passed} PASSED, {failed} FAILED")
        print("="*50 + "\n")
        
        if os.path.exists(TEST_HIST):
            os.remove(TEST_HIST)
        
        sys.exit(0 if failed == 0 else 1)
