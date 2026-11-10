"""Exercise the real app delegate with isolated fake OS and daemon dependencies."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def app_handoff_binary(tmp_path_factory):
    if sys.platform != "darwin" or shutil.which("swiftc") is None:
        pytest.skip("Swift on macOS required")
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path_factory.mktemp("app-handoff") / "app-handoff"
    subprocess.run(
        [
            "swiftc",
            "-D",
            "APP_HANDOFF_TESTING",
            str(root / "wav2sum/native/app/DevWatch.swift"),
            str(root / "wav2sum/native/app/MainWindow.swift"),
            str(root / "wav2sum/native/app/HistoryView.swift"),
            str(root / "wav2sum/native/app/main.swift"),
            str(root / "tests/native_app_handoff.swift"),
            str(root / "tests/native_main_window.swift"),
            str(root / "tests/native_history_view.swift"),
            str(root / "tests/native_history_flow.swift"),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return binary


def test_app_handoff(app_handoff_binary):
    subprocess.run([str(app_handoff_binary)], check=True, capture_output=True, timeout=10)


@pytest.mark.parametrize("scenario", ["--main-window", "--history-view", "--history-flow"])
def test_main_window(app_handoff_binary, scenario):
    result = subprocess.run(
        [str(app_handoff_binary), scenario], check=False, capture_output=True, text=True, timeout=15
    )
    if result.returncode == 77:
        pytest.skip(result.stdout.strip())
    result.check_returncode()


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
let window = NSApp.windows.first {
    $0.contentView.map(descendants)?.contains { $0.identifier?.rawValue == "settings.apply" } == true
}!
window.setContentSize(window.contentMinSize)
let content = window.contentView!
content.layoutSubtreeIfNeeded()
func find<T: NSView>(_ id: String, _ type: T.Type) -> T {
    descendants(content).compactMap { $0 as? T }.first { $0.identifier?.rawValue == id }!
}
let apply = find("settings.apply", NSButton.self)
let processing = find("settings.processing", NSStackView.self)
precondition(!processing.visibleRect.isEmpty, "settings must open at processing mode, not the bottom of the form")
precondition(content.bounds.contains(apply.convert(apply.bounds, to: content)),
             "Apply must remain visible at minimum size")
let local = find("settings.localModels", NSStackView.self)
let cloud = find("settings.cloudCredentials", NSStackView.self)
let mode = find("settings.mode", NSPopUpButton.self)
let consent = find("settings.cloudConsent", NSButton.self)
let automatic = find("settings.autoProcess", NSButton.self)
let automaticConsent = find("settings.autoConsent", NSButton.self)
mode.selectItem(at: 1)
_ = mode.sendAction(mode.action!, to: mode.target)
precondition(local.isHidden && !cloud.isHidden && consent.state == .off)
consent.state = .on
automatic.performClick(nil)
precondition(!automaticConsent.isHidden && automaticConsent.state == .off)
automaticConsent.state = .on
mode.selectItem(at: 0)
_ = mode.sendAction(mode.action!, to: mode.target)
precondition(!local.isHidden && cloud.isHidden && consent.state == .off && automaticConsent.state == .off)
precondition(DaemonProcess.calls == ["status"], "mode and consent edits must not trigger helper mutations")
var allow = false
var transitions: [Bool] = []
setup.onApplying = { applying in transitions.append(applying); return !applying || allow }
apply.performClick(nil)
precondition(DaemonProcess.calls == ["status"], "veto must precede configure helper")
precondition(apply.isEnabled, "busy refusal remains retryable")
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
