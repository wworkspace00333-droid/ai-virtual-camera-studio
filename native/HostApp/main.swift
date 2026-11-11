// AI Virtual Camera Studio — host application.
//
// 1. Contains the camera system extension (Contents/Library/SystemExtensions/) and activates or
//    deactivates it on request:   <app>/Contents/MacOS/AIVirtualCameraStudio --activate-camera
//                                 <app>/Contents/MacOS/AIVirtualCameraStudio --deactivate-camera
//    Exit codes: 0 done, 2 waiting for user approval in System Settings, 3 needs reboot, 1 error.
// 2. Otherwise launches the Python application as a child process, so macOS attributes camera
//    permission to this bundle (NSCameraUsageDescription). Development builds run the project's
//    .venv; packaged builds (Stage 13) run the embedded Python runtime.
import Foundation
import SystemExtensions

let extensionID = (Bundle.main.bundleIdentifier ?? "com.aivcstudio.app") + ".camera"

final class ExtensionRequester: NSObject, OSSystemExtensionRequestDelegate {
    func request(_ request: OSSystemExtensionRequest,
                 actionForReplacingExtension existing: OSSystemExtensionProperties,
                 withExtension ext: OSSystemExtensionProperties) -> OSSystemExtensionRequest.ReplacementAction {
        print("Replacing camera extension \(existing.bundleShortVersion) -> \(ext.bundleShortVersion)")
        return .replace
    }

    func requestNeedsUserApproval(_ request: OSSystemExtensionRequest) {
        print("NEEDS_APPROVAL: open System Settings > General > Login Items & Extensions > Camera Extensions "
              + "and enable \"AI Virtual Camera\".")
        fflush(stdout)
        exit(2)
    }

    func request(_ request: OSSystemExtensionRequest, didFinishWithResult result: OSSystemExtensionRequest.Result) {
        switch result {
        case .completed:
            print("OK")
            exit(0)
        case .willCompleteAfterReboot:
            print("REBOOT_REQUIRED")
            exit(3)
        @unknown default:
            print("UNKNOWN_RESULT \(result.rawValue)")
            exit(1)
        }
    }

    func request(_ request: OSSystemExtensionRequest, didFailWithError error: Error) {
        let e = error as NSError
        print("ERROR \(e.domain) \(e.code): \(e.localizedDescription)")
        exit(1)
    }
}

let args = CommandLine.arguments
if args.contains("--activate-camera") || args.contains("--deactivate-camera") {
    let delegate = ExtensionRequester()
    let req = args.contains("--activate-camera")
        ? OSSystemExtensionRequest.activationRequest(forExtensionWithIdentifier: extensionID, queue: .main)
        : OSSystemExtensionRequest.deactivationRequest(forExtensionWithIdentifier: extensionID, queue: .main)
    req.delegate = delegate
    OSSystemExtensionManager.shared.submitRequest(req)
    DispatchQueue.main.asyncAfter(deadline: .now() + 120) { print("TIMEOUT"); exit(1) }
    dispatchMain()
}

// ---- launch the Python application
let info = Bundle.main.infoDictionary ?? [:]
let resources = Bundle.main.resourceURL!
let embeddedPython = resources.appendingPathComponent("python/bin/python3").path
// Packaged build: the frozen (PyInstaller) payload sits next to this launcher in Contents/MacOS.
let frozenPayload = (Bundle.main.executableURL!.deletingLastPathComponent()
    .appendingPathComponent("vcstudio")).path
var pythonPath: String
var workDir: String
var pyArgs: [String] = ["-m", "vcstudio"]
if FileManager.default.isExecutableFile(atPath: frozenPayload) {
    pythonPath = frozenPayload
    workDir = resources.path
    pyArgs = []
} else if FileManager.default.isExecutableFile(atPath: embeddedPython) {
    pythonPath = embeddedPython
    workDir = resources.appendingPathComponent("app").path
} else if let root = info["VCSProjectRoot"] as? String {
    pythonPath = (root as NSString).appendingPathComponent(".venv/bin/python")
    workDir = root
} else {
    FileHandle.standardError.write("No Python runtime: neither embedded nor VCSProjectRoot\n".data(using: .utf8)!)
    exit(2)
}
let proc = Process()
proc.executableURL = URL(fileURLWithPath: pythonPath)
proc.arguments = pyArgs + args.dropFirst()
proc.currentDirectoryURL = URL(fileURLWithPath: workDir)
var env = ProcessInfo.processInfo.environment
env["VCSTUDIO_HOST_APP"] = Bundle.main.bundlePath
env["VCSTUDIO_HOST_EXECUTABLE"] = Bundle.main.executablePath ?? ""
proc.environment = env
for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { proc.terminate() }
    src.resume()
    _ = Unmanaged.passRetained(src)
}
proc.terminationHandler = { p in exit(p.terminationStatus) }
do { try proc.run() } catch {
    FileHandle.standardError.write("Failed to start Python: \(error)\n".data(using: .utf8)!)
    exit(1)
}
dispatchMain()
