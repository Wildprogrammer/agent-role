from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode


class FrontmatterError(ValueError):
    pass


class DuplicateKeyError(ConstructorError):
    pass


class DuplicateSafeLoader(yaml.SafeLoader):
    def construct_mapping(
        self, node: MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        if not isinstance(node, MappingNode):
            raise ConstructorError(
                None,
                None,
                f"expected a mapping node, but found {node.id}",
                node.start_mark,
            )
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found unhashable key",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise DuplicateKeyError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found duplicate key",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def parse_markdown(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    return parse_markdown_text(text, path)


def parse_markdown_text(
    text: str,
    source_path: Path,
) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise FrontmatterError(
            f"{source_path}: missing opening frontmatter delimiter"
        )
    try:
        closing = next(i for i, line in enumerate(lines[1:], 1) if line == "---")
    except StopIteration as exc:
        raise FrontmatterError(
            f"{source_path}: missing closing frontmatter delimiter"
        ) from exc
    try:
        data = yaml.load(
            "\n".join(lines[1:closing]),
            Loader=DuplicateSafeLoader,
        )
    except yaml.YAMLError as exc:
        category = (
            "duplicate key"
            if isinstance(exc, DuplicateKeyError)
            else "invalid YAML"
        )
        problem_mark = getattr(exc, "problem_mark", None)
        location = ""
        if problem_mark is not None:
            location = (
                f" at line {problem_mark.line + 1}, "
                f"column {problem_mark.column + 1}"
            )
        raise FrontmatterError(
            f"{source_path}: {category} frontmatter{location}"
        ) from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise FrontmatterError(
            f"{source_path}: frontmatter must be a mapping"
        )
    return data, "\n".join(lines[closing + 1 :]).lstrip()
