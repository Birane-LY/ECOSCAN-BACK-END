import argparse
import hashlib
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import closing
from pathlib import Path


PUBLISHED_DEMO_EMAILS = {
    "mobile.commande.test@ecoscan.invalid",
    "pilote.multisite@ecoscan.invalid",
    "pilote.anomalies@ecoscan.invalid",
}
PRIVATE_TABLES = (
    "django_session",
    "django_admin_log",
    "audit_journalaudit",
    "billing_webhookevent",
)
EMAIL_PATTERN = re.compile(
    r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
)
CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~-]{20,}"),
)


def export_database(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        raise ValueError("Source and destination must be different files.")
    if not source.is_file():
        raise FileNotFoundError(f"SQLite source not found: {source}")
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing file: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)

        with closing(
            sqlite3.connect(
                f"{source.as_uri()}?mode=ro",
                uri=True,
                timeout=30,
            )
        ) as source_connection, closing(
            sqlite3.connect(temporary_path)
        ) as target_connection:
            source_connection.backup(target_connection)
            target_connection.execute("PRAGMA foreign_keys = OFF")

            user_columns = {
                row[1]
                for row in target_connection.execute(
                    'PRAGMA table_info("accounts_utilisateur")'
                )
            }
            required_columns = {
                "id",
                "nom",
                "email",
                "password",
                "mot_de_passe_hash",
            }
            if not required_columns.issubset(user_columns):
                raise ValueError("The source has an unsupported user table schema.")

            users = target_connection.execute(
                'SELECT "id", "email" FROM "accounts_utilisateur"'
            ).fetchall()
            for index, (user_id, email) in enumerate(users, start=1):
                if email.lower() in PUBLISHED_DEMO_EMAILS:
                    name = f"Compte démo EcoScan {index}"
                    safe_email = email.lower()
                else:
                    name = f"Compte de démonstration {index}"
                    safe_email = f"demo-{uuid.uuid5(uuid.NAMESPACE_URL, user_id)}@example.invalid"
                target_connection.execute(
                    """
                    UPDATE accounts_utilisateur
                    SET nom = ?, email = ?, password = '!', mot_de_passe_hash = '!',
                        last_login = NULL
                    WHERE id = ?
                    """,
                    (name, safe_email, user_id),
                )

            for table in PRIVATE_TABLES:
                exists = target_connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
                if exists:
                    target_connection.execute(f'DELETE FROM "{table}"')

            _anonymize_embedded_emails(target_connection)
            _clear_sensitive_urls(target_connection)

            organization_columns = {
                row[1]
                for row in target_connection.execute(
                    'PRAGMA table_info("organizations_organisation")'
                )
            }
            if "details_demande" in organization_columns:
                target_connection.execute(
                    'UPDATE organizations_organisation SET details_demande = \'{}\''
                )

            target_connection.commit()
            checkpoint = target_connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if checkpoint[0] != 0:
                raise ValueError("Could not checkpoint the sanitized SQLite database.")
            journal_mode = target_connection.execute(
                "PRAGMA journal_mode = DELETE"
            ).fetchone()[0]
            if journal_mode.lower() != "delete":
                raise ValueError("The sanitized database must be a standalone SQLite file.")
            violations = target_connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if violations:
                raise ValueError("The sanitized database contains foreign-key violations.")
            integrity = target_connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
            if integrity != "ok":
                raise ValueError("The sanitized database failed its integrity check.")
            target_connection.execute("VACUUM")
            _assert_public_data(target_connection)

        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _anonymize_embedded_emails(connection: sqlite3.Connection) -> None:
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (table,) in tables:
        columns = [
            row[1]
            for row in connection.execute(
                f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")'
            )
            if any(kind in row[2].upper() for kind in ("TEXT", "CHAR", "CLOB"))
        ]
        if not columns:
            continue
        quoted_table = f'"{table.replace(chr(34), chr(34) * 2)}"'
        selected_columns = ", ".join(
            f'"{column.replace(chr(34), chr(34) * 2)}"' for column in columns
        )
        rows = connection.execute(
            f"SELECT rowid, {selected_columns} FROM {quoted_table}"
        ).fetchall()
        for row in rows:
            updates = {}
            for column, value in zip(columns, row[1:]):
                if not isinstance(value, str):
                    continue
                sanitized = EMAIL_PATTERN.sub(_safe_email, value)
                if sanitized != value:
                    updates[column] = sanitized
            if updates:
                assignments = ", ".join(
                    f'"{column.replace(chr(34), chr(34) * 2)}" = ?'
                    for column in updates
                )
                connection.execute(
                    f"UPDATE {quoted_table} SET {assignments} WHERE rowid = ?",
                    (*updates.values(), row[0]),
                )


def _safe_email(match: re.Match) -> str:
    email = match.group(0).lower()
    if email in PUBLISHED_DEMO_EMAILS:
        return email
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()[:12]
    return f"contact-{digest}@example.invalid"


def _clear_sensitive_urls(connection: sqlite3.Connection) -> None:
    for table, column in (
        ("analysis_livrable", "url_fichier"),
        ("billing_facture", "pdf_url"),
    ):
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if not exists:
            continue
        columns = {
            row[1]
            for row in connection.execute(
                f'PRAGMA table_info("{table}")'
            )
        }
        if column in columns:
            connection.execute(f'UPDATE "{table}" SET "{column}" = \'\'')


def _assert_public_data(connection: sqlite3.Connection) -> None:
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (table,) in tables:
        columns = [
            row[1]
            for row in connection.execute(
                f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")'
            )
            if any(kind in row[2].upper() for kind in ("TEXT", "CHAR", "CLOB"))
        ]
        if not columns:
            continue
        selected_columns = ", ".join(
            f'"{column.replace(chr(34), chr(34) * 2)}"' for column in columns
        )
        for row in connection.execute(
            f'SELECT {selected_columns} FROM "{table.replace(chr(34), chr(34) * 2)}"'
        ):
            for value in row:
                if not isinstance(value, str):
                    continue
                if any(pattern.search(value) for pattern in CREDENTIAL_PATTERNS):
                    raise ValueError(
                        "A credential-like value remains in the sanitized database."
                    )
                for email in EMAIL_PATTERN.findall(value):
                    if email.lower() not in PUBLISHED_DEMO_EMAILS and not email.lower().endswith(
                        "@example.invalid"
                    ):
                        raise ValueError(
                            "An unapproved email address remains in the sanitized database."
                        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a separate SQLite demo copy with unusable account passwords."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("db.sqlite3"),
        help="Source SQLite database; it is opened read-only.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("demo-data.sqlite3"),
        help="New sanitized database path; an existing file is never overwritten.",
    )
    arguments = parser.parse_args()

    export_database(arguments.source, arguments.output)
    print(f"Sanitized demo database created at {arguments.output.resolve()}")
    print("All stored account passwords are unusable; provision credentials after deployment.")


if __name__ == "__main__":
    main()
