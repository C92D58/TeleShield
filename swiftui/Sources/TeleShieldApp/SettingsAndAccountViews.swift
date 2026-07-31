import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct SettingsView: View {
    @ObservedObject var client: CoreClient
    @State private var settings = ScanSettings.defaults
    @State private var showLogoutConfirmation = false
    @State private var showClearConfirmation = false
    @State private var showClearCredentialsConfirmation = false

    var body: some View {
        Form {
            Section("啟動與防護") {
                VStack(alignment: .leading, spacing: 10) {
                    Toggle("登入系統時自動啟動 TeleShield", isOn: Binding<Bool>(get: { client.startupEnabled }, set: { value in Task { await client.setStartup(value) } }))
                    Toggle("啟用自動啟動防護", isOn: Binding<Bool>(
                        get: { !clientAutoStartAccountIDs.isEmpty },
                        set: { enabled in
                            let ids: [String] = enabled
                                ? (clientAutoStartAccountIDs.isEmpty ? configuredAccountIDs : Array(clientAutoStartAccountIDs))
                                : []
                            Task { await client.setAutoStartAccounts(accountIDs: ids) }
                        }
                    ))
                    .disabled(configuredAccountIDs.isEmpty)
                    Text("勾選的 Telegram 帳號會在背景啟動時個別開始防護。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    ForEach(client.status?.accounts ?? []) { account in
                        Toggle(account.label, isOn: Binding<Bool>(
                            get: { clientAutoStartAccountIDs.contains(account.id) },
                            set: { enabled in updateAutoStart(accountID: account.id, enabled: enabled) }
                        ))
                        .help(account.configured ? account.accountIdentifier : "尚未登入")
                        .disabled(!account.configured)
                    }
                    Text("關閉視窗只會隱藏到 Menu Bar，不會停止防護；要停止請使用防護頁或 Menu Bar。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Section("歷史掃描限制") {
                ScanStepper(title: "私訊對話數", value: $settings.privateDialogLimit, range: 1...100)
                ScanStepper(title: "每個私訊訊息數", value: $settings.privateMessageLimit, range: 1...100)
                ScanStepper(title: "私訊日期範圍", value: $settings.privateDays, range: 1...365)
                ScanStepper(title: "群組數", value: $settings.groupDialogLimit, range: 1...100)
                ScanStepper(title: "每個群組訊息數", value: $settings.groupMessageLimit, range: 1...100)
                ScanStepper(title: "群組日期範圍", value: $settings.groupDays, range: 1...365)
                Button("儲存掃描設定") { Task { await client.updateScanSettings(settings) } }
                    .buttonStyle(.borderedProminent)
            }
            Section("OCR") {
                HStack {
                    Label(client.status?.ocr.available == true ? "OCR 可用" : "OCR 尚未可用", systemImage: "text.viewfinder")
                    Spacer()
                    if let ocr = client.status?.ocr, ocr.available { Text(ocr.languages.joined(separator: "+")).foregroundStyle(.secondary) }
                    Button("重新檢查") { Task { await client.refreshOCR() } }
                }
            }
            Section("Telegram Session") {
                Button("登出 Telegram（保留 API 設定）", role: .destructive) { showLogoutConfirmation = true }
                    .disabled(!client.canModifySelectedAccount)
                Button("只刪除本機 Session", role: .destructive) { showClearConfirmation = true }
                    .disabled(!client.canModifySelectedAccount)
                Button("刪除 Session 與 API 設定", role: .destructive) { showClearCredentialsConfirmation = true }
                    .disabled(!client.canModifySelectedAccount)
                Text("這些操作只作用於目前選取帳號；即時防護執行中時會被鎖定。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .padding(20)
        .task {
            await client.refreshAccountData()
            settings = client.scanSettings
        }
        .confirmationDialog("登出目前 Telegram 帳號？", isPresented: $showLogoutConfirmation, titleVisibility: .visible) {
            Button("確認登出", role: .destructive) { Task { await client.logout(removeCredentials: false) } }
            Button("取消", role: .cancel) {}
        } message: { Text("API 設定會保留，但 Session 會失效，需要再次登入。") }
        .confirmationDialog("刪除本機 Session？", isPresented: $showClearConfirmation, titleVisibility: .visible) {
            Button("確認刪除", role: .destructive) { Task { await client.clearSession(removeCredentials: false) } }
            Button("取消", role: .cancel) {}
        }
        .confirmationDialog("刪除 Session 與 API 設定？", isPresented: $showClearCredentialsConfirmation, titleVisibility: .visible) {
            Button("確認全部刪除", role: .destructive) { Task { await client.clearSession(removeCredentials: true) } }
            Button("取消", role: .cancel) {}
        }
    }

    private var clientAutoStartAccountIDs: Set<String> {
        Set(client.details?.autoStartAccountIDs ?? [])
    }

    private var configuredAccountIDs: [String] {
        (client.status?.accounts ?? []).filter(\.configured).map(\.id)
    }

    private func updateAutoStart(accountID: String, enabled: Bool) {
        var ids = clientAutoStartAccountIDs
        if enabled {
            ids.insert(accountID)
        } else {
            ids.remove(accountID)
        }
        let orderedIDs = (client.status?.accounts ?? []).map(\.id).filter { ids.contains($0) }
        Task { await client.setAutoStartAccounts(accountIDs: orderedIDs) }
    }
}

struct ScanStepper: View {
    let title: String
    @Binding var value: Int
    let range: ClosedRange<Int>

    var body: some View {
        Stepper("\(title)：\(value)", value: $value, in: range)
    }
}

struct AccountsView: View {
    @ObservedObject var client: CoreClient
    @Binding var showLogin: Bool
    @State private var removalAccountID: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            PageHeader(title: "帳號", subtitle: "每個 Telegram 帳號都有獨立 Session 與政策資料") {
                Button("全部啟動") { Task { await client.startAll() } }
                Button("全部停止") { Task { await client.stopAll() } }
            }
            List(client.status?.accounts ?? []) { account in
                HStack(spacing: 12) {
                    AccountRow(account: account, selected: client.selectedAccount?.id == account.id)
                    Spacer()
                    Button(account.configured ? "重新登入" : "登入") {
                        Task { await client.selectAccount(account.id); showLogin = true }
                    }
                    .disabled(account.running || client.isBusy)
                    Button("移除", role: .destructive) {
                        removalAccountID = account.id
                    }
                    .disabled(account.running || client.isBusy)
                }
                .contentShape(Rectangle())
                .onTapGesture { Task { await client.selectAccount(account.id) } }
            }
            .listStyle(.inset)
        }
        .padding(28)
        .confirmationDialog("移除帳號？", isPresented: Binding(get: { removalAccountID != nil }, set: { if !$0 { removalAccountID = nil } }), titleVisibility: .visible) {
            Button("確認移除全部本機資料", role: .destructive) {
                if let accountID = removalAccountID {
                    Task { _ = await client.removeAccount(accountID) }
                }
                removalAccountID = nil
            }
            Button("取消", role: .cancel) { removalAccountID = nil }
        } message: {
            Text("會刪除所選帳號的 Session、設定、名單、群組與封鎖記錄；其他帳號不受影響。")
        }
    }
}

struct LoginSheet: View {
    @ObservedObject var client: CoreClient
    @Binding var temporaryAccountID: String?
    @Environment(\.dismiss) private var dismiss
    @State private var apiID = ""
    @State private var apiHash = ""
    @State private var phone = ""
    @State private var code = ""
    @State private var password = ""
    @State private var cleanupStarted = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text(client.selectedAccount?.configured == true ? "重新登入 Telegram" : "登入 Telegram").font(.title2.bold())
                    Text("目前帳號：\(client.selectedAccount?.label ?? "尚未選取")").foregroundStyle(.secondary)
                }
                Spacer()
                if let url = URL(string: "https://my.telegram.org/auth?to=apps") {
                    Link("MTProto API 註冊", destination: url)
                        .font(.callout.weight(.semibold))
                }
                Button("關閉") { dismiss() }
            }
            Divider()
            Text("API 憑證只會交給本機 Swift 服務；請勿把 Hash 或 2FA 密碼貼到聊天或記錄中。")
                .font(.callout).foregroundStyle(.secondary)
            if client.authChallengeKind == nil {
                TextField("API ID（正整數）", text: $apiID)
                SecureField("API Hash", text: $apiHash)
                TextField("手機號碼（含國碼）", text: $phone)
            }
            if !client.authDeliveryMessage.isEmpty {
                Text(client.authDeliveryMessage).font(.callout).foregroundStyle(.secondary)
            }
            if client.authChallengeKind == "code" {
                TextField("Telegram 驗證碼", text: $code)
            } else if client.authChallengeKind == "password" {
                SecureField("Telegram 兩步驟驗證密碼", text: $password)
            }
            HStack {
                if client.authChallengeKind == "code" {
                    Button("送出驗證碼") {
                        Task {
                            await client.submitAuthCode(code)
                            code = ""
                        }
                    }
                        .disabled(code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || client.isBusy)
                } else if client.authChallengeKind == "password" {
                    Button("送出 2FA 密碼") {
                        Task {
                            await client.submitAuthPassword(password)
                            password = ""
                        }
                    }
                        .disabled(password.isEmpty || client.isBusy)
                } else {
                    Button("開始登入") {
                        Task {
                            let accountID = temporaryAccountID ?? client.selectedAccountID
                            await client.startAuthentication(
                                apiID: apiID,
                                apiHash: apiHash,
                                phone: phone,
                                accountID: accountID
                            )
                            apiID = ""
                            apiHash = ""
                            phone = ""
                        }
                    }
                        .buttonStyle(.borderedProminent)
                        .disabled(apiID.isEmpty || apiHash.isEmpty || phone.isEmpty || client.isBusy || client.authInProgress)
                }
                if client.authFlowID != nil {
                    Button("取消登入") { dismiss() }
                }
                Spacer()
                if !client.authInProgress && client.selectedAccount?.configured == true {
                    Button("完成") { temporaryAccountID = nil; dismiss() }
                }
            }
            if let error = client.errorMessage { Text(error).foregroundStyle(.red).font(.callout) }
        }
        .padding(28)
        .frame(width: 520)
        .onChange(of: client.authInProgress) { inProgress in
            if !inProgress {
                apiHash = ""
                code = ""
                password = ""
            }
        }
        .onAppear { cleanupStarted = false }
        .onDisappear { cleanupAfterDismissal() }
    }

    private func cleanupAfterDismissal() {
        guard !cleanupStarted else { return }
        cleanupStarted = true
        Task {
            if client.authInProgress || client.authFlowID != nil {
                await client.cancelAuthentication()
            }
            guard let accountID = temporaryAccountID else { return }
            if client.authenticatedAccountID == accountID {
                temporaryAccountID = nil
                return
            }
            if await client.removeAccount(accountID, deleteFiles: true) {
                temporaryAccountID = nil
            }
        }
    }
}

