import AppKit
import SwiftUI

final class TeleShieldAppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        if ProcessInfo.processInfo.arguments.contains("--background") {
            DispatchQueue.main.async {
                NSApp.hide(nil)
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        // MenuBarExtra remains the recoverable surface; closing a window is not quitting.
        false
    }
}

@main
struct TeleShieldApp: App {
    @NSApplicationDelegateAdaptor(TeleShieldAppDelegate.self) private var appDelegate
    @StateObject private var client = CoreClient()

    var body: some Scene {
        WindowGroup("TeleShield") {
            ContentView(client: client)
        }
        .commands {
            CommandGroup(replacing: .appTermination) {
                Button("結束 TeleShield") {
                    client.shutdown()
                    NSApplication.shared.terminate(nil)
                }
                .keyboardShortcut("q")
            }
        }

        MenuBarExtra("TeleShield", systemImage: "shield.lefthalf.filled") {
            MenuBarView(client: client)
        }
    }
}
