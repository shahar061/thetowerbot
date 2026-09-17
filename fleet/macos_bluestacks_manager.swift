// Narrow macOS bridge for the one visible BlueStacks Air Manager window.
// It supports only inspect, read-only capture, and one Accessibility button press.

import ApplicationServices
import CoreGraphics
import Darwin
import Foundation

let managerTitle = "BlueStacks Air Manager"
let playerExecutable = "/Applications/BlueStacks.app/Contents/MacOS/BlueStacks"
let managerCompanionExecutable = "/Applications/BlueStacks Air multi-instance manager.app/Contents/MacOS/BlueStacks Air multi-instance manager"
let blueStacksExecutables: Set<String> = [
    playerExecutable,
    managerCompanionExecutable,
]
let allowedControlLabels: Set<String> = ["Start", "Stop", "Instance", "Clone instance", "Create"]

struct WindowInfo: Encodable {
    let id: UInt32
    let title: String
    let ownerPath: String
    let x: Double
    let y: Double
    let width: Double
    let height: Double
    let pid: Int32

    enum CodingKeys: String, CodingKey {
        case id, title, ownerPath = "owner_path", x, y, width, height, pid
    }
}

struct ModalInfo: Encodable {
    let id: UInt32
    let title: String
    let parentID: UInt32
    let pid: Int32
    let x: Double
    let y: Double
    let width: Double
    let height: Double

    enum CodingKeys: String, CodingKey {
        case id, title, parentID = "parent_id", pid, x, y, width, height
    }
}

func fail(_ message: String) -> Never {
    fputs("BlueStacks manager unavailable: \(message)\n", stderr)
    exit(1)
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
    var position = CGPoint.zero
    var size = CGSize.zero
    guard AXValueGetValue(positionRef as! AXValue, .cgPoint, &position),
          AXValueGetValue(sizeRef as! AXValue, .cgSize, &size) else { return nil }
    return CGRect(origin: position, size: size)
}

func processPath(_ pid: Int32) -> String? {
    var buffer = [CChar](repeating: 0, count: Int(MAXPATHLEN))
    guard proc_pidpath(pid, &buffer, UInt32(buffer.count)) > 0 else { return nil }
    return String(cString: buffer)
}

func exactManager() -> WindowInfo {
    guard let rows = CGWindowListCopyWindowInfo(
        [.optionAll, .excludeDesktopElements], kCGNullWindowID
    ) as? [[String: Any]] else { fail("window inventory") }
    let visibleRows = rows.filter { row in
        guard
              let layer = row[kCGWindowLayer as String] as? NSNumber, layer.intValue == 0,
              let alpha = row[kCGWindowAlpha as String] as? NSNumber, alpha.doubleValue > 0.01,
              let bounds = row[kCGWindowBounds as String] as? [String: Any],
              bounds["X"] is NSNumber, bounds["Y"] is NSNumber,
              let width = bounds["Width"] as? NSNumber, let height = bounds["Height"] as? NSNumber,
              width.doubleValue > 0, height.doubleValue > 0 else { return false }
        return true
    }
    let matches = visibleRows.filter { row in
        row[kCGWindowName as String] as? String == managerTitle
    }
    guard !matches.isEmpty else {
        let hasUnexpectedCompanionWindow = visibleRows.contains { row in
            guard let owner = row[kCGWindowOwnerPID as String] as? NSNumber else { return false }
            return processPath(owner.int32Value) == managerCompanionExecutable
        }
        if hasUnexpectedCompanionWindow { fail("unexpected manager title") }
        fail("no visible exact manager window")
    }
    guard matches.count == 1 else { fail("multiple visible exact manager windows") }
    let match = matches[0]
    guard let owner = match[kCGWindowOwnerPID as String] as? NSNumber,
          let number = match[kCGWindowNumber as String] as? NSNumber,
          let bounds = match[kCGWindowBounds as String] as? [String: Any],
          let x = bounds["X"] as? NSNumber, let y = bounds["Y"] as? NSNumber,
          let width = bounds["Width"] as? NSNumber, let height = bounds["Height"] as? NSNumber,
          let ownerPath = processPath(owner.int32Value), blueStacksExecutables.contains(ownerPath) else {
        fail("exact manager owner is not BlueStacks Air")
    }
    return WindowInfo(id: number.uint32Value, title: managerTitle, ownerPath: ownerPath,
                      x: x.doubleValue, y: y.doubleValue, width: width.doubleValue,
                      height: height.doubleValue, pid: owner.int32Value)
}

