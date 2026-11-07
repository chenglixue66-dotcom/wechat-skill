// Experimental, read-only single-window ScreenCaptureKit + local Vision probe.
// Build only until the user explicitly authorizes this non-CUA capture path.
// Single-window SCStreamConfiguration.sourceRect is ignored by macOS: one full
// window frame exists briefly in memory. Only its predetermined top strip is
// passed to Vision; no full-window image or recognized text is saved/output.
// No SCStream, audio, input, clipboard, AX writes, sending, networking or grants.
import AppKit
import CoreGraphics
import CryptoKit
import Darwin
import Foundation
import ImageIO
import ScreenCaptureKit
import UniformTypeIdentifiers
import Vision

private let bundleID = "com.tencent.xinWeChat"
private let teamID = "5A4RE8SF68"
private let appURL = URL(fileURLWithPath: "/Applications/WeChat.app")
private let maximumPixels = 12_000_000
private struct Failure: Error { let code: String }
private func fail(_ code: String) throws -> Never { throw Failure(code: code) }
private func finalReport(_ report: [String: Any]) -> [String: Any] {
    let base: [String: Any] = ["mode": "experimental_sck_header_probe",
        "live_send_permitted": false, "send_attempts": 0, "ui_writes": 0,
        "capture_attempted": false, "frame_captured": false,
        "full_window_image_saved": false, "ocr_plaintext_output": false,
        "capture_is_not_send_preflight": true]
    var merged = base.merging(report) { _, new in new }
    // An observer result cannot promote itself into a real-sending capability.
    merged["live_send_permitted"] = false
    merged["capture_is_not_send_preflight"] = true
    return merged
}
private func emit(_ report: [String: Any], status: Int32 = 0) -> Never {
    let merged = finalReport(report)
    if let data = try? JSONSerialization.data(withJSONObject: merged, options: [.sortedKeys]),
       let text = String(data: data, encoding: .utf8) { print(text) }
    else { print("{\"status\":\"blocked\",\"code\":\"REPORT_FAILED\",\"live_send_permitted\":false}") }
    exit(status)
}

private struct Options {
    var capture = false, acknowledged = false, help = false, selfTest = false
    var pid: pid_t = 0
    var windowID: CGWindowID = 0
    var headerPoints = 64
    var waitForeground: TimeInterval = 0
    var saveDirectory: URL?
}
private func options(_ supplied: [String]) throws -> Options {
    var result = Options(), arguments = supplied
    if arguments.isEmpty || arguments == ["--help"] {
        result.help = true
        return result
    }
    if arguments == ["--self-test"] {
        result.selfTest = true
        return result
    }
    func value() throws -> String {
        guard !arguments.isEmpty else { try fail("MISSING_ARGUMENT_VALUE") }
        return arguments.removeFirst()
    }
    while !arguments.isEmpty {
        switch arguments.removeFirst() {
        case "--capture": result.capture = true
        case "--ack-local-window-capture": result.acknowledged = true
        case "--pid":
            guard let pid = Int32(try value()), pid > 0 else { try fail("INVALID_PID") }
            result.pid = pid
        case "--window-id":
            guard let window = UInt32(try value()), window > 0 else { try fail("INVALID_WINDOW_ID") }
            result.windowID = window
        case "--header-points":
            guard let points = Int(try value()), (8...64).contains(points) else { try fail("INVALID_HEADER_HEIGHT") }
            result.headerPoints = points
        case "--wait-foreground":
            guard let seconds = Double(try value()), seconds.isFinite, (0...30).contains(seconds)
            else { try fail("INVALID_FOREGROUND_WAIT") }
            result.waitForeground = seconds
        case "--save-header-dir":
            let path = try value()
            guard path.hasPrefix("/") else { try fail("ABSOLUTE_PRIVATE_DIRECTORY_REQUIRED") }
            result.saveDirectory = URL(fileURLWithPath: path, isDirectory: true)
        default: try fail("UNKNOWN_ARGUMENT")
        }
    }
    guard result.capture, result.acknowledged else { try fail("EXPLICIT_CAPTURE_OPT_IN_REQUIRED") }
    guard result.pid > 0, result.windowID > 0 else { try fail("PID_AND_WINDOW_ID_REQUIRED") }
    return result
}

