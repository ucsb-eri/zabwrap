#!/usr/bin/env python3
import subprocess
import argparse
import re
import os
import sys
import logging
import socket
import datetime

lockfile_path = "/tmp/zabwrap.locl"
logfile_path = "/var/log/zfs_backup.log"

# Backup settings
BACKUP_TYPES = {

    "one": "175,1h5d,1w1y",
    "r2": "652,1h10d,1d1y",
    "r1": "651,1h10d,1d1y",
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
RESET = "\033[0m"
GREEN = "\033[32m"

pattern = r'[A-Z]'

# Configure logging
logging.basicConfig(filename=logfile_path, level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

def to_lowercase(match):
    return match.group(0).lower()

def run_subprocess(cmd, use_sudo=False, *args, **kwargs):
    if use_sudo:
        cmd.insert(0, 'sudo')
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        *args,
        **kwargs,
    )

def acquire_lock(lockfile_path):
    if os.path.exists(lockfile_path):
        logging.error("Another instance of the script is running.")
        print(f"{RED}Another instance of the script is running.{RESET}")
        sys.exit(1)
    else:
        with open(lockfile_path, 'w') as lock_file:
            lock_file.write(str(os.getpid()))
        logging.info("Lock acquired, no other instances are running.")
        print(f"{GREEN}Lock acquired, no other instances are running.{RESET}")

def release_lock(lockfile_path):
    if os.path.exists(lockfile_path):
        os.remove(lockfile_path)
        logging.info("Lock released, script completed.")
        print(f"{GREEN}Lock released, script completed.{RESET}")

def get_zfs_fs_list():
    fslist = run_subprocess(["zfs", "list", "-Hp", "-o", "name"])
    fslist = fslist.stdout
    result = {}

    for line in fslist.split("\n"):
        parts = line.split("\n")
        fs = parts[-1]
        result[fs] = {}

        for i in range(len(parts) - 2, -1, -1):
            result = {parts[i]: result}

    return {k: v for k, v in result.items() if k != ""}

def send_to_zabbix(host, key, value):
    # Ensure the message does not contain newlines and is properly escaped
    sanitized_value = value.replace('\n', '\\n').replace('"', '').replace('\\', '\\\\')
    command = f'sudo zabbix_sender -z {ZABBIX_SERVER} -s "{host}" --tls-connect psk --tls-psk-identity "{PSK_IDENTITY}" --tls-psk-file "{PSK_FILE}" -k {key} -o "{sanitized_value}"'
    print(command)  # Print the command for debugging
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        print(f"Error sending data to Zabbix: {stderr.decode().strip()}")
    else:
        print(f"Data sent to Zabbix: {stdout.decode().strip()}")

def get_last_log_entry():
    with open(logfile_path, 'r') as file:
        lines = file.readlines()
        return lines[-1].strip() if lines else "No log entries found."

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
        "--other-snapshots",
        "--strip-path",
        "1",
        "--destroy-incompatible",

    ]
    if include_snapshots:
        command_parts.append("--other-snapshots")
    if dry_run:
        print(f"{GREEN}Backup Retention:{RESET}{retention} {GREEN}Command:{RESET}{' '.join(command_parts)}")
        set_backup_property(fs, "dry-run", "No actual backup performed")
    else:
        run = run_subprocess(command_parts)
        if run.returncode == 0:
            set_backup_property(fs, "success", "Backup successful")
        else:
            set_backup_property(fs, "failed", f"Backup failed: {run.stderr}")
        print(run.stdout)
        print(run.stderr)

def run_sandbox_backup(dry_run, fs, zabselect, retention):
    command_parts = [
        "/usr/local/bin/zfs-autobackup",
        zabselect,
        "--verbose",
        "--keep-source",
        retention,
    ]

    if dry_run:
        #print(f"{GREEN}Backup Type:{RESET}{zabselect} {GREEN}Command:{RESET}{' '.join(command_parts)}")
        print(f"{GREEN}Backup Retention:{RESET}{retention} {GREEN}Command:{RESET}{' '.join(command_parts)}")
        set_backup_property(fs, "dry-run", "No actual backup performed")
    else:
        run = run_subprocess(command_parts)
        if run.returncode == 0:
            set_backup_property(fs, "success", "Backup successful")
        else:
            set_backup_property(fs, "failed", f"Backup failed: {run.stderr}")
        print(run.stdout)
        print(run.stderr)

