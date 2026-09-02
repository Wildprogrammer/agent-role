from __future__ import annotations

import asyncio
import json
import socket
import struct
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import MappingProxyType

import asyncssh
import pytest

from agent_workflow_hub.ssh_operations.commands import CommandExecutor
from agent_workflow_hub.ssh_operations.connections import ConnectionManager
from agent_workflow_hub.ssh_operations.forwarding import ForwardService
from agent_workflow_hub.ssh_operations.host_keys import HostKeyMismatch
from agent_workflow_hub.ssh_operations.models import SSHConfig, StepSpec, TargetConfig
from agent_workflow_hub.ssh_operations.service import SSHOperationsService
from agent_workflow_hub.ssh_operations.shells import PowerShell
from agent_workflow_hub.ssh_operations.transfers import SCPService, SFTPService


class _Server(asyncssh.SSHServer):
    def __init__(self, public_key: asyncssh.SSHKey) -> None:
        self.public_key = public_key

    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return username == "tester" and password == "test-password"

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        return (
            username == "tester"
            and key.export_public_key("openssh")
            == self.public_key.export_public_key("openssh")
        )

    def connection_requested(self, *args):
        return True

    def server_requested(self, *args):
        return True


async def _process(process: asyncssh.SSHServerProcess) -> None:
    async def execute(command: str) -> tuple[bytes, bytes, int]:
        child = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await child.communicate()
        return stdout, stderr, child.returncode

    if process.command:
        stdout, stderr, status = await execute(process.command)
        process.stdout.write(stdout.decode("utf-8", errors="replace"))
        process.stderr.write(stderr.decode("utf-8", errors="replace"))
        process.exit(status)
        return

    while True:
        line = await process.stdin.readline()
        if not line or line.strip().casefold() == "exit":
            process.exit(0)
            return
        command = line
        while "_ERR_END__')" not in command:
            continuation = await process.stdin.readline()
            if not continuation:
                process.exit(1)
                return
            command += continuation
        stdout, stderr, _ = await execute(command)
        process.stdout.write(stdout.decode("utf-8", errors="replace"))
        process.stderr.write(stderr.decode("utf-8", errors="replace"))


@asynccontextmanager
async def _server(tmp_path: Path, host_key, public_key, *, port: int = 0):
    sftp_root = tmp_path / "sftp"
    sftp_root.mkdir(exist_ok=True)
    listener = await asyncssh.listen(
        "127.0.0.1",
        port,
        server_factory=lambda: _Server(public_key),
        server_host_keys=[host_key],
        process_factory=_process,
        allow_scp=True,
        sftp_factory=lambda channel: asyncssh.SFTPServer(
            channel, chroot=str(sftp_root).encode()
        ),
    )
    try:
        yield listener, listener.get_port(), sftp_root
    finally:
        listener.close()
        await listener.wait_closed()


def _write_private_key(path: Path, key: asyncssh.SSHKey) -> None:
    path.write_bytes(key.export_private_key("openssh"))


def _config(tmp_path: Path, port: int, private_key: Path | None = None) -> SSHConfig:
    target = TargetConfig(
        "local", "127.0.0.1", port=port, username="tester",
        auth="key" if private_key else "password",
        password=None if private_key else "test-password",
        sudo_password=None,
        private_key=private_key,
        remote_os="windows", shell="powershell", timeout_seconds=5,
    )
    return SSHConfig(
        tmp_path / "ssh.ini", tmp_path / "known_hosts",
        MappingProxyType({"local": target}),
    )


