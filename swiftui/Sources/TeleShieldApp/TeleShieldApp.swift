import AppKit
import SwiftUI

extension Notification.Name {
    static let teleShieldOpenMainWindow = Notification.Name("TeleShieldOpenMainWindow")
}

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

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        guard !flag else { return true }
        NotificationCenter.default.post(name: .teleShieldOpenMainWindow, object: nil)
        return true
    }
}

@main
struct TeleShieldApp: App {
    @NSApplicationDelegateAdaptor(TeleShieldAppDelegate.self) private var appDelegate
    @StateObject private var client = CoreClient()

    var body: some Scene {
        WindowGroup("TeleShield", id: "main") {
            ContentView(client: client)
        }
        .commands {
            CommandGroup(replacing: .appTermination) {
                Button("結束 TeleShield") {
                    Task {
                        await client.shutdownGracefully()
                        NSApplication.shared.terminate(nil)
                    }
                }
                .keyboardShortcut("q")
            }
        }

        MenuBarExtra("TeleShield", systemImage: "shield.lefthalf.filled") {
            MenuBarView(client: client)
        }
    }
}
