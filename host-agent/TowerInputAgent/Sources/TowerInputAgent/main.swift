import ApplicationServices
import CoreGraphics
import CryptoKit
import Darwin
import Foundation
import ScreenCaptureKit

private let managerBundleID = "com.now.gg.BlueStacksAirMIM"
private let managerTitle = "BlueStacks Air Manager"
private let maxRequestBytes = 4_096

struct Doctor: Encodable {
    let accessibility: Bool
    let screenRecording: Bool
    let managerVisible: Bool
    let socketPath: String
}

struct AgentFrame: Encodable {
    let windowID: UInt32
    let ownerBundleID: String
    let x: Double
    let y: Double
    let width: Double
    let height: Double
    let pixelWidth: Int
    let pixelHeight: Int
    let digest: String

    enum CodingKeys: String, CodingKey {
        case windowID = "window_id"
        case ownerBundleID = "owner_bundle_id"
        case x, y, width, height, digest
        case pixelWidth = "pixel_width"
        case pixelHeight = "pixel_height"
    }
}

enum CaptureError: LocalizedError {
    case managerUnavailable
    case managerAmbiguous
    case imageUnavailable

    var errorDescription: String? {
        switch self {
        case .managerUnavailable: return "exact BlueStacks Air Manager window is unavailable"
        case .managerAmbiguous: return "more than one BlueStacks Air Manager window is visible"
        case .imageUnavailable: return "manager capture image is unavailable"
        }
    }
}

let arguments = CommandLine.arguments
let socketIndex = arguments.firstIndex(of: "--socket")
let socketPath = socketIndex.flatMap { arguments.indices.contains(arguments.index(after: $0))
    ? arguments[arguments.index(after: $0)] : nil } ?? ""

func jsonData(_ object: [String: Any]) -> Data {
    (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])) ?? Data("{\"state\":\"failed\",\"detail\":\"unable to encode response\"}".utf8)
}

func writeJSON(_ descriptor: Int32, _ object: [String: Any]) {
    var payload = jsonData(object)
    payload.append(0x0A)
    _ = payload.withUnsafeBytes { write(descriptor, $0.baseAddress, $0.count) }
}

@available(macOS 14.0, *)
func exactManagerWindow() async throws -> SCWindow {
    let visibleWindowIDs = Set((CGWindowListCopyWindowInfo(
        [.optionAll, .excludeDesktopElements], kCGNullWindowID
    ) as? [[String: Any]] ?? []).compactMap { row -> UInt32? in
        guard let number = row[kCGWindowNumber as String] as? NSNumber,
              let layer = row[kCGWindowLayer as String] as? NSNumber, layer.intValue == 0,
              let alpha = row[kCGWindowAlpha as String] as? NSNumber, alpha.doubleValue > 0.01,
              let bounds = row[kCGWindowBounds as String] as? [String: Any],
              let width = bounds["Width"] as? NSNumber, width.doubleValue > 0,
              let height = bounds["Height"] as? NSNumber, height.doubleValue > 0 else {
            return nil
        }
        return number.uint32Value
    })
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    let matches = content.windows.filter { window in
        window.owningApplication?.bundleIdentifier == managerBundleID
            && window.title == managerTitle
            && visibleWindowIDs.contains(window.windowID)
            && !window.frame.isEmpty
    }
    guard matches.count == 1 else {
        throw matches.isEmpty ? CaptureError.managerUnavailable : CaptureError.managerAmbiguous
    }
    return matches[0]
}

@available(macOS 14.0, *)
func captureManager() async throws -> AgentFrame {
    let window = try await exactManagerWindow()
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    guard let display = content.displays.first(where: { $0.frame.intersects(window.frame) }),
          display.frame.width > 0, display.frame.height > 0 else {
        throw CaptureError.imageUnavailable
    }
    let scaleX = Double(display.width) / display.frame.width
    let scaleY = Double(display.height) / display.frame.height
    guard scaleX.isFinite, scaleY.isFinite, scaleX > 0, scaleY > 0 else {
        throw CaptureError.imageUnavailable
    }
    let filter = SCContentFilter(desktopIndependentWindow: window)
    let configuration = SCStreamConfiguration()
    configuration.showsCursor = false
    configuration.width = Int((window.frame.width * scaleX).rounded())
    configuration.height = Int((window.frame.height * scaleY).rounded())
    let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration)
    guard let provider = image.dataProvider, let bytes = provider.data as Data? else {
        throw CaptureError.imageUnavailable
    }
    let frame = window.frame
    return AgentFrame(
        windowID: window.windowID,
        ownerBundleID: managerBundleID,
        x: frame.origin.x,
        y: frame.origin.y,
        width: frame.size.width,
        height: frame.size.height,
        pixelWidth: image.width,
        pixelHeight: image.height,
        digest: SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
    )
}

