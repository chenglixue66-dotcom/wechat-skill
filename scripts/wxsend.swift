// Compatibility source: the old keyboard sender is retired.
// Compile wxprobe.swift for read-only diagnostics. No live sending in this file.
import Foundation
let report = "{\"ok\":false,\"code\":\"LIVE_SEND_DISABLED\",\"send_attempts\":0,\"writes_performed\":0,\"reason\":\"Use wxsend --probe; recipient binding is not validated.\"}"
print(report)
exit(78)
