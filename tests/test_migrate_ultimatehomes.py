import csv
import importlib.util
import json
import re
import sqlite3
import struct
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

import yaml

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "migrate_ultimatehomes.py"
SPEC = importlib.util.spec_from_file_location("migrate_ultimatehomes", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MIGRATE = importlib.util.module_from_spec(SPEC)
sys.modules["migrate_ultimatehomes"] = MIGRATE
SPEC.loader.exec_module(MIGRATE)


HUSKHOMES_SQLITE_SCHEMA = """
CREATE TABLE `huskhomes_metadata` (`schema_version` integer NOT NULL, PRIMARY KEY (`schema_version`));
INSERT INTO `huskhomes_metadata` VALUES (0);
CREATE TABLE `huskhomes_position_data` (
    `id` integer NOT NULL,
    `x` double NOT NULL,
    `y` double NOT NULL,
    `z` double NOT NULL,
    `yaw` float NOT NULL,
    `pitch` float NOT NULL,
    `world_name` varchar(255) NOT NULL,
    `world_uuid` char(36) NOT NULL,
    `server_name` varchar(255) NOT NULL,
    PRIMARY KEY (`id`)
);
CREATE TABLE `huskhomes_users` (
    `uuid` char(36) NOT NULL UNIQUE,
    `username` varchar(16) NOT NULL,
    `last_position` integer DEFAULT NULL,
    `offline_position` integer DEFAULT NULL,
    `respawn_position` integer DEFAULT NULL,
    `home_slots` integer NOT NULL DEFAULT 0,
    `ignoring_requests` boolean NOT NULL DEFAULT FALSE,
    PRIMARY KEY (`uuid`)
);
CREATE TABLE `huskhomes_saved_positions` (
    `id` integer NOT NULL,
    `position_id` integer NOT NULL,
    `name` varchar(64) NOT NULL,
    `description` varchar(255) NOT NULL,
    `tags` mediumtext DEFAULT NULL,
    `timestamp` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    FOREIGN KEY (`position_id`) REFERENCES `huskhomes_position_data` (`id`) ON DELETE CASCADE
);
CREATE TABLE `huskhomes_homes` (
    `uuid` char(36) NOT NULL UNIQUE,
    `saved_position_id` integer NOT NULL,
    `owner_uuid` char(36) NOT NULL,
    `public` boolean NOT NULL DEFAULT FALSE,
    PRIMARY KEY (`uuid`),
    FOREIGN KEY (`owner_uuid`) REFERENCES `huskhomes_users` (`uuid`) ON DELETE CASCADE,
    FOREIGN KEY (`saved_position_id`) REFERENCES `huskhomes_saved_positions` (`id`) ON DELETE CASCADE
);
"""


def write_huskhomes_config(path: Path, backend: str = "SQLITE", **extra: object) -> None:
    cfg = {
        "database": {
            "type": backend,
            "credentials": {
                "host": "db.example.local",
                "port": 3306,
                "database": "huskhomes",
                "username": "hh_user",
                "password": "secret",
            },
            "table_names": dict(MIGRATE.DEFAULT_TABLES),
        },
        "general": {
            "max_homes": 5,
            "home_delimiter": ".",
            "names": {
                "case_insensitive": False,
                "restrict": True,
                "regex": "[a-zA-Z0-9-_]*",
            },
        },
        "economy": {"enabled": False, "free_home_slots": 5},
    }
    if "case_insensitive" in extra:
        cfg["general"]["names"]["case_insensitive"] = extra["case_insensitive"]
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")


def create_sqlite_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(HUSKHOMES_SQLITE_SCHEMA)
    conn.commit()
    conn.close()


class FakeMySQLCursor:
    def __init__(self, conn: sqlite3.Connection, db_name: str, engine: str = "InnoDB", float32_cols: bool = True, counter=None):
        self.conn = conn
        self.db_name = db_name
        self.engine = engine
        self.float32_cols = float32_cols
        self.counter = counter
        self.lastrowid = None
        self._rows: list[dict] = []

    def _round_position_params(self, normalized: str, params):
        if not self.float32_cols:
            return params
        if "INSERT INTO `huskhomes_position_data`" in normalized:
            cols_match = re.search(r"INSERT INTO `huskhomes_position_data`\s*\(([^)]+)\)", normalized)
            if cols_match:
                cols = [c.strip().strip("`") for c in cols_match.group(1).split(",")]
                yaw_idx = cols.index("yaw")
                pitch_idx = cols.index("pitch")
                stride = len(cols)
                params = list(params)
                for base in range(0, len(params), stride):
                    params[base + yaw_idx] = struct.unpack("f", struct.pack("f", float(params[base + yaw_idx])))[0]
                    params[base + pitch_idx] = struct.unpack("f", struct.pack("f", float(params[base + pitch_idx])))[0]
                return tuple(params)
        if "UPDATE `huskhomes_position_data`" in normalized:
            set_match = re.search(r"SET\s+(.+?)\s+WHERE\s+", normalized)
            if set_match:
                params = list(params)
                param_offset = 0
                for clause in set_match.group(1).split(", "):
                    col_name = clause.split("=")[0].strip().strip("`")
                    ph_count = clause.count("%s")
                    if col_name in ("yaw", "pitch"):
                        if "CASE " in clause:
                            for idx in range(1, ph_count, 2):
                                pos = param_offset + idx
                                params[pos] = struct.unpack("f", struct.pack("f", float(params[pos])))[0]
                        elif ph_count == 1:
                            params[param_offset] = struct.unpack("f", struct.pack("f", float(params[param_offset])))[0]
                    param_offset += ph_count
                return tuple(params)
        return params

    def execute(self, sql: str, params=()) -> None:
        if self.counter is not None:
            self.counter[0] += 1
        normalized = " ".join(sql.split())
        if "FROM information_schema.COLUMNS" in normalized:
            table_name = params[1]
            rows = self.conn.execute(f"PRAGMA table_info(`{table_name}`)").fetchall()
            self._rows = [{"COLUMN_NAME": r[1]} for r in rows]
            return
        if "FROM information_schema.TABLES" in normalized:
            table_name = params[1]
            exists = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
            ).fetchone()
            self._rows = [{"ENGINE": self.engine}] if exists else []
            return
        # Translate PyMySQL %s placeholders and LOWER/etc. for SQLite backing.
        sqlite_sql = sql.replace("%s", "?")
        params = self._round_position_params(normalized, params)
        cur = self.conn.execute(sqlite_sql, params)
        self.lastrowid = cur.lastrowid
        if cur.description:
            cols = [d[0] for d in cur.description]
            self._rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        else:
            self._rows = []

    def executemany(self, sql: str, seq_of_params) -> None:
        for params in seq_of_params:
            self.execute(sql, params)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeMySQLConnection:
    def __init__(self, sqlite_path: Path, db_name: str, engine: str = "InnoDB", nlogin_rows=None):
        self.sqlite_conn = sqlite3.connect(str(sqlite_path))
        self.db_name = db_name
        self.engine = engine
        self.nlogin_rows = nlogin_rows
        self.begun = False
        self.committed = 0
        self.rolled_back = 0
        self.query_count = [0]

    def cursor(self):
        return FakeMySQLCursor(self.sqlite_conn, self.db_name, engine=self.engine, counter=self.query_count)

    def begin(self):
        self.begun = True

    def commit(self):
        self.committed += 1
        self.sqlite_conn.commit()

    def rollback(self):
        self.rolled_back += 1
        self.sqlite_conn.rollback()

    def close(self):
        self.sqlite_conn.close()


