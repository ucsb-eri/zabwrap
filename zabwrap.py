#!/usr/bin/env python3
import subprocess
import argparse
import re
import os
import sys
import logging
import socket
import datetime

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
logging.basicConfig(filename=logfile_path, level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

def run_subprocess(cmd, use_sudo=False, timeout=300):
    if use_sudo:
        cmd.insert(0, 'sudo')
    try:
        return subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        logging.error(f"Command timed out: {' '.join(cmd)}")
        print(f"{RED}Timeout expired while running: {' '.join(cmd)}{RESET}")
        return None

def acquire_lock():
    if os.path.exists(lockfile_path):
        logging.error("Another instance of the script is running.")
        print(f"{RED}Another instance of the script is running.{RESET}")
        sys.exit(1)
    else:
        with open(lockfile_path, 'w') as lock_file:
            lock_file.write(str(os.getpid()))
        logging.info("Lock acquired, no other instances are running.")
        print(f"{GREEN}Lock acquired, no other instances are running.{RESET}")

def release_lock():
    if os.path.exists(lockfile_path):
        os.remove(lockfile_path)
        logging.info("Lock released, script completed.")
        print(f"{GREEN}Lock released, script completed.{RESET}")

def get_zfs_fs_list():
    fslist = run_subprocess(["zfs", "list", "-Hp", "-o", "name"]).stdout.strip().split("\n")
    return {fs: {} for fs in fslist if fs}

def send_to_zabbix(host, key, value):
    sanitized_value = value.replace('\n', '\\n').replace('"', '').replace('\\', '\\\\')
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
        "--other-snapshots",
        "--destroy-incompatible",
        "--clear-mountpoint",  # Ensure received dataset does not retain mountpoint
        "--exclude-received", # Ensure received dataset is ignored on shared ZAB systems
    ]
    if include_snapshots:
        command_parts.append("--other-snapshots")

    if dry_run:
        print(f"{GREEN}Backup Retention:{RESET} {retention} {GREEN}Command:{RESET} {' '.join(command_parts)}")
        set_backup_property(fs, "dry-run", "No actual backup performed")
    else:
        run = run_subprocess(command_parts)
        if run.returncode == 0:
            set_backup_property(fs, "success", "Backup successful")
        else:
            set_backup_property(fs, "failed", f"Backup failed: {run.stderr}")
        print(run.stdout)
        print(run.stderr)

def zabwrap(dry_run, orphans, limit, debug, include_snapshots):
    result = get_zfs_fs_list() if not limit else limit
    for fs in result:
        zabprop = "autobackup:" + fs.replace("/", "-").lower()
        zabselect = fs.replace("/", "-").lower()
        backupsfs = run_subprocess(["zfs", "get", "-s", "local", "-H", "-o", "value", zabprop, fs]).stdout.strip()

        if "true" in backupsfs:
            if debug:
                print(f'Filesystems with autobackup:zab=true: {fs}')
            backupfstype = run_subprocess(["zfs", "get", "-H", "-o", "value", "zab:backuptype", fs]).stdout.strip()

            if backupfstype in BACKUP_TYPES:
                if backupfstype == "scratch":
                    print(f"{YELLOW}Filesystem backup type is scratch: {RESET}{fs}")
                    continue
                elif backupfstype == "sandbox":
                    retention = BACKUP_TYPES["sandbox"]
                    run_backup(dry_run, fs, zabselect, "", retention, "", include_snapshots)
                else:
                    backupdest = run_subprocess(["zfs", "get", "-H", "-o", "value", "zab:server", fs]).stdout.strip()
                    backupServers = backupdest.split(",")
                    for server in backupServers:
                        try:
                            server, path = server.split(':')
                        except ValueError:
                            logging.error(f'The zfs attribute zab:server contains an error: {server}')
                            print(f'The zfs attribute zab:server contains an error: {server}')
                        else:
                            path = path.replace("--", "<<HYPHEN>>")
                            path = path.replace("-", "/")
                            path = path.replace("<<HYPHEN>>", "-")
                            retention = BACKUP_TYPES[backupfstype]
                            run_backup(dry_run, fs, zabselect, server, retention, path, include_snapshots)
            else:
                logging.error(f'Unknown backup type for filesystem: {fs}')
                print(f'{RED}Unknown backup type for filesystem: {fs}{RESET}')

if __name__ == "__main__":
    acquire_lock()
    try:
        parser = argparse.ArgumentParser(description="ZFS autobackup wrapper")
        parser.add_argument("--dry-run", "-d", action="store_true", help="Print the commands to be run")
        parser.add_argument("--orphans", "-o", action="store_true", help="Print a list of filesystems set to not backup")
        parser.add_argument("--limit", "-l", nargs="+", help="Limit the list of filesystems to process")
        parser.add_argument("--debug", "-v", action="store_true", help="Print debug information")
        parser.add_argument("--include_snapshots", "-s", action="store_true", help="Include all snapshots from the ZFS FS")
        args = parser.parse_args()

        zabwrap(args.dry_run, args.orphans, args.limit, args.debug, args.include_snapshots)
    finally:
        release_lock()
