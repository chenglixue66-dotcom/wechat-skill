// Read-only WeChat accessibility capability probe. No key, click, paste or send.
import AppKit
import ApplicationServices
import Foundation

func read(_ element: AXUIElement, _ key: String) -> (AnyObject?, Int32) {
    var value: AnyObject?
    let error = AXUIElementCopyAttributeValue(element, key as CFString, &value)
    return (value, error.rawValue)
}
func boolean(_ element: AXUIElement, _ key: String) -> [String: Any] {
    let (value, code) = read(element, key)
    let typed: Bool? = code == 0 && value != nil && CFGetTypeID(value!) == CFBooleanGetTypeID()
        ? (value as? NSNumber)?.boolValue : nil
    return ["status": code, "value": typed as Any? ?? NSNull()]
}
func actions(_ element: AXUIElement) -> [String] {
    var result: CFArray?
    guard AXUIElementCopyActionNames(element, &result) == .success else { return [] }
    return result as? [String] ?? []
}
func pumpWorkspaceUpdates() {
    // NSRunningApplication time-varying properties refresh on the main run loop.
    // Sleeping while polling may otherwise keep a stale active/frontmost state.
    _ = RunLoop.main.run(mode: .default, before: Date(timeIntervalSinceNow: 0.05))
}
func output(_ value: [String: Any], code: Int32) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
    exit(code)
}
// Pure decoder: unavailable or malformed metadata must not become zero windows.
func onScreenWindowCount(_ raw: Any?, targetPID: Int32) -> (status: String, count: Int?) {
    guard let raw else { return ("unavailable", nil) }
    guard let windows = raw as? [[String: Any]] else { return ("invalid_type", nil) }
    var matches = 0
    for window in windows {
        guard let owner = window[kCGWindowOwnerPID as String] as? NSNumber,
              CFGetTypeID(owner) != CFBooleanGetTypeID(), owner.int64Value > 0,
              owner.int64Value <= Int64(Int32.max), owner.doubleValue == Double(owner.int64Value)
        else { return ("invalid_owner_pid", nil) }
        if owner.int32Value == targetPID { matches += 1 }
    }
    return ("available", matches)
}
let args = Array(CommandLine.arguments.dropFirst())
if args.contains("--self-test") {
    guard args == ["--self-test"] else {
        output(["mode": "synthetic_self_test", "code": "INVALID_ARGUMENTS", "ok": false,
            "synthetic_only": true, "ui_reads": 0, "writes_performed": 0, "send_attempts": 0,
            "live_send_permitted": false], code: 78)
    }
    let unavailable = onScreenWindowCount(nil, targetPID: 42)
    let nullValue = onScreenWindowCount(NSNull(), targetPID: 42)
    let wrongType = onScreenWindowCount("synthetic-invalid", targetPID: 42)
    let wrongElement = onScreenWindowCount(["synthetic-invalid"], targetPID: 42)
    let wrongOwner = onScreenWindowCount([[kCGWindowOwnerPID as String: "42"]], targetPID: 42)
    let booleanOwner = onScreenWindowCount([[kCGWindowOwnerPID as String: true]], targetPID: 42)
    let empty = onScreenWindowCount([] as [[String: Any]], targetPID: 42)
    let populated = onScreenWindowCount([[kCGWindowOwnerPID as String: 42],
        [kCGWindowOwnerPID as String: 99], [kCGWindowOwnerPID as String: 42]], targetPID: 42)
    let passed = unavailable.status == "unavailable" && unavailable.count == nil
        && [nullValue, wrongType, wrongElement, wrongOwner, booleanOwner].allSatisfy { $0.count == nil }
        && empty.status == "available" && empty.count == 0
        && populated.status == "available" && populated.count == 2
    output(["mode": "synthetic_self_test", "synthetic_only": true, "ok": passed,
        "check_count": 8, "ui_reads": 0, "writes_performed": 0, "send_attempts": 0,
        "live_send_permitted": false], code: passed ? 0 : 1)
}
if args.contains("--environment-only") {
    guard args == ["--environment-only"] else {
        output(["mode": "environment_only", "code": "INVALID_ARGUMENTS",
            "live_send_permitted": false, "send_preflight_passed": false,
            "ax_calls": 0, "writes_performed": 0, "send_attempts": 0], code: 78)
    }
    // NSWorkspace/WindowServer metadata only, before AX trust or AX objects.
    pumpWorkspaceUpdates()
    let before = NSRunningApplication.runningApplications(withBundleIdentifier: "com.tencent.xinWeChat")
    let selected = before.count == 1 ? before.first : nil
    let selectedPID = selected?.processIdentifier
    let selectedLaunch = selected?.launchDate
    let expectedPath = URL(fileURLWithPath: "/Applications/WeChat.app").resolvingSymlinksInPath()
    let pathMatches = selected?.bundleURL.map { $0.resolvingSymlinksInPath() == expectedPath }
    let frontmostID = NSWorkspace.shared.frontmostApplication?.bundleIdentifier
    let count = onScreenWindowCount(CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID),
                                    targetPID: selectedPID ?? 0)
    pumpWorkspaceUpdates()
    let after = NSRunningApplication.runningApplications(withBundleIdentifier: "com.tencent.xinWeChat")
    let stable: Bool
    if let selectedPID, let selectedLaunch, after.count == 1, let current = after.first {
        stable = !current.isTerminated && current.processIdentifier == selectedPID
            && current.launchDate == selectedLaunch && current.bundleURL?.resolvingSymlinksInPath() == expectedPath
            && pathMatches == true
    } else { stable = false }
    let formatter = ISO8601DateFormatter()
    formatter.timeZone = TimeZone(secondsFromGMT: 0)
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    output(["mode": "environment_only", "code": "ENVIRONMENT_METADATA_ONLY",
        "observed_at_utc": formatter.string(from: Date()), "matching_processes": before.count,
        "pid": selectedPID as Any? ?? NSNull(),
        "process_launch_epoch": selectedLaunch?.timeIntervalSince1970 as Any? ?? NSNull(),
        "official_path": pathMatches as Any? ?? NSNull(), "process_instance_stable": stable,
        "frontmost_is_wechat": frontmostID.map { $0 == "com.tencent.xinWeChat" } as Any? ?? NSNull(),
        "on_screen_windows_status": count.status, "on_screen_windows_available": count.status == "available",
        "on_screen_window_count": stable ? (count.count as Any? ?? NSNull()) : NSNull(),
        "live_send_permitted": false, "send_preflight_passed": false,
        "ax_calls": 0, "writes_performed": 0, "send_attempts": 0], code: 78)
}
if args.contains("--help") || args.contains("-h") {
    print("wxprobe: read-only WeChat capability report; never sends or changes UI")
    exit(0)
}
var foregroundWait: Double = 0
if let index = args.firstIndex(of: "--wait-foreground"), index + 1 < args.count,
   let value = Double(args[index + 1]), value.isFinite, value >= 0, value <= 30 {
    foregroundWait = value
}
var report: [String: Any] = [
    "mode": "read_only_probe", "live_send_permitted": false,
    "writes_performed": 0, "send_attempts": 0,
    "accessibility_trusted": AXIsProcessTrusted(),
    "frontmost_bundle_id": NSWorkspace.shared.frontmostApplication?.bundleIdentifier as Any? ?? NSNull(),
    "recipient_binding": "unavailable", "account_binding": "unavailable"
]
guard AXIsProcessTrusted() else {
    report["code"] = "ACCESSIBILITY_PERMISSION_MISSING"
    output(report, code: 78)
}
let running = NSRunningApplication.runningApplications(withBundleIdentifier: "com.tencent.xinWeChat")
report["matching_processes"] = running.count
guard running.count == 1, let app = running.first else {
    report["code"] = running.isEmpty ? "WECHAT_NOT_RUNNING" : "AMBIGUOUS_PROCESS"
    output(report, code: 78)
}
let expected = URL(fileURLWithPath: "/Applications/WeChat.app").resolvingSymlinksInPath()
guard app.bundleURL?.resolvingSymlinksInPath() == expected, !app.isTerminated,
      let launchDate = app.launchDate else {
    report["code"] = "UNEXPECTED_APP_PATH"
    output(report, code: 78)
}
let verify = Process()
verify.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
verify.arguments = ["--verify", "--deep", "--strict", "-R",
    "=anchor apple generic and identifier \"com.tencent.xinWeChat\"",
    expected.path]
