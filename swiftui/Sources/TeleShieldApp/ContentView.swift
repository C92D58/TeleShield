import AppKit
import SwiftUI
import UniformTypeIdentifiers

private enum AppSection: String, CaseIterable, Hashable, Identifiable {
    case overview = "總覽"
    case protection = "防護與掃描"
    case lists = "白名單／黑名單"
    case rules = "學習規則"
    case reports = "報告"
    case records = "封鎖記錄"
    case groups = "群組管理"
    case settings = "設定"
    case accounts = "帳號"

    var id: String { rawValue }
    var icon: String {
        switch self {
        case .overview: return "rectangle.3.group"
        case .protection: return "shield.lefthalf.filled"
        case .lists: return "person.2.badge.gearshape"
        case .rules: return "text.magnifyingglass"
        case .reports: return "chart.bar.xaxis"
        case .records: return "list.bullet.rectangle"
        case .groups: return "person.3"
        case .settings: return "gearshape"
        case .accounts: return "person.crop.circle"
        }
    }
}

struct ContentView: View {
    @ObservedObject var client: CoreClient
    @State private var section: AppSection? = .overview
    @State private var showLogin = false
    @State private var temporaryLoginAccountID: String?

    var body: some View {
        NavigationSplitView {
            sidebar
        } detail: {
            VStack(spacing: 0) {
                CurrentAccountBar(client: client)
                Divider()
                detailView
            }
        }
        .frame(minWidth: 1080, minHeight: 700)
        .onAppear { client.launch() }
        .task {
            while !Task.isCancelled {
                await client.refresh()
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
        .sheet(isPresented: $showLogin) {
            LoginSheet(client: client, temporaryAccountID: $temporaryLoginAccountID)
        }
        .onChange(of: client.authenticatedAccountID) { accountID in
            let targetAccountID = temporaryLoginAccountID ?? client.selectedAccountID
            guard AuthenticationPresentation.shouldDismissLoginSheet(
                event: "auth_succeeded",
                accountID: accountID,
                targetAccountID: targetAccountID
            ) else { return }
            temporaryLoginAccountID = nil
            showLogin = false
        }
    }

    private var sidebar: some View {
        List(selection: $section) {
            Section("工作帳號") {
                ForEach(client.status?.accounts ?? []) { account in
                    Button {
                        Task { await client.selectAccount(account.id) }
                    } label: {
                        AccountRow(account: account, selected: client.selectedAccount?.id == account.id)
                    }
                    .buttonStyle(.plain)
                }
                Button {
                    Task {
                        if let accountID = await client.createAccount() {
                            temporaryLoginAccountID = accountID
                            section = .accounts
                            showLogin = true
                        }
                    }
                } label: {
                    Label("新增 Telegram 帳號", systemImage: "plus.circle")
                }
                .disabled(client.isBusy)
            }

            Section("工作區") {
                ForEach(AppSection.allCases) { item in
                    Label(item.rawValue, systemImage: item.icon)
                        .tag(item)
                }
            }
        }
        .listStyle(.sidebar)
        .navigationTitle("TeleShield")
        .safeAreaInset(edge: .bottom) {
            VStack(alignment: .leading, spacing: 7) {
                Label(client.connectionMessage, systemImage: client.helperIsRunning ? "circle.fill" : "circle")
                    .font(.caption)
                    .foregroundStyle(client.helperIsRunning ? .green : .secondary)
                if let account = client.selectedAccount {
                    Text(account.running ? "\(account.label) 正在防護" : "\(account.label) 已停止")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(12)
            .background(.bar)
        }
    }

    @ViewBuilder
    private var detailView: some View {
        switch section ?? .overview {
        case .overview: OverviewView(client: client, showLogin: $showLogin)
        case .protection: ProtectionView(client: client, showLogin: $showLogin)
        case .lists: ListManagementView(client: client)
        case .rules: RulesView(client: client)
        case .reports: ReportView(client: client)
        case .records: BlockRecordsView(client: client)
        case .groups: GroupsView(client: client)
        case .settings: SettingsView(client: client)
        case .accounts: AccountsView(client: client, showLogin: $showLogin)
        }
    }
}

struct PageHeader<Content: View>: View {
    let title: String
    let subtitle: String
    let content: () -> Content

    init(title: String, subtitle: String, @ViewBuilder content: @escaping () -> Content) {
        self.title = title
        self.subtitle = subtitle
        self.content = content
    }

    var body: some View {
        HStack(alignment: .top, spacing: 18) {
            VStack(alignment: .leading, spacing: 5) {
                Text(title).font(.largeTitle.bold())
                Text(subtitle).foregroundStyle(.secondary)
            }
            Spacer()
            content()
        }
    }
}