def set_backup_property(fs, status, message):
    timestamp = datetime.datetime.now().isoformat()
    status_message = f"{status} at {timestamp}: {message}"
    subprocess.run(["zfs", "set", f"zab:lastbackup={status_message}", fs])

def check_orphans(fs, result):
    zfsautobackup = "false"
    backupfstype = run_subprocess(["zfs", "get", "-H", "-o", "value", "zab:backuptype", fs])
    backupfstype = backupfstype.stdout.strip()

    for j in result:
        j = "autobackup:" + j.replace("/", "-")

        orphanfs = run_subprocess(["zfs", "get", "-H", "-o", "value", j, fs])
        orphanfs = orphanfs.stdout.strip()

        if "true" in orphanfs:
            zfsautobackup = "true"
            break

    if "false" in zfsautobackup:
        if "scratch" in backupfstype:
            print(f"{YELLOW}filesystem backup type is scratch: {RESET}" + fs)
        else:
            logging.error(f"filesystem autobackup:zab not defined: {fs}")
            print(f"{RED}filesystem autobackup:zab not defined: {fs} {RESET}")
    elif "scratch" in backupfstype:
        print(f"{YELLOW}filesystem backup type is scratch: {RESET}" + fs)

def zabwrap(dry_run, orphans, limit, debug, include_snapshots):
    result = get_zfs_fs_list() if not limit else limit
    if orphans:
        for fs in result:
            check_orphans(fs, result)
    else:
        for fs in result:
            zabprop = "autobackup:" + fs.replace("/", "-")
            zabprop = re.sub(pattern, to_lowercase, zabprop)
            zabselect = fs.replace("/", "-")
            zabselect = re.sub(pattern, to_lowercase, zabselect)
            backupsfs = run_subprocess(["zfs", "get", "-s", "local", "-H", "-o", "value", zabprop, fs])
            backupsfs = backupsfs.stdout.strip()

            if "true" in backupsfs:
                if debug: print('filesystems with autobackup:zab=true: '+fs)
                backupfstype = run_subprocess(["zfs", "get", "-H", "-o", "value", "zab:backuptype", fs])
                backupfstype = backupfstype.stdout.strip()

                if backupfstype in BACKUP_TYPES:
                    if backupfstype == "scratch":
                        print(f"{YELLOW}filesystem backup type is scratch: {RESET}" + fs)
                        continue
                    elif backupfstype == "sandbox":
                        retention = BACKUP_TYPES["sandbox"]
                        run_sandbox_backup(dry_run, fs, zabselect, retention)
                    else:
                        backupdest = subprocess.run(
                            ["zfs", "get", "-H", "-o", "value", "zab:server", fs],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            universal_newlines=True,
                        )
                        backupdest = backupdest.stdout.strip("[ ]\n")
                        backupServers = backupdest.split(",")
                        for server in backupServers:
                            try:
                                server, path = server.split(':')
                            except ValueError:
                                logging.error(f'the zfs attribute zab:server contains an error: {server}')
                                print('the zfs attribute zab:server contains an error: '+server)
                            else:
                                path = path.replace("-", "/")
                                include_snapshots = args.include_snapshots
                                retention = BACKUP_TYPES[backupfstype]
                                run_backup(dry_run, fs, zabselect, server, retention, path, include_snapshots)
                else:
                    logging.error(f'Unknown backup type for filesystem: {fs}')
                    print(f'{RED}Unknown backup type for filesystem: {fs}{RESET}')

if __name__ == "__main__":
    lockfile = "/tmp/zfs_autobackup.lock"

    try:
        acquire_lock(lockfile)
        parser = argparse.ArgumentParser(description="ZFS autobackup wrapper")
        parser.add_argument("--dry-run", "-d", action="store_true", help="print the commands to be run")
        parser.add_argument("--orphans", "-o", action="store_true", help="print a list of filesystems set to not backup")
        parser.add_argument("--limit", "-l", nargs="+", help="supply a list of filesystems to run zfs-autobackup on, must be in raid/fs format")
        parser.add_argument("--debug", "-v", action="store_true", help="print filesystems to backup")
        parser.add_argument("--include_snapshots", "-s", action="store_true", help="include all snapshots from the zfs fs")
        args = parser.parse_args()
        zabwrap(args.dry_run, args.orphans, args.limit, args.debug, args.include_snapshots)
    finally:
        release_lock(lockfile)
