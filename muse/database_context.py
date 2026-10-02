"""Read database schema metadata for agent context, without exposing credentials."""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit


MAX_TABLES = 100
MAX_COLUMNS = 500
DATABASE_TYPES = {"postgresql", "mysql", "sqlserver", "turso"}


class DatabaseContextError(ValueError):
    """A connection configuration or schema read failed."""


def validate_database_connections(value) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 5:
        raise DatabaseContextError("Provide up to five database connections.")
    connections = []
    names = set()
    for item in value:
        if not isinstance(item, dict):
            raise DatabaseContextError("Each database connection must be an object.")
        allowed = {"name", "type", "host", "port", "database", "username", "schema", "password_env", "url", "token_env"}
        if set(item) - allowed:
            raise DatabaseContextError("Unsupported database fields. Store secrets in server environment variables.")
        connection = {}
        for key, field in item.items():
            if key == "port":
                if isinstance(field, bool) or not isinstance(field, int) or not 1 <= field <= 65535:
                    raise DatabaseContextError("Database port must be between 1 and 65535.")
            elif not isinstance(field, str) or len(field) > 500:
                raise DatabaseContextError("Database fields must be text of 500 characters or fewer.")
            connection[key] = field.strip() if isinstance(field, str) else field
        if connection.get("type") not in DATABASE_TYPES:
            raise DatabaseContextError("Choose postgresql, mysql, sqlserver, or turso.")
        name = connection.get("name", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name) or name.casefold() in names:
            raise DatabaseContextError("Each database needs a unique name using letters, digits, underscores, or hyphens.")
        names.add(name.casefold())
        secret_key = "token_env" if connection["type"] == "turso" else "password_env"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", connection.get(secret_key, "")):
            raise DatabaseContextError(f"Provide a server environment variable name for {secret_key}.")
        if connection["type"] == "turso":
            parsed = urlsplit(connection.get("url", ""))
            if (parsed.scheme not in {"libsql", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or parsed.path not in {"", "/"}):
                raise DatabaseContextError("Provide a Turso libsql:// or https:// URL without credentials.")
            if set(connection) & {"host", "database", "username", "password_env", "port", "schema"}:
                raise DatabaseContextError("Turso uses a URL and token environment variable.")
        else:
            if not all(connection.get(key) for key in ("host", "database", "username")):
                raise DatabaseContextError("Server databases require host, database name, and username.")
            if set(connection) & {"url", "token_env"}:
                raise DatabaseContextError("Server databases use host and password environment variable.")
            if any(char in connection["host"] for char in "/@; \n\r"):
                raise DatabaseContextError("Provide a database hostname or IP address.")
        connections.append(connection)
    return connections


def read_database_schema(configuration: dict) -> dict:
    """Read only catalogs; user rows and credentials never enter the result."""
    config = validate_database_connections([configuration])[0]
    secret_key = "token_env" if config["type"] == "turso" else "password_env"
    secret = os.getenv(config[secret_key])
    if not secret:
        raise DatabaseContextError(f"Server environment variable {config[secret_key]} is not configured.")
    try:
        tables, truncated = (
            _read_turso(config, secret) if config["type"] == "turso"
            else _read_server(config, secret)
        )
    except Exception:
        # Driver errors can contain connection strings or passwords.
        raise DatabaseContextError(
            f"Could not read schema for database '{config['name']}'. Check its connection, driver, and schema permissions."
        ) from None
    return {"name": config["name"], "type": config["type"], "tables": tables, "truncated": truncated}


def _read_server(config: dict, password: str) -> tuple[list[dict], bool]:
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.engine import URL
    from sqlalchemy.pool import NullPool

    drivers = {"postgresql": "postgresql+psycopg", "mysql": "mysql+pymysql", "sqlserver": "mssql+pymssql"}
    defaults = {"postgresql": 5432, "mysql": 3306, "sqlserver": 1433}
    connect_args = {
        "postgresql": {"connect_timeout": 10, "options": "-c statement_timeout=15000 -c default_transaction_read_only=on"},
        "mysql": {"connect_timeout": 10, "read_timeout": 15},
        "sqlserver": {"login_timeout": 10, "timeout": 15},
    }
    url = URL.create(drivers[config["type"]], username=config["username"], password=password,
                     host=config["host"], port=config.get("port", defaults[config["type"]]), database=config["database"])
    engine = create_engine(url, poolclass=NullPool, echo=False, hide_parameters=True,
                           connect_args=connect_args[config["type"]])
    try:
        with engine.connect() as connection:
            return _reflect_schema(inspect(connection), config.get("schema") or None)
    finally:
        engine.dispose()


def _reflect_schema(inspector, schema: str | None) -> tuple[list[dict], bool]:
    tables = []
    objects = [(name, "table") for name in inspector.get_table_names(schema=schema)]
    objects += [(name, "view") for name in inspector.get_view_names(schema=schema)]
    for name, kind in sorted(objects)[:MAX_TABLES]:
        columns = inspector.get_columns(name, schema=schema)
        primary_key = inspector.get_pk_constraint(name, schema=schema) if kind == "table" else {}
        foreign_keys = inspector.get_foreign_keys(name, schema=schema) if kind == "table" else []
        indexes = inspector.get_indexes(name, schema=schema) if kind == "table" else []
        tables.append({
            "name": name, "schema": schema or inspector.default_schema_name or "", "kind": kind,
            "columns": [{"name": column["name"], "type": str(column["type"]), "nullable": bool(column.get("nullable", True))}
                        for column in columns[:MAX_COLUMNS]],
            "primary_key": list(primary_key.get("constrained_columns") or []),
            "foreign_keys": [{"columns": list(key.get("constrained_columns") or []),
                              "target_table": key.get("referred_table", ""),
                              "target_schema": key.get("referred_schema") or schema or inspector.default_schema_name or "",
                              "target_columns": list(key.get("referred_columns") or [])} for key in foreign_keys[:100]],
            "indexes": [{"name": index.get("name", ""), "columns": list(index.get("column_names") or []),
                         "unique": bool(index.get("unique"))} for index in indexes[:100]],
            "truncated": len(columns) > MAX_COLUMNS or len(foreign_keys) > 100 or len(indexes) > 100,
        })
    return tables, len(objects) > MAX_TABLES or any(table["truncated"] for table in tables)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _read_turso(config: dict, token: str) -> tuple[list[dict], bool]:
    import turso_serverless

    connection = turso_serverless.connect(config["url"], auth_token=token)
    try:
        return _read_sqlite_catalog(connection)
    finally:
        connection.close()


def _read_sqlite_catalog(connection) -> tuple[list[dict], bool]:
    objects = connection.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT ?", (MAX_TABLES + 1,),
    ).fetchall()
    tables = []
    for name, kind in objects[:MAX_TABLES]:
        quoted = _quote_identifier(name)
        columns = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
        raw_keys = connection.execute(f"PRAGMA foreign_key_list({quoted})").fetchall() if kind == "table" else []
        keys = {}
        for key in raw_keys:
            group = keys.setdefault(key[0], {"columns": [], "target_table": key[2], "target_schema": "", "target_columns": []})
            group["columns"].append(key[3])
            group["target_columns"].append(key[4])
        raw_indexes = connection.execute(f"PRAGMA index_list({quoted})").fetchall() if kind == "table" else []
        indexes = []
        for index in raw_indexes[:100]:
            index_columns = connection.execute(f"PRAGMA index_info({_quote_identifier(index[1])})").fetchall()
            indexes.append({"name": index[1], "columns": [column[2] for column in index_columns], "unique": bool(index[2])})
        tables.append({
            "name": name, "schema": "", "kind": kind,
            "columns": [{"name": column[1], "type": column[2], "nullable": not bool(column[3] or column[5])}
                        for column in columns[:MAX_COLUMNS]],
            "primary_key": [column[1] for column in sorted(columns, key=lambda column: column[5]) if column[5]],
            "foreign_keys": list(keys.values())[:100], "indexes": indexes,
            "truncated": len(columns) > MAX_COLUMNS or len(keys) > 100 or len(raw_indexes) > 100,
        })
    return tables, len(objects) > MAX_TABLES or any(table["truncated"] for table in tables)
