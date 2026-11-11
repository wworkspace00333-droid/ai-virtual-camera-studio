// System-wide hotkeys via Carbon RegisterEventHotKey (no Accessibility permission required).
// The handler runs on the main thread's event loop (Qt's Cocoa run loop delivers Carbon events).
import Carbon
import Foundation

public typealias VCSHotkeyCallback = @convention(c) (Int32) -> Void

private var hotkeyCallback: VCSHotkeyCallback?
private var hotkeyRefs: [Int32: EventHotKeyRef] = [:]
private var handlerInstalled = false

private func installHandler() -> OSStatus {
    if handlerInstalled { return noErr }
    var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
    let status = InstallEventHandler(GetApplicationEventTarget(), { _, event, _ -> OSStatus in
        var hk = EventHotKeyID()
        let r = GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                                  nil, MemoryLayout<EventHotKeyID>.size, nil, &hk)
        if r == noErr, let cb = hotkeyCallback { cb(Int32(hk.id)) }
        return noErr
    }, 1, &spec, nil, nil)
    if status == noErr { handlerInstalled = true }
    return status
}

@_cdecl("vcs_hotkeys_set_callback")
public func vcs_hotkeys_set_callback(_ cb: VCSHotkeyCallback?) {
    hotkeyCallback = cb
}

/// Register hotkey `id` for a macOS virtual key code + Carbon modifier mask. 0 = ok, else OSStatus.
@_cdecl("vcs_hotkey_register")
public func vcs_hotkey_register(_ id: Int32, _ keyCode: UInt32, _ modifiers: UInt32) -> Int32 {
    let s = installHandler()
    if s != noErr { return Int32(s) }
    if let old = hotkeyRefs.removeValue(forKey: id) { UnregisterEventHotKey(old) }
    var ref: EventHotKeyRef?
    let hkID = EventHotKeyID(signature: OSType(0x5643_5354), id: UInt32(id))   // 'VCST'
    let r = RegisterEventHotKey(keyCode, modifiers, hkID, GetApplicationEventTarget(), 0, &ref)
    if r == noErr, let ref = ref { hotkeyRefs[id] = ref }
    return Int32(r)
}

@_cdecl("vcs_hotkey_unregister_all")
public func vcs_hotkey_unregister_all() {
    for (_, ref) in hotkeyRefs { UnregisterEventHotKey(ref) }
    hotkeyRefs.removeAll()
}

@_cdecl("vcs_hotkey_count")
public func vcs_hotkey_count() -> Int32 { Int32(hotkeyRefs.count) }
