import AppKit

enum Logo {
    static func draw(in rect: NSRect, color: NSColor) {
        color.setFill()
        let unit = min(rect.width, rect.height)
        let origin = NSPoint(x: rect.midX - unit / 2, y: rect.midY - unit / 2)
        let thickness: CGFloat = 0.08

        func capsule(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat) {
            let r = NSRect(x: origin.x + x * unit, y: origin.y + y * unit, width: w * unit, height: h * unit)
            let radius = min(r.width, r.height) / 2
            NSBezierPath(roundedRect: r, xRadius: radius, yRadius: radius).fill()
        }

        for (x, height) in [(0.06, 0.30), (0.19, 0.66), (0.32, 0.46), (0.45, 0.22)] {
            capsule(x, 0.5 - height / 2, thickness, height)
        }
        for (y, width) in [(0.64, 0.34), (0.46, 0.26), (0.28, 0.31)] {
            capsule(0.62, y, width, thickness)
        }
    }

    static var menuBarImage: NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { rect in
            draw(in: rect.insetBy(dx: 0, dy: 1), color: .black)
            return true
        }
        image.isTemplate = true
        return image
    }

    static func drawAppIcon(in rect: NSRect) {
        let side = rect.width
        let tile = rect.insetBy(dx: side * 0.1, dy: side * 0.1)
        let squircle = NSBezierPath(roundedRect: tile, xRadius: side * 0.18, yRadius: side * 0.18)

        NSGraphicsContext.saveGraphicsState()
        let shadow = NSShadow()
        shadow.shadowBlurRadius = side * 0.025
        shadow.shadowOffset = NSSize(width: 0, height: -side * 0.01)
        shadow.shadowColor = NSColor.black.withAlphaComponent(0.35)
        shadow.set()
        NSColor.black.setFill()
        squircle.fill()
        NSGraphicsContext.restoreGraphicsState()

        let gradient = NSGradient(colors: [
            NSColor(srgbRed: 0.33, green: 0.24, blue: 0.96, alpha: 1),
            NSColor(srgbRed: 0.76, green: 0.25, blue: 0.93, alpha: 1),
        ])!
        gradient.draw(in: squircle, angle: -45)

        draw(in: tile.insetBy(dx: tile.width * 0.2, dy: tile.height * 0.2), color: .white)
    }
}
