#!/usr/bin/env python3
"""ZFS autobackup wrapper with layered local configuration support.

Configuration is loaded in this order:

1. Built-in defaults in this file.
2. /etc/zabwrap/zabwrap.conf (normally managed by Ansible).
3. /etc/zabwrap/zabwrap.d/*.conf in lexical order (local overrides).

Later files override earlier files. The configuration locations can be changed
with --config/--config-dir or the ZABWRAP_CONFIG/ZABWRAP_CONFIG_DIR
environment variables.
"""

import argparse
import configparser
import datetime
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence


DEFAULT_CONFIG_FILE = "/etc/zabwrap/zabwrap.conf"
DEFAULT_CONFIG_DIR = "/etc/zabwrap/zabwrap.d"

# Snapshot retention counts intentionally left unchanged.
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

    logging.info("Lock released, script completed.")
    print(f"{GREEN}Lock released, script completed.{RESET}")


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


def run_backup(
    settings: Settings,
    dry_run: bool,
    fs: str,
    zabselect: str,
    server: str,
    retention: str,
    path: str,
) -> bool:
    command_parts = [
        settings.zfs_autobackup,
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
        # Always enabled by design. This preserves and transfers snapshots
        # not created by zfs-autobackup.
        "--other-snapshots",
    ]

    # --destroy-incompatible is intentionally not used during routine backups.
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
    command_parts = [
        settings.zfs_autobackup,
        zabselect,
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
) -> bool:
    filesystems = list(limit) if limit else list(get_zfs_fs_list(settings))
    all_succeeded = True

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
                fs,
                zabselect,
                server,
                retention,
                path,
            ):
                all_succeeded = False

    return all_succeeded


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

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
