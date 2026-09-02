from __future__ import annotations

import configparser
import re
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

import yaml

from .models import ControllerConfig, JenkinsConfig, JenkinsCredentials, Policy
from .policy import PolicyError, parse_policy


class ConfigError(ValueError):
    pass


ENVIRONMENT_NAMES = frozenset({"nonproduction", "production"})
ENVIRONMENT_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def load_config(path: Path) -> JenkinsConfig:
    if not path.is_absolute() or not path.is_file():
        raise ConfigError("Jenkins INI configuration must be an existing absolute file")
    source = path.resolve()
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        with source.open("r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error):
        raise ConfigError("could not read Jenkins INI configuration") from None
    sections = set(parser.sections())
    if "jenkins" in sections:
        controller, policy_path = _parse_jenkins_section(parser, source)
        return JenkinsConfig(
            controllers={controller.name: controller},
            policy_path=policy_path,
        )
    if "environment" in sections and "target.jenkins" in sections:
        return _parse_shared_jenkins(parser, source)
    raise ConfigError(
        "Jenkins INI configuration requires a [jenkins] "
        "or [environment]+[target.jenkins] section"
    )


def load_policy(config: JenkinsConfig) -> Policy | None:
    if config.policy_path is None:
        return None
    try:
        raw = yaml.safe_load(config.policy_path.read_text(encoding="utf-8"))
    except OSError:
        raise ConfigError("could not read Jenkins policy") from None
    except yaml.YAMLError:
        raise ConfigError("could not parse Jenkins policy") from None
    if not isinstance(raw, Mapping):
        raise ConfigError("Jenkins policy must be a mapping")
    try:
        policy = parse_policy(raw)
    except PolicyError:
        raise ConfigError("Jenkins policy is invalid") from None
    unknown_controllers = sorted(
        {
            controller
            for rule in policy.rules
            for controller in rule.controllers
            if controller not in config.controllers
        }
    )
    if unknown_controllers:
        raise ConfigError("Jenkins policy references unknown controllers")
    return policy


def resolve_credentials(
    controller: ControllerConfig,
    *,
    environ: Mapping[str, str],
) -> JenkinsCredentials:
    if controller.username is not None and controller.token is not None:
        return JenkinsCredentials(username=controller.username, token=controller.token)
    if (
        controller.username is None
        and controller.token is None
        and controller.username_env is None
        and controller.token_env is None
    ):
        return JenkinsCredentials(username="", token="")
    if controller.username_env is None or controller.token_env is None:
        raise ConfigError("Jenkins credential configuration is incomplete")
    username = environ.get(controller.username_env)
    token = environ.get(controller.token_env)
    if not username or not token:
        raise ConfigError("Jenkins credential environment variables are missing or empty")
    return JenkinsCredentials(username=username, token=token)


def safe_config_summary(config: JenkinsConfig) -> str:
    lines = []
    for name, controller in sorted(config.controllers.items()):
        if controller.username is not None:
            credential_source = "ini"
        elif controller.username_env is not None:
            credential_source = "environment"
        else:
            credential_source = "anonymous"
        lines.append(
            " ".join(
                (
                    f"controller={name}",
                    f"url={controller.url}",
                    f"environment={controller.environment}",
                    f"credential_source={credential_source}",
                    f"allow_insecure_http={controller.allow_insecure_http}",
                    f"require_crumb={controller.require_crumb}",
                    f"ca_bundle={controller.ca_bundle or 'system'}",
                    f"confirm_writes={controller.confirm_writes}",
                    f"policy_path={config.policy_path}",
                )
            )
        )
    return "\n".join(lines)


def _parse_jenkins_section(
    parser: configparser.ConfigParser,
    source: Path,
) -> tuple[ControllerConfig, Path | None]:
    name = _required_value(parser, "name")
    url = _safe_url(_required_value(parser, "url"))
    environment = _required_value(parser, "environment")
    if environment not in ENVIRONMENT_NAMES:
        raise ConfigError(
            f"[jenkins] environment must be one of {sorted(ENVIRONMENT_NAMES)}"
        )
    allow_insecure_http = _boolean_value(parser, "allow_insecure_http", default=False)
    require_crumb = _boolean_value(parser, "require_crumb", default=False)
    parsed = urlparse(url)
    if parsed.scheme == "http" and not allow_insecure_http:
        raise ConfigError("[jenkins] url must use https unless allow_insecure_http is true")
    if parsed.scheme == "http" and environment != "nonproduction":
        raise ConfigError("[jenkins] allow_insecure_http is limited to nonproduction")
    if parsed.scheme == "https" and allow_insecure_http:
        raise ConfigError("[jenkins] allow_insecure_http only applies to http URLs")

    policy_value = _value(parser, "policy_file")
    policy_path = (
        None if policy_value is None else _relative_path(source, policy_value)
    )
    ca_bundle = _optional_ca_bundle(parser, source, scheme=parsed.scheme)
    confirm_writes = _boolean_value(parser, "confirm_writes", default=True)
    username, token, username_env, token_env = _parse_credentials(
        parser,
        allow_anonymous=True,
    )
    return (
        ControllerConfig(
            name=name,
            url=url,
            environment=environment,
            username_env=username_env,
            token_env=token_env,
            allow_insecure_http=allow_insecure_http,
            require_crumb=require_crumb,
            ca_bundle=ca_bundle,
            username=username,
            token=token,
            confirm_writes=confirm_writes,
        ),
        policy_path,
    )


def _parse_shared_jenkins(
    parser: configparser.ConfigParser,
    source: Path,
) -> JenkinsConfig:
    if parser.defaults():
        raise ConfigError("Jenkins INI configuration must not define defaults")
    environment_name = _shared_environment_name(parser)
    section = "target.jenkins"
    environment = _value(parser, "environment", section=section)
    if environment is None:
        environment = "nonproduction"
    if environment not in ENVIRONMENT_NAMES:
        raise ConfigError(
            f"[target.jenkins] environment must be one of {sorted(ENVIRONMENT_NAMES)}"
        )
    url = _shared_url(parser, section=section)
    allow_insecure_http = _boolean_value(
        parser,
        "allow_insecure_http",
        section=section,
        default=True,
    )
    require_crumb = _boolean_value(
        parser,
        "require_crumb",
        section=section,
        default=False,
    )
    parsed = urlparse(url)
    if parsed.scheme == "http" and environment != "nonproduction":
        raise ConfigError("[target.jenkins] production controllers must use https")
    if parsed.scheme == "http" and not allow_insecure_http:
        raise ConfigError(
            "[target.jenkins] nonproduction http URLs require an explicit insecure opt in"
        )
    if (
        parsed.scheme == "https"
        and allow_insecure_http
        and parser.has_option(section, "allow_insecure_http")
    ):
        raise ConfigError(
            "[target.jenkins] allow_insecure_http only applies to http URLs"
        )
    policy_value = _value(parser, "policy_file", section=section)
    policy_path = (
        None if policy_value is None else _relative_path(source, policy_value)
    )
    ca_bundle = _optional_ca_bundle(
        parser,
        source,
        scheme=parsed.scheme,
        section=section,
    )
    confirm_writes = _boolean_value(
        parser,
        "confirm_writes",
        section=section,
        default=True,
    )
    username, token, username_env, token_env = _parse_credentials(
        parser,
        section=section,
        allow_anonymous=True,
    )
    controller = ControllerConfig(
        name=f"{environment_name}-jenkins",
        url=url,
        environment=environment,
        username_env=username_env,
        token_env=token_env,
        allow_insecure_http=allow_insecure_http,
        require_crumb=require_crumb,
        ca_bundle=ca_bundle,
        username=username,
        token=token,
        confirm_writes=confirm_writes,
    )
    return JenkinsConfig(
        controllers={controller.name: controller},
        policy_path=policy_path,
    )


def _shared_environment_name(parser: configparser.ConfigParser) -> str:
    if not parser.has_section("environment"):
        raise ConfigError("Jenkins INI configuration requires an [environment] section")
    value = _value(parser, "name", section="environment")
    if value is None:
        raise ConfigError("[environment] name is required")
    if not _IDENTIFIER.fullmatch(value):
        raise ConfigError("[environment] name must be a conservative identifier")
    return value


def _shared_url(
    parser: configparser.ConfigParser,
    *,
    section: str,
) -> str:
    host = _required_value(parser, "host", section=section)
    port = _required_value(parser, "port", section=section)
    _port(port, section=section)
    if "://" in host:
        parsed = urlparse(host)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigError(
                f"[{section}] host must be an http or https hostname without credentials"
            )
        if parsed.port is not None:
            raise ConfigError(
                f"[{section}] host must not include a port when port is configured"
            )
        url = f"{host.rstrip('/')}:{port}"
    else:
        url = f"http://{host}:{port}"
    return _safe_url(url)


def _port(value: str, *, section: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise ConfigError(f"[{section}] port must be an integer from 1 to 65535")
    parsed = int(value)
    if not 1 <= parsed <= 65535:
        raise ConfigError(f"[{section}] port must be an integer from 1 to 65535")
    return parsed


def _parse_credentials(
    parser: configparser.ConfigParser,
    *,
    section: str = "jenkins",
    allow_anonymous: bool = False,
) -> tuple[str | None, str | None, str | None, str | None]:
    username = _value(parser, "username", section=section)
    token = _value(parser, "token", section=section)
    password = _value(parser, "password", section=section)
    username_env = _value(parser, "username_env", section=section)
    token_env = _value(parser, "token_env", section=section)
    direct_present = any(value is not None for value in (username, token, password))
    environment_present = any(value is not None for value in (username_env, token_env))
    if direct_present and environment_present:
        raise ConfigError("Jenkins credentials cannot mix direct values and environment references")
    if token is not None and password is not None:
        raise ConfigError("Jenkins credentials cannot define both token and password")
    if direct_present:
        if username is None or (token is None and password is None):
            raise ConfigError("direct Jenkins credentials require username and token or password")
        return username, token or password, None, None
    if environment_present:
        if username_env is None or token_env is None:
            raise ConfigError(
                "environment Jenkins credentials require username_env and token_env"
            )
        return None, None, _environment_variable_name(username_env), _environment_variable_name(token_env)
    if allow_anonymous:
        return None, None, None, None
    raise ConfigError("Jenkins credentials are required")


def _value(
    parser: configparser.ConfigParser,
    option: str,
    *,
    section: str = "jenkins",
) -> str | None:
    if not parser.has_option(section, option):
        return None
    value = parser.get(section, option).strip()
    return value or None


def _required_value(
    parser: configparser.ConfigParser,
    option: str,
    *,
    section: str = "jenkins",
) -> str:
    value = _value(parser, option, section=section)
    if value is None:
        raise ConfigError(f"[{section}] {option} is required")
    return value


def _boolean_value(
    parser: configparser.ConfigParser,
    option: str,
    *,
    section: str = "jenkins",
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


def _safe_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("[jenkins] url must be an http or https URL without credentials")
    return value.rstrip("/")


def _relative_path(source: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = source.parent / candidate
    try:
        return candidate.resolve(strict=False)
    except OSError:
        raise ConfigError("[jenkins] path is invalid") from None


def _optional_ca_bundle(
    parser: configparser.ConfigParser,
    source: Path,
    *,
    scheme: str,
    section: str = "jenkins",
) -> Path | None:
    value = _value(parser, "ca_bundle", section=section)
    if value is None:
        return None
    if scheme != "https":
        raise ConfigError(f"[{section}] ca_bundle requires an https URL")
    resolved = _relative_path(source, value)
    if not resolved.is_file():
        raise ConfigError(f"[{section}] ca_bundle must be an existing file")
    return resolved


def _environment_variable_name(value: str) -> str:
    if not ENVIRONMENT_VARIABLE.fullmatch(value):
        raise ConfigError("[jenkins] environment variable reference is invalid")
    return value
