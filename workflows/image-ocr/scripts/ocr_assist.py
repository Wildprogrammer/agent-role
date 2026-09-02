from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Mapping


SUPPORTED_IMAGE_SUFFIXES = {
    ".bmp",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
OFFLINE_ENVIRONMENT = {"PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True"}
SCRIPTS_DIRECTORY = Path(__file__).resolve().parent
WORKER_PATH = SCRIPTS_DIRECTORY / "paddleocr_worker.py"
UMI_WORKER_PATH = SCRIPTS_DIRECTORY / "umiocr_worker.py"
PADDLE_WORKER_TIMEOUT_SECONDS = 120
UMIOCR_WORKER_TIMEOUT_SECONDS = 60


class WorkflowError(Exception):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


@dataclass(frozen=True)
class PaddleConfig:
    python: Path
    det_model: Path
    rec_model: Path
    orientation_model: Path | None
    manifest: Path


@dataclass(frozen=True)
class UmiOcrConfig:
    executable: Path


def _existing_absolute_file(value: str | Path, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise WorkflowError("needs-dependency", f"{label} must be an existing absolute file")
    return path.resolve(strict=True)


def _existing_absolute_directory(value: str | Path, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise WorkflowError("needs-dependency", f"{label} must be an existing absolute directory")
    return path.resolve(strict=True)


def existing_absolute_images(values: list[str]) -> list[Path]:
    if not values:
        raise WorkflowError("invalid-input", "at least one --input image is required")

    images: list[Path] = []
    for value in values:
        candidate = Path(value)
        if candidate.suffix.casefold() not in SUPPORTED_IMAGE_SUFFIXES:
            raise WorkflowError("invalid-input", "input must use a supported image suffix")
        if not candidate.is_absolute() or not candidate.is_file():
            raise WorkflowError("invalid-input", "input must be an existing absolute image file")
        images.append(candidate.resolve(strict=True))
    return images


def directory_digest(directory: Path) -> str:
    resolved = _existing_absolute_directory(directory, "model directory")
    digest = hashlib.sha256()
    files = sorted(
        (path for path in resolved.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(resolved).as_posix(),
    )
    if not files:
        raise WorkflowError("needs-dependency", "model directory must contain files")

    for file_path in files:
        relative = file_path.relative_to(resolved).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _new_absolute_path(value: Path, suffixes: set[str], label: str) -> Path:
    if not value.is_absolute():
        raise WorkflowError("invalid-input", f"{label} must be an absolute path")
    if value.suffix.casefold() not in suffixes:
        allowed = ".txt or .md" if suffixes == {".txt", ".md"} else " or ".join(sorted(suffixes))
        raise WorkflowError("invalid-input", f"{label} must use {allowed}")
    parent = value.parent
    if not parent.is_dir():
        raise WorkflowError("invalid-input", f"{label} parent directory must exist")
    if value.exists():
        raise FileExistsError(f"{label} already exists: {value}")
    return value.resolve(strict=False)


def new_output_path(value: str) -> Path:
    return _new_absolute_path(Path(value), {".txt", ".md"}, "output")


def _new_manifest_path(value: Path) -> Path:
    return _new_absolute_path(value, {".json"}, "manifest")


def write_manifest(path: Path, models: dict[str, Path]) -> None:
    target = _new_manifest_path(path)
    if set(models) - {"det", "rec", "orientation"}:
        raise WorkflowError("invalid-input", "manifest models may only be det, rec, or orientation")
    if not {"det", "rec"}.issubset(models):
        raise WorkflowError("invalid-input", "manifest requires det and rec model directories")

    payload = {
        "schema": 1,
        "models": {
            name: {
                "path": str(_existing_absolute_directory(model, f"{name} model")),
                "sha256": directory_digest(model),
            }
            for name, model in sorted(models.items())
        },
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_manifest(path: Path) -> Mapping[str, Any]:
    manifest = _existing_absolute_file(path, "manifest")
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError("needs-dependency", "manifest must be valid UTF-8 JSON") from error
    if not isinstance(payload, Mapping) or payload.get("schema") != 1:
        raise WorkflowError("needs-dependency", "manifest schema must equal 1")
    if not isinstance(payload.get("models"), Mapping):
        raise WorkflowError("needs-dependency", "manifest must contain a models mapping")
    return payload


def _validate_manifest_model(models: Mapping[str, Any], name: str, directory: Path) -> None:
    entry = models.get(name)
    if not isinstance(entry, Mapping):
        raise WorkflowError("needs-dependency", f"manifest is missing {name} model")
    expected_path = entry.get("path")
    expected_digest = entry.get("sha256")
    if not isinstance(expected_path, str) or not isinstance(expected_digest, str):
        raise WorkflowError("needs-dependency", f"manifest {name} entry is incomplete")
    if Path(expected_path).resolve(strict=False) != directory:
        raise WorkflowError("needs-dependency", f"manifest path does not match {name} model")
    actual_digest = directory_digest(directory)
    if actual_digest != expected_digest:
        raise WorkflowError("needs-dependency", f"manifest digest does not match {name} model")


def load_paddle_config(
    det: Path,
    rec: Path,
    orientation: Path | None,
    manifest: Path,
    python: Path,
) -> PaddleConfig:
    python_path = _existing_absolute_file(python, "PaddleOCR Python")
    det_model = _existing_absolute_directory(det, "detection model")
    rec_model = _existing_absolute_directory(rec, "recognition model")
    orientation_model = (
        _existing_absolute_directory(orientation, "orientation model")
        if orientation is not None
        else None
    )
    manifest_path = _existing_absolute_file(manifest, "manifest")
    manifest_payload = _load_manifest(manifest_path)
    models = manifest_payload["models"]
    assert isinstance(models, Mapping)
    _validate_manifest_model(models, "det", det_model)
    _validate_manifest_model(models, "rec", rec_model)
    if orientation_model is not None:
        _validate_manifest_model(models, "orientation", orientation_model)
    return PaddleConfig(
        python=python_path,
        det_model=det_model,
        rec_model=rec_model,
        orientation_model=orientation_model,
        manifest=manifest_path,
    )


def load_umiocr_config(executable: Path) -> UmiOcrConfig:
    return UmiOcrConfig(
        executable=_existing_absolute_file(executable, "Umi-OCR PaddleOCR-json executable")
    )


def _worker_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(OFFLINE_ENVIRONMENT)
    return environment


def _run_json_worker(
    worker_path: Path,
    request: Mapping[str, Any],
    *,
    python: Path,
    timeout: int,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    descriptor, temporary_name = tempfile.mkstemp(prefix="image-ocr-", suffix=".json")
    request_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(request, handle, ensure_ascii=False)
        options: dict[str, Any] = {
            "check": False,
            "cwd": str(SCRIPTS_DIRECTORY),
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": timeout,
        }
        if environment is not None:
            options["env"] = dict(environment)
        try:
            completed = subprocess.run(
                [str(python), str(worker_path), str(request_path)],
                **options,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "processing-failed",
                "message": "local OCR worker timed out",
            }
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            if completed.returncode != 0:
                return {
                    "status": "processing-failed",
                    "message": completed.stderr.strip() or "OCR worker failed",
                }
            return {
                "status": "processing-failed",
                "message": "OCR worker returned invalid JSON",
            }
        if not isinstance(payload, dict):
            return {
                "status": "processing-failed",
                "message": "OCR worker returned an invalid payload",
            }
        return payload
    finally:
        request_path.unlink(missing_ok=True)


def run_paddle_worker(image: Path, config: PaddleConfig, language: str) -> dict[str, Any]:
    return _run_json_worker(
        WORKER_PATH,
        {
            "image": str(image),
            "det_model": str(config.det_model),
            "rec_model": str(config.rec_model),
            "orientation_model": str(config.orientation_model) if config.orientation_model else None,
            "language": language,
        },
        python=config.python,
        timeout=PADDLE_WORKER_TIMEOUT_SECONDS,
        environment=_worker_environment(),
    )


def run_umiocr_worker(image: Path | None, config: UmiOcrConfig) -> dict[str, Any]:
    request: dict[str, Any] = {
        "mode": "run" if image is not None else "doctor",
        "executable": str(config.executable),
    }
    if image is not None:
        request["image"] = str(image)
    return _run_json_worker(
        UMI_WORKER_PATH,
        request,
        python=Path(sys.executable),
        timeout=UMIOCR_WORKER_TIMEOUT_SECONDS,
    )


def normalise_worker_result(payload: Mapping[str, Any]) -> dict[str, str]:
    if payload.get("status") != "success":
        message = payload.get("message")
        return {
            "status": "processing-failed",
            "message": message if isinstance(message, str) and message else "OCR processing failed",
        }
    text = payload.get("text")
    reliable = payload.get("reliable")
    notice = payload.get("notice")
    if isinstance(text, str) and text.strip() and (
        reliable is True or isinstance(notice, str) and notice.strip()
    ):
        result = {"status": "success", "text": text.strip()}
        if isinstance(notice, str) and notice.strip():
            result["notice"] = notice.strip()
        return result
    if not isinstance(text, str) or not text.strip() or reliable is not True:
        return {"status": "success", "text": "", "notice": "无法可靠识别"}
    return {"status": "success", "text": text.strip()}


def run_paddle(images: list[Path], config: PaddleConfig, language: str) -> dict[str, Any]:
    results: list[dict[str, str]] = []
    success_count = 0
    for image in images:
        item = {"input": str(image), **normalise_worker_result(run_paddle_worker(image, config, language))}
        results.append(item)
        if item["status"] == "success":
            success_count += 1
    if success_count == len(results):
        status = "success"
    elif success_count:
        status = "partial-success"
    else:
        status = "processing-failed"
    return {"status": status, "engine": "paddleocr", "results": results}


def run_umiocr(images: list[Path], config: UmiOcrConfig) -> dict[str, Any]:
    results: list[dict[str, str]] = []
    success_count = 0
    for image in images:
        item = {"input": str(image), **normalise_worker_result(run_umiocr_worker(image, config))}
        results.append(item)
        if item["status"] == "success":
            success_count += 1
    if success_count == len(results):
        status = "success"
    elif success_count:
        status = "partial-success"
    else:
        status = "processing-failed"
    return {"status": status, "engine": "umiocr", "results": results}


def run_tesseract(
    images: list[Path],
    executable: Path,
    tessdata_dir: Path,
    language: str,
) -> dict[str, Any]:
    command = _existing_absolute_file(executable, "Tesseract executable")
    data_directory = _existing_absolute_directory(tessdata_dir, "tessdata directory")
    results: list[dict[str, str]] = []
    success_count = 0
    for image in images:
        completed = subprocess.run(
            [str(command), str(image), "stdout", "-l", language, "--tessdata-dir", str(data_directory)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        payload: dict[str, Any]
        if completed.returncode:
            payload = {"status": "processing-failed", "message": completed.stderr.strip() or "Tesseract failed"}
        else:
            payload = {"status": "success", "text": completed.stdout, "reliable": bool(completed.stdout.strip())}
        item = {"input": str(image), **normalise_worker_result(payload)}
        results.append(item)
        if item["status"] == "success":
            success_count += 1
    status = "success" if success_count == len(results) else "partial-success" if success_count else "processing-failed"
    return {"status": status, "engine": "tesseract", "results": results}


def _paddle_version(config: PaddleConfig) -> str:
    completed = subprocess.run(
        [
            str(config.python),
            "-c",
            "import os; os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK']='True'; "
            "from importlib.metadata import version; print(version('paddleocr'))",
        ],
        check=False,
        env=_worker_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        raise WorkflowError(
            "needs-dependency",
            completed.stderr.strip() or "PaddleOCR is not ready in the selected Python runtime",
        )
    version = completed.stdout.strip().splitlines()
    if not version:
        raise WorkflowError("needs-dependency", "PaddleOCR version could not be read")
    return version[-1]


def _required_path(arguments: argparse.Namespace, name: str, engine: str) -> Path:
    value = getattr(arguments, name)
    if value is None:
        raise WorkflowError("needs-dependency", f"--{name.replace('_', '-')} is required for {engine}")
    return Path(value)


def _paddle_config_from_arguments(arguments: argparse.Namespace) -> PaddleConfig:
    return load_paddle_config(
        _required_path(arguments, "det_model", "PaddleOCR"),
        _required_path(arguments, "rec_model", "PaddleOCR"),
        Path(arguments.orientation_model) if arguments.orientation_model else None,
        _required_path(arguments, "manifest", "PaddleOCR"),
        Path(arguments.paddle_python),
    )


def _umiocr_config_from_arguments(arguments: argparse.Namespace) -> UmiOcrConfig:
    return load_umiocr_config(
        _required_path(arguments, "umiocr_executable", "Umi-OCR")
    )


def _doctor(arguments: argparse.Namespace) -> dict[str, Any]:
    if arguments.engine == "umiocr":
        payload = run_umiocr_worker(None, _umiocr_config_from_arguments(arguments))
        if payload.get("status") != "ready":
            message = payload.get("message")
            raise WorkflowError(
                "needs-dependency",
                message if isinstance(message, str) and message else "Umi-OCR is not ready",
            )
        return payload

    if arguments.engine == "paddleocr":
        config = _paddle_config_from_arguments(arguments)
        return {
            "status": "ready",
            "engine": "paddleocr",
            "paddleocr_version": _paddle_version(config),
            "offline_guard": OFFLINE_ENVIRONMENT["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"],
        }

    executable = _existing_absolute_file(
        _required_path(arguments, "tesseract_executable", "Tesseract"),
        "Tesseract executable",
    )
    _existing_absolute_directory(
        _required_path(arguments, "tessdata_dir", "Tesseract"),
        "tessdata directory",
    )
    completed = subprocess.run(
        [str(executable), "--version"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        raise WorkflowError("needs-dependency", completed.stderr.strip() or "Tesseract version check failed")
    return {"status": "ready", "engine": "tesseract", "version": completed.stdout.splitlines()[0]}


def run_selected_engine(arguments: argparse.Namespace) -> dict[str, Any]:
    images = existing_absolute_images(arguments.input)
    if arguments.engine == "umiocr":
        if arguments.language:
            raise WorkflowError(
                "invalid-input",
                "Umi-OCR uses its installed default language profile; select PaddleOCR or Tesseract for another language",
            )
        return run_umiocr(images, _umiocr_config_from_arguments(arguments))
    if arguments.engine == "paddleocr":
        config = _paddle_config_from_arguments(arguments)
        _paddle_version(config)
        return run_paddle(images, config, arguments.language or "ch")
    return run_tesseract(
        images,
        _required_path(arguments, "tesseract_executable", "Tesseract"),
        _required_path(arguments, "tessdata_dir", "Tesseract"),
        arguments.language or "chi_sim+eng",
    )


def deliver(payload: Mapping[str, Any], output: Path | None) -> str:
    results = payload.get("results")
    if not isinstance(results, list):
        raise WorkflowError("processing-failed", "OCR result did not contain image results")
    multiple = len(results) > 1
    rendered: list[str] = []
    for item in results:
        if not isinstance(item, Mapping):
            raise WorkflowError("processing-failed", "OCR result item was invalid")
        input_path = item.get("input")
        text = item.get("text")
        notice = item.get("notice")
        message = item.get("message")
        content = (
            f"{text.strip()}\n\n{notice.strip()}"
            if isinstance(text, str)
            and text.strip()
            and isinstance(notice, str)
            and notice.strip()
            else text.strip()
            if isinstance(text, str) and text.strip()
            else notice
            if isinstance(notice, str) and notice
            else f"处理失败：{message}" if isinstance(message, str) and message else "无法可靠识别"
        )
        if multiple:
            title = Path(input_path).name if isinstance(input_path, str) else "未命名图片"
            rendered.append(f"{title}\n{content}")
        else:
            rendered.append(content)
    text_output = "\n\n".join(rendered)
    if output is not None:
        target = new_output_path(str(output))
        target.write_text(text_output + "\n", encoding="utf-8")
    return text_output


class WorkflowArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise WorkflowError("invalid-input", message)


def _add_engine_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--engine", choices=("umiocr", "paddleocr", "tesseract"), default="umiocr")
    parser.add_argument("--umiocr-executable")
    parser.add_argument("--paddle-python", default=sys.executable)
    parser.add_argument("--det-model")
    parser.add_argument("--rec-model")
    parser.add_argument("--orientation-model")
    parser.add_argument("--manifest")
    parser.add_argument("--tesseract-executable")
    parser.add_argument("--tessdata-dir")


def _manifest(arguments: argparse.Namespace) -> dict[str, Any]:
    models = {
        "det": _required_path(arguments, "det_model", "manifest"),
        "rec": _required_path(arguments, "rec_model", "manifest"),
    }
    if arguments.orientation_model:
        models["orientation"] = Path(arguments.orientation_model)
    output = _new_manifest_path(Path(arguments.output))
    write_manifest(output, models)
    return {"status": "manifest-created", "manifest": str(output)}


def _parser() -> argparse.ArgumentParser:
    parser = WorkflowArgumentParser(description="Run local, offline-first image OCR.")
    commands = parser.add_subparsers(dest="command", required=True)

    manifest = commands.add_parser("manifest", help="create a local model hash manifest")
    manifest.add_argument("--det-model", required=True)
    manifest.add_argument("--rec-model", required=True)
    manifest.add_argument("--orientation-model")
    manifest.add_argument("--output", required=True)
    manifest.set_defaults(handler=_manifest)

    doctor = commands.add_parser("doctor", help="read-only local dependency check")
    _add_engine_options(doctor)
    doctor.set_defaults(handler=_doctor)

    run = commands.add_parser("run", help="run one explicitly selected local OCR engine")
    _add_engine_options(run)
    run.add_argument("--input", action="append", required=True)
    run.add_argument("--language")
    run.add_argument("--output")
    run.set_defaults(handler=run_selected_engine)
    return parser


def _json_print(payload: Mapping[str, Any]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        payload = arguments.handler(arguments)
        if arguments.command == "run":
            payload = {**payload, "text": deliver(payload, Path(arguments.output) if arguments.output else None)}
        _json_print(payload)
        return 0 if payload["status"] in {"ready", "manifest-created", "success", "partial-success"} else 2
    except WorkflowError as error:
        _json_print({"status": error.category, "message": error.message})
        return 2
    except FileExistsError as error:
        _json_print({"status": "invalid-input", "message": str(error)})
        return 2
    except OSError as error:
        _json_print({"status": "processing-failed", "message": str(error)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
