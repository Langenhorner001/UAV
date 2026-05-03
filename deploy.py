#!/usr/bin/env python3
"""
Auto-deploy: GitHub push + EC2 sync + service restart.

Usage:
    python3 deploy.py "commit message"
    python3 deploy.py                 # default message: "updated"
    python3 deploy.py --skip-github   # only deploy to EC2
    python3 deploy.py --skip-ec2      # only push to GitHub

Requires (in bot/.env):
    GITHUB_TOKEN=ghp_xxx              # for GitHub push
    EC2_HOST=18.145.2.26              # default already set
    EC2_USER=ubuntu                   # default already set
    EC2_SSH_KEY_PATH=...              # path to .pem/.ppk key (auto-detected if absent)
"""

import os
import sys
import shlex
import shutil
import subprocess
import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _early_load_env():
    """Load .env BEFORE reading EC2_* defaults below.
    Without this, hardcoded fallback host/user are used even when .env overrides them."""
    env_file = HERE / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_early_load_env()

# ── Config (override via .env or env vars) ───────────────────────────────────
REPO_URL        = "https://github.com/Langenhorner001/Auto-TGBot-Clicker.git"
BRANCH          = "main"
EC2_HOST        = os.environ.get("EC2_HOST", "138.128.243.189")
EC2_USER        = os.environ.get("EC2_USER", "root")
EC2_PATH        = os.environ.get("EC2_DEPLOY_PATH", "/root/Auto-TGBot-Clicker")
EC2_SERVICE     = os.environ.get("EC2_SERVICE", "visitor-bot")

# Critical files that MUST update on every deploy (verification list)
VERIFY_FILES    = ["bot.py"]

# Files/dirs we never want to overwrite on EC2 or push to GitHub
EXCLUDES = [
    "venv", "__pycache__", ".git", "node_modules",
    "*.pyc", "*.pyo", "*.db", "*.sqlite", "*.sqlite3",
    ".env",                      # EC2 ka apna .env hai — overwrite nahi karna
    "attached_assets",
    # Replit-only files — EC2 par nahi chahiye
    ".local", ".pythonlibs", ".cache", ".upm", ".agents",
    ".replit", ".replitignore", "replit.nix", ".ec2_key.pem",
    # Other bot — alag deploy script use karein
    "tg-post-fetcher",
    # Local docs — server par nahi chahiye
    "GUIDE.md", "MIGRATION.md", "replit.md",
    # Other bot's deploy script
    "deploy_fetcher.py",
]

# Colors
G = "\033[92m"; R = "\033[91m"; Y = "\033[93m"; C = "\033[96m"; B = "\033[1m"; X = "\033[0m"


def log(msg, color=""):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{color}[{ts}] {msg}{X}")


def header(text):
    print(f"\n{B}{C}{'━' * 50}{X}")
    print(f"{B}{C}  {text}{X}")
    print(f"{B}{C}{'━' * 50}{X}")


def load_env():
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def run(cmd, cwd=None, check=True, capture=False):
    if isinstance(cmd, list):
        printable = " ".join(cmd)
        shell = False
    else:
        printable = cmd
        shell = True
    print(f"  $ {printable}")
    r = subprocess.run(
        cmd, cwd=cwd or HERE, shell=shell,
        capture_output=capture, text=True
    )
    if check and r.returncode != 0:
        if capture:
            print(r.stdout); print(f"{R}{r.stderr}{X}")
        log(f"Command failed (exit {r.returncode})", R)
        sys.exit(r.returncode)
    return r


# ── 1) GitHub push ───────────────────────────────────────────────────────────
def push_to_github(msg):
    header("📦  GITHUB PUSH")

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        log("GITHUB_TOKEN missing in bot/.env — skipping GitHub push.", Y)
        return False

    remote = REPO_URL.replace("https://", f"https://{token}@", 1)

    git_dir = HERE / ".git"
    if git_dir.exists():
        log("Removing old .git ...", C)
        shutil.rmtree(git_dir)

    run(["git", "init"])
    run(["git", "add", "."])
    run([
        "git",
        "-c", "user.email=deploy@local",
        "-c", "user.name=deploy.py",
        "commit", "--allow-empty", "-m", msg,
    ])
    run(["git", "branch", "-M", BRANCH])
    run(["git", "remote", "add", "origin", remote])
    r = run(["git", "push", "-u", "origin", BRANCH, "--force"], check=False)

    if r.returncode == 0:
        log(f"GitHub push OK ✅  ({REPO_URL})", G)
        return True
    log("GitHub push failed ❌", R)
    return False


