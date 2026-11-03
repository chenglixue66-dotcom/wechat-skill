// 用辅助功能接口（AX）给微信会话发文字和图片，不截图、不点坐标。
// 用法：wxsend [--dry] <会话名> [--text 文字] [--image 图片路径] ...（按参数顺序依次发）
// --dry：走完全部步骤但不按回车，每条写进输入框后核对，再清空——用来测试，不会发出任何消息。
// 会话名必须和聊天输入框的标题完全一致才发；对不上、输入框有草稿、写入没生效都直接中止。
// 退出码 0 = 全部发出；其他 = 中止，见输出。
import ApplicationServices; import AppKit

var args = Array(CommandLine.arguments.dropFirst())
let dry = args.first == "--dry"
if dry { args.removeFirst() }
guard let target = args.first else { print("用法：wxsend <会话名> [--text 文字] [--image 路径]"); exit(64) }
var jobs: [(String, String)] = []
var i = 1
while i + 1 < args.count { jobs.append((args[i], args[i + 1])); i += 2 }
guard !jobs.isEmpty, jobs.allSatisfy({ $0.0 == "--text" || $0.0 == "--image" }) else { print("参数不对"); exit(64) }
guard AXIsProcessTrusted() else { print("没有辅助功能权限"); exit(10) }
guard let wx = NSRunningApplication.runningApplications(withBundleIdentifier: "com.tencent.xinWeChat").first else { print("微信没开"); exit(11) }
let app = AXUIElementCreateApplication(wx.processIdentifier)

func attr(_ e: AXUIElement, _ a: String) -> AnyObject? { var v: AnyObject?; AXUIElementCopyAttributeValue(e, a as CFString, &v); return v }
func find(_ ok: (String, String) -> Bool) -> AXUIElement? {
  func walk(_ e: AXUIElement) -> AXUIElement? {
    let r = attr(e, kAXRoleAttribute) as? String ?? ""
    let t = (attr(e, kAXTitleAttribute) as? String) ?? (attr(e, kAXDescriptionAttribute) as? String) ?? ""
    if ok(r, t) { return e }
    for c in (attr(e, kAXChildrenAttribute) as? [AXUIElement]) ?? [] { if let f = walk(c) { return f } }
    return nil
  }
  for w in (attr(app, kAXWindowsAttribute) as? [AXUIElement]) ?? [] { if let f = walk(w) { return f } }
  return nil
}
func key(_ code: CGKeyCode, _ flags: CGEventFlags = []) {
  for down in [true, false] { let e = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: down)!; e.flags = flags; e.post(tap: .cghidEventTap); usleep(40000) }
}
func pause(_ s: Double) { usleep(useconds_t(s * 1_000_000)) }
func inputBox() -> AXUIElement? { find { r, t in r == "AXTextArea" && t != "搜索" } }
// 输入框标题 = 会话名；输入框空着且有焦点时，微信会在后面接上提示语（如「长按触控板 语音输入」），去掉再比
func title() -> String? {
  guard let t = inputBox().flatMap({ attr($0, kAXTitleAttribute) as? String }) else { return nil }
  return t.replacingOccurrences(of: "长按.*语音输入$", with: "", options: .regularExpression)
}
func value(_ e: AXUIElement) -> String { attr(e, kAXValueAttribute) as? String ?? "" }

if CGSessionCopyCurrentDictionary().flatMap({ ($0 as NSDictionary)["CGSSessionScreenIsLocked"] as? Bool }) == true { print("屏幕锁着，发不了"); exit(12) }
// 新版 macOS 不让后台进程直接抢前台，用 open -a 走系统的「打开应用」
let opener = Process(); opener.executableURL = URL(fileURLWithPath: "/usr/bin/open"); opener.arguments = ["-b", "com.tencent.xinWeChat"]
try? opener.run(); opener.waitUntilExit()
for _ in 0..<20 where NSWorkspace.shared.frontmostApplication?.processIdentifier != wx.processIdentifier { pause(0.2) }
guard NSWorkspace.shared.frontmostApplication?.processIdentifier == wx.processIdentifier else { print("微信切不到前台"); exit(13) }

// 1. 进会话：逐个试搜索结果（第 k 个 = 下移 k 次再回车），用输入框标题核对
key(53); pause(0.3)
var k = 0
while title() != target && k < 6 {
  guard let search = find({ r, t in r == "AXTextArea" && t == "搜索" }) else { print("找不到搜索框"); exit(20) }
  AXUIElementSetAttributeValue(search, kAXFocusedAttribute as CFString, kCFBooleanTrue); pause(0.4)
  AXUIElementSetAttributeValue(search, kAXValueAttribute as CFString, target as CFString); pause(1.8)
  for _ in 0..<k { key(125); pause(0.25) }
  key(36); pause(1.5)
  print("试第 \(k + 1) 个结果 → \(title() ?? "?")")
  k += 1
}
guard title() == target, let box = inputBox() else { key(53); print("找不到会话「\(target)」，没有发送"); exit(21) }
AXUIElementSetAttributeValue(box, kAXFocusedAttribute as CFString, kCFBooleanTrue); pause(0.3)
guard value(box).isEmpty else { print("输入框里有草稿，不覆盖，没有发送"); exit(22) }

// 2. 依次发送；每条发之前都再核对一次会话名
let saved = NSPasteboard.general.pasteboardItems?.compactMap { item -> NSPasteboardItem? in
  let copy = NSPasteboardItem(); for t in item.types { if let d = item.data(forType: t) { copy.setData(d, forType: t) } }; return copy } ?? []
defer { NSPasteboard.general.clearContents(); if !saved.isEmpty { NSPasteboard.general.writeObjects(saved) } }
var sent = 0
for (kind, payload) in jobs {
  guard title() == target else { print("会话变了（\(title() ?? "?")），停止；已发 \(sent) 条"); exit(30) }
  if kind == "--text" {
    AXUIElementSetAttributeValue(box, kAXValueAttribute as CFString, payload as CFString); pause(0.5)
    guard value(box) == payload else { print("文字写入没生效，停止；已发 \(sent) 条"); exit(31) }
  } else {
    guard let img = NSImage(contentsOfFile: payload) else { print("图片读不了：\(payload)"); exit(32) }
    NSPasteboard.general.clearContents(); NSPasteboard.general.writeObjects([img]); pause(0.3)
    AXUIElementSetAttributeValue(box, kAXFocusedAttribute as CFString, kCFBooleanTrue); pause(0.2)
    key(9, .maskCommand); pause(1.5)   // ⌘V
  }
  let name = kind == "--text" ? "文字" : "图片"
  let filled = value(box)
  guard !filled.isEmpty else { print("\(name)没有进输入框，停止；已发 \(sent) 条"); exit(33) }
  if dry {
    print("[试运行] \(name)已进输入框（\(filled.count) 字符，图片显示为占位符），不发送，清空")
    key(0, .maskCommand); pause(0.2); key(51); pause(0.5)
    guard value(box).isEmpty else { print("[试运行] 清空失败，请手动清掉输入框"); exit(34) }
    continue
  }
  key(36); pause(2.0)
  guard value(box).isEmpty else { print("\(name)回车后输入框没清空，可能没发出去，停止；已发 \(sent) 条"); exit(35) }
  print("\(name)已发出")
  sent += 1
}
print(dry ? "[试运行] 完成，没有发出任何消息" : "全部发出：\(sent) 条")
