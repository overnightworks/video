"""The built image: its contents, its user, and the service on a host with no card.

`VIDEO_IMAGE` names the image under test; `-m image` selects this module, which the
default run leaves out. Every container is started as the deployment starts one:
a read-only root, `/tmp` writable, no capability, no new privilege, no network.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from pydantic import BaseModel, ConfigDict, Field
from video_provider_contract.worker import CARD_MEMORY_MB_ENV

from support.identity_support import write_caller_config
from video_client.wire import (
    REFUSAL_STATUS,
    Health,
    Refusal,
    RefusalCode,
    viewers_path,
)
from video_service.config import Settings
from video_service.provider_process import ProcessFailure
from video_service.providers import ProviderName, worker_executable

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

pytestmark = pytest.mark.image

_REPOSITORY = Path(__file__).resolve().parents[1]
_PROVIDER_ROOT_ENV = "VIDEO_PROVIDER_ROOT"
_DEPLOYMENT_SETTINGS = (
    "VIDEO_PROVIDER",
    CARD_MEMORY_MB_ENV,
    "VIDEO_CONFIG_FILE",
    "VIDEO_HOST",
)
_RUN_AS_THE_DEPLOYMENT: Final = (
    "--read-only",
    "--tmpfs",
    "/tmp",  # noqa: S108 - the container's own memory-backed directory
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "--network",
    "none",
)
_CALLER_DIRECTORY = "/run/callers"
_PORT = Settings.model_fields["port"].default
_START_WITHIN_SECONDS = 240
_STOP_WITHIN_SECONDS = 60
_POLL_SECONDS = 1.0
_COMMAND_SECONDS = 900
_HASH_BASED = 0b1

# Run by an interpreter of the image; each prints one JSON document.
_TOOLS = """
import importlib.util, json, pathlib, shutil, sys
worker = pathlib.Path(sys.prefix) / "bin" / "video-fake-worker"
print(json.dumps({
    "tools": {name: shutil.which(name) for name in ("uv", "git", "gcc")},
    "forbidden": {
        **{name: importlib.util.find_spec(name) is not None
           for name in ("hatchling", "video_provider_fake")},
        "video-fake-worker": worker.exists(),
    },
}))
"""
_POINTERS = """
import json, pathlib, site, sys
prefix = pathlib.Path(sys.prefix)
site_packages = pathlib.Path(site.getsitepackages()[0])
own = f"#!{prefix}/".encode()
path_files = site_packages.glob("*.pth")
records = site_packages.glob("*.dist-info/direct_url.json")
scripts = [f for f in (prefix / "bin").iterdir() if f.is_file()]
print(json.dumps({
    "path_files": [f.name for f in path_files if "/build" in f.read_text()],
    "editable": [f.parent.name for f in records
                 if json.loads(f.read_text()).get("dir_info", {}).get("editable")],
    "foreign_interpreters": [f.name for f in scripts
                             if f.read_bytes().startswith(b"#!")
                             and not f.read_bytes().startswith(own)],
}))
"""
_BYTECODE_FLAGS = """
import importlib.util, json, sys
path = importlib.util.cache_from_source(importlib.util.find_spec(sys.argv[1]).origin)
print(json.dumps({"flags": int.from_bytes(open(path, "rb").read(8)[4:8], "little")}))
"""
_LICENCES = """
import hashlib, importlib.metadata, json, sys
distribution = importlib.metadata.distribution(sys.argv[1])
print(json.dumps({"hashes": sorted(
    hashlib.sha256(distribution.locate_file(file).read_bytes()).hexdigest()
    for file in distribution.files if "licenses" in file.parts
)}))
"""
_WEIGHTS = """
import hashlib, json, os
from video_provider_flashhead_lite.upstream import locate_weights
weights = locate_weights()
files, others = {}, []
for directory, names, leaves in os.walk(weights.root):
    for name in names + leaves:
        path = os.path.join(directory, name)
        relative = os.path.relpath(path, weights.root)
        if os.path.islink(path):
            others.append(relative)
        elif os.path.isfile(path):
            digest = hashlib.sha256()
            with open(path, "rb") as source:
                while chunk := source.read(1 << 20):
                    digest.update(chunk)
            files[relative] = digest.hexdigest()
        elif not os.path.isdir(path):
            others.append(relative)
