// AI Virtual Camera — CoreMediaIO Camera Extension entry point.
import CoreMediaIO
import Foundation

let providerSource = VCProviderSource(clientQueue: nil)
CMIOExtensionProvider.startService(provider: providerSource.provider)
CFRunLoopRun()