@pytest.mark.anyio
async def test_real_password_key_tofu_command_steps_and_sftp(tmp_path: Path) -> None:
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    private_key = tmp_path / "id_ed25519"
    _write_private_key(private_key, client_key)

    async with _server(tmp_path, host_key, client_key) as (_, port, sftp_root):
        password_manager = ConnectionManager(_config(tmp_path, port), retries=0)
        async with password_manager.connect("local") as connection:
            command = await CommandExecutor(connection, PowerShell()).exec(
                "Write-Output integration", timeout=5
            )
            assert command.status == "success"
            assert command.stdout.strip() == "integration"
            steps = await CommandExecutor(connection, PowerShell()).run_steps(
                (
                    StepSpec("one", "Write-Output first", timeout_seconds=5),
                    StepSpec(
                        "two", "Write-Output ${steps.one.stdout}",
                        depends_on=("one",), timeout_seconds=5,
                    ),
                )
            )
            assert [item.status for item in steps] == ["success", "success"], steps
            assert steps[0].stdout.strip() == "first"
            assert steps[1].stdout.strip() == "first"
            sftp = SFTPService(connection)
            await sftp.write("/item.txt", b"payload")
            assert await sftp.read("/item.txt") == b"payload"
            payload = b"0123456789" * 1000
            local_upload = tmp_path / "upload.bin"
            local_upload.write_bytes(payload)
            await sftp.write(
                "/upload.bin.agent-workflow-hub-resume.part", payload[:137]
            )
            uploaded = await sftp.upload(
                local_upload, "/upload.bin", request_id="resume", resume=True
            )
            assert uploaded.status == "success"
            assert await sftp.read("/upload.bin") == payload
            local_download = tmp_path / "download.bin"
            local_partial = tmp_path / "download.bin.agent-workflow-hub-resume.part"
            local_partial.write_bytes(payload[:211])
            downloaded = await sftp.download(
                "/upload.bin", local_download, request_id="resume", resume=True
            )
            assert downloaded.status == "success"
            assert local_download.read_bytes() == payload
            await sftp.close()
            assert (sftp_root / "item.txt").read_bytes() == b"payload"

        key_manager = ConnectionManager(_config(tmp_path, port, private_key), retries=0)
        async with key_manager.connect("local"):
            pass
        assert key_manager.close_order == ("local",)


@pytest.mark.anyio
async def test_tofu_store_does_not_fall_back_to_user_known_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")

    async with _server(tmp_path, host_key, client_key) as (_, port, _):
        fake_home = tmp_path / "home"
        global_known_hosts = fake_home / ".ssh" / "known_hosts"
        global_known_hosts.parent.mkdir(parents=True)
        public_key = host_key.export_public_key("openssh").decode("ascii").strip()
        global_known_hosts.write_text(
            f"[127.0.0.1]:{port} {public_key}\n", encoding="utf-8"
        )
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("USERPROFILE", str(fake_home))

        config = _config(tmp_path, port)
        manager = ConnectionManager(config, retries=0)
        async with manager.connect("local"):
            pass

        assert config.known_hosts.is_file()
        assert f"[127.0.0.1]:{port}" in config.known_hosts.read_text(
            encoding="utf-8"
        )


@pytest.mark.anyio
async def test_real_tofu_rejects_changed_host_key(tmp_path: Path) -> None:
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    first = asyncssh.generate_private_key("ssh-ed25519")
    second = asyncssh.generate_private_key("ssh-ed25519")
    async with _server(tmp_path, first, client_key) as (_, port, _):
        manager = ConnectionManager(_config(tmp_path, port), retries=0)
        async with manager.connect("local"):
            pass
    async with _server(tmp_path, second, client_key, port=port):
        manager = ConnectionManager(_config(tmp_path, port), retries=0)
        with pytest.raises(HostKeyMismatch):
            async with manager.connect("local"):
                pass