verify.standardOutput = FileHandle.nullDevice
verify.standardError = FileHandle.nullDevice
do { try verify.run(); verify.waitUntilExit() } catch {
    report["code"] = "SIGNATURE_CHECK_UNAVAILABLE"; output(report, code: 78)
}
guard verify.terminationStatus == 0 else {
    report["official_signature_verified"] = false
    report["code"] = "OFFICIAL_SIGNATURE_NOT_VERIFIED"; output(report, code: 78)
}
// App Store signatures use an Apple leaf certificate; verify the signed Team ID
// rather than assuming a Developer ID certificate's subject.OU layout.
let details = Process(), detailPipe = Pipe()
details.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
details.arguments = ["--display", "--verbose=2", expected.path]
details.standardOutput = FileHandle.nullDevice
details.standardError = detailPipe
do { try details.run() } catch {
    report["code"] = "SIGNATURE_METADATA_UNAVAILABLE"; output(report, code: 78)
}
let detailData = detailPipe.fileHandleForReading.readDataToEndOfFile()
details.waitUntilExit()
let signedDetails = String(data: detailData, encoding: .utf8) ?? ""
let teamMatches = signedDetails.split(separator: "\n").contains("TeamIdentifier=5A4RE8SF68")
report["official_signature_verified"] = details.terminationStatus == 0 && teamMatches
guard details.terminationStatus == 0 && teamMatches else {
    report["code"] = "OFFICIAL_TEAM_NOT_VERIFIED"; output(report, code: 78)
}
report["pid"] = app.processIdentifier
report["process_launch_epoch"] = launchDate.timeIntervalSince1970
func sameProcessInstance() -> Bool {
    let current = NSRunningApplication.runningApplications(withBundleIdentifier: "com.tencent.xinWeChat")
    guard current.count == 1, let candidate = current.first else { return false }
    return !candidate.isTerminated && candidate.processIdentifier == app.processIdentifier
        && candidate.launchDate == launchDate && candidate.bundleURL?.resolvingSymlinksInPath() == expected
}
let waitStarted = Date()
if foregroundWait > 0 {
    let readiness = "{\"event\":\"READY_FOR_FOREGROUND\",\"wait_seconds\":\(Int(foregroundWait)),\"send_attempts\":0}\n"
    FileHandle.standardError.write(readiness.data(using: .utf8)!)
}
pumpWorkspaceUpdates()
var stableForegroundSince: Date?
while foregroundWait > 0 && Date().timeIntervalSince(waitStarted) < foregroundWait {
    pumpWorkspaceUpdates()
    if NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier {
        if stableForegroundSince == nil { stableForegroundSince = Date() }
        if Date().timeIntervalSince(stableForegroundSince!) >= 0.7 { break }
    } else { stableForegroundSince = nil }
}
let foregroundBeforeAX = NSWorkspace.shared.frontmostApplication?.processIdentifier
report["process_instance_before_ax_matches"] = sameProcessInstance()
guard sameProcessInstance() else {
    report["code"] = "APP_INSTANCE_CHANGED"; output(report, code: 78)
}
report["frontmost_before_ax_is_wechat"] = foregroundBeforeAX == app.processIdentifier
report["app_active"] = app.isActive
let ax = AXUIElementCreateApplication(app.processIdentifier)
AXUIElementSetMessagingTimeout(ax, 1.0)
let (rawWindows, windowStatus) = read(ax, kAXWindowsAttribute)
report["windows_read_status"] = windowStatus
guard windowStatus == 0, let windows = rawWindows as? [AXUIElement] else {
    report["code"] = "WINDOWS_UNREADABLE"; output(report, code: 78)
}
let cgWindowCount = onScreenWindowCount(
    CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID), targetPID: app.processIdentifier)