struct AccountRow: View {
    let account: AccountSummary
    let selected: Bool

    var body: some View {
        HStack(spacing: 9) {
            Image(systemName: account.configured ? "person.crop.circle.fill" : "person.crop.circle")
                .font(.title3)
                .foregroundStyle(selected ? .blue : .secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(account.label).font(.body.weight(.medium))
                Text(account.configured ? (account.phoneMasked.isEmpty ? account.username : account.phoneMasked) : "尚未登入")
                    .font(.caption).foregroundStyle(.secondary)
                AccountStatusBadge(account: account)
            }
            Spacer()
            if selected {
                Image(systemName: "checkmark.circle.fill")
                    .foregroundStyle(.blue)
                    .accessibilityLabel("目前使用中")
            }
        }
        .padding(.vertical, 7)
        .padding(.horizontal, 8)
        .background(selected ? Color.accentColor.opacity(0.13) : .clear, in: RoundedRectangle(cornerRadius: 8))
        .contentShape(Rectangle())
    }
}

struct CurrentAccountBar: View {
    @ObservedObject var client: CoreClient

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "arrow.left.arrow.right.circle.fill")
                .font(.title3)
                .foregroundStyle(.blue)

            VStack(alignment: .leading, spacing: 2) {
                Text("目前工作帳號")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if let account = client.selectedAccount {
                    HStack(spacing: 7) {
                        Text(account.label)
                            .font(.body.weight(.semibold))
                        AccountStatusBadge(account: account)
                    }
                } else {
                    Text("尚未選取帳號")
                        .font(.body.weight(.semibold))
                }
            }

            Spacer()
            AccountSwitcher(client: client)
        }
        .padding(.horizontal, 28)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.bar)
        .accessibilityElement(children: .contain)
    }
}

