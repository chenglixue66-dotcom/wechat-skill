// Local JSONL AX bridge. Native semantic navigation, object-bound draft writes
// and AXPress submission require explicit capabilities and live identity evidence.
// No global keys, coordinates, clipboard, screenshots, network or system settings.
import AppKit
import ApplicationServices
import CoreGraphics
import CryptoKit
import Darwin
import Foundation
import Security

private let officialBundle = "/Applications/WeChat.app"
private let officialID = "com.tencent.xinWeChat"
private let maximumLine = 1_048_576
private struct Failure: Error { let code: String }
private func fail(_ code: String) throws -> Never { throw Failure(code: code) }
private func digest(_ text: String) -> String { SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined() }
private func byteEqual(_ left: String, _ right: String) -> Bool { Data(left.utf8) == Data(right.utf8) }
private func null(_ value: Any?) -> Any { value ?? NSNull() }
private func canonical(_ value: Any) -> Data? { try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) }
private func read(_ element: AXUIElement, _ key: String) -> (AnyObject?, AXError) {
    var result: AnyObject?
    let code = AXUIElementCopyAttributeValue(element, key as CFString, &result)
    return (result, code)
}
private func text(_ element: AXUIElement, _ key: String) -> String? {
    let (value, code) = read(element, key)
    return code == .success ? value as? String : nil
}
private func strictBoolean(_ value: Any?) -> Bool? {
    guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
    return number.boolValue
}
private func decodedPressSupport(_ raw: Any?, status: AXError) -> Bool? {
    guard status == .success, let raw, CFGetTypeID(raw as AnyObject) == CFArrayGetTypeID(),
          let elements = raw as? [Any] else { return nil }
    var names: [String] = []
    for element in elements {
        guard CFGetTypeID(element as AnyObject) == CFStringGetTypeID(),
              let name = element as? String else { return nil }
        names.append(name)
    }
    return names.contains(kAXPressAction)
}
private func boolean(_ element: AXUIElement, _ key: String) -> Bool? {
    let (value, code) = read(element, key)
    return code == .success ? strictBoolean(value) : nil
}
private func elementValue(_ value: AnyObject?, _ status: AXError) -> AXUIElement? {
    guard status == .success, let value, CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
    return (value as! AXUIElement)
}
private func descendants(_ root: AXUIElement, errors: inout Set<String>) -> [AXUIElement] {
    var result: [AXUIElement] = [], seen: [CFHashCode: [AXUIElement]] = [:]
    func walk(_ element: AXUIElement, depth: Int) {
        guard result.count < 3000, depth <= 40 else { errors.insert("AX_TREE_INCOMPLETE"); return }
        let hash = CFHash(element)
        if (seen[hash] ?? []).contains(where: { CFEqual($0, element) }) { return }
        seen[hash, default: []].append(element); result.append(element)
        for attribute in [kAXChildrenAttribute, "AXChildrenInNavigationOrder", "AXVisibleChildren", "AXContents", "AXProxy"] {
            let (value, status) = read(element, attribute)
            if status == .success {
                if let children = value as? [AXUIElement] {
                    for child in children { walk(child, depth: depth + 1) }
                } else if let child = elementValue(value, status) {
                    walk(child, depth: depth + 1)
                }
            } else if attribute == kAXChildrenAttribute && status != .attributeUnsupported && status != .noValue {
                errors.insert("AX_CHILDREN_UNREADABLE")
            }
        }
    }
    walk(root, depth: 0)
    return result
}
private func parentChain(_ element: AXUIElement) -> [AXUIElement] {
    var chain: [AXUIElement] = [], next: AXUIElement? = element
    for _ in 0..<40 {
        guard let current = next, !chain.contains(where: { CFEqual($0, current) }) else { break }
        chain.append(current)
        let (parent, status) = read(current, kAXParentAttribute)
        next = elementValue(parent, status)
    }
    return chain
}
private func belongs(_ element: AXUIElement, to window: AXUIElement, pid: pid_t) -> Bool {
    var owner: pid_t = 0
    return AXUIElementGetPid(element, &owner) == .success && owner == pid
        && parentChain(element).contains(where: { CFEqual($0, window) })
}
private func geometry(_ window: AXUIElement) -> CGRect? {
    let (position, positionStatus) = read(window, kAXPositionAttribute)
    let (size, sizeStatus) = read(window, kAXSizeAttribute)
    guard positionStatus == .success, sizeStatus == .success, let position, let size,
          CFGetTypeID(position) == AXValueGetTypeID(), CFGetTypeID(size) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero, dimensions = CGSize.zero
    guard AXValueGetValue(position as! AXValue, .cgPoint, &point),
          AXValueGetValue(size as! AXValue, .cgSize, &dimensions) else { return nil }
    return CGRect(origin: point, size: dimensions)
}
private func windowID(_ window: AXUIElement, pid: pid_t) -> (String, CGRect)? {
    guard let rectangle = geometry(window) else { return nil }
    let entries = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
    let matches = entries.filter { item in
        guard (item[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid,
              (item[kCGWindowLayer as String] as? NSNumber)?.intValue == 0,
              ((item[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 0) > 0,
              let values = item[kCGWindowBounds as String] as? [String: Any],
              let candidate = CGRect(dictionaryRepresentation: values as CFDictionary) else { return false }
        return abs(candidate.minX - rectangle.minX) <= 1 && abs(candidate.minY - rectangle.minY) <= 1
            && abs(candidate.width - rectangle.width) <= 1 && abs(candidate.height - rectangle.height) <= 1
    }
    guard matches.count == 1, let number = matches[0][kCGWindowNumber as String] as? NSNumber else { return nil }
    return (number.stringValue, rectangle)
}

// These pure predicates are shared by real AX selection and synthetic
// regressions. A left sidebar label must never identify a right chat composer.
private func isLocalChatHeader(pane: CGRect, composer: CGRect, candidate: CGRect,
                               role: String, ancestorRoles: [String]) -> Bool {
    guard ["AXButton", "AXStaticText", "AXHeading", "AXImage"].contains(role),
          !ancestorRoles.contains(where: { ["AXRow", "AXCell", "AXList", "AXTable", "AXOutline", "AXSearchField"].contains($0) }),
          pane.width > 100, pane.height > composer.height,
          pane.insetBy(dx: -1, dy: -1).contains(composer) else { return false }
    let tolerance = min(32, max(12, composer.width * 0.06))
    // A whole-window group spanning the sidebar is not a local chat pane.
    guard pane.minX >= composer.minX - tolerance,
          pane.maxX <= composer.maxX + tolerance,
          candidate.minX >= composer.minX - tolerance,
          candidate.maxX <= composer.maxX + tolerance,
          candidate.minY >= pane.minY - 1,
          candidate.maxY <= composer.minY,
          candidate.minY < pane.minY + min(120, max(40, pane.height * 0.25)) else { return false }
    return true
}
private func recipientMarker(identifier: String?, fields: [String], rawIDs: [String]) -> String? {
    // The raw profile ID was independently matched first. This does not infer
    // an ID from an arbitrary head_view suffix: only exact supported encodings
    // of that already verified ID, or an exact raw header field, qualify.
    let prefixes = ["", "head_view_", "chat_header_", "chat_avatar_", "conversation_", "contact_profile_", "recipient_"]
    if let identifier, rawIDs.contains(where: { raw in prefixes.contains(where: { byteEqual(identifier, $0 + raw) }) }) {
        return "AXIdentifier:" + identifier
    }
    if let raw = fields.first(where: { value in rawIDs.contains(where: { byteEqual($0, value) }) }) {
        return "raw_header_field:" + raw
    }
    return nil
}

private struct ProcessIdentity {
    let pid: pid_t, launch: String
    var epoch: String { "\(pid):\(launch)" }
}
private func processIdentity() throws -> ProcessIdentity {
    _ = RunLoop.main.run(mode: .default, before: Date(timeIntervalSinceNow: 0.01))
    let applications = NSRunningApplication.runningApplications(withBundleIdentifier: officialID)
    guard applications.count == 1, let app = applications.first, !app.isTerminated,
          app.bundleURL?.resolvingSymlinksInPath().path == officialBundle,
          let launch = app.launchDate else { try fail("APP_INSTANCE_UNAVAILABLE") }
    return ProcessIdentity(pid: app.processIdentifier, launch: String(format: "%.6f", launch.timeIntervalSince1970))
}
private func verifySignature() throws {
    // Validate the actual running code against Apple's dynamic-code API. A
    // recursive disk scan on every observation makes a short-lived binding
    // impossible for the 1.5 GB bundle and does not identify the running guest.
    let process = try processIdentity()
    let attributes = [kSecGuestAttributePid as String: NSNumber(value: process.pid)] as CFDictionary
    var guest: SecCode?
    guard SecCodeCopyGuestWithAttributes(nil, attributes, SecCSFlags(), &guest) == errSecSuccess,
          let guest else { try fail("OFFICIAL_RUNNING_CODE_UNAVAILABLE") }
    var requirement: SecRequirement?
    let expression = "anchor apple generic and identifier \"\(officialID)\" and certificate leaf[subject.OU] = \"5A4RE8SF68\""
    guard SecRequirementCreateWithString(expression as CFString, SecCSFlags(), &requirement) == errSecSuccess,
          let requirement else { try fail("OFFICIAL_REQUIREMENT_UNAVAILABLE") }
    guard SecCodeCheckValidity(guest, SecCSFlags(), requirement) == errSecSuccess else {
        try fail("OFFICIAL_SIGNATURE_NOT_VERIFIED")
    }
    guard try processIdentity().epoch == process.epoch else { try fail("APP_INSTANCE_CHANGED") }
}

private struct Scope {
    let process: ProcessIdentity, app: AXUIElement, window: AXUIElement
    let windowID: String, frame: CGRect
    let composer: AXUIElement?, send: AXUIElement?
    let composerCount: Int, sendCount: Int
    let focusedParents: [AXUIElement]
}
private struct VerifiedIdentity {
    let accountID: String, recipientID: String, recipientType: String
    let catalogToken: String, profileScopeToken: String
    let sameWindowVerified: Bool
}

private struct Handle {
    let element: AXUIElement, scope: String, kind: String
}
private struct Observed {
    let document: [String: Any], scope: Scope?, binding: [String: Any]?
}
private func typedStringField(_ element: AXUIElement, _ key: String, keep: (String) -> Bool = { _ in true }) -> [String: Any] {
    let (value, status) = read(element, key)
    let isString = value.map { CFGetTypeID($0) == CFStringGetTypeID() } ?? false
    let string = isString ? value as? String : nil
    return ["status": status.rawValue, "type": isString ? "CFString" : "unknown",
            "value": string.flatMap { keep($0) ? $0 : nil } as Any? ?? NSNull()]
}

private func safeDraftDocument(_ document: [String: Any], bindingAvailable: Bool) -> Bool {
    guard bindingAvailable, (document["read_errors"] as? [String])?.isEmpty == true else { return false }
    for name in ["identity_verified", "on_screen", "focused", "composer_focused", "composer_editable"] {
        guard let value = document[name] as? NSNumber, CFGetTypeID(value) == CFBooleanGetTypeID(), value.boolValue else { return false }
    }
    for name in ["window_minimized", "modal"] {
        guard let value = document[name] as? NSNumber, CFGetTypeID(value) == CFBooleanGetTypeID(), !value.boolValue else { return false }
    }
    guard document["locked"] is NSNull || strictBoolean(document["locked"]) == false else { return false }
    for name in ["account_matches", "recipient_matches", "composer_matches", "send_matches"] {
        guard let value = document[name] as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(), value.intValue == 1 else { return false }
    }
    for name in ["send_enabled", "press_supported"] {
        guard let value = document[name] as? NSNumber, CFGetTypeID(value) == CFBooleanGetTypeID() else { return false }
    }
    return true
}

private final class Bridge {
    let draftWritesEnabled: Bool
    let liveSendEnabled: Bool
    private var epoch: String?, scopeEpoch: String?
    private var handles: [String: Handle] = [:]
    private var accessibilityOverrides: [(ProcessIdentity, String, Bool)] = []
    init(draftWritesEnabled: Bool, liveSendEnabled: Bool = false) {
        self.draftWritesEnabled = draftWritesEnabled; self.liveSendEnabled = liveSendEnabled
    }

    private func token(_ element: AXUIElement, scope: String, kind: String) -> String {
        for (key, handle) in handles where handle.scope == scope && handle.kind == kind && CFEqual(handle.element, element) { return key }
        let identifier = UUID().uuidString
        handles[identifier] = Handle(element: element, scope: scope, kind: kind)
        return identifier
    }
    private struct Navigation {
        let process: ProcessIdentity, accountID: String, recipientID: String, kind: String
        let catalogToken: String, sourceToken: String, profileToken: String
        let window: AXUIElement, composer: AXUIElement, send: AXUIElement
        let pane: AXUIElement, headerValue: String, markerFingerprint: String
        let displayNames: [String], rawRecipientIDs: [String]
        let expiresAt: TimeInterval
    }
    private var navigation: Navigation?
    private var submittedOperations = Set<String>()
    private func exactStrings(_ element: AXUIElement) -> [String] {
        [kAXTitleAttribute, kAXValueAttribute, kAXDescriptionAttribute].compactMap { text(element, $0) }
    }
    private func nodes(_ root: AXUIElement) throws -> [AXUIElement] {
        var errors = Set<String>()
        let result = descendants(root, errors: &errors)
        guard errors.isEmpty else { try fail("NAVIGATION_TREE_UNREADABLE") }
        return result
    }
    private func windows(_ app: AXUIElement) throws -> [AXUIElement] {
        let (raw, code) = read(app, kAXWindowsAttribute)
        guard code == .success, let result = raw as? [AXUIElement] else { try fail("AX_WINDOWS_UNREADABLE") }
        return result
    }
    private func press(_ element: AXUIElement, process: ProcessIdentity) throws {
        guard try processIdentity().epoch == process.epoch else { try fail("APP_INSTANCE_CHANGED") }
        guard boolean(element, kAXEnabledAttribute) != false else { try fail("NAVIGATION_ACTION_DISABLED") }
        var names: CFArray?
        guard AXUIElementCopyActionNames(element, &names) == .success,
              (names as? [String] ?? []).contains(kAXPressAction) else { try fail("NAVIGATION_ACTION_UNAVAILABLE") }
        guard AXUIElementPerformAction(element, kAXPressAction as CFString) == .success else { try fail("NAVIGATION_ACTION_FAILED") }
        _ = RunLoop.main.run(mode: .default, before: Date(timeIntervalSinceNow: 0.12))
    }
    private func wait<T>(_ deadline: TimeInterval, failureCode: String = "NAVIGATION_TIMEOUT", sample: () throws -> T?) throws -> T {
        repeat {
            if let result = try sample() { return result }
            _ = RunLoop.main.run(mode: .default, before: Date(timeIntervalSinceNow: 0.12))
        } while ProcessInfo.processInfo.systemUptime < deadline
        try fail(failureCode)
    }
    private func selectComposer(_ elements: [AXUIElement], frame: CGRect) -> [AXUIElement] {
        let identifiers = ["chat_input_field", "chat_input", "message_input", "chat_input_editor"]
        let marked = elements.filter { ["AXTextArea", "AXTextField"].contains(text($0, kAXRoleAttribute) ?? "") && identifiers.contains(text($0, "AXIdentifier") ?? "") }
        if !marked.isEmpty { return marked }
        return elements.filter { element in
            guard text(element, kAXRoleAttribute) == "AXTextArea",
                  !["AXSearchField", "AXSecureTextField"].contains(text(element, kAXSubroleAttribute) ?? ""),
                  !(text(element, "AXIdentifier") ?? "").lowercased().contains("search"),
                  let rect = geometry(element), frame.contains(rect), rect.midY >= frame.midY else { return false }
            return true
        }
    }
    private func selectSend(_ elements: [AXUIElement]) -> [AXUIElement] {
        elements.filter { element in text(element, kAXRoleAttribute) == "AXButton" && exactStrings(element).contains(where: { ["Send", "发送"].contains($0) }) }
    }
    private struct ChatContext {
        let pane: AXUIElement, header: AXUIElement
        let headerValue: String, markerFingerprint: String
    }
    private func chatContext(window: AXUIElement, composer: AXUIElement, send: AXUIElement,
                             displays: [String], rawIDs: [String], pid: pid_t) -> ChatContext? {
        guard let composerFrame = geometry(composer) else { return nil }
        for pane in parentChain(composer).dropFirst() {
            if CFEqual(pane, window) { break }
            guard ["AXGroup", "AXSplitGroup", "AXScrollArea", "AXLayoutArea", "AXPane"].contains(text(pane, kAXRoleAttribute) ?? ""),
                  let paneFrame = geometry(pane), belongs(send, to: pane, pid: pid),
                  let elements = try? nodes(pane) else { continue }
            let composers = selectComposer(elements, frame: paneFrame)
            guard composers.count == 1, CFEqual(composers[0], composer) else { continue }
            func local(_ element: AXUIElement) -> Bool {
                guard let rect = geometry(element), let role = text(element, kAXRoleAttribute) else { return false }
                let path = parentChain(element).prefix(while: { !CFEqual($0, pane) })
                return isLocalChatHeader(pane: paneFrame, composer: composerFrame, candidate: rect, role: role,
                    ancestorRoles: path.compactMap { text($0, kAXRoleAttribute) })
            }
            let rawHeaders = elements.filter { element in
                guard ["AXButton", "AXStaticText", "AXHeading"].contains(text(element, kAXRoleAttribute) ?? ""), local(element) else { return false }
                return exactStrings(element).contains(where: { value in displays.contains(where: { byteEqual($0, value) }) })
            }
            let preferredIDs = ["chat_header", "chat_title", "chat_name", "chat_title_label", "toolbar_title", "conversation_title"]
            let preferred = rawHeaders.filter { preferredIDs.contains(text($0, "AXIdentifier") ?? "") }
            let candidates = preferred.isEmpty ? rawHeaders : preferred
            let headers = candidates.filter { element in
                !parentChain(element).dropFirst().contains { parent in candidates.contains(where: { CFEqual($0, parent) }) }
            }
            guard headers.count == 1,
                  let value = exactStrings(headers[0]).first(where: { value in displays.contains(where: { byteEqual($0, value) }) }) else { continue }
            guard let headerFrame = geometry(headers[0]) else { continue }
            let markers = Set(elements.filter { element in
                guard local(element), let rect = geometry(element) else { return false }
                // Recipient evidence must be on this header row, never a
                // message bubble or avatar in the history below the header.
                return rect.minY >= headerFrame.minY - 16 && rect.maxY <= headerFrame.maxY + 24
            }.compactMap { element -> String? in
                let identifier = text(element, "AXIdentifier")
                let rawFields = ["recipient_id", "chat_recipient_id", "conversation_recipient_id", "weixin_id"].contains(identifier ?? "") ? exactStrings(element) : []
                return recipientMarker(identifier: identifier, fields: rawFields, rawIDs: rawIDs)
            })
            let explicitMarkers = markers.filter { $0.hasPrefix("AXIdentifier:") }
            let selectedMarkers = explicitMarkers.isEmpty ? markers : Set(explicitMarkers)
            guard selectedMarkers.count == 1, let marker = selectedMarkers.first else { continue }
            return ChatContext(pane: pane, header: headers[0], headerValue: value, markerFingerprint: digest(marker))
        }
        return nil
    }
    private func resolveIdentity(_ scope: Scope) -> VerifiedIdentity? {
        guard let proof = navigation, ProcessInfo.processInfo.systemUptime < proof.expiresAt,
              proof.process.epoch == scope.process.epoch, CFEqual(proof.window, scope.window),
              let composer = scope.composer, CFEqual(proof.composer, composer),
              let send = scope.send, CFEqual(proof.send, send),
              let current = chatContext(window: scope.window, composer: composer, send: send,
                  displays: proof.displayNames, rawIDs: proof.rawRecipientIDs, pid: scope.process.pid),
              CFEqual(current.pane, proof.pane), byteEqual(current.headerValue, proof.headerValue),
              byteEqual(current.markerFingerprint, proof.markerFingerprint) else { return nil }
        return VerifiedIdentity(accountID: proof.accountID, recipientID: proof.recipientID, recipientType: proof.kind,
            catalogToken: proof.catalogToken, profileScopeToken: proof.profileToken, sameWindowVerified: true)
    }
    func openChat(_ arguments: [String: Any]) throws -> [String: Any] {
        navigation = nil
        guard let account = arguments["account_id"] as? String, !account.isEmpty,
              let recipient = arguments["recipient_id"] as? String, !recipient.isEmpty,
              let kind = arguments["kind"] as? String, kind == "person",
              let rawIDs = arguments["recipient_raw_ids"] as? [String], !rawIDs.isEmpty,
              let displays = arguments["display_names"] as? [String], !displays.isEmpty,
              let searches = arguments["search_terms"] as? [String], !searches.isEmpty,
              let accountFields = arguments["account_fields"] as? [String], !accountFields.isEmpty,
              let catalog = arguments["catalog_token"] as? String, !catalog.isEmpty,
              let source = arguments["source_token"] as? String, !source.isEmpty,
              let timeout = arguments["timeout"] as? Double, (1...30).contains(timeout) else { try fail("INVALID_NAVIGATION_ARGUMENTS") }
        let process = try processIdentity()
        try verifySignature()
        guard AXIsProcessTrusted(), let running = NSRunningApplication(processIdentifier: process.pid) else { try fail("AX_PERMISSION_MISSING") }
        running.activate(options: [])
        let app = AXUIElementCreateApplication(process.pid)
        AXUIElementSetMessagingTimeout(app, 0.3)
        let deadline = ProcessInfo.processInfo.systemUptime + timeout
        // Open Settings via the application's own menu, then locate the account
        // row semantically. No nickname splitting, fixed child index or count.
        // Restoring an app can focus its article or utility window. Select
        // Chats through the application's own menu before opening Settings.
        let (initialMenuRaw, initialMenuCode) = read(app, kAXMenuBarAttribute)
        guard let initialMenu = elementValue(initialMenuRaw, initialMenuCode) else { try fail("ACCOUNT_MENU_UNAVAILABLE") }
        let initialMenuNodes = try nodes(initialMenu)
        let chatsItems = initialMenuNodes.filter { text($0, kAXRoleAttribute) == "AXMenuItem" && ["Chats", "聊天"].contains(text($0, kAXTitleAttribute) ?? "") }
        if chatsItems.count == 1 {
            try press(chatsItems[0], process: process)
        } else {
            let mainCandidates = try windows(app).filter { ["Weixin", "微信", "WeChat"].contains(text($0, kAXTitleAttribute) ?? "") }
            guard mainCandidates.count == 1 else { try fail("MAIN_CHAT_WINDOW_UNAVAILABLE") }
            guard AXUIElementPerformAction(mainCandidates[0], kAXRaiseAction as CFString) == .success else { try fail("MAIN_CHAT_WINDOW_UNAVAILABLE") }
        }
        let (rawMenu, menuCode) = read(app, kAXMenuBarAttribute)
        guard let menu = elementValue(rawMenu, menuCode) else { try fail("ACCOUNT_MENU_UNAVAILABLE") }
        let menuNodes = try nodes(menu)
        let settingsItems = menuNodes.filter { ["Settings", "Settings…", "Settings...", "设置", "设置…", "偏好设置…"].contains(text($0, kAXTitleAttribute) ?? "") && text($0, kAXRoleAttribute) == "AXMenuItem" }
        guard settingsItems.count == 1 else { try fail("ACCOUNT_MENU_UNAVAILABLE") }
        try press(settingsItems[0], process: process)
        let settings: AXUIElement = try wait(deadline, failureCode: "SETTINGS_WINDOW_UNAVAILABLE") {
            let candidates = try windows(app).filter { ["Settings", "设置", "Preferences", "偏好设置"].contains(text($0, kAXTitleAttribute) ?? "") }
            return candidates.count == 1 ? candidates[0] : nil
        }
        let settingsNodes = try nodes(settings)
        let accountTabs = settingsNodes.filter { element in ["AXButton", "AXRadioButton"].contains(text(element, kAXRoleAttribute) ?? "") && exactStrings(element).contains(where: { ["My Account", "Account", "我的账号", "账号设置", "帐号设置"].contains($0) }) }
        if accountTabs.count == 1 { try press(accountTabs[0], process: process) }
        let accountEvidence: AXUIElement = try wait(deadline, failureCode: "ACCOUNT_FIELD_UNAVAILABLE") {
            let elements = try nodes(settings)
            let logouts = elements.filter { exactStrings($0).contains(where: { ["Log Out", "退出登录"].contains($0) }) }
            guard !logouts.isEmpty else { return nil }
            let fields = elements.filter { element in
                guard ["AXTextField", "AXStaticText"].contains(text(element, kAXRoleAttribute) ?? ""),
                      exactStrings(element).contains(where: { value in accountFields.contains(where: { byteEqual($0, value) }) }) else { return false }
                let chain = parentChain(element)
                return chain.contains { root in
                    guard let children = try? nodes(root), children.count <= 100 else { return false }
                    return children.contains(where: { exactStrings($0).contains(where: { ["Account", "账号", "帐号", "微信号", "微信号:"].contains($0) }) })
                        && children.contains(where: { exactStrings($0).contains(where: { ["Log Out", "退出登录"].contains($0) }) })
                }
            }
            return fields.count == 1 ? fields[0] : nil
        }
        guard exactStrings(accountEvidence).contains(where: { value in accountFields.contains(where: { byteEqual($0, value) }) }) else { try fail("ACCOUNT_GUI_MISMATCH") }
        let (rawClose, closeCode) = read(settings, kAXCloseButtonAttribute)
        guard let close = elementValue(rawClose, closeCode) else { try fail("ACCOUNT_WINDOW_CANNOT_CLOSE") }
        try press(close, process: process)
        let search: AXUIElement = try wait(deadline, failureCode: "SEARCH_CONTROL_UNAVAILABLE") {
            var candidates: [AXUIElement] = []
            for window in try windows(app) {
                for element in try nodes(window) where ["AXTextField", "AXSearchField"].contains(text(element, kAXRoleAttribute) ?? "") {
                    let identifier = (text(element, "AXIdentifier") ?? "").lowercased()
                    if text(element, kAXSubroleAttribute) == "AXSearchField" || identifier.contains("search") || exactStrings(element).contains(where: { ["Search", "搜索"].contains($0) }) { candidates.append(element) }
                }
            }
            return candidates.count == 1 ? candidates[0] : nil
        }
        guard AXUIElementSetAttributeValue(search, kAXFocusedAttribute as CFString, kCFBooleanTrue) == .success else { try fail("SEARCH_FOCUS_FAILED") }
        guard AXUIElementSetAttributeValue(search, kAXValueAttribute as CFString, searches[0] as CFString) == .success else { try fail("SEARCH_WRITE_FAILED") }
        let result: AXUIElement = try wait(deadline, failureCode: "SEARCH_RESULT_UNAVAILABLE_OR_AMBIGUOUS") {
            var candidates: [AXUIElement] = []
            for window in try windows(app) {
                for element in try nodes(window) where ["AXRow", "AXCell", "AXButton", "AXStaticText"].contains(text(element, kAXRoleAttribute) ?? "") && exactStrings(element).contains(where: { displays.contains($0) || rawIDs.contains($0) }) {
                    let target = parentChain(element).first { ["AXRow", "AXCell", "AXButton"].contains(text($0, kAXRoleAttribute) ?? "") } ?? element
                    if !candidates.contains(where: { CFEqual($0, target) }) { candidates.append(target) }
                }
            }
            return candidates.count == 1 ? candidates[0] : nil
        }
        try press(result, process: process)
        // Search can either open a profile or a chat. In the latter case open
        // its unique top header avatar; the raw profile field must still match.
        func currentProfile() -> (AXUIElement, String)? {
            guard let observed = try? observe(), let scope = observed.scope else { return nil }
            let profile = recipientProfilePair(scope, key: "navigation:" + process.epoch, composerToken: nil, sendToken: nil)
            guard let first = profile["first"] as? [String: Any],
                  let second = profile["second"] as? [String: Any], canonical(first) == canonical(second), let children = first["children"] as? [[String: Any]],
                  let rootToken = first["root_token"] as? String, let root = handles[rootToken]?.element else { return nil }
            let labels = children.indices.filter { ["Weixin ID:", "WeChat ID:", "微信号:", "微信号："].contains((children[$0]["value"] as? [String: Any])?["value"] as? String ?? "") }
            guard labels.count == 1, labels[0]+1 < children.count, let raw = (children[labels[0]+1]["value"] as? [String: Any])?["value"] as? String else { return nil }
            return (root, raw)
        }
        if currentProfile() == nil {
            var markedHeaders: [AXUIElement] = [], semanticHeaders: [AXUIElement] = []
            for window in try windows(app) {
                guard let frame = geometry(window) else { continue }
                let elements = try nodes(window)
                guard selectComposer(elements, frame: frame).count == 1 else { continue }
                for element in elements {
                    guard let rect = geometry(element), rect.minY < frame.minY+120 else { continue }
                    let identifier = text(element, "AXIdentifier") ?? ""
                    if identifier.hasPrefix("head_view_") { markedHeaders.append(element); continue }
                    guard ["AXButton", "AXImage"].contains(text(element, kAXRoleAttribute) ?? "") else { continue }
                    let lower = identifier.lowercased()
                    if lower.contains("avatar") || lower.contains("contact_profile") || lower.contains("profile_button")
                        || exactStrings(element).contains(where: { ["Contact Info", "Profile", "个人资料", "联系人资料", "详细资料"].contains($0) || displays.contains($0) }) {
                        semanticHeaders.append(element)
                    }
                }
            }
            let headers = markedHeaders.isEmpty ? semanticHeaders : markedHeaders
            guard headers.count == 1 else { try fail("RECIPIENT_PROFILE_TRIGGER_UNAVAILABLE") }
            try press(headers[0], process: process)
        }
        let profile: (AXUIElement, String) = try wait(deadline, failureCode: "RECIPIENT_PROFILE_UNAVAILABLE") { currentProfile() }
        guard rawIDs.contains(where: { byteEqual($0, profile.1) }) else { try fail("RECIPIENT_GUI_MISMATCH") }
        let profileNodes = try nodes(profile.0)
        let markedMessageButtons = profileNodes.filter { text($0, "AXIdentifier") == "chat_img_button" }
        let semanticMessageButtons = profileNodes.filter { text($0, kAXRoleAttribute) == "AXButton" && exactStrings($0).contains(where: { ["Message", "Send Message", "发消息", "发送消息"].contains($0) }) }
        let messageButtons = markedMessageButtons.isEmpty ? semanticMessageButtons : markedMessageButtons
        guard messageButtons.count == 1 else { try fail("PROFILE_MESSAGE_ACTION_UNAVAILABLE") }
        let profileToken = digest(process.epoch + ":" + profile.1 + ":" + String(CFHash(profile.0)))
        try press(messageButtons[0], process: process)
        let selected: (AXUIElement, AXUIElement, AXUIElement, ChatContext) = try wait(deadline, failureCode: "COMPOSER_OR_HEADER_UNAVAILABLE") {
            var matches: [(AXUIElement, AXUIElement, AXUIElement, ChatContext)] = []
            for window in try windows(app) {
                guard let frame = geometry(window), windowID(window, pid: process.pid) != nil else { continue }
                let elements = try nodes(window), composers = selectComposer(elements, frame: frame), sends = selectSend(elements)
                guard composers.count == 1, sends.count == 1,
                      let context = chatContext(window: window, composer: composers[0], send: sends[0],
                          displays: displays, rawIDs: rawIDs, pid: process.pid) else { continue }
                matches.append((window, composers[0], sends[0], context))
            }
            return matches.count == 1 ? matches[0] : nil
        }
        guard AXUIElementSetAttributeValue(selected.1, kAXFocusedAttribute as CFString, kCFBooleanTrue) == .success else { try fail("COMPOSER_FOCUS_FAILED") }
        guard try processIdentity().epoch == process.epoch else { try fail("APP_INSTANCE_CHANGED") }
        navigation = Navigation(process: process, accountID: account, recipientID: recipient, kind: kind,
            catalogToken: catalog, sourceToken: source, profileToken: profileToken, window: selected.0,
            composer: selected.1, send: selected.2, pane: selected.3.pane, headerValue: selected.3.headerValue,
            markerFingerprint: selected.3.markerFingerprint, displayNames: displays, rawRecipientIDs: rawIDs,
            expiresAt: ProcessInfo.processInfo.systemUptime + 30)
        let final = try observe()
        guard final.binding != nil else { navigation = nil; try fail("CHAT_BINDING_UNAVAILABLE") }
        return final.document
    }

    // Application-local accessibility activation only. Never changes TCC,
    // VoiceOver, global settings, app signatures, or persisted preferences.
    func restoreAccessibility() {
        for (process, attribute, previous) in accessibilityOverrides.reversed() {
            guard (try? processIdentity().epoch) == process.epoch else { continue }
            let app = AXUIElementCreateApplication(process.pid)
            _ = AXUIElementSetAttributeValue(app, attribute as CFString, previous ? kCFBooleanTrue : kCFBooleanFalse)
        }
        accessibilityOverrides.removeAll()
    }
    func enableAccessibility() throws -> [String: Any] {
        guard AXIsProcessTrusted() else { try fail("AX_PERMISSION_MISSING") }
        let process = try processIdentity()
        try verifySignature()
        let app = AXUIElementCreateApplication(process.pid)
        AXUIElementSetMessagingTimeout(app, 0.5)
        var reports: [[String: Any]] = []
        for name in ["AXManualAccessibility", "AXEnhancedUserInterface"] {
            let (raw, status) = read(app, name)
            let previous = status == .success ? strictBoolean(raw) : nil
            var writable = DarwinBoolean(false)
            let settable = AXUIElementIsAttributeSettable(app, name as CFString, &writable)
            var row: [String: Any] = ["attribute": name, "read_status": status.rawValue,
                "old_value": null(previous), "settable_status": settable.rawValue,
                "settable": settable == .success ? writable.boolValue as Any : NSNull(), "changed": false]
            // An unknown original value cannot be safely restored. Unsupported
            // attributes are reported, never forced onto the target process.
            if let previous, settable == .success, writable.boolValue {
                let code = AXUIElementSetAttributeValue(app, name as CFString, kCFBooleanTrue)
                row["write_status"] = code.rawValue
                if code == .success {
                    accessibilityOverrides.append((process, name, previous))
                    row["changed"] = !previous
                    row["reasserted"] = previous
                    _ = RunLoop.main.run(mode: .default, before: Date(timeIntervalSinceNow: 0.15))
                    row["readback"] = null(boolean(app, name))
                }
            }
            reports.append(row)
        }
        guard try processIdentity().epoch == process.epoch else {
            restoreAccessibility(); try fail("APP_INSTANCE_CHANGED")
        }
        return ["status": "application_accessibility_checked", "attributes": reports,
            "restore_on_close": true, "system_settings_changed": false, "send_attempts": 0]
    }
    func activateAndProbe() throws -> [String: Any] {
        guard AXIsProcessTrusted() else { try fail("AX_PERMISSION_MISSING") }
        let process = try processIdentity()
        try verifySignature()
        guard let running = NSRunningApplication(processIdentifier: process.pid) else { try fail("APP_INSTANCE_UNAVAILABLE") }
        let app = AXUIElementCreateApplication(process.pid)
        AXUIElementSetMessagingTimeout(app, 0.3)
        let candidates = try windows(app)
        let named = candidates.filter { ["Weixin", "微信", "WeChat"].contains(text($0, kAXTitleAttribute) ?? "") }
        let main = named.count == 1 ? named : candidates.filter { boolean($0, kAXMainAttribute) == true }
        guard main.count == 1, let window = main.first, let rect = geometry(window),
              windowID(window, pid: process.pid) != nil else { try fail("MAIN_CHAT_WINDOW_UNAVAILABLE") }
        var beforeErrors = Set<String>()
        let beforeNodes = descendants(window, errors: &beforeErrors).count
        let beforePID = NSWorkspace.shared.frontmostApplication?.processIdentifier
        let activated = running.activate(options: [])
        let raised = AXUIElementPerformAction(window, kAXRaiseAction as CFString)
        _ = RunLoop.main.run(mode: .default, before: Date(timeIntervalSinceNow: 0.2))
        let (focusedRaw, focusedStatus) = read(app, kAXFocusedUIElementAttribute)
        let focused = elementValue(focusedRaw, focusedStatus)
        var hits: [[String: Any]] = []
        for point in [CGPoint(x: rect.minX + rect.width * 0.7, y: rect.minY + min(110, rect.height * 0.25)),
                      CGPoint(x: rect.midX, y: rect.midY)] {
            var hit: AXUIElement?
            let code = AXUIElementCopyElementAtPosition(app, Float(point.x), Float(point.y), &hit)
            hits.append(["status": code.rawValue, "role": null(hit.flatMap { text($0, kAXRoleAttribute) })])
        }
        _ = RunLoop.main.run(mode: .default, before: Date(timeIntervalSinceNow: 0.2))
        var afterErrors = Set<String>()
        let afterNodes = descendants(window, errors: &afterErrors).count
        guard try processIdentity().epoch == process.epoch else { try fail("APP_INSTANCE_CHANGED") }
        let afterPID = NSWorkspace.shared.frontmostApplication?.processIdentifier
        return ["status": "foreground_accessibility_probe_complete", "pid": process.pid,
            "before_frontmost_pid": null(beforePID), "after_frontmost_pid": null(afterPID),
            "activate_returned": activated, "application_active": running.isActive,
            "frontmost_verified": afterPID == process.pid, "raise_status": raised.rawValue,
            "focused_status": focusedStatus.rawValue, "focused_role": null(focused.flatMap { text($0, kAXRoleAttribute) }),
            "hit_tests": hits, "before_node_count": beforeNodes, "after_node_count": afterNodes,
            "before_errors": beforeErrors.sorted(), "after_errors": afterErrors.sorted(),
            "message_writes": 0, "send_attempts": 0, "system_settings_changed": false]
    }
    func inspectStructure() throws -> [String: Any] {
        guard AXIsProcessTrusted() else { try fail("AX_PERMISSION_MISSING") }
        let process = try processIdentity()
        try verifySignature()
        let app = AXUIElementCreateApplication(process.pid)
        AXUIElementSetMessagingTimeout(app, 0.3)
        let (raw, status) = read(app, kAXWindowsAttribute)
        guard status == .success, let windows = raw as? [AXUIElement] else { try fail("AX_WINDOWS_UNREADABLE") }
        var rows: [[String: Any]] = []
        for (index, window) in windows.enumerated() {
            var errors = Set<String>()
            let nodes = descendants(window, errors: &errors)
            let metadata = nodes.enumerated().map { position, element -> [String: Any] in
                var attributeNames: CFArray?
                let attributeStatus = AXUIElementCopyAttributeNames(element, &attributeNames)
                let identifier = text(element, "AXIdentifier")
                let safeIdentifier = identifier.map { $0.hasPrefix("head_view_") ? "head_view_<redacted>" : $0 }
                return ["index": position, "role": null(text(element, kAXRoleAttribute)),
                    "identifier": null(safeIdentifier), "title_sha256": null(text(element, kAXTitleAttribute).map(digest)),
                    "value_sha256": null(text(element, kAXValueAttribute).map(digest)),
                    "attribute_status": attributeStatus.rawValue,
                    "attributes": attributeStatus == .success ? (attributeNames as? [String] ?? []) : []]
            }
            let rect = geometry(window)
            let bound = windowID(window, pid: process.pid)
            rows.append(["ax_window_index": index, "main": null(boolean(window, kAXMainAttribute)),
                "focused": null(boolean(window, kAXFocusedAttribute)), "minimized": null(boolean(window, kAXMinimizedAttribute)),
                "title_sha256": null(text(window, kAXTitleAttribute).map(digest)), "node_count": nodes.count,
                "matched_on_screen_window_id": null(bound?.0),
                "bounds": rect.map { ["x": $0.minX, "y": $0.minY, "width": $0.width, "height": $0.height] } as Any? ?? NSNull(),
                "errors": errors.sorted(), "nodes": metadata])
        }
        let rawServer = CGWindowListCopyWindowInfo([.excludeDesktopElements], kCGNullWindowID) as? [[String: Any]]
        let server = (rawServer ?? []).filter { ($0[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == process.pid }.map { item -> [String: Any] in
            let title = item[kCGWindowName as String] as? String
            return ["window_id": null((item[kCGWindowNumber as String] as? NSNumber)?.stringValue),
                "layer": null((item[kCGWindowLayer as String] as? NSNumber)?.intValue),
                "sharing_state": null((item[kCGWindowSharingState as String] as? NSNumber)?.intValue),
                "on_screen": null(strictBoolean(item[kCGWindowIsOnscreen as String])),
                "alpha": null((item[kCGWindowAlpha as String] as? NSNumber)?.doubleValue),
                "bounds": item[kCGWindowBounds as String] as Any? ?? NSNull(), "title_sha256": null(title.map(digest))]
        }
        guard try processIdentity().epoch == process.epoch else { try fail("APP_INSTANCE_CHANGED") }
        return ["status": "structure_observed", "pid": process.pid, "launch_token": process.epoch,
            "ax_windows": rows, "window_server_available": rawServer != nil, "window_server_windows": server,
            "ui_writes": 0, "send_attempts": 0]
    }
    private func unknown(_ code: String) -> Observed {
        handles.removeAll(); scopeEpoch = nil
        return Observed(document: ["binding": NSNull(), "draft": NSNull(), "read_errors": [code],
            "identity_verified": false, "identity_reason": code, "native_scope": NSNull(),
            "policy_live_send_enabled": liveSendEnabled, "policy_draft_writes_enabled": draftWritesEnabled], scope: nil, binding: nil)
    }
    private func recipientProfileSnapshot(_ scope: Scope, key: String,
                                          composerToken: String?, sendToken: String?) throws -> [String: Any] {
        guard try processIdentity().epoch == scope.process.epoch else { try fail("PROFILE_PROCESS_CHANGED") }
        let (focusedValue, focusedStatus) = read(scope.app, kAXFocusedUIElementAttribute)
        guard let focused = elementValue(focusedValue, focusedStatus) else { try fail("PROFILE_FOCUS_UNKNOWN") }
        var candidates: [(AXUIElement, [AXUIElement])] = []
        for root in parentChain(focused) {
            let (value, status) = read(root, kAXChildrenAttribute)
            guard status == .success, let children = value as? [AXUIElement], children.count <= 256 else { continue }
            let identifiers = children.compactMap { text($0, "AXIdentifier") }
            let marked = identifiers.contains(where: { $0.hasPrefix("head_view_") })
                && identifiers.contains("value_remark_view") && identifiers.contains("chat_img_button")
            let labels = children.filter { child in [kAXValueAttribute, kAXTitleAttribute, kAXDescriptionAttribute].contains { attribute in
                ["Weixin ID:", "WeChat ID:", "微信号:", "微信号："].contains(text(child, attribute) ?? "")
            } }
            let messageActions = children.filter { child in text(child, kAXRoleAttribute) == "AXButton"
                && (exactStrings(child).contains(where: { ["Message", "Send Message", "发消息", "发送消息"].contains($0) })
                    || text(child, "AXIdentifier") == "chat_img_button") }
            if marked || (labels.count == 1 && messageActions.count == 1) { candidates.append((root, children)) }
        }
        // All candidates are ancestors of the same focused element. Use the
        // nearest bounded profile scope, not its broader window ancestor.
        guard let (root, children) = candidates.first else { try fail("PROFILE_SCOPE_MISSING_OR_AMBIGUOUS") }
        guard (4...256).contains(children.count) else { try fail("PROFILE_SCHEMA_UNSUPPORTED") }
        var rootPID: pid_t = 0
        guard AXUIElementGetPid(root, &rootPID) == .success, rootPID == scope.process.pid else { try fail("PROFILE_PROCESS_CHANGED") }
        let rootToken = token(root, scope: key, kind: "recipient_profile_root")
        var nodes: [[String: Any]] = []
        for child in children {
            var owner: pid_t = 0
            let ownerMatches = AXUIElementGetPid(child, &owner) == .success && owner == scope.process.pid
            let (parentValue, parentStatus) = read(child, kAXParentAttribute)
            let parent = elementValue(parentValue, parentStatus)
            let parentMatches = parent.map { CFEqual($0, root) } ?? false
            // Retain only the public label or its invalid near-variants while
            // scanning; unrelated profile values never enter the IPC snapshot.
            let acceptedLabels = ["Weixin ID:", "WeChat ID:", "微信号:", "微信号："]
            var publicValue = typedStringField(child, kAXValueAttribute, keep: { acceptedLabels.contains($0) })
            for attribute in [kAXTitleAttribute, kAXDescriptionAttribute] where publicValue["value"] is NSNull {
                publicValue = typedStringField(child, attribute, keep: { acceptedLabels.contains($0) })
            }
            let messageAction = text(child, kAXRoleAttribute) == "AXButton"
                && (text(child, "AXIdentifier") == "chat_img_button"
                    || exactStrings(child).contains(where: { ["Message", "Send Message", "发消息", "发送消息"].contains($0) }))
            nodes.append(["token": token(child, scope: key + ":" + rootToken, kind: "recipient_profile_child"),
                "process_epoch": scope.process.epoch, "parent_root_token": rootToken,
                "parent_matches_root": ownerMatches && parentMatches,
                "role": typedStringField(child, kAXRoleAttribute),
                "identifier": typedStringField(child, "AXIdentifier"), "value": publicValue,
                "public_action": messageAction ? "message" as Any : NSNull()])
        }
        let labels = nodes.indices.filter { ["Weixin ID:", "WeChat ID:", "微信号:", "微信号："].contains((nodes[$0]["value"] as? [String: Any])?["value"] as? String ?? "") }
        if labels.count == 1, labels[0] + 1 < children.count {
            // The raw adjacent field is read as CFString without trimming,
            // normalization or guessing from an AXIdentifier suffix.
            var rawField = typedStringField(children[labels[0] + 1], kAXValueAttribute)
            if rawField["value"] is NSNull { rawField = typedStringField(children[labels[0] + 1], kAXTitleAttribute) }
            nodes[labels[0] + 1]["value"] = rawField
        }
        let associated = belongs(root, to: scope.window, pid: scope.process.pid)
        return ["process_epoch": scope.process.epoch, "root_token": rootToken,
            "window_id": associated ? scope.windowID as Any : NSNull(),
            "window_association_verified": associated,
            "composer_token": null(composerToken), "send_token": null(sendToken), "children": nodes]
    }
    private func recipientProfilePair(_ scope: Scope, key: String,
                                      composerToken: String?, sendToken: String?) -> [String: Any] {
        do {
            let first = try recipientProfileSnapshot(scope, key: key, composerToken: composerToken, sendToken: sendToken)
            let second = try recipientProfileSnapshot(scope, key: key, composerToken: composerToken, sendToken: sendToken)
            guard try processIdentity().epoch == scope.process.epoch else { try fail("PROFILE_PROCESS_CHANGED") }
            return ["schema": "recipient-profile-v2", "first": first, "second": second]
        } catch let error as Failure { return ["status": "unavailable", "code": error.code] }
        catch { return ["status": "unavailable", "code": "PROFILE_READ_FAILED"] }
    }
    func observe() throws -> Observed {
        guard AXIsProcessTrusted() else { return unknown("AX_PERMISSION_MISSING") }
        let process = try processIdentity()
        if epoch != process.epoch {
            handles.removeAll(); scopeEpoch = nil
            try verifySignature()
            epoch = process.epoch
        }
        let app = AXUIElementCreateApplication(process.pid)
        AXUIElementSetMessagingTimeout(app, 0.5)
        let (rawWindows, status) = read(app, kAXWindowsAttribute)
        guard status == .success, let windows = rawWindows as? [AXUIElement] else { return unknown("AX_WINDOWS_UNREADABLE") }
        let main = navigation.map { proof in windows.filter { CFEqual($0, proof.window) } } ?? windows.filter { boolean($0, kAXMainAttribute) == true }
        guard main.count == 1, let window = main.first,
              let (selectedWindowID, frame) = windowID(window, pid: process.pid) else { return unknown("WINDOW_UNBOUND_OR_AMBIGUOUS") }
        var errors = Set<String>()
        let elements = descendants(window, errors: &errors)
        let knownComposerIDs = ["chat_input_field", "chat_input", "message_input", "chat_input_editor"]
        let markedComposers = elements.filter { element in
            ["AXTextArea", "AXTextField"].contains(text(element, kAXRoleAttribute) ?? "")
                && knownComposerIDs.contains(text(element, "AXIdentifier") ?? "")
        }
        // Multiline editors are a semantic fallback. Search boxes and secure
        // fields never become composers, and ambiguity remains explicit.
        let semanticComposers = elements.filter { element in
            guard text(element, kAXRoleAttribute) == "AXTextArea",
                  text(element, kAXSubroleAttribute) != "AXSearchField",
                  text(element, kAXSubroleAttribute) != "AXSecureTextField" else { return false }
            let identifier = (text(element, "AXIdentifier") ?? "").lowercased()
            let title = text(element, kAXTitleAttribute) ?? ""
            guard !identifier.contains("search"), !["Search", "搜索"].contains(title),
                  let rect = geometry(element), frame.contains(rect), rect.midY >= frame.midY else { return false }
            return true
        }
        let composers = markedComposers.isEmpty ? semanticComposers : markedComposers
        let sends = elements.filter { element in
            text(element, kAXRoleAttribute) == "AXButton" && ["Send", "发送"].contains(text(element, kAXTitleAttribute) ?? "")
        }
        let composer = composers.count == 1 ? composers[0] : nil
        let send = sends.count == 1 ? sends[0] : nil
        let (rawFocus, focusStatus) = read(app, kAXFocusedUIElementAttribute)
        let focusedElement = elementValue(rawFocus, focusStatus)
        let scope = Scope(process: process, app: app, window: window, windowID: selectedWindowID,
            frame: frame, composer: composer, send: send, composerCount: composers.count, sendCount: sends.count,
            focusedParents: focusedElement.map(parentChain) ?? [])
        let identity = resolveIdentity(scope)
        let verified = identity.map { !$0.accountID.isEmpty && !$0.recipientID.isEmpty
            && ["person", "group"].contains($0.recipientType) && !$0.catalogToken.isEmpty
            && !$0.profileScopeToken.isEmpty && $0.sameWindowVerified } ?? false
        if !verified { errors.insert("IDENTITY_EVIDENCE_UNAVAILABLE") }
        let key = process.epoch + ":" + selectedWindowID + ":" + String(describing: frame)
            + ":" + (verified ? identity!.catalogToken + ":" + identity!.profileScopeToken + ":" + identity!.accountID + ":" + identity!.recipientID : "unverified")
        if scopeEpoch != key { handles.removeAll(); scopeEpoch = key }
        var composerToken: String?, sendToken: String?, draft: String?, editable: Bool?, pressSupported: Bool?, enabled: Bool?
        if let composer {
            if belongs(composer, to: window, pid: process.pid) {
                composerToken = token(composer, scope: key, kind: "composer")
                let (value, code) = read(composer, kAXValueAttribute)
                if code == .success, let string = value as? String { draft = string }
                else { errors.insert("DRAFT_UNREADABLE") }
                var settable = DarwinBoolean(false)
                let writableStatus = AXUIElementIsAttributeSettable(composer, kAXValueAttribute as CFString, &settable)
                if writableStatus == .success { editable = settable.boolValue } else { errors.insert("COMPOSER_WRITABILITY_UNKNOWN") }
            } else { errors.insert("COMPOSER_OUTSIDE_SCOPE") }
        }
        if let send {
            if belongs(send, to: window, pid: process.pid) {
                sendToken = token(send, scope: key, kind: "send")
                enabled = boolean(send, kAXEnabledAttribute)
                var names: CFArray?
                let code = AXUIElementCopyActionNames(send, &names)
                pressSupported = decodedPressSupport(names, status: code)
                if pressSupported == nil { errors.insert("SEND_ACTION_UNKNOWN") }
            } else { errors.insert("SEND_OUTSIDE_SCOPE") }
        }
        let minimized = boolean(window, kAXMinimizedAttribute), modal = boolean(window, kAXModalAttribute)
        let (rawFocusedWindow, focusedWindowStatus) = read(app, kAXFocusedWindowAttribute)
        let focusedWindow = elementValue(rawFocusedWindow, focusedWindowStatus)
        let foreground = NSWorkspace.shared.frontmostApplication?.processIdentifier
        let focused: Bool? = focusedWindow.map { CFEqual($0, window) && foreground == process.pid }
        let composerFocused: Bool? = composer.flatMap { editor in focusedElement.map { CFEqual($0, editor) } }
        let session = CGSessionCopyCurrentDictionary() as? [String: Any]
        let locked = strictBoolean(session?["CGSSessionScreenIsLocked"])
        for (name, value) in [("MINIMIZED", minimized), ("MODAL", modal), ("FOCUS", focused),
                               ("COMPOSER_FOCUS", composerFocused), ("SEND_ENABLED", enabled)] where value == nil {
            errors.insert(name + "_UNKNOWN")
        }
        let profile = recipientProfilePair(scope, key: key, composerToken: composerToken, sendToken: sendToken)
        guard try processIdentity().epoch == process.epoch,
              let (afterWindow, afterFrame) = windowID(window, pid: process.pid),
              afterWindow == selectedWindowID, afterFrame == frame else { return unknown("APP_OR_WINDOW_CHANGED") }
        var binding: [String: Any]?
        if verified, let identity, let composerToken, let sendToken {
            binding = ["account_id": identity.accountID, "recipient_id": identity.recipientID,
                "recipient_type": identity.recipientType, "pid": process.pid, "launch_token": process.epoch,
                "bundle_path": officialBundle, "window_id": selectedWindowID,
                "composer_token": composerToken, "send_token": sendToken]
        }
        let document: [String: Any] = ["binding": null(binding), "draft": null(draft), "read_errors": errors.sorted(),
            "identity_verified": verified, "identity_reason": verified ? "VERIFIED_CATALOG_SCOPE" : "IDENTITY_EVIDENCE_UNAVAILABLE",
            "account_matches": verified ? 1 as Any : NSNull(), "recipient_matches": verified ? 1 as Any : NSNull(),
            "composer_matches": composers.count, "send_matches": sends.count,
            "on_screen": true, "focused": null(focused), "composer_focused": null(composerFocused),
            "window_minimized": null(minimized), "modal": null(modal), "locked": null(locked),
            "composer_editable": null(editable), "send_enabled": null(enabled), "press_supported": null(pressSupported),
            "policy_live_send_enabled": liveSendEnabled, "policy_draft_writes_enabled": draftWritesEnabled,
            "recipient_profile": profile,
            "native_scope": ["pid": process.pid, "launch_token": process.epoch, "window_id": selectedWindowID,
                "composer_token": null(composerToken), "send_token": null(sendToken), "node_count": elements.count]]
        return Observed(document: document, scope: scope, binding: binding)
    }
    private func safeForDraft(_ current: Observed) -> Bool {
        safeDraftDocument(current.document, bindingAvailable: current.binding != nil)
    }
    func setDraft(_ arguments: [String: Any]) throws -> [String: Any] {
        guard draftWritesEnabled else { try fail("DRAFT_WRITES_DISABLED") }
        guard let expected = arguments["binding"] as? [String: Any],
              let expectedHash = arguments["expected_draft_hash"] as? String,
              expectedHash.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
              let requested = arguments["text"] as? String else { try fail("INVALID_DRAFT_ARGUMENTS") }
        let current = try observe()
        guard safeForDraft(current), let binding = current.binding, canonical(binding) == canonical(expected),
              let currentDraft = current.document["draft"] as? String, digest(currentDraft) == expectedHash,
              let identifier = expected["composer_token"] as? String, let handle = handles[identifier],
              handle.kind == "composer", handle.scope == scopeEpoch,
              let scope = current.scope, let composer = scope.composer,
              CFEqual(handle.element, composer), belongs(composer, to: scope.window, pid: scope.process.pid)
        else { try fail("DRAFT_BINDING_OR_STATE_CHANGED") }
        let code = AXUIElementSetAttributeValue(handle.element, kAXValueAttribute as CFString, requested as CFString)
        guard code == .success else { try fail("DRAFT_WRITE_UNCERTAIN") }
        let after = try observe()
        guard safeForDraft(after), let afterBinding = after.binding, canonical(afterBinding) == canonical(binding),
              (after.document["draft"] as? String) == requested else { try fail("DRAFT_WRITE_UNCERTAIN") }
        return ["applied": true, "observation": after.document]
    }
    func pressSend(_ arguments: [String: Any]) throws -> [String: Any] {
        guard liveSendEnabled else { try fail("LIVE_SEND_DISABLED") }
        guard let expected = arguments["binding"] as? [String: Any],
              let expectedHash = arguments["expected_draft_hash"] as? String,
              expectedHash.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
              let operation = arguments["operation_id"] as? String,
              operation.range(of: "^[A-Za-z0-9_-]{1,128}$", options: .regularExpression) != nil,
              let expiration = arguments["expires_at"] as? NSNumber,
              CFGetTypeID(expiration) != CFBooleanGetTypeID(), expiration.doubleValue.isFinite,
              ProcessInfo.processInfo.systemUptime < expiration.doubleValue,
              expiration.doubleValue <= ProcessInfo.processInfo.systemUptime + 30 else { try fail("INVALID_SEND_ARGUMENTS") }
        guard !submittedOperations.contains(operation) else { try fail("SEND_ALREADY_ATTEMPTED") }
        let current = try observe()
        guard safeForDraft(current), current.document["send_enabled"] as? Bool == true,
              current.document["press_supported"] as? Bool == true,
              let binding = current.binding, canonical(binding) == canonical(expected),
              let draft = current.document["draft"] as? String, !draft.isEmpty, digest(draft) == expectedHash,
              let identifier = expected["send_token"] as? String, let handle = handles[identifier],
              handle.kind == "send", handle.scope == scopeEpoch,
              let scope = current.scope, let send = scope.send, CFEqual(handle.element, send),
              belongs(send, to: scope.window, pid: scope.process.pid),
              try processIdentity().epoch == scope.process.epoch else { try fail("SEND_BINDING_OR_STATE_CHANGED") }
        // Mark before the only effect. An AX error may still have produced the
        // effect, so the operation can never be submitted again in this bridge.
        submittedOperations.insert(operation)
        let code = AXUIElementPerformAction(handle.element, kAXPressAction as CFString)
        return ["outcome": code == .success ? "ACCEPTED" : "UNKNOWN", "action_status": code.rawValue]
    }
    func request(_ request: [String: Any]) -> [String: Any] {
        let identifier = request["id"] as? String ?? "invalid"
        do {
            guard identifier.range(of: "^[A-Za-z0-9-]{1,64}$", options: .regularExpression) != nil,
                  let operation = request["op"] as? String else { try fail("INVALID_REQUEST") }
            switch operation {
            case "read_observation": return ["id": identifier, "ok": true, "result": try observe().document]
            case "open_chat": return ["id": identifier, "ok": true, "result": try openChat(request["args"] as? [String: Any] ?? [:])]
            case "activate_and_probe": return ["id": identifier, "ok": true, "result": try activateAndProbe()]
            case "inspect_structure": return ["id": identifier, "ok": true, "result": try inspectStructure()]
            case "enable_app_accessibility": return ["id": identifier, "ok": true, "result": try enableAccessibility()]
            case "restore_app_accessibility": restoreAccessibility(); return ["id": identifier, "ok": true, "result": ["status": "application_accessibility_restored"]]
            case "set_draft": return ["id": identifier, "ok": true, "result": try setDraft(request["args"] as? [String: Any] ?? [:])]
            case "press_send": return ["id": identifier, "ok": true, "result": try pressSend(request["args"] as? [String: Any] ?? [:])]
            default: try fail("UNSUPPORTED_OPERATION")
            }
        } catch let error as Failure { return ["id": identifier, "ok": false, "code": error.code] }
        catch { return ["id": identifier, "ok": false, "code": "NATIVE_OPERATION_FAILED"] }
    }
}

let arguments = Array(CommandLine.arguments.dropFirst())
if arguments == ["--signature-check"] {
    let started = ProcessInfo.processInfo.systemUptime
    do {
        try verifySignature()
        print("{\"ok\":true,\"status\":\"running_code_verified\",\"elapsed_ms\":\((ProcessInfo.processInfo.systemUptime-started)*1000),\"ui_reads\":0,\"ui_writes\":0,\"send_attempts\":0}")
        exit(0)
    } catch let error as Failure { print("{\"ok\":false,\"code\":\"\(error.code)\",\"send_attempts\":0}"); exit(78) }
    catch { print("{\"ok\":false,\"code\":\"SIGNATURE_CHECK_FAILED\",\"send_attempts\":0}"); exit(78) }
}
if arguments == ["--self-test"] {
    let bridge = Bridge(draftWritesEnabled: false)
    let disabled = bridge.request(["id": "synthetic-1", "op": "press_send"])
    let draftDisabled = bridge.request(["id": "synthetic-2", "op": "set_draft", "args": [:]])
    let actionDecoderPassed = decodedPressSupport(nil, status: .success) == nil
        && decodedPressSupport(NSNull(), status: .success) == nil
        && decodedPressSupport("AXPress", status: .success) == nil
        && decodedPressSupport([42], status: .success) == nil
        && decodedPressSupport([kAXPressAction, 42] as [Any], status: .success) == nil
        && decodedPressSupport([] as [String], status: .success) == false
        && decodedPressSupport([kAXPressAction] as CFArray, status: .success) == true
        && decodedPressSupport(["AXCancel"], status: .success) == false
        && decodedPressSupport([kAXPressAction], status: .cannotComplete) == nil
    let emptyDraftState: [String: Any] = ["read_errors": [] as [String], "identity_verified": true,
        "on_screen": true, "focused": true, "composer_focused": true, "composer_editable": true,
        "window_minimized": false, "modal": false, "locked": false,
        "account_matches": 1, "recipient_matches": 1, "composer_matches": 1, "send_matches": 1,
        "send_enabled": false, "press_supported": false]
    var payloadState = emptyDraftState
    payloadState["send_enabled"] = true; payloadState["press_supported"] = true
    var unknownPress = emptyDraftState; unknownPress["press_supported"] = NSNull()
    var missingPress = emptyDraftState; missingPress.removeValue(forKey: "press_supported")
    var unknownEnabled = emptyDraftState; unknownEnabled["send_enabled"] = NSNull()
    var numberPress = emptyDraftState; numberPress["press_supported"] = NSNumber(value: 0)
    let draftCapabilityPassed = safeDraftDocument(emptyDraftState, bindingAvailable: true)
        && safeDraftDocument(payloadState, bindingAvailable: true)
        && !safeDraftDocument(unknownPress, bindingAvailable: true)
        && !safeDraftDocument(missingPress, bindingAvailable: true)
        && !safeDraftDocument(unknownEnabled, bindingAvailable: true)
        && !safeDraftDocument(numberPress, bindingAvailable: true)
    let fixturePane = CGRect(x: 300, y: 0, width: 600, height: 700)
    let fixtureComposer = CGRect(x: 320, y: 500, width: 560, height: 180)
    let leftAlice = CGRect(x: 30, y: 30, width: 100, height: 24)
    let rightTitle = CGRect(x: 350, y: 30, width: 100, height: 24)
    let candidates = [(leftAlice, "Alice"), (rightTitle, "Bob")]
    let selectedAlice = candidates.filter { rect, title in
        byteEqual(title, "Alice") && isLocalChatHeader(pane: fixturePane, composer: fixtureComposer,
            candidate: rect, role: "AXStaticText", ancestorRoles: ["AXStaticText", "AXGroup"])
    }
    let localHeaderPassed = selectedAlice.isEmpty
        && isLocalChatHeader(pane: fixturePane, composer: fixtureComposer, candidate: rightTitle,
            role: "AXStaticText", ancestorRoles: ["AXStaticText", "AXGroup"])
        && !isLocalChatHeader(pane: CGRect(x: 0, y: 0, width: 900, height: 700), composer: fixtureComposer,
            candidate: rightTitle, role: "AXStaticText", ancestorRoles: ["AXStaticText", "AXGroup"])
        && !isLocalChatHeader(pane: fixturePane, composer: fixtureComposer, candidate: rightTitle,
            role: "AXStaticText", ancestorRoles: ["AXStaticText", "AXRow", "AXList"])
        && recipientMarker(identifier: "head_view_fixture_a", fields: [], rawIDs: ["fixture_a"]) != nil
        && recipientMarker(identifier: "head_view_fixture_b", fields: [], rawIDs: ["fixture_a"]) == nil
        && recipientMarker(identifier: "head_view_", fields: ["Alice"], rawIDs: ["fixture_a"]) == nil
    let passed = disabled["code"] as? String == "LIVE_SEND_DISABLED" && draftDisabled["code"] as? String == "DRAFT_WRITES_DISABLED"
        && digest("synthetic") == "b3cc0475bb78a5026098858e9889acf666d31062d513d303314eca31d36e72f2"
        && strictBoolean(false) == false && strictBoolean(true) == true
        && strictBoolean(NSNumber(value: 0)) == nil && strictBoolean(NSNull()) == nil && strictBoolean(nil) == nil
        && actionDecoderPassed && draftCapabilityPassed && localHeaderPassed
    print("{\"ok\":\(passed),\"synthetic_only\":true,\"action_decoder_checks\":9,\"draft_capability_checks\":6,\"local_header_checks\":7,\"ui_reads\":0,\"ui_writes\":0,\"send_attempts\":0}")
    exit(passed ? 0 : 1)
}
guard arguments.first == "--serve", Set(arguments).count == arguments.count,
      arguments.dropFirst().allSatisfy({ ["--allow-draft-writes", "--allow-live-send"].contains($0) }),
      isatty(STDIN_FILENO) == 0, isatty(STDOUT_FILENO) == 0 else {
    print("{\"ok\":false,\"code\":\"LOCAL_PIPE_SERVE_REQUIRED\",\"send_attempts\":0}"); exit(64)
}
private let bridge = Bridge(draftWritesEnabled: arguments.contains("--allow-draft-writes"), liveSendEnabled: arguments.contains("--allow-live-send"))
while let line = readLine() {
    var response: [String: Any]
    if line.utf8.count <= maximumLine, let data = line.data(using: .utf8),
       let request = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] { response = bridge.request(request) }
    else { response = ["id": "invalid", "ok": false, "code": "INVALID_JSONL_REQUEST"] }
    if let data = canonical(response) { FileHandle.standardOutput.write(data); FileHandle.standardOutput.write(Data([10])) }
    else { print("{\"id\":\"invalid\",\"ok\":false,\"code\":\"IPC_ENCODING_FAILED\"}") }
}

bridge.restoreAccessibility()
