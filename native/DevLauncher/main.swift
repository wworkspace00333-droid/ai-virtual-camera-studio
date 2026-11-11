// Development launcher for AI Virtual Camera Studio.
//
// macOS attributes camera (TCC) permission to the *responsible* app bundle. A bare `python`
// started from a terminal or IDE inherits that host's identity, which may lack
// NSCameraUsageDescription. This tiny bundle declares the usage string and spawns the
// project's venv Python as a child, so the system prompt names "AI Virtual Camera Studio Dev".
// The packaged release (Stage 13) replaces this with the real application bundle.
import Foundation

let info = Bundle.main.infoDictionary ?? [:]
guard let root = info["VCSProjectRoot"] as? String else {
    FileHandle.standardError.write("VCSProjectRoot missing from Info.plist\n".data(using: .utf8)!)
    exit(2)
}
let python = (root as NSString).appendingPathComponent(".venv/bin/python")
let proc = Process()
proc.executableURL = URL(fileURLWithPath: python)
proc.arguments = ["-m", "vcstudio"] + CommandLine.arguments.dropFirst()
proc.currentDirectoryURL = URL(fileURLWithPath: root)
var env = ProcessInfo.processInfo.environment
env["VCSTUDIO_LAUNCHER"] = "dev-app"
proc.environment = env

for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { proc.terminate() }
    src.resume()
    _ = Unmanaged.passRetained(src)
}
proc.terminationHandler = { p in exit(p.terminationStatus) }
do {
    try proc.run()
} catch {
    FileHandle.standardError.write("Failed to start \(python): \(error)\n".data(using: .utf8)!)
    exit(1)
}
dispatchMain()
