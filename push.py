#!/usr/bin/env python3
"""
Force-push current folder to GitHub.

Usage:
    python3 push.py "commit message"
    python3 push.py            # default message: "updated"

Requires a GitHub Personal Access Token in env var GITHUB_TOKEN
(or in a local .env file as GITHUB_TOKEN=ghp_xxx).
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

REPO_URL = "https://github.com/Langenhorner001/Auto-TGBot-Clicker.git"
BRANCH = "main"
HERE = Path(__file__).resolve().parent


def load_env():
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def run(cmd, check=True):
    print(f"\n$ {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    r = subprocess.run(cmd, cwd=HERE, shell=isinstance(cmd, str))
    if check and r.returncode != 0:
        print(f"\n[push.py] Command failed (exit {r.returncode}). Aborting.")
        sys.exit(r.returncode)
    return r.returncode


def main():
    msg = sys.argv[1] if len(sys.argv) > 1 else "updated"

    load_env()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("[push.py] ERROR: GITHUB_TOKEN not set.")
        print("  Add it to bot/.env  ->  GITHUB_TOKEN=ghp_xxxxxxxxxxxx")
        sys.exit(1)

    # Build authenticated remote URL
    remote = REPO_URL.replace("https://", f"https://{token}@", 1)

    # 1) wipe any existing .git
    git_dir = HERE / ".git"
    if git_dir.exists():
        print(f"[push.py] Removing existing .git in {HERE}")
        shutil.rmtree(git_dir)

    # 2) re-init + commit + push
    run(["git", "init"])
    run(["git", "add", "."])
    # allow empty commit so script never fails on no-change
    run(["git", "-c", "user.email=push@local", "-c", "user.name=push.py",
         "commit", "--allow-empty", "-m", msg])
    run(["git", "branch", "-M", BRANCH])
    run(["git", "remote", "add", "origin", remote])
    run(["git", "push", "-u", "origin", BRANCH, "--force"])

    print(f"\n[push.py] Done. Pushed '{msg}' to {REPO_URL} ({BRANCH}).")


if __name__ == "__main__":
    main()
