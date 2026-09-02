from __future__ import annotations

import configparser
import ipaddress
import os
import re
import stat
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from io import IOBase
from pathlib import Path

from .models import MySqlCredentials, MySqlTarget

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes


class ConfigError(ValueError):
    """A MySQL MCP configuration is absent, malformed, or unsafe."""


_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_ENVIRONMENT_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DNS_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
_CONFIG_OPTIONS = frozenset(
    {
        "name",
        "environment",
        "host",
        "port",
        "database",
        "tls_verify",
        "allow_insecure_tls",
        "ca_bundle",
        "connect_timeout_seconds",
        "policy_file",
        "migrations_dir",
        "migration_ledger_table",
        "read_only_environments",
        "username",
        "password",
        "username_env",
        "password_env",
        "max_result_rows",
        "require_confirmation",
    }
)
_TARGET_OPTIONS = _CONFIG_OPTIONS - {"name", "environment"}


def load_config(path: Path) -> MySqlTarget:
    """Load one externally managed MySQL INI without exposing its credentials."""

    source = _absolute_regular_file(path, label="MySQL INI configuration")
    _require_unlinked_ini_directory(source)
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        with _guarded_regular_file(source, binary=False) as handle:
            parser.read_file(handle)
    except (_UnsafePathError, OSError, UnicodeError, configparser.Error):
        raise ConfigError(
            "MySQL INI configuration must be an existing absolute regular file"
        ) from None
    if parser.defaults():
        raise ConfigError("MySQL INI configuration must not define defaults")
    sections = set(parser.sections())
    if "mysql" in sections:
        unknown_options = set(parser.options("mysql")) - _CONFIG_OPTIONS
        if unknown_options:
            raise ConfigError("MySQL INI configuration contains unknown options")
        return _build_target(
            parser,
            source,
            section="mysql",
            name=_identifier(
                _required_value(parser, "name"),
                option="name",
            ),
            environment=_identifier(
                _required_value(parser, "environment"),
                option="environment",
            ),
            database=_identifier(
                _required_value(parser, "database"),
                option="database",
            ),
            diagnostics=("using [mysql] section",),
        )
    if "environment" not in sections:
        raise ConfigError(
            "MySQL INI configuration requires a [mysql] or [environment] section"
        )
    if "target.mysql" not in sections:
        raise ConfigError("MySQL INI configuration requires a [target.mysql] section")
    _environment_section(parser)
    unknown_target_options = set(parser.options("target.mysql")) - _TARGET_OPTIONS
    if unknown_target_options:
        raise ConfigError(
            "MySQL INI configuration contains unknown [target.mysql] options"
        )
    environment = _identifier(
        _required_value(parser, "name", section="environment"),
        option="name",
    )
    return _build_target(
        parser,
        source,
        section="target.mysql",
        name=f"{environment}-mysql",
        environment=environment,
        database=_optional_identifier(
            parser,
            "database",
            section="target.mysql",
        ),
        diagnostics=("using [environment]+[target.mysql] sections",),
    )


def resolve_credentials(
    config: MySqlTarget,
    *,
    environ: Mapping[str, str],
) -> MySqlCredentials:
    """Resolve configured credentials without including values in error messages."""

    if config.username is not None and config.password is not None:
        return MySqlCredentials(config.username, config.password)
    if config.username_env is None or config.password_env is None:
        raise ConfigError("MySQL credential configuration is incomplete")
    username = environ.get(config.username_env)
    password = environ.get(config.password_env)
    if not username or not password:
        raise ConfigError("MySQL credential environment variables are missing or empty")
    return MySqlCredentials(username, password)


def safe_config_summary(config: MySqlTarget) -> str:
    """Return only non-secret configuration facts suitable for status output."""

    credential_source = "ini" if config.username is not None else "environment"
    return " ".join(
        (
            f"target={config.name}",
            f"environment={config.environment}",
            f"host={config.host}",
            f"port={config.port}",
            f"database={config.database}",
            f"tls_verify={config.tls_verify}",
            f"credential_source={credential_source}",
        )
    )