print(json.dumps({
    "files": files,
    "others": others,
    "directories": sorted(
        os.path.relpath(os.path.join(d, n), weights.root)
        for d, names, _ in os.walk(weights.root) for n in names
    ),
    "staging_exists": os.path.lexists(weights.staging),
}))
"""
_USER = """
import json, os, pwd
entry = pwd.getpwuid(os.getuid())
print(json.dumps({"uid": os.getuid(), "name": entry.pw_name,
                  "home_exists": os.path.exists(entry.pw_dir)}))
"""
_IMPORT_UPSTREAM = """
from video_provider_flashhead_lite.upstream import import_inference
import_inference()
"""
_HEALTH = f"""
from video_client.health import fetch_health
outcome = fetch_health("http://127.0.0.1:{_PORT}", deadline_seconds=5, cap_bytes=65536)
print(outcome.model_dump_json())
"""
_REQUEST = f"""
import http.client, json, sys
connection = http.client.HTTPConnection("127.0.0.1", {_PORT}, timeout=5)
connection.request(sys.argv[1], sys.argv[2])
answer = connection.getresponse()
print(json.dumps({{"status": answer.status, "body": answer.read().decode()}}))
"""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class _ImageConfiguration(_Model):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    user: str = Field(alias="User")
    environment: list[str] = Field(alias="Env")
    healthcheck: object | None = Field(default=None, alias="Healthcheck")


class _State(_Model):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    status: str = Field(alias="Status")
    exit_code: int = Field(alias="ExitCode")


class _User(_Model):
    uid: int
    name: str
    home_exists: bool


class _Tools(_Model):
    tools: dict[str, str | None]
    forbidden: dict[str, bool]


class _Pointers(_Model):
    path_files: list[str]
    editable: list[str]
    foreign_interpreters: list[str]


class _Flags(_Model):
    flags: int


class _Licences(_Model):
    hashes: list[str]


class _Weights(_Model):
    files: dict[str, str]
    others: list[str]
    directories: list[str]
    staging_exists: bool


class _Answer(_Model):
    status: int
    body: str


def _docker(*arguments: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - docker, with the test's own arguments
        ["docker", *arguments],  # noqa: S607 - the runner's docker
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _events(logs: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in logs.splitlines() if line.startswith("{")]


@pytest.fixture(scope="module")
def image() -> str:
    """The image under test, which `VIDEO_IMAGE` names."""
    named = os.environ.get("VIDEO_IMAGE")
    if not named:
        pytest.fail(
            "VIDEO_IMAGE must name the image under test: build or pull it first"
        )
    return named


@pytest.fixture(scope="module")
def configuration(image: str) -> _ImageConfiguration:
    """What the image declares: its user, its environment, its health check."""
    inspected = _docker("image", "inspect", image)
    assert inspected.returncode == 0, inspected.stderr
    return _ImageConfiguration.model_validate(json.loads(inspected.stdout)[0]["Config"])


@pytest.fixture(scope="module")
def provider_python(configuration: _ImageConfiguration) -> str:
    """The provider's own interpreter, beside the worker the service looks for."""
    environment = dict(entry.split("=", 1) for entry in configuration.environment)
    worker = worker_executable(
        ProviderName.FLASHHEAD_LITE, Path(environment[_PROVIDER_ROOT_ENV])
    )
    return str(worker.parent / "python")


@pytest.fixture(scope="module")
def interpreters(provider_python: str) -> dict[str, str]:
    """The image's interpreters by the environment they belong to."""
    return {"service": "python", "provider": provider_python}


def _run(
    image: str, *command: str, variables: Sequence[str] = ()
) -> subprocess.CompletedProcess[str]:
    environment = [part for variable in variables for part in ("--env", variable)]
    return _docker(
        "run",
        "--rm",
        *_RUN_AS_THE_DEPLOYMENT,
        *environment,
        image,
        *command,
        timeout=_COMMAND_SECONDS,
    )


def _probe[Found: BaseModel](
    image: str, interpreter: str, script: str, found: type[Found], *arguments: str
) -> Found:
    ran = _run(image, interpreter, "-c", script, *arguments)
    assert ran.returncode == 0, ran.stderr
    return found.model_validate_json(ran.stdout)


