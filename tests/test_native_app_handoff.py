"""Exercise the real app delegate with isolated fake OS and daemon dependencies."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("swiftc") is None, reason="Swift on macOS required")
def test_app_handoff(tmp_path):
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path / "app-handoff"
    subprocess.run(
        [
            "swiftc",
            "-D",
            "APP_HANDOFF_TESTING",
            str(root / "wav2sum/native/app/DevWatch.swift"),
            str(root / "wav2sum/native/app/main.swift"),
            str(root / "tests/native_app_handoff.swift"),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    subprocess.run([str(binary)], check=True, capture_output=True, timeout=10)


@pytest.mark.skipif(sys.platform != "darwin" or shutil.which("swiftc") is None, reason="Swift on macOS required")
def test_settings_apply_veto_precedes_helper(tmp_path):
    root = Path(__file__).resolve().parents[1]
    main = tmp_path / "main.swift"
    main.write_text(
        r"""
import AppKit
import Foundation

typealias JSON = [String: Any]
enum FocusedApp { static func ensureAccessibility(prompt: Bool) -> Bool { false } }
enum DaemonProcess {
    static var calls: [String] = []
    static var pending: ((JSON) -> Void)?
    static func setup(_ request: JSON, completion: @escaping (JSON) -> Void) {
        let action = request["action"] as! String
        calls.append(action)
        if action == "status" { completion(["ok": true, "settings": ["completed": true, "mode": "local"]]) }
        else { pending = completion }
    }
}
func descendants(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(descendants) }
_ = NSApplication.shared
let setup = SetupWindow(initialCloud: false, permissions: { "synthetic permissions" })
setup.refresh()
let views = NSApp.windows.flatMap { $0.contentView.map(descendants) ?? [] }
let apply = views.compactMap { $0 as? NSButton }.first { $0.title == "Apply Settings" }!
var allow = false
var transitions: [Bool] = []
setup.onApplying = { applying in transitions.append(applying); return !applying || allow }
apply.performClick(nil)
precondition(DaemonProcess.calls == ["status"], "veto must precede configure helper")
precondition(apply.isEnabled, "busy refusal remains retryable")
let messages = views.compactMap { ($0 as? NSTextField)?.stringValue }
precondition(messages.contains("Finish the current dictation or pending action before applying settings."))
allow = true
apply.performClick(nil)
precondition(DaemonProcess.calls == ["status", "configure"] && !apply.isEnabled)
DaemonProcess.pending?(["ok": false, "error": "synthetic failed ACK"])
precondition(transitions == [true, true, false] && apply.isEnabled)
print("native settings preflight tests passed")
"""
    )
    binary = tmp_path / "settings-preflight"
    subprocess.run(
        ["swiftc", str(root / "wav2sum/native/app/Setup.swift"), str(main), "-o", str(binary)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    subprocess.run([str(binary)], check=True, capture_output=True, timeout=10)
