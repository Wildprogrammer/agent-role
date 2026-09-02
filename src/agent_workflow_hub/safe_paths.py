"""Safe cleanup planning that returns an advisory dry-run plan only.

Any deleting process must re-run the same containment and reparse checks
immediately before removal.
"""

from __future__ import annotations

import os
import stat as stat_module
from collections.abc import Iterable, Mapping
from pathlib import Path

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
REBUILDABLE_KINDS = frozenset({"cache", "temporary", "checkpoint"})
PROTECTED_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx"})
PROTECTED_FILENAMES = frozenset(
    {
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "auth.json",
        "client_secret.json",
        "client_secrets.json",
        "credentials",
        "credentials.json",
        "credentials.toml",
        "credentials.yaml",
        "credentials.yml",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secret",
        "secret.json",
        "secrets",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "service-account.json",
        "service_account.json",
        "token",
        "token.json",
        "tokens",
        "tokens.json",
    }
)
SSH_PRIVATE_KEY_NAMES = (
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
)


class UnsafePath(ValueError):
    pass


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        stat = os.lstat(path)
    except FileNotFoundError:
        return False
    return bool(
        getattr(stat, "st_file_attributes", 0)
        & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return path != root
    except ValueError:
        return False


def _is_equal_or_within(path: Path, root: Path) -> bool:
    return path == root or _is_within(path, root)


def _has_link_component(path: Path, stop: Path) -> bool:
    current = path
    while True:
        if _is_link_or_reparse(current):
            return True
        if current == stop or current.parent == current:
            return False
        current = current.parent


def _validated_allowed_root(path: Path, project_root: Path) -> Path:
    if _has_link_component(path, project_root):
        raise UnsafePath("link or reparse targets are not allowed")
    resolved = path.resolve(strict=False)
    if not _is_within(resolved, project_root):
        raise UnsafePath("allowed cleanup roots must stay inside project root")
    return resolved


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _validated_output_roots(project_root: Path) -> list[Path]:
    lexical_workflows = project_root / "workflows"
    workflows = _validated_allowed_root(lexical_workflows, project_root)
    workflows_stat = _lstat_or_none(lexical_workflows)
    if workflows_stat is None:
        return []
    if not stat_module.S_ISDIR(workflows_stat.st_mode):
        return []

    output_roots: list[Path] = []
    for lexical_workflow in sorted(
        workflows.iterdir(),
        key=lambda path: path.name,
    ):
        _validated_allowed_root(
            lexical_workflow,
            project_root,
        )
        workflow_stat = os.lstat(lexical_workflow)
        if not stat_module.S_ISDIR(workflow_stat.st_mode):
            continue

        lexical_output = lexical_workflow / "outputs"
        if _lstat_or_none(lexical_output) is None:
            continue
        output_roots.append(
            _validated_allowed_root(lexical_output, project_root)
        )
    return output_roots


def _is_lexical_output_descendant(path: Path, project_root: Path) -> bool:
    try:
        parts = path.relative_to(project_root).parts
    except ValueError:
        return False
    return (
        len(parts) > 3
        and parts[0] == "workflows"
        and parts[2] == "outputs"
    )


def _resolve_project_root(project_root: Path) -> Path:
    if _is_link_or_reparse(project_root):
        raise UnsafePath(
            "project root is a symlink, junction, or reparse point"
        )
    try:
        root = project_root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise UnsafePath(
            "project root must exist and be a directory"
        ) from exc
    root_stat = os.stat(root)
    if not stat_module.S_ISDIR(root_stat.st_mode):
        raise UnsafePath("project root must exist and be a directory")
    return root


def _reject_overlaps(paths: list[Path]) -> None:
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if _is_within(path, other) or _is_within(other, path):
                raise UnsafePath("cleanup targets overlap")


def _is_protected_name(path: Path) -> bool:
    name = path.name.casefold()
    is_private_ssh_key = not name.endswith(".pub") and any(
        name == base or name.startswith(f"{base}_")
        for base in SSH_PRIVATE_KEY_NAMES
    )
    return (
        name == ".env"
        or name.startswith(".env.")
        or name in PROTECTED_FILENAMES
        or is_private_ssh_key
        or path.suffix.casefold() in PROTECTED_SUFFIXES
    )


def _has_protected_component(parts: tuple[str, ...]) -> bool:
    return any(_is_protected_name(Path(part)) for part in parts)


def _validated_rebuildable_items(
    declarations: object,
) -> tuple[tuple[Path, str], ...]:
    if not isinstance(declarations, Mapping):
        raise UnsafePath("invalid rebuildable_roots mapping")
    try:
        raw_items = tuple(declarations.items())
    except Exception as exc:
        raise UnsafePath("invalid rebuildable_roots mapping") from exc

    validated: list[tuple[Path, str]] = []
    for item in raw_items:
        try:
            declaration, kind = item
        except Exception as exc:
            raise UnsafePath("invalid rebuildable declaration") from exc
        if not isinstance(declaration, Path):
            raise UnsafePath("invalid rebuildable declaration")
        if not isinstance(kind, str):
            raise UnsafePath("invalid rebuildable kind")
        try:
            hash(kind)
        except Exception as exc:
            raise UnsafePath("invalid rebuildable kind") from exc
        validated.append((declaration, kind))
    return tuple(validated)


def _validated_protected_paths(
    protected_paths: object,
) -> tuple[tuple[str, ...], ...]:
    if isinstance(protected_paths, (str, bytes, Path)) or not isinstance(
        protected_paths,
        Iterable,
    ):
        raise UnsafePath("invalid protected_paths collection")
    try:
        declarations = tuple(protected_paths)
    except Exception as exc:
        raise UnsafePath("invalid protected_paths collection") from exc

    validated: list[tuple[str, ...]] = []
    for declaration in declarations:
        if not isinstance(declaration, Path):
            raise UnsafePath("invalid protected path declaration")
        if (
            declaration.is_absolute()
            or declaration.anchor
            or ".." in declaration.parts
            or not declaration.parts
        ):
            raise UnsafePath("invalid protected path declaration")
        validated.append(
            tuple(part.casefold() for part in declaration.parts)
        )
    return tuple(validated)


def _validated_rebuildable_roots(
    declarations: tuple[tuple[Path, str], ...],
    project_root: Path,
) -> list[Path]:
    validated: list[Path] = []
    declaration_identities: set[str] = set()
    resolved_identities: set[str] = set()
    for declaration, kind in declarations:
        if kind not in REBUILDABLE_KINDS:
            raise UnsafePath("invalid rebuildable kind")
        if (
            declaration.is_absolute()
            or declaration.anchor
            or ".." in declaration.parts
            or len(declaration.parts) < 2
            or declaration.parts[0] != "workspace"
        ):
            raise UnsafePath("invalid rebuildable declaration")
        if _has_protected_component(declaration.parts[1:]):
            raise UnsafePath("protected credential cannot be cleaned")

        lexical = project_root / declaration
        resolved = _validated_allowed_root(lexical, project_root)
        declaration_identity = declaration.as_posix().casefold()
        resolved_identity = resolved.as_posix().casefold()
        if (
            declaration_identity in declaration_identities
            or resolved_identity in resolved_identities
        ):
            raise UnsafePath("duplicate rebuildable declaration")
        declaration_identities.add(declaration_identity)
        resolved_identities.add(resolved_identity)
        validated.append(resolved)
    return sorted(validated)


def _portable_paths_overlap(
    left: tuple[str, ...],
    right: tuple[str, ...],
) -> bool:
    shared_length = min(len(left), len(right))
    return left[:shared_length] == right[:shared_length]


def _stat_is_link_or_reparse(stat: os.stat_result) -> bool:
    return stat_module.S_ISLNK(stat.st_mode) or bool(
        getattr(stat, "st_file_attributes", 0)
        & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _validate_selected_tree(path: Path) -> None:
    if _is_protected_name(path):
        raise UnsafePath("protected credential cannot be cleaned")

    target_stat = os.lstat(path)
    if _stat_is_link_or_reparse(target_stat):
        raise UnsafePath("link or reparse targets are not allowed")
    if not stat_module.S_ISDIR(target_stat.st_mode):
        return

    pending = [path]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            names = sorted(entry.name for entry in entries)

        directories: list[Path] = []
        for name in names:
            descendant = Path(current) / name
            if _is_protected_name(descendant):
                raise UnsafePath(
                    "protected credential cannot be cleaned"
                )
            descendant_stat = os.lstat(descendant)
            if _stat_is_link_or_reparse(descendant_stat):
                raise UnsafePath(
                    "link or reparse targets are not allowed"
                )
            if stat_module.S_ISDIR(descendant_stat.st_mode):
                directories.append(descendant)
        pending.extend(reversed(directories))


def plan_clean(
    project_root: Path,
    targets: list[Path],
    *,
    rebuildable_roots: Mapping[Path, str],
    protected_paths: Iterable[Path],
    include_outputs: bool,
) -> list[Path]:
    """Return an advisory dry-run plan without deleting or writing anything.

    This advisory scan assumes a stable filesystem. Tool-specific protected
    paths must be supplied by orchestration because built-in names are not
    exhaustive. A future deleter must re-run checks immediately before removal.
    It must use handle-relative/no-follow operations; returned paths are not a
    deletion capability.
    """

    if type(include_outputs) is not bool:
        raise UnsafePath("include_outputs must be a boolean")
    if not isinstance(project_root, Path):
        raise UnsafePath("invalid project_root")
    if not isinstance(targets, list) or any(
        not isinstance(target, Path) for target in targets
    ):
        raise UnsafePath("invalid targets list")
    rebuildable_items = _validated_rebuildable_items(rebuildable_roots)
    protected_declarations = _validated_protected_paths(protected_paths)
    if not targets:
        raise UnsafePath("at least one cleanup target is required")
    for raw in targets:
        if ".." in raw.parts:
            raise UnsafePath("path traversal is not allowed")

    try:
        root = _resolve_project_root(project_root)
        workspace = _validated_allowed_root(root / "workspace", root)
        declared_roots = _validated_rebuildable_roots(
            rebuildable_items,
            root,
        )
        output_roots = (
            _validated_output_roots(root)
            if include_outputs
            else []
        )

        planned: list[Path] = []
        for raw in targets:
            unresolved = raw if raw.is_absolute() else root / raw
            if _has_link_component(unresolved, root):
                raise UnsafePath(
                    "link or reparse targets are not allowed"
                )
            target = unresolved.resolve(strict=False)
            try:
                os.stat(target)
            except FileNotFoundError as exc:
                raise UnsafePath(
                    "cleanup target does not exist"
                ) from exc

            in_workspace = _is_within(target, workspace)
            matched_output_root: Path | None = None
            if include_outputs:
                matched_output_root = next(
                    (
                        output_root
                        for output_root in output_roots
                        if _is_within(target, output_root)
                    ),
                    None,
                )
                in_outputs = matched_output_root is not None
            else:
                in_outputs = _is_lexical_output_descendant(
                    target,
                    root,
                )
            if in_outputs and not include_outputs:
                raise UnsafePath("include_outputs must be explicit")
            if not in_workspace and not (
                include_outputs and in_outputs
            ):
                raise UnsafePath(
                    "target is outside allowed cleanup roots"
                )
            target_declaration = tuple(
                part.casefold()
                for part in target.relative_to(root).parts
            )
            if any(
                _portable_paths_overlap(
                    target_declaration,
                    protected_declaration,
                )
                for protected_declaration in protected_declarations
            ):
                raise UnsafePath(
                    "target overlaps a declared protected path"
                )
            if in_workspace and not any(
                _is_equal_or_within(target, declared_root)
                for declared_root in declared_roots
            ):
                raise UnsafePath(
                    "workspace target is not classified as rebuildable"
                )
            if in_workspace and _has_protected_component(
                target.relative_to(workspace).parts
            ):
                raise UnsafePath(
                    "protected credential cannot be cleaned"
                )
            if (
                matched_output_root is not None
                and _has_protected_component(
                    target.relative_to(matched_output_root).parts
                )
            ):
                raise UnsafePath(
                    "protected credential cannot be cleaned"
                )
            _validate_selected_tree(target)
            planned.append(target)

        unique = sorted(set(planned))
        _reject_overlaps(unique)
        return unique
    except UnsafePath:
        raise
    except OSError as exc:
        raise UnsafePath(f"filesystem access failed: {exc}") from exc