def _build_target(
    parser: configparser.ConfigParser,
    source: Path,
    *,
    section: str,
    name: str,
    environment: str,
    database: str | None,
    diagnostics: tuple[str, ...],
) -> MySqlTarget:
    host = _safe_host(_required_value(parser, "host", section=section))
    port = _port(_required_value(parser, "port", section=section))
    tls_verify = _boolean_value(
        parser,
        "tls_verify",
        section=section,
        default=False,
    )
    allow_insecure_tls = _boolean_value(
        parser,
        "allow_insecure_tls",
        section=section,
        default=False,
    )
    ca_value = _value(parser, "ca_bundle", section=section)
    ca_bundle = (
        None
        if ca_value is None
        else _relative_regular_file(source, ca_value, option="ca_bundle")
    )
    policy_value = _value(parser, "policy_file", section=section)
    policy_path = (
        None
        if policy_value is None
        else _relative_regular_file(source, policy_value, option="policy_file")
    )
    migrations_value = _value(parser, "migrations_dir", section=section)
    migrations_dir = (
        None
        if migrations_value is None
        else _relative_directory(source, migrations_value, option="migrations_dir")
    )
    ledger_value = _value(parser, "migration_ledger_table", section=section)
    migration_ledger_table = (
        None
        if ledger_value is None
        else _identifier(ledger_value, option="migration_ledger_table")
    )
    read_only_value = _value(parser, "read_only_environments", section=section)
    read_only_environments = (
        frozenset()
        if read_only_value is None
        else _environment_list(read_only_value, section=section)
    )
    username, password, username_env, password_env = _parse_credentials(
        parser,
        section=section,
    )
    return MySqlTarget(
        name=name,
        environment=environment,
        host=host,
        port=port,
        database=database,
        username=username,
        password=password,
        username_env=username_env,
        password_env=password_env,
        tls_verify=tls_verify,
        ca_bundle=ca_bundle,
        connect_timeout_seconds=_positive_integer(
            _value(parser, "connect_timeout_seconds", section=section),
            option="connect_timeout_seconds",
            default=10,
        ),
        read_only_environments=read_only_environments,
        source_path=source,
        policy_path=policy_path,
        migrations_dir=migrations_dir,
        migration_ledger_table=migration_ledger_table,
        allow_insecure_tls=allow_insecure_tls,
        max_result_rows=_positive_integer(
            _value(parser, "max_result_rows", section=section),
            option="max_result_rows",
            default=100,
        ),
        require_confirmation=_boolean_value(
            parser,
            "require_confirmation",
            section=section,
            default=False,
        ),
        diagnostics=diagnostics,
    )


