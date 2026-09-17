#!/usr/bin/env python3

import argparse
import configparser
import datetime
import logging
import os
import re
import shlex
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple


DEFAULT_CONFIG_FILE = "/etc/zabwrap/zabwrap.conf"
DEFAULT_CONFIG_DIR = "/etc/zabwrap/zabwrap.d"

DEFAULT_BACKUP_TYPES = {
    "one": "175,1h5d,1w1y",
    "r2": "650,1h10d,1d1y",
    "r1": "650,1h10d,1d1y",
    "r0": "0",
    "sandbox": "250,1h10d",
    "raid-sandbox": "10,1h10d",
    "scratch": "",
}

RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RESET = "\033[0m"


@dataclass
class Settings:
    config_file: Path
    config_dir: Path
    loaded_config_files: List[Path]
    lockfile_path: Path
    logfile_path: Path
    zfs_autobackup: str
    zabbix_sender: str
    zabbix_server: str
    psk_identity: str
    psk_file: str
    command_timeout_seconds: Optional[int]
    backup_types: Dict[str, str]


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ZFS autobackup wrapper")
    parser.add_argument(
        "--config",
        default=os.environ.get("ZABWRAP_CONFIG", DEFAULT_CONFIG_FILE),
        help=(
            "Base configuration file "
            f"(default: {DEFAULT_CONFIG_FILE}; env: ZABWRAP_CONFIG)"
        ),
    )
    parser.add_argument(
        "--config-dir",
        default=os.environ.get("ZABWRAP_CONFIG_DIR", DEFAULT_CONFIG_DIR),
        help=(
            "Configuration drop-in directory "
            f"(default: {DEFAULT_CONFIG_DIR}; env: ZABWRAP_CONFIG_DIR)"
        ),
    )
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
        help="Print debug information, including loaded configuration files",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print the effective configuration and exit",
    )
    parser.add_argument(
        "--migrate-legacy",
        action="store_true",
        help=(
            "Move matching legacy target children into the hostname-prefixed "
            "layout and rename unambiguous legacy snapshots"
        ),
    )
    parser.add_argument(
        "--unmount-migration-targets",
        action="store_true",
        help=(
            "Unmount mounted remote backup filesystems when required by "
            "--migrate-legacy; never forces an unmount"
        ),
    )
    return parser


def validate_config_file(path: Path) -> None:
    """Reject configuration files writable by group or other users."""
    try:
        stat_result = path.stat()
    except OSError as exc:
        raise RuntimeError(f"Unable to stat configuration file {path}: {exc}") from exc

    if not path.is_file():
        raise RuntimeError(f"Configuration path is not a regular file: {path}")

    if stat_result.st_mode & 0o022:
        raise RuntimeError(
            f"Unsafe configuration permissions on {path}: "
            "file must not be group- or world-writable"
        )


def get_config_files(config_file: Path, config_dir: Path) -> List[Path]:
    config_files: List[Path] = []

    if config_file.exists():
        validate_config_file(config_file)
        config_files.append(config_file)

    if config_dir.exists():
        if not config_dir.is_dir():
            raise RuntimeError(
                f"Configuration drop-in path is not a directory: {config_dir}"
            )

        for drop_in in sorted(config_dir.glob("*.conf")):
            validate_config_file(drop_in)
            config_files.append(drop_in)

    return config_files


def load_settings(config_file_name: str, config_dir_name: str) -> Settings:
    config_file = Path(config_file_name)
    config_dir = Path(config_dir_name)
    config_files = get_config_files(config_file, config_dir)

    parser = configparser.ConfigParser(
        interpolation=None,
        strict=True,
        empty_lines_in_values=False,
    )

    try:
        loaded_names = parser.read(
            [str(path) for path in config_files],
            encoding="utf-8",
        )
    except (configparser.Error, OSError) as exc:
        raise RuntimeError(f"Unable to load zabwrap configuration: {exc}") from exc

    if len(loaded_names) != len(config_files):
        loaded_set = set(loaded_names)
        failed = [str(path) for path in config_files if str(path) not in loaded_set]
        raise RuntimeError(
            "Unable to read one or more configuration files: " + ", ".join(failed)
        )

    backup_types = dict(DEFAULT_BACKUP_TYPES)
    if parser.has_section("backup_types"):
        for backup_type, retention in parser.items("backup_types"):
            normalized_type = backup_type.strip().lower()
            if not normalized_type:
                raise RuntimeError("Empty backup type name in [backup_types]")
            backup_types[normalized_type] = retention.strip()

    for backup_type, retention in backup_types.items():
        if backup_type != "scratch" and not retention:
            raise RuntimeError(
                f"Backup type {backup_type!r} has an empty retention policy"
            )

    timeout_seconds = parser.getint(
        "runtime",
        "command_timeout_seconds",
        fallback=0,
    )
    if timeout_seconds < 0:
        raise RuntimeError("runtime.command_timeout_seconds cannot be negative")

    settings = Settings(
        config_file=config_file,
        config_dir=config_dir,
        loaded_config_files=[Path(name) for name in loaded_names],
        lockfile_path=Path(
            parser.get(
                "paths",
                "lockfile",
                fallback="/tmp/zfs_autobackup.lock",
            )
        ),
        logfile_path=Path(
            parser.get(
                "paths",
                "logfile",
                fallback="/var/log/zfs_backup.log",
            )
        ),
        zfs_autobackup=parser.get(
            "paths",
            "zfs_autobackup",
            fallback="/usr/local/bin/zfs-autobackup",
        ).strip(),
        zabbix_sender=parser.get(
            "zabbix",
            "sender",
            fallback="zabbix_sender",
        ).strip(),
        zabbix_server=parser.get(
            "zabbix",
            "server",
            fallback="zabbix.grit.ucsb.edu",
        ).strip(),
        psk_identity=parser.get(
            "zabbix",
            "psk_identity",
            fallback="GEOG Linux Servers",
        ).strip(),
        psk_file=parser.get(
            "zabbix",
            "psk_file",
            fallback="/etc/zabbix/zabbix_agent.psk",
        ).strip(),
        command_timeout_seconds=timeout_seconds or None,
        backup_types=backup_types,
    )

    if not settings.zfs_autobackup:
        raise RuntimeError("paths.zfs_autobackup cannot be empty")
    if not settings.zabbix_sender:
        raise RuntimeError("zabbix.sender cannot be empty")

    return settings