struct AccountSwitcher: View {
    @ObservedObject var client: CoreClient

    private var accounts: [AccountSummary] { client.status?.accounts ?? [] }
    private var isSwitching: Bool { client.busyOperation == "切換帳號" }

    var body: some View {
        Menu {
            if accounts.isEmpty {
                Text("尚未建立 Telegram 帳號")
            } else {
                ForEach(accounts) { account in
                    Button {
                        guard account.id != client.selectedAccount?.id else { return }
                        Task { await client.selectAccount(account.id) }
                    } label: {
                        Label {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(account.label)
                                Text(account.accountIdentifier)
                                    .font(.caption)
                            }
                        } icon: {
                            Image(systemName: account.id == client.selectedAccount?.id ? "checkmark.circle.fill" : "person.crop.circle")
                        }
                    }
                    .disabled(client.isBusy)
                }
            }
        } label: {
            HStack(spacing: 7) {
                if isSwitching {
                    ProgressView()
                        .controlSize(.small)
                    Text("切換中…")
                } else {
                    Image(systemName: "arrow.left.arrow.right")
                    Text("切換帳號")
                }
                Image(systemName: "chevron.down")
                    .font(.caption.weight(.bold))
            }
        }
        .buttonStyle(.bordered)
        .disabled(client.isBusy || accounts.isEmpty)
        .help("切換目前工作帳號")
        .accessibilityLabel(isSwitching ? "正在切換工作帳號" : "切換目前工作帳號")
    }
}

