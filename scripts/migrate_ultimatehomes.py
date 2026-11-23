#!/usr/bin/env python3
"""
Migrate UltimateHomes player homes to HuskHomes.

This tool is intentionally plan-first:

    scan   -> read UltimateHomes + nLogin + the HuskHomes database, write a plan
    apply  -> validate the plan/source files, then write it atomically
    verify -> re-read HuskHomes and prove every planned home exists correctly

It supports:
  * UltimateHomes <uuid>.yml playerdata files
  * nLogin MySQL/MariaDB (and CSV exports for offline testing)
  * HuskHomes SQLite (normal Paper setup) and MySQL/MariaDB
  * explicit UUID overrides for hard cases

It never deletes existing homes. Existing HuskHomes homes that conflict with a
migrated home are reported and, by default, stop the migration.
"""

from __future__ import annotations

import argparse
import base64
import csv
import getpass
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import uuid
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

try:
    import yaml
except ImportError:  # pragma: no cover - handled with a friendly message in main()
    yaml = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLAN_FORMAT = "ultimatehomes-to-huskhomes-plan"
PLAN_VERSION = 1
TOOL_VERSION = "1.0.0"

HUSKHOMES_MAX_NAME_LENGTH = 16
DEFAULT_HOME_DELIMITER = "."
DEFAULT_NAME_REGEX = r"[a-zA-Z0-9-_]*"
DEFAULT_SERVER_NAME = "server"

# Deterministic UUID namespaces. These are part of the migration's identity:
# changing them would cause re-runs to create duplicate homes.
HOME_UUID_NAMESPACE = uuid.UUID("e12193e2-1783-5132-a234-e6cd7f03edc8")
WORLD_UUID_NAMESPACE = uuid.UUID("4e7d5148-7275-50d1-8ac1-006965e26a72")

DEFAULT_TABLES = {
    "META_DATA": "huskhomes_metadata",
    "PLAYER_DATA": "huskhomes_users",
    "PLAYER_COOLDOWNS_DATA": "huskhomes_user_cooldowns",
    "POSITION_DATA": "huskhomes_position_data",
    "SAVED_POSITION_DATA": "huskhomes_saved_positions",
    "HOME_DATA": "huskhomes_homes",
    "WARP_DATA": "huskhomes_warps",
    "TELEPORT_DATA": "huskhomes_teleports",
}

REQUIRED_TABLE_KEYS = (
    "META_DATA",
    "PLAYER_DATA",
    "POSITION_DATA",
    "SAVED_POSITION_DATA",
    "HOME_DATA",
)

REQUIRED_COLUMNS = {
    "META_DATA": {"schema_version"},
    "PLAYER_DATA": {"uuid", "username"},
    "POSITION_DATA": {"id", "x", "y", "z", "yaw", "pitch", "world_name", "world_uuid", "server_name"},
    "SAVED_POSITION_DATA": {"id", "position_id", "name", "description", "tags", "timestamp"},
    "HOME_DATA": {"uuid", "saved_position_id", "owner_uuid", "public"},
}


# ---------------------------------------------------------------------------
# Errors, logging, small utilities
# ---------------------------------------------------------------------------


class MigrationError(Exception):
    """A fatal migration error with a user-facing message."""


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def log(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr, flush=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sql_timestamp_now() -> str:
    """UTC timestamp in the format HuskHomes' JDBC driver writes to a DATETIME."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_uuid(value: Any) -> Optional[uuid.UUID]:
    """Accept a UUID object, dashed/undashed UUID, or bytes. Return None if invalid."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, (bytes, bytearray)):
        if len(value) == 16:
            return uuid.UUID(bytes=bytes(value))
        value = value.decode("ascii", "strict")
    text = str(value).strip()
    if not text:
        return None
    text = text.lower()
    if re.fullmatch(r"[0-9a-f]{32}", text):
        return uuid.UUID(hex=text)
    try:
        return uuid.UUID(text)
    except (ValueError, AttributeError):
        return None


def offline_uuid(name: str) -> uuid.UUID:
    """The UUID a vanilla offline-mode server derives from a player name."""
    digest = hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest()
    return uuid.UUID(bytes=digest, version=3)


def normalize_username(value: Any, fallback_uuid: uuid.UUID) -> tuple[str, list[str]]:
    """Return a HuskHomes-safe username (max 16 chars) and optional warnings."""
    warnings: list[str] = []
    if value is None:
        name = ""
    else:
        name = str(value).strip()
    if not name:
        name = f"unknown_{fallback_uuid.hex[:8]}"
        warnings.append(f"source has no username; using placeholder {name!r}")
    if len(name) > HUSKHOMES_MAX_NAME_LENGTH:
        shortened = name[:HUSKHOMES_MAX_NAME_LENGTH]
        warnings.append(f"username {name!r} is longer than 16 chars; storing {shortened!r}")
        name = shortened
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name) or f"unknown_{fallback_uuid.hex[:8]}"
        warnings.append(f"username {name!r} contains unusual characters; storing {cleaned!r}")
        name = cleaned
    return name, warnings


def to_float(value: Any, label: str) -> float:
    if isinstance(value, bool) or value is None:
        raise MigrationError(f"{label} is not a number: {value!r}")
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        try:
            result = float(str(value).strip())
        except (TypeError, ValueError):
            raise MigrationError(f"{label} is not a number: {value!r}") from None
    if not math.isfinite(result):
        raise MigrationError(f"{label} is not finite: {value!r}")
    return result


def floats_equal(a: float, b: float, tolerance: float = 1e-6) -> bool:
    return abs(float(a) - float(b)) <= tolerance


def float32_value(value: float) -> float:
    try:
        return struct.unpack("f", struct.pack("f", float(value)))[0]
    except OverflowError:
        return float(value)


def float32_equal(actual: float, expected: float) -> bool:
    # HuskHomes stores yaw and pitch in 32-bit FLOAT columns on MySQL/MariaDB,
    # and MariaDB/MySQL text protocol formats FLOAT values to 6 significant
    # digits (FLT_DIG=6, max relative rounding error <= 5e-6).
    target = float32_value(expected)
    tolerance = max(1e-5, abs(target) * 6e-6)
    return abs(float(actual) - target) <= tolerance


def base36(number: int, width: int = 4) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    number = abs(int(number))
    chars: list[str] = []
    while number:
        number, rem = divmod(number, 36)
        chars.append(alphabet[rem])
    text = "".join(reversed(chars)) or "0"
    return text.rjust(width, "0")[-width:]


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=False) + "\n")


def sql_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        raise MigrationError(f"Unsafe SQL identifier in HuskHomes config: {name!r}")
    return f"`{name}`"


def format_issue(issue: Mapping[str, Any]) -> str:
    bits = []
    if issue.get("source_file"):
        bits.append(str(issue["source_file"]))
    if issue.get("home_name") is not None:
        bits.append(f"home={issue['home_name']!r}")
    prefix = f"[{issue.get('code', 'issue')}] "
    location = f" ({', '.join(bits)})" if bits else ""
    return prefix + str(issue.get("message", "")) + location


# ---------------------------------------------------------------------------
# YAML loading with duplicate-key detection
# ---------------------------------------------------------------------------


if yaml is not None:

    class UniqueKeyLoader(yaml.SafeLoader):
        """SafeLoader that refuses silently-lost duplicate map keys."""

    def _construct_unique_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
        mapping: dict = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise MigrationError(
                    f"duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}"
                )
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    UniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        _construct_unique_mapping,
    )


def load_yaml(path: Path) -> Any:
    if yaml is None:
        raise MigrationError("PyYAML is required. Install with: python3 -m pip install -r requirements.txt")
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            return yaml.load(f, Loader=UniqueKeyLoader)
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError(f"failed to read YAML {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Source data model
# ---------------------------------------------------------------------------


@dataclass
class SourceHome:
    raw_name: str
    x: float
    y: float
    z: float
    yaw: float
    pitch: float
    world: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class SourcePlayer:
    source_uuid: uuid.UUID
    file: Path
    relative_file: str
    sha256: str
    last_seen_as: Optional[str]
    homes: list[SourceHome] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class NLoginAccount:
    name: Optional[str]
    unique_id: Optional[uuid.UUID]
    mojang_id: Optional[uuid.UUID]
    bedrock_id: Optional[str]
    canonical_uuid: uuid.UUID


@dataclass
class OwnerResolution:
    uuid: uuid.UUID
    username: str
    method: str
    source_uuid: uuid.UUID
    source_name: Optional[str]
    account: Optional[NLoginAccount]
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# UltimateHomes parsing
# ---------------------------------------------------------------------------


def parse_ultimatehomes_playerdata(directory: Path) -> tuple[list[SourcePlayer], list[dict[str, Any]]]:
    """Parse every <uuid>.yml file. Returns players (including empty ones) and global issues."""
    if not directory.is_dir():
        raise MigrationError(f"UltimateHomes playerdata directory not found: {directory}")

    players: list[SourcePlayer] = []
    issues: list[dict[str, Any]] = []

    files = sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == ".yml")
    if not files:
        raise MigrationError(f"no .yml playerdata files found in {directory}")

    for path in files:
        relative = path.relative_to(directory).as_posix()
        try:
            source_uuid = uuid.UUID(path.stem)
        except ValueError:
            issues.append({
                "level": "error",
                "code": "invalid_player_filename",
                "message": f"filename is not a UUID: {path.name}",
                "source_file": relative,
            })
            continue

        digest = sha256_file(path)
        try:
            data = load_yaml(path)
        except MigrationError as exc:
            issues.append({
                "level": "error",
                "code": "invalid_yaml",
                "message": str(exc),
                "source_file": relative,
                "source_uuid": str(source_uuid),
            })
            continue

        if data is None:
            data = {}
        if not isinstance(data, dict):
            issues.append({
                "level": "error",
                "code": "invalid_structure",
                "message": f"top-level YAML is {type(data).__name__}, expected a mapping",
                "source_file": relative,
                "source_uuid": str(source_uuid),
            })
            continue

        last_seen_as: Optional[str] = None
        player_info = data.get("player")
        if isinstance(player_info, dict):
            info = player_info.get("info")
            if isinstance(info, dict):
                raw_name = info.get("lastSeenAs")
                if raw_name is not None and str(raw_name).strip():
                    last_seen_as = str(raw_name).strip()

        player = SourcePlayer(
            source_uuid=source_uuid,
            file=path,
            relative_file=relative,
            sha256=digest,
            last_seen_as=last_seen_as,
        )

        homes_data = data.get("homes")
        if homes_data is None:
            homes_data = {}
        if not isinstance(homes_data, dict):
            issue = {
                "level": "error",
                "code": "invalid_homes_section",
                "message": f"'homes' is {type(homes_data).__name__}, expected a mapping",
                "source_file": relative,
                "source_uuid": str(source_uuid),
            }
            player.issues.append(issue)
            issues.append(issue)
            players.append(player)
            continue

        seen_raw_names: set[str] = set()
        for raw_key, value in homes_data.items():
            raw_name = str(raw_key)
            if raw_name in seen_raw_names:
                issue = {
                    "level": "error",
                    "code": "duplicate_home_key",
                    "message": f"duplicate home key {raw_name!r}",
                    "source_file": relative,
                    "source_uuid": str(source_uuid),
                    "home_name": raw_name,
                }
                player.issues.append(issue)
                issues.append(issue)
                continue
            seen_raw_names.add(raw_name)

            if not raw_name.strip():
                issue = {
                    "level": "error",
                    "code": "empty_home_name",
                    "message": "home name is empty",
                    "source_file": relative,
                    "source_uuid": str(source_uuid),
                    "home_name": raw_name,
                }
                player.issues.append(issue)
                issues.append(issue)
                continue

            warnings: list[str] = []
            home_value = value

            # Older/hand-edited UltimateHomes files occasionally contain an
            # extra nested mapping: homes.<name>.<something>.{x,y,z,...}.
            # Salvage it when the shape is unambiguous instead of dropping it.
            if isinstance(home_value, dict) and not all(
                key in home_value for key in ("x", "y", "z", "world")
            ):
                nested = [
                    (key, sub) for key, sub in home_value.items()
                    if isinstance(sub, dict) and all(k in sub for k in ("x", "y", "z", "world"))
                ]
                if len(nested) == 1:
                    nested_key, home_value = nested[0]
                    warnings.append(
                        f"recovered nested home data (unexpected child key {nested_key!r})"
                    )
                else:
                    issue = {
                        "level": "error",
                        "code": "invalid_home",
                        "message": "home is missing x/y/z/world and could not be recovered",
                        "source_file": relative,
                        "source_uuid": str(source_uuid),
                        "home_name": raw_name,
                    }
                    player.issues.append(issue)
                    issues.append(issue)
                    continue

            if not isinstance(home_value, dict):
                issue = {
                    "level": "error",
                    "code": "invalid_home",
                    "message": f"home value is {type(home_value).__name__}, expected a mapping",
                    "source_file": relative,
                    "source_uuid": str(source_uuid),
                    "home_name": raw_name,
                }
                player.issues.append(issue)
                issues.append(issue)
                continue

            try:
                for required in ("x", "y", "z", "yaw", "pitch", "world"):
                    if required not in home_value:
                        raise MigrationError(f"missing field {required!r}")
                world = home_value.get("world")
                if not isinstance(world, str) or not world.strip():
                    raise MigrationError(f"invalid world {world!r}")
                home = SourceHome(
                    raw_name=raw_name,
                    x=to_float(home_value["x"], f"home {raw_name!r} x"),
                    y=to_float(home_value["y"], f"home {raw_name!r} y"),
                    z=to_float(home_value["z"], f"home {raw_name!r} z"),
                    yaw=to_float(home_value["yaw"], f"home {raw_name!r} yaw"),
                    pitch=to_float(home_value["pitch"], f"home {raw_name!r} pitch"),
                    world=world.strip(),
                    warnings=warnings,
                )
            except MigrationError as exc:
                issue = {
                    "level": "error",
                    "code": "invalid_home",
                    "message": str(exc),
                    "source_file": relative,
                    "source_uuid": str(source_uuid),
                    "home_name": raw_name,
                }
                player.issues.append(issue)
                issues.append(issue)
                continue

            unknown = sorted(
                set(home_value.keys()) - {"x", "y", "z", "yaw", "pitch", "world"}
            )
            if unknown:
                home.warnings.append(f"ignored unknown home fields: {', '.join(map(str, unknown))}")
            player.homes.append(home)

        players.append(player)

    return players, issues