def configure_logging(logfile_path: Path) -> None:
    try:
        logging.basicConfig(
            filename=str(logfile_path),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            force=True,
        )
    except OSError as exc:
        raise RuntimeError(f"Unable to configure logging to {logfile_path}: {exc}") from exc


def print_effective_config(settings: Settings) -> None:
    print("Configuration files loaded:")
    if settings.loaded_config_files:
        for path in settings.loaded_config_files:
            print(f"  {path}")
    else:
        print("  none; using built-in defaults")

    print("Effective paths:")
    print(f"  lockfile = {settings.lockfile_path}")
    print(f"  logfile = {settings.logfile_path}")
    print(f"  zfs_autobackup = {settings.zfs_autobackup}")
    print(f"  config = {settings.config_file}")
    print(f"  config_dir = {settings.config_dir}")

    print("Effective Zabbix settings:")
    print(f"  sender = {settings.zabbix_sender}")
    print(f"  server = {settings.zabbix_server}")
    print(f"  psk_identity = {settings.psk_identity}")
    print(f"  psk_file = {settings.psk_file}")

    timeout = settings.command_timeout_seconds or "disabled"
    print(f"Command timeout: {timeout}")
    print("Other snapshots: always enabled")
    print("Destroy incompatible: disabled")

    print("Backup types:")
    for backup_type in sorted(settings.backup_types):
        retention = settings.backup_types[backup_type]
        print(f"  {backup_type} = {retention}")