report["on_screen_windows_status"] = cgWindowCount.status
report["on_screen_windows_available"] = cgWindowCount.status == "available"
report["on_screen_window_count"] = cgWindowCount.count as Any? ?? NSNull()
var windowReports: [[String: Any]] = []
for (index, window) in windows.enumerated() {
    var counts: [String: Int] = [:]
    var nodeCount = 0, readFailures = 0, searchCount = 0, editorCount = 0, sendCount = 0
    var truncated = false
    func walk(_ element: AXUIElement, depth: Int) {
        if depth > 40 || nodeCount >= 5000 { truncated = true; return }
        nodeCount += 1
        let (rawRole, roleStatus) = read(element, kAXRoleAttribute)
        if roleStatus != 0 { readFailures += 1; return }
        let role = rawRole as? String ?? "unknown"
        counts[role, default: 0] += 1
        // Do not read labels of static chat-message nodes just to count roles.
        let classifiedControl = ["AXTextArea", "AXTextField", "AXSearchField", "AXComboBox", "AXButton"].contains(role)
        let title = classifiedControl ? ((read(element, kAXTitleAttribute).0 as? String) ?? "") : ""
        let description = classifiedControl ? ((read(element, kAXDescriptionAttribute).0 as? String) ?? "") : ""
        let labels = [title, description].filter { !$0.isEmpty }
        if ["AXTextArea", "AXTextField", "AXSearchField", "AXComboBox"].contains(role) {
            editorCount += 1
            if labels.contains(where: { ["Search", "搜索"].contains($0) }) { searchCount += 1 }
        }
        if role == "AXButton", labels.contains(where: { ["Send", "发送"].contains($0) }), actions(element).contains(kAXPressAction) {
            sendCount += 1
        }
        let (children, status) = read(element, kAXChildrenAttribute)
        if status == 0 {
            for child in children as? [AXUIElement] ?? [] { walk(child, depth: depth + 1) }
        } else if ![AXError.attributeUnsupported.rawValue, AXError.noValue.rawValue].contains(status) {
            readFailures += 1
        }
    }
    walk(window, depth: 0)
    windowReports.append([
        "window_index": index, "minimized": boolean(window, kAXMinimizedAttribute),
        "main": boolean(window, kAXMainAttribute), "focused": boolean(window, kAXFocusedAttribute),
        "nodes": nodeCount, "roles": counts, "read_failures": readFailures,
        "editable_controls": editorCount, "search_controls": searchCount,
        "object_send_action_candidates": sendCount, "truncated": truncated
    ])
}
report["windows"] = windowReports
pumpWorkspaceUpdates()
let foregroundAfterAX = NSWorkspace.shared.frontmostApplication?.processIdentifier
report["frontmost_after_ax_is_wechat"] = foregroundAfterAX == app.processIdentifier
report["foreground_consistent_during_ax_read"] = foregroundBeforeAX == app.processIdentifier && foregroundAfterAX == app.processIdentifier
let instanceAfterAX = sameProcessInstance()
report["process_instance_after_ax_matches"] = instanceAfterAX
guard instanceAfterAX else {
    report["code"] = "APP_INSTANCE_CHANGED"; output(report, code: 78)
}
report["code"] = "RECIPIENT_UNVERIFIABLE"
report["reason"] = "No validated account/recipient/composer/send-object binding; live writes are disabled."
output(report, code: 78)