func waitForCapture() -> Result<AgentFrame, Error> {
    let semaphore = DispatchSemaphore(value: 0)
    var result: Result<AgentFrame, Error> = .failure(CaptureError.managerUnavailable)
    Task {
        guard #available(macOS 14.0, *) else {
            result = .failure(CaptureError.managerUnavailable)
            semaphore.signal()
            return
        }
        do {
            result = .success(try await captureManager())
        } catch {
            result = .failure(error)
        }
        semaphore.signal()
    }
    semaphore.wait()
    return result
}

func managerIsVisible() async -> Bool {
    guard #available(macOS 14.0, *) else { return false }
    return (try? await exactManagerWindow()) != nil
}

func requestAction(_ descriptor: Int32, payload: Data) {
    guard let object = try? JSONSerialization.jsonObject(with: payload) as? [String: Any],
          let action = object["action"] as? String else {
        writeJSON(descriptor, ["state": "blocked", "detail": "request is invalid"])
        return
    }
    switch action {
    case "health":
        writeJSON(descriptor, [
            "state": AXIsProcessTrusted() && CGPreflightScreenCaptureAccess() ? "ready" : "blocked",
            "detail": "accessibility and screen recording permissions are required",
        ])
    case "capture_manager":
        switch waitForCapture() {
        case .success(let frame):
            guard let frameObject = try? JSONSerialization.jsonObject(with: JSONEncoder().encode(frame)) else {
                writeJSON(descriptor, ["state": "failed", "detail": "manager capture encoding failed"])
                return
            }
            writeJSON(descriptor, ["state": "ready", "detail": "exact manager captured", "evidence": frameObject])
        case .failure(let error):
            writeJSON(descriptor, ["state": "blocked", "detail": error.localizedDescription])
        }
    case "open_new_instance", "choose_clone_instance", "choose_source", "create_clone":
        writeJSON(descriptor, ["state": "blocked", "detail": "manager input is not installed"])
    default:
        writeJSON(descriptor, ["state": "blocked", "detail": "action is unavailable"])
    }
}

func prepareRuntimeDirectory(for socketPath: String) -> Bool {
    let runtimePath = (NSHomeDirectory() as NSString).appendingPathComponent(
        "Library/Application Support/TheTowerBot/runtime"
    )
    let actualRuntimePath = URL(fileURLWithPath: socketPath).deletingLastPathComponent().path
    guard actualRuntimePath == runtimePath else { return false }
    do {
        try FileManager.default.createDirectory(
            atPath: runtimePath,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        return chmod(runtimePath, 0o700) == 0
    } catch {
        return false
    }
}

func serve(socketPath: String) -> Never {
    guard !socketPath.isEmpty, socketPath.utf8.count < MemoryLayout.size(ofValue: sockaddr_un().sun_path) else {
        fputs("socket path is invalid\n", stderr); exit(64)
    }
    guard prepareRuntimeDirectory(for: socketPath) else {
        fputs("socket runtime is invalid\n", stderr); exit(64)
    }
    unlink(socketPath)
    let listener = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
    guard listener >= 0 else { fputs("socket unavailable\n", stderr); exit(1) }
    var address = sockaddr_un()
    address.sun_family = sa_family_t(AF_UNIX)
    withUnsafeMutableBytes(of: &address.sun_path) { bytes in
        socketPath.withCString { source in bytes.baseAddress!.copyMemory(from: source, byteCount: socketPath.utf8.count + 1) }
    }
    let bound = withUnsafePointer(to: &address) { pointer in
        pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(listener, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
    }
    guard bound == 0, chmod(socketPath, 0o600) == 0, listen(listener, 8) == 0 else {
        fputs("socket bind failed\n", stderr); exit(1)
    }
    while true {
        let client = accept(listener, nil, nil)
        guard client >= 0 else { continue }
        var peerUID: uid_t = 0
        var peerGID: gid_t = 0
        guard getpeereid(client, &peerUID, &peerGID) == 0, peerUID == getuid() else {
            writeJSON(client, ["state": "blocked", "detail": "client uid is unavailable"])
            close(client)
            continue
        }
        var buffer = [UInt8](repeating: 0, count: maxRequestBytes + 1)
        let received = read(client, &buffer, buffer.count)
        guard received > 0, received <= maxRequestBytes,
              let newline = buffer[..<received].firstIndex(of: 0x0A) else {
            writeJSON(client, ["state": "blocked", "detail": "request is invalid"])
            close(client)
            continue
        }
        requestAction(client, payload: Data(buffer[..<newline]))
        close(client)
    }
}

func printDoctorAndExit() {
    Task {
        let result = Doctor(
            accessibility: AXIsProcessTrusted(),
            screenRecording: CGPreflightScreenCaptureAccess(),
            managerVisible: await managerIsVisible(),
            socketPath: socketPath
        )
        let encoder = JSONEncoder()
        print(String(data: try! encoder.encode(result), encoding: .utf8)!)
        exit(0)
    }
    dispatchMain()
}

if arguments.dropFirst().first == "doctor" {
    printDoctorAndExit()
} else if arguments.dropFirst().first == "serve" {
    serve(socketPath: socketPath)
} else {
    fputs("usage: tower-input-agent doctor|serve --socket PATH\n", stderr)
    exit(64)
}
