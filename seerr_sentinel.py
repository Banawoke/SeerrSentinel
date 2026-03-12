#!/usr/bin/env python3
"""
SeerrSentinel — Central orchestrator and configuration loader.

Usage:
    python3 seerr_sentinel.py all [--dry-run]
    python3 seerr_sentinel.py --check-env
    python3 seerr_sentinel.py search
    python3 seerr_sentinel.py clean [--dry-run]
    python3 seerr_sentinel.py import [--radarr] [--sonarr] [--force-id N]
"""

import os
import sys
import subprocess
import argparse
from pathlib import Path
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration — chargement du .env
# ---------------------------------------------------------------------------

# Locate .env: look next to this script first, then fall back to /cronjob/.env (Docker)
_SCRIPT_DIR = Path(__file__).parent
_ENV_CANDIDATES = [
    _SCRIPT_DIR / ".env",
    Path("/cronjob/.env"),
]
_ENV_PATH = next((p for p in _ENV_CANDIDATES if p.exists()), _SCRIPT_DIR / ".env")
load_dotenv(dotenv_path=_ENV_PATH)

# All known variables across the suite
_ALL_REQUIRED_VARS = [
    "JELLYSEER_API_KEY",
    "JELLYSEER_URL",
    "TMDB_API_KEY",
    "RADARR_API_KEY",
    "RADARR_URL",
    "SONARR_API_KEY",
    "SONARR_URL",
    "DOWNLOADS_PATH",
    "PUID",
    "PGID",
]

_ALL_OPTIONAL_VARS = {
    "RELEASE_BUFFER_DAYS": "7",
    "DELETION_DELAY_DAYS": "2",
    "KEEP_REQUESTS_OLDER_THAN_DAYS": "14",
    "STUCK_DOWNLOAD_MINUTES": "20.0",
    "MAX_DOWNLOAD_HOURS": "6.0",
}


def load_config(required: list) -> dict:
    """
    Validates that all variables listed in `required` are set in the environment
    (loaded from .env).

    Returns a dict {var_name: value}.
    Calls sys.exit(1) if any variable is missing.
    """
    config = {}
    missing = []
    for var in required:
        value = os.environ.get(var, "").strip()
        if not value:
            missing.append(var)
        else:
            config[var] = value

    if missing:
        print(
            f"[SeerrSentinel] Missing variables in .env: {', '.join(missing)}\n"
            f"                Expected .env file: {_ENV_PATH}"
        )
        sys.exit(1)

    return config


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# CLI — Commands
# ---------------------------------------------------------------------------

def _cmd_check_env() -> int:
    """Display the status of all known environment variables."""
    print("SeerrSentinel — Environment check")
    print("=" * 52)

    all_ok = True

    print("\n Required variables:")
    for var in _ALL_REQUIRED_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            display = value[:10] + "…" if "KEY" in var else value
            print(f"     {var:<35} = {display}")
        else:
            print(f"     {var:<35} — MISSING")
            all_ok = False

    print("\n Optional variables (default values used if absent):")
    for var, default in _ALL_OPTIONAL_VARS.items():
        value = os.environ.get(var, "")
        if value:
            print(f"     {var:<35} = {value}")
        else:
            print(f"     {var:<35} — (default: {default})")

    print()
    if all_ok:
        print("Environment complete. SeerrSentinel is ready.\n")
        return 0
    else:
        print("Some variables are missing. Please complete the .env file.\n")
        return 1