private func helpReport() -> [String: Any] {
    ["status": "not_run", "code": "CAPTURE_NOT_REQUESTED",
     "usage": "sck_probe --self-test | --capture --ack-local-window-capture --pid PID --window-id ID [--wait-foreground 0..30] [--header-points 8..64] [--save-header-dir PRIVATE_DIRECTORY]",
     "foreground_policy": "Default wait=0 is a passive observer: the selected visible window may be in the background. Positive wait explicitly requests strict foreground and first-major-window stability; neither mode is a send preflight.",
     "authorization_required": "Explicit user approval for one official-WeChat window frame in local memory, full-frame pixel statistics, and local OCR of only its top <=64 point strip. Saving that strip requires a separate optional flag; no full image or OCR plaintext is emitted."]
}

private func officialSignature() throws {
    let verify = Process()
    verify.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
    verify.arguments = ["--verify", "--deep", "--strict", "-R",
        "=anchor apple generic and identifier \"\(bundleID)\"", appURL.path]
    verify.standardOutput = FileHandle.nullDevice; verify.standardError = FileHandle.nullDevice
    try verify.run(); verify.waitUntilExit()
    guard verify.terminationStatus == 0 else { try fail("OFFICIAL_SIGNATURE_NOT_VERIFIED") }
    let details = Process(), pipe = Pipe()
    details.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
    details.arguments = ["--display", "--verbose=2", appURL.path]
    details.standardOutput = FileHandle.nullDevice; details.standardError = pipe
    try details.run()
    let data = pipe.fileHandleForReading.readDataToEndOfFile(); details.waitUntilExit()
    let text = String(data: data, encoding: .utf8) ?? ""
    guard details.terminationStatus == 0,
          text.split(separator: "\n").contains(Substring("TeamIdentifier=\(teamID)"))
    else { try fail("OFFICIAL_TEAM_NOT_VERIFIED") }
}

