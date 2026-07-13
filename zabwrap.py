#!/usr/bin/env python3
import subprocess
import argparse
import os
import sys
import logging
import datetime

# Lockfile and logging settings
lockfile_path = "/tmp/zfs_autobackup.lock"
logfile_path = "/var/log/zfs_backup.log"

# Backup settings
# Snapshot retention counts intentionally left unchanged.
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
    format="%(asctime)s %(levelname)s %(message)s",
)


def run_subprocess(cmd, use_sudo=False, timeout=None):
    command = list(cmd)
    if use_sudo:
        command.insert(0, "sudo")

    try:
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logging.error("Command timed out: %s", " ".join(command))
        print(f"{RED}Timeout expired while running: {' '.join(command)}{RESET}")
        return None


def acquire_lock():
    if os.path.exists(lockfile_path):
        logging.error("Another instance of the script is running.")
        print(f"{RED}Another instance of the script is running.{RESET}")
        sys.exit(1)

    with open(lockfile_path, "w", encoding="utf-8") as lock_file:
        lock_file.write(str(os.getpid()))

    logging.info("Lock acquired, no other instances are running.")
    print(f"{GREEN}Lock acquired, no other instances are running.{RESET}")


def release_lock():
    if os.path.exists(lockfile_path):
        os.remove(lockfile_path)
        logging.info("Lock released, script completed.")
        print(f"{GREEN}Lock released, script completed.{RESET}")


def get_zfs_fs_list():
    result = run_subprocess(["zfs", "list", "-Hp", "-o", "name"])
    if result is None or result.returncode != 0:
        error = result.stderr.strip() if result else "zfs list did not run"
        raise RuntimeError(f"Unable to list ZFS filesystems: {error}")

    return {fs: {} for fs in result.stdout.strip().splitlines() if fs}


def send_to_zabbix(host, key, value):
    sanitized_value = value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', "")
    cmd = [
        "sudo",
        "zabbix_sender",
        "-z",
        ZABBIX_SERVER,
        "-s",
        host,
        "--tls-connect",
        "psk",
        "--tls-psk-identity",
        PSK_IDENTITY,
        "--tls-psk-file",
        PSK_FILE,
        "-k",
        key,
        "-o",
        sanitized_value,
    ]
    process = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        print(f"Error sending data to Zabbix: {process.stderr.strip()}")
    else:
        print(f"Data sent to Zabbix: {process.stdout.strip()}")


def set_backup_property(fs, status, message):
    timestamp = datetime.datetime.now().isoformat()
    status_message = f"{status} at {timestamp}: {message}"
    subprocess.run(
        ["zfs", "set", f"zab:lastbackup={status_message}", fs],
        check=False,
    )


def print_process_output(process):
    if process.stdout:
        print(process.stdout, end="" if process.stdout.endswith("\n") else "\n")
    if process.stderr:
        print(
            process.stderr,
            end="" if process.stderr.endswith("\n") else "\n",
            file=sys.stderr,
        )


def execute_zfs_autobackup(command_parts, dry_run, fs, success_message):
    """
    Run zfs-autobackup.

    In dry-run mode, execute the real zfs-autobackup command with --test so
    snapshot creation, transfer, and thinning are planned but no changes are
    made. A test run also does not update zab:lastbackup, preserving the
    read-only nature of the test.
    """
    command = list(command_parts)

    if dry_run:
        command.append("--test")

    mode = "TEST" if dry_run else "RUN"
    print(f"{GREEN}[{mode}] Command:{RESET} {' '.join(command)}")
    logging.info("[%s] Running command: %s", mode, " ".join(command))

    result = run_subprocess(command)
    if result is None:
        if not dry_run:
            set_backup_property(
                fs,
                "failed",
                f"Backup timed out: {' '.join(command)}",
            )
        return False

    print_process_output(result)

    if result.returncode == 0:
        if dry_run:
            logging.info("Test completed successfully for %s", fs)
            print(
                f"{GREEN}Test completed successfully for {fs}; "
                f"no changes were made.{RESET}"
            )
        else:
            set_backup_property(fs, "success", success_message)
        return True

    failure = (
        f"zfs-autobackup exited with status {result.returncode}: "
        f"{result.stderr.strip()}"
    )
    logging.error("Backup failed for %s: %s", fs, failure)

    if not dry_run:
        set_backup_property(fs, "failed", failure)

    return False


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
        "--strip-path",
        "1",
        "--clear-mountpoint",
        "--exclude-received",
    ]

    # Do not include unrelated snapshots unless explicitly requested with -s.
    if include_snapshots:
        command_parts.append("--other-snapshots")

    # --destroy-incompatible is intentionally not used during routine backups.
    execute_zfs_autobackup(
        command_parts,
        dry_run,
        fs,
        "Backup successful",
    )


