# AGENTS.md

## Project Overview
This repository holds a migration tool (`scripts/migrate_ultimatehomes.py`) that moves player homes from **UltimateHomes** (`plugins/UltimateHomes/playerdata/<uuid>.yml`) into **HuskHomes** (`SQLITE`, `MYSQL`, or `MARIADB`), resolving player identities against a cracked **nLogin** MySQL database.

`samples/` contains local server data (player UUIDs, names, configs, databases) and is gitignored via `.gitignore`. Never commit `samples/`, database dumps, plans, CSVs, or credentials.

## Repository Layout
- `scripts/migrate_ultimatehomes.py`: Self-contained Python 3 CLI implementing `scan`, `apply`, and `verify`.
- `migration.example.yml`: Template YAML config file (`--config migration.yml`) for paths, database credentials, and migration options; `migration.yml` and `migration-output/` are gitignored.
- `tests/test_migrate_ultimatehomes.py`: Automated `unittest` suite covering parsing, nLogin identity resolution, home name allocation, config-file loading, SQLite end-to-end migration, and MySQL backend behavior.
- `requirements.txt`: Python dependencies (`PyYAML`, `PyMySQL`, `cryptography`).
- `README.md`: Operator runbook for production migration.
- `samples/`: Gitignored local test/sample fixtures (`ultimatehomes/playerdata/`, `huskhomes/`, `nlogin/`).

## Domain Rules & Invariants
1. **Plan-first workflow (`scan` -> `apply` -> `verify`)**:
   - `scan` is strictly read-only. It produces a plan JSON, a Markdown audit report (`.md`), and a CSV (`.csv`).
   - `apply` refuses any plan with errors or `blocked` homes, verifies SHA-256 manifests of all UltimateHomes YAML files, verifies the nLogin snapshot hash, verifies the HuskHomes target fingerprint, takes a pre-apply backup (`sqlite3.backup` or `mysqldump`), executes inside a single transaction, and re-verifies every user and home after commit.
   - `verify` re-reads HuskHomes and proves every planned user and home matches the database.
2. **Coordinate conversion**:
   - UltimateHomes stores integer block coordinates (and sometimes floats if hand-edited) and adds `+0.5` to `x` and `z` at teleport time (`HomeCommand.java`).
   - HuskHomes stores exact teleport coordinates, so the migrator writes `x + 0.5`, `y`, `z + 0.5` with original `yaw` and `pitch`.
   - HuskHomes MySQL/MariaDB stores `yaw` and `pitch` as 32-bit `FLOAT`, so verification compares against IEEE-754 float32 rounding.
3. **Player identity & nLogin**:
   - Production uses cracked mode with nLogin MySQL (`nlogin` table: `last_name`, `unique_id`, `mojang_id`, `bedrock_id`).
   - Canonical UUID per nLogin account: `unique_id` (when set, normalized from 32-char hex or dashed UUID) -> `OfflinePlayer:<last_name>` UUIDv3 -> `mojang_id` (only if `last_name` is also missing).
   - UltimateHomes files are matched by UUID alias (`unique_id`, `mojang_id`, or offline UUID of `last_name`) first, then case-insensitive `last_name`, then manual `--uuid-map` overrides, with fallback to the source filename UUID and explicit warnings.
4. **Home names & IDs**:
   - HuskHomes enforces a 16-character hard limit (`TextValidator.java`) and configurable delimiter/regex rules. Invalid/overlong names are deterministically renamed (`<stem>-<4-char base36 crc32>`) and logged in the report.
   - MySQL/MariaDB collations (`utf8_unicode_ci` / `utf8mb4_unicode_ci`) treat home names and usernames case-insensitively even when `general.names.case_insensitive: false`, so `NameAllocator` treats `MYSQL`/`MARIADB` as case-insensitive.
   - Home UUIDs are deterministic UUIDv5 (`HOME_UUID_NAMESPACE` + `<source_uuid>:<raw_home_name>`), making re-runs idempotent (`update` instead of duplicating).
5. **Non-destructive guarantee**:
   - Never delete existing HuskHomes homes or users.
   - Never guess silently when a HuskHomes username or home name collides; fail by default (`--on-conflict fail`), or apply explicit `--on-conflict rename|skip`.

## Verification
Run syntax and unit tests after any code changes:
```bash
python3 -m py_compile scripts/migrate_ultimatehomes.py
python3 -m unittest discover -s tests -v
```
