# UltimateHomes → HuskHomes Migration Tool

Migrates player homes from **UltimateHomes** (`plugins/UltimateHomes/playerdata/<uuid>.yml`) into **HuskHomes** (**MySQL**, **MariaDB**, or **SQLite**), resolving player UUIDs against **nLogin** (MySQL or CSV export).

Designed around an auditable **scan → review → apply → verify** workflow:
- **No silent data loss**: every home in UltimateHomes is accounted for, including nested/malformed entries that can be unambiguously recovered and home names exceeding HuskHomes' 16-character limit (deterministically shortened and listed in the report).
- **Safe for cracked / nLogin servers**: maps players using nLogin `unique_id`, `mojang_id`, and `OfflinePlayer:<last_name>` UUIDv3, with explicit warnings for unmatched accounts and optional `--uuid-map` overrides.
- **Idempotent**: deterministic UUIDv5 home IDs mean re-running `scan` + `apply` updates previously migrated homes instead of duplicating them.
- **Atomic & backed up**: `apply` verifies that source files and nLogin data have not changed since `scan`, creates a pre-apply backup (`mysqldump` for MySQL/MariaDB, `sqlite3.backup` for SQLite), writes in a single transaction, and verifies every row after commit.

---

## Requirements

- Python 3.10+
- `mysqldump` (from MySQL/MariaDB client tools) when backing up a HuskHomes MySQL/MariaDB database
- Python packages in `requirements.txt`:

```bash
python3 -m pip install -r requirements.txt
```

(`PyYAML`, `PyMySQL`, and `cryptography` for MySQL 8 `caching_sha2_password` authentication.)

---

## Production Runbook (nLogin MySQL + HuskHomes MySQL)

1. **Stop the Minecraft server / proxy** so neither nLogin nor HuskHomes writes while migrating.
2. **Export database passwords** (avoid passing secrets on the command line):
   ```bash
   export NLOGIN_DB_PASSWORD='...'
   export HUSKHOMES_DB_PASSWORD='...'   # optional if already in HuskHomes/config.yml
   ```
3. **Scan (read-only)** and generate the plan, Markdown audit report, and CSV:
   ```bash
   python3 scripts/migrate_ultimatehomes.py scan \
     --playerdata /path/to/plugins/UltimateHomes/playerdata \
     --huskhomes-config /path/to/plugins/HuskHomes/config.yml \
     --server-name server \
     --nlogin-mysql-host 127.0.0.1 \
     --nlogin-mysql-port 3306 \
     --nlogin-mysql-database nlogin \
     --nlogin-mysql-user nlogin_user \
     --output ./migration-output/plan.json
   ```
   - If your production `HuskHomes/config.yml` uses `type: MYSQL`, the host/port/database/user/table names are read automatically (and can be overridden with `--huskhomes-mysql-*` or `--huskhomes-backend MYSQL`).
   - If `--server-name` differs from `"server"`, set it to match `HuskHomes/server.yml` (cross-server mode) or `"server"` (single-server default).
   - If you already have homes in HuskHomes that might share names with UltimateHomes homes, review conflicts or pass `--on-conflict rename`.
4. **Inspect `./migration-output/plan.md` (and `plan.csv`)**:
   - Check **Errors** (must be `0` before `apply` can run).
   - Check **User warnings** (e.g. players missing from nLogin or missing `lastSeenAs`). Provide a `--uuid-map overrides.csv` (`source_uuid,target_uuid,username`) if needed and re-run `scan`.
   - Check **Renamed homes** (any home name >16 chars or disallowed by HuskHomes' regex).
5. **Apply**:
   ```bash
   python3 scripts/migrate_ultimatehomes.py apply \
     --plan ./migration-output/plan.json \
     --huskhomes-config /path/to/plugins/HuskHomes/config.yml \
     --backup-dir ./migration-output/backups
   ```
6. **Verify** (independent read-only check; also runs automatically at the end of `apply`):
   ```bash
   python3 scripts/migrate_ultimatehomes.py verify \
     --plan ./migration-output/plan.json \
     --huskhomes-config /path/to/plugins/HuskHomes/config.yml
   ```
7. **Start the server** and spot-check `/homes` and `/home <name>` in-game.

---

## Coordinate, UUID, and Name Behavior

- **Coordinates**: UltimateHomes stores block integer `x`/`z` and adds `+0.5` during `/home`. The tool writes `x + 0.5`, `y`, `z + 0.5`, `yaw`, `pitch` so players land at the exact same spot in HuskHomes.
- **Worlds**: Existing `world_uuid` values from HuskHomes are reused; if a world has no existing row in HuskHomes yet, a deterministic placeholder UUIDv5 is written (`--world-uuid NAME=UUID` can override it). HuskHomes falls back to resolving worlds by `world_name` at runtime.
- **MySQL collation**: MySQL/MariaDB `utf8_unicode_ci`/`utf8mb4_unicode_ci` collations compare home names case-insensitively regardless of `general.names.case_insensitive`, so the planner enforces case-insensitive uniqueness for MySQL/MariaDB targets.
- **No deletions**: Existing HuskHomes users and homes are never deleted.

---

## Testing

```bash
python3 -m unittest discover -s tests -v
```