def run_sandbox_backup(dry_run, fs, zabselect, retention, include_snapshots):
    """Create and thin local snapshots without a target dataset."""
    command_parts = [
        "/usr/local/bin/zfs-autobackup",
        zabselect,
        "--verbose",
        "--keep-source",
        retention,
        "--exclude-received",
    ]

    # This is conditional for consistency. In snapshot-only mode there is no
    # target to receive other snapshots, so the option has no transfer effect.
    if include_snapshots:
        command_parts.append("--other-snapshots")

    # No target-only options are included here. With no target path,
    # zfs-autobackup creates a local snapshot and thins source snapshots.
    execute_zfs_autobackup(
        command_parts,
        dry_run,
        fs,
        "Sandbox snapshot and thinning successful",
    )


def zabwrap(dry_run, orphans, limit, debug, include_snapshots):
    result = get_zfs_fs_list() if not limit else limit

    for fs in result:
        zabprop = "autobackup:" + fs.replace("/", "-").lower()
        zabselect = fs.replace("/", "-").lower()

        backupsfs_result = run_subprocess(
            ["zfs", "get", "-s", "local", "-H", "-o", "value", zabprop, fs]
        )
        if backupsfs_result is None or backupsfs_result.returncode != 0:
            error = (
                backupsfs_result.stderr.strip()
                if backupsfs_result
                else "zfs get did not run"
            )
            logging.error("Unable to read %s from %s: %s", zabprop, fs, error)
            print(f"{RED}Unable to read {zabprop} from {fs}: {error}{RESET}")
            continue

        backupsfs = backupsfs_result.stdout.strip()

        if "true" not in backupsfs.lower():
            if orphans:
                print(fs)
            continue

        if debug:
            print(f"Filesystem selected by {zabprop}=true: {fs}")

        backupfstype_result = run_subprocess(
            ["zfs", "get", "-H", "-o", "value", "zab:backuptype", fs]
        )
        if backupfstype_result is None or backupfstype_result.returncode != 0:
            error = (
                backupfstype_result.stderr.strip()
                if backupfstype_result
                else "zfs get did not run"
            )
            logging.error("Unable to read zab:backuptype from %s: %s", fs, error)
            print(
                f"{RED}Unable to read zab:backuptype from {fs}: "
                f"{error}{RESET}"
            )
            continue

        backupfstype = backupfstype_result.stdout.strip()

        if backupfstype not in BACKUP_TYPES:
            logging.error(
                "Unknown backup type for filesystem %s: %s",
                fs,
                backupfstype,
            )
            print(
                f"{RED}Unknown backup type for filesystem {fs}: "
                f"{backupfstype}{RESET}"
            )
            continue

        if backupfstype == "scratch":
            print(f"{YELLOW}Filesystem backup type is scratch: {RESET}{fs}")
            continue

        retention = BACKUP_TYPES[backupfstype]

        if backupfstype == "sandbox":
            print(f"{YELLOW}Running local-only sandbox snapshots for {fs}{RESET}")
            run_sandbox_backup(
                dry_run,
                fs,
                zabselect,
                retention,
                include_snapshots,
            )
            continue

        backupdest_result = run_subprocess(
            ["zfs", "get", "-H", "-o", "value", "zab:server", fs]
        )
        if backupdest_result is None or backupdest_result.returncode != 0:
            error = (
                backupdest_result.stderr.strip()
                if backupdest_result
                else "zfs get did not run"
            )
            logging.error("Unable to read zab:server from %s: %s", fs, error)
            print(f"{RED}Unable to read zab:server from {fs}: {error}{RESET}")
            continue

        backupdest = backupdest_result.stdout.strip()
        backup_servers = [
            destination.strip()
            for destination in backupdest.split(",")
            if destination.strip()
        ]

        for destination in backup_servers:
            try:
                server, path = destination.split(":", 1)
            except ValueError:
                logging.error(
                    "The zfs attribute zab:server contains an error: %s",
                    destination,
                )
                print(
                    f"{RED}The zfs attribute zab:server contains an error: "
                    f"{destination}{RESET}"
                )
                continue

            path = path.replace("--", "<<HYPHEN>>")
            path = path.replace("-", "/")
            path = path.replace("<<HYPHEN>>", "-")

            run_backup(
                dry_run,
                fs,
                zabselect,
                server,
                retention,
                path,
                include_snapshots,
            )


if __name__ == "__main__":
    acquire_lock()
    try:
        parser = argparse.ArgumentParser(description="ZFS autobackup wrapper")
        parser.add_argument(
            "--dry-run",
            "-d",
            action="store_true",
            help="Run zfs-autobackup with --test and make no changes",
        )
        parser.add_argument(
            "--orphans",
            "-o",
            action="store_true",
            help="Print filesystems that are not selected for backup",
        )
        parser.add_argument(
            "--limit",
            "-l",
            nargs="+",
            help="Limit the list of filesystems to process",
        )
        parser.add_argument(
            "--debug",
            "-v",
            action="store_true",
            help="Print debug information",
        )
        parser.add_argument(
            "--include-snapshots",
            "--include_snapshots",
            "-s",
            dest="include_snapshots",
            action="store_true",
            help="Also transfer snapshots not created by zfs-autobackup",
        )
        args = parser.parse_args()

        zabwrap(
            args.dry_run,
            args.orphans,
            args.limit,
            args.debug,
            args.include_snapshots,
        )
    finally:
        release_lock()