class MigrationTests(unittest.TestCase):
    def test_parse_ultimatehomes_duplicate_keys_and_nested_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p_uuid = MIGRATE.offline_uuid("Alice")
            (root / f"{p_uuid}.yml").write_text(
                "player:\n  info:\n    lastSeenAs: Alice\nhomes:\n  '1':\n    4m:\n"
                "      world: world\n      x: 10\n      y: 64\n      z: -20\n      yaw: 90.0\n      pitch: 0.0\n",
                encoding="utf-8",
            )
            players, issues = MIGRATE.parse_ultimatehomes_playerdata(root)
            self.assertEqual(issues, [])
            self.assertEqual(len(players), 1)
            self.assertEqual(len(players[0].homes), 1)
            self.assertIn("recovered nested home data", players[0].homes[0].warnings[0])

            # Duplicate YAML key must fail explicitly.
            (root / f"{p_uuid}.yml").write_text(
                "homes:\n  base:\n    world: world\n    x: 1\n    y: 2\n    z: 3\n    yaw: 0\n    pitch: 0\n"
                "  base:\n    world: world\n    x: 9\n    y: 2\n    z: 3\n    yaw: 0\n    pitch: 0\n",
                encoding="utf-8",
            )
            _, dup_issues = MIGRATE.parse_ultimatehomes_playerdata(root)
            self.assertEqual(len(dup_issues), 1)
            self.assertEqual(dup_issues[0]["code"], "invalid_yaml")

    def test_nlogin_resolution_priorities_and_overrides(self) -> None:
        premium_uuid = uuid.uuid4()
        mojang_uuid = uuid.uuid4()
        rows = [
            {"last_name": "Alice", "unique_id": None, "mojang_id": None, "bedrock_id": None},
            {"last_name": "Bob", "unique_id": premium_uuid.hex, "mojang_id": mojang_uuid.hex, "bedrock_id": None},
        ]
        index = MIGRATE.build_nlogin_index(rows)
        # Bob's UltimateHomes file used mojang_uuid or offline uuid; both resolve to premium_uuid.
        res_mojang = MIGRATE.resolve_owner(mojang_uuid, "Bob", index, {})
        self.assertEqual(res_mojang.uuid, premium_uuid)
        self.assertEqual(res_mojang.username, "Bob")
        self.assertEqual(res_mojang.method, "nlogin_uuid_alias")

        res_off = MIGRATE.resolve_owner(MIGRATE.offline_uuid("Alice"), "Alice", index, {})
        self.assertEqual(res_off.uuid, MIGRATE.offline_uuid("Alice"))

        # Manual override wins over everything.
        forced = uuid.uuid4()
        override = {mojang_uuid: MIGRATE.ManualOverride(target_uuid=forced, username="Bobby")}
        res_ov = MIGRATE.resolve_owner(mojang_uuid, "Bob", index, override)
        self.assertEqual(res_ov.uuid, forced)
        self.assertEqual(res_ov.username, "Bobby")
        self.assertEqual(res_ov.method, "manual_override")

    def test_mysql_case_insensitive_name_allocator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.yml"
            write_huskhomes_config(cfg_path, backend="MYSQL", case_insensitive=False)
            args = MIGRATE.build_parser().parse_args([
                "verify", "--plan", "p.json", "--huskhomes-config", str(cfg_path),
            ])
            cfg = MIGRATE.read_huskhomes_config(cfg_path, args)
            alloc = MIGRATE.NameAllocator(cfg)
            owner = uuid.uuid4()
            h1 = uuid.uuid4()
            h2 = uuid.uuid4()
            name1, _, err1, _ = alloc.allocate(owner, "Base", owner, h1, "fail")
            self.assertEqual(name1, "Base")
            self.assertIsNone(err1)
            # On MySQL, "base" collides with "Base" even when config says case_insensitive: false.
            name2, warns2, err2, _ = alloc.allocate(owner, "base", owner, h2, "rename")
            self.assertIsNone(err2)
            self.assertNotEqual(name2.lower(), "base")
            self.assertLessEqual(len(name2), 16)
            self.assertTrue(any("collides" in w for w in warns2))

            # Long home name > 16 chars gets deterministically shortened.
            long_name, warns_long, err_long, _ = alloc.allocate(
                owner, "averyveryverylonghomename", owner, uuid.uuid4(), "fail"
            )
            self.assertIsNone(err_long)
            self.assertLessEqual(len(long_name), 16)
            self.assertTrue(any("not valid for HuskHomes" in w for w in warns_long))

    def test_sqlite_end_to_end_idempotent_and_tamper_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            playerdata = root / "playerdata"
            playerdata.mkdir()
            alice_uuid = MIGRATE.offline_uuid("Alice")
            (playerdata / f"{alice_uuid}.yml").write_text(
                "player:\n  info:\n    lastSeenAs: Alice\nhomes:\n  home:\n"
                "    x: 100\n    y: 65\n    z: -42\n    yaw: 178.52899\n    pitch: -12.5\n    world: world\n"
                "  superlonghomenameover16chars:\n"
                "    x: -5\n    y: 70\n    z: 9\n    yaw: 0.0\n    pitch: 90.0\n    world: world_nether\n",
                encoding="utf-8",
            )
            nlogin_csv = root / "nlogin.csv"
            nlogin_csv.write_text("last_name,unique_id,mojang_id\nAlice,,\n", encoding="utf-8")

            db_path = root / "HuskHomesData.db"
            create_sqlite_db(db_path)
            cfg_path = root / "config.yml"
            write_huskhomes_config(cfg_path, backend="SQLITE")
            plan_path = root / "plan.json"
            backup_dir = root / "backups"

            rc = MIGRATE.main([
                "scan",
                "--playerdata", str(playerdata),
                "--nlogin-csv", str(nlogin_csv),
                "--huskhomes-config", str(cfg_path),
                "--output", str(plan_path),
            ])
            self.assertEqual(rc, 0)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["summary"]["source_homes"], 2)
            self.assertEqual(plan["summary"]["actions"], {"create": 2})

            # Modifying source YAML after scan must abort apply.
            (playerdata / f"{alice_uuid}.yml").write_text(
                "player:\n  info:\n    lastSeenAs: Alice\nhomes:\n  home:\n"
                "    x: 101\n    y: 65\n    z: -42\n    yaw: 178.52899\n    pitch: -12.5\n    world: world\n",
                encoding="utf-8",
            )
            rc_tamper = MIGRATE.main([
                "apply", "--plan", str(plan_path), "--huskhomes-config", str(cfg_path),
                "--backup-dir", str(backup_dir),
            ])
            self.assertEqual(rc_tamper, 1)

            # Re-scan clean state and apply.
            rc = MIGRATE.main([
                "scan",
                "--playerdata", str(playerdata),
                "--nlogin-csv", str(nlogin_csv),
                "--huskhomes-config", str(cfg_path),
                "--output", str(plan_path),
            ])
            self.assertEqual(rc, 0)
            rc = MIGRATE.main([
                "apply", "--plan", str(plan_path), "--huskhomes-config", str(cfg_path),
                "--backup-dir", str(backup_dir),
            ])
            self.assertEqual(rc, 0)
            self.assertEqual(len(list(backup_dir.glob("*.bak"))), 1)

            conn = sqlite3.connect(str(db_path))
            row = conn.execute(
                "SELECT p.x, p.y, p.z, s.name FROM huskhomes_homes h "
                "JOIN huskhomes_saved_positions s ON h.saved_position_id = s.id "
                "JOIN huskhomes_position_data p ON s.position_id = p.id"
            ).fetchone()
            conn.close()
            self.assertEqual(row[0], 101.5)
            self.assertEqual(row[1], 65.0)
            self.assertEqual(row[2], -41.5)
            self.assertEqual(row[3], "home")

            # Re-scan and re-apply must update in-place without duplicating.
            plan2_path = root / "plan2.json"
            self.assertEqual(MIGRATE.main([
                "scan",
                "--playerdata", str(playerdata),
                "--nlogin-csv", str(nlogin_csv),
                "--huskhomes-config", str(cfg_path),
                "--output", str(plan2_path),
            ]), 0)
            plan2 = json.loads(plan2_path.read_text(encoding="utf-8"))
            self.assertEqual(plan2["summary"]["actions"], {"update": 1})
            self.assertEqual(MIGRATE.main([
                "apply", "--plan", str(plan2_path), "--huskhomes-config", str(cfg_path),
                "--backup-dir", str(backup_dir),
            ]), 0)
            self.assertEqual(MIGRATE.main([
                "verify", "--plan", str(plan2_path), "--huskhomes-config", str(cfg_path),
            ]), 0)

    def test_mysql_end_to_end_and_nlogin_mysql_with_float32_rounding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            playerdata = root / "playerdata"
            playerdata.mkdir()
            bob_id = uuid.uuid4()
            (playerdata / f"{MIGRATE.offline_uuid('Bob')}.yml").write_text(
                "player:\n  info:\n    lastSeenAs: Bob\nhomes:\n  stash:\n"
                "    x: -243404\n    y: 78\n    z: -210852\n"
                "    yaw: 178.52899\n    pitch: -89.899994\n    world: world_the_end\n",
                encoding="utf-8",
            )
            cfg_path = root / "config.yml"
            write_huskhomes_config(cfg_path, backend="MYSQL")
            mysql_backing = root / "mysql_sim.db"
            create_sqlite_db(mysql_backing)

            nlogin_backing = root / "nlogin_sim.db"
            nc = sqlite3.connect(str(nlogin_backing))
            nc.execute(
                "CREATE TABLE `nlogin` (last_name TEXT, unique_id TEXT, mojang_id TEXT, bedrock_id TEXT)"
            )
            nc.execute(
                "INSERT INTO `nlogin` VALUES (?, ?, ?, ?)",
                ("Bob", bob_id.hex, None, None),
            )
            nc.commit()
            nc.close()

            fake_pymysql = types.ModuleType("pymysql")
            fake_cursors = types.ModuleType("pymysql.cursors")
            fake_cursors.DictCursor = object
            fake_pymysql.cursors = fake_cursors

            created_conns: list[FakeMySQLConnection] = []

            def fake_connect(**kwargs):
                if kwargs.get("database") == "nlogin_db":
                    c = FakeMySQLConnection(nlogin_backing, "nlogin_db")
                else:
                    c = FakeMySQLConnection(mysql_backing, "huskhomes")
                created_conns.append(c)
                return c

            fake_pymysql.connect = fake_connect

            plan_path = root / "plan.json"
            backup_dir = root / "backups"

            def fake_run(cmd, stdout=None, stderr=None, env=None, check=False):
                if stdout is not None:
                    stdout.write(b"-- fake mysqldump\n")
                return mock.Mock(returncode=0, stderr=b"")

            with mock.patch.dict(sys.modules, {"pymysql": fake_pymysql, "pymysql.cursors": fake_cursors}), \
                 mock.patch.dict("os.environ", {"NLOGIN_DB_PASSWORD": "npw", "HUSKHOMES_DB_PASSWORD": "hpw"}), \
                 mock.patch.object(MIGRATE.shutil, "which", return_value="/usr/bin/mysqldump"), \
                 mock.patch.object(MIGRATE.subprocess, "run", side_effect=fake_run):
                rc = MIGRATE.main([
                    "scan",
                    "--playerdata", str(playerdata),
                    "--huskhomes-config", str(cfg_path),
                    "--nlogin-mysql-host", "127.0.0.1",
                    "--nlogin-mysql-database", "nlogin_db",
                    "--nlogin-mysql-user", "nlogin_u",
                    "--output", str(plan_path),
                ])
                self.assertEqual(rc, 0)
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                self.assertEqual(plan["homes"][0]["owner_uuid"], str(bob_id))

                rc_apply = MIGRATE.main([
                    "apply",
                    "--plan", str(plan_path),
                    "--huskhomes-config", str(cfg_path),
                    "--backup-dir", str(backup_dir),
                ])
                self.assertEqual(rc_apply, 0)
                self.assertEqual(len(list(backup_dir.glob("*.sql"))), 1)

                rc_verify = MIGRATE.main([
                    "verify",
                    "--plan", str(plan_path),
                    "--huskhomes-config", str(cfg_path),
                ])
                self.assertEqual(rc_verify, 0)

    def test_mysql_refuses_myisam_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = root / "config.yml"
            write_huskhomes_config(cfg_path, backend="MYSQL")
            mysql_backing = root / "mysql_sim.db"
            create_sqlite_db(mysql_backing)

            fake_pymysql = types.ModuleType("pymysql")
            fake_cursors = types.ModuleType("pymysql.cursors")
            fake_cursors.DictCursor = object
            fake_pymysql.cursors = fake_cursors
            fake_pymysql.connect = lambda **kwargs: FakeMySQLConnection(mysql_backing, "huskhomes", engine="MyISAM")

            args = MIGRATE.build_parser().parse_args([
                "verify", "--plan", "p.json", "--huskhomes-config", str(cfg_path),
            ])
            cfg = MIGRATE.read_huskhomes_config(cfg_path, args)
            db = MIGRATE.HuskHomesDatabase(cfg)
            with mock.patch.dict(sys.modules, {"pymysql": fake_pymysql, "pymysql.cursors": fake_cursors}):
                db.connect()
                try:
                    with self.assertRaises(MIGRATE.MigrationError) as ctx:
                        db.validate_schema()
                    self.assertIn("MyISAM", str(ctx.exception))
                finally:
                    db.close()


    def test_config_file_mysql_workflow_and_no_secret_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_dir = root / "ops"
            cfg_dir.mkdir()
            playerdata = root / "playerdata"
            playerdata.mkdir()
            bob_id = uuid.uuid4()
            (playerdata / f"{MIGRATE.offline_uuid('Bob')}.yml").write_text(
                "player:\n  info:\n    lastSeenAs: Bob\nhomes:\n  base:\n"
                "    x: 10\n    y: 70\n    z: 20\n    yaw: 45.0\n    pitch: 10.0\n    world: world\n",
                encoding="utf-8",
            )

            hh_cfg_path = root / "huskhomes_config.yml"
            write_huskhomes_config(hh_cfg_path, backend="MYSQL")
            mysql_backing = root / "mysql_sim.db"
            create_sqlite_db(mysql_backing)

            nlogin_backing = root / "nlogin_sim.db"
            nc = sqlite3.connect(str(nlogin_backing))
            nc.execute(
                "CREATE TABLE `nlogin` (last_name TEXT, unique_id TEXT, mojang_id TEXT, bedrock_id TEXT)"
            )
            nc.execute("INSERT INTO `nlogin` VALUES (?, ?, ?, ?)", ("Bob", bob_id.hex, None, None))
            nc.commit()
            nc.close()

            nlogin_secret = "super-secret-nlogin-pw-99"
            hh_secret = "super-secret-huskhomes-pw-88"
            migration_cfg = cfg_dir / "migration.yml"
            migration_cfg.write_text(
                yaml.safe_dump({
                    "ultimatehomes": {"playerdata": "../playerdata"},
                    "huskhomes": {
                        "config": "../huskhomes_config.yml",
                        "server_name": "survival",
                        "mysql": {"password": hh_secret},
                    },
                    "nlogin": {
                        "mysql": {
                            "host": "127.0.0.1",
                            "port": 3306,
                            "database": "nlogin_db",
                            "user": "nlogin_u",
                            "password": nlogin_secret,
                        }
                    },
                    "migration": {
                        "output": "./out/plan.json",
                        "backup_dir": "./out/backups",
                        "on_conflict": "fail",
                    },
                }),
                encoding="utf-8",
            )

            fake_pymysql = types.ModuleType("pymysql")
            fake_cursors = types.ModuleType("pymysql.cursors")
            fake_cursors.DictCursor = object
            fake_pymysql.cursors = fake_cursors
            seen_passwords: list[tuple[str, str]] = []

            def fake_connect(**kwargs):
                seen_passwords.append((str(kwargs.get("database")), str(kwargs.get("password"))))
                if kwargs.get("database") == "nlogin_db":
                    return FakeMySQLConnection(nlogin_backing, "nlogin_db")
                return FakeMySQLConnection(mysql_backing, "huskhomes")

            fake_pymysql.connect = fake_connect

            def fake_run(cmd, stdout=None, stderr=None, env=None, check=False):
                if stdout is not None:
                    stdout.write(b"-- fake mysqldump\n")
                return mock.Mock(returncode=0, stderr=b"")

            with mock.patch.dict(sys.modules, {"pymysql": fake_pymysql, "pymysql.cursors": fake_cursors}), \
                 mock.patch.object(MIGRATE.shutil, "which", return_value="/usr/bin/mysqldump"), \
                 mock.patch.object(MIGRATE.subprocess, "run", side_effect=fake_run):
                self.assertEqual(MIGRATE.main(["scan", "--config", str(migration_cfg)]), 0)
                self.assertEqual(MIGRATE.main(["apply", "--config", str(migration_cfg)]), 0)
                self.assertEqual(MIGRATE.main(["verify", "--config", str(migration_cfg)]), 0)

            self.assertIn(("nlogin_db", nlogin_secret), seen_passwords)
            self.assertIn(("huskhomes", hh_secret), seen_passwords)

            # Passwords must never be serialized into generated artifacts.
            for artifact in (cfg_dir / "out" / "plan.json", cfg_dir / "out" / "plan.md", cfg_dir / "out" / "plan.csv"):
                content = artifact.read_text(encoding="utf-8")
                self.assertNotIn(nlogin_secret, content)
                self.assertNotIn(hh_secret, content)

    def test_mysql_batched_apply_and_update_query_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            playerdata = root / "playerdata"
            playerdata.mkdir()

            nlogin_lines = ["last_name,unique_id,mojang_id\n"]
            for i in range(300):
                name = f"Player{i:03d}"
                p_uuid = MIGRATE.offline_uuid(name)
                nlogin_lines.append(f"{name},{p_uuid},\n")
                (playerdata / f"{p_uuid}.yml").write_text(
                    f"player:\n  info:\n    lastSeenAs: {name}\n"
                    f"homes:\n"
                    f"  home1:\n    x: {i}\n    y: 64\n    z: {i * 2}\n    yaw: 45.12345\n    pitch: -12.34567\n    world: world\n"
                    f"  home2:\n    x: {i + 10}\n    y: 70\n    z: {-i}\n    yaw: 178.52899\n    pitch: 89.899994\n    world: world_nether\n",
                    encoding="utf-8",
                )

            nlogin_csv = root / "nlogin.csv"
            nlogin_csv.write_text("".join(nlogin_lines), encoding="utf-8")

            cfg_path = root / "config.yml"
            write_huskhomes_config(cfg_path, backend="MYSQL")
            mysql_backing = root / "mysql_sim.db"
            create_sqlite_db(mysql_backing)

            fake_pymysql = types.ModuleType("pymysql")
            fake_cursors = types.ModuleType("pymysql.cursors")
            fake_cursors.DictCursor = object
            fake_pymysql.cursors = fake_cursors

            hh_conns: list[FakeMySQLConnection] = []

            def fake_connect(**kwargs):
                c = FakeMySQLConnection(mysql_backing, "huskhomes")
                hh_conns.append(c)
                return c

            fake_pymysql.connect = fake_connect
            plan_path = root / "plan.json"

            with mock.patch.dict(sys.modules, {"pymysql": fake_pymysql, "pymysql.cursors": fake_cursors}), \
                 mock.patch.dict("os.environ", {"HUSKHOMES_DB_PASSWORD": "hpw"}):
                self.assertEqual(MIGRATE.main([
                    "scan",
                    "--playerdata", str(playerdata),
                    "--nlogin-csv", str(nlogin_csv),
                    "--huskhomes-config", str(cfg_path),
                    "--output", str(plan_path),
                ]), 0)

                hh_conns.clear()
                self.assertEqual(MIGRATE.main([
                    "apply",
                    "--plan", str(plan_path),
                    "--huskhomes-config", str(cfg_path),
                    "--no-backup",
                ]), 0)
                apply_queries = hh_conns[-1].query_count[0]
                # 600 homes + 300 users must be batched into well under 100 queries (was ~3,600+).
                self.assertLess(apply_queries, 60)

                self.assertEqual(MIGRATE.main([
                    "verify",
                    "--plan", str(plan_path),
                    "--huskhomes-config", str(cfg_path),
                ]), 0)

                # Modify all 300 player files and verify batched CASE UPDATEs.
                for i in range(300):
                    name = f"Player{i:03d}"
                    p_uuid = MIGRATE.offline_uuid(name)
                    (playerdata / f"{p_uuid}.yml").write_text(
                        f"player:\n  info:\n    lastSeenAs: {name}\n"
                        f"homes:\n"
                        f"  home1:\n    x: {i + 500}\n    y: 65\n    z: {i * 2}\n    yaw: 90.25\n    pitch: 15.5\n    world: world\n"
                        f"  home2:\n    x: {i + 510}\n    y: 71\n    z: {-i}\n    yaw: -45.75\n    pitch: 0.0\n    world: world_nether\n",
                        encoding="utf-8",
                    )

                plan2_path = root / "plan2.json"
                self.assertEqual(MIGRATE.main([
                    "scan",
                    "--playerdata", str(playerdata),
                    "--nlogin-csv", str(nlogin_csv),
                    "--huskhomes-config", str(cfg_path),
                    "--output", str(plan2_path),
                ]), 0)
                plan2 = json.loads(plan2_path.read_text(encoding="utf-8"))
                self.assertEqual(plan2["summary"]["actions"], {"update": 600})

                hh_conns.clear()
                self.assertEqual(MIGRATE.main([
                    "apply",
                    "--plan", str(plan2_path),
                    "--huskhomes-config", str(cfg_path),
                    "--no-backup",
                ]), 0)
                update_queries = hh_conns[-1].query_count[0]
                self.assertLess(update_queries, 60)

    def test_sqlite_skip_policy_and_user_conflict_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            playerdata = root / "playerdata"
            playerdata.mkdir()
            alice_uuid = MIGRATE.offline_uuid("Alice")
            (playerdata / f"{alice_uuid}.yml").write_text(
                "player:\n  info:\n    lastSeenAs: Alice\nhomes:\n  base:\n"
                "    x: 10\n    y: 65\n    z: 20\n    yaw: 0.0\n    pitch: 0.0\n    world: world\n"
                "  farm:\n"
                "    x: 30\n    y: 70\n    z: 40\n    yaw: 90.0\n    pitch: 0.0\n    world: world\n",
                encoding="utf-8",
            )
            nlogin_csv = root / "nlogin.csv"
            nlogin_csv.write_text("last_name,unique_id,mojang_id\nAlice,,\n", encoding="utf-8")

            db_path = root / "HuskHomesData.db"
            create_sqlite_db(db_path)
            existing_home_uuid = str(uuid.uuid4())
            world_uuid = str(uuid.uuid4())
            conn = sqlite3.connect(str(db_path))
            conn.execute("INSERT INTO huskhomes_users (uuid, username) VALUES (?, ?)", (str(alice_uuid), "Alice"))
            conn.execute(
                "INSERT INTO huskhomes_position_data (id, x, y, z, yaw, pitch, world_name, world_uuid, server_name) "
                "VALUES (1, 999.5, 80.0, 999.5, 0.0, 0.0, 'world', ?, 'server')",
                (world_uuid,),
            )
            conn.execute(
                "INSERT INTO huskhomes_saved_positions (id, position_id, name, description, tags, timestamp) "
                "VALUES (1, 1, 'base', '', NULL, '2026-01-01 00:00:00')",
            )
            conn.execute(
                "INSERT INTO huskhomes_homes (uuid, saved_position_id, owner_uuid, public) VALUES (?, 1, ?, 0)",
                (existing_home_uuid, str(alice_uuid)),
            )
            conn.commit()
            conn.close()

            cfg_path = root / "config.yml"
            write_huskhomes_config(cfg_path, backend="SQLITE")
            plan_path = root / "plan.json"

            self.assertEqual(MIGRATE.main([
                "scan",
                "--playerdata", str(playerdata),
                "--nlogin-csv", str(nlogin_csv),
                "--huskhomes-config", str(cfg_path),
                "--on-conflict", "skip",
                "--output", str(plan_path),
            ]), 0)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["summary"]["actions"], {"create": 1, "skip": 1})

            self.assertEqual(MIGRATE.main([
                "apply",
                "--plan", str(plan_path),
                "--huskhomes-config", str(cfg_path),
                "--no-backup",
            ]), 0)
            self.assertEqual(MIGRATE.main([
                "verify",
                "--plan", str(plan_path),
                "--huskhomes-config", str(cfg_path),
            ]), 0)

            # A conflicting username inserted after scan must abort apply cleanly.
            bob_uuid = MIGRATE.offline_uuid("Bob")
            (playerdata / f"{bob_uuid}.yml").write_text(
                "player:\n  info:\n    lastSeenAs: Bob\nhomes:\n  camp:\n"
                "    x: 1\n    y: 64\n    z: 2\n    yaw: 0.0\n    pitch: 0.0\n    world: world\n",
                encoding="utf-8",
            )
            nlogin_csv.write_text("last_name,unique_id,mojang_id\nAlice,,\nBob,,\n", encoding="utf-8")
            plan2_path = root / "plan2.json"
            self.assertEqual(MIGRATE.main([
                "scan",
                "--playerdata", str(playerdata),
                "--nlogin-csv", str(nlogin_csv),
                "--huskhomes-config", str(cfg_path),
                "--on-conflict", "skip",
                "--output", str(plan2_path),
            ]), 0)
            conn = sqlite3.connect(str(db_path))
            conn.execute("INSERT INTO huskhomes_users (uuid, username) VALUES (?, ?)", (str(uuid.uuid4()), "bob"))
            conn.commit()
            conn.close()
            self.assertEqual(MIGRATE.main([
                "apply",
                "--plan", str(plan2_path),
                "--huskhomes-config", str(cfg_path),
                "--no-backup",
            ]), 1)

    def test_float32_equal_handles_mariadb_six_sig_digits(self) -> None:
        self.assertTrue(MIGRATE.float32_equal(18.7184, 18.718445))
        self.assertTrue(MIGRATE.float32_equal(113.699, 113.6987))
        self.assertTrue(MIGRATE.float32_equal(-172.018, -172.01836))
        self.assertTrue(MIGRATE.float32_equal(0.0, 0.0))
        self.assertFalse(MIGRATE.float32_equal(18.8, 18.718445))
        self.assertFalse(MIGRATE.float32_equal(113.701, 113.6987))


if __name__ == "__main__":
    unittest.main()
