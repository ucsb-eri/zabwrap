#!/usr/bin/env python3
import subprocess
import argparse
import os
import sys
import logging
import datetime
import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed

# Lockfile and logging settings
lockfile_path = "/tmp/zfs_autobackup.lock"
logfile_path = "/var/log/zfs_backup.log"

# Backup settings
BACKUP_TYPES = {
    "one": "175,1h5d,1w1y",
    "r2": "650,1h10d,1d1y",
    "r1": "650,1h10d,1d1y",
    "r0": "0",
    "sandbox": "250,1h10d",
    "raid-sandbox": "10,1h10d",
    "scratch": "",
}

# Zabbix settings
ZABBIX_SERVER = "zabbix.grit.ucsb.edu"
PSK_IDENTITY = "GEOG Linux Servers"
PSK_FILE = "/etc/zabbix/zabbix_agent.psk"

# ANSI color codes
RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RESET = "\033[0m"

# Configure logging
logging.basicConfig(
    filename=logfile_path,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

lock_file_handle = None


def run_subprocess(cmd, use_sudo=False, timeout=None):
    cmd = list(cmd)

    if use_sudo:
        cmd.insert(0, "sudo")

    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout
        )
    except subprocess.TimeoutExpired:
        logging.error(f"Command timed out: {' '.join(cmd)}")
        print(f"{RED}Timeout expired while running: {' '.join(cmd)}{RESET}")
        return None


