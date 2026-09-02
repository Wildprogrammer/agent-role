"""Fail-closed PyMySQL read and execute connections for the MySQL MCP."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import ssl
from contextlib import contextmanager
import inspect
from typing import Iterator
from urllib.parse import parse_qsl, unquote, urlsplit

import pymysql
from pymysql.constants import CLIENT

from .config import ConfigError
from .models import MySqlConnectionOverride, MySqlCredentials, MySqlTarget


class MySqlClientError(RuntimeError):
    """A sanitized MySQL connection failure."""


_READ_TIMEOUT_SECONDS = 30


class _VerifiedTlsConnection(pymysql.connections.Connection):
    """Reject a server that would receive credentials without negotiated TLS.

    PyMySQL calls ``_request_authentication`` after parsing the server handshake
    and before emitting its first authentication packet. The upstream fallback
    otherwise sends the authentication response in plaintext when ``CLIENT.SSL``
    is absent, so this guard aborts the raw socket without a MySQL protocol write
    before delegating to PyMySQL's implementation.
    """

    def _request_authentication(self) -> None:
        if self.ssl and not self.server_capabilities & CLIENT.SSL:
            self._force_close()
            raise MySqlClientError("MySQL server did not negotiate required TLS")
        super()._request_authentication()


@contextmanager
def read_connection(
    target: MySqlTarget,
    credentials: MySqlCredentials,
) -> Iterator[object]:
    """Open one non-autocommitting connection and always close it cleanly.

    This is deliberately a read-only lifecycle helper: it never commits, and rolls
    back in ``finally`` to release any implicit transaction state left by a read.
    Errors omit connection details, SQL, and credential values.
    """

    if not isinstance(target, MySqlTarget) or not isinstance(credentials, MySqlCredentials):
        raise MySqlClientError("MySQL read connection configuration is invalid")
    _require_driver_pre_authentication_hook()
    try:
        connection = _VerifiedTlsConnection(
            host=target.host,
            port=target.port,
            user=credentials.username,
            password=credentials.password,
            database=target.database,
            autocommit=False,
            connect_timeout=target.connect_timeout_seconds,
            read_timeout=_READ_TIMEOUT_SECONDS,
            write_timeout=_READ_TIMEOUT_SECONDS,
            cursorclass=pymysql.cursors.DictCursor,
            ssl=_optional_tls_context(target),
        )
    except MySqlClientError:
        raise
    except Exception:
        raise MySqlClientError("could not establish a verified MySQL read connection") from None

    try:
        yield connection
    finally:
        try:
            connection.rollback()
        except Exception:
            # Connection failures during cleanup must not hide the original result
            # or leak details from the driver.
            pass
        try:
            connection.close()
        except Exception:
            pass


@contextmanager
def execute_connection(
    target: MySqlTarget,
    credentials: MySqlCredentials,
) -> Iterator[object]:
    """Open one autocommitting multi-statement connection and only close it.

    Unlike the read helper, exit never commits or rolls back: raw statements run
    in autocommit, so a lost connection makes server state unknowable and cleanup
    must not mutate anything. Errors omit connection details and credential values.
    """

    if not isinstance(target, MySqlTarget) or not isinstance(credentials, MySqlCredentials):
        raise MySqlClientError("MySQL execution connection configuration is invalid")
    try:
        if target.tls_verify:
            _require_driver_pre_authentication_hook()
            connection_type = _VerifiedTlsConnection
        else:
            connection_type = pymysql.connections.Connection
        connection = connection_type(
            host=target.host,
            port=target.port,
            user=credentials.username,
            password=credentials.password,
            database=target.database,
            autocommit=True,
            connect_timeout=target.connect_timeout_seconds,
            read_timeout=_READ_TIMEOUT_SECONDS,
            write_timeout=_READ_TIMEOUT_SECONDS,
            cursorclass=pymysql.cursors.DictCursor,
            client_flag=CLIENT.MULTI_STATEMENTS,
            ssl=_optional_tls_context(target),
        )
    except MySqlClientError:
        raise
    except Exception:
        raise MySqlClientError("could not establish a MySQL execution connection") from None

    try:
        yield connection
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _optional_tls_context(target: MySqlTarget) -> ssl.SSLContext | None:
    """Return None when TLS is off; otherwise a verified context from the CA."""

    if not target.tls_verify:
        return None
    if target.ca_bundle is None:
        raise MySqlClientError("MySQL tls_verify=True requires ca_bundle")
    try:
        context = ssl.create_default_context(cafile=str(target.ca_bundle))
    except Exception:
        raise MySqlClientError("MySQL TLS CA bundle could not be loaded") from None
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def parse_connection_string(connection_string: str) -> MySqlConnectionOverride:
    """Parse a mysql:// URL into a sanitized connection override.

    Query ``tls_verify`` and ``ca_bundle`` values are explicitly mapped onto the
    override; parsing errors never echo the input, so credentials stay out of
    messages and repr.
    """

    if not isinstance(connection_string, str) or not connection_string.strip():
        raise MySqlClientError("MySQL connection string must be a nonblank mysql:// URL")
    try:
        parsed = urlsplit(connection_string.strip())
        if parsed.scheme.casefold() != "mysql" or parsed.hostname is None:
            raise ValueError("unsupported connection string")
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("invalid port")
        database = unquote(parsed.path.lstrip("/")) or None
        username = unquote(parsed.username) if parsed.username is not None else None
        password = unquote(parsed.password) if parsed.password is not None else None
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        tls_verify = _connection_tls_verify(query.get("tls_verify"))
        ca_bundle = query.get("ca_bundle")
        if ca_bundle is not None:
            ca_bundle = unquote(ca_bundle)
    except ValueError:
        raise MySqlClientError("MySQL connection string is invalid") from None
    return MySqlConnectionOverride(
        connection_string=connection_string.strip(),
        host=parsed.hostname,
        port=port,
        database=database,
        username=username,
        password=password,
        tls_verify=tls_verify,
        ca_bundle=ca_bundle,
    )


def resolve_connection(
    target: MySqlTarget,
    credentials: MySqlCredentials,
    override: MySqlConnectionOverride | None,
) -> tuple[MySqlTarget, MySqlCredentials]:
    """Merge one optional override into a target/credentials pair.

    A ``mysql://`` connection string takes priority, including its TLS/CA query
    values; otherwise individual override fields replace matching target fields.
    Missing host, database, or credentials raise ``ConfigError`` listing the
    missing fields and a safe remediation; credential values are never included.
    """

    if not isinstance(target, MySqlTarget) or not isinstance(credentials, MySqlCredentials):
        raise ConfigError("MySQL connection configuration is invalid")
    effective_credentials = credentials
    if override is not None and override.connection_string is not None:
        parsed = parse_connection_string(override.connection_string)
        merged = _merged_target(
            target,
            host=parsed.host,
            port=parsed.port,
            database=parsed.database,
            tls_verify=parsed.tls_verify,
            ca_bundle=parsed.ca_bundle,
            max_result_rows=None,
        )
        if parsed.username is not None or parsed.password is not None:
            if parsed.username is None or parsed.password is None:
                raise ConfigError(
                    "MySQL connection string requires both username and password"
                )
            effective_credentials = MySqlCredentials(parsed.username, parsed.password)
    elif override is not None:
        merged = _merged_target(
            target,
            host=override.host,
            port=override.port,
            database=override.database,
            tls_verify=override.tls_verify,
            ca_bundle=override.ca_bundle,
            max_result_rows=override.max_result_rows,
        )
        if override.username is not None or override.password is not None:
            if override.username is None or override.password is None:
                raise ConfigError(
                    "MySQL connection override requires both username and password"
                )
            effective_credentials = MySqlCredentials(override.username, override.password)
    else:
        merged = target
    missing = _missing_connection_fields(merged, effective_credentials)
    if missing:
        raise ConfigError(
            "MySQL connection is missing: "
            + ", ".join(missing)
            + "; provide a mysql:// connection_string or set the missing override fields"
        )
    return merged, effective_credentials


def _merged_target(
    target: MySqlTarget,
    *,
    host: str | None,
    port: int | None,
    database: str | None,
    tls_verify: bool | None,
    ca_bundle: str | None,
    max_result_rows: int | None,
) -> MySqlTarget:
    updates: dict[str, object] = {}
    if host is not None:
        updates["host"] = host
    if port is not None:
        updates["port"] = port
    if database is not None:
        updates["database"] = database
    if tls_verify is not None:
        updates["tls_verify"] = tls_verify
    if ca_bundle is not None:
        updates["ca_bundle"] = Path(ca_bundle)
    if max_result_rows is not None:
        updates["max_result_rows"] = max_result_rows
    return replace(target, **updates)


def _missing_connection_fields(
    target: MySqlTarget,
    credentials: MySqlCredentials,
) -> tuple[str, ...]:
    missing: list[str] = []
    if not isinstance(target.host, str) or not target.host:
        missing.append("host")
    if not isinstance(target.database, str) or not target.database:
        missing.append("database")
    if (
        not isinstance(credentials.username, str)
        or not credentials.username
        or not isinstance(credentials.password, str)
        or not credentials.password
    ):
        missing.append("credentials")
    return tuple(missing)


def _connection_tls_verify(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("invalid tls_verify")


def _require_driver_pre_authentication_hook() -> None:
    """Prove the supported driver still calls the security interception point."""

    base_connection = pymysql.connections.Connection
    if not callable(getattr(base_connection, "_request_authentication", None)):
        raise MySqlClientError("MySQL driver lacks a verified pre-authentication TLS hook")
    try:
        connect_source = inspect.getsource(base_connection.connect)
    except (OSError, TypeError):
        raise MySqlClientError("MySQL driver pre-authentication TLS hook cannot be verified") from None
    if "self._request_authentication()" not in connect_source:
        raise MySqlClientError("MySQL driver lacks a verified pre-authentication TLS hook")
