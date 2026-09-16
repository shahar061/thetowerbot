// Narrow macOS host bridge for the measured BlueStacks Air update dialog.
// The Python caller verifies the dialog twice before invoking `press`.

import ApplicationServices
import CoreGraphics
import Foundation

struct WindowInfo: Codable {
    let id: UInt32
    let title: String
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

func fail(_ message: String) -> Never {
    fputs("BlueStacks window unavailable: \(message)\n", stderr)
    exit(1)
}

func windowsForPID(_ pid: Int32) -> [WindowInfo] {
    guard let rows = CGWindowListCopyWindowInfo(
        [.optionAll, .excludeDesktopElements], kCGNullWindowID
    ) as? [[String: Any]] else { fail("window inventory") }
    let matches = rows.compactMap { row -> WindowInfo? in
        guard let owner = row[kCGWindowOwnerPID as String] as? NSNumber,
              owner.int32Value == pid,
              let layer = row[kCGWindowLayer as String] as? NSNumber,
              let title = row[kCGWindowName as String] as? String,
              title == "BlueStacks Air" || title == "Upgrade available",
              (title == "Upgrade available" || layer.intValue == 0),
              let alpha = row[kCGWindowAlpha as String] as? NSNumber,
              (title == "BlueStacks Air" || alpha.doubleValue > 0.01),
              let number = row[kCGWindowNumber as String] as? NSNumber,
              let bounds = row[kCGWindowBounds as String] as? [String: Any],
              let x = bounds["X"] as? NSNumber,
              let y = bounds["Y"] as? NSNumber,
              let width = bounds["Width"] as? NSNumber,
              let height = bounds["Height"] as? NSNumber,
              width.doubleValue > 0, height.doubleValue > 0
        else { return nil }
        return WindowInfo(id: number.uint32Value, title: title,
                          x: x.doubleValue, y: y.doubleValue,
                          width: width.doubleValue, height: height.doubleValue)
    }
    guard matches.filter({ $0.title == "BlueStacks Air" }).count == 1 else {
        fail("expected one exact BlueStacks Air parent window")
    }
    return matches.filter { $0.title == "Upgrade available" }
}

func attribute(_ element: AXUIElement, _ key: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, key as CFString, &value) == .success ? value : nil
}

func elementFrame(_ element: AXUIElement) -> CGRect? {
    guard let positionRef = attribute(element, kAXPositionAttribute),
          let sizeRef = attribute(element, kAXSizeAttribute),
          CFGetTypeID(positionRef) == AXValueGetTypeID(),
          CFGetTypeID(sizeRef) == AXValueGetTypeID() else { return nil }
    let position = positionRef as! AXValue
    let size = sizeRef as! AXValue
    var origin = CGPoint.zero
    var dimensions = CGSize.zero
    guard AXValueGetValue(position, .cgPoint, &origin),
          AXValueGetValue(size, .cgSize, &dimensions) else { return nil }
    return CGRect(origin: origin, size: dimensions)
}

func buttonsNear(_ element: AXUIElement, _ point: CGPoint, _ depth: Int) -> [AXUIElement] {
    if depth > 12 { return [] }
    var matches: [AXUIElement] = []
    if let role = attribute(element, kAXRoleAttribute) as? String,
       role == kAXButtonRole,
       let frame = elementFrame(element),
       frame.width >= 12, frame.width <= 80,
       frame.height >= 12, frame.height <= 80,
       abs(frame.midX - point.x) <= 12,
       abs(frame.midY - point.y) <= 12 {
        matches.append(element)
    }
    if let children = attribute(element, kAXChildrenAttribute) as? [AXUIElement] {
        for child in children {
            matches.append(contentsOf: buttonsNear(child, point, depth + 1))
        }
    }
    return matches
}

let args = CommandLine.arguments
guard args.count >= 3, let pid = Int32(args[2]), pid > 0 else {
    fail("usage: inspect PID | press PID WINDOW_ID X Y")
}
let popupWindows = windowsForPID(pid)
if args[1] == "count" && args.count == 3 {
    print(popupWindows.count)
    exit(0)
}
guard popupWindows.count == 1 else { fail("expected one exact upgrade window") }
let current = popupWindows[0]
if args[1] == "inspect" && args.count == 3 {
    let data = try JSONEncoder().encode(current)
    print(String(data: data, encoding: .utf8)!)
} else if args[1] == "press" && args.count == 6 {
    guard let expectedWindow = UInt32(args[3]), expectedWindow == current.id,
          let x = Double(args[4]), let y = Double(args[5]),
          x.isFinite, y.isFinite,
          x >= current.x, x <= current.x + current.width,
          y >= current.y, y <= current.y + current.height else {
        fail("window moved or target outside exact window")
    }
    guard AXIsProcessTrusted() else { fail("macOS Accessibility permission required") }
    let app = AXUIElementCreateApplication(pid)
    var windows = attribute(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
    if windows.isEmpty, let focused = attribute(app, kAXFocusedWindowAttribute) {
        windows = [focused as! AXUIElement]
    }
    let matchingWindows = windows.filter { window in
        guard attribute(window, kAXTitleAttribute) as? String == current.title,
              let frame = elementFrame(window) else { return false }
        return abs(frame.minX - current.x) <= 2 && abs(frame.minY - current.y) <= 2
            && abs(frame.width - current.width) <= 2
            && abs(frame.height - current.height) <= 2
    }
    guard matchingWindows.count == 1 else { fail("Accessibility window ambiguous") }
    let matches = buttonsNear(matchingWindows[0], CGPoint(x: x, y: y), 0)
    guard matches.count == 1 else { fail("close button missing or ambiguous") }
    guard AXUIElementPerformAction(matches[0], kAXPressAction as CFString) == .success else {
        fail("close button press failed")
    }
    print("pressed")
} else {
    fail("unsupported command")
}
