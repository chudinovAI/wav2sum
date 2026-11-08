import AppKit

@main
struct HUDTest {
    static func main() {
        _ = NSApplication.shared
        guard let screen = NSScreen.main ?? NSScreen.screens.first else {
            fatalError("No screen available for HUD test")
        }
        let hud = HUD()
        hud.cloud = true
        hud.show(.listening(command: false, translate: false))
        guard let panel = NSApp.windows.first(where: { $0.title == "Dictation preview" }) else {
            fatalError("HUD panel not found")
        }
        let frame = screen.visibleFrame
        precondition(panel.isVisible)
        precondition(panel.frame.size == NSSize(width: 190, height: 40), "Cloud panel must be 190×40")
        precondition(abs(panel.frame.midX - frame.midX) < 1, "Cloud panel must be centered")
        precondition(abs(panel.frame.minY - (frame.minY + 60)) < 1, "Cloud panel must be 60pt from bottom")
        precondition(panel.ignoresMouseEvents, "Cloud panel must not intercept mouse events")
        hud.show(.thinking)
        precondition(panel.isVisible && panel.frame.width == 190)
        hud.hide()
        precondition(!panel.isVisible)

        hud.cloud = false
        hud.show(.listening(command: false, translate: false))
        precondition(panel.isVisible && panel.frame.size == NSSize(width: 320, height: 80))
        precondition(!panel.ignoresMouseEvents, "Local preview must remain draggable")
        hud.setPreview("Local preview")
        hud.show(.thinking)
        let localFrame = panel.frame
        hud.hide()
        hud.cloud = true
        hud.show(.listening(command: false, translate: false))
        hud.hide()
        hud.cloud = false
        hud.show(.listening(command: false, translate: false))
        precondition(panel.frame == localFrame, "Cloud must not change local panel position")
        hud.show(.success)
        precondition(panel.isVisible)
        hud.hide()
    }
}