# ── 2) EC2 deploy ────────────────────────────────────────────────────────────
def find_ssh_key():
    # 1) explicit path from env
    p = os.environ.get("EC2_SSH_KEY_PATH", "").strip()
    if p and Path(p).exists():
        return Path(p)

    # 2) common local locations (search workspace + home)
    candidates = [
        HERE / "ec2_key.pem",
        HERE / "ec2_key.ppk",
        HERE.parent / "attached_assets",        # ppk dropped here
        Path.home() / ".ssh" / "ec2_key.pem",
        Path("/tmp/sshwork/ec2_key.pem"),       # converted earlier
    ]
    for c in candidates:
        if c.is_dir():
            for f in c.glob("*.pem"):
                return f
            for f in c.glob("*.ppk"):
                return f
        elif c.exists():
            return c
    return None


def convert_ppk_if_needed(key_path: Path) -> Path:
    """If key is PPK, convert to OpenSSH PEM via Node sshpk (already installed in /tmp/sshwork)."""
    if key_path.suffix.lower() != ".ppk":
        return key_path

    out = HERE / ".ec2_key.pem"
    if out.exists():
        return out

    log(f"Converting PPK → OpenSSH ({key_path.name}) ...", C)
    converter = Path("/tmp/sshwork/node_modules/sshpk")
    if not converter.exists():
        log("sshpk not installed — install via: cd /tmp/sshwork && npm i sshpk", R)
        sys.exit(1)

    js = f"""
        const sshpk = require('{converter}');
        const fs = require('fs');
        const ppk = fs.readFileSync({repr(str(key_path))}, 'utf8');
        const key = sshpk.parsePrivateKey(ppk, 'putty');
        fs.writeFileSync({repr(str(out))}, key.toString('ssh'));
    """
    subprocess.run(["node", "-e", js], check=True)
    out.chmod(0o600)
    return out


def ssh_base(key: Path | None):
    """Build SSH command. If EC2_SSH_PASSWORD is set, use sshpass; else use key."""
    pwd = os.environ.get("EC2_SSH_PASSWORD", "").strip()
    common_opts = [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=20",
    ]
    if pwd:
        return [
            "sshpass", "-p", pwd,
            "ssh",
            "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=password",
            *common_opts,
            f"{EC2_USER}@{EC2_HOST}",
        ]
    return [
        "ssh", "-i", str(key),
        *common_opts,
        f"{EC2_USER}@{EC2_HOST}",
    ]


