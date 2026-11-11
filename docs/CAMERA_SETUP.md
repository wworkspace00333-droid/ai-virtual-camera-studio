# AI Virtual Camera: build, signing and installation

"AI Virtual Camera" is a CoreMediaIO **Camera Extension**, which is a system extension. macOS only loads
system extensions that are signed with an Apple-issued certificate and embedded in an app in `/Applications`.
That's why this one step needs Xcode and an Apple Developer team. Everything else in the project builds with
the Command Line Tools.

## One-time setup

1. **Install Xcode** from the App Store (or developer.apple.com). Open it once to accept the licence and install
   its components, then select it:
   ```bash
   sudo xcode-select -s /Applications/Xcode.app
   ```
2. **Join the Apple Developer Program** (paid membership). The `com.apple.developer.system-extension.install`
   entitlement needs a provisioning profile, which free Apple accounts cannot get.
3. **Sign in to Xcode:** go to Xcode > Settings > Accounts, click **+**, choose Apple ID, select your team, and click
   **Manage Certificates… > + > Apple Development**. This puts an "Apple Development" certificate in your login keychain.
4. **Find your Team ID** (10 characters). It's shown in Xcode > Settings > Accounts > (team), or on
   developer.apple.com > Account > Membership details.

## Build, install, activate

```bash
VCS_TEAM_ID=ABCDE12345 scripts/build_camera.sh
```

The script:
- generates the Xcode project from `native/project.yml` (XcodeGen);
- builds with automatic signing (`-allowProvisioningUpdates` registers the bundle IDs and the App Group
  `<TEAM>.com.aivcstudio.app.camera`);
- checks the signatures and entitlements;
- copies the app to `/Applications/AI Virtual Camera Studio.app`;
- asks macOS to activate the extension.

On the first install macOS blocks the extension until you approve it:

**System Settings > General > Login Items & Extensions > Camera Extensions**: turn on **AI Virtual Camera**.

The Virtual Camera page in the app shows the state and has an **Open approval settings** button. After approval,
"AI Virtual Camera" appears in camera menus. Already-running apps (Zoom, browsers) may need to be restarted
before they list it.

## Verify

1. In the app, open **Virtual Camera > Start test pattern**.
2. Open **QuickTime Player > File > New Movie Recording**, click the arrow next to the record button, and choose
   **AI Virtual Camera**. You should see moving colour bars with an increasing frame number. FaceTime, Photo Booth
   and Zoom work the same way.
3. **Run loopback test** captures the camera the way a client app does and reports delivered frames, order and latency.
   The same check runs as an automated test:
   ```bash
   VCS_LIVE=1 .venv/bin/python -m pytest tests/test_camera.py -s
   ```

## How frames get to the camera

The app keeps processed frames as IOSurface-backed CVPixelBuffers. These are produced by the Metal compositor.
The app enqueues them into the extension's hidden sink stream through the CoreMediaIO C API, the same mechanism
OBS uses. The extension then sends the newest frame on its public stream at the client's frame rate (60 or 30 fps;
1920×1080 or 1280×720), scaling in hardware if needed.

If no frame arrives for 1 second, the extension shows a "Waiting for AI Virtual Camera Studio…" card. Client apps
never lose the device when the studio app stops or restarts.

This transport was verified on this Mac against OBS's camera extension, which uses the same sink mechanism:
1080p60, 99.7% of frames delivered in order, 6.7 ms median and 17.9 ms p95 latency from sender to client.

## Updating and uninstalling

- **Update:** rerun the script. macOS replaces the extension when the version changes (bump `CURRENT_PROJECT_VERSION`
  in `native/project.yml` for each update you want to install).
- **Uninstall:** use **Virtual Camera > Uninstall**, or run
  `"/Applications/AI Virtual Camera Studio.app/Contents/MacOS/AIVirtualCameraStudio" --deactivate-camera`,
  then delete the app.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `xcodebuild` fails with "No Account for Team" or "No signing certificate" | Do setup steps 3–4. Check that the Team ID matches. |
| "Provisioning profile doesn't include the system-extension entitlement" | The team must be a paid Developer Program team. Rerun the script so Xcode refreshes the profiles. |
| Activation exits with `ERROR OSSystemExtensionErrorDomain 4` (extension not found) | The app must run from `/Applications`, and the extension must be embedded. The script checks both. |
| Exit code 2 / state "activated waiting for user" | Approve in System Settings > General > Login Items & Extensions > Camera Extensions. |
| Camera not listed in Zoom or a browser | Quit and reopen that app. |
| Camera shows "Waiting for AI Virtual Camera Studio…" | The studio isn't sending frames: start the test pattern or the pipeline. |
| `systemextensionsctl list` shows an old version stuck at "terminating for uninstall" | Restart the Mac. |