private struct Activity {
    let appActive: Bool, frontmostMatchesTarget: Bool?, firstMajorMatchesTarget: Bool
    var targetIsForeground: Bool? {
        if !appActive || !firstMajorMatchesTarget { return false }
        return frontmostMatchesTarget
    }
}
private func foregroundGateAllows(_ activity: Activity, requiresForeground: Bool) -> Bool {
    !requiresForeground || activity.targetIsForeground == true
}
private struct Identity {
    let pid: pid_t, launch: Date, windowID: CGWindowID, frame: CGRect
    let activity: Activity
}
private func pumpWorkspace() {
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
}
private func currentIdentity(_ selected: Options, previous: Identity? = nil) throws -> Identity {
    pumpWorkspace()
    let apps = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID)
    guard apps.count == 1, let app = apps.first, app.processIdentifier == selected.pid,
          app.bundleURL?.resolvingSymlinksInPath() == appURL.resolvingSymlinksInPath(),
          let launched = app.launchDate else { try fail("OFFICIAL_PROCESS_IDENTITY_MISMATCH") }
    let frontmost = NSWorkspace.shared.frontmostApplication?.processIdentifier
    let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
    guard let window = windows.first(where: {
        ($0[kCGWindowNumber as String] as? NSNumber)?.uint32Value == selected.windowID
    }), (window[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == selected.pid,
        (window[kCGWindowLayer as String] as? NSNumber)?.intValue == 0,
        ((window[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 0) > 0,
        let bounds = window[kCGWindowBounds as String] as? [String: Any],
        let frame = CGRect(dictionaryRepresentation: bounds as CFDictionary),
        frame.width.isFinite, frame.height.isFinite,
        frame.width >= 200, frame.height >= 200,
        frame.width <= 8192, frame.height <= 8192
    else { try fail("TARGET_WINDOW_NOT_VISIBLE_OR_INVALID") }
    let firstMajor = windows.first { item in
        guard (item[kCGWindowLayer as String] as? NSNumber)?.intValue == 0,
              let bounds = item[kCGWindowBounds as String] as? [String: Any],
              let rectangle = CGRect(dictionaryRepresentation: bounds as CFDictionary) else { return false }
        return rectangle.width >= 200 && rectangle.height >= 200
    }
    let activity = Activity(appActive: app.isActive,
        frontmostMatchesTarget: frontmost.map { $0 == selected.pid },
        firstMajorMatchesTarget: (firstMajor?[kCGWindowNumber as String] as? NSNumber)?.uint32Value == selected.windowID)
    guard foregroundGateAllows(activity, requiresForeground: selected.waitForeground > 0)
    else { try fail("TARGET_NOT_STRICTLY_FOREGROUND") }
    // This observer needs a visible, non-minimized selected window, not keyboard
    // focus. CG's on-screen set plus SCWindow.isOnScreen are checked separately;
    // no unreadable AX minimization flag is converted to a false value.
    let identity = Identity(pid: selected.pid, launch: launched, windowID: selected.windowID, frame: frame, activity: activity)
    if let previous = previous {
        guard previous.pid == identity.pid, previous.launch == identity.launch,
              previous.windowID == identity.windowID, previous.frame == identity.frame
        else { try fail("TARGET_CHANGED_DURING_CAPTURE") }
    }
    return identity
}

@MainActor
private func waitForForeground(_ selected: Options) async throws -> Identity {
    guard selected.waitForeground > 0 else { try fail("FOREGROUND_WAIT_NOT_REQUESTED") }
    // Announce readiness after signature verification, before the user's switch.
    FileHandle.standardError.write(Data("READY_FOR_FOREGROUND\n".utf8))
    let deadline = ProcessInfo.processInfo.systemUptime + selected.waitForeground
    var stableIdentity: Identity?, stableSince: TimeInterval?
    while true {
        do {
            let candidate = try currentIdentity(selected)
            if let old = stableIdentity,
               old.pid == candidate.pid, old.launch == candidate.launch,
               old.windowID == candidate.windowID, old.frame == candidate.frame {
                if let since = stableSince, ProcessInfo.processInfo.systemUptime - since >= 0.7 { return candidate }
            } else {
                stableIdentity = candidate; stableSince = ProcessInfo.processInfo.systemUptime
            }
        } catch {
            stableIdentity = nil; stableSince = nil
        }
        if ProcessInfo.processInfo.systemUptime >= deadline {
            try fail("FOREGROUND_WAIT_TIMEOUT")
        }
        // Only explicitly requested strict mode enters this waiting loop.
        // Passive observers never activate, focus, or wait for the target.
        try await Task.sleep(nanoseconds: 100_000_000)
    }
}

private func validateDimensions(_ width: Int, _ height: Int) throws {
    guard width > 0, height > 0, width <= maximumPixels / height
    else { try fail("IMAGE_DIMENSIONS_UNSAFE") }
}

private func headerCrop(_ image: CGImage, windowHeightPoints: CGFloat, headerPoints: Int) throws -> CGImage {
    try validateDimensions(image.width, image.height)
    guard windowHeightPoints.isFinite, windowHeightPoints >= 200,
          (8...64).contains(headerPoints) else { try fail("INVALID_HEADER_GEOMETRY") }
    let height = Int(ceil(Double(headerPoints) * Double(image.height) / Double(windowHeightPoints)))
    guard height > 0, height <= image.height / 3,
          let cropped = image.cropping(to: CGRect(x: 0, y: 0, width: image.width, height: height))
    else { try fail("HEADER_CROP_FAILED") }
    return cropped
}

private func whiteStatistics(_ image: CGImage) throws -> [String: Any] {
    try validateDimensions(image.width, image.height)
    let count = image.width * image.height
    var pixels = [UInt8](repeating: 0, count: count * 4)
    let rendered = pixels.withUnsafeMutableBytes { memory -> Bool in
        guard let context = CGContext(data: memory.baseAddress, width: image.width, height: image.height,
            bitsPerComponent: 8, bytesPerRow: image.width * 4,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue)
        else { return false }
        context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
        return true
    }
    guard rendered else { try fail("PIXEL_STATISTICS_FAILED") }
    var white = 0, black = 0, transparent = 0
    for index in stride(from: 0, to: pixels.count, by: 4) {
        if pixels[index + 3] <= 5 { transparent += 1 }
        if pixels[index] >= 250 && pixels[index + 1] >= 250 && pixels[index + 2] >= 250 && pixels[index + 3] >= 250 { white += 1 }
        if pixels[index] <= 5 && pixels[index + 1] <= 5 && pixels[index + 2] <= 5 && pixels[index + 3] >= 250 { black += 1 }
    }
    return ["all_pixels_near_white": white == count,
            "all_pixels_near_black": black == count,
            "near_white_fraction": Double(white) / Double(count),
            "near_black_fraction": Double(black) / Double(count),
            "transparent_fraction": Double(transparent) / Double(count)]
}

private func headerOCR(_ image: CGImage) throws -> [String: Any] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate; request.usesLanguageCorrection = false
    request.automaticallyDetectsLanguage = false
    let supported = try request.supportedRecognitionLanguages()
    let selected = ["zh-Hans", "en-US"].filter { supported.contains($0) }
    guard !selected.isEmpty else { try fail("LOCAL_OCR_LANGUAGE_UNAVAILABLE") }
    request.recognitionLanguages = selected
    try VNImageRequestHandler(cgImage: image, orientation: .up).perform([request])
    let candidates = (request.results ?? []).compactMap { $0.topCandidates(1).first }.filter { $0.confidence >= 0.3 }
    let lines = candidates.prefix(100).map { $0.string.trimmingCharacters(in: .whitespacesAndNewlines) }
    let publicLabels: Set<String> = ["weixin", "wechat", "微信", "search", "搜索", "chats", "聊天", "contacts", "联系人"]
    let matched = Set(lines.map { $0.lowercased() }).intersection(publicLabels).sorted()
    let digest = SHA256.hash(data: Data(lines.joined(separator: "\n").utf8)).map { String(format: "%02x", $0) }.joined()
    return ["line_count": lines.count, "text_sha256": digest,
            "public_control_markers": matched, "plaintext_output": false,
            "recipient_binding": "not_established_by_this_probe"]
}

private let privateImageRoot = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".config/wechat-skill/sck-probes", isDirectory: true)
private func saveHeader(_ image: CGImage, directory: URL, allowedRoot root: URL = privateImageRoot) throws -> String {
    // Foundation standardization rewrites /private/var to the /var symlink on
    // macOS; retain the explicit path and check every component with lstat.
    let path = directory
    guard path.path == root.path || path.path.hasPrefix(root.path + "/"),
          path.isFileURL, !directory.pathComponents.contains(".."), !directory.pathComponents.contains(".")
    else { try fail("HEADER_DIRECTORY_OUTSIDE_PRIVATE_ROOT") }
    var cursor = URL(fileURLWithPath: "/", isDirectory: true)
    for component in path.pathComponents.dropFirst() {
        cursor.appendPathComponent(component, isDirectory: true)
        var metadata = stat()
        if lstat(cursor.path, &metadata) == 0 {
            guard metadata.st_mode & S_IFMT != S_IFLNK else { try fail("HEADER_DIRECTORY_SYMLINK_REJECTED") }
        }
    }
    try FileManager.default.createDirectory(at: path, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    var info = stat()
    guard lstat(path.path, &info) == 0, info.st_uid == getuid(),
          info.st_mode & S_IFMT == S_IFDIR, info.st_mode & 0o777 == 0o700
    else { try fail("PRIVATE_HEADER_DIRECTORY_REQUIRED") }
    let data = NSMutableData()
    guard let destination = CGImageDestinationCreateWithData(data, UTType.png.identifier as CFString, 1, nil)
    else { try fail("HEADER_ENCODING_FAILED") }
    CGImageDestinationAddImage(destination, image, nil)
    guard CGImageDestinationFinalize(destination) else { try fail("HEADER_ENCODING_FAILED") }
    let file = path.appendingPathComponent("header-\(UUID().uuidString).png")
    let descriptor = open(file.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
    guard descriptor >= 0 else { try fail("HEADER_SAVE_FAILED") }
    let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
    do { try handle.write(contentsOf: data as Data); try handle.close() }
    catch { try? handle.close(); try? FileManager.default.removeItem(at: file); try fail("HEADER_SAVE_FAILED") }
    return file.path
}

// No screenshot API, UI, permission check, OCR, or real user image is involved.
private func syntheticImage() throws -> CGImage {
    let width = 64, height = 256
    var bytes = [UInt8](repeating: 0, count: width * height * 4)
    for y in 0..<height {
        for x in 0..<width {
            let index = (y * width + x) * 4
            let color: UInt8 = y < 64 ? 255 : 0
            bytes[index] = color; bytes[index + 1] = color; bytes[index + 2] = color; bytes[index + 3] = 255
        }
    }
    guard let provider = CGDataProvider(data: Data(bytes) as CFData),
          let image = CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32,
            bytesPerRow: width * 4, space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue),
            provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)
    else { try fail("SYNTHETIC_IMAGE_FAILED") }
    return image
}

private func syntheticTests() throws -> [String: Any] {
    var passed: [String] = []
    func require(_ condition: Bool, _ label: String) throws {
        guard condition else { try fail("SELF_TEST_" + label.uppercased()) }
        passed.append(label)
    }
    func rejects(_ label: String, _ operation: () throws -> Void) throws {
        do { try operation() } catch is Failure { passed.append(label); return }
        try fail("SELF_TEST_EXPECTED_REJECTION")
    }
    let image = try syntheticImage()
    let header = try headerCrop(image, windowHeightPoints: 256, headerPoints: 32)
    let wholeStats = try whiteStatistics(image), headerStats = try whiteStatistics(header)
    try require(header.width == 64 && header.height == 32 && headerStats["all_pixels_near_white"] as? Bool == true,
                "production_crop_is_top_strip")
    try require(wholeStats["all_pixels_near_white"] as? Bool == false && wholeStats["near_black_fraction"] as? Double == 0.75,
                "dark_body_is_not_blank_white")
    if let bottom = image.cropping(to: CGRect(x: 0, y: 192, width: 64, height: 32)) {
        try require(try whiteStatistics(bottom)["all_pixels_near_black"] as? Bool == true, "black_fixture_recognized")
    } else { try fail("SELF_TEST_BOTTOM_CROP") }
    try rejects("oversize_image_rejected") { try validateDimensions(Int.max, 2) }
    try rejects("zero_image_rejected") { try validateDimensions(0, 256) }
    try rejects("oversize_header_rejected") { _ = try headerCrop(image, windowHeightPoints: 256, headerPoints: 65) }
    try rejects("invalid_window_height_rejected") { _ = try headerCrop(image, windowHeightPoints: .nan, headerPoints: 32) }
    try rejects("capture_without_ack_rejected") { _ = try options(["--capture", "--pid", "1", "--window-id", "1"]) }
    try rejects("invalid_wait_rejected") { _ = try options(["--capture", "--ack-local-window-capture", "--pid", "1", "--window-id", "1", "--wait-foreground", "31"]) }
    try require(try options([]).help && options(["--self-test"]).selfTest, "no_capture_routes_selected")
    let background = Activity(appActive: false, frontmostMatchesTarget: false, firstMajorMatchesTarget: false)
    let foreground = Activity(appActive: true, frontmostMatchesTarget: true, firstMajorMatchesTarget: true)
    let unknown = Activity(appActive: true, frontmostMatchesTarget: nil, firstMajorMatchesTarget: true)
    let defaultCapture = try options(["--capture", "--ack-local-window-capture", "--pid", "1", "--window-id", "1"])
    try require(defaultCapture.waitForeground == 0 && foregroundGateAllows(background, requiresForeground: defaultCapture.waitForeground > 0),
                "background_observer_foreground_gate_not_required")
    try require(!foregroundGateAllows(background, requiresForeground: true), "explicit_strict_mode_rejects_background")
    try require(foregroundGateAllows(foreground, requiresForeground: true), "explicit_strict_mode_accepts_foreground")
    try require(unknown.targetIsForeground == nil && !foregroundGateAllows(unknown, requiresForeground: true), "unknown_foreground_not_promoted")
    let observerReport = finalReport(["status": "observer", "live_send_permitted": true, "capture_is_not_send_preflight": false])
    try require(observerReport["live_send_permitted"] as? Bool == false && observerReport["capture_is_not_send_preflight"] as? Bool == true,
                "observer_can_never_claim_send_ready")

    // Exercise the actual default entry point in a child process. It has no
    // capture opt-in, so must terminate before app/permission/SCK/OCR access.
    let child = Process(), output = Pipe()
    child.executableURL = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
    child.arguments = []; child.standardOutput = output; child.standardError = FileHandle.nullDevice
    try child.run()
    let childData = output.fileHandleForReading.readDataToEndOfFile(); child.waitUntilExit()
    let report = try JSONSerialization.jsonObject(with: childData) as? [String: Any]
    try require(child.terminationStatus == 0 && report?["status"] as? String == "not_run"
                && report?["capture_attempted"] as? Bool == false && report?["frame_captured"] as? Bool == false,
                "default_entry_zero_capture")

    guard let resolvedTemporary = realpath(FileManager.default.temporaryDirectory.path, nil)
    else { try fail("SYNTHETIC_TEMP_DIRECTORY_UNAVAILABLE") }
    let fixtureParent = URL(fileURLWithPath: String(cString: resolvedTemporary), isDirectory: true)
    free(resolvedTemporary)
    let fixture = fixtureParent.appendingPathComponent("sck-synthetic-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: fixture, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
    defer { try? FileManager.default.removeItem(at: fixture) }
    let directory = fixture.appendingPathComponent("private", isDirectory: true)
    let file = try saveHeader(header, directory: directory, allowedRoot: fixture)
    var directoryInfo = stat(), fileInfo = stat()
    guard lstat(directory.path, &directoryInfo) == 0, lstat(file, &fileInfo) == 0 else { try fail("SELF_TEST_STAT_FAILED") }
    let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: file) as CFURL, nil)
    let saved = source.flatMap { CGImageSourceCreateImageAtIndex($0, 0, nil) }
    try require(directoryInfo.st_mode & 0o777 == 0o700 && fileInfo.st_mode & 0o777 == 0o600
                && saved?.width == 64 && saved?.height == 32, "only_private_header_png_saved")
    try rejects("outside_private_root_rejected") {
        _ = try saveHeader(header, directory: fixture.deletingLastPathComponent().appendingPathComponent("never-create-\(UUID().uuidString)"), allowedRoot: fixture)
    }
    let link = fixture.appendingPathComponent("linked", isDirectory: true)
    try FileManager.default.createSymbolicLink(at: link, withDestinationURL: directory)
    try rejects("save_symlink_rejected") { _ = try saveHeader(header, directory: link, allowedRoot: fixture) }
    let publicDirectory = fixture.appendingPathComponent("nonprivate", isDirectory: true)
    try FileManager.default.createDirectory(at: publicDirectory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o755])
    try rejects("nonprivate_directory_rejected") { _ = try saveHeader(header, directory: publicDirectory, allowedRoot: fixture) }
    try FileManager.default.removeItem(at: fixture)
    try require(!FileManager.default.fileExists(atPath: fixture.path), "synthetic_artifacts_removed")
    return ["status": "synthetic_tests_passed", "checks": passed, "check_count": passed.count,
            "synthetic_files_only": true, "temporary_artifacts_removed_on_return": true,
            "ocr_performed": false, "capture_attempted": false, "frame_captured": false]
}