# ---------------------------------------------------------------------------
# nLogin loading and UUID resolution
# ---------------------------------------------------------------------------


class NLoginIndex:
    def __init__(self) -> None:
        self.accounts: dict[uuid.UUID, NLoginAccount] = {}
        self.by_alias: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        self.by_name: dict[str, set[uuid.UUID]] = defaultdict(set)
        self.issues: list[dict[str, Any]] = []

    def add(self, account: NLoginAccount) -> None:
        if account.canonical_uuid in self.accounts:
            existing = self.accounts[account.canonical_uuid]
            self.issues.append({
                "level": "warning",
                "code": "nlogin_duplicate_uuid",
                "message": (
                    f"nLogin UUID {account.canonical_uuid} belongs to both "
                    f"{existing.name!r} and {account.name!r}; keeping the first"
                ),
            })
            return
        self.accounts[account.canonical_uuid] = account
        aliases = {account.canonical_uuid}
        if account.unique_id:
            aliases.add(account.unique_id)
        if account.mojang_id:
            aliases.add(account.mojang_id)
        if account.name:
            aliases.add(offline_uuid(account.name))
        for alias in aliases:
            self.by_alias[alias].add(account.canonical_uuid)
        if account.name:
            self.by_name[account.name.casefold()].add(account.canonical_uuid)

    @property
    def row_count(self) -> int:
        return len(self.accounts)


def nlogin_account_from_row(row: Mapping[str, Any]) -> tuple[Optional[NLoginAccount], list[str]]:
    warnings: list[str] = []
    raw_name = row.get("last_name")
    name = str(raw_name).strip() if raw_name is not None else ""
    name = name or None
    unique_id = parse_uuid(row.get("unique_id"))
    mojang_id = parse_uuid(row.get("mojang_id"))
    bedrock_raw = row.get("bedrock_id")
    bedrock_id = str(bedrock_raw).strip() if bedrock_raw not in (None, "") else None

    if unique_id is not None:
        canonical = unique_id
    elif name:
        canonical = offline_uuid(name)
    elif mojang_id is not None:
        canonical = mojang_id
        warnings.append("nLogin row has no name and no unique_id; using mojang_id")
    else:
        return None, ["nLogin row has neither a name nor a usable UUID"]

    return NLoginAccount(
        name=name,
        unique_id=unique_id,
        mojang_id=mojang_id,
        bedrock_id=bedrock_id,
        canonical_uuid=canonical,
    ), warnings


def build_nlogin_index(rows: Iterable[Mapping[str, Any]]) -> NLoginIndex:
    index = NLoginIndex()
    for row in rows:
        account, warnings = nlogin_account_from_row(row)
        for message in warnings:
            index.issues.append({
                "level": "warning",
                "code": "nlogin_row",
                "message": message,
            })
        if account is not None:
            index.add(account)
    return index


def normalize_nlogin_row(row: Mapping[str, Any]) -> dict[str, str]:
    raw_name = row.get("last_name")
    return {
        "name": str(raw_name).strip() if raw_name is not None else "",
        "unique_id": str(parse_uuid(row.get("unique_id")) or ""),
        "mojang_id": str(parse_uuid(row.get("mojang_id")) or ""),
        "bedrock_id": str(row.get("bedrock_id") or "").strip(),
    }


def nlogin_snapshot(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    normalized = [normalize_nlogin_row(row) for row in rows]
    normalized.sort(
        key=lambda r: (r["name"].casefold(), r["unique_id"], r["mojang_id"], r["bedrock_id"])
    )
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"sha256": sha256_text(payload), "row_count": len(normalized)}


