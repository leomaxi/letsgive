"""Backup and restore for Let's Give's database (spec 12: "Recovery" --
encrypted daily backups, point-in-time recovery, documented restore
exercises).

This environment only has a local SQLite dev database to actually run
against, so the SQLite path below is a real, working implementation you can
exercise end to end. Production runs on Postgres or MySQL (both are real,
supported backends -- see app/db/base.py and migrations/); those paths are
the vendors' own standard dump/restore tools and are documented, not
scripted here, since there's no live Postgres/MySQL instance in this
environment to test them against -- see `postgres_commands()` and
`mysql_commands()`.

Neither local path encrypts the backup file itself; spec 12 calls for
encrypted backups, so in production this script's output should be piped
through (or uploaded via) whatever the hosting provider's at-rest encryption
is -- e.g. an encrypted S3 bucket for the dump file, or `pg_dump | gpg
--encrypt` / `mysqldump | gpg --encrypt`.

Usage:
    python scripts/backup_restore.py backup <db_path> <backup_path>
    python scripts/backup_restore.py restore <backup_path> <db_path>
    python scripts/backup_restore.py postgres-commands
    python scripts/backup_restore.py mysql-commands
"""

import argparse
import sqlite3
from pathlib import Path


def backup_sqlite(db_path: str, backup_path: str) -> None:
    """Uses SQLite's own online backup API (not a raw file copy), so a
    backup taken while the app is writing to the database is still
    transactionally consistent -- no risk of copying a half-written page.
    """
    source = sqlite3.connect(db_path)
    try:
        Path(backup_path).parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(backup_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def restore_sqlite(backup_path: str, db_path: str) -> None:
    if not Path(backup_path).exists():
        raise FileNotFoundError(f"Backup file not found: {backup_path}")
    source = sqlite3.connect(backup_path)
    try:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(db_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def postgres_commands(database_url: str = "postgresql://user:pass@host:5432/letsgive") -> str:
    return f"""# Backup (run daily via cron / a scheduled job in production):
pg_dump --format=custom --file=letsgive_$(date +%Y%m%d_%H%M%S).dump "{database_url}"

# Encrypt at rest before/while shipping off-box, e.g.:
gpg --symmetric --cipher-algo AES256 letsgive_20260101_020000.dump

# Restore into a fresh (or deliberately emptied) database:
pg_restore --clean --if-exists --no-owner --dbname="{database_url}" letsgive_20260101_020000.dump

# Point-in-time recovery needs WAL archiving enabled on the Postgres server
# itself (archive_mode=on + archive_command) -- that's an infra/hosting
# provider setting, not something this application script controls.
"""


def mysql_commands(
    host: str = "localhost", db: str = "letsgive", user: str = "letsgive"
) -> str:
    return f"""# Backup (run daily via cron / a scheduled job in production):
mysqldump --single-transaction --routines --host="{host}" --user="{user}" -p "{db}" \\
    > letsgive_$(date +%Y%m%d_%H%M%S).sql

# --single-transaction takes a consistent InnoDB snapshot without locking
# tables for the dump's duration -- the MySQL equivalent of pg_dump's default
# safety, and essential since this runs against a live app database.

# Encrypt at rest before/while shipping off-box, e.g.:
gpg --symmetric --cipher-algo AES256 letsgive_20260101_020000.sql

# Restore into a fresh (or deliberately emptied) database:
mysql --host="{host}" --user="{user}" -p "{db}" < letsgive_20260101_020000.sql

# Point-in-time recovery needs binary logging enabled on the MySQL server
# itself (log_bin, with binlog_expire_logs_seconds set generously) -- that's
# an infra/hosting provider setting, not something this application script
# controls.
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="Back up a SQLite database file.")
    backup_parser.add_argument("db_path")
    backup_parser.add_argument("backup_path")

    restore_parser = subparsers.add_parser("restore", help="Restore a SQLite database file from backup.")
    restore_parser.add_argument("backup_path")
    restore_parser.add_argument("db_path")

    subparsers.add_parser("postgres-commands", help="Print the Postgres backup/restore commands.")
    subparsers.add_parser("mysql-commands", help="Print the MySQL backup/restore commands.")

    args = parser.parse_args()

    if args.command == "backup":
        backup_sqlite(args.db_path, args.backup_path)
        print(f"Backed up {args.db_path} -> {args.backup_path}")
    elif args.command == "restore":
        restore_sqlite(args.backup_path, args.db_path)
        print(f"Restored {args.backup_path} -> {args.db_path}")
    elif args.command == "postgres-commands":
        print(postgres_commands())
    elif args.command == "mysql-commands":
        print(mysql_commands())


if __name__ == "__main__":
    main()