@main
private struct Main {
    @MainActor static func main() async {
        var captureAttempted = false, frameCaptured = false
        do {
            let selected = try options(Array(CommandLine.arguments.dropFirst()))
            if selected.help { emit(helpReport()) }
            if selected.selfTest { emit(try syntheticTests()) }
            guard #available(macOS 15.2, *) else { try fail("MACOS_15_2_REQUIRED") }
            guard CGPreflightScreenCaptureAccess() else { try fail("SCREEN_CAPTURE_PERMISSION_NOT_GRANTED_NO_PROMPT_REQUESTED") }
            try officialSignature()
            let before: Identity
            if selected.waitForeground > 0 {
                before = try await waitForForeground(selected)
            } else {
                before = try currentIdentity(selected)
                FileHandle.standardError.write(Data("READY_FOR_READ_ONLY_OBSERVER\n".utf8))
            }
            let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
            let matches = content.windows.filter { $0.windowID == selected.windowID }
            guard matches.count == 1, let window = matches.first,
                  window.owningApplication?.processID == selected.pid,
                  window.owningApplication?.bundleIdentifier == bundleID,
                  window.windowLayer == 0, window.isOnScreen,
                  abs(window.frame.width - before.frame.width) <= 1,
                  abs(window.frame.height - before.frame.height) <= 1
            else { try fail("SCK_WINDOW_IDENTITY_MISMATCH") }
            let captureBefore = try currentIdentity(selected, previous: before)
            let filter = SCContentFilter(desktopIndependentWindow: window)
            guard filter.includedWindows.count == 1, filter.includedWindows.first?.windowID == selected.windowID
            else { try fail("SCK_FILTER_NOT_SINGLE_WINDOW") }
            let configuration = SCStreamConfiguration()
            configuration.width = Int(ceil(window.frame.width)); configuration.height = Int(ceil(window.frame.height))
            guard configuration.width <= maximumPixels / configuration.height else { try fail("CAPTURE_DIMENSIONS_UNSAFE") }
            configuration.showsCursor = false; configuration.capturesAudio = false
            configuration.captureMicrophone = false; configuration.includeChildWindows = false
            configuration.ignoreShadowsSingleWindow = true; configuration.ignoreGlobalClipSingleWindow = false
            // Deliberately no sourceRect: it is ignored for single-window capture.
            captureAttempted = true
            let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration)
            frameCaptured = true
            let captureAfter = try currentIdentity(selected, previous: before)
            guard image.width == configuration.width, image.height == configuration.height else { try fail("UNEXPECTED_CAPTURE_GEOMETRY") }
            let statistics = try whiteStatistics(image)
            let header = try headerCrop(image, windowHeightPoints: window.frame.height, headerPoints: selected.headerPoints)
            let ocr = try headerOCR(header)
            _ = try currentIdentity(selected, previous: before)
            var report: [String: Any] = ["status": "captured_read_only", "code": "CAPTURE_DIAGNOSTIC_ONLY",
                "pid": selected.pid, "window_id": selected.windowID,
                "official_signature_verified": true, "pre_post_target_identity_matched": true,
                "strict_foreground_requested": selected.waitForeground > 0,
                "target_is_foreground_before": captureBefore.activity.targetIsForeground as Any? ?? NSNull(),
                "target_is_foreground_after": captureAfter.activity.targetIsForeground as Any? ?? NSNull(),
                "frontmost_application_is_target_before": captureBefore.activity.frontmostMatchesTarget as Any? ?? NSNull(),
                "frontmost_application_is_target_after": captureAfter.activity.frontmostMatchesTarget as Any? ?? NSNull(),
                "target_app_active_before": captureBefore.activity.appActive,
                "target_app_active_after": captureAfter.activity.appActive,
                "target_window_is_first_major_before": captureBefore.activity.firstMajorMatchesTarget,
                "target_window_is_first_major_after": captureAfter.activity.firstMajorMatchesTarget,
                "selected_window_in_onscreen_set_before": true,
                "selected_window_in_onscreen_set_after": true,
                "capture_attempted": captureAttempted, "frame_captured": frameCaptured,
                "full_frame_in_memory_only": true,
                "image_width": image.width, "image_height": image.height,
                "header_height_points": selected.headerPoints, "header_height_pixels": header.height,
                "pixel_statistics": statistics, "header_ocr": ocr,
                "header_image_saved": false]
            if let directory = selected.saveDirectory {
                report["header_image_path"] = try saveHeader(header, directory: directory)
                report["header_image_saved"] = true
            }
            emit(report)
        } catch let error as Failure {
            emit(["status": "blocked", "code": error.code,
                  "capture_attempted": captureAttempted, "frame_captured": frameCaptured], status: 78)
        } catch {
            let code = (error as NSError).code
            emit(["status": "blocked", "code": "SYSTEM_DIAGNOSTIC_FAILED", "system_error_code": code,
                  "capture_attempted": captureAttempted, "frame_captured": frameCaptured], status: 78)
        }
    }
}