def load_nlogin_csv(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise MigrationError(f"nLogin CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        if not reader.fieldnames:
            raise MigrationError(f"nLogin CSV has no header: {path}")
        fields = {name.strip().casefold(): name for name in reader.fieldnames if name}
        name_key = fields.get("last_name") or fields.get("name") or fields.get("username")
        unique_key = fields.get("unique_id") or fields.get("uuid")
        mojang_key = fields.get("mojang_id") or fields.get("mojang_uuid")
        if not name_key:
            raise MigrationError("nLogin CSV needs a last_name/name/username column")
        rows: list[dict[str, Any]] = []
        for record in reader:
            rows.append({
                "last_name": record.get(name_key),
                "unique_id": record.get(unique_key) if unique_key else None,
                "mojang_id": record.get(mojang_key) if mojang_key else None,
                "bedrock_id": None,
            })
        return rows


def resolve_mysql_password(args: argparse.Namespace, prefix: str) -> str:
    from_cli = getattr(args, f"_{prefix}_password_from_cli", False)
    direct = getattr(args, f"{prefix}_password", None)
    if from_cli and direct:
        warn(f"passing the {prefix} database password on the command line is visible in process listings")
        return direct
    config_pw = getattr(args, f"{prefix}_config_password", None)
    if config_pw:
        return str(config_pw)
    env_name = getattr(args, f"{prefix}_password_env", None)
    if env_name and os.environ.get(env_name) is not None:
        return os.environ[env_name]
    if direct:
        if not from_cli:
            return str(direct)
        warn(f"passing the {prefix} database password on the command line is visible in process listings")
        return direct
    if sys.stdin.isatty():
        return getpass.getpass(f"{prefix} database password (leave blank if none): ")
    return ""


def load_nlogin_mysql(args: argparse.Namespace) -> list[dict[str, Any]]:
    try:
        import pymysql  # type: ignore
    except ImportError as exc:
        raise MigrationError(
            "PyMySQL is required for nLogin MySQL. Install with: "
            "python3 -m pip install -r requirements.txt"
        ) from exc

    table = args.nlogin_table
    name_col = args.nlogin_name_column
    unique_col = args.nlogin_unique_id_column
    mojang_col = args.nlogin_mojang_id_column
    bedrock_col = getattr(args, "nlogin_bedrock_id_column", "bedrock_id")
    for value in (table, name_col, unique_col, mojang_col, bedrock_col):
        if not re.fullmatch(r"[A-Za-z0-9_]+", value or ""):
            raise MigrationError(f"invalid nLogin SQL identifier: {value!r}")

    password = resolve_mysql_password(args, "nlogin")
    try:
        connection = pymysql.connect(
            host=args.nlogin_mysql_host,
            port=args.nlogin_mysql_port,
            user=args.nlogin_mysql_user,
            password=password,
            database=args.nlogin_mysql_database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
            connect_timeout=15,
            read_timeout=120,
            write_timeout=30,
        )
    except Exception as exc:
        raise MigrationError(f"could not connect to nLogin MySQL: {exc}") from exc

    try:
        with connection.cursor() as cursor:
            # Confirm the configured columns really exist before querying.
            cursor.execute(
                """
                SELECT COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                """,
                (args.nlogin_mysql_database, table),
            )
            columns = {row["COLUMN_NAME"].lower() for row in cursor.fetchall()}
            required = {name_col.lower(), unique_col.lower(), mojang_col.lower()}
            missing = required - columns
            if missing:
                raise MigrationError(
                    f"nLogin table {table!r} is missing expected column(s): {', '.join(sorted(missing))}"
                )

            select_bedrock = (
                f", {sql_ident(bedrock_col)} AS `bedrock_id`"
                if bedrock_col.lower() in columns
                else ", NULL AS `bedrock_id`"
            )
            cursor.execute(
                f"SELECT {sql_ident(name_col)} AS `last_name`, "
                f"{sql_ident(unique_col)} AS `unique_id`, "
                f"{sql_ident(mojang_col)} AS `mojang_id` "
                f"{select_bedrock} "
                f"FROM {sql_ident(table)}"
            )
            rows = list(cursor.fetchall())
    finally:
        connection.rollback()
        connection.close()

    if not rows:
        raise MigrationError("nLogin MySQL query returned no rows")
    return rows


def nlogin_source_from_args(args: argparse.Namespace) -> dict[str, Any]:
    if args.nlogin_csv:
        return {"kind": "csv", "path": str(Path(args.nlogin_csv).resolve())}
    return {
        "kind": "mysql",
        "host": args.nlogin_mysql_host,
        "port": int(args.nlogin_mysql_port),
        "database": args.nlogin_mysql_database,
        "user": args.nlogin_mysql_user,
        "table": args.nlogin_table,
        "name_column": args.nlogin_name_column,
        "unique_id_column": args.nlogin_unique_id_column,
        "mojang_id_column": args.nlogin_mojang_id_column,
        "bedrock_id_column": args.nlogin_bedrock_id_column,
    }


def load_nlogin_rows_for_plan(
    plan: Mapping[str, Any],
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    info = (plan.get("nlogin") or {}).get("source")
    if not isinstance(info, dict):
        raise MigrationError("plan has no nLogin source information; re-run scan with this tool version")
    kind = info.get("kind")
    if kind == "csv":
        return load_nlogin_csv(Path(str(info.get("path") or "")))
    if kind != "mysql":
        raise MigrationError(f"unknown nLogin source kind in plan: {kind!r}")

    namespace = argparse.Namespace(
        nlogin_mysql_host=info.get("host"),
        nlogin_mysql_port=int(info.get("port") or 3306),
        nlogin_mysql_database=info.get("database"),
        nlogin_mysql_user=info.get("user"),
        nlogin_table=info.get("table") or "nlogin",
        nlogin_name_column=info.get("name_column") or "last_name",
        nlogin_unique_id_column=info.get("unique_id_column") or "unique_id",
        nlogin_mojang_id_column=info.get("mojang_id_column") or "mojang_id",
        nlogin_bedrock_id_column=info.get("bedrock_id_column") or "bedrock_id",
        nlogin_password_env=getattr(args, "nlogin_password_env", "NLOGIN_DB_PASSWORD"),
        nlogin_password=getattr(args, "nlogin_password", None),
        nlogin_config_password=getattr(args, "nlogin_config_password", None),
        _nlogin_password_from_cli=getattr(args, "_nlogin_password_from_cli", False),
    )
    if not namespace.nlogin_mysql_host or not namespace.nlogin_mysql_database or not namespace.nlogin_mysql_user:
        raise MigrationError("plan's nLogin MySQL connection details are incomplete")
    return load_nlogin_mysql(namespace)


def verify_nlogin_snapshot(plan: Mapping[str, Any], args: argparse.Namespace) -> None:
    if getattr(args, "skip_nlogin_verify", False):
        warn("skipping nLogin snapshot verification (--skip-nlogin-verify)")
        return
    info = plan.get("nlogin") or {}
    expected = info.get("snapshot")
    if not isinstance(expected, dict) or not expected.get("sha256"):
        raise MigrationError("plan has no nLogin snapshot; re-run scan with this tool version")
    rows = load_nlogin_rows_for_plan(plan, args)
    actual = nlogin_snapshot(rows)
    if actual.get("sha256") != expected.get("sha256") or actual.get("row_count") != expected.get("row_count"):
        raise MigrationError(
            "nLogin account data changed after the scan "
            f"(planned {expected.get('row_count')} rows/{str(expected.get('sha256'))[:12]}…, "
            f"now {actual.get('row_count')} rows/{str(actual.get('sha256'))[:12]}…). "
            "Re-run scan, or pass --skip-nlogin-verify only if you are certain the change "
            "does not affect player UUIDs."
        )


@dataclass
class ManualOverride:
    target_uuid: uuid.UUID
    username: Optional[str]


def load_uuid_overrides(path: Optional[Path]) -> dict[uuid.UUID, ManualOverride]:
    if path is None:
        return {}
    if not path.is_file():
        raise MigrationError(f"UUID override CSV not found: {path}")

    overrides: dict[uuid.UUID, ManualOverride] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise MigrationError("UUID override CSV has no header")
        fields = {name.strip().casefold(): name for name in reader.fieldnames if name}
        source_key = fields.get("source_uuid") or fields.get("ultimatehomes_uuid")
        target_key = fields.get("target_uuid") or fields.get("huskhomes_uuid")
        user_key = fields.get("username") or fields.get("name")
        if not source_key or not target_key:
            raise MigrationError("UUID override CSV needs source_uuid,target_uuid[,username] columns")

        for line_number, record in enumerate(reader, start=2):
            source = parse_uuid(record.get(source_key))
            target = parse_uuid(record.get(target_key))
            if source is None or target is None:
                raise MigrationError(f"invalid UUID on line {line_number} of {path}")
            username = record.get(user_key) if user_key else None
            username = str(username).strip() if username not in (None, "") else None
            if source in overrides:
                raise MigrationError(f"duplicate source UUID {source} in {path}")
            overrides[source] = ManualOverride(target_uuid=target, username=username)
    return overrides


def resolve_owner(
    source_uuid: uuid.UUID,
    source_name: Optional[str],
    nlogin: NLoginIndex,
    overrides: Mapping[uuid.UUID, ManualOverride],
) -> OwnerResolution:
    warnings: list[str] = []

    if source_uuid in overrides:
        override = overrides[source_uuid]
        username, username_warnings = normalize_username(
            override.username or source_name, override.target_uuid
        )
        warnings.extend(username_warnings)
        return OwnerResolution(
            uuid=override.target_uuid,
            username=username,
            method="manual_override",
            source_uuid=source_uuid,
            source_name=source_name,
            account=nlogin.accounts.get(override.target_uuid),
            warnings=warnings,
        )

    account: Optional[NLoginAccount] = None
    method = "source_uuid_unmatched"

    alias_candidates = nlogin.by_alias.get(source_uuid, set())
    if len(alias_candidates) == 1:
        account = nlogin.accounts[next(iter(alias_candidates))]
        method = "nlogin_uuid_alias"
    elif len(alias_candidates) > 1:
        warnings.append(
            f"nLogin maps source UUID {source_uuid} to multiple accounts: "
            + ", ".join(sorted(map(str, alias_candidates)))
        )

    if account is None and source_name:
        name_candidates = nlogin.by_name.get(source_name.casefold(), set())
        if len(name_candidates) == 1:
            account = nlogin.accounts[next(iter(name_candidates))]
            method = "nlogin_name"
        elif len(name_candidates) > 1:
            warnings.append(
                f"nLogin has multiple accounts named {source_name!r}: "
                + ", ".join(sorted(map(str, name_candidates)))
            )

    if account is not None and source_name:
        by_name = nlogin.by_name.get(source_name.casefold(), set())
        if by_name and account.canonical_uuid not in by_name:
            warnings.append(
                f"source UUID and source name point at different nLogin accounts "
                f"({account.canonical_uuid} vs {', '.join(map(str, by_name))}); using the UUID match"
            )

    if account is None:
        username, username_warnings = normalize_username(source_name, source_uuid)
        warnings.extend(username_warnings)
        if source_name:
            warnings.append(
                f"no nLogin account matched UUID {source_uuid} or name {source_name!r}; "
                "using the UltimateHomes UUID"
            )
        else:
            warnings.append(
                f"no nLogin account matched UUID {source_uuid} and the file has no username; "
                "using the UltimateHomes UUID with a placeholder name"
            )
        return OwnerResolution(
            uuid=source_uuid,
            username=username,
            method=method,
            source_uuid=source_uuid,
            source_name=source_name,
            account=None,
            warnings=warnings,
        )

    username, username_warnings = normalize_username(account.name or source_name, account.canonical_uuid)
    warnings.extend(username_warnings)
    if source_name and account.name and source_name.casefold() != account.name.casefold():
        warnings.append(
            f"nLogin current name {account.name!r} differs from UltimateHomes name {source_name!r}"
        )
    if account.canonical_uuid != source_uuid:
        warnings.append(
            f"nLogin UUID {account.canonical_uuid} differs from UltimateHomes UUID {source_uuid}; "
            "using the nLogin UUID"
        )

    return OwnerResolution(
        uuid=account.canonical_uuid,
        username=username,
        method=method,
        source_uuid=source_uuid,
        source_name=source_name,
        account=account,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# HuskHomes target (SQLite / MySQL)
# ---------------------------------------------------------------------------


@dataclass
class HuskHomesConfig:
    path: Path
    sha256: str
    backend: str
    tables: dict[str, str]
    home_delimiter: str
    restrict_names: bool
    name_regex: str
    case_insensitive_names: bool
    sqlite_path: Optional[Path] = None
    mysql_host: Optional[str] = None
    mysql_port: int = 3306
    mysql_database: Optional[str] = None
    mysql_user: Optional[str] = None
    mysql_password: str = ""
    server_name: str = DEFAULT_SERVER_NAME
    max_homes: int = 10
    economy_enabled: bool = False
    free_home_slots: int = 5


def read_huskhomes_config(path: Path, args: argparse.Namespace) -> HuskHomesConfig:
    if not path.is_file():
        raise MigrationError(f"HuskHomes config.yml not found: {path}")
    raw = load_yaml(path)
    if not isinstance(raw, dict):
        raise MigrationError("HuskHomes config.yml is not a mapping")

    database = raw.get("database") or {}
    if not isinstance(database, dict):
        raise MigrationError("HuskHomes database section is not a mapping")
    backend = str(getattr(args, "huskhomes_backend", None) or database.get("type", "SQLITE")).upper()

    configured_tables = database.get("table_names") or {}
    tables = dict(DEFAULT_TABLES)
    if isinstance(configured_tables, dict):
        for key, value in configured_tables.items():
            if value is not None:
                tables[str(key)] = str(value)
    for key in REQUIRED_TABLE_KEYS:
        if key not in tables:
            raise MigrationError(f"HuskHomes config is missing table name {key}")
        sql_ident(tables[key])

    general = raw.get("general") or {}
    if not isinstance(general, dict):
        raise MigrationError("HuskHomes general section is not a mapping")
    names = general.get("names") or {}
    if not isinstance(names, dict):
        names = {}
    delimiter = str(general.get("home_delimiter", DEFAULT_HOME_DELIMITER))
    restrict_names = bool(names.get("restrict", True))
    name_regex = str(names.get("regex", DEFAULT_NAME_REGEX))
    case_insensitive = bool(names.get("case_insensitive", False))
    try:
        re.compile(name_regex)
    except re.error as exc:
        raise MigrationError(f"invalid HuskHomes home-name regex {name_regex!r}: {exc}") from exc

    economy = raw.get("economy") or {}
    if not isinstance(economy, dict):
        economy = {}

    result = HuskHomesConfig(
        path=path.resolve(),
        sha256=sha256_file(path),
        backend=backend,
        tables=tables,
        home_delimiter=delimiter,
        restrict_names=restrict_names,
        name_regex=name_regex,
        case_insensitive_names=case_insensitive,
        server_name=args.server_name or DEFAULT_SERVER_NAME,
        max_homes=int(general.get("max_homes", 10) or 0),
        economy_enabled=bool(economy.get("enabled", False)),
        free_home_slots=int(economy.get("free_home_slots", 5) or 0),
    )

    if backend == "SQLITE":
        if args.huskhomes_sqlite:
            result.sqlite_path = Path(args.huskhomes_sqlite).resolve()
        else:
            result.sqlite_path = (path.parent / "HuskHomesData.db").resolve()
        if not result.sqlite_path.is_file():
            raise MigrationError(f"HuskHomes SQLite database not found: {result.sqlite_path}")
    elif backend in ("MYSQL", "MARIADB"):
        credentials = database.get("credentials") or {}
        if not isinstance(credentials, dict):
            credentials = {}
        result.mysql_host = args.huskhomes_mysql_host or credentials.get("host") or "localhost"
        result.mysql_port = int(args.huskhomes_mysql_port or credentials.get("port") or 3306)
        result.mysql_database = args.huskhomes_mysql_database or credentials.get("database")
        result.mysql_user = args.huskhomes_mysql_user or credentials.get("username")
        env_name = getattr(args, "huskhomes_password_env", "HUSKHOMES_DB_PASSWORD") or "HUSKHOMES_DB_PASSWORD"
        from_cli = getattr(args, "_huskhomes_password_from_cli", False)
        config_pw = getattr(args, "huskhomes_config_password", None)
        if from_cli and args.huskhomes_password:
            warn("passing the HuskHomes database password on the command line is visible in process listings")
            result.mysql_password = args.huskhomes_password
        elif config_pw:
            result.mysql_password = str(config_pw)
        elif env_name and os.environ.get(env_name) is not None:
            result.mysql_password = os.environ[env_name]
        elif args.huskhomes_password:
            result.mysql_password = str(args.huskhomes_password)
        else:
            result.mysql_password = str(credentials.get("password") or "")
        if not result.mysql_database:
            raise MigrationError("HuskHomes MySQL database name is missing from config and CLI")
        if not result.mysql_user:
            raise MigrationError("HuskHomes MySQL username is missing from config and CLI")
    else:
        raise MigrationError(
            f"HuskHomes database type {backend!r} is not supported by this tool. "
            "Supported: SQLITE, MYSQL, MARIADB."
        )

    return result


@dataclass
class ExistingHome:
    home_uuid: uuid.UUID
    owner_uuid: uuid.UUID
    owner_username: str
    name: str
    description: str
    tags: Optional[str]
    timestamp: str
    x: float
    y: float
    z: float
    yaw: float
    pitch: float
    world_name: str
    world_uuid: uuid.UUID
    server_name: str
    public: bool

    def position_key(self) -> tuple:
        return (
            round(self.x, 6), round(self.y, 6), round(self.z, 6),
            round(self.yaw, 6), round(self.pitch, 6),
            self.world_name,
        )


class _ExecuteResult:
    __slots__ = ("lastrowid",)

    def __init__(self, lastrowid: Optional[int]) -> None:
        self.lastrowid = lastrowid


class HuskHomesDatabase:
    def __init__(self, config: HuskHomesConfig) -> None:
        self.config = config
        self.backend = config.backend
        self.connection: Any = None
        self.ph = "?" if self.backend == "SQLITE" else "%s"
        self.tables = config.tables

    # -- lifecycle ---------------------------------------------------------

    def connect(self, read_only: bool = False) -> None:
        if self.connection is not None:
            return
        if self.backend == "SQLITE":
            if not self.config.sqlite_path:
                raise MigrationError("SQLite path is not configured")
            uri = self.config.sqlite_path.as_uri() + ("?mode=ro" if read_only else "")
            try:
                self.connection = sqlite3.connect(uri, uri=True, timeout=30)
            except sqlite3.Error as exc:
                raise MigrationError(f"could not open HuskHomes SQLite database: {exc}") from exc
            self.connection.row_factory = sqlite3.Row
            self.execute("PRAGMA foreign_keys=ON")
            self.execute("PRAGMA busy_timeout=30000")
        else:
            try:
                import pymysql  # type: ignore
            except ImportError as exc:
                raise MigrationError(
                    "PyMySQL is required for HuskHomes MySQL. Install with: "
                    "python3 -m pip install -r requirements.txt"
                ) from exc
            try:
                self.connection = pymysql.connect(
                    host=self.config.mysql_host,
                    port=self.config.mysql_port,
                    user=self.config.mysql_user,
                    password=self.config.mysql_password,
                    database=self.config.mysql_database,
                    charset="utf8mb4",
                    cursorclass=pymysql.cursors.DictCursor,
                    autocommit=False,
                    connect_timeout=15,
                    read_timeout=120,
                    write_timeout=60,
                )
            except Exception as exc:
                raise MigrationError(f"could not connect to HuskHomes MySQL: {exc}") from exc

    def close(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None

    def begin(self) -> None:
        if self.backend == "SQLITE":
            self.connection.execute("BEGIN IMMEDIATE")
        else:
            # Close any implicit read snapshot opened during schema validation
            # before starting the write transaction.
            self.connection.rollback()
            self.connection.begin()

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        if self.connection is not None:
            self.connection.rollback()

    # -- SQL helpers -------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _ExecuteResult:
        if self.backend == "SQLITE":
            cursor = self.connection.execute(sql, params)
            try:
                return _ExecuteResult(cursor.lastrowid)
            finally:
                cursor.close()
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql, params)
            return _ExecuteResult(cursor.lastrowid)
        finally:
            cursor.close()

    def execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        if self.backend == "SQLITE":
            cursor = self.connection.executemany(sql, rows)
            cursor.close()
            return
        cursor = self.connection.cursor()
        try:
            cursor.executemany(sql, rows)
        finally:
            cursor.close()

    def insert_rows(
        self,
        table_key: str,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        batch_size: int = 250,
    ) -> None:
        if not rows:
            return
        cols_sql = ", ".join(sql_ident(col) for col in columns)
        row_ph = "(" + ",".join([self.ph] * len(columns)) + ")"
        if self.backend == "SQLITE":
            sql = f"INSERT INTO {self.table(table_key)} ({cols_sql}) VALUES {row_ph}"
            self.execute_many(sql, rows)
            return
        table_sql = self.table(table_key)
        for start in range(0, len(rows), batch_size):
            chunk = rows[start : start + batch_size]
            values_sql = ",".join([row_ph] * len(chunk))
            flat_params = [value for row in chunk for value in row]
            sql = f"INSERT INTO {table_sql} ({cols_sql}) VALUES {values_sql}"
            self.execute(sql, flat_params)

    def update_rows_by_key(
        self,
        table_key: str,
        key_col: str,
        update_cols: Sequence[str],
        rows: Sequence[tuple[Any, Sequence[Any]]],
        batch_size: int = 100,
    ) -> None:
        if not rows:
            return
        table_sql = self.table(table_key)
        key_ident = sql_ident(key_col)
        if self.backend == "SQLITE":
            set_sql = ", ".join(f"{sql_ident(col)}={self.ph}" for col in update_cols)
            sql = f"UPDATE {table_sql} SET {set_sql} WHERE {key_ident}={self.ph}"
            self.execute_many(sql, [tuple(vals) + (key,) for key, vals in rows])
            return
        for start in range(0, len(rows), batch_size):
            chunk = rows[start : start + batch_size]
            if len(chunk) == 1:
                key, vals = chunk[0]
                set_sql = ", ".join(f"{sql_ident(col)}={self.ph}" for col in update_cols)
                sql = f"UPDATE {table_sql} SET {set_sql} WHERE {key_ident}={self.ph}"
                self.execute(sql, tuple(vals) + (key,))
                continue
            set_clauses: list[str] = []
            params: list[Any] = []
            for col_idx, col in enumerate(update_cols):
                when_parts = " ".join(f"WHEN {self.ph} THEN {self.ph}" for _ in chunk)
                set_clauses.append(f"{sql_ident(col)} = CASE {key_ident} {when_parts} END")
                for key, vals in chunk:
                    params.append(key)
                    params.append(vals[col_idx])
            in_ph = ",".join([self.ph] * len(chunk))
            params.extend(key for key, _ in chunk)
            sql = f"UPDATE {table_sql} SET {', '.join(set_clauses)} WHERE {key_ident} IN ({in_ph})"
            self.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        if self.backend == "SQLITE":
            cursor = self.connection.execute(sql, params)
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
        else:
            cursor = self.connection.cursor()
            try:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
            finally:
                cursor.close()
        return [dict(row) for row in rows]

    def query_in_chunks(
        self,
        sql_template: str,
        keys: Sequence[Any],
        chunk_size: int = 500,
    ) -> list[dict[str, Any]]:
        unique_keys = list(dict.fromkeys(keys))
        if not unique_keys:
            return []
        results: list[dict[str, Any]] = []
        for start in range(0, len(unique_keys), chunk_size):
            chunk = unique_keys[start : start + chunk_size]
            placeholders = ",".join([self.ph] * len(chunk))
            sql = sql_template.replace("{placeholders}", placeholders)
            results.extend(self.query(sql, chunk))
        return results

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[dict[str, Any]]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def table(self, key: str) -> str:
        return sql_ident(self.tables[key])

    # -- schema validation -------------------------------------------------

    def validate_schema(self) -> None:
        for key in REQUIRED_TABLE_KEYS:
            table = self.tables[key]
            if self.backend == "SQLITE":
                row = self.query_one(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
                )
                if not row:
                    raise MigrationError(f"HuskHomes table {table!r} does not exist")
                columns = {
                    str(r["name"]).lower()
                    for r in self.query(f"PRAGMA table_info({sql_ident(table)})")
                }
            else:
                columns = {
                    str(r["COLUMN_NAME"]).lower()
                    for r in self.query(
                        """
                        SELECT COLUMN_NAME
                        FROM information_schema.COLUMNS
                        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                        """,
                        (self.config.mysql_database, table),
                    )
                }
                if not columns:
                    raise MigrationError(f"HuskHomes table {table!r} does not exist")
                table_info = self.query_one(
                    """
                    SELECT ENGINE
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                    """,
                    (self.config.mysql_database, table),
                )
                engine = str((table_info or {}).get("ENGINE") or "").upper()
                if engine == "MYISAM":
                    raise MigrationError(
                        f"HuskHomes table {table!r} uses MyISAM, which does not support transactions; "
                        "convert HuskHomes tables to InnoDB before migrating"
                    )
            missing = REQUIRED_COLUMNS[key] - columns
            if missing:
                raise MigrationError(
                    f"HuskHomes table {table!r} is missing column(s): {', '.join(sorted(missing))}"
                )

        metadata = self.query_one(f"SELECT schema_version FROM {self.table('META_DATA')}")
        if metadata is None:
            raise MigrationError("HuskHomes metadata table is empty")
        version = int(metadata["schema_version"])
        if version != 0:
            raise MigrationError(
                f"HuskHomes schema version is {version}, but this tool was written for version 0. "
                "Use the matching HuskHomes version or update the tool first."
            )

        if self.backend == "SQLITE":
            integrity = self.query_one("PRAGMA integrity_check")
            value = list(integrity.values())[0] if integrity else None
            if str(value).lower() != "ok":
                raise MigrationError(f"HuskHomes SQLite integrity_check failed: {value}")

    # -- reads -------------------------------------------------------------

    def fetch_users(self) -> dict[uuid.UUID, dict[str, Any]]:
        rows = self.query(f"SELECT `uuid`, `username` FROM {self.table('PLAYER_DATA')}")
        users: dict[uuid.UUID, dict[str, Any]] = {}
        for row in rows:
            user_uuid = parse_uuid(row.get("uuid"))
            if user_uuid is None:
                raise MigrationError(f"invalid user UUID in HuskHomes database: {row.get('uuid')!r}")
            users[user_uuid] = dict(row)
        return users

    def fetch_homes(self, include_cooldowns: bool = False) -> list[ExistingHome]:
        sql = f"""
            SELECT h.`uuid` AS home_uuid, h.`owner_uuid`, u.`username` AS owner_username,
                   s.`name`, s.`description`, s.`tags`, s.`timestamp`,
                   p.`x`, p.`y`, p.`z`, p.`yaw`, p.`pitch`,
                   p.`world_name`, p.`world_uuid`, p.`server_name`, h.`public`
            FROM {self.table('HOME_DATA')} h
            INNER JOIN {self.table('SAVED_POSITION_DATA')} s ON h.`saved_position_id` = s.`id`
            INNER JOIN {self.table('POSITION_DATA')} p ON s.`position_id` = p.`id`
            INNER JOIN {self.table('PLAYER_DATA')} u ON h.`owner_uuid` = u.`uuid`
        """
        homes: list[ExistingHome] = []
        for row in self.query(sql):
            home_uuid = parse_uuid(row.get("home_uuid"))
            owner_uuid = parse_uuid(row.get("owner_uuid"))
            world_uuid = parse_uuid(row.get("world_uuid"))
            if home_uuid is None or owner_uuid is None or world_uuid is None:
                raise MigrationError("invalid UUID found in HuskHomes home data")
            homes.append(ExistingHome(
                home_uuid=home_uuid,
                owner_uuid=owner_uuid,
                owner_username=str(row.get("owner_username") or ""),
                name=str(row.get("name") or ""),
                description=str(row.get("description") or ""),
                tags=row.get("tags"),
                timestamp=str(row.get("timestamp") or ""),
                x=float(row["x"]),
                y=float(row["y"]),
                z=float(row["z"]),
                yaw=float(row["yaw"]),
                pitch=float(row["pitch"]),
                world_name=str(row.get("world_name") or ""),
                world_uuid=world_uuid,
                server_name=str(row.get("server_name") or ""),
                public=bool(row.get("public")),
            ))
        return homes

    def fetch_world_uuids(self) -> dict[str, uuid.UUID]:
        rows = self.query(
            f"SELECT DISTINCT `world_name`, `world_uuid` FROM {self.table('POSITION_DATA')}"
        )
        result: dict[str, uuid.UUID] = {}
        conflicts: set[str] = set()
        for row in rows:
            name = str(row.get("world_name") or "")
            world_uuid = parse_uuid(row.get("world_uuid"))
            if not name or world_uuid is None:
                continue
            if name in result and result[name] != world_uuid:
                conflicts.add(name)
            result[name] = world_uuid
        for name in conflicts:
            result.pop(name, None)
            warn(f"world {name!r} has multiple UUIDs in the HuskHomes database; using a placeholder")
        return result

    def fetch_server_name(self) -> Optional[str]:
        rows = self.query(
            f"SELECT `server_name`, COUNT(*) AS c FROM {self.table('POSITION_DATA')} "
            "GROUP BY `server_name` ORDER BY c DESC LIMIT 1"
        )
        if rows:
            return str(rows[0].get("server_name") or "") or None
        return None


# ---------------------------------------------------------------------------
# Home name allocation
# ---------------------------------------------------------------------------


class NameAllocator:
    def __init__(self, config: HuskHomesConfig) -> None:
        self.config = config
        self.regex = re.compile(config.name_regex)
        # owner UUID -> case-folded name -> UUID of the home using that name.
        self.taken: dict[uuid.UUID, dict[str, uuid.UUID]] = defaultdict(dict)

    @property
    def case_insensitive(self) -> bool:
        # MySQL/MariaDB use a case-insensitive collation by default even when
        # HuskHomes' own case_insensitive setting is false. Treating names as
        # case-insensitive there prevents two homes that look distinct in the
        # plan from becoming ambiguous in the database.
        return self.config.case_insensitive_names or self.config.backend in ("MYSQL", "MARIADB")

    def key(self, name: str) -> str:
        return name.casefold() if self.case_insensitive else name

    def reserve(self, owner_uuid: uuid.UUID, name: str, home_uuid: uuid.UUID) -> None:
        self.taken[owner_uuid].setdefault(self.key(name), home_uuid)

    def is_taken(self, owner_uuid: uuid.UUID, name: str, home_uuid: uuid.UUID) -> bool:
        """True when another home (not this planned home) owns the name."""
        existing = self.taken[owner_uuid].get(self.key(name))
        return existing is not None and existing != home_uuid

    def valid(self, name: str) -> bool:
        if not name or len(name) > HUSKHOMES_MAX_NAME_LENGTH:
            return False
        if "\x00" in name or any(ch.isspace() for ch in name):
            return False
        if self.config.home_delimiter and self.config.home_delimiter in name:
            return False
        if self.config.restrict_names and not self.regex.fullmatch(name):
            return False
        return True

    def _candidate(self, raw: str, source_uuid: uuid.UUID, attempt: int) -> str:
        # Keep the original name readable for short names; otherwise use a
        # deterministic 4-char hash suffix so re-runs produce the same name.
        cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", raw).strip("_")
        if not cleaned:
            cleaned = "home"
        separator = "-" if self.valid("a-b") else "_"
        suffix = separator + base36(zlib.crc32(f"{raw}|{source_uuid}|{attempt}".encode("utf-8")), 4)
        stem_length = max(1, HUSKHOMES_MAX_NAME_LENGTH - len(suffix))
        candidate = (cleaned[:stem_length] + suffix)[:HUSKHOMES_MAX_NAME_LENGTH]
        return candidate

    def allocate(
        self,
        owner_uuid: uuid.UUID,
        raw_name: str,
        source_uuid: uuid.UUID,
        home_uuid: uuid.UUID,
        on_conflict: str,
    ) -> tuple[Optional[str], list[str], Optional[str], Optional[uuid.UUID]]:
        """
        Return (final_name, warnings, conflict_message, conflicting_home_uuid).
        final_name is None when the home cannot be safely named.
        """
        warnings: list[str] = []
        raw_name = str(raw_name)

        if self.valid(raw_name):
            preferred = raw_name
        else:
            preferred = self._candidate(raw_name, source_uuid, 0)
            warnings.append(
                f"home name {raw_name!r} is not valid for HuskHomes; renamed to {preferred!r}"
            )

        if not self.valid(preferred):
            return None, warnings, (
                f"could not construct a HuskHomes-valid name from {raw_name!r}; "
                f"check general.names.regex in config.yml"
            ), None

        if not self.is_taken(owner_uuid, preferred, home_uuid):
            self.reserve(owner_uuid, preferred, home_uuid)
            return preferred, warnings, None, None

        if on_conflict == "skip":
            return (
                None,
                warnings,
                f"home name {preferred!r} is already in use (policy: skip)",
                self.taken[owner_uuid].get(self.key(preferred)),
            )

        for attempt in range(1, 5000):
            candidate = self._candidate(raw_name, source_uuid, attempt)
            if not self.valid(candidate):
                continue
            if not self.is_taken(owner_uuid, candidate, home_uuid):
                self.reserve(owner_uuid, candidate, home_uuid)
                warnings.append(
                    f"home name {raw_name!r} collides with an existing home; renamed to {candidate!r}"
                )
                return candidate, warnings, None, None

        return None, warnings, f"could not find a free name for home {raw_name!r}", None


# ---------------------------------------------------------------------------
# Plan building
# ---------------------------------------------------------------------------


def home_uuid_for(source_uuid: uuid.UUID, raw_name: str) -> uuid.UUID:
    return uuid.uuid5(HOME_UUID_NAMESPACE, f"{source_uuid}:{raw_name}")


def world_uuid_for(
    world_name: str,
    known: Mapping[str, uuid.UUID],
    overrides: Mapping[str, uuid.UUID],
) -> tuple[uuid.UUID, Optional[str]]:
    if world_name in overrides:
        return overrides[world_name], None
    if world_name in known:
        return known[world_name], None
    return uuid.uuid5(WORLD_UUID_NAMESPACE, world_name), (
        f"no known world UUID for {world_name!r}; wrote a deterministic placeholder. "
        "HuskHomes will still resolve this world by name."
    )


def build_plan(
    players: Sequence[SourcePlayer],
    parse_issues: Sequence[dict[str, Any]],
    nlogin: NLoginIndex,
    overrides: Mapping[uuid.UUID, ManualOverride],
    config: HuskHomesConfig,
    database: HuskHomesDatabase,
    on_conflict: str,
    world_overrides: Mapping[str, uuid.UUID],
    require_nlogin_match: bool,
    playerdata_dir: Path,
) -> dict[str, Any]:
    users_existing = database.fetch_users()
    existing_homes = database.fetch_homes()
    known_worlds = database.fetch_world_uuids()
    existing_server = database.fetch_server_name()
    server_name = existing_server or config.server_name or DEFAULT_SERVER_NAME

    username_owner: dict[str, set[uuid.UUID]] = defaultdict(set)
    for user_uuid, row in users_existing.items():
        name = str(row.get("username") or "")
        if name:
            username_owner[name.casefold()].add(user_uuid)

    existing_by_uuid: dict[uuid.UUID, ExistingHome] = {h.home_uuid: h for h in existing_homes}

    allocator = NameAllocator(config)
    # Seed with existing homes so re-runs allocate the same names and never
    # silently collide with a pre-existing home. A home may reuse its own name.
    for home in existing_homes:
        allocator.reserve(home.owner_uuid, home.name, home.home_uuid)

    all_issues: list[dict[str, Any]] = list(parse_issues) + list(nlogin.issues)
    users_plan: dict[uuid.UUID, dict[str, Any]] = {}
    homes_plan: list[dict[str, Any]] = []
    source_home_count = 0
    skipped_empty_files = 0
    world_warning_seen: set[str] = set()
    planned_usernames: dict[str, uuid.UUID] = {}
    planned_uuid_username: dict[uuid.UUID, str] = {}
    migration_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def add_user_issue(level: str, code: str, message: str, player: SourcePlayer) -> None:
        all_issues.append({
            "level": level,
            "code": code,
            "message": message,
            "source_file": player.relative_file,
            "source_uuid": str(player.source_uuid),
        })

    for player in players:
        if not player.homes:
            skipped_empty_files += 1
            continue

        source_home_count += len(player.homes)
        resolution = resolve_owner(
            player.source_uuid, player.last_seen_as, nlogin, overrides
        )
        if require_nlogin_match and resolution.account is None and player.source_uuid not in overrides:
            add_user_issue(
                "error",
                "required_nlogin_match_missing",
                f"no nLogin match for source UUID {player.source_uuid} "
                f"(name {player.last_seen_as!r}); --require-nlogin-match is enabled",
                player,
            )

        # User-level validation.
        same_name_users = username_owner.get(resolution.username.casefold(), set())
        other_users = same_name_users - {resolution.uuid}
        if other_users:
            add_user_issue(
                "error",
                "huskhomes_username_conflict",
                f"username {resolution.username!r} already belongs to HuskHomes UUID(s) "
                f"{', '.join(sorted(map(str, other_users)))}, but migration maps this player to "
                f"{resolution.uuid}. Refusing to create a duplicate-name user.",
                player,
            )

        planned_name_owner = planned_usernames.get(resolution.username.casefold())
        if planned_name_owner is not None and planned_name_owner != resolution.uuid:
            add_user_issue(
                "error",
                "planned_username_conflict",
                f"username {resolution.username!r} is assigned to two different HuskHomes UUIDs "
                f"({planned_name_owner} and {resolution.uuid}); resolve this with --uuid-map or a "
                "username override before applying",
                player,
            )
        else:
            planned_usernames[resolution.username.casefold()] = resolution.uuid

        previous_planned_name = planned_uuid_username.get(resolution.uuid)
        if (
            previous_planned_name is not None
            and previous_planned_name.casefold() != resolution.username.casefold()
        ):
            add_user_issue(
                "error",
                "planned_uuid_username_conflict",
                f"HuskHomes UUID {resolution.uuid} is assigned two different usernames "
                f"({previous_planned_name!r} and {resolution.username!r}); resolve this with "
                "--uuid-map before applying",
                player,
            )
        else:
            planned_uuid_username[resolution.uuid] = resolution.username

        existing_user = users_existing.get(resolution.uuid)
        if existing_user is not None:
            existing_name = str(existing_user.get("username") or "")
            if existing_name and existing_name.casefold() != resolution.username.casefold():
                resolution.warnings.append(
                    f"HuskHomes user {resolution.uuid} is named {existing_name!r}; "
                    f"will update to {resolution.username!r}"
                )

        user_entry = users_plan.setdefault(resolution.uuid, {
            "uuid": str(resolution.uuid),
            "username": resolution.username,
            "mapping": resolution.method,
            "source_uuids": [],
            "source_names": [],
            "existing_username": str(existing_user.get("username")) if existing_user else None,
            "warnings": [],
        })
        if player.source_uuid not in [uuid.UUID(x) for x in user_entry["source_uuids"]]:
            user_entry["source_uuids"].append(str(player.source_uuid))
        if player.last_seen_as and player.last_seen_as not in user_entry["source_names"]:
            user_entry["source_names"].append(player.last_seen_as)
        for message in resolution.warnings:
            if message not in user_entry["warnings"]:
                user_entry["warnings"].append(message)

        for source_home in player.homes:
            h_uuid = home_uuid_for(player.source_uuid, source_home.raw_name)
            warnings = list(source_home.warnings)

            final_name, name_warnings, conflict_message, conflict_uuid = allocator.allocate(
                resolution.uuid, source_home.raw_name, player.source_uuid, h_uuid, on_conflict
            )
            warnings.extend(name_warnings)

            world_uuid, world_warning = world_uuid_for(
                source_home.world, known_worlds, world_overrides
            )
            if world_warning and source_home.world not in world_warning_seen:
                world_warning_seen.add(source_home.world)
                warnings.append(world_warning)

            home_uuid_existing = existing_by_uuid.get(h_uuid)
            action = "create"
            existing_info: Optional[dict[str, Any]] = None
            error_message: Optional[str] = None

            if conflict_message:
                if conflict_uuid is not None and conflict_uuid in existing_by_uuid:
                    existing_info = existing_home_info(existing_by_uuid[conflict_uuid])
                if on_conflict == "skip":
                    action = "skip"
                    warnings.append(conflict_message)
                    all_issues.append({
                        "level": "warning",
                        "code": "home_skipped",
                        "message": (
                            f"{conflict_message}; this source home will NOT be migrated "
                            "(policy: --on-conflict skip)"
                        ),
                        "source_file": player.relative_file,
                        "source_uuid": str(player.source_uuid),
                        "home_name": source_home.raw_name,
                    })
                else:
                    action = "blocked"
                    error_message = (
                        f"{conflict_message}; use --on-conflict rename to preserve both homes, "
                        "or resolve the conflict manually"
                    )
            elif home_uuid_existing is not None:
                existing_info = existing_home_info(home_uuid_existing)
                action = "update"

            home_entry = {
                "action": action,
                "home_uuid": str(h_uuid),
                "owner_uuid": str(resolution.uuid),
                "owner_username": resolution.username,
                "name": final_name,
                "timestamp": migration_timestamp,
                "source": {
                    "playerdata_dir": str(playerdata_dir),
                    "file": player.relative_file,
                    "file_sha256": player.sha256,
                    "player_uuid": str(player.source_uuid),
                    "player_name": player.last_seen_as,
                    "home_name": source_home.raw_name,
                    "recovered": any("recovered nested" in w for w in source_home.warnings),
                },
                "position": {
                    "x": source_home.x + 0.5,
                    "y": source_home.y,
                    "z": source_home.z + 0.5,
                    "yaw": source_home.yaw,
                    "pitch": source_home.pitch,
                    "world_name": source_home.world,
                    "world_uuid": str(world_uuid),
                    "server_name": server_name,
                },
                "existing": existing_info,
                "warnings": warnings,
            }
            if error_message:
                home_entry["error"] = error_message
                all_issues.append({
                    "level": "error",
                    "code": "home_conflict",
                    "message": error_message,
                    "source_file": player.relative_file,
                    "source_uuid": str(player.source_uuid),
                    "home_name": source_home.raw_name,
                })
            homes_plan.append(home_entry)

    # Warn when a player ends up with more homes than HuskHomes' configured
    # creation limit. Existing homes remain usable, but /sethome may refuse.
    planned_uuids = {uuid.UUID(h["home_uuid"]) for h in homes_plan}
    homes_per_owner: Counter[uuid.UUID] = Counter()
    for home in homes_plan:
        if home["action"] in ("create", "update"):
            homes_per_owner[uuid.UUID(home["owner_uuid"])] += 1
    for existing_home in existing_homes:
        if existing_home.home_uuid not in planned_uuids:
            homes_per_owner[existing_home.owner_uuid] += 1
    for owner_uuid, count in sorted(homes_per_owner.items(), key=lambda item: str(item[0])):
        if config.max_homes and count > config.max_homes:
            username = next(
                (
                    u["username"] for u in users_plan.values()
                    if u["uuid"] == str(owner_uuid)
                ),
                str(owner_uuid),
            )
            all_issues.append({
                "level": "warning",
                "code": "above_max_homes",
                "message": (
                    f"{username!r} will have {count} homes, above HuskHomes' configured "
                    f"general.max_homes={config.max_homes}. Existing homes remain usable, "
                    "but creating more homes may be blocked."
                ),
            })

    actions = Counter(home["action"] for home in homes_plan)
    critical_issues = [i for i in all_issues if i.get("level") == "error"]
    warning_issues = [i for i in all_issues if i.get("level") != "error"]
    user_warning_count = sum(len(u.get("warnings") or []) for u in users_plan.values())
    home_warning_count = sum(len(h.get("warnings") or []) for h in homes_plan)
    planned_created = sum(1 for home in homes_plan if home["action"] == "create")
    planned_updated = sum(1 for home in homes_plan if home["action"] == "update")

    return {
        "format": PLAN_FORMAT,
        "version": PLAN_VERSION,
        "tool_version": TOOL_VERSION,
        "created_at": utc_now_iso(),
        "database_timestamp": migration_timestamp,
        "source_home_count": source_home_count,
        "planned_home_count": len(homes_plan),
        "skipped_empty_files": skipped_empty_files,
        "summary": {
            "source_files": len(players),
            "source_files_with_homes": sum(1 for p in players if p.homes),
            "source_homes": source_home_count,
            "planned_homes": len(homes_plan),
            "homes_to_create": planned_created,
            "homes_to_update": planned_updated,
            "actions": dict(actions),
            "users": len(users_plan),
            "errors": len(critical_issues),
            "warnings": len(warning_issues),
            "user_warnings": user_warning_count,
            "home_warnings": home_warning_count,
            "nlogin_accounts": nlogin.row_count,
        },
        "users": sorted(users_plan.values(), key=lambda u: (u["username"].casefold(), u["uuid"])),
        "homes": homes_plan,
        "issues": all_issues,
        "worlds": {
            "known": {name: str(value) for name, value in sorted(known_worlds.items())},
            "overrides": {name: str(value) for name, value in sorted(world_overrides.items())},
        },
        "target": target_fingerprint(config),
    }


def existing_home_info(home: ExistingHome) -> dict[str, Any]:
    return {
        "home_uuid": str(home.home_uuid),
        "name": home.name,
        "owner_uuid": str(home.owner_uuid),
        "position": {
            "x": home.x,
            "y": home.y,
            "z": home.z,
            "yaw": home.yaw,
            "pitch": home.pitch,
            "world_name": home.world_name,
            "world_uuid": str(home.world_uuid),
            "server_name": home.server_name,
        },
    }



def target_fingerprint(config: HuskHomesConfig) -> dict[str, Any]:
    result: dict[str, Any] = {
        "backend": config.backend,
        "tables": dict(config.tables),
        "config_sha256": config.sha256,
        "home_delimiter": config.home_delimiter,
        "restrict_names": config.restrict_names,
        "name_regex": config.name_regex,
        "case_insensitive_names": config.case_insensitive_names,
        "effective_case_insensitive": (
            config.case_insensitive_names or config.backend in ("MYSQL", "MARIADB")
        ),
        "max_homes": config.max_homes,
        "economy_enabled": config.economy_enabled,
    }
    if config.backend == "SQLITE":
        assert config.sqlite_path is not None
        result["sqlite_path"] = str(config.sqlite_path)
    else:
        result["mysql_host"] = config.mysql_host
        result["mysql_port"] = config.mysql_port
        result["mysql_database"] = config.mysql_database
    return result


# ---------------------------------------------------------------------------
# Plan I/O, reports, and verification helpers
# ---------------------------------------------------------------------------


def load_plan(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise MigrationError(f"plan not found: {path}")
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise MigrationError(f"could not read plan {path}: {exc}") from exc
    if not isinstance(plan, dict):
        raise MigrationError("plan is not a JSON object")
    if plan.get("format") != PLAN_FORMAT:
        raise MigrationError(f"unrecognized plan format: {plan.get('format')!r}")
    if plan.get("version") != PLAN_VERSION:
        raise MigrationError(
            f"plan version {plan.get('version')!r} is not supported (expected {PLAN_VERSION})"
        )
    return plan


def verify_source_manifest(plan: Mapping[str, Any]) -> None:
    source = plan.get("source") or {}
    playerdata_dir = Path(str(source.get("playerdata_dir") or ""))
    manifest = source.get("files") or {}
    if not playerdata_dir.is_dir():
        raise MigrationError(f"UltimateHomes playerdata directory from plan is missing: {playerdata_dir}")
    if not isinstance(manifest, dict) or not manifest:
        raise MigrationError("plan has no source file manifest")

    actual_files = {
        p.relative_to(playerdata_dir).as_posix(): p
        for p in playerdata_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".yml"
    }
    missing = sorted(set(manifest) - set(actual_files))
    extra = sorted(set(actual_files) - set(manifest))
    if missing:
        raise MigrationError(f"source playerdata file(s) disappeared since scan: {', '.join(missing[:5])}")
    if extra:
        raise MigrationError(f"new source playerdata file(s) appeared since scan: {', '.join(extra[:5])}")
    for relative, expected in manifest.items():
        actual_hash = sha256_file(actual_files[relative])
        if actual_hash != expected.get("sha256"):
            raise MigrationError(f"source playerdata file changed since scan: {relative}")


def verify_target(plan: Mapping[str, Any], config: HuskHomesConfig) -> None:
    expected = plan.get("target") or {}
    actual = target_fingerprint(config)
    for key in ("backend", "tables", "home_delimiter", "restrict_names", "name_regex", "case_insensitive_names"):
        if expected.get(key) != actual.get(key):
            raise MigrationError(
                f"HuskHomes target/config mismatch for {key!r}: plan={expected.get(key)!r} "
                f"actual={actual.get(key)!r}"
            )
    if config.backend == "SQLITE":
        if expected.get("sqlite_path") != actual.get("sqlite_path"):
            raise MigrationError(
                f"refusing to apply to a different SQLite database: "
                f"plan={expected.get('sqlite_path')!r} actual={actual.get('sqlite_path')!r}"
            )
    else:
        for key in ("mysql_host", "mysql_port", "mysql_database"):
            if expected.get(key) != actual.get(key):
                raise MigrationError(
                    f"refusing to apply to a different MySQL database: "
                    f"{key} plan={expected.get(key)!r} actual={actual.get(key)!r}"
                )


def write_report(path: Path, plan: Mapping[str, Any]) -> None:
    summary = plan.get("summary") or {}
    lines: list[str] = []
    lines.append("# UltimateHomes → HuskHomes migration report")
    lines.append("")
    lines.append(f"Generated: `{plan.get('created_at')}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    for key in (
        "source_files", "source_files_with_homes", "source_homes", "planned_homes",
        "users", "nlogin_accounts", "errors", "warnings", "user_warnings", "home_warnings",
    ):
        lines.append(f"- {key}: **{summary.get(key, 0)}**")
    lines.append("")
    lines.append("Actions:")
    lines.append("")
    for action, count in sorted((summary.get("actions") or {}).items()):
        lines.append(f"- `{action}`: {count}")
    lines.append("")

    errors = [i for i in plan.get("issues", []) if i.get("level") == "error"]
    warnings = [i for i in plan.get("issues", []) if i.get("level") != "error"]
    if errors:
        lines.append("## Errors (migration cannot be applied until these are resolved)")
        lines.append("")
        for issue in errors:
            lines.append(f"- {format_issue(issue)}")
        lines.append("")
    if warnings:
        lines.append("## Warnings")
        lines.append("")
        for issue in warnings:
            lines.append(f"- {format_issue(issue)}")
        lines.append("")

    user_warnings = [
        (u.get("username"), u.get("uuid"), w)
        for u in plan.get("users", [])
        for w in (u.get("warnings") or [])
    ]
    if user_warnings:
        lines.append("## User warnings")
        lines.append("")
        for username, u_uuid, message in user_warnings:
            lines.append(f"- `{username}` (`{u_uuid}`): {message}")
        lines.append("")

    home_warnings = [
        (h.get("owner_username"), h.get("source", {}).get("home_name"), h.get("name"), w)
        for h in plan.get("homes", [])
        for w in (h.get("warnings") or [])
    ]
    if home_warnings:
        lines.append("## Home warnings")
        lines.append("")
        for username, orig_name, final_name, message in home_warnings:
            lines.append(f"- `{username}` `{orig_name}` -> `{final_name}`: {message}")
        lines.append("")

    renames = [
        h for h in plan.get("homes", [])
        if h.get("name") is not None and h.get("source", {}).get("home_name") != h.get("name")
    ]
    if renames:
        lines.append("## Renamed homes")
        lines.append("")
        lines.append("| Player | Original | Migrated |")
        lines.append("|---|---|---|")
        for home in renames:
            lines.append(
                f"| {home.get('owner_username')} | `{home.get('source', {}).get('home_name')}` "
                f"| `{home.get('name')}` |"
            )
        lines.append("")

    lines.append("## Per-user mapping")
    lines.append("")
    lines.append("| HuskHomes UUID | Username | Method | Source UUID(s) |")
    lines.append("|---|---|---|---|")
    for user in plan.get("users", []):
        lines.append(
            f"| `{user.get('uuid')}` | `{user.get('username')}` | `{user.get('mapping')}` "
            f"| `{', '.join(user.get('source_uuids', []))}` |"
        )
    lines.append("")

    atomic_write_text(path, "\n".join(lines) + "\n")


def write_homes_csv(path: Path, plan: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "action", "owner_uuid", "owner_username", "home_uuid",
                "source_file", "source_uuid", "source_name", "original_home_name",
                "final_home_name", "x", "y", "z", "yaw", "pitch", "world",
                "warnings", "error",
            ])
            for home in plan.get("homes", []):
                source = home.get("source") or {}
                position = home.get("position") or {}
                writer.writerow([
                    home.get("action"), home.get("owner_uuid"), home.get("owner_username"),
                    home.get("home_uuid"), source.get("file"), source.get("player_uuid"),
                    source.get("player_name"), source.get("home_name"), home.get("name"),
                    position.get("x"), position.get("y"), position.get("z"),
                    position.get("yaw"), position.get("pitch"), position.get("world_name"),
                    " | ".join(home.get("warnings") or []), home.get("error", ""),
                ])
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def summarize_plan(plan: Mapping[str, Any]) -> None:
    summary = plan.get("summary") or {}
    log("")
    log("Scan summary")
    log("------------")
    for key in (
        "source_files", "source_files_with_homes", "source_homes", "planned_homes",
        "users", "nlogin_accounts", "errors", "warnings", "user_warnings", "home_warnings",
    ):
        log(f"  {key}: {summary.get(key, 0)}")
    actions = summary.get("actions") or {}
    if actions:
        log("  actions: " + ", ".join(f"{k}={v}" for k, v in sorted(actions.items())))
    if summary.get("errors"):
        log("")
        log("Errors must be resolved before applying. See the report.")
    else:
        log("")
        log("No blocking errors found.")


# ---------------------------------------------------------------------------
# Apply / verify
# ---------------------------------------------------------------------------


def plan_home_by_uuid(plan: Mapping[str, Any]) -> dict[uuid.UUID, dict[str, Any]]:
    result: dict[uuid.UUID, dict[str, Any]] = {}
    for home in plan.get("homes", []):
        h_uuid = parse_uuid(home.get("home_uuid"))
        if h_uuid is None:
            raise MigrationError(f"invalid home UUID in plan: {home.get('home_uuid')!r}")
        if h_uuid in result:
            raise MigrationError(f"duplicate home UUID in plan: {h_uuid}")
        result[h_uuid] = home
    return result


def check_plan_ready(plan: Mapping[str, Any], config: Optional[HuskHomesConfig] = None) -> None:
    errors = [i for i in plan.get("issues", []) if i.get("level") == "error"]
    blocked = [h for h in plan.get("homes", []) if h.get("action") == "blocked"]
    if errors or blocked:
        messages = [format_issue(i) for i in errors[:10]]
        messages += [
            f"[blocked] {h.get('owner_username')}/{h.get('name')!r}: {h.get('error', '')}"
            for h in blocked[:10]
        ]
        raise MigrationError(
            "plan contains blocking errors; refusing to apply:\n  " + "\n  ".join(messages)
        )
    if int(plan.get("source_home_count", -1)) != int(plan.get("planned_home_count", -2)):
        raise MigrationError("plan source/planned home counts do not match")
    if int(plan.get("planned_home_count", -1)) != len(plan.get("homes", [])):
        raise MigrationError("plan planned_home_count does not match the home list")

    # Structural checks. These defend against a plan that was edited by hand or
    # generated by a different/incompatible tool version.
    users = plan.get("users", [])
    user_uuids: set[uuid.UUID] = set()
    usernames: dict[str, uuid.UUID] = {}
    for user in users:
        user_uuid = parse_uuid(user.get("uuid"))
        username = str(user.get("username") or "")
        if user_uuid is None:
            raise MigrationError(f"plan has an invalid user UUID: {user.get('uuid')!r}")
        if user_uuid in user_uuids:
            raise MigrationError(f"plan has duplicate user UUID {user_uuid}")
        user_uuids.add(user_uuid)
        if not username or len(username) > HUSKHOMES_MAX_NAME_LENGTH:
            raise MigrationError(f"plan has an invalid username for {user_uuid}: {username!r}")
        key = username.casefold()
        if key in usernames and usernames[key] != user_uuid:
            raise MigrationError(
                f"plan assigns username {username!r} to two UUIDs: "
                f"{usernames[key]} and {user_uuid}"
            )
        usernames[key] = user_uuid

    home_uuids: set[uuid.UUID] = set()
    owner_names: set[tuple[uuid.UUID, str]] = set()
    name_regex = re.compile(config.name_regex) if config else None
    for home in plan.get("homes", []):
        action = str(home.get("action"))
        h_uuid = parse_uuid(home.get("home_uuid"))
        owner_uuid = parse_uuid(home.get("owner_uuid"))
        if h_uuid is None:
            raise MigrationError(f"plan has an invalid home UUID: {home.get('home_uuid')!r}")
        if h_uuid in home_uuids:
            raise MigrationError(f"plan has duplicate home UUID {h_uuid}")
        home_uuids.add(h_uuid)
        if owner_uuid is None or owner_uuid not in user_uuids:
            raise MigrationError(f"home {h_uuid} has an owner UUID not present in plan users")
        if action not in ("create", "update", "skip"):
            raise MigrationError(f"home {h_uuid} has unsupported action {action!r}")
        if action == "skip":
            continue

        name = str(home.get("name") or "")
        if not name or len(name) > HUSKHOMES_MAX_NAME_LENGTH:
            raise MigrationError(f"home {h_uuid} has an invalid name: {name!r}")
        if "\x00" in name or any(ch.isspace() for ch in name):
            raise MigrationError(f"home {h_uuid} name contains whitespace: {name!r}")
        if config:
            if config.home_delimiter and config.home_delimiter in name:
                raise MigrationError(
                    f"home {h_uuid} name contains the home delimiter {config.home_delimiter!r}: {name!r}"
                )
            if config.restrict_names and name_regex and not name_regex.fullmatch(name):
                raise MigrationError(
                    f"home {h_uuid} name does not match the configured regex: {name!r}"
                )
        name_key = name.casefold() if (
            config and (config.case_insensitive_names or config.backend in ("MYSQL", "MARIADB"))
        ) else (name.casefold() if config is None else name)
        key = (owner_uuid, name_key)
        if key in owner_names:
            raise MigrationError(
                f"plan assigns home name {name!r} twice to owner {owner_uuid}"
            )
        owner_names.add(key)

        position = home.get("position")
        if not isinstance(position, dict):
            raise MigrationError(f"home {h_uuid} has no position")
        for field in ("x", "y", "z", "yaw", "pitch"):
            if field not in position:
                raise MigrationError(f"home {h_uuid} position is missing {field!r}")
            to_float(position[field], f"home {h_uuid} {field}")
        if not str(position.get("world_name") or ""):
            raise MigrationError(f"home {h_uuid} has no world name")
        if parse_uuid(position.get("world_uuid")) is None:
            raise MigrationError(f"home {h_uuid} has an invalid world UUID")
        if not str(position.get("server_name") or ""):
            raise MigrationError(f"home {h_uuid} has no server name")


def backup_sqlite(path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    destination = backup_dir / f"{path.name}.{stamp}.bak"
    source = sqlite3.connect(str(path))
    try:
        target = sqlite3.connect(str(destination))
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
    finally:
        source.close()
    return destination


def backup_mysql(config: HuskHomesConfig, backup_dir: Path) -> Path:
    mysqldump = shutil.which("mysqldump")
    if not mysqldump:
        raise MigrationError(
            "mysqldump was not found, so no MySQL backup can be made. Install the MySQL/MariaDB "
            "client tools, or re-run with --no-backup only if you have your own verified backup."
        )
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    destination = backup_dir / f"{config.mysql_database}.{stamp}.sql"
    env = os.environ.copy()
    if config.mysql_password:
        env["MYSQL_PWD"] = config.mysql_password
    command = [
        mysqldump,
        "--single-transaction",
        "--skip-lock-tables",
        "--no-tablespaces",
        "--default-character-set=utf8mb4",
        f"--host={config.mysql_host}",
        f"--port={config.mysql_port}",
        f"--user={config.mysql_user}",
        str(config.mysql_database),
    ]
    with destination.open("wb") as f:
        result = subprocess.run(command, stdout=f, stderr=subprocess.PIPE, env=env, check=False)
    if result.returncode != 0:
        destination.unlink(missing_ok=True)
        raise MigrationError(
            f"mysqldump failed: {result.stderr.decode('utf-8', 'replace').strip()}"
        )
    if destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise MigrationError("mysqldump produced an empty backup; refusing to continue")
    return destination


def ensure_users_batch(
    database: HuskHomesDatabase,
    users: Sequence[Mapping[str, Any]],
) -> tuple[int, int]:
    validated: list[tuple[uuid.UUID, str]] = []
    for user in users:
        user_uuid = parse_uuid(user.get("uuid"))
        if user_uuid is None:
            raise MigrationError(f"invalid user UUID in plan: {user.get('uuid')!r}")
        username = str(user.get("username") or "")
        if not username or len(username) > 16:
            raise MigrationError(f"invalid username in plan for {user_uuid}: {username!r}")
        validated.append((user_uuid, username))

    if not validated:
        return 0, 0

    existing_users = database.fetch_users()
    by_lower_name: dict[str, set[uuid.UUID]] = defaultdict(set)
    for existing_uuid, row in existing_users.items():
        existing_name = str(row.get("username") or "")
        if existing_name:
            by_lower_name[existing_name.lower()].add(existing_uuid)

    to_insert: list[tuple[str, str]] = []
    to_update: list[tuple[str, Sequence[Any]]] = []

    for user_uuid, username in validated:
        lower_name = username.lower()
        other_owners = by_lower_name.get(lower_name, set()) - {user_uuid}
        if other_owners:
            conflict_uuid = sorted(map(str, other_owners))[0]
            raise MigrationError(
                f"HuskHomes already has username {username!r} under UUID {conflict_uuid}; "
                f"refusing to create a duplicate-name user for {user_uuid}"
            )

        existing = existing_users.get(user_uuid)
        if existing is None:
            to_insert.append((str(user_uuid), username))
            by_lower_name[lower_name].add(user_uuid)
            existing_users[user_uuid] = {"uuid": str(user_uuid), "username": username}
        else:
            old_name = str(existing.get("username") or "")
            if old_name != username:
                to_update.append((str(user_uuid), (username,)))
                if old_name:
                    by_lower_name[old_name.lower()].discard(user_uuid)
                by_lower_name[lower_name].add(user_uuid)
                existing_users[user_uuid] = {"uuid": str(user_uuid), "username": username}

    database.insert_rows("PLAYER_DATA", ("uuid", "username"), to_insert)
    database.update_rows_by_key("PLAYER_DATA", "uuid", ("username",), to_update)
    return len(to_insert), len(to_update)


def fetch_home_rows_by_uuids(
    database: HuskHomesDatabase,
    home_uuids: Sequence[str],
) -> dict[uuid.UUID, dict[str, Any]]:
    rows = database.query_in_chunks(
        f"""
        SELECT h.`uuid` AS home_uuid, h.`owner_uuid`, h.`public`,
               h.`saved_position_id`, s.`position_id`,
               s.`name`, s.`description`, s.`tags`,
               p.`x`, p.`y`, p.`z`, p.`yaw`, p.`pitch`,
               p.`world_name`, p.`world_uuid`, p.`server_name`
        FROM {database.table('HOME_DATA')} h
        INNER JOIN {database.table('SAVED_POSITION_DATA')} s ON h.`saved_position_id` = s.`id`
        INNER JOIN {database.table('POSITION_DATA')} p ON s.`position_id` = p.`id`
        WHERE h.`uuid` IN ({{placeholders}})
        """,
        home_uuids,
    )
    result: dict[uuid.UUID, dict[str, Any]] = {}
    for row in rows:
        h_uuid = parse_uuid(row.get("home_uuid"))
        if h_uuid is None:
            raise MigrationError("invalid UUID found in HuskHomes home data")
        result[h_uuid] = row
    return result


def write_homes_batch(
    database: HuskHomesDatabase,
    homes: Sequence[Mapping[str, Any]],
    default_timestamp: Optional[str] = None,
) -> tuple[int, int, int]:
    if not homes:
        return 0, 0, 0

    lookup_uuids: list[str] = []
    for home in homes:
        action = str(home.get("action"))
        if action not in ("create", "update", "skip"):
            raise MigrationError(f"unsupported plan action {action!r}")
        h_uuid = uuid.UUID(str(home["home_uuid"]))
        lookup_uuids.append(str(h_uuid))
        if action == "skip" and home.get("existing"):
            lookup_uuids.append(str(uuid.UUID(str(home["existing"]["home_uuid"]))))

    existing_by_uuid = fetch_home_rows_by_uuids(database, lookup_uuids)

    creates: list[Mapping[str, Any]] = []
    updates: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    skip_count = 0

    for home in homes:
        action = str(home.get("action"))
        h_uuid = uuid.UUID(str(home["home_uuid"]))
        existing = existing_by_uuid.get(h_uuid)
        if action == "create":
            if existing is not None:
                raise MigrationError(f"plan wanted to create home {h_uuid}, but it already exists")
            creates.append(home)
        elif action == "update":
            if existing is None:
                raise MigrationError(f"plan wanted to update home {h_uuid}, but it does not exist")
            updates.append((home, existing))
        elif action == "skip":
            skip_existing = home.get("existing")
            skip_uuid = uuid.UUID(str(skip_existing["home_uuid"])) if skip_existing else h_uuid
            verify_home_row(home, existing_by_uuid.get(skip_uuid))
            skip_count += 1

    fallback_ts = default_timestamp or sql_timestamp_now()

    if creates:
        max_pos_row = database.query_one(
            f"SELECT COALESCE(MAX(`id`), 0) AS max_id FROM {database.table('POSITION_DATA')}"
        )
        max_saved_row = database.query_one(
            f"SELECT COALESCE(MAX(`id`), 0) AS max_id FROM {database.table('SAVED_POSITION_DATA')}"
        )
        next_pos_id = int((max_pos_row or {}).get("max_id") or 0) + 1
        next_saved_id = int((max_saved_row or {}).get("max_id") or 0) + 1

        pos_rows: list[tuple[Any, ...]] = []
        saved_rows: list[tuple[Any, ...]] = []
        home_rows: list[tuple[Any, ...]] = []

        for offset, home in enumerate(creates):
            pos_id = next_pos_id + offset
            saved_id = next_saved_id + offset
            h_uuid = uuid.UUID(str(home["home_uuid"]))
            owner_uuid = uuid.UUID(str(home["owner_uuid"]))
            name = str(home["name"])
            position = home["position"]

            pos_rows.append((
                float(position["x"]), float(position["y"]), float(position["z"]),
                float(position["yaw"]), float(position["pitch"]),
                str(position["world_name"]), str(position["world_uuid"]),
                str(position["server_name"]), pos_id,
            ))
            saved_rows.append((
                pos_id, name, "", None, str(home.get("timestamp") or fallback_ts), saved_id,
            ))
            home_rows.append((str(h_uuid), saved_id, str(owner_uuid), 0))

        database.insert_rows(
            "POSITION_DATA",
            ("x", "y", "z", "yaw", "pitch", "world_name", "world_uuid", "server_name", "id"),
            pos_rows,
        )
        database.insert_rows(
            "SAVED_POSITION_DATA",
            ("position_id", "name", "description", "tags", "timestamp", "id"),
            saved_rows,
        )
        database.insert_rows(
            "HOME_DATA",
            ("uuid", "saved_position_id", "owner_uuid", "public"),
            home_rows,
        )

    if updates:
        pos_updates: list[tuple[int, Sequence[Any]]] = []
        saved_updates: list[tuple[int, Sequence[Any]]] = []
        home_updates: list[tuple[str, Sequence[Any]]] = []

        for home, existing in updates:
            h_uuid = uuid.UUID(str(home["home_uuid"]))
            owner_uuid = uuid.UUID(str(home["owner_uuid"]))
            name = str(home["name"])
            position = home["position"]
            position_id = int(existing["position_id"])
            saved_position_id = int(existing["saved_position_id"])

            pos_updates.append((
                position_id,
                (
                    float(position["x"]), float(position["y"]), float(position["z"]),
                    float(position["yaw"]), float(position["pitch"]),
                    str(position["world_name"]), str(position["world_uuid"]),
                    str(position["server_name"]),
                ),
            ))
            saved_updates.append((saved_position_id, (name, "", None)))
            home_updates.append((str(h_uuid), (str(owner_uuid), 0)))

        database.update_rows_by_key(
            "POSITION_DATA",
            "id",
            ("x", "y", "z", "yaw", "pitch", "world_name", "world_uuid", "server_name"),
            pos_updates,
        )
        database.update_rows_by_key(
            "SAVED_POSITION_DATA",
            "id",
            ("name", "description", "tags"),
            saved_updates,
        )
        database.update_rows_by_key(
            "HOME_DATA",
            "uuid",
            ("owner_uuid", "public"),
            home_updates,
        )

    return len(creates), len(updates), skip_count


def verify_home_row(
    home: Mapping[str, Any],
    row: Optional[Mapping[str, Any]],
) -> None:
    h_uuid = uuid.UUID(str(home["home_uuid"]))
    existing = home.get("existing") if home.get("action") == "skip" else None
    lookup_uuid = uuid.UUID(str(existing["home_uuid"])) if existing else h_uuid
    if row is None:
        raise MigrationError(f"verification failed: home {lookup_uuid} is missing")

    if home.get("action") == "skip":
        # A policy skip intentionally leaves an existing home untouched. When
        # the plan recorded the conflicting home, prove it is still exactly
        # what we saw; otherwise there is nothing database-side to verify.
        if not existing:
            return
        expected = existing["position"]
        expected_owner = str(existing["owner_uuid"])
        expected_name = str(existing["name"])
    else:
        expected = home["position"]
        expected_owner = str(home["owner_uuid"])
        expected_name = str(home["name"])

    checks = [
        ("owner_uuid", str(row.get("owner_uuid")), expected_owner),
        ("name", str(row.get("name")), expected_name),
        ("x", float(row["x"]), float(expected["x"])),
        ("y", float(row["y"]), float(expected["y"])),
        ("z", float(row["z"]), float(expected["z"])),
        ("yaw", float(row["yaw"]), float(expected["yaw"])),
        ("pitch", float(row["pitch"]), float(expected["pitch"])),
        ("world_name", str(row.get("world_name")), str(expected["world_name"])),
        ("world_uuid", str(row.get("world_uuid")), str(expected["world_uuid"])),
        ("server_name", str(row.get("server_name")), str(expected["server_name"])),
        ("public", bool(row.get("public")), False),
    ]
    for label, actual, wanted in checks:
        if label in ("yaw", "pitch"):
            if not float32_equal(actual, wanted):
                raise MigrationError(
                    f"verification failed for home {h_uuid}: {label}={actual!r}, expected {wanted!r}"
                )
        elif isinstance(wanted, float):
            if not floats_equal(actual, wanted):
                raise MigrationError(
                    f"verification failed for home {h_uuid}: {label}={actual!r}, expected {wanted!r}"
                )
        elif actual != wanted:
            raise MigrationError(
                f"verification failed for home {h_uuid}: {label}={actual!r}, expected {wanted!r}"
            )


def verify_plan_in_database(database: HuskHomesDatabase, plan: Mapping[str, Any]) -> None:
    homes = plan.get("homes", [])
    lookup_uuids: list[str] = []
    for home in homes:
        if home.get("action") == "blocked":
            raise MigrationError(f"blocked home in plan: {home.get('home_uuid')}")
        h_uuid = uuid.UUID(str(home["home_uuid"]))
        existing = home.get("existing") if home.get("action") == "skip" else None
        lookup_uuid = uuid.UUID(str(existing["home_uuid"])) if existing else h_uuid
        lookup_uuids.append(str(lookup_uuid))

    home_rows = fetch_home_rows_by_uuids(database, lookup_uuids)
    for home in homes:
        h_uuid = uuid.UUID(str(home["home_uuid"]))
        existing = home.get("existing") if home.get("action") == "skip" else None
        lookup_uuid = uuid.UUID(str(existing["home_uuid"])) if existing else h_uuid
        verify_home_row(home, home_rows.get(lookup_uuid))

    # Verify users too.
    users = plan.get("users", [])
    user_uuids: list[uuid.UUID] = []
    for user in users:
        user_uuid = parse_uuid(user.get("uuid"))
        if user_uuid is None:
            raise MigrationError(f"invalid user UUID in plan: {user.get('uuid')!r}")
        user_uuids.append(user_uuid)

    user_rows = database.query_in_chunks(
        f"SELECT `uuid`, `username` FROM {database.table('PLAYER_DATA')} WHERE `uuid` IN ({{placeholders}})",
        [str(u) for u in user_uuids],
    )
    db_users: dict[uuid.UUID, dict[str, Any]] = {}
    for row in user_rows:
        row_uuid = parse_uuid(row.get("uuid"))
        if row_uuid is not None:
            db_users[row_uuid] = row

    for user, user_uuid in zip(users, user_uuids):
        row = db_users.get(user_uuid)
        if row is None:
            raise MigrationError(f"verification failed: user {user_uuid} is missing")
        if str(row.get("username")) != str(user.get("username")):
            raise MigrationError(
                f"verification failed: user {user_uuid} has username {row.get('username')!r}, "
                f"expected {user.get('username')!r}"
            )


def apply_plan(
    plan: Mapping[str, Any],
    config: HuskHomesConfig,
    backup_dir: Optional[Path],
    do_backup: bool,
    args: argparse.Namespace,
) -> None:
    check_plan_ready(plan, config)
    verify_source_manifest(plan)
    verify_nlogin_snapshot(plan, args)
    verify_target(plan, config)

    database = HuskHomesDatabase(config)
    database.connect()
    try:
        database.validate_schema()
        backup_path: Optional[Path] = None
        if do_backup:
            directory = backup_dir or Path.cwd() / "migration-output" / "backups"
            if config.backend == "SQLITE":
                assert config.sqlite_path is not None
                backup_path = backup_sqlite(config.sqlite_path, directory)
                log(f"Backup written to {backup_path}")
            else:
                backup_path = backup_mysql(config, directory)
                log(f"Backup written to {backup_path}")

        users = plan.get("users", [])
        homes = plan.get("homes", [])
        log(f"Applying {len(users)} user(s) and {len(homes)} home(s)...")

        database.begin()
        try:
            users_created, users_updated = ensure_users_batch(database, users)
            log(f"Users synced ({users_created} inserted, {users_updated} updated).")
            homes_created, homes_updated, homes_skipped = write_homes_batch(
                database,
                homes,
                default_timestamp=str(plan.get("database_timestamp") or sql_timestamp_now()),
            )
            log(
                f"Homes written ({homes_created} created, {homes_updated} updated, "
                f"{homes_skipped} skipped). Verifying before commit..."
            )
            verify_plan_in_database(database, plan)
            database.commit()
            log("Transaction committed. Running post-commit verification...")
        except Exception:
            database.rollback()
            raise

        # Re-read after commit as a second, independent verification.
        verify_plan_in_database(database, plan)
    finally:
        database.close()


def verify_only(plan: Mapping[str, Any], config: HuskHomesConfig, args: argparse.Namespace) -> None:
    check_plan_ready(plan, config)
    verify_source_manifest(plan)
    verify_nlogin_snapshot(plan, args)
    verify_target(plan, config)
    database = HuskHomesDatabase(config)
    database.connect(read_only=True)
    try:
        database.validate_schema()
        verify_plan_in_database(database, plan)
    finally:
        database.close()


# ---------------------------------------------------------------------------
# Scan orchestration
# ---------------------------------------------------------------------------


def parse_world_overrides(values: Optional[Sequence[str]]) -> dict[str, uuid.UUID]:
    result: dict[str, uuid.UUID] = {}
    for value in values or []:
        if "=" not in value:
            raise MigrationError(f"--world-uuid must be NAME=UUID, got {value!r}")
        name, raw_uuid = value.split("=", 1)
        name = name.strip()
        world_uuid = parse_uuid(raw_uuid)
        if not name or world_uuid is None:
            raise MigrationError(f"invalid --world-uuid value: {value!r}")
        if name in result and result[name] != world_uuid:
            raise MigrationError(f"conflicting --world-uuid values for {name!r}")
        result[name] = world_uuid
    return result


def cmd_scan(args: argparse.Namespace) -> int:
    playerdata_dir = Path(args.playerdata).resolve()
    players, parse_issues = parse_ultimatehomes_playerdata(playerdata_dir)

    # nLogin
    if args.nlogin_csv:
        nlogin_rows = load_nlogin_csv(Path(args.nlogin_csv))
    else:
        if not args.nlogin_mysql_host or not args.nlogin_mysql_database or not args.nlogin_mysql_user:
            raise MigrationError(
                "nLogin MySQL requires --nlogin-mysql-host, --nlogin-mysql-database and "
                "--nlogin-mysql-user (or use --nlogin-csv)"
            )
        nlogin_rows = load_nlogin_mysql(args)
    nlogin = build_nlogin_index(nlogin_rows)
    if args.require_nlogin_match:
        # The per-player check is in build_plan; this is just a fast sanity check.
        pass

    overrides = load_uuid_overrides(Path(args.uuid_map) if args.uuid_map else None)
    world_overrides = parse_world_overrides(args.world_uuid)

    config = read_huskhomes_config(Path(args.huskhomes_config).resolve(), args)
    database = HuskHomesDatabase(config)
    database.connect(read_only=True)
    try:
        database.validate_schema()
        plan = build_plan(
            players=players,
            parse_issues=parse_issues,
            nlogin=nlogin,
            overrides=overrides,
            config=config,
            database=database,
            on_conflict=args.on_conflict,
            world_overrides=world_overrides,
            require_nlogin_match=args.require_nlogin_match,
            playerdata_dir=playerdata_dir,
        )
    finally:
        database.close()

    plan["source"] = {
        "playerdata_dir": str(playerdata_dir),
        "files": {
            player.relative_file: {
                "sha256": player.sha256,
                "homes": len(player.homes),
                "player_name": player.last_seen_as,
            }
            for player in players
        },
        "nlogin_rows": len(nlogin_rows),
    }
    plan["nlogin"] = {
        "source": nlogin_source_from_args(args),
        "snapshot": nlogin_snapshot(nlogin_rows),
    }

    output = Path(args.output).resolve()
    atomic_write_json(output, plan)
    report = Path(args.report).resolve() if args.report else output.with_suffix(".md")
    write_report(report, plan)
    csv_path = output.with_suffix(".csv")
    write_homes_csv(csv_path, plan)

    summarize_plan(plan)
    log("")
    log(f"Plan:   {output}")
    log(f"Report: {report}")
    log(f"CSV:    {csv_path}")
    if (plan.get("summary") or {}).get("errors"):
        return 2
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    plan = load_plan(Path(args.plan).resolve())
    config = read_huskhomes_config(Path(args.huskhomes_config).resolve(), args)
    backup_dir = Path(args.backup_dir).resolve() if args.backup_dir else None
    apply_plan(plan, config, backup_dir, do_backup=not args.no_backup, args=args)
    log("Apply complete. Post-commit verification passed.")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    plan = load_plan(Path(args.plan).resolve())
    config = read_huskhomes_config(Path(args.huskhomes_config).resolve(), args)
    verify_only(plan, config, args)
    log("Verification passed: every home and user in the plan is present and correct.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def resolve_config_path(base_dir: Path, raw: Any) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return str(path)


def load_migration_config(path: Path) -> tuple[Path, dict[str, Any]]:
    if not path.is_file():
        raise MigrationError(f"migration config file not found: {path}")
    raw = load_yaml(path)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise MigrationError(f"migration config must be a YAML mapping: {path}")
    return path.resolve().parent, raw


def apply_migration_config(args: argparse.Namespace) -> None:
    args._nlogin_password_from_cli = bool(getattr(args, "nlogin_password", None))
    args._huskhomes_password_from_cli = bool(getattr(args, "huskhomes_password", None))
    args.nlogin_config_password = None
    args.huskhomes_config_password = None

    config_file = getattr(args, "config", None)
    if config_file:
        base_dir, cfg = load_migration_config(Path(config_file).resolve())
    else:
        base_dir, cfg = Path.cwd(), {}

    uh = cfg.get("ultimatehomes") or {}
    if not isinstance(uh, dict):
        raise MigrationError("migration config 'ultimatehomes' must be a mapping")
    hh = cfg.get("huskhomes") or {}
    if not isinstance(hh, dict):
        raise MigrationError("migration config 'huskhomes' must be a mapping")
    hh_mysql = hh.get("mysql") or {}
    if not isinstance(hh_mysql, dict):
        raise MigrationError("migration config 'huskhomes.mysql' must be a mapping")
    nl = cfg.get("nlogin") or {}
    if not isinstance(nl, dict):
        raise MigrationError("migration config 'nlogin' must be a mapping")
    nl_mysql = nl.get("mysql") or {}
    if not isinstance(nl_mysql, dict):
        raise MigrationError("migration config 'nlogin.mysql' must be a mapping")
    nl_cols = nl.get("columns") or {}
    if not isinstance(nl_cols, dict):
        raise MigrationError("migration config 'nlogin.columns' must be a mapping")
    mig = cfg.get("migration") or {}
    if not isinstance(mig, dict):
        raise MigrationError("migration config 'migration' must be a mapping")

    def pick(attr: str, fallback: Any, default: Any = None) -> None:
        if getattr(args, attr, None) is None:
            setattr(args, attr, fallback if fallback is not None else default)

    # Shared HuskHomes target settings
    pick("huskhomes_config", resolve_config_path(base_dir, hh.get("config")))
    pick("huskhomes_backend", str(hh["backend"]).upper() if hh.get("backend") else None)
    pick("huskhomes_sqlite", resolve_config_path(base_dir, hh.get("sqlite")))
    pick("huskhomes_mysql_host", hh_mysql.get("host"))
    pick("huskhomes_mysql_port", int(hh_mysql["port"]) if hh_mysql.get("port") is not None else None)
    pick("huskhomes_mysql_database", hh_mysql.get("database"))
    pick("huskhomes_mysql_user", hh_mysql.get("user"))
    pick("huskhomes_password_env", hh_mysql.get("password_env"), "HUSKHOMES_DB_PASSWORD")
    if hh_mysql.get("password") not in (None, ""):
        args.huskhomes_config_password = str(hh_mysql.get("password"))
    pick("server_name", hh.get("server_name"), DEFAULT_SERVER_NAME)

    # nLogin verification/connection password settings
    pick("nlogin_password_env", nl_mysql.get("password_env"), "NLOGIN_DB_PASSWORD")
    if nl_mysql.get("password") not in (None, ""):
        args.nlogin_config_password = str(nl_mysql.get("password"))

    if args.command == "scan":
        pick("playerdata", resolve_config_path(base_dir, uh.get("playerdata")))
        pick("output", resolve_config_path(base_dir, mig.get("output")))
        pick("report", resolve_config_path(base_dir, mig.get("report")))
        pick("uuid_map", resolve_config_path(base_dir, mig.get("uuid_map")))
        pick("on_conflict", mig.get("on_conflict"), "fail")
        if args.on_conflict not in ("fail", "rename", "skip"):
            raise MigrationError(f"invalid migration.on_conflict in config: {args.on_conflict!r}")
        if args.require_nlogin_match is None:
            args.require_nlogin_match = bool(mig.get("require_nlogin_match", False))

        cfg_worlds = mig.get("world_uuids") or {}
        if cfg_worlds:
            if not isinstance(cfg_worlds, dict):
                raise MigrationError("migration config 'migration.world_uuids' must be a mapping")
            combined_worlds = [f"{k}={v}" for k, v in cfg_worlds.items()]
            combined_worlds.extend(args.world_uuid or [])
            args.world_uuid = combined_worlds

        pick("nlogin_csv", resolve_config_path(base_dir, nl.get("csv")))
        pick("nlogin_mysql_host", nl_mysql.get("host"))
        pick("nlogin_mysql_port", int(nl_mysql["port"]) if nl_mysql.get("port") is not None else None, 3306)
        pick("nlogin_mysql_database", nl_mysql.get("database"))
        pick("nlogin_mysql_user", nl_mysql.get("user"))
        pick("nlogin_table", nl.get("table"), "nlogin")
        pick("nlogin_name_column", nl_cols.get("name"), "last_name")
        pick("nlogin_unique_id_column", nl_cols.get("unique_id"), "unique_id")
        pick("nlogin_mojang_id_column", nl_cols.get("mojang_id"), "mojang_id")
        pick("nlogin_bedrock_id_column", nl_cols.get("bedrock_id"), "bedrock_id")

        if not args.playerdata:
            raise MigrationError("missing --playerdata (or ultimatehomes.playerdata in --config)")
        if not args.output:
            raise MigrationError("missing --output (or migration.output in --config)")
    elif args.command in ("apply", "verify"):
        pick("plan", resolve_config_path(base_dir, mig.get("plan") or mig.get("output")))
        if args.command == "apply":
            pick("backup_dir", resolve_config_path(base_dir, mig.get("backup_dir")))
        if not args.plan:
            raise MigrationError("missing --plan (or migration.output in --config)")

    if not args.huskhomes_config:
        raise MigrationError("missing --huskhomes-config (or huskhomes.config in --config)")


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-c", "--config",
        help="Path to migration YAML config file (CLI flags override config values)",
    )


def add_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--huskhomes-config",
        help="Path to plugins/HuskHomes/config.yml",
    )
    parser.add_argument(
        "--huskhomes-backend", choices=("SQLITE", "MYSQL", "MARIADB"),
        help="Override database.type from HuskHomes config.yml",
    )
    parser.add_argument(
        "--huskhomes-sqlite",
        help="Path to HuskHomesData.db (defaults to the config directory)",
    )
    parser.add_argument("--huskhomes-mysql-host", help="Override HuskHomes MySQL host")
    parser.add_argument("--huskhomes-mysql-port", type=int, help="Override HuskHomes MySQL port")
    parser.add_argument("--huskhomes-mysql-database", help="Override HuskHomes MySQL database")
    parser.add_argument("--huskhomes-mysql-user", help="Override HuskHomes MySQL user")
    parser.add_argument(
        "--huskhomes-password-env", default=None,
        help="Environment variable containing the HuskHomes MySQL password (default: HUSKHOMES_DB_PASSWORD)",
    )
    parser.add_argument(
        "--huskhomes-password",
        help="HuskHomes MySQL password (prefer --config or --huskhomes-password-env)",
    )
    parser.add_argument(
        "--server-name", default=None,
        help=f"HuskHomes server_name for new positions (default: {DEFAULT_SERVER_NAME})",
    )


def add_nlogin_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--nlogin-csv", help="Use a CSV export instead of connecting to MySQL")
    parser.add_argument("--nlogin-mysql-host", help="nLogin MySQL host")
    parser.add_argument("--nlogin-mysql-port", type=int, default=None, help="nLogin MySQL port (default: 3306)")
    parser.add_argument("--nlogin-mysql-database", help="nLogin MySQL database name")
    parser.add_argument("--nlogin-mysql-user", help="nLogin MySQL user")
    parser.add_argument(
        "--nlogin-password-env", default=None,
        help="Environment variable containing the nLogin MySQL password (default: NLOGIN_DB_PASSWORD)",
    )
    parser.add_argument(
        "--nlogin-password",
        help="nLogin MySQL password (prefer --config or --nlogin-password-env)",
    )
    parser.add_argument("--nlogin-table", default=None, help="nLogin accounts table (default: nlogin)")
    parser.add_argument("--nlogin-name-column", default=None, help="nLogin name column (default: last_name)")
    parser.add_argument("--nlogin-unique-id-column", default=None, help="nLogin unique_id column (default: unique_id)")
    parser.add_argument("--nlogin-mojang-id-column", default=None, help="nLogin mojang_id column (default: mojang_id)")
    parser.add_argument("--nlogin-bedrock-id-column", default=None, help="nLogin bedrock_id column (default: bedrock_id)")


def add_nlogin_verify_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--nlogin-password-env", default=None,
        help="Environment variable containing the nLogin MySQL password (default: NLOGIN_DB_PASSWORD)",
    )
    parser.add_argument(
        "--nlogin-password",
        help="nLogin MySQL password (prefer --config or --nlogin-password-env)",
    )
    parser.add_argument(
        "--skip-nlogin-verify", action="store_true",
        help="Do not re-check that nLogin's data is unchanged since scan (not recommended)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate UltimateHomes homes to HuskHomes (plan-first, no deletions).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Typical use:\n"
            "  1. stop the server\n"
            "  2. cp migration.example.yml migration.yml && chmod 600 migration.yml\n"
            "  3. python3 migrate_ultimatehomes.py scan --config migration.yml\n"
            "  4. read the generated report\n"
            "  5. python3 migrate_ultimatehomes.py apply --config migration.yml\n"
            "  6. python3 migrate_ultimatehomes.py verify --config migration.yml\n"
        ),
    )
    parser.add_argument("--debug", action="store_true", help="Print a traceback on unexpected errors")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="Read source/target data and write a migration plan")
    add_config_argument(scan)
    scan.add_argument("--playerdata", help="UltimateHomes playerdata directory")
    scan.add_argument("--output", help="Where to write the plan JSON")
    scan.add_argument("--report", help="Where to write the Markdown report (default: plan.md)")
    scan.add_argument("--uuid-map", help="Optional CSV: source_uuid,target_uuid[,username]")
    scan.add_argument(
        "--on-conflict", choices=("fail", "rename", "skip"), default=None,
        help="What to do when a home name conflicts (default: fail)",
    )
    scan.add_argument(
        "--world-uuid", action="append",
        help="Override a world UUID: NAME=UUID (repeatable)",
    )
    scan.add_argument(
        "--require-nlogin-match", action="store_true", default=None,
        help="Fail if a player with homes has no matching nLogin account",
    )
    add_target_arguments(scan)
    add_nlogin_arguments(scan)

    apply = subparsers.add_parser("apply", help="Apply a previously scanned plan")
    add_config_argument(apply)
    apply.add_argument("--plan", help="Plan JSON from the scan command")
    apply.add_argument("--backup-dir", help="Directory for pre-apply backups")
    apply.add_argument("--no-backup", action="store_true", help="Do not create a backup")
    add_target_arguments(apply)
    add_nlogin_verify_arguments(apply)

    verify = subparsers.add_parser("verify", help="Verify that an applied plan matches the database")
    add_config_argument(verify)
    verify.add_argument("--plan", help="Plan JSON from the scan command")
    add_target_arguments(verify)
    add_nlogin_verify_arguments(verify)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        apply_migration_config(args)
        if args.command == "scan":
            return cmd_scan(args)
        if args.command == "apply":
            return cmd_apply(args)
        if args.command == "verify":
            return cmd_verify(args)
        parser.error(f"unknown command {args.command!r}")
    except MigrationError as exc:
        eprint(f"ERROR: {exc}")
        return 1
    except KeyboardInterrupt:
        eprint("Interrupted.")
        return 130
    except Exception as exc:  # pragma: no cover - defensive
        if getattr(args, "debug", False):
            raise
        eprint(f"ERROR: unexpected failure: {exc}")
        eprint("Re-run with --debug for a traceback.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