func verify(_ current: WindowInfo, _ args: ArraySlice<String>) {
    guard args.count == 5, let id = UInt32(args[args.startIndex]), id == current.id,
          let x = Double(args[args.index(args.startIndex, offsetBy: 1)]),
          let y = Double(args[args.index(args.startIndex, offsetBy: 2)]),
          let width = Double(args[args.index(args.startIndex, offsetBy: 3)]),
          let height = Double(args[args.index(args.startIndex, offsetBy: 4)]),
          [x, y, width, height].allSatisfy(\.isFinite), width > 0, height > 0,
          x == current.x, y == current.y,
          width == current.width, height == current.height else {
        fail("manager window changed")
    }
}

func exactModal(_ parent: WindowInfo) -> ModalInfo {
    guard processPath(parent.pid) == managerCompanionExecutable else {
        fail("modal parent owner changed")
    }
    guard let rows = CGWindowListCopyWindowInfo(
        [.optionAll, .excludeDesktopElements], kCGNullWindowID
    ) as? [[String: Any]] else { fail("modal window inventory") }
    let matches: [ModalInfo] = rows.compactMap { row in
        guard let layer = row[kCGWindowLayer as String] as? NSNumber, layer.intValue >= 0,
              let isOnscreen = row[kCGWindowIsOnscreen as String] as? NSNumber, isOnscreen.boolValue,
              let alpha = row[kCGWindowAlpha as String] as? NSNumber, alpha.doubleValue > 0.01,
              let owner = row[kCGWindowOwnerPID as String] as? NSNumber, owner.int32Value == parent.pid,
              let number = row[kCGWindowNumber as String] as? NSNumber, number.uint32Value != parent.id,
              (row[kCGWindowName as String] as? String).map(\.isEmpty) ?? true,
              let bounds = row[kCGWindowBounds as String] as? [String: Any],
              let x = bounds["X"] as? NSNumber, let y = bounds["Y"] as? NSNumber,
              let width = bounds["Width"] as? NSNumber, let height = bounds["Height"] as? NSNumber,
              [x.doubleValue, y.doubleValue, width.doubleValue, height.doubleValue].allSatisfy(\.isFinite),
              width.doubleValue > 0, height.doubleValue > 0,
              width.doubleValue < parent.width, height.doubleValue < parent.height,
              x.doubleValue >= parent.x, y.doubleValue >= parent.y,
              x.doubleValue + width.doubleValue <= parent.x + parent.width,
              y.doubleValue + height.doubleValue <= parent.y + parent.height,
              x.doubleValue + width.doubleValue / 2 == parent.x + parent.width / 2,
              y.doubleValue + height.doubleValue / 2 == parent.y + parent.height / 2 else { return nil }
        return ModalInfo(id: number.uint32Value, title: "", parentID: parent.id, pid: parent.pid,
                         x: x.doubleValue, y: y.doubleValue,
                         width: width.doubleValue, height: height.doubleValue)
    }
    guard matches.count == 1 else { fail("modal window missing or ambiguous") }
    return matches[0]
}

func verifyParentForModalCapture(_ current: WindowInfo, _ args: ArraySlice<String>) {
    guard args.count == 6, let id = UInt32(args[args.startIndex]), id == current.id,
          let pid = Int32(args[args.index(args.startIndex, offsetBy: 1)]), pid == current.pid,
          let x = Double(args[args.index(args.startIndex, offsetBy: 2)]),
          let y = Double(args[args.index(args.startIndex, offsetBy: 3)]),
          let width = Double(args[args.index(args.startIndex, offsetBy: 4)]),
          let height = Double(args[args.index(args.startIndex, offsetBy: 5)]),
          [x, y, width, height].allSatisfy(\.isFinite), width > 0, height > 0,
          x == current.x, y == current.y, width == current.width, height == current.height else {
        fail("modal parent window changed")
    }
}