def run_subprocess(
    cmd: Sequence[str],
    timeout: Optional[int] = None,
) -> Optional[subprocess.CompletedProcess]:
    command = list(cmd)
    try:
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logging.error("Command timed out: %s", " ".join(command))
        print(
            f"{RED}Timeout expired while running: {' '.join(command)}{RESET}",
            file=sys.stderr,
        )
        return None
    except OSError as exc:
        logging.error("Unable to execute command %s: %s", " ".join(command), exc)
        print(
            f"{RED}Unable to execute {' '.join(command)}: {exc}{RESET}",
            file=sys.stderr,
        )
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock(lockfile_path: Path) -> None:
    """Create a PID lock, removing it only when it is demonstrably stale."""
    lockfile_path.parent.mkdir(parents=True, exist_ok=True)

    for _attempt in range(2):
        try:
            descriptor = os.open(
                str(lockfile_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            existing_pid: Optional[int] = None
            try:
                contents = lockfile_path.read_text(encoding="utf-8").strip()
                existing_pid = int(contents)
            except (OSError, ValueError):
                pass

            if existing_pid is not None and process_is_running(existing_pid):
                logging.error(
                    "Another instance of the script is running with PID %s.",
                    existing_pid,
                )
                print(
                    f"{RED}Another instance of the script is running "
                    f"with PID {existing_pid}.{RESET}",
                    file=sys.stderr,
                )
                raise SystemExit(1)

            try:
                lockfile_path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise RuntimeError(
                    f"Unable to remove stale lockfile {lockfile_path}: {exc}"
                ) from exc
            continue
        except OSError as exc:
            raise RuntimeError(
                f"Unable to create lockfile {lockfile_path}: {exc}"
            ) from exc

        with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
            lock_file.write(str(os.getpid()))
            lock_file.flush()
            os.fsync(lock_file.fileno())

        logging.info("Lock acquired, no other instances are running.")
        print(f"{GREEN}Lock acquired, no other instances are running.{RESET}")
        return

    raise RuntimeError(f"Unable to acquire lockfile {lockfile_path}")


def release_lock(lockfile_path: Path) -> None:
    try:
        contents = lockfile_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    except OSError as exc:
        logging.error("Unable to read lockfile during release: %s", exc)
        return

    if contents != str(os.getpid()):
        logging.error(
            "Refusing to remove lockfile %s because it belongs to PID %s",
            lockfile_path,
            contents or "unknown",
        )
        return

    try:
        lockfile_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logging.error("Unable to remove lockfile %s: %s", lockfile_path, exc)
        print(
            f"{RED}Unable to remove lockfile {lockfile_path}: {exc}{RESET}",
            file=sys.stderr,
        )
        return

    logging.info("Lock released.")
    print(f"{GREEN}Lock released.{RESET}")


def get_zfs_fs_list(settings: Settings) -> Dict[str, Dict[str, str]]:
    result = run_subprocess(
        ["zfs", "list", "-Hp", "-o", "name"],
        timeout=settings.command_timeout_seconds,
    )
    if result is None or result.returncode != 0:
        error = result.stderr.strip() if result else "zfs list did not run"
        raise RuntimeError(f"Unable to list ZFS filesystems: {error}")

    return {fs: {} for fs in result.stdout.strip().splitlines() if fs}


def send_to_zabbix(settings: Settings, host: str, key: str, value: str) -> bool:
    sanitized_value = (
        value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', "")
    )
    command = [
        "sudo",
        settings.zabbix_sender,
        "-z",
        settings.zabbix_server,
        "-s",
        host,
        "--tls-connect",
        "psk",
        "--tls-psk-identity",
        settings.psk_identity,
        "--tls-psk-file",
        settings.psk_file,
        "-k",
        key,
        "-o",
        sanitized_value,
    ]
    process = run_subprocess(command, timeout=settings.command_timeout_seconds)
    if process is None:
        return False
    if process.returncode != 0:
        print(f"Error sending data to Zabbix: {process.stderr.strip()}")
        return False

    print(f"Data sent to Zabbix: {process.stdout.strip()}")
    return True


def set_backup_property(
    settings: Settings,
    fs: str,
    status: str,
    message: str,
) -> None:
    timestamp = datetime.datetime.now().isoformat()
    status_message = f"{status} at {timestamp}: {message}"
    result = run_subprocess(
        ["zfs", "set", f"zab:lastbackup={status_message}", fs],
        timeout=settings.command_timeout_seconds,
    )
    if result is None or result.returncode != 0:
        error = result.stderr.strip() if result else "zfs set did not run"
        logging.error("Unable to set zab:lastbackup on %s: %s", fs, error)


def print_process_output(process: subprocess.CompletedProcess) -> None:
    if process.stdout:
        print(process.stdout, end="" if process.stdout.endswith("\n") else "\n")
    if process.stderr:
        print(
            process.stderr,
            end="" if process.stderr.endswith("\n") else "\n",
            file=sys.stderr,
        )


def execute_zfs_autobackup(
    settings: Settings,
    command_parts: Sequence[str],
    dry_run: bool,
    fs: str,
    success_message: str,
) -> bool:
    """Run zfs-autobackup, adding --test for a read-only dry run."""
    command = list(command_parts)
    if dry_run:
        command.append("--test")

    mode = "TEST" if dry_run else "RUN"
    print(f"{GREEN}[{mode}] Command:{RESET} {' '.join(command)}")
    logging.info("[%s] Running command: %s", mode, " ".join(command))

    result = run_subprocess(command, timeout=settings.command_timeout_seconds)
    if result is None:
        if not dry_run:
            set_backup_property(
                settings,
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
            set_backup_property(settings, fs, "success", success_message)
        return True

    failure = (
        f"zfs-autobackup exited with status {result.returncode}: "
        f"{result.stderr.strip()}"
    )
    logging.error("Backup failed for %s: %s", fs, failure)
    if not dry_run:
        set_backup_property(settings, fs, "failed", failure)
    return False


def get_source_hostname() -> str:
    """Return a stable hostname component for target paths and snapshot names."""
    source_hostname = socket.gethostname().split(".", 1)[0].strip().lower()
    if not source_hostname:
        raise RuntimeError("Unable to determine the source server hostname")
    return source_hostname


def get_snapshot_format(zabselect: str) -> str:
    """Build a host- and selection-specific snapshot naming format."""
    return f"{get_source_hostname()}-{zabselect}-%Y%m%d%H%M%S"


def ensure_remote_target(
    settings: Settings,
    server: str,
    target_path: str,
    dry_run: bool,
    prepared_targets: Set[Tuple[str, str]],
) -> bool:
    """Ensure the hostname-specific target root exists on the remote server."""
    target_key = (server, target_path)
    if target_key in prepared_targets:
        return True

    quoted_path = shlex.quote(target_path)
    check_command = [
        "ssh",
        server,
        f"zfs list -H -o name {quoted_path} >/dev/null 2>&1",
    ]
    logging.info(
        "Checking remote target: server=%s path=%s",
        server,
        target_path,
    )
    check_result = run_subprocess(
        check_command,
        timeout=settings.command_timeout_seconds,
    )

    if check_result is not None and check_result.returncode == 0:
        prepared_targets.add(target_key)
        return True

    if dry_run:
        error = (
            check_result.stderr.strip()
            if check_result is not None and check_result.stderr.strip()
            else "dataset does not exist or could not be accessed"
        )
        message = (
            f"Remote target {target_path} is not available on {server}: {error}. "
            "A live run would attempt to create it."
        )
        logging.error(message)
        print(f"{RED}{message}{RESET}", file=sys.stderr)
        return False

    print(
        f"{YELLOW}Remote target {server}:{target_path} does not exist; "
        f"creating it.{RESET}"
    )
    logging.info(
        "Creating missing remote target: server=%s path=%s",
        server,
        target_path,
    )
    create_command = [
        "ssh",
        server,
        (
            f"zfs create -p -o canmount=off -o mountpoint=none "
            f"{quoted_path}"
        ),
    ]
    create_result = run_subprocess(
        create_command,
        timeout=settings.command_timeout_seconds,
    )
    if create_result is not None and create_result.returncode == 0:
        prepared_targets.add(target_key)
        if create_result.stdout.strip():
            print(create_result.stdout.strip())
        return True

    error = (
        create_result.stderr.strip()
        if create_result is not None and create_result.stderr.strip()
        else "remote zfs create did not run successfully"
    )
    message = (
        f"Unable to create remote target {target_path} on {server}: {error}"
    )

    # A concurrent wrapper might have created the dataset after our check.
    recheck_result = run_subprocess(
        check_command,
        timeout=settings.command_timeout_seconds,
    )
    if recheck_result is not None and recheck_result.returncode == 0:
        prepared_targets.add(target_key)
        logging.info(
            "Remote target appeared after create attempt: server=%s path=%s",
            server,
            target_path,
        )
        return True

    logging.error(message)
    print(f"{RED}{message}{RESET}", file=sys.stderr)
    return False


def list_zfs_names(
    settings: Settings,
    root: str,
    object_type: str,
    server: Optional[str] = None,
) -> Optional[List[str]]:
    """List datasets or snapshots locally or through the target SSH host."""
    zfs_command = [
        "zfs",
        "list",
        "-H",
        "-t",
        object_type,
        "-o",
        "name",
        "-r",
        root,
    ]
    command = zfs_command
    if server:
        command = [
            "ssh",
            server,
            " ".join(shlex.quote(part) for part in zfs_command),
        ]

    result = run_subprocess(command, timeout=settings.command_timeout_seconds)
    if result is None or result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def remote_dataset_exists(
    settings: Settings,
    server: str,
    dataset: str,
) -> bool:
    quoted_dataset = shlex.quote(dataset)
    result = run_subprocess(
        [
            "ssh",
            server,
            f"zfs list -H -o name {quoted_dataset} >/dev/null 2>&1",
        ],
        timeout=settings.command_timeout_seconds,
    )
    return result is not None and result.returncode == 0


def list_remote_mounted_filesystems(
    settings: Settings,
    server: str,
    root: str,
) -> Optional[List[Tuple[str, str]]]:
    """Return mounted filesystems at or below a remote dataset root.

    None means the remote ZFS query itself failed.  Volumes are deliberately
    excluded because they do not have filesystem mount state.
    """
    zfs_command = [
        "zfs",
        "list",
        "-H",
        "-r",
        "-t",
        "filesystem",
        "-o",
        "name,mounted,mountpoint",
        root,
    ]
    result = run_subprocess(
        [
            "ssh",
            server,
            " ".join(shlex.quote(part) for part in zfs_command),
        ],
        timeout=settings.command_timeout_seconds,
    )
    if result is None or result.returncode != 0:
        error = result.stderr.strip() if result else "remote zfs list did not run"
        print(
            f"{RED}Unable to check mount state below {server}:{root}: "
            f"{error}{RESET}",
            file=sys.stderr,
        )
        return None

    mounted: List[Tuple[str, str]] = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            print(
                f"{RED}Unexpected mount-state output from {server}: "
                f"{line!r}{RESET}",
                file=sys.stderr,
            )
            return None
        name, is_mounted, mountpoint = fields
        if is_mounted == "yes":
            mounted.append((name, mountpoint))

    return mounted


def migration_mount_preflight(
    settings: Settings,
    server: str,
    migrations: Sequence[Tuple[str, str]],
    dry_run: bool,
    unmount_migration_targets: bool,
) -> bool:
    """Verify migration trees are unmounted, optionally unmounting them."""
    conflicts: List[Tuple[str, str]] = []
    for old_child, _new_child in migrations:
        mounted = list_remote_mounted_filesystems(settings, server, old_child)
        if mounted is None:
            return False
        conflicts.extend(mounted)

    if not conflicts:
        return True

    # A child and one of its descendants can both be mounted.  Unmount the
    # deepest datasets first, and de-duplicate entries defensively.
    conflicts = sorted(
        set(conflicts),
        key=lambda item: (item[0].count("/"), item[0]),
        reverse=True,
    )

    if unmount_migration_targets:
        mode = "TEST" if dry_run else "UNMOUNT"
        for dataset, mountpoint in conflicts:
            print(
                f"{YELLOW}[{mode}] {server}:{dataset} "
                f"mounted at {mountpoint}{RESET}"
            )
            if dry_run:
                continue

            zfs_command = ["zfs", "unmount", dataset]
            result = run_subprocess(
                [
                    "ssh",
                    server,
                    " ".join(shlex.quote(part) for part in zfs_command),
                ],
                timeout=settings.command_timeout_seconds,
            )
            if result is None or result.returncode != 0:
                error = (
                    result.stderr.strip()
                    if result is not None and result.stderr.strip()
                    else "remote zfs unmount did not run successfully"
                )
                print(
                    f"{RED}Unable to unmount {server}:{dataset}: "
                    f"{error}{RESET}",
                    file=sys.stderr,
                )
                return False

        if dry_run:
            print(
                f"{GREEN}Dry run: the listed filesystems would be "
                f"unmounted before migration.{RESET}"
            )
            return True

        # Do not trust a successful command alone; verify every migration tree
        # before creating parents or renaming datasets.
        for old_child, _new_child in migrations:
            remaining = list_remote_mounted_filesystems(
                settings,
                server,
                old_child,
            )
            if remaining is None:
                return False
            if remaining:
                print(
                    f"{RED}Mount verification failed below "
                    f"{server}:{old_child}:{RESET}",
                    file=sys.stderr,
                )
                for dataset, mountpoint in remaining:
                    print(
                        f"  {dataset} mounted at {mountpoint}",
                        file=sys.stderr,
                    )
                return False
        return True

    print(
        f"{RED}Migration cannot proceed because target filesystems on "
        f"{server} are mounted:{RESET}",
        file=sys.stderr,
    )
    for dataset, mountpoint in conflicts:
        print(f"  {dataset} mounted at {mountpoint}", file=sys.stderr)
    print(
        "Unmount the listed backup filesystems and rerun the migration. "
        "Alternatively, add --unmount-migration-targets. "
        "No migration changes were made.",
        file=sys.stderr,
    )
    return False


def relative_dataset_names(names: Sequence[str], root: str) -> Set[str]:
    """Convert a recursive dataset list to paths relative to its root."""
    relative: Set[str] = set()
    prefix = root + "/"
    for name in names:
        if name == root:
            relative.add("")
        elif name.startswith(prefix):
            relative.add(name[len(prefix):])
    return relative


def rename_legacy_snapshots(
    settings: Settings,
    root: str,
    zabselect: str,
    source_hostname: str,
    dry_run: bool,
    server: Optional[str] = None,
) -> bool:
    """Rename legacy snapshots under root without changing their GUIDs."""
    snapshots = list_zfs_names(settings, root, "snapshot", server=server)
    if snapshots is None:
        location = f" on {server}" if server else " locally"
        print(
            f"{RED}Unable to list snapshots below {root}{location}.{RESET}",
            file=sys.stderr,
        )
        return False

    legacy_pattern = re.compile(
        rf"^{re.escape(zabselect)}-(\d{{14}})$"
    )
    existing = set(snapshots)

    for snapshot in snapshots:
        try:
            dataset, snapshot_name = snapshot.rsplit("@", 1)
        except ValueError:
            continue

        match = legacy_pattern.fullmatch(snapshot_name)
        if not match:
            continue

        renamed = (
            f"{dataset}@{source_hostname}-{zabselect}-{match.group(1)}"
        )
        if renamed in existing:
            print(
                f"{RED}Cannot migrate snapshot because both names exist: "
                f"{snapshot} and {renamed}{RESET}",
                file=sys.stderr,
            )
            return False

        location = f" on {server}" if server else ""
        print(f"{YELLOW}[MIGRATE] {snapshot} -> {renamed}{location}{RESET}")
        if dry_run:
            continue

        zfs_command = ["zfs", "rename", snapshot, renamed]
        command = zfs_command
        if server:
            command = [
                "ssh",
                server,
                " ".join(shlex.quote(part) for part in zfs_command),
            ]
        result = run_subprocess(
            command,
            timeout=settings.command_timeout_seconds,
        )
        if result is None or result.returncode != 0:
            error = result.stderr.strip() if result else "zfs rename did not run"
            print(
                f"{RED}Unable to rename {snapshot}: {error}{RESET}",
                file=sys.stderr,
            )
            return False
        existing.discard(snapshot)
        existing.add(renamed)

    return True


def migrate_legacy_backup(
    settings: Settings,
    dry_run: bool,
    unmount_migration_targets: bool,
    fs: str,
    zabselect: str,
    server: str,
    path: str,
    target_path: str,
) -> bool:
    """Move unambiguous legacy children into the hostname-prefixed tree.

    The legacy root may be shared by backups from more than one source host.
    Consequently, the root itself is never renamed.  A top-level child is
    migrated only when it exists on both source and target and its complete
    descendant layout matches.  Target-only children remain in place.
    """
    source_hostname = get_source_hostname()
    source_parts = fs.split("/", 1)
    if len(source_parts) != 2 or not source_parts[1]:
        print(
            f"{RED}Legacy migration of a whole pool is not supported: {fs}{RESET}",
            file=sys.stderr,
        )
        return False

    stripped_source = source_parts[1]
    legacy_root = f"{path.rstrip('/')}/{stripped_source}"
    new_root = f"{target_path.rstrip('/')}/{stripped_source}"
    legacy_exists = remote_dataset_exists(settings, server, legacy_root)
    new_exists = remote_dataset_exists(settings, server, new_root)

    target_snapshot_roots: List[str] = []
    if legacy_exists:
        source_datasets = list_zfs_names(settings, fs, "filesystem,volume")
        target_datasets = list_zfs_names(
            settings,
            legacy_root,
            "filesystem,volume",
            server=server,
        )
        if source_datasets is None or target_datasets is None:
            print(
                f"{RED}Unable to compare source and legacy target trees for "
                f"{fs}.{RESET}",
                file=sys.stderr,
            )
            return False

        source_relative = relative_dataset_names(source_datasets, fs) - {""}
        target_relative = relative_dataset_names(target_datasets, legacy_root) - {""}
        source_children = {name.split("/", 1)[0] for name in source_relative}
        target_children = {name.split("/", 1)[0] for name in target_relative}
        matching_children = sorted(source_children & target_children)

        def child_tree(names: Set[str], child: str) -> Set[str]:
            return {
                "" if name == child else name[len(child) + 1:]
                for name in names
                if name == child or name.startswith(child + "/")
            }

        # Validate every candidate and destination before changing anything.
        migrations: List[Tuple[str, str]] = []
        for child in matching_children:
            source_tree = child_tree(source_relative, child)
            target_tree = child_tree(target_relative, child)
            if source_tree != target_tree:
                only_source = sorted(source_tree - target_tree)
                only_target = sorted(target_tree - source_tree)
                print(
                    f"{RED}Refusing to migrate mismatched child "
                    f"{server}:{legacy_root}/{child}.{RESET}",
                    file=sys.stderr,
                )
                if only_source:
                    print(
                        f"  Only on source: {', '.join(only_source)}",
                        file=sys.stderr,
                    )
                if only_target:
                    print(
                        f"  Only on target: {', '.join(only_target)}",
                        file=sys.stderr,
                    )
                return False

            old_child = f"{legacy_root}/{child}"
            new_child = f"{new_root}/{child}"
            if remote_dataset_exists(settings, server, new_child):
                print(
                    f"{RED}Cannot migrate child because both "
                    f"{server}:{old_child} and {server}:{new_child} exist."
                    f"{RESET}",
                    file=sys.stderr,
                )
                return False
            migrations.append((old_child, new_child))

        target_only = sorted(target_children - source_children)
        if target_only:
            print(
                f"{YELLOW}Leaving target-only children under "
                f"{server}:{legacy_root}: {', '.join(target_only)}{RESET}"
            )

        source_only = sorted(source_children - target_children)
        if source_only:
            print(
                f"{YELLOW}No legacy target child to migrate for: "
                f"{', '.join(source_only)}{RESET}"
            )

        if migrations:
            # A ZFS filesystem rename may need to unmount the filesystem and
            # its descendants.  Never let migration implicitly disturb a
            # mounted backup tree; detect every conflict before making the
            # first change so the operation does not become partially applied.
            if not migration_mount_preflight(
                settings,
                server,
                migrations,
                dry_run,
                unmount_migration_targets,
            ):
                return False

            if not dry_run:
                parent_result = run_subprocess(
                    [
                        "ssh",
                        server,
                        (
                            f"zfs list -H -o name {shlex.quote(new_root)} "
                            f">/dev/null 2>&1 || zfs create -p "
                            f"-o canmount=off -o mountpoint=none "
                            f"{shlex.quote(new_root)}"
                        ),
                    ],
                    timeout=settings.command_timeout_seconds,
                )
                if parent_result is None or parent_result.returncode != 0:
                    error = (
                        parent_result.stderr.strip()
                        if parent_result
                        else "remote parent creation did not run"
                    )
                    print(f"{RED}{error}{RESET}", file=sys.stderr)
                    return False

            for old_child, new_child in migrations:
                print(
                    f"{YELLOW}[MIGRATE] {server}:{old_child} -> "
                    f"{server}:{new_child}{RESET}"
                )
                if not dry_run:
                    # Close the gap between the plan-wide preflight and this
                    # rename in case something mounted the tree meanwhile.
                    mounted = list_remote_mounted_filesystems(
                        settings,
                        server,
                        old_child,
                    )
                    if mounted is None:
                        return False
                    if mounted:
                        if not migration_mount_preflight(
                            settings,
                            server,
                            [(old_child, new_child)],
                            False,
                            unmount_migration_targets,
                        ):
                            return False

                    rename_result = run_subprocess(
                        [
                            "ssh",
                            server,
                            (
                                f"zfs rename {shlex.quote(old_child)} "
                                f"{shlex.quote(new_child)}"
                            ),
                        ],
                        timeout=settings.command_timeout_seconds,
                    )
                    if rename_result is None or rename_result.returncode != 0:
                        error = (
                            rename_result.stderr.strip()
                            if rename_result
                            else "remote dataset rename did not run"
                        )
                        print(f"{RED}{error}{RESET}", file=sys.stderr)
                        return False
                target_snapshot_roots.append(
                    old_child if dry_run else new_child
                )

        if not migrations and not new_exists:
            print(
                f"{YELLOW}No matching legacy target children found for "
                f"{fs} on {server}.{RESET}"
            )
    elif new_exists:
        target_snapshot_roots.append(new_root)
    else:
        print(
            f"{YELLOW}No legacy target tree found for {fs} on {server}; "
            f"only local legacy snapshot names will be checked.{RESET}"
        )

    if not rename_legacy_snapshots(
        settings,
        fs,
        zabselect,
        source_hostname,
        dry_run,
    ):
        return False

    # Never rename snapshots on the shared legacy root.  Only operate below
    # children assigned to this host, or below its existing hostname tree.
    if new_exists and new_root not in target_snapshot_roots:
        target_snapshot_roots.append(new_root)
    for target_snapshot_root in target_snapshot_roots:
        if not rename_legacy_snapshots(
            settings,
            target_snapshot_root,
            zabselect,
            source_hostname,
            dry_run,
            server=server,
        ):
            return False

    return True


def run_backup(
    settings: Settings,
    dry_run: bool,
    unmount_migration_targets: bool,
    fs: str,
    zabselect: str,
    server: str,
    retention: str,
    path: str,
    prepared_targets: Set[Tuple[str, str]],
    migrate_legacy: bool,
) -> bool:
    # Keep backups from different source servers in distinct dataset trees.
    # Use the short hostname so an FQDN change does not alter the backup path.
    source_hostname = get_source_hostname()
    target_path = f"{path.rstrip('/')}/{source_hostname}"
    snapshot_format = get_snapshot_format(zabselect)

    if migrate_legacy and not migrate_legacy_backup(
        settings,
        dry_run,
        unmount_migration_targets,
        fs,
        zabselect,
        server,
        path,
        target_path,
    ):
        return False

    if not ensure_remote_target(
        settings,
        server,
        target_path,
        dry_run,
        prepared_targets,
    ):
        if not dry_run:
            set_backup_property(
                settings,
                fs,
                "failed",
                f"Unable to prepare remote target {server}:{target_path}",
            )
        return False

    command_parts = [
        settings.zfs_autobackup,
        zabselect,
        target_path,
        "--strip-path",
        "1",
        "--snapshot-format",
        snapshot_format,
        "--verbose",
        "--keep-source",
        retention,
        "--ssh-target",
        server,
        "--keep-target",
        retention,
        "--filter-properties",
        "mountpoint",
        "--set-properties",
        "canmount=off",
        "--exclude-received",
    ]

    return execute_zfs_autobackup(
        settings,
        command_parts,
        dry_run,
        fs,
        "Backup successful",
    )


def run_sandbox_backup(
    settings: Settings,
    dry_run: bool,
    fs: str,
    zabselect: str,
    retention: str,
) -> bool:
    """Create and thin local snapshots without a target dataset."""
    snapshot_format = get_snapshot_format(zabselect)
    command_parts = [
        settings.zfs_autobackup,
        zabselect,
        "--snapshot-format",
        snapshot_format,
        "--verbose",
        "--keep-source",
        retention,
        "--exclude-received",
        # Kept enabled consistently with normal backups.
        "--other-snapshots",
    ]

    # No target-only options are included here. With no target path,
    # zfs-autobackup creates a local snapshot and thins source snapshots.
    return execute_zfs_autobackup(
        settings,
        command_parts,
        dry_run,
        fs,
        "Sandbox snapshot and thinning successful",
    )


def read_zfs_property(
    settings: Settings,
    fs: str,
    property_name: str,
    local_only: bool = False,
) -> Optional[str]:
    command = ["zfs", "get"]
    if local_only:
        command.extend(["-s", "local"])
    command.extend(["-H", "-o", "value", property_name, fs])

    result = run_subprocess(command, timeout=settings.command_timeout_seconds)
    if result is None or result.returncode != 0:
        error = result.stderr.strip() if result else "zfs get did not run"
        logging.error(
            "Unable to read %s from %s: %s",
            property_name,
            fs,
            error,
        )
        print(
            f"{RED}Unable to read {property_name} from {fs}: {error}{RESET}",
            file=sys.stderr,
        )
        return None

    return result.stdout.strip()


def decode_backup_path(encoded_path: str) -> str:
    placeholder = "<<HYPHEN>>"
    return (
        encoded_path.replace("--", placeholder)
        .replace("-", "/")
        .replace(placeholder, "-")
    )


def zabwrap(
    settings: Settings,
    dry_run: bool,
    orphans: bool,
    limit: Optional[Sequence[str]],
    debug: bool,
    migrate_legacy: bool,
    unmount_migration_targets: bool,
) -> bool:
    filesystems = list(limit) if limit else list(get_zfs_fs_list(settings))
    all_succeeded = True
    prepared_targets: Set[Tuple[str, str]] = set()

    for fs in filesystems:
        zabprop = "autobackup:" + fs.replace("/", "-").lower()
        zabselect = fs.replace("/", "-").lower()

        backupsfs = read_zfs_property(
            settings,
            fs,
            zabprop,
            local_only=True,
        )
        if backupsfs is None:
            all_succeeded = False
            continue

        if backupsfs.lower() != "true":
            if orphans:
                print(fs)
            continue

        if debug:
            print(f"Filesystem selected by {zabprop}=true: {fs}")

        backupfstype = read_zfs_property(
            settings,
            fs,
            "zab:backuptype",
        )
        if backupfstype is None:
            all_succeeded = False
            continue

        backupfstype = backupfstype.lower()
        if backupfstype not in settings.backup_types:
            logging.error(
                "Unknown backup type for filesystem %s: %s",
                fs,
                backupfstype,
            )
            print(
                f"{RED}Unknown backup type for filesystem {fs}: "
                f"{backupfstype}{RESET}",
                file=sys.stderr,
            )
            all_succeeded = False
            continue

        if backupfstype == "scratch":
            print(f"{YELLOW}Filesystem backup type is scratch: {RESET}{fs}")
            continue

        retention = settings.backup_types[backupfstype]

        if backupfstype == "sandbox":
            print(f"{YELLOW}Running local-only sandbox snapshots for {fs}{RESET}")
            if not run_sandbox_backup(
                settings,
                dry_run,
                fs,
                zabselect,
                retention,
            ):
                all_succeeded = False
            continue

        backupdest = read_zfs_property(settings, fs, "zab:server")
        if backupdest is None:
            all_succeeded = False
            continue

        backup_servers = [
            destination.strip()
            for destination in backupdest.split(",")
            if destination.strip()
        ]
        if not backup_servers:
            logging.error("No backup destinations configured for %s", fs)
            print(
                f"{RED}No backup destinations configured for {fs}.{RESET}",
                file=sys.stderr,
            )
            all_succeeded = False
            continue

        for destination in backup_servers:
            try:
                server, encoded_path = destination.split(":", 1)
            except ValueError:
                logging.error(
                    "The zfs attribute zab:server contains an error: %s",
                    destination,
                )
                print(
                    f"{RED}The zfs attribute zab:server contains an error: "
                    f"{destination}{RESET}",
                    file=sys.stderr,
                )
                all_succeeded = False
                continue

            server = server.strip()
            encoded_path = encoded_path.strip()
            if not server or not encoded_path:
                logging.error(
                    "The zfs attribute zab:server contains an incomplete "
                    "destination: %s",
                    destination,
                )
                print(
                    f"{RED}The zfs attribute zab:server contains an incomplete "
                    f"destination: {destination}{RESET}",
                    file=sys.stderr,
                )
                all_succeeded = False
                continue

            path = decode_backup_path(encoded_path)
            if not run_backup(
                settings,
                dry_run,
                unmount_migration_targets,
                fs,
                zabselect,
                server,
                retention,
                path,
                prepared_targets,
                migrate_legacy,
            ):
                all_succeeded = False

    return all_succeeded


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

    if args.unmount_migration_targets and not args.migrate_legacy:
        parser.error(
            "--unmount-migration-targets requires --migrate-legacy"
        )

    try:
        settings = load_settings(args.config, args.config_dir)
    except (RuntimeError, ValueError) as exc:
        print(f"{RED}Configuration error: {exc}{RESET}", file=sys.stderr)
        return 2

    if args.print_config:
        print_effective_config(settings)
        return 0

    try:
        configure_logging(settings.logfile_path)
    except RuntimeError as exc:
        print(f"{RED}{exc}{RESET}", file=sys.stderr)
        return 2

    if args.debug:
        print_effective_config(settings)

    lock_acquired = False
    try:
        acquire_lock(settings.lockfile_path)
        lock_acquired = True
        succeeded = zabwrap(
            settings,
            args.dry_run,
            args.orphans,
            args.limit,
            args.debug,
            args.migrate_legacy,
            args.unmount_migration_targets,
        )
        return 0 if succeeded else 1
    except RuntimeError as exc:
        logging.exception("Fatal zabwrap error")
        print(f"{RED}{exc}{RESET}", file=sys.stderr)
        return 1
    finally:
        if lock_acquired:
            release_lock(settings.lockfile_path)


if __name__ == "__main__":
    sys.exit(main())