@pytest.fixture(scope="module")
def caller_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A valid caller file in a directory the image's user reads."""
    directory = tmp_path_factory.mktemp("callers")
    directory.chmod(0o755)
    file = write_caller_config(directory)
    file.chmod(0o644)
    return file


def _logs(name: str) -> str:
    logged = _docker("logs", name)
    return logged.stdout + logged.stderr


def _wait_for_event(name: str, event: str) -> None:
    deadline = time.monotonic() + _START_WITHIN_SECONDS
    while time.monotonic() < deadline:
        if any(record["event"] == event for record in _events(_logs(name))):
            return
        time.sleep(_POLL_SECONDS)
    pytest.fail(f"the service wrote no {event} line:\n{_logs(name)}")


@contextmanager
def _started(image: str, caller_file: Path) -> Iterator[str]:
    """A container of the service with `flashhead-lite`, once its provider refused."""
    name = f"video-image-test-{uuid.uuid4().hex[:12]}"
    variables = [
        "VIDEO_PROVIDER=flashhead-lite",
        f"{CARD_MEMORY_MB_ENV}=1024",
        f"VIDEO_CONFIG_FILE={_CALLER_DIRECTORY}/{caller_file.name}",
    ]
    started = _docker(
        "run",
        "--detach",
        "--name",
        name,
        *_RUN_AS_THE_DEPLOYMENT,
        "--volume",
        f"{caller_file.parent}:{_CALLER_DIRECTORY}:ro",
        *(part for variable in variables for part in ("--env", variable)),
        image,
    )
    assert started.returncode == 0, started.stderr
    try:
        _wait_for_event(name, "provider.failed")
        yield name
    finally:
        _docker("rm", "--force", name)


@pytest.fixture(scope="module")
def service(image: str, caller_file: Path) -> Iterator[str]:
    """A running service whose provider refused for want of a card."""
    with _started(image, caller_file) as name:
        yield name


def _answer(service: str, method: str, path: str) -> _Answer:
    ran = _docker("exec", service, "python", "-c", _REQUEST, method, path)
    assert ran.returncode == 0, ran.stderr
    return _Answer.model_validate_json(ran.stdout)


def test_the_image_declares_a_numeric_non_root_user_and_no_deployment_setting(
    configuration: _ImageConfiguration,
) -> None:
    names = {entry.split("=", 1)[0] for entry in configuration.environment}

    assert configuration.user.isdecimal()
    assert int(configuration.user) != 0
    assert not any("TOKEN" in name for name in names)
    assert names.isdisjoint(_DEPLOYMENT_SETTINGS)
    assert not configuration.healthcheck


def test_the_user_has_a_passwd_entry_and_no_home_directory(image: str) -> None:
    user = _probe(image, "python", _USER, _User)

    assert user.uid != 0
    assert user.name
    assert not user.home_exists


@pytest.mark.parametrize("environment", ["service", "provider"])
def test_an_environment_holds_no_build_tool_and_no_fake_provider(
    image: str, interpreters: dict[str, str], environment: str
) -> None:
    found = _probe(image, interpreters[environment], _TOOLS, _Tools)

    assert not any(found.forbidden.values())
    assert found.tools["uv"] is None
    assert found.tools["git"] is None


def test_the_image_holds_the_compiler_triton_needs(image: str) -> None:
    assert _probe(image, "python", _TOOLS, _Tools).tools["gcc"]


@pytest.mark.parametrize("environment", ["service", "provider"])
def test_an_environment_points_at_no_build_stage(
    image: str, interpreters: dict[str, str], environment: str
) -> None:
    found = _probe(image, interpreters[environment], _POINTERS, _Pointers)

    assert found == _Pointers(path_files=[], editable=[], foreign_interpreters=[])


@pytest.mark.parametrize(
    ("environment", "module"),
    [("service", "video_service"), ("provider", "video_provider_flashhead_lite")],
)
def test_the_bytecode_of_a_package_is_hash_based(
    image: str, interpreters: dict[str, str], environment: str, module: str
) -> None:
    found = _probe(image, interpreters[environment], _BYTECODE_FLAGS, _Flags, module)

    assert found.flags & _HASH_BASED