def _absolute_regular_file(path: Path, *, label: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ConfigError(f"{label} must be an existing absolute regular file")
    return Path(os.path.abspath(path))


def _environment_section(parser: configparser.ConfigParser) -> None:
    """Validate [environment] metadata while tolerating unrelated keys."""

    if not parser.has_section("environment"):
        raise ConfigError("MySQL INI configuration requires an [environment] section")
    value = _value(parser, "name", section="environment")
    if value is None:
        raise ConfigError("[environment] name is required")
    _identifier(value, option="name")


def _identifier(value: str, *, option: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ConfigError(f"[mysql] {option} must be a conservative identifier")
    return value


def _optional_identifier(
    parser: configparser.ConfigParser,
    option: str,
    *,
    section: str = "mysql",
) -> str | None:
    value = _value(parser, option, section=section)
    if value is None:
        return None
    return _identifier(value, option=option)


def _safe_host(value: str) -> str:
    if (
        not value
        or any(character.isspace() or unicodedata.category(character).startswith("C") for character in value)
        or any(marker in value for marker in ("://", "@", "/", "?", "#", "\\"))
    ):
        raise ConfigError("[mysql] host must be a hostname or IP address")
    try:
        value.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        raise ConfigError("[mysql] host must be a hostname or IP address") from None
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if ":" in value or len(value) > 253 or not all(
            _DNS_LABEL.fullmatch(label) for label in value.split(".")
        ):
            raise ConfigError("[mysql] host must be a hostname or IP address")
    return value


def _port(value: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise ConfigError("[mysql] port must be an integer from 1 to 65535")
    parsed = int(value)
    if not 1 <= parsed <= 65535:
        raise ConfigError("[mysql] port must be an integer from 1 to 65535")
    return parsed


def _positive_integer(value: str | None, *, option: str, default: int) -> int:
    if value is None:
        return default
    if not value.isascii() or not value.isdecimal() or int(value) < 1:
        raise ConfigError(f"[mysql] {option} must be a positive integer")
    return int(value)


def _environment_list(value: str, *, section: str = "mysql") -> frozenset[str]:
    values = [item.strip() for item in value.split(",")]
    if not values or any(not item for item in values):
        raise ConfigError(
            f"[{section}] read_only_environments must be a nonempty list"
        )
    normalized = [_identifier(item, option="read_only_environments") for item in values]
    if len(set(normalized)) != len(normalized):
        raise ConfigError(
            f"[{section}] read_only_environments must not contain duplicates"
        )
    return frozenset(normalized)


def _parse_credentials(
    parser: configparser.ConfigParser,
    *,
    section: str = "mysql",
) -> tuple[str | None, str | None, str | None, str | None]:
    username = _value(parser, "username", section=section)
    password = _value(parser, "password", section=section)
    username_env = _value(parser, "username_env", section=section)
    password_env = _value(parser, "password_env", section=section)
    direct_declared = parser.has_option(section, "username") or parser.has_option(
        section,
        "password",
    )
    environment_declared = parser.has_option(
        section,
        "username_env",
    ) or parser.has_option(section, "password_env")
    if direct_declared and environment_declared:
        raise ConfigError(
            "MySQL credentials cannot mix direct values and environment references"
        )
    if direct_declared:
        if username is None or password is None:
            raise ConfigError("direct MySQL credentials require username and password")
        return username, password, None, None
    if environment_declared:
        if username_env is None or password_env is None:
            raise ConfigError(
                "environment MySQL credentials require both references"
            )
        return None, None, _environment_variable_name(username_env), _environment_variable_name(password_env)
    raise ConfigError("MySQL credentials are required")


def _relative_regular_file(source: Path, value: str, *, option: str) -> Path:
    candidate = _relative_path(source, value, option=option)
    try:
        with _guarded_regular_file(candidate, binary=True):
            pass
    except (_UnsafePathError, OSError):
        raise ConfigError(f"[mysql] {option} must be an existing regular file") from None
    return candidate


def _relative_directory(source: Path, value: str, *, option: str) -> Path:
    candidate = _relative_path(source, value, option=option)
    try:
        file_stat = os.lstat(candidate)
    except OSError:
        raise ConfigError(f"[mysql] {option} must be an existing directory") from None
    if _stat_is_reparse(file_stat) or not stat.S_ISDIR(file_stat.st_mode):
        raise ConfigError(f"[mysql] {option} must be an existing directory")
    if os.name == "nt" and not _opened_directory_matches(candidate):
        raise ConfigError(f"[mysql] {option} must not use links")
    return candidate


def _relative_path(source: Path, value: str, *, option: str) -> Path:
    raw = Path(value)
    if raw.is_absolute() or raw.drive or raw.anchor or not raw.parts or any(
        part in {"", ".", ".."} for part in raw.parts
    ):
        raise ConfigError(f"[mysql] {option} must be relative to the INI directory")
    base = source.parent
    candidate = Path(os.path.abspath(base / raw))
    try:
        candidate.relative_to(base)
    except ValueError:
        raise ConfigError(f"[mysql] {option} must be relative to the INI directory") from None
    current = base
    try:
        for part in raw.parts:
            current = current / part
            if _stat_is_reparse(os.lstat(current)):
                raise _UnsafePathError
    except (OSError, _UnsafePathError):
        raise ConfigError(f"[mysql] {option} must not use links") from None
    return candidate


def _require_unlinked_ini_directory(source: Path) -> None:
    current = source.parent
    try:
        while True:
            if _stat_is_reparse(os.lstat(current)):
                raise _UnsafePathError
            parent = current.parent
            if parent == current:
                return
            current = parent
    except (OSError, _UnsafePathError):
        raise ConfigError(
            "MySQL INI configuration directory must not use links"
        ) from None


def _value(
    parser: configparser.ConfigParser,
    option: str,
    section: str = "mysql",
) -> str | None:
    if not parser.has_option(section, option):
        return None
    value = parser.get(section, option).strip()
    return value or None


def _required_value(
    parser: configparser.ConfigParser,
    option: str,
    section: str = "mysql",
) -> str:
    value = _value(parser, option, section=section)
    if value is None:
        raise ConfigError(f"[{section}] {option} is required")
    return value


def _boolean_value(
    parser: configparser.ConfigParser,
    option: str,
    *,
    section: str = "mysql",
    default: bool,
) -> bool:
    value = _value(parser, option, section=section)
    if value is None:
        return default
    normalized = value.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"[{section}] {option} must be true or false")


def _environment_variable_name(value: str) -> str:
    if not _ENVIRONMENT_VARIABLE.fullmatch(value):
        raise ConfigError("[mysql] environment variable reference is invalid")
    return value


class _UnsafePathError(OSError):
    pass


@contextmanager
def _guarded_regular_file(path: Path, *, binary: bool) -> Iterator[IOBase]:
    descriptor = _open_regular_file_descriptor(path)
    try:
        if binary:
            handle = os.fdopen(descriptor, "rb")
        else:
            handle = os.fdopen(descriptor, "r", encoding="utf-8")
        descriptor = -1
        with handle:
            yield handle
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_regular_file_descriptor(path: Path) -> int:
    descriptor = -1
    flags = os.O_RDONLY
    for flag_name in ("O_BINARY", "O_CLOEXEC", "O_NOINHERIT", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, flag_name, 0)
    try:
        before = os.lstat(path)
        if _stat_is_reparse(before) or not stat.S_ISREG(before.st_mode):
            raise _UnsafePathError
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            _stat_is_reparse(opened)
            or not stat.S_ISREG(opened.st_mode)
            or not os.path.samestat(before, opened)
            or not _opened_path_matches(descriptor, path)
        ):
            raise _UnsafePathError
        after = os.lstat(path)
        if (
            _stat_is_reparse(after)
            or not stat.S_ISREG(after.st_mode)
            or not os.path.samestat(after, opened)
        ):
            raise _UnsafePathError
        return descriptor
    except (OSError, ValueError) as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if isinstance(exc, _UnsafePathError):
            raise
        raise _UnsafePathError from None


def _stat_is_reparse(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(file_stat.st_mode) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _opened_path_matches(descriptor: int, expected_path: Path) -> bool:
    if os.name != "nt":
        return True
    handle = msvcrt.get_osfhandle(descriptor)
    actual = _windows_final_path(handle)
    return actual is not None and actual == _normalize_windows_path(expected_path)


def _windows_final_path(handle: int) -> str | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_final_path.restype = wintypes.DWORD
    size = get_final_path(handle, None, 0, 0)
    if not size:
        return None
    buffer = ctypes.create_unicode_buffer(size + 1)
    written = get_final_path(handle, buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        return None
    return _normalize_windows_path(buffer.value)


def _opened_directory_matches(expected_path: Path) -> bool:
    handle = _windows_directory_handle(expected_path)
    if handle is None:
        return False
    try:
        actual = _windows_final_path(handle)
        return actual is not None and actual == _normalize_windows_path(expected_path)
    finally:
        _windows_close_handle(handle)


def _windows_directory_handle(path: Path) -> int | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x0080,  # FILE_READ_ATTRIBUTES
        0x0007,  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
        None,
        3,  # OPEN_EXISTING
        0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        return None
    return handle


def _windows_close_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    close_handle(handle)


def _normalize_windows_path(path: Path | str) -> str:
    value = str(path)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.normpath(value))
