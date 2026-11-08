"""Run the actual Swift socket client against owned temporary Unix peers only."""

import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None, reason="Swift on macOS required"
)


def compile_probe(tmp_path, source):
    root = Path(__file__).resolve().parents[1]
    main = tmp_path / "main.swift"
    main.write_text(source)
    binary = tmp_path / "daemon-probe"
    subprocess.run(
        ["swiftc", str(root / "wav2sum/native/app/Daemon.swift"), str(main), "-o", str(binary)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return binary


def test_closed_peer_write_does_not_kill_app(tmp_path):
    binary = compile_probe(
        tmp_path,
        """
import Foundation
signal(SIGPIPE, SIG_DFL) // Ordinary app: no dev-watch process-wide signal override.
let client = DaemonClient()
var disconnected = false, failed = false
client.onDisconnect = { disconnected = true }
precondition(client.connect(path: CommandLine.arguments[1]))
FileHandle.standardOutput.write(Data("connected\\n".utf8))
_ = readLine() // The main queue cannot consume EOF cleanup before the synthetic request.
client.request("probe") { reply in failed = reply["ok"] as? Bool == false }
Thread.sleep(forTimeInterval: 0.2) // Let the real write queue hit the closed peer before EOF cleanup.
let deadline = Date().addingTimeInterval(3)
while !disconnected && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.01)) }
precondition(failed && disconnected && !client.isConnected)
print("safe disconnect")
""",
    )
    with (
        tempfile.TemporaryDirectory(prefix="w2s-socket-", dir="/tmp") as directory,
        socket.socket(socket.AF_UNIX) as server,
    ):
        path = str(Path(directory) / "peer.sock")
        server.bind(path)
        server.listen(1)
        server.settimeout(5)
        process = subprocess.Popen(
            [str(binary), path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            peer, _ = server.accept()
            with peer:
                assert process.stdout.readline() == b"connected\n"
                peer.shutdown(socket.SHUT_RDWR)
            stdout, stderr = process.communicate(b"write now\n", timeout=10)
            assert process.returncode == 0, (process.returncode, stderr.decode())
            assert stdout == b"safe disconnect\n"
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_socket_option_failure_closes_descriptor(tmp_path):
    binary = compile_probe(
        tmp_path,
        """
import Foundation
var configured: Int32 = -1
func setsockopt(_ fd: Int32, _ level: Int32, _ name: Int32, _ value: UnsafeRawPointer!, _ length: socklen_t) -> Int32 {
    precondition(level == SOL_SOCKET && name == SO_NOSIGPIPE)
    configured = fd
    errno = EINVAL
    return -1
}
let client = DaemonClient()
precondition(!client.connect(path: CommandLine.arguments[1]))
precondition(configured >= 0 && fcntl(configured, F_GETFD) == -1 && errno == EBADF)
precondition(!client.isConnected)
""",
    )
    with (
        tempfile.TemporaryDirectory(prefix="w2s-socket-", dir="/tmp") as directory,
        socket.socket(socket.AF_UNIX) as server,
    ):
        path = str(Path(directory) / "peer.sock")
        server.bind(path)
        server.listen(1)
        subprocess.run([str(binary), path], check=True, capture_output=True, timeout=10)