func verifyModal(_ current: ModalInfo, _ args: ArraySlice<String>) {
    guard args.count == 6, let id = UInt32(args[args.startIndex]), id == current.id,
          let pid = Int32(args[args.index(args.startIndex, offsetBy: 1)]), pid == current.pid,
          let x = Double(args[args.index(args.startIndex, offsetBy: 2)]),
          let y = Double(args[args.index(args.startIndex, offsetBy: 3)]),
          let width = Double(args[args.index(args.startIndex, offsetBy: 4)]),
          let height = Double(args[args.index(args.startIndex, offsetBy: 5)]),
          [x, y, width, height].allSatisfy(\.isFinite), width > 0, height > 0,
          x == current.x, y == current.y, width == current.width, height == current.height else {
        fail("modal window changed")
    }
}

func captureWindow(_ id: UInt32, _ filename: String) {
    let path = FileManager.default.temporaryDirectory
        .appendingPathComponent("thetowerbot-\(filename)-\(UUID().uuidString).png")
    defer { try? FileManager.default.removeItem(at: path) }
    let capture = Process()
    capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    capture.arguments = ["-x", "-o", "-l", String(id), path.path]
    do {
        try capture.run()
        capture.waitUntilExit()
    } catch {
        fail("\(filename) capture unavailable")
    }
    guard capture.terminationStatus == 0, let data = try? Data(contentsOf: path), !data.isEmpty else {
        fail("\(filename) capture unavailable")
    }
    print(data.base64EncodedString())
}

func systemEventsClick(_ point: CGPoint) {
    guard let source = CGEventSource(stateID: .hidSystemState),
          let move = CGEvent(mouseEventSource: source, mouseType: .mouseMoved,
                             mouseCursorPosition: point, mouseButton: .left),
          let down = CGEvent(mouseEventSource: source, mouseType: .leftMouseDown,
                             mouseCursorPosition: point, mouseButton: .left),
          let up = CGEvent(mouseEventSource: source, mouseType: .leftMouseUp,
                           mouseCursorPosition: point, mouseButton: .left) else {
        fail("manager click unavailable")
    }
    move.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.05)
    down.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.05)
    up.post(tap: .cghidEventTap)
}

func clickModalClone(_ modal: ModalInfo, _ point: CGPoint) {
    guard point.x >= modal.x, point.x <= modal.x + modal.width,
          point.y >= modal.y, point.y <= modal.y + modal.height else {
        fail("target outside exact manager modal")
    }
    systemEventsClick(point)
}

