"""Conservative code structure extraction without business-purpose invention."""

from __future__ import annotations

import ast
from pathlib import PurePosixPath
import re

from agent_workflow_hub.git_operations import validate_commit_sha

from .documents import KnowledgeChunk, _chunk


_SCRIPT_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".vue"}
_SCRIPT_SYMBOLS = re.compile(
    r"(?m)^\s*(?:export\s+)?(?:default\s+)?class\s+(?P<class>[A-Za-z_$][\w$]*)"
    r"|^\s*(?:export\s+)?(?:async\s+)?function\s+(?P<function>[A-Za-z_$][\w$]*)\s*\((?P<args>[^)]*)\)"
    r"|^\s*(?:export\s+)?const\s+(?P<arrow>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\((?P<arrow_args>[^)]*)\)\s*=>"
)


def extract_code_facts(
    *,
    source_id: str,
    path: str,
    content: bytes,
    commit_sha: str,
) -> tuple[KnowledgeChunk, ...]:
    sha = validate_commit_sha(commit_sha)
    suffix = PurePosixPath(path.replace("\\", "/")).suffix.casefold()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ()
    if suffix == ".py":
        return _python_facts(source_id, path, text, sha)
    if suffix in _SCRIPT_EXTENSIONS:
        return _script_facts(source_id, path, text, sha)
    return ()


def _python_facts(
    source_id: str,
    path: str,
    text: str,
    sha: str,
) -> tuple[KnowledgeChunk, ...]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return ()
    chunks: list[KnowledgeChunk] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        symbol_type = "class" if isinstance(node, ast.ClassDef) else "function"
        if isinstance(node, ast.ClassDef):
            signature = f"class {node.name}"
        else:
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            signature = f"{prefix} {node.name}({ast.unparse(node.args)})"
        documented = ast.get_docstring(node, clean=True)
        chunks.append(
            _fact_chunk(
                source_id=source_id,
                path=path,
                sha=sha,
                name=node.name,
                symbol_type=symbol_type,
                signature=signature,
                content=documented.strip() if documented else signature,
                line=getattr(node, "lineno", None),
                summary_state="documented" if documented else "needed",
            )
        )
    return tuple(chunks)


def _script_facts(
    source_id: str,
    path: str,
    text: str,
    sha: str,
) -> tuple[KnowledgeChunk, ...]:
    chunks: list[KnowledgeChunk] = []
    for match in _SCRIPT_SYMBOLS.finditer(text):
        if match.group("class"):
            name = match.group("class")
            symbol_type = "class"
            signature = f"class {name}"
        elif match.group("function"):
            name = match.group("function")
            symbol_type = "function"
            signature = f"function {name}({match.group('args').strip()})"
        else:
            name = match.group("arrow")
            symbol_type = "function"
            signature = f"const {name} = ({match.group('arrow_args').strip()}) =>"
        chunks.append(
            _fact_chunk(
                source_id=source_id,
                path=path,
                sha=sha,
                name=name,
                symbol_type=symbol_type,
                signature=signature,
                line=text.count("\n", 0, match.start()) + 1,
                summary_state="needed",
            )
        )
    return tuple(chunks)


def _fact_chunk(
    *,
    source_id: str,
    path: str,
    sha: str,
    name: str,
    symbol_type: str,
    signature: str,
    line: int | None,
    summary_state: str,
    content: str | None = None,
) -> KnowledgeChunk:
    return _chunk(
        source_id=source_id,
        source_kind="code-fact",
        source_version=sha,
        source_path=path,
        title=name,
        section=signature,
        content=content or signature,
        provenance={
            "commit_sha": sha,
            "source_path": path,
            "line": line,
            "symbol": name,
            "symbol_type": symbol_type,
            "signature": signature,
            "summary_state": summary_state,
        },
        inferred=False,
    )
