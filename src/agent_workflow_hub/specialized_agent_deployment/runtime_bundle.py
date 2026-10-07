"""Versioned Hub runtime with no dependency on the editable development tree."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from pathlib import Path
from collections.abc import Mapping

from .contracts import DeploymentRequest, SkillSnapshot, canonical_sha256
from .sources import _assert_no_reparse_ancestry, _source_files, _stable_read, resolve_skill_source


class RuntimeBundleError(RuntimeError):
    """A release could not be prepared; active host configuration is untouched."""


def launcher_bytes() -> bytes:
    """Return a deterministic bootstrap that always imports the deployed source."""
    return (
        'from pathlib import Path\n'
        'import json\n'
        'import sys\n'
        'ROOT = Path(__file__).resolve().parent\n'
        'sys.path.insert(0, str(ROOT / "packages"))\n'
        'sys.path.insert(0, str(ROOT / "hub" / "src"))\n'
        'import agent_workflow_hub\n'
        'if sys.argv[1:] == ["--runtime-probe"]:\n'
        '    import mcp\n'
        '    print(json.dumps({"package": str(Path(agent_workflow_hub.__file__).resolve()), "dependency": str(Path(mcp.__file__).resolve()), "prefix": sys.prefix}))\n'
        'else:\n'
        '    from agent_workflow_hub.cli import entrypoint\n'
        '    raise SystemExit(entrypoint())\n'
    ).encode("utf-8")


def workflow_runner_bytes() -> bytes:
    """Return a runner for deployed workflow scripts using release-local packages."""
    return (
        'from pathlib import Path\n'
        'import runpy\n'
        'import sys\n'
        'ROOT = Path(__file__).resolve().parent\n'
        'sys.path.insert(0, str(ROOT / "packages"))\n'
        'sys.path.insert(0, str(ROOT / "hub" / "src"))\n'
        'if len(sys.argv) < 2:\n'
        '    raise SystemExit("workflow script path is required")\n'
        'relative = Path(sys.argv[1])\n'
        'if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".py":\n'
        '    raise SystemExit("workflow script must be a relative Python path")\n'
        'workflow_root = (ROOT / "hub" / "workflows").resolve()\n'
        'script = (ROOT / "hub" / relative).resolve()\n'
        'if not script.is_relative_to(workflow_root) or not script.is_file():\n'
        '    raise SystemExit("workflow script is outside the deployed workflows")\n'
        'sys.argv = [str(script), *sys.argv[2:]]\n'
        'runpy.run_path(str(script), run_name="__main__")\n'
    ).encode("utf-8")


def prepare_wheelhouse(
    hub: Path,
    python: Path,
    destination: Path,
    requirement_locks: tuple[Path, ...] = (),
) -> dict:
    """Download project-declared wheels to local staging; never install on the host."""
    for path in (hub, python, destination, *requirement_locks):
        _assert_no_reparse_ancestry(path)
    if destination.exists():
        raise RuntimeBundleError("wheelhouse destination is existing; use a new staging directory")
    project = tomllib.loads((hub / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [*project["build-system"]["requires"], "wheel", *project["project"].get("dependencies", [])]
    destination.mkdir(parents=True, exist_ok=False)
    _run([str(python), "-I", "-m", "pip", "--isolated", "download",
          "--index-url", "https://pypi.org/simple", "--only-binary=:all:",
          "--dest", str(destination), *requirements], destination)
    for lock in requirement_locks:
        if not lock.is_file():
            raise RuntimeBundleError(f"runtime requirement lock is missing: {lock}")
        _run([str(python), "-I", "-m", "pip", "--isolated", "download",
              "--index-url", "https://pypi.org/simple", "--only-binary=:all:",
              "--require-hashes", "--dest", str(destination), "-r", str(lock)], destination)
    return {"wheelhouse": str(destination), "wheels": len(list(destination.glob("*.whl"))),
            "requirements": requirements,
            "requirement_locks": [str(item) for item in requirement_locks]}


def runtime_python(runtime: Mapping) -> str:
    return str(Path(runtime["destination"]) / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))


def runtime_launch(runtime: Mapping) -> tuple[str, list[str], str]:
    root = Path(runtime["destination"])
    if runtime.get("mode") == "system-source":
        return (
            str(runtime["python"]),
            ["-I", str(root / "run-workflow-hub.py")],
            str(root / "hub"),
        )
    return runtime_python(runtime), ["-I", "-m", "agent_workflow_hub.cli"], str(root / "hub")


def runtime_commands(
    runtime: Mapping,
    build_requires: list[str],
    requirements: list[str] | None = None,
    requirement_files: list[str] | None = None,
) -> list[list[str]]:
    root = Path(runtime["destination"])
    if runtime.get("mode") == "system-source":
        launch = [runtime["python"], "-I", str(root / "run-workflow-hub.py")]
        commands = [
            [runtime["python"], "-I", "-m", "pip", "--isolated", "install",
             "--no-index", "--find-links", str(root / "wheelhouse"),
             "--ignore-installed", "--target", str(root / "packages"),
             *(requirements or [])],
        ]
        commands.extend(
            [runtime["python"], "-I", "-m", "pip", "--isolated", "install",
             "--no-index", "--find-links", str(root / "wheelhouse"),
             "--ignore-installed", "--target", str(root / "packages"),
             "--require-hashes", "-r", item]
            for item in (requirement_files or [])
        )
        commands.extend([
            [*launch, "--runtime-probe"],
            [*launch, "--help"],
            [*launch, "validate", str(root / "hub")],
        ])
        return commands
    pip = [runtime_python(runtime), "-I", "-m", "pip", "--isolated"]
    install = [*pip, "install", "--no-index", "--find-links", str(root / "wheelhouse")]
    commands = [
        [runtime["python"], "-I", "-m", "venv", "--copies", str(root / "venv")],
        [*install, *build_requires, "wheel"],
        [*install, "--no-build-isolation", str(root / "hub")],
    ]
    commands.extend([*install, "--require-hashes", "-r", item]
                    for item in (requirement_files or []))
    commands.append([*pip, "check"])
    return commands


def _record(source: Path, relative: str) -> dict:
    _assert_no_reparse_ancestry(source)
    content, _ = _stable_read(source)
    return {"source": str(source), "path": relative, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def _generated_record(relative: str, content: bytes) -> dict:
    return {"generated": content.decode("utf-8"), "path": relative,
            "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def plan_runtime(hub: Path, request: DeploymentRequest, snapshots: tuple[SkillSnapshot, ...]) -> dict:
    runtime = request.runtime
    if runtime is None:
        raise RuntimeBundleError("runtime was not requested")
    destination = Path(runtime["destination"])
    _assert_no_reparse_ancestry(destination)
    if destination.exists():
        raise RuntimeBundleError("runtime destination is existing; choose a new release directory")
    resolved_hub, resolved_destination = hub.resolve(), destination.resolve()
    if resolved_destination.is_relative_to(resolved_hub) or resolved_hub.is_relative_to(resolved_destination):
        raise RuntimeBundleError("runtime destination must be outside the development Hub")
    interpreter = Path(runtime["python"])
    if interpreter.resolve().is_relative_to(resolved_hub) or (interpreter.parent.parent / "pyvenv.cfg").exists():
        raise RuntimeBundleError("runtime.python must be a base Python, not a development virtualenv")
    interpreter = interpreter.resolve(strict=True)
    executable = _record(interpreter, "base-python")
    files = []
    for name in ("SKILL.md", "pyproject.toml", "README.md", "LICENSE"):
        path = hub / name
        if path.is_file():
            files.append(_record(path, f"hub/{name}"))
    for folder in ("src/agent_workflow_hub", "capabilities", "adapters"):
        if folder != "src/agent_workflow_hub" and not (hub / folder).exists():
            continue
        for path in _source_files(hub / folder):
            files.append(_record(path, "hub/" + path.relative_to(hub).as_posix()))
    for snapshot in snapshots:
        source = resolve_skill_source(hub, snapshot.selection)
        for item in snapshot.files:
            if Path(item.relative_path).name == ".env":
                raise RuntimeBundleError("private .env is not a deployable Skill resource; use an external config reference")
            record = _record(source / item.relative_path, f"hub/workflows/{snapshot.selection.name}/{item.relative_path}")
            if (record["sha256"], record["size"]) != (item.sha256, item.size):
                raise RuntimeBundleError("Skill changed while preparing runtime")
            files.append(record)
    wheels = Path(runtime["wheelhouse"])
    _assert_no_reparse_ancestry(wheels)
    wheel_files = sorted(wheels.glob("*.whl"))
    if not wheel_files:
        raise RuntimeBundleError("prepare a wheelhouse for this Python/platform first")
    files.extend(_record(path, f"wheelhouse/{path.name}") for path in wheel_files)
    requirement_files: list[str] = []
    for index, lock_value in enumerate(runtime.get("requirement_locks", ())):
        lock = Path(str(lock_value))
        _assert_no_reparse_ancestry(lock)
        if not lock.is_file():
            raise RuntimeBundleError(f"runtime requirement lock is missing: {lock}")
        relative = f"runtime-requirements/{index:02d}-{lock.name}"
        files.append(_record(lock, relative))
        requirement_files.append(str(destination / relative))
    if runtime["mode"] == "system-source":
        files.append(_generated_record("run-workflow-hub.py", launcher_bytes()))
        files.append(_generated_record("run-workflow-script.py", workflow_runner_bytes()))
    project = tomllib.loads((hub / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = list(project["project"].get("dependencies", ()))
    result = {
        **dict(runtime), "python": str(interpreter), "executable_sha256": executable["sha256"],
        "files": sorted(files, key=lambda item: item["path"]),
        "commands": runtime_commands(
            {**runtime, "python": str(interpreter)},
            project["build-system"]["requires"],
            requirements,
            requirement_files,
        ),
        "requirements": requirements,
        "requirement_files": requirement_files,
        "mcp_servers": list(effective_mcp_servers(request, tuple(
            {**item, "args": list(item["args"])} for item in request.host_options.get("mcp_servers", ())
        ))),
    }
    result["sha256"] = canonical_sha256(result)
    return result


def _run(argv: list[str], cwd: Path) -> str:
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("PYTHON", "PIP_", "UV_"))}
    env["PYTHONUTF8"] = "1"
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeBundleError(f"runtime command failed: {type(exc).__name__}") from exc
    if result.returncode:
        # Do not echo arbitrary process output (which can contain private config).
        raise RuntimeBundleError(f"runtime command failed (exit {result.returncode}): {argv[:5]}")
    return result.stdout


def prepare_runtime(plan: Mapping) -> dict:
    """Install a new immutable release before the adapter switches host pointers."""
    root = Path(plan["destination"])
    _assert_no_reparse_ancestry(root)
    if root.exists():
        raise RuntimeBundleError("runtime destination is existing")
    if _record(Path(plan["python"]), "base-python")["sha256"] != plan["executable_sha256"]:
        raise RuntimeBundleError("base Python changed since preview")
    # Verify everything before creating the release; check again while copying.
    for record in plan["files"]:
        if "source" in record and _record(Path(record["source"]), record["path"]) != dict(record):
            raise RuntimeBundleError(f"runtime source changed: {record['path']}")
    root.mkdir(parents=True, exist_ok=False)
    for record in plan["files"]:
        if "source" in record:
            content, _ = _stable_read(Path(record["source"]))
        else:
            content = record["generated"].encode("utf-8")
        if len(content) != record["size"] or hashlib.sha256(content).hexdigest() != record["sha256"]:
            raise RuntimeBundleError("runtime source changed during copy")
        target = root / record["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(content)
    for command in plan["commands"]:
        _run(list(command), root)
    result = verify_runtime(plan, require_receipt=False)
    with (root / "runtime-ready.json").open("x", encoding="utf-8") as stream:
        json.dump({"sha256": plan["sha256"], **result}, stream, ensure_ascii=False)
    return result


def verify_runtime(plan: Mapping, *, require_receipt: bool = True) -> dict:
    root = Path(plan["destination"])
    _assert_no_reparse_ancestry(root)
    if require_receipt:
        receipt = json.loads((root / "runtime-ready.json").read_text(encoding="utf-8"))
        if receipt["sha256"] != plan["sha256"]:
            raise RuntimeBundleError("runtime receipt does not match deployment")
    for record in plan["files"]:
        current = _record(root / record["path"], record["path"])
        if (current["sha256"], current["size"]) != (record["sha256"], record["size"]):
            raise RuntimeBundleError(f"deployed runtime file changed: {record['path']}")
    if plan["mode"] == "system-source":
        commands = list(plan["commands"])
        if not commands or "install" not in commands[0]:
            raise RuntimeBundleError("system runtime install command is missing")
        checks = [
            command for command in commands
            if not ("pip" in command and "install" in command)
        ]
        if not checks or "--runtime-probe" not in checks[0]:
            raise RuntimeBundleError("system runtime probe command is missing")
        outputs = [_run(list(command), root) for command in checks]
        imported = json.loads(outputs[0])
        package = Path(imported["package"])
        if not package.is_relative_to(root / "hub" / "src"):
            raise RuntimeBundleError("system Python loaded Hub outside the deployed source")
        dependency = Path(imported["dependency"])
        if not dependency.is_relative_to(root / "packages"):
            raise RuntimeBundleError("system Python loaded dependencies outside the deployed runtime")
        python = str(Path(plan["python"]))
    else:
        python = runtime_python(plan)
        probe = (
        "import pathlib,sys,json,agent_workflow_hub,importlib.metadata; "
        "p=pathlib.Path(agent_workflow_hub.__file__).resolve(); "
        "assert p.is_relative_to(pathlib.Path(sys.prefix).resolve()), str(p); "
        "d=json.loads(importlib.metadata.distribution('agent-workflow-hub').read_text('direct_url.json')); "
        "assert not d.get('dir_info',{}).get('editable',False), d; "
        "print(json.dumps({'package':str(p),'prefix':sys.prefix}))"
        )
        imported = json.loads(_run([python, "-I", "-c", probe], root))
        _run([python, "-I", "-m", "pip", "--isolated", "check"], root)
        _run([python, "-I", "-m", "agent_workflow_hub.cli", "--help"], root)
        _run([python, "-I", "-m", "agent_workflow_hub.cli", "validate", str(root / "hub")], root)
    mcp = []
    for server in plan.get("mcp_servers", ()):
        # Probe only the Hub services relocated into this runtime, not arbitrary
        # third-party transports. No business tools are invoked.
        if server["command"] != python:
            continue
        probe = (
            "import asyncio,json,sys\n"
            "s=json.loads(sys.argv[1])\n"
            "dependency_path=s.pop('dependency_path',None)\n"
            "if dependency_path: sys.path.insert(0,dependency_path)\n"
            "from mcp import ClientSession,StdioServerParameters\n"
            "from mcp.client.stdio import stdio_client\n"
            "async def main():\n"
            " async with asyncio.timeout(60):\n"
            "  async with stdio_client(StdioServerParameters(command=s['command'],args=s['args'],cwd=s['cwd'])) as (r,w):\n"
            "   async with ClientSession(r,w) as c:\n"
            "    init=await c.initialize()\n"
            "    found=await c.list_tools()\n"
            "    assert found.tools, 'no tools discovered'\n"
            "    print(json.dumps({'server':s['server_name'],'server_info':init.serverInfo.model_dump(),'tools':[t.name for t in found.tools]}))\n"
            "asyncio.run(main())\n"
        )
        binding = {**server, "args": list(server["args"])}
        if plan["mode"] == "system-source":
            binding["dependency_path"] = str(root / "packages")
        mcp.append(json.loads(_run([python, "-I", "-c", probe, json.dumps(binding)], root)))
    return {"status": "verified", **imported, "files": len(plan["files"]), "mcp": mcp}


def effective_mcp_servers(request: DeploymentRequest, servers: tuple[dict, ...]) -> tuple[dict, ...]:
    """Retarget Hub-provided MCPs only; third-party transports remain unchanged."""
    if request.runtime is None:
        return servers
    result = []
    for server in servers:
        args = list(server["args"])
        if args[:1] == ["-I"]:
            args = args[1:]
        if args[:2] == ["-m", "agent_workflow_hub.cli"]:
            command, prefix, cwd = runtime_launch(request.runtime)
            server = {**server, "command": command,
                      "args": [*prefix, *args[2:]], "cwd": cwd}
        result.append(server)
    return tuple(result)
