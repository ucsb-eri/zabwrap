#!/usr/bin/env python3
import subprocess
import argparse
import re
import os
import sys

lockfile_path = "/tmp/zabwrap.locl"

# Backup settings
# custom retention periods can be set with "sandbox-<unique>": "###,#h#d,#d#y"
BACKUP_TYPES = {
    "one": "175,1h5d,1w1y",
    "r2": "650,1h10d,1d1y",
    "r1": "650,1h10d,1d1y",
    "r0": "0",
    "sandbox": "250,1h10d",
    "raid-sandbox": "10,1h10d",
    "scratch": "",
}

# ANSI color codes
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"
GREEN = "\033[32m"

pattern = r'[A-Z]'


def to_lowercase(match):
    return match.group(0).lower()


def run_subprocess(cmd, *args, **kwargs):
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
        print(f"{RED}Another instance of the script is running.{RESET}")
        sys.exit(1)
    else:
        # Create a lock file to signify that the script is running
        with open(lockfile_path, 'w') as lock_file:
            lock_file.write(str(os.getpid()))
        print(f"{GREEN}Lock acquired, no other instances are running.{RESET}")

def release_lock(lockfile_path):
    if os.path.exists(lockfile_path):
        os.remove(lockfile_path)
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
    ]
    if include_snapshots:
        command_parts.append("--other-snapshots")
    if dry_run:
        print(f"{GREEN}Backup Retention:{RESET}{retention} {GREEN}Command:{RESET}{' '.join(command_parts)}")
    else:
        run = run_subprocess(command_parts)
        print(run.stdout)
        print(run.stderr)


def run_sandbox_backup(dry_run, fs, zabselect, retention):
    command_parts = [
        "/usr/local/bin/zfs-autobackup",
        zabselect,
        fs,
        "--verbose",
        "--keep-source",
        retention,
    ]

    if dry_run:
        print(f"{GREEN}Backup Type:{RESET}{zabselect} {GREEN}Command:{RESET}{' '.join(command_parts)}")
    else:
        run = run_subprocess(command_parts)
        print(run.stdout)
        print(run.stderr)


    if dry_run:
        print(f"{GREEN}Backup Type:{RESET}{zabselect} {GREEN}Command:{RESET}{' '.join(command_parts)}")
    else:
        run = run_subprocess(command_parts)
        print(run.stdout)
        print(run.stderr)


def check_orphans(fs, result):
    zfsautobackup = "false"
    backupfstype = run_subprocess(["zfs", "get", "-H", "-o", "value", "zab:backuptype", fs])
    backupfstype = backupfstype.stdout

    for j in result:
        j = "autobackup:" + j.replace("/", "-")

        orphanfs = run_subprocess(["zfs", "get", "-H", "-o", "value", j, fs])
        orphanfs = orphanfs.stdout

        if "true" in orphanfs:
            zfsautobackup = "true"
            break

    if "false" in zfsautobackup:
        if "scratch" in backupfstype:
            print(f"{YELLOW}filesystem backup type is scratch: {RESET}" + fs)
        else:
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
            backupsfs = backupsfs.stdout

            if "true" in backupsfs:  # is this part of the zab backup group? if yes check what type it is
                if debug: print('filesystems with autobackup:zab=true: '+fs)
                backupfstype = run_subprocess(["zfs", "get", "-H", "-o", "value", "zab:backuptype", fs])
                backupfstype = backupfstype.stdout

                for types in BACKUP_TYPES:
                    if "scratch" in backupfstype:  # check if its scratch and if it is ignore it
                        break
                    elif "sandbox" in backupfstype:
                        if backupfstype == "sandbox":
                            run_sandbox_backup(dry_run, fs, zabselect, BACKUP_TYPES[types])
                            break
                        else:
                            run_sandbox_backup(dry_run, fs, zabselect, BACKUP_TYPES[types])
                            break
                    elif types in backupfstype:  # check what server(s) this fs should be backed up to
                        backupdest = subprocess.run(
                            ["zfs", "get", "-H", "-o", "value", "zab:server", fs],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            universal_newlines=True,
                        )
                        backupdest = backupdest.stdout.strip("[ ]\n")
                        backupServers = backupdest.split(",")
                        for server in backupServers:  # generate a command for each backup destination defined with the correct backup retention
                            try:
                                server, path = server.split(':')
                            except ValueError:
                                print('the zfs attribute zab:server contains an error: '+server)
                            else:
                                path = path.replace("-", "/")
                                include_snapshots = args.include_snapshots
                                run_backup(dry_run, fs, zabselect, server, BACKUP_TYPES[types], path, include_snapshots)


if __name__ == "__main__":
    lockfile = "/tmp/zfs_autobackup.lock"  # Define the path to the lock file

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