@pytest.mark.parametrize(
    ("environment", "distribution", "licences"),
    [
        ("service", "overnightworks-video-service", ["LICENSE"]),
        (
            "provider",
            "overnightworks-video-provider-flashhead-lite",
            [
                "providers/flashhead-lite/LICENSE",
                "providers/flashhead-lite/soulx-flashhead/LICENSE",
            ],
        ),
    ],
)
def test_a_wheel_carries_the_licence_texts_of_the_repository(
    image: str,
    interpreters: dict[str, str],
    environment: str,
    distribution: str,
    licences: list[str],
) -> None:
    carried = _probe(
        image, interpreters[environment], _LICENCES, _Licences, distribution
    )

    expected = {
        hashlib.sha256((_REPOSITORY / path).read_bytes()).hexdigest()
        for path in licences
    }
    assert expected <= set(carried.hashes)


def test_the_weights_directory_holds_exactly_the_pinned_files_the_user_reads(
    image: str, provider_python: str
) -> None:
    from video_provider_flashhead_lite.upstream import (  # noqa: PLC0415 - the pins of the provider under test
        PINNED_FILES,
    )

    found = _probe(image, provider_python, _WEIGHTS, _Weights)

    assert found.files == {str(pin.destination): pin.sha256 for pin in PINNED_FILES}
    assert set(found.directories) == {
        str(parent)
        for pin in PINNED_FILES
        for parent in pin.destination.parents
        if parent != Path()
    }
    assert found.others == []
    assert not found.staging_exists


def test_the_provider_imports_upstream_without_a_card(
    image: str, provider_python: str
) -> None:
    imported = _run(image, provider_python, "-c", _IMPORT_UPSTREAM)

    assert imported.returncode == 0, imported.stderr


def test_without_the_card_memory_cap_the_start_stops_and_names_the_setting(
    image: str,
) -> None:
    ran = _run(image, variables=["VIDEO_PROVIDER=flashhead-lite"])

    assert ran.returncode != 0
    assert CARD_MEMORY_MB_ENV in ran.stderr


def test_without_a_card_health_names_the_provider_not_ready(service: str) -> None:
    ran = _docker("exec", service, "python", "-c", _HEALTH)
    assert ran.returncode == 0, ran.stderr
    health = Health.model_validate_json(ran.stdout)

    assert not health.ready
    assert health.provider.name == ProviderName.FLASHHEAD_LITE
    assert not health.provider.ready


def test_without_a_card_the_log_carries_the_refusal_as_reported_and_none_of_its_text(
    service: str,
) -> None:
    logs = _logs(service)
    records = _events(logs)
    (failed,) = (record for record in records if record["event"] == "provider.failed")
    (started,) = (record for record in records if record["event"] == "process.started")

    assert failed["reason"] == ProcessFailure.REPORTED.value
    assert failed["phase"] == "before_ready"
    assert set(failed) - set(started) == {"reason", "phase", "exit_status"}
    assert "Traceback" not in logs


def test_a_request_without_tokens_is_refused_as_the_wire_says(service: str) -> None:
    answer = _answer(service, "POST", viewers_path("none"))

    refusal = Refusal.model_validate_json(answer.body)
    assert refusal.error.code is RefusalCode.UNAUTHORIZED
    assert answer.status == REFUSAL_STATUS[RefusalCode.UNAUTHORIZED]


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_the_service_offers_no_documentation_path(service: str, path: str) -> None:
    answer = _answer(service, "GET", path)

    refusal = Refusal.model_validate_json(answer.body)
    assert answer.status == HTTPStatus.NOT_FOUND
    assert refusal.error.code is RefusalCode.NOT_FOUND


def test_a_stop_signal_drains_and_the_service_exits_zero(
    image: str, caller_file: Path
) -> None:
    with _started(image, caller_file) as name:
        stopped = _docker("stop", "--timeout", str(_STOP_WITHIN_SECONDS), name)
        inspected = _docker("inspect", name)
        records = _events(_logs(name))

    state = _State.model_validate(json.loads(inspected.stdout)[0]["State"])
    assert stopped.returncode == 0, stopped.stderr
    assert (state.status, state.exit_code) == ("exited", 0)
    (ended,) = (record for record in records if record["event"] == "process.stopped")
    assert ended["exit_code"] == 0
