// Read-only Account-field source observer. No navigation, activation, keys,
// clipboard, screenshots, sending, settings changes, database or key-file reads.
// Recipient cards remain handled by native_backend.parse_recipient_profile_pair.
import AppKit
import ApplicationServices
import CoreGraphics
import CryptoKit
import Foundation

private struct Failure: Error { let code: String }
private func stop(_ code: String) throws -> Never { throw Failure(code: code) }
private func hash(_ string: String) -> String { SHA256.hash(data: Data(string.utf8)).map { String(format: "%02x", $0) }.joined() }
private func read(_ element: AXUIElement, _ name: String) -> (AnyObject?, AXError) {
    var result: AnyObject?; let code = AXUIElementCopyAttributeValue(element, name as CFString, &result); return (result, code)
}
private func axElement(_ value: AnyObject?, _ code: AXError) -> AXUIElement? {
    guard code == .success, let value, CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
    return (value as! AXUIElement)
}
private func parentChain(_ element: AXUIElement) -> [AXUIElement] {
    var result: [AXUIElement] = [], cursor: AXUIElement? = element
    for _ in 0..<20 { guard let e = cursor, !result.contains(where: { CFEqual($0, e) }) else { break }; result.append(e); let (v,c) = read(e,kAXParentAttribute); cursor = axElement(v,c) }
    return result
}
private func children(_ element: AXUIElement) -> [AXUIElement]? {
    let (value,code) = read(element,kAXChildrenAttribute); return code == .success ? value as? [AXUIElement] : nil
}
private func literal(_ element: AXUIElement, _ name: String) -> String? {
    let (value,code) = read(element,name)
    guard code == .success, let value, CFGetTypeID(value) == CFStringGetTypeID() else { return nil }
    return value as? String
}
private struct Typed: Equatable {
    let status: Int32, type: String, raw: String?
    static func == (left:Typed,right:Typed)->Bool {
        guard left.status==right.status,left.type==right.type else{return false}
        switch (left.raw,right.raw) {
        case (nil,nil):return true
        case let (a?,b?):return Data(a.utf8)==Data(b.utf8)
        default:return false
        }
    }
    var publicValue: [String: Any] { ["status":status,"type":type,"utf8_length":raw.map { $0.utf8.count } as Any? ?? NSNull(),"sha256":raw.map(hash) as Any? ?? NSNull()] }
}
private func typed(_ value: AnyObject?, _ status: AXError) -> Typed {
    guard status == .success, let value else { return Typed(status:status.rawValue,type:"unavailable",raw:nil) }
    if CFGetTypeID(value) == CFStringGetTypeID(), let string = value as? String { return Typed(status:0,type:"CFString",raw:string) }
    if let attributed = value as? NSAttributedString { return Typed(status:0,type:"AttributedString",raw:attributed.string) }
    return Typed(status:0,type:"non_string",raw:nil)
}
private func typed(_ element: AXUIElement, _ attribute: String) -> Typed { let (v,c) = read(element,attribute); return typed(v,c) }
private func fullCharacterCount(_ value:AnyObject?,status:AXError)->Int? {
    guard status == .success,let number=value as? NSNumber,
          CFGetTypeID(number) != CFBooleanGetTypeID() else{return nil}
    let numeric=number.doubleValue
    guard numeric.isFinite,numeric.rounded(.towardZero)==numeric,(1...256).contains(numeric) else{return nil}
    let count=Int(numeric)
    // NSNumber.compare may round an NSDecimalNumber through binary floating
    // point; compare Decimal values to reject even tiny retained fractions.
    var decimal=number.decimalValue,integer=Decimal(count)
    guard !NSDecimalIsNotANumber(&decimal),NSDecimalCompare(&decimal,&integer) == .orderedSame else{return nil}
    return count
}
private func fullRangeField(_ value:AnyObject?,status:AXError,requested:Int)->Typed {
    let result=typed(value,status)
    guard (1...256).contains(requested) else{return Typed(status:result.status,type:"invalid_range_count",raw:nil)}
    guard let raw=result.raw else{return result}
    guard raw.utf16.count==requested else{return Typed(status:result.status,type:"range_length_mismatch",raw:nil)}
    return result
}
private func geometry(_ element: AXUIElement) -> CGRect? {
    let (p,pc) = read(element,kAXPositionAttribute), (s,sc) = read(element,kAXSizeAttribute)
    guard pc == .success, sc == .success, let p, let s, CFGetTypeID(p) == AXValueGetTypeID(), CFGetTypeID(s) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero, size = CGSize.zero
    guard AXValueGetValue(p as! AXValue,.cgPoint,&point), AXValueGetValue(s as! AXValue,.cgSize,&size), size.width > 0, size.height > 0 else { return nil }
    return CGRect(origin:point,size:size)
}
private func visibleWindow(_ element: AXUIElement, pid: pid_t) -> (CGWindowID,CGRect)? {
    guard let rect = geometry(element) else { return nil }
    let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly,.excludeDesktopElements],kCGNullWindowID) as? [[String:Any]] ?? []
    let matches = windows.filter { value in
        guard (value[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid, (value[kCGWindowLayer as String] as? NSNumber)?.intValue == 0,
              let bounds = value[kCGWindowBounds as String] as? [String:Any], let frame = CGRect(dictionaryRepresentation:bounds as CFDictionary) else { return false }
        return abs(frame.minX-rect.minX)<=1 && abs(frame.minY-rect.minY)<=1 && abs(frame.width-rect.width)<=1 && abs(frame.height-rect.height)<=1
    }
    guard matches.count == 1, let number = matches[0][kCGWindowNumber as String] as? NSNumber else { return nil }; return (number.uint32Value,rect)
}

private struct Options {
    var observe = false, ack = false, selfTest = false
    var pid: pid_t = 0, launch: Double = 0, duration: Double = 60
    var expectedHash: String?
}
private func parse(_ supplied:[String]) throws -> Options {
    var args = supplied, options = Options()
    if args == ["--self-test"] { options.selfTest = true; return options }
    func value() throws -> String { guard !args.isEmpty else { try stop("ARGUMENT_VALUE_REQUIRED") }; return args.removeFirst() }
    while !args.isEmpty {
        switch args.removeFirst() {
        case "--observe": options.observe = true
        case "--ack-local-ui-read": options.ack = true
        case "--pid": guard let n = Int32(try value()), n>0 else { try stop("INVALID_PID") }; options.pid=n
        case "--launch-epoch": guard let n = Double(try value()), n.isFinite,n>0 else { try stop("INVALID_LAUNCH") }; options.launch=n
        case "--seconds": guard let n = Double(try value()), n.isFinite,(1...90).contains(n) else { try stop("INVALID_DURATION") }; options.duration=n
        case "--expected-account-sha256": let s = try value(); guard s.range(of:"^[a-f0-9]{64}$",options:.regularExpression) != nil else { try stop("INVALID_EXPECTED_HASH") }; options.expectedHash=s
        default: try stop("INVALID_ARGUMENT")
        }
    }
    guard options.observe,options.ack,options.pid>0,options.launch>0 else { try stop("EXPLICIT_READ_OPT_IN_REQUIRED") }; return options
}
private func official(_ options:Options, verifySignature:Bool) throws {
    let apps = NSRunningApplication.runningApplications(withBundleIdentifier:"com.tencent.xinWeChat")
    guard apps.count==1,let app=apps.first,app.processIdentifier==options.pid,!app.isTerminated,
          app.bundleURL?.resolvingSymlinksInPath().path=="/Applications/WeChat.app",let launched=app.launchDate,
          abs(launched.timeIntervalSince1970-options.launch)<0.00001 else { try stop("OFFICIAL_PROCESS_CHANGED") }
    if verifySignature {
        let command=Process();command.executableURL=URL(fileURLWithPath:"/usr/bin/codesign")
        command.arguments=["--verify","--deep","--strict","-R","=anchor apple generic and identifier \"com.tencent.xinWeChat\"","/Applications/WeChat.app"]
        command.standardOutput=FileHandle.nullDevice;command.standardError=FileHandle.nullDevice;try command.run();command.waitUntilExit();guard command.terminationStatus==0 else {try stop("OFFICIAL_SIGNATURE_NOT_VERIFIED")}
        let details=Process(),pipe=Pipe();details.executableURL=URL(fileURLWithPath:"/usr/bin/codesign");details.arguments=["--display","--verbose=2","/Applications/WeChat.app"];details.standardOutput=FileHandle.nullDevice;details.standardError=pipe;try details.run();let data=pipe.fileHandleForReading.readDataToEndOfFile();details.waitUntilExit()
        guard details.terminationStatus==0,(String(data:data,encoding:.utf8) ?? "").split(separator:"\n").contains("TeamIdentifier=5A4RE8SF68") else {try stop("OFFICIAL_TEAM_NOT_VERIFIED")}
    }
}

private struct Envelope {
    let process:String,window:UInt32,root:String,field:String
    let accountLabel:Typed,logoutLabel:Typed
    let settingsWindow:Bool,insideField:Bool
    let sources:[String:Typed]
}
private func pairReport(_ first:Envelope,_ second:Envelope,expected:String?) throws -> [String:Any] {
    guard !first.process.isEmpty,!first.root.isEmpty,!first.field.isEmpty,first.window>0 else{try stop("ACCOUNT_SCOPE_UNVERIFIABLE")}
    guard first.process==second.process,first.window==second.window,first.root==second.root,first.field==second.field else {try stop("ACCOUNT_SCOPE_CHANGED")}
    guard first.settingsWindow,second.settingsWindow,first.insideField,second.insideField,
          first.accountLabel==Typed(status:0,type:"CFString",raw:"Account"),second.accountLabel==first.accountLabel,
          ["Log Out","退出登录"].contains(first.logoutLabel.raw ?? ""),first.logoutLabel.type=="CFString",first.logoutLabel.status==0,second.logoutLabel==first.logoutLabel else {try stop("ACCOUNT_SCOPE_UNVERIFIABLE")}
    guard first.sources==second.sources else {try stop("ACCOUNT_SOURCES_CHANGED")}
    let matches=first.sources.keys.filter { key in guard let raw=first.sources[key]?.raw,!raw.isEmpty,let expected else{return false};return hash(raw)==expected }.sorted()
    let rows=first.sources.keys.sorted().map { key -> [String:Any] in var row=first.sources[key]!.publicValue;row["attribute"]=key;row["matches_expected_full_field_hash"]=matches.contains(key);return row }
    return ["status":matches.isEmpty ? "account_sources_observed" : "account_full_field_hash_observed",
        "schema":"settings-account-source-v1","process_epoch_sha256":hash(first.process),"window_id":first.window,
        "scope_sha256":first.root,"field_sha256":first.field,"scope_verified":true,"double_read_stable":true,
        "public_labels":["Account","Log Out"],"typed_sources":rows,"matching_source_attributes":matches,
        "identity_binding_established":false,"account_binding":"unverified","live_send_permitted":false]
}

private final class Observer {
    let options:Options, app:AXUIElement
    var last:[String:Any] = ["status":"account_scope_not_observed"],completed=false,events=0
    init(_ options:Options){self.options=options;app=AXUIElementCreateApplication(options.pid);AXUIElementSetMessagingTimeout(app,0.15)}
    func sources(_ field:AXUIElement)->[String:Typed] {
        var names:CFArray?;let status=AXUIElementCopyAttributeNames(field,&names)
        guard status == .success else{return ["ATTRIBUTE_NAMES":Typed(status:status.rawValue,type:"unavailable",raw:nil)]}
        var result:[String:Typed]=[:]
        for name in names as? [String] ?? [] {let value=typed(field,name);if value.raw != nil || [kAXValueAttribute,"AXAttributedValue",kAXTitleAttribute,kAXDescriptionAttribute,kAXHelpAttribute,kAXRoleDescriptionAttribute].contains(name){result[name]=value}}
        var parameters:CFArray?;let pc=AXUIElementCopyParameterizedAttributeNames(field,&parameters)
        let available=Set(parameters as? [String] ?? [])
        let (rawCount,countStatus)=read(field,kAXNumberOfCharactersAttribute)
        if pc == .success,let count=fullCharacterCount(rawCount,status:countStatus){
            var range=CFRange(location:0,length:count)
            if let argument=AXValueCreate(.cfRange,&range){for name in [kAXStringForRangeParameterizedAttribute,kAXAttributedStringForRangeParameterizedAttribute] where available.contains(name){var value:AnyObject?;let code=AXUIElementCopyParameterizedAttributeValue(field,name as CFString,argument,&value);result[name]=fullRangeField(value,status:code,requested:count)}}
        }
        return result
    }
    func sample(_ trigger:AXUIElement?) throws -> [Envelope] {
        try official(options,verifySignature:false)
        var candidates:[AXUIElement]=[]
        if let trigger {candidates.append(trigger)}
        let (fv,fc)=read(app,kAXFocusedUIElementAttribute);if let focus=axElement(fv,fc){candidates.append(focus)}
        let windows=children(app) ?? []
        var settings=windows.filter{literal($0,kAXRoleAttribute)=="AXWindow" && literal($0,kAXTitleAttribute)=="Settings"}
        let (wv,wc)=read(app,kAXWindowsAttribute)
        for window in wc == .success ? (wv as? [AXUIElement] ?? []) : [] where literal(window,kAXTitleAttribute)=="Settings" {if !settings.contains(where:{CFEqual($0,window)}){settings.append(window)}}
        for candidate in candidates {for parent in parentChain(candidate) where literal(parent,kAXRoleAttribute)=="AXWindow" && literal(parent,kAXTitleAttribute)=="Settings" {if !settings.contains(where:{CFEqual($0,parent)}){settings.append(parent)}}}
        guard settings.count==1,let window=settings.first,let (id,frame)=visibleWindow(window,pid:options.pid),let nodes=children(window),nodes.count>8,nodes.count<=80 else {try stop("SETTINGS_WINDOW_UNBOUND")}
        // This is the observed current English Settings schema, not a fuzzy
        // search across preference values. Unknown layout stops before values.
        let label=typed(nodes[6],kAXValueAttribute)
        guard label==Typed(status:0,type:"CFString",raw:"Account") else {try stop("ACCOUNT_LABEL_SCHEMA_UNSUPPORTED")}
        var logout:Typed?
        for name in [kAXTitleAttribute,kAXValueAttribute,kAXDescriptionAttribute] {let value=typed(nodes[8],name);if value.type=="CFString",value.status==0,["Log Out","退出登录"].contains(value.raw ?? ""){logout=value;break}}
        guard let logout else {try stop("ACCOUNT_LOGOUT_LABEL_UNVERIFIABLE")}
        guard let fieldFrame=geometry(nodes[7]) else {try stop("ACCOUNT_FIELD_GEOMETRY_UNVERIFIABLE")}
        candidates.append(nodes[7])
        var seen:[AXUIElement]=[],result:[Envelope]=[]
        for field in candidates {
            guard !seen.contains(where:{CFEqual($0,field)}) else{continue};seen.append(field)
            var owner:pid_t=0;guard AXUIElementGetPid(field,&owner) == .success,owner==options.pid else{continue}
            let associated=parentChain(field).contains{CFEqual($0,window)}
            let sameField=CFEqual(field,nodes[7])
            let inside=geometry(field).map{fieldFrame.insetBy(dx:-2,dy:-2).contains($0)} ?? false
            guard associated && (sameField || inside) else{continue}
            let epoch="\(options.pid):\(options.launch)"
            let scopeHash=hash(epoch+":\(id):\(frame):\(CFHash(window))")
            result.append(Envelope(process:epoch,window:id,root:scopeHash,field:hash(scopeHash+":\(CFHash(field))"),accountLabel:label,logoutLabel:logout,settingsWindow:true,insideField:true,sources:sources(field)))
        }
        guard let after=visibleWindow(window,pid:options.pid),after.0==id,after.1==frame else{try stop("SETTINGS_WINDOW_CHANGED")}
        try official(options,verifySignature:false);return result
    }
    func inspect(_ trigger:AXUIElement?) {
        guard !completed else{return}
        do {let first=try sample(trigger),second=try sample(trigger)
            for initial in first {if let repeated=second.first(where:{$0.field==initial.field}){var report=try pairReport(initial,repeated,expected:options.expectedHash);report["measured_epoch"]=Date().timeIntervalSince1970;last=report;if !(report["matching_source_attributes"] as? [String] ?? []).isEmpty{completed=true;return}}}
        } catch let error as Failure {last=["status":"unavailable","code":error.code]} catch {last=["status":"unavailable","code":"ACCOUNT_READ_FAILED"]}
    }
}

private func selfTest() throws -> [String:Any] {
    let raw="FixtureNick fixture_alias",label=Typed(status:0,type:"CFString",raw:"Account"),logout=Typed(status:0,type:"CFString",raw:"Log Out")
    func sample(_ raw:String?=raw,_ status:Int32=0,_ root:String="root",_ scope:Bool=true)->Envelope{Envelope(process:"fixture:1",window:1,root:root,field:"field",accountLabel:label,logoutLabel:logout,settingsWindow:scope,insideField:scope,sources:["AXValue":Typed(status:status,type:status==0 ? "CFString" : "unavailable",raw:raw)])}
    var checks:[String]=[]
    func expect(_ value:Bool,_ code:String)throws{guard value else{try stop("SELF_TEST_"+code)};checks.append(code)}
    let before=sample(),good=try pairReport(before,before,expected:hash(raw))
    try expect(good["status"] as? String=="account_full_field_hash_observed","raw_full_string_exact")
    let spaces=sample(" "+raw),spaceResult=try pairReport(spaces,spaces,expected:hash(raw))
    try expect((spaceResult["matching_source_attributes"] as? [String])?.isEmpty==true,"no_trim_or_guess_split")
    let unknown=sample(nil,-25212),unknownResult=try pairReport(unknown,unknown,expected:hash(""))
    try expect((unknownResult["matching_source_attributes"] as? [String])?.isEmpty==true,"unknown_not_empty")
    for (name,other) in [("changed_value",sample("other")),("changed_root",sample(raw,0,"different")),("unknown_scope",sample(raw,0,"root",false))] {do{_ = try pairReport(before,other,expected:hash(raw));try stop("SELF_TEST_EXPECTED_REJECTION")}catch let failure as Failure{guard !failure.code.hasPrefix("SELF_TEST") else{throw failure};checks.append(name)}}
    do{_ = try pairReport(sample("e\u{301}"),sample("é"),expected:nil);try stop("SELF_TEST_EXPECTED_REJECTION")}catch let failure as Failure{guard failure.code=="ACCOUNT_SOURCES_CHANGED" else{throw failure};checks.append("unicode_byte_change_rejected")}
    let attributed=typed(NSAttributedString(string:raw),.success)
    try expect(attributed.type=="AttributedString" && attributed.raw.map{Data($0.utf8)==Data(raw.utf8)}==true,"attributed_source_preserves_bytes")
    try expect([NSNumber(value:1.5),NSDecimalNumber(string:"1.000000000000000000001")].allSatisfy{fullCharacterCount($0,status:.success)==nil},"fractional_character_count_rejected")
    try expect([NSNumber(value:0),NSNumber(value:-1),NSNumber(value:257)].allSatisfy{fullCharacterCount($0,status:.success)==nil},"out_of_range_character_count_rejected")
    try expect([NSNumber(value:true),NSNumber(value:Double.nan),NSNumber(value:Double.infinity)].allSatisfy{fullCharacterCount($0,status:.success)==nil},"bool_and_nonfinite_character_count_rejected")
    try expect(fullCharacterCount(NSNumber(value:1),status:.success)==1 && fullCharacterCount(NSNumber(value:256.0),status:.success)==256 && fullCharacterCount(NSNumber(value:2),status:.noValue)==nil,"valid_complete_character_count_only")
    let short=fullRangeField("ab" as NSString,status:.success,requested:3)
    let shortAttributed=fullRangeField(NSAttributedString(string:"ab"),status:.success,requested:3)
    try expect(short.raw==nil && short.type=="range_length_mismatch" && shortAttributed.raw==nil,"returned_short_range_not_full_field")
    try expect(fullRangeField("😀" as NSString,status:.success,requested:2).raw != nil && fullRangeField("😀" as NSString,status:.success,requested:1).raw==nil && fullRangeField(NSAttributedString(string:"😀"),status:.success,requested:2).raw != nil,"range_length_is_utf16_not_graphemes")
    let data=try JSONSerialization.data(withJSONObject:good,options:[.sortedKeys]);let output=String(decoding:data,as:UTF8.self)
    try expect(!output.contains(raw) && !output.contains("fixture_alias") && good["live_send_permitted"] as? Bool==false,"redacted_no_send_capability")
    do{_ = try parse(["--observe","--ack-local-ui-read","--pid","1","--launch-epoch","1","--seconds","91"]);try stop("SELF_TEST_EXPECTED_REJECTION")}catch let failure as Failure{guard failure.code=="INVALID_DURATION" else{throw failure};checks.append("bounded_duration")}
    return ["status":"synthetic_tests_passed","checks":checks,"ui_reads":0,"ui_writes":0,"send_attempts":0,"live_send_permitted":false]
}

@main private struct Main {
    static func main() {
        do {
            let options=try parse(Array(CommandLine.arguments.dropFirst()))
            if options.selfTest {let result=try selfTest();print(String(decoding:try JSONSerialization.data(withJSONObject:result,options:[.sortedKeys]),as:UTF8.self));return}
            guard AXIsProcessTrusted() else{try stop("AX_PERMISSION_NOT_GRANTED")};try official(options,verifySignature:true)
            let context=Observer(options);var observer:AXObserver?
            let watchdog=DispatchWorkItem {
                let fixed="{\"status\":\"observer_time_limit\",\"live_send_permitted\":false,\"raw_values_output\":false,\"ui_writes\":0,\"send_attempts\":0}\n"
                fixed.withCString{pointer in _ = Darwin.write(STDOUT_FILENO,pointer,strlen(pointer))}
                _exit(78)
            }
            let callback:AXObserverCallback={_,element,_,pointer in guard let pointer else{return};let context=Unmanaged<Observer>.fromOpaque(pointer).takeUnretainedValue();context.events+=1;context.inspect(element)}
            let registration=AXObserverCreate(options.pid,callback,&observer)
            var notificationStatus:[String:Int32]=[:]
            if registration == .success,let observer {let pointer=Unmanaged.passUnretained(context).toOpaque();for name in [kAXFocusedUIElementChangedNotification,kAXFocusedWindowChangedNotification,kAXWindowCreatedNotification]{notificationStatus[name]=AXObserverAddNotification(observer,context.app,name as CFString,pointer).rawValue};CFRunLoopAddSource(CFRunLoopGetCurrent(),AXObserverGetRunLoopSource(observer),.defaultMode)}
            FileHandle.standardError.write(Data("READY_ACCOUNT_SOURCE_OBSERVER\n".utf8))
            let start=ProcessInfo.processInfo.systemUptime
            DispatchQueue.global().asyncAfter(deadline:.now()+options.duration,execute:watchdog)
            while !context.completed && ProcessInfo.processInfo.systemUptime-start<max(0,options.duration-0.5) {context.inspect(nil);RunLoop.current.run(until:Date().addingTimeInterval(0.15))}
            watchdog.cancel()
            if let observer {CFRunLoopRemoveSource(CFRunLoopGetCurrent(),AXObserverGetRunLoopSource(observer),.defaultMode)}
            var result=context.last;result["mode"]="readonly_account_source_observer";result["elapsed_seconds"]=round((ProcessInfo.processInfo.systemUptime-start)*1000)/1000;result["observer_registration_status"]=registration.rawValue;result["notification_status"]=notificationStatus;result["events_seen"]=context.events;result["expected_hash_matched"]=context.completed;result["identity_binding_established"]=false;result["live_send_permitted"]=false;result["ui_writes"]=0;result["send_attempts"]=0;result["raw_values_output"]=false
            print(String(decoding:try JSONSerialization.data(withJSONObject:result,options:[.sortedKeys]),as:UTF8.self))
        } catch let error as Failure {print("{\"status\":\"blocked\",\"code\":\"\(error.code)\",\"live_send_permitted\":false,\"send_attempts\":0}");exit(78)} catch {print("{\"status\":\"blocked\",\"code\":\"OBSERVER_FAILED\",\"live_send_permitted\":false,\"send_attempts\":0}");exit(78)}
    }
}