def acquire_lock():
    global lock_file_handle

    lock_file_handle = open(lockfile_path, "w")
    lock_file_handle.write(str(os.getpid()) + "\n")
    lock_file_handle.flush()

    try:
        fcntl.flock(lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logging.info("Lock acquired, no other instances are running.")
        print(f"{GREEN}Lock acquired, no other instances are running.{RESET}")
    except BlockingIOError:
        logging.error("Another instance of the script is running.")
        print(f"{RED}Another instance of the script is running.{RESET}")
        lock_file_handle.close()
        lock_file_handle = None
        sys.exit(1)


def release_lock():
    global lock_file_handle

    if lock_file_handle:
        try:
            fcntl.flock(lock_file_handle, fcntl.LOCK_UN)
            lock_file_handle.close()
            lock_file_handle = None

            try:
                os.remove(lockfile_path)
                logging.info("Lock file removed.")
                print(f"{GREEN}Lock file removed.{RESET}")
            except FileNotFoundError:
                pass

            logging.info("Lock released, script completed.")
            print(f"{GREEN}Lock released, script completed.{RESET}")

        except Exception as e:
            logging.error(f"Error releasing lock: {e}")
            print(f"{RED}Error releasing lock: {e}{RESET}")


def get_zfs_fs_list():
    run = run_subprocess(["zfs", "list", "-Hp", "-o", "name"])

    if not run or run.returncode != 0:
        logging.error(f"Failed to list ZFS filesystems: {run.stderr if run else 'timeout'}")
        print(f"{RED}Failed to list ZFS filesystems{RESET}")
        return {}

    fslist = run.stdout.strip().split("\n")
    return {fs: {} for fs in fslist if fs}


def send_to_zabbix(host, key, value):
    sanitized_value = value.replace("\n", "\\n").replace('"', "").replace("\\", "\\\\")
    cmd = [
        "sudo", "zabbix_sender",
        "-z", ZABBIX_SERVER,
        "-s", host,
        "--tls-connect", "psk",
        "--tls-psk-identity", PSK_IDENTITY,
        "--tls-psk-file", PSK_FILE,
        "-k", key,
        "-o", sanitized_value
    ]

    process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if process.returncode != 0:
        print(f"Error sending data to Zabbix: {process.stderr.strip()}")
    else:
        print(f"Data sent to Zabbix: {process.stdout.strip()}")


def set_backup_property(fs, status, message):
    timestamp = datetime.datetime.now().isoformat()
    status_message = f"{status} at {timestamp}: {message}"
    subprocess.run(["zfs", "set", f"zab:lastbackup={status_message}", fs])


def run_backup(dry_run, fs, zabselect, server, retention, path, include_snapshots):
    command_parts = [
        "/usr/local/bin/zfs-autobackup",
        zabselect,
        path,
        "--verbose",
        "--keep-source",
        retention,
        "--ssh-target",
        server,
        "--keep-target",
        retention,
        "--strip-path", "1",
        "--destroy-incompatible",
        "--clear-mountpoint",
        "--exclude-received",
    ]

    if include_snapshots:
        command_parts.append("--other-snapshots")

    if dry_run:
        print(f"{GREEN}Backup Retention:{RESET} {retention} {GREEN}Command:{RESET} {' '.join(command_parts)}")
        set_backup_property(fs, "dry-run", "No actual backup performed")
        return True

    run = run_subprocess(command_parts)

    if not run:
        set_backup_property(fs, "failed", f"Backup timed out: {' '.join(command_parts)}")
        return False

    print(run.stdout)
    print(run.stderr)

    if run.returncode == 0:
        set_backup_property(fs, "success", "Backup successful")
        return True

    set_backup_property(fs, "failed", f"Backup failed: {run.stderr}")
    return False


def run_sandbox_backup(dry_run, fs, zabselect, retention, include_snapshots):
    """Local snapshot-only backup for sandbox datasets"""
    cmd = [
        "/usr/local/bin/zfs-autobackup",
        zabselect,
        "--verbose",
        "--keep-source",
        retention,
        "--keep-target",
        retention,
        "--strip-path", "1",
        "--destroy-incompatible",
        "--clear-mountpoint",
        "--exclude-received",
    ]

    if include_snapshots:
        cmd.append("--other-snapshots")

    if dry_run:
        print(f"Sandbox dry run: {' '.join(cmd)}")
        set_backup_property(fs, "dry-run", "No actual sandbox backup performed")
        return True

    run = run_subprocess(cmd)

    if run and run.returncode == 0:
        print(run.stdout)
        print(run.stderr)
        set_backup_property(fs, "success", "Sandbox backup successful")
        return True

    if run:
        print(run.stdout)
        print(run.stderr)
        set_backup_property(fs, "failed", f"Sandbox backup failed: {run.stderr}")
    else:
        set_backup_property(fs, "failed", "Sandbox backup failed: timeout")

    return False


def build_backup_jobs(limit, debug):
    result = get_zfs_fs_list() if not limit else limit
    jobs = []

    for fs in result:
        zabprop = "autobackup:" + fs.replace("/", "-").lower()
        zabselect = fs.replace("/", "-").lower()

        backupsfs_run = run_subprocess([
            "zfs", "get", "-s", "local", "-H", "-o", "value", zabprop, fs
        ])

        if not backupsfs_run or backupsfs_run.returncode != 0:
            logging.error(f"Could not read autobackup property {zabprop} on {fs}")
            print(f"{RED}Could not read autobackup property {zabprop} on {fs}{RESET}")
            continue

        backupsfs = backupsfs_run.stdout.strip()

        if "true" not in backupsfs:
            continue

        if debug:
            print(f"Filesystems with autobackup:zab=true: {fs}")

        backupfstype_run = run_subprocess([
            "zfs", "get", "-H", "-o", "value", "zab:backuptype", fs
        ])

        if not backupfstype_run or backupfstype_run.returncode != 0:
            logging.error(f"Could not read zab:backuptype on {fs}")
            print(f"{RED}Could not read zab:backuptype on {fs}{RESET}")
            continue

        backupfstype = backupfstype_run.stdout.strip()

        if backupfstype not in BACKUP_TYPES:
            logging.error(f"Unknown backup type for filesystem: {fs}")
            print(f"{RED}Unknown backup type for filesystem: {fs}{RESET}")
            continue

        if backupfstype == "scratch":
            print(f"{YELLOW}Filesystem backup type is scratch: {RESET}{fs}")
            continue

        if backupfstype == "sandbox":
            retention = BACKUP_TYPES["sandbox"]
            jobs.append({
                "kind": "sandbox",
                "fs": fs,
                "zabselect": zabselect,
                "retention": retention,
            })
            continue

        backupdest_run = run_subprocess([
            "zfs", "get", "-H", "-o", "value", "zab:server", fs
        ])

        if not backupdest_run or backupdest_run.returncode != 0:
            logging.error(f"Could not read zab:server on {fs}")
            print(f"{RED}Could not read zab:server on {fs}{RESET}")
            continue

        backupdest = backupdest_run.stdout.strip()
        backup_servers = backupdest.split(",")

        for server_entry in backup_servers:
            try:
                server, path = server_entry.split(":")
            except ValueError:
                logging.error(f"The zfs attribute zab:server contains an error: {server_entry}")
                print(f"The zfs attribute zab:server contains an error: {server_entry}")
                continue

            path = path.replace("--", "<<HYPHEN>>")
            path = path.replace("-", "/")
            path = path.replace("<<HYPHEN>>", "-")

            retention = BACKUP_TYPES[backupfstype]

            jobs.append({
                "kind": "remote",
                "fs": fs,
                "zabselect": zabselect,
                "server": server,
                "retention": retention,
                "path": path,
            })

    return jobs


def zabwrap(dry_run, orphans, limit, debug, include_snapshots, workers):
    jobs = build_backup_jobs(limit, debug)

    if not jobs:
        print("No backup jobs found.")
        return True

    workers = max(1, min(workers, len(jobs)))

    print(f"{GREEN}Starting {len(jobs)} backup job(s) with {workers} worker(s){RESET}")

    failed = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_job = {}

        for job in jobs:
            if job["kind"] == "sandbox":
                future = executor.submit(
                    run_sandbox_backup,
                    dry_run,
                    job["fs"],
                    job["zabselect"],
                    job["retention"],
                    include_snapshots,
                )
            else:
                future = executor.submit(
                    run_backup,
                    dry_run,
                    job["fs"],
                    job["zabselect"],
                    job["server"],
                    job["retention"],
                    job["path"],
                    include_snapshots,
                )

            future_to_job[future] = job

        for future in as_completed(future_to_job):
            job = future_to_job[future]
            fs = job["fs"]

            try:
                ok = future.result()
                if not ok:
                    failed.append(fs)
            except Exception as exc:
                logging.error(f"Backup job for {fs} generated an exception: {exc}")
                print(f"{RED}Backup job for {fs} generated an exception: {exc}{RESET}")
                failed.append(fs)

    if failed:
        print(f"{RED}Failed backup jobs:{RESET}")
        for fs in sorted(set(failed)):
            print(f"  - {fs}")
        return False

    print(f"{GREEN}All backup jobs completed successfully.{RESET}")
    return True


if __name__ == "__main__":
    acquire_lock()
    exit_code = 0

    try:
        parser = argparse.ArgumentParser(description="ZFS autobackup wrapper")
        parser.add_argument("--dry-run", "-d", action="store_true", help="Print the commands to be run")
        parser.add_argument("--orphans", "-o", action="store_true", help="Print a list of filesystems set to not backup")
        parser.add_argument("--limit", "-l", nargs="+", help="Limit the list of filesystems to process")
        parser.add_argument("--debug", "-v", action="store_true", help="Print debug information")
        parser.add_argument("--include_snapshots", "-s", action="store_true", help="Include all snapshots from the ZFS FS")
        parser.add_argument("--workers", "-w", type=int, default=2, help="Number of filesystem backups to run in parallel")
        args = parser.parse_args()

        ok = zabwrap(
            args.dry_run,
            args.orphans,
            args.limit,
            args.debug,
            args.include_snapshots,
            args.workers,
        )

        if not ok:
            exit_code = 1

    finally:
        release_lock()

    sys.exit(exit_code)