def _ssh_capture(ssh_cmd, remote_cmd):
    """Run remote command via SSH, return stdout (stripped). Empty on failure."""
    r = subprocess.run(
        ssh_cmd + [remote_cmd],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def deploy_to_ec2():
    header("🚀  EC2 DEPLOY")

    pwd = os.environ.get("EC2_SSH_PASSWORD", "").strip()
    key_path = None
    if pwd:
        log("Auth: PASSWORD (sshpass)", C)
    else:
        key_path = find_ssh_key()
        if not key_path:
            log("SSH key not found. Set EC2_SSH_KEY_PATH, EC2_SSH_PASSWORD, or place .ppk/.pem in attached_assets/", R)
            return False
        log(f"SSH key: {key_path}", C)
        key_path = convert_ppk_if_needed(key_path)
        key_path.chmod(0o600)

    log(f"Target  : {EC2_USER}@{EC2_HOST}:{EC2_PATH}", C)
    log(f"Service : {EC2_SERVICE}", C)
    log(f"Verify  : {', '.join(VERIFY_FILES)}", C)

    ssh_cmd = ssh_base(key_path)

    # Snapshot pre-deploy mtimes — we'll compare after sync to confirm files changed
    pre_deploy_ts = int(datetime.datetime.now().timestamp())
    pre_mtimes = {}
    for fname in VERIFY_FILES:
        out = _ssh_capture(ssh_cmd, f"stat -c %Y {EC2_PATH}/{fname} 2>/dev/null || echo 0")
        pre_mtimes[fname] = int(out or "0")
    log(f"Pre-deploy snapshot taken (ts={pre_deploy_ts})", C)

    # Build tar locally (with excludes), stream via SSH, extract on EC2
    tar_excludes = []
    for pat in EXCLUDES:
        tar_excludes += ["--exclude", pat]

    log("Packing files ...", C)
    tar_proc = subprocess.Popen(
        ["tar", "czf", "-", *tar_excludes, "-C", str(HERE), "."],
        stdout=subprocess.PIPE,
    )

    remote_cmd = (
        f"mkdir -p {EC2_PATH} && "
        f"tar xzf - -C {EC2_PATH} && "
        f"echo '--- files synced ---' && "
        f"ls -la {EC2_PATH} | head -20"
    )

    log("Streaming to EC2 ...", C)
    ssh = subprocess.Popen(
        ssh_cmd + [remote_cmd],
        stdin=tar_proc.stdout,
    )
    tar_proc.stdout.close()
    ssh_rc = ssh.wait()
    tar_rc = tar_proc.wait()

    if tar_rc != 0 or ssh_rc != 0:
        log(f"File sync failed (tar={tar_rc}, ssh={ssh_rc})", R)
        return False
    log("Files synced ✅", G)

    # ── POST-DEPLOY VERIFICATION (catches the "silent old-file" bug) ─────
    log("Verifying files actually updated on remote ...", C)
    verify_failed = []
    for fname in VERIFY_FILES:
        local_path = HERE / fname
        if not local_path.exists():
            log(f"  ⚠️  {fname}: local file missing — skipping verify", Y)
            continue
        local_size = local_path.stat().st_size
        out = _ssh_capture(
            ssh_cmd,
            f"stat -c '%Y %s' {EC2_PATH}/{fname} 2>/dev/null || echo '0 0'",
        )
        try:
            new_mtime, new_size = (int(x) for x in out.split()[:2])
        except ValueError:
            new_mtime, new_size = 0, 0

        old_mtime  = pre_mtimes.get(fname, 0)
        local_mtime = int(local_path.stat().st_mtime)
        # tar preserves source mtime on extract, so remote mtime ≈ local mtime
        # (NOT >= pre_deploy_ts). Trust size + (mtime changed OR mtime ≈ local mtime).
        size_ok   = new_size == local_size
        mtime_changed = new_mtime != old_mtime
        mtime_matches_local = abs(new_mtime - local_mtime) <= 2
        unchanged = (not mtime_changed) and (not mtime_matches_local) and old_mtime != 0

        status = (f"  {fname}: mtime {old_mtime} → {new_mtime} (local {local_mtime})  "
                  f"size local={local_size} remote={new_size}")
        if size_ok and (mtime_changed or mtime_matches_local):
            log(status + "  ✅", G)
        elif unchanged:
            log(status + "  ❌ NOT UPDATED (file unchanged on remote!)", R)
            verify_failed.append(fname)
        else:
            log(status + "  ❌ MISMATCH", R)
            verify_failed.append(fname)

    if verify_failed:
        log(f"Verification FAILED for: {', '.join(verify_failed)}", R)
        log(f"Hint: check EC2_PATH={EC2_PATH} matches the path the service "
            f"actually runs from. Common gotcha: service runs from /root/... "
            f"but deploy.py defaulted to /home/ubuntu/...", Y)
        return False

    # Restart service. If user requires a sudo password, pipe EC2_SSH_PASSWORD via `sudo -S`.
    log(f"Restarting service: {EC2_SERVICE} ...", C)
    sudo_pwd = os.environ.get("EC2_SUDO_PASSWORD", "").strip() or pwd
    if sudo_pwd:
        sudo_prefix = f"echo {shlex.quote(sudo_pwd)} | sudo -S -p ''"
    else:
        sudo_prefix = "sudo -n"
    remote_restart = (
        f"{sudo_prefix} systemctl restart {EC2_SERVICE} && "
        f"sleep 2 && "
        f"systemctl is-active {EC2_SERVICE} && "
        f"systemctl status {EC2_SERVICE} --no-pager -n 5"
    )
    r = subprocess.run(ssh_cmd + [remote_restart])
    if r.returncode != 0:
        log("Service restart failed ❌", R)
        return False
    log("Service restarted ✅", G)
    return True


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    skip_github = "--skip-github" in args
    skip_ec2    = "--skip-ec2"    in args
    args = [a for a in args if not a.startswith("--")]
    msg = args[0] if args else f"updated - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"

    load_env()

    print(f"\n{B}Deploy starting{X}  ({msg})")

    gh_ok = ec2_ok = True
    if not skip_github:
        gh_ok = push_to_github(msg)
    if not skip_ec2:
        ec2_ok = deploy_to_ec2()

    header("📊  SUMMARY")
    print(f"  GitHub : {'✅' if gh_ok else ('⏭️ skipped' if skip_github else '❌')}")
    print(f"  EC2    : {'✅' if ec2_ok else ('⏭️ skipped' if skip_ec2 else '❌')}")

    if (not skip_github and not gh_ok) or (not skip_ec2 and not ec2_ok):
        sys.exit(1)


if __name__ == "__main__":
    main()