struct AccountStatusBadge: View {
    let account: AccountSummary

    var body: some View {
        HStack(spacing: 4) {
            Circle()
                .fill(account.statusColor)
                .frame(width: 6, height: 6)
            Text(account.statusLabel)
        }
        .font(.caption2.weight(.medium))
        .foregroundStyle(account.statusColor)
    }
}

private extension AccountSummary {
    var accountIdentifier: String {
        if !phoneMasked.isEmpty { return phoneMasked }
        if !username.isEmpty { return "@\(username)" }
        return "Telegram 帳號"
    }

    var statusLabel: String {
        if !configured { return "未登入" }
        if let error, !error.isEmpty { return "需要注意" }
        if running { return "防護中" }
        if ready { return "已就緒" }
        return "已停止"
    }

    var statusColor: Color {
        if !configured { return .secondary }
        if let error, !error.isEmpty { return .red }
        if running { return .orange }
        if ready { return .green }
        return .secondary
    }
}

struct MetricCard: View {
    let title: String
    let value: String
    let icon: String

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            Label(title, systemImage: icon).font(.callout).foregroundStyle(.secondary)
            Text(value).font(.title2.bold().monospacedDigit())
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 12))
    }
}

struct EmptyPanel: View {
    let title: String
    let message: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.title3.bold())
            Text(message).foregroundStyle(.secondary)
        }
        .padding(24)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 14))
    }
}

struct MenuBarView: View {
    @ObservedObject var client: CoreClient
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("TeleShield").font(.headline)
            Text(client.connectionMessage).font(.caption).foregroundStyle(.secondary)
            Divider()
            Button("開啟 TeleShield") { openMainWindow() }
            if let account = client.selectedAccount, account.configured {
                Button(account.running ? "停止防護" : "啟動防護") {
                    Task { if account.running { await client.stopProtection() } else { await client.startProtection() } }
                }
                Button("重新整理") { Task { await client.refresh() } }
            }
            Button("結束 TeleShield") {
                Task {
                    await client.shutdownGracefully()
                    NSApplication.shared.terminate(nil)
                }
            }
        }
        .padding(12)
        .frame(width: 230)
        // MenuBarExtra can be the first and only surface in background mode;
        // do not depend on a WindowGroup appearing before the service starts.
        .onAppear { client.launch() }
        .onReceive(NotificationCenter.default.publisher(for: .teleShieldOpenMainWindow)) { _ in
            openMainWindow()
        }
    }

    private func openMainWindow() {
        let windows = NSApp.windows
            .filter { $0.title == "TeleShield" && $0.styleMask.contains(.titled) }
            .sorted { lhs, rhs in
                if lhs.isVisible != rhs.isVisible { return lhs.isVisible }
                return lhs.isKeyWindow && !rhs.isKeyWindow
            }

        if let mainWindow = windows.first {
            // Reuse the existing SwiftUI window. Calling openWindow every time
            // would create another WindowGroup instance instead of restoring it.
            for duplicate in windows.dropFirst() {
                duplicate.close()
            }
            reveal(mainWindow)
            return
        }

        // The red close button may have removed the WindowGroup instance.
        // Only create one when there is no existing main window at all.
        openWindow(id: "main")
        DispatchQueue.main.async {
            if let mainWindow = NSApp.windows.first(where: { $0.title == "TeleShield" && $0.styleMask.contains(.titled) }) {
                reveal(mainWindow)
            }
        }
    }

    private func reveal(_ window: NSWindow) {
        if window.isMiniaturized { window.deminiaturize(nil) }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }
}

@MainActor
func openPanel(extensions: [String]) -> URL? {
    let panel = NSOpenPanel()
    panel.canChooseFiles = true
    panel.canChooseDirectories = false
    panel.allowsMultipleSelection = false
    panel.allowedContentTypes = extensions.compactMap { UTType(filenameExtension: $0) }
    return panel.runModal() == .OK ? panel.url : nil
}

@MainActor
func savePanel(fileExtension: String, name: String) -> URL? {
    let panel = NSSavePanel()
    panel.nameFieldStringValue = name
    if let type = UTType(filenameExtension: fileExtension) {
        panel.allowedContentTypes = [type]
    }
    return panel.runModal() == .OK ? panel.url : nil
}