@pytest.mark.anyio
async def test_real_scp_and_all_forwarding_modes(tmp_path: Path) -> None:
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(4096):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    echo_server = await asyncio.start_server(echo, "127.0.0.1", 0)
    echo_port = echo_server.sockets[0].getsockname()[1]
    try:
        async with _server(tmp_path, host_key, client_key) as (_, port, sftp_root):
            manager = ConnectionManager(_config(tmp_path, port), retries=0)
            async with manager.connect("local") as connection:
                local_file = tmp_path / "local.txt"
                local_file.write_text("scp-payload", encoding="utf-8")
                scp = SCPService(connection)
                uploaded = await scp.upload([local_file], "/scp.txt", preserve=True)
                assert uploaded.status == "success"
                assert uploaded.resume_supported is False
                assert (sftp_root / "scp.txt").read_text(encoding="utf-8") == "scp-payload"
                download = tmp_path / "download.txt"
                downloaded = await scp.download(["/scp.txt"], download)
                assert downloaded.status == "success"
                assert download.read_text(encoding="utf-8") == "scp-payload"

                forwarding = ForwardService(connection)
                local = await forwarding.local(
                    "127.0.0.1", 0, "127.0.0.1", echo_port
                )
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", local.ready["listen_port"]
                )
                writer.write(b"local")
                await writer.drain()
                assert await reader.readexactly(5) == b"local"
                writer.close()
                await writer.wait_closed()
                await local.close()

                remote = await forwarding.remote(
                    "127.0.0.1", 0, "127.0.0.1", echo_port
                )
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", remote.ready["listen_port"]
                )
                writer.write(b"remote")
                await writer.drain()
                assert await reader.readexactly(6) == b"remote"
                writer.close()
                await writer.wait_closed()
                await remote.close()

                socks = await forwarding.socks("127.0.0.1", 0)
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", socks.ready["listen_port"]
                )
                writer.write(b"\x05\x01\x00")
                await writer.drain()
                assert await reader.readexactly(2) == b"\x05\x00"
                writer.write(
                    b"\x05\x01\x00\x01"
                    + socket.inet_aton("127.0.0.1")
                    + struct.pack("!H", echo_port)
                )
                await writer.drain()
                response = await reader.readexactly(10)
                assert response[:2] == b"\x05\x00"
                writer.write(b"socks")
                await writer.drain()
                assert await reader.readexactly(5) == b"socks"
                writer.close()
                await writer.wait_closed()
                await socks.close()
    finally:
        echo_server.close()
        await echo_server.wait_closed()


@pytest.mark.anyio
async def test_real_auto_detection_and_jump_chain(tmp_path: Path) -> None:
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    leaf_key = asyncssh.generate_private_key("ssh-ed25519")
    bastion_key = asyncssh.generate_private_key("ssh-ed25519")
    async with _server(tmp_path, leaf_key, client_key) as (_, leaf_port, _):
        async with _server(tmp_path, bastion_key, client_key) as (_, bastion_port, _):
            targets = MappingProxyType(
                {
                    "bastion": TargetConfig(
                        "bastion", "127.0.0.1", port=bastion_port,
                        username="tester", auth="password", password="test-password",
                        remote_os="windows", shell="powershell", timeout_seconds=5,
                    ),
                    "leaf": TargetConfig(
                        "leaf", "127.0.0.1", port=leaf_port,
                        username="tester", auth="password", password="test-password",
                        via="bastion", remote_os="auto", shell="auto", timeout_seconds=5,
                    ),
                }
            )
            config = SSHConfig(
                tmp_path / "ssh.ini", tmp_path / "known_hosts", targets
            )
            manager = ConnectionManager(config, retries=0)
            service = SSHOperationsService(config, manager)
            result = await service.exec_many(
                ("leaf",), "Write-Output detected", max_parallel=1
            )
            assert result.status == "success"
            assert result.results[0].stdout.strip() == "detected"
            assert manager.close_order == ("leaf", "bastion")


@pytest.mark.anyio
async def test_cli_end_to_end_uses_private_runtime_without_real_device(tmp_path: Path) -> None:
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    async with _server(tmp_path, host_key, client_key) as (_, port, _):
        config = tmp_path / "ssh.ini"
        config.write_text(
            "\n".join(
                (
                    "[ssh]",
                    "known_hosts = known_hosts",
                    "[target:local]",
                    "host = 127.0.0.1",
                    f"port = {port}",
                    "username = tester",
                    "auth = password",
                    "password = test-password",
                    "remote_os = windows",
                    "shell = powershell",
                    "timeout_seconds = 5",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        script = Path(__file__).resolve().parents[1] / "scripts" / "ssh_operations.py"
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            "exec",
            "--config",
            str(config.resolve()),
            "--target",
            "local",
            "--command",
            "Write-Output cli-e2e",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=10)
        result = json.loads(stdout.decode("utf-8"))
        assert child.returncode == 0, stderr.decode("utf-8", errors="replace")
        assert result["status"] == "success"
        assert result["results"][0]["stdout"].strip() == "cli-e2e"
        assert "test-password" not in stdout.decode("utf-8")