def _run_script(script_name: str, extra_args: list = None) -> int:
    """Run a sub-script from the same directory via subprocess."""
    script_path = Path(__file__).parent / script_name
    cmd = [sys.executable, str(script_path)] + (extra_args or [])
    result = subprocess.run(cmd)
    return result.returncode


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seerr_sentinel",
        description="SeerrSentinel — Jellyseerr / Radarr / Sonarr management suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  seerr_sentinel.py --check-env\n"
            "  seerr_sentinel.py search\n"
            "  seerr_sentinel.py clean --dry-run\n"
            "  seerr_sentinel.py import --sonarr --force-id 42\n"
            "  seerr_sentinel.py all --dry-run\n"
        ),
    )
    parser.add_argument(
        "--check-env",
        action="store_true",
        help="Check environment variables and exit",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # search
    subparsers.add_parser(
        "search",
        help="Trigger Radarr/Sonarr searches for missing media",
    )

    # clean
    clean_p = subparsers.add_parser(
        "clean",
        help="Clean up missing/stalled media in Radarr, Sonarr and Jellyseerr",
    )
    clean_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulation without making any changes",
    )

    # import
    import_p = subparsers.add_parser(
        "import",
        help="Inject video files from the downloads folder",
    )
    import_p.add_argument("--radarr", action="store_true", help="Radarr only")
    import_p.add_argument("--sonarr", action="store_true", help="Sonarr only")
    import_p.add_argument(
        "--force-id",
        type=int,
        metavar="ID",
        help="Force a specific Series/Movie ID",
    )

    all_p = subparsers.add_parser("all", help="Run all operations in sequence")
    all_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry-run mode for the clean step",
    )

    # daemon
    daemon_p = subparsers.add_parser("daemon", help="Run continuously in the background")
    daemon_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry-run mode for the clean step",
    )
    daemon_p.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Wait time in seconds between loops",
    )

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.check_env:
        sys.exit(_cmd_check_env())

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # En-tête
    separator = "=" * 52
    print(f"\n{separator}")
    print(f"  SeerrSentinel  ›  {args.command.upper()}")
    print(f"{separator}\n")

    if args.command == "search":
        sys.exit(_run_script("sentinel_search.py"))

    elif args.command == "clean":
        extra = ["--dry-run"] if args.dry_run else []
        sys.exit(_run_script("sentinel_cleaner.py", extra))

    elif args.command == "import":
        extra = []
        if args.radarr:
            extra.append("--radarr")
        if args.sonarr:
            extra.append("--sonarr")
        if args.force_id:
            extra += ["--force-id", str(args.force_id)]
        sys.exit(_run_script("sentinel_import.py", extra))

    elif args.command in ("all", "daemon"):
        import json
        import time
        from datetime import datetime, timezone, timedelta
        
        def _check_schedule(job: str, interval_minutes: int) -> bool:
            fpath = Path("/tmp/seerr_sentinel_schedule.json")
            sched = {}
            if fpath.exists():
                try:
                    with open(fpath, "r") as f:
                        sched = json.load(f)
                except Exception:
                    pass
            now = datetime.now(timezone.utc)
            last_run_str = sched.get(job)
            if not last_run_str:
                sched[job] = now.isoformat()
                with open(fpath, "w") as f: json.dump(sched, f)
                return True
            try:
                last_run = datetime.fromisoformat(last_run_str)
                if (now - last_run) >= timedelta(minutes=interval_minutes):
                    sched[job] = now.isoformat()
                    with open(fpath, "w") as f: json.dump(sched, f)
                    return True
            except Exception:
                sched[job] = now.isoformat()
                with open(fpath, "w") as f: json.dump(sched, f)
                return True
            return False

        def _run_pass(is_daemon=False):
            rc1 = 0
            if is_daemon and not _check_schedule("search", 15):
                print("▶  [1/3] SEARCH (Skipped - Interval 15m not reached)")
            else:
                print("▶  [1/3] SEARCH (Executed)")
                rc1 = _run_script("sentinel_search.py")

            rc2 = 0
            if _check_schedule("clean", 240):
                print("\n▶  [2/3] CLEAN (Executed - Interval 4h reached)")
                clean_args = ["--dry-run"] if args.dry_run else []
                rc2 = _run_script("sentinel_cleaner.py", clean_args)
            else:
                print("\n▶  [2/3] CLEAN (Skipped - Interval 4h not reached)")

            rc3 = 0
            if _check_schedule("import", 30):
                print("\n▶  [3/3] IMPORT (Executed - Interval 30m reached)")
                rc3 = _run_script("sentinel_import.py")
            else:
                print("\n▶  [3/3] IMPORT (Skipped - Interval 30m not reached)")

            return max(rc1, rc2, rc3)

        if args.command == "all":
            worst = _run_pass(is_daemon=False)
            print(f"\n{separator}")
            print(f"  SeerrSentinel  ›  ALL done (exit codes up to {worst})")
            print(f"{separator}")
            sys.exit(worst)
        elif args.command == "daemon":
            print("Starting daemon mode. Press Ctrl+C to stop.")
            try:
                while True:
                    print(f"\n{separator}")
                    print(f"  SeerrSentinel  ›  DAEMON PASS AT {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    print(f"{separator}\n")
                    _run_pass(is_daemon=True)
                    print(f"\nSleeping for {args.interval} seconds...")
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\nDaemon stopped by user.")
                sys.exit(0)


if __name__ == "__main__":
    main()