func matchingAccessibilityWindow(_ current: WindowInfo) -> AXUIElement {
    guard let ownerPath = processPath(current.pid), blueStacksExecutables.contains(ownerPath) else {
        fail("manager owner changed")
    }
    let app = AXUIElementCreateApplication(current.pid)
    var windows = attribute(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
    if windows.isEmpty, let focused = attribute(app, kAXFocusedWindowAttribute) {
        windows = [focused as! AXUIElement]
    }
    let matches = windows.filter { window in
        guard attribute(window, kAXTitleAttribute) as? String == managerTitle,
              let frame = elementFrame(window) else { return false }
        return frame.minX == current.x && frame.minY == current.y
            && frame.width == current.width && frame.height == current.height
    }
    guard matches.count == 1 else { fail("Accessibility manager window unavailable") }
    return matches[0]
}

func buttonsNear(_ element: AXUIElement, _ point: CGPoint, _ label: String,
                 _ depth: Int) -> [AXUIElement] {
    if depth > 12 { return [] }
    var matches: [AXUIElement] = []
    if attribute(element, kAXRoleAttribute) as? String == kAXButtonRole,
       attribute(element, kAXTitleAttribute) as? String == label,
       let frame = elementFrame(element),
       frame.contains(point) {
        var actions: CFArray?
        if AXUIElementCopyActionNames(element, &actions) == .success,
           let names = actions as? [String], names.contains(kAXPressAction as String) {
            matches.append(element)
        }
    }
    if let children = attribute(element, kAXChildrenAttribute) as? [AXUIElement] {
        for child in children {
            matches.append(contentsOf: buttonsNear(child, point, label, depth + 1))
        }
    }
    return matches
}

func namedPressableButtons(_ element: AXUIElement, _ label: String,
                           _ depth: Int = 0) -> [AXUIElement] {
    if depth > 12 { return [] }
    var matches: [AXUIElement] = []
    if attribute(element, kAXRoleAttribute) as? String == kAXButtonRole,
       attribute(element, kAXTitleAttribute) as? String == label {
        var actions: CFArray?
        if AXUIElementCopyActionNames(element, &actions) == .success,
           let names = actions as? [String], names.contains(kAXPressAction as String) {
            matches.append(element)
        }
    }
    if let children = attribute(element, kAXChildrenAttribute) as? [AXUIElement] {
        for child in children {
            matches.append(contentsOf: namedPressableButtons(child, label, depth + 1))
        }
    }
    return matches
}

func closeButtonAtTopRight(_ element: AXUIElement, _ dialog: CGRect,
                           _ depth: Int = 0) -> [AXUIElement] {
    if depth > 12 { return [] }
    var matches: [AXUIElement] = []
    if attribute(element, kAXRoleAttribute) as? String == kAXButtonRole,
       let frame = elementFrame(element),
       frame.minX >= dialog.midX, frame.maxX <= dialog.maxX,
       frame.minY >= dialog.minY, frame.maxY <= dialog.minY + dialog.height / 3,
       (attribute(element, kAXTitleAttribute) as? String ?? "").isEmpty {
        var actions: CFArray?
        if AXUIElementCopyActionNames(element, &actions) == .success,
           let names = actions as? [String], names.contains(kAXPressAction as String) {
            matches.append(element)
        }
    }
    if let children = attribute(element, kAXChildrenAttribute) as? [AXUIElement] {
        for child in children {
            matches.append(contentsOf: closeButtonAtTopRight(child, dialog, depth + 1))
        }
    }
    return matches
}

func dismissUpgrade(forPlayerNamed playerTitle: String) {
    guard !playerTitle.isEmpty else { fail("player title is invalid") }
    guard let rows = CGWindowListCopyWindowInfo(
        [.optionAll, .excludeDesktopElements], kCGNullWindowID
    ) as? [[String: Any]] else { fail("player window inventory") }
    let players = rows.compactMap { row -> (pid: Int32, frame: CGRect)? in
        guard row[kCGWindowName as String] as? String == playerTitle,
              let owner = row[kCGWindowOwnerPID as String] as? NSNumber,
              processPath(owner.int32Value) == playerExecutable,
              let bounds = row[kCGWindowBounds as String] as? [String: Any],
              let x = bounds["X"] as? NSNumber, let y = bounds["Y"] as? NSNumber,
              let width = bounds["Width"] as? NSNumber, let height = bounds["Height"] as? NSNumber else {
            return nil
        }
        let frame = CGRect(x: x.doubleValue, y: y.doubleValue,
                           width: width.doubleValue, height: height.doubleValue)
        return frame.width > 0 && frame.height > 0 ? (owner.int32Value, frame) : nil
    }
    guard players.count == 1 else { fail("exact player window is unavailable") }
    let player = players[0]
    let dialogs = rows.compactMap { row -> CGRect? in
        guard row[kCGWindowName as String] as? String == "Upgrade available",
              let owner = row[kCGWindowOwnerPID as String] as? NSNumber, owner.int32Value == player.pid,
              let bounds = row[kCGWindowBounds as String] as? [String: Any],
              let x = bounds["X"] as? NSNumber, let y = bounds["Y"] as? NSNumber,
              let width = bounds["Width"] as? NSNumber, let height = bounds["Height"] as? NSNumber else {
            return nil
        }
        let frame = CGRect(x: x.doubleValue, y: y.doubleValue,
                           width: width.doubleValue, height: height.doubleValue)
        return player.frame.contains(frame) ? frame : nil
    }
    guard dialogs.count == 1 else { fail("player upgrade dialog is unavailable") }
    let app = AXUIElementCreateApplication(player.pid)
    let windows = attribute(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
    let matches = windows.filter {
        guard attribute($0, kAXTitleAttribute) as? String == "Upgrade available",
              let frame = elementFrame($0) else { return false }
        return frame == dialogs[0]
    }
    guard matches.count == 1 else { fail("player upgrade accessibility dialog is unavailable") }
    let closeButtons = closeButtonAtTopRight(matches[0], dialogs[0])
    guard closeButtons.count == 1 else { fail("player upgrade close button is unavailable") }
    guard AXUIElementPerformAction(closeButtons[0], kAXPressAction as CFString) == .success else {
        fail("player upgrade close button press failed")
    }
}

func confirmStopDialog(_ current: WindowInfo) {
    guard processPath(current.pid) == managerCompanionExecutable else {
        fail("stop confirmation owner changed")
    }
    let app = AXUIElementCreateApplication(current.pid)
    let deadline = Date().addingTimeInterval(10)
    while Date() < deadline {
        let windows = attribute(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
        let dialogs = windows.filter {
            attribute($0, kAXTitleAttribute) as? String == "Close instance"
        }
        if dialogs.count == 1 {
            let closeButtons = namedPressableButtons(dialogs[0], "Close")
            guard closeButtons.count == 1 else {
                fail("stop confirmation Close button is unavailable")
            }
            guard AXUIElementPerformAction(closeButtons[0], kAXPressAction as CFString) == .success else {
                fail("stop confirmation Close button press failed")
            }
            return
        }
        if dialogs.count > 1 { fail("stop confirmation is ambiguous") }
        Thread.sleep(forTimeInterval: 0.1)
    }
    fail("stop confirmation did not appear")
}

let args = CommandLine.arguments
guard args.count >= 2 else { fail("usage: dismiss-upgrade PLAYER_TITLE | inspect | capture ID X Y WIDTH HEIGHT | modal-inspect ID X Y WIDTH HEIGHT | modal-capture PARENT_ID PARENT_PID PARENT_X PARENT_Y PARENT_WIDTH PARENT_HEIGHT MODAL_ID MODAL_PID MODAL_X MODAL_Y MODAL_WIDTH MODAL_HEIGHT | modal-press-clone PARENT_ID PARENT_PID PARENT_X PARENT_Y PARENT_WIDTH PARENT_HEIGHT MODAL_ID MODAL_PID MODAL_X MODAL_Y MODAL_WIDTH MODAL_HEIGHT TARGET_X TARGET_Y | press ID X Y WIDTH HEIGHT TARGET_X TARGET_Y LABEL") }
guard AXIsProcessTrusted() else { fail("macOS Accessibility permission required") }
if args[1] == "dismiss-upgrade" {
    guard args.count == 3 else { fail("player title is required") }
    dismissUpgrade(forPlayerNamed: args[2])
    print("dismissed")
    exit(0)
}
let current = exactManager()

if args[1] == "inspect" && args.count == 2 {
    let data = try JSONEncoder().encode(current)
    print(String(data: data, encoding: .utf8)!)
} else if args[1] == "capture" && args.count == 7 {
    verify(current, args[2...6])
    captureWindow(current.id, "manager")
} else if args[1] == "modal-inspect" && args.count == 7 {
    verify(current, args[2...6])
    let modal = exactModal(current)
    let data = try JSONEncoder().encode([modal])
    print(String(data: data, encoding: .utf8)!)
} else if args[1] == "modal-capture" && args.count == 14 {
    verifyParentForModalCapture(current, args[2...7])
    let modal = exactModal(current)
    verifyModal(modal, args[8...13])
    captureWindow(modal.id, "manager-modal")
} else if args[1] == "modal-press-clone" && args.count == 16 {
    verifyParentForModalCapture(current, args[2...7])
    let modal = exactModal(current)
    verifyModal(modal, args[8...13])
    guard let x = Double(args[14]), let y = Double(args[15]), x.isFinite, y.isFinite else {
        fail("target outside exact manager modal")
    }
    clickModalClone(modal, CGPoint(x: x, y: y))
    print("pressed")
} else if args[1] == "press" && args.count == 10 {
    verify(current, args[2...6])
    guard allowedControlLabels.contains(args[9]) else { fail("exact manager control unavailable") }
    guard let x = Double(args[7]), let y = Double(args[8]), x.isFinite, y.isFinite,
          x >= current.x, x <= current.x + current.width,
          y >= current.y, y <= current.y + current.height else {
        fail("target outside exact manager window")
    }
    systemEventsClick(CGPoint(x: x, y: y))
    if args[9] == "Stop" { confirmStopDialog(current) }
    print("pressed")
} else {
    fail("unsupported command")
}
