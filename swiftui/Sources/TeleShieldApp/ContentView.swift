import AppKit
import SwiftUI

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

private struct PageHeader<Content: View>: View {
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

private struct OverviewView: View {
    @ObservedObject var client: CoreClient
    @Binding var showLogin: Bool

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                PageHeader(title: "總覽", subtitle: "先看目前風險，再決定要啟動防護或處理歷史資料") {
                    Button { Task { await client.refresh() } } label: {
                        Label("重新整理", systemImage: "arrow.clockwise")
                    }
                    .disabled(client.isBusy)
                }
                if let account = client.selectedAccount, account.configured {
                    ProtectionSummary(client: client, account: account, showLogin: $showLogin)
                    EventLogCard(client: client)
                } else {
                    OnboardingCard(client: client, showLogin: $showLogin)
                }
            }
            .padding(28)
        }
    }
}

private struct ProtectionView: View {
    @ObservedObject var client: CoreClient
    @Binding var showLogin: Bool

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                PageHeader(title: "防護與掃描", subtitle: "即時防護與歷史掃描共用同一個 Telegram Session，不能同時執行") {
                    if client.selectedAccount?.configured == true {
                        Button {
                            Task {
                                if client.selectedAccount?.running == true { await client.stopProtection() }
                                else { await client.startProtection() }
                            }
                        } label: {
                            Label(client.selectedAccount?.running == true ? "停止防護" : "啟動防護", systemImage: client.selectedAccount?.running == true ? "stop.fill" : "play.fill")
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(client.isBusy)
                    }
                }
                if let account = client.selectedAccount, account.configured {
                    ProtectionSummary(client: client, account: account, showLogin: $showLogin)
                    HistoryScanView(client: client)
                } else {
                    OnboardingCard(client: client, showLogin: $showLogin)
                }
            }
            .padding(28)
        }
    }
}

private struct ProtectionSummary: View {
    @ObservedObject var client: CoreClient
    let account: AccountSummary
    @Binding var showLogin: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Label(account.ready ? "防護執行中" : (account.running ? "正在啟動" : "防護已停止"), systemImage: account.ready ? "checkmark.shield.fill" : "shield")
                        .font(.title2.bold())
                        .foregroundStyle(account.ready ? .green : (account.running ? .orange : .secondary))
                    Text(account.displayName.isEmpty ? "@\(account.username)" : account.displayName)
                        .foregroundStyle(.secondary)
                    if let userID = account.userID { Text("Telegram ID：\(userID)").font(.caption).foregroundStyle(.tertiary) }
                }
                Spacer()
                Button("重新登入") { showLogin = true }
                    .disabled(account.running || client.isBusy)
            }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 12)], spacing: 12) {
                MetricCard(title: "24 小時封鎖", value: "\(account.recentBlockCount)", icon: "calendar")
                MetricCard(title: "累計私訊封鎖", value: "\(account.blockedCount)", icon: "hand.raised")
                MetricCard(title: "累計群組處理", value: "\(account.kickedCount)", icon: "person.3")
                MetricCard(title: "白名單", value: "\(account.whitelistCount)", icon: "checkmark.shield")
                MetricCard(title: "黑名單", value: "\(account.blacklistCount)", icon: "nosign")
                MetricCard(title: "學習關鍵詞", value: "\(account.learnedKeywordCount)", icon: "text.magnifyingglass")
            }
        }
        .padding(20)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16))
    }
}

private struct OnboardingCard: View {
    @ObservedObject var client: CoreClient
    @Binding var showLogin: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("先建立一個 Telegram 帳號", systemImage: "person.crop.circle.badge.plus")
                .font(.title2.bold())
            Text("每個帳號都有獨立 Session、設定、名單、群組與封鎖記錄。建立後再進入登入流程。")
                .foregroundStyle(.secondary)
            Button {
                Task {
                    if await client.createAccount() != nil { showLogin = true }
                }
            } label: {
                Label("建立並登入", systemImage: "plus.circle.fill")
            }
            .buttonStyle(.borderedProminent)
            .disabled(client.isBusy)
        }
        .padding(24)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16))
    }
}

private struct EventLogCard: View {
    @ObservedObject var client: CoreClient

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label("最近活動", systemImage: "list.bullet.rectangle")
                    .font(.headline)
                Spacer()
                Text("最多保留 300 筆")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }
            if client.eventLog.isEmpty {
                Text("尚無事件。啟動防護、掃描或管理名單後，操作紀錄會顯示在這裡。")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(Array(client.eventLog.suffix(12).reversed())) { event in
                    HStack(alignment: .top, spacing: 8) {
                        Circle().fill(event.level == "error" ? .red : (event.level == "stderr" ? .orange : .blue)).frame(width: 7, height: 7).padding(.top, 5)
                        Text(event.message).font(.callout.monospaced())
                        Spacer()
                        Text(event.time, style: .time).font(.caption2).foregroundStyle(.tertiary)
                    }
                }
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 14))
    }
}

private struct HistoryScanView: View {
    @ObservedObject var client: CoreClient
    @State private var scope = "private"
    @State private var dryRun = true
    @State private var showApplyConfirmation = false
    @State private var showListConfirmation: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 15) {
            HStack {
                Label("歷史訊息掃描", systemImage: "magnifyingglass.circle")
                    .font(.title3.bold())
                Spacer()
                if client.hasActiveScan {
                    Button("停止掃描") { Task { await client.cancelScan() } }
                        .buttonStyle(.bordered)
                }
            }
            HStack {
                Picker("範圍", selection: $scope) {
                    Text("私訊（\(client.scanSettings.privateDialogLimit) 對話／\(client.scanSettings.privateMessageLimit) 訊息）").tag("private")
                    Text("群組（\(client.scanSettings.groupDialogLimit) 群組／\(client.scanSettings.groupMessageLimit) 訊息）").tag("group")
                }
                .frame(maxWidth: 430)
                Toggle("預覽模式（不封鎖／不踢除）", isOn: $dryRun)
            }
            HStack {
                Button(dryRun ? "開始預覽" : "開始處理") {
                    if dryRun { Task { await client.startScan(scope: scope, dryRun: true) } }
                    else { showApplyConfirmation = true }
                }
                .buttonStyle(.borderedProminent)
                .disabled(client.selectedAccount?.running == true || client.hasActiveScan || client.isBusy)
                if let result = client.scanResult {
                    Text("匹配 \(result.matched)／已處理 \(result.acted)")
                        .font(.callout.bold())
                        .foregroundStyle(result.dryRun ? .orange : .green)
                }
            }
            if !client.scanProgress.isEmpty {
                ScrollView {
                    VStack(alignment: .leading, spacing: 5) {
                        ForEach(Array(client.scanProgress.suffix(14).enumerated()), id: \.offset) { _, message in
                            Text(message).font(.caption.monospaced())
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(height: 150)
                .padding(10)
                .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
            }
            if let result = client.scanResult, !result.findings.isEmpty {
                HStack {
                    Text("發現項目：\(result.findings.count)").font(.headline)
                    Spacer()
                    Button("加入黑名單") { showListConfirmation = "blacklist" }
                    Button("加入白名單") { showListConfirmation = "whitelist" }
                }
                ForEach(result.findings) { finding in
                    HStack {
                        Text(finding.name).fontWeight(.medium)
                        Text(finding.userID).font(.caption.monospaced()).foregroundStyle(.secondary)
                        if let group = finding.group { Text(group).font(.caption).foregroundStyle(.secondary) }
                        Spacer()
                        Text(finding.reason).font(.caption).lineLimit(1).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .padding(18)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 14))
        .confirmationDialog("即將對 Telegram 執行封鎖／踢除", isPresented: $showApplyConfirmation, titleVisibility: .visible) {
            Button("確認開始處理", role: .destructive) { Task { await client.startScan(scope: scope, dryRun: false) } }
            Button("取消", role: .cancel) {}
        } message: {
            Text("這會對掃描匹配的帳號執行實際處理，並寫入封鎖記錄。建議先用預覽模式確認結果。")
        }
        .confirmationDialog("將掃描發現加入名單", isPresented: Binding(get: { showListConfirmation != nil }, set: { if !$0 { showListConfirmation = nil } }), titleVisibility: .visible) {
            Button("確認加入") {
                if let list = showListConfirmation, let findings = client.scanResult?.findings { Task { await client.addFindings(findings, to: list) } }
                showListConfirmation = nil
            }
            Button("取消", role: .cancel) { showListConfirmation = nil }
        }
    }
}

private struct ListManagementView: View {
    @ObservedObject var client: CoreClient
    @State private var listType = "whitelist"
    @State private var query = ""
    @State private var userID = ""
    @State private var username = ""
    @State private var reason = "desktop"
    @State private var selectedIDs: Set<String> = []
    @State private var importReplace = false
    @State private var showDeleteConfirmation = false

    private var rows: [ListEntry] { listType == "whitelist" ? client.whitelist : client.blacklist }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            PageHeader(title: "白名單／黑名單", subtitle: "先保護例外，再處理自動偵測到的帳號") {
                Picker("名單", selection: $listType) {
                    Text("白名單").tag("whitelist")
                    Text("黑名單").tag("blacklist")
                }
                .frame(width: 150)
            }
            HStack {
                TextField("搜尋 ID、Username 或原因", text: $query)
                Button("重新整理") { Task { await client.fetchList(listType, query: query) } }
            }
            HStack {
                TextField("Telegram User ID", text: $userID).frame(width: 170)
                TextField("Username", text: $username).frame(width: 170)
                TextField("原因", text: $reason)
                Button("新增／更新") {
                    Task { await client.upsertList(listType, userID: userID, username: username, reason: reason); userID = ""; username = "" }
                }
                .buttonStyle(.borderedProminent)
                .disabled(userID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            HStack {
                Button("移除選取項目", role: .destructive) { showDeleteConfirmation = true }
                    .disabled(selectedIDs.isEmpty)
                Toggle("匯入時取代整份名單", isOn: $importReplace)
                Spacer()
                Button("匯入 JSON／CSV") { importList() }
                Menu("匯出") {
                    Button("JSON") { exportList("json") }
                    Button("CSV") { exportList("csv") }
                }
            }
            List(rows, selection: $selectedIDs) { row in
                HStack {
                    Text(row.userID).font(.body.monospaced())
                    Text(row.username.isEmpty ? "—" : "@\(row.username)").foregroundStyle(.secondary)
                    Spacer()
                    Text(row.reason).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    Text(row.added).font(.caption2).foregroundStyle(.tertiary)
                }
                .tag(row.id)
            }
            .listStyle(.inset)
        }
        .padding(28)
        .task(id: listType) { await client.fetchList(listType, query: query) }
        .confirmationDialog("移除選取的名單項目？", isPresented: $showDeleteConfirmation, titleVisibility: .visible) {
            Button("確認移除", role: .destructive) {
                Task {
                    for id in selectedIDs { await client.removeListEntry(listType, userID: id) }
                    selectedIDs.removeAll()
                }
            }
            Button("取消", role: .cancel) {}
        }
    }

    private func importList() {
        guard let url = openPanel(extensions: ["json", "csv"]) else { return }
        Task { await client.importList(listType, path: url.path, replace: importReplace) }
    }

    private func exportList(_ format: String) {
        guard let url = savePanel(fileExtension: format, name: "\(listType).\(format)") else { return }
        Task { await client.exportList(listType, path: url.path, format: format) }
    }
}

private struct RulesView: View {
    @ObservedObject var client: CoreClient
    @State private var sample = ""
    @State private var selected: (kind: String, value: String)?

    private var rules: [(kind: String, value: String)] {
        client.learnedPatterns.keywords.map { ("keywords", $0) } + client.learnedPatterns.patterns.map { ("patterns", $0) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            PageHeader(title: "學習規則", subtitle: "從實際廣告文字建立可持久化的關鍵詞與模式") {
                Button("重新整理") { Task { await client.refreshAccountData() } }
            }
            HStack(alignment: .top) {
                TextEditor(text: $sample)
                    .font(.body)
                    .frame(minHeight: 130)
                    .padding(6)
                    .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
                VStack(alignment: .leading, spacing: 10) {
                    Text("貼上一則廣告或可疑訊息").font(.headline)
                    Text("系統會拆出關鍵詞與可重用模式；不會把完整訊息送到網路。")
                        .font(.callout).foregroundStyle(.secondary)
                    Button("學習這則廣告") { Task { await client.learn(sample); sample = "" } }
                        .buttonStyle(.borderedProminent)
                        .disabled(sample.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || client.isBusy)
                }
                .frame(width: 280, alignment: .leading)
            }
            HStack {
                Text("目前規則：\(rules.count)").font(.headline)
                Spacer()
                Button("刪除選取規則", role: .destructive) {
                    if let selected { Task { await client.removeLearnedPattern(kind: selected.kind, value: selected.value); self.selected = nil } }
                }
                .disabled(selected == nil)
            }
            List(rules, id: \.value, selection: Binding(get: { selected?.value }, set: { value in selected = rules.first { $0.value == value } })) { rule in
                HStack {
                    Text(rule.kind == "keywords" ? "關鍵詞" : "模式")
                        .font(.caption.bold())
                        .foregroundStyle(rule.kind == "keywords" ? .blue : .orange)
                        .frame(width: 65, alignment: .leading)
                    Text(rule.value).font(.body.monospaced())
                }
                .tag(rule.value)
            }
            .listStyle(.inset)
        }
        .padding(28)
        .task { await client.refreshAccountData() }
    }
}

private struct ReportView: View {
    @ObservedObject var client: CoreClient
    @State private var period = "day"

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                PageHeader(title: "封鎖報告", subtitle: "用時間範圍整理來源、原因與每日趨勢") {
                    Picker("期間", selection: $period) {
                        Text("過去 24 小時").tag("day")
                        Text("過去 7 天").tag("week")
                        Text("全部").tag("all")
                    }
                    .frame(width: 150)
                    Button("重新產生") { Task { await client.buildReport(period: period) } }
                    Menu("匯出") {
                        Button("JSON") { exportReport() }
                    }
                }
                if let report = client.report {
                    HStack(spacing: 14) {
                        MetricCard(title: report.label, value: "\(report.total)", icon: "chart.bar")
                        MetricCard(title: "來源種類", value: "\(report.bySource.count)", icon: "arrow.triangle.branch")
                        MetricCard(title: "主要原因", value: "\(report.byReason.count)", icon: "tag")
                    }
                    HStack(alignment: .top, spacing: 20) {
                        ReportDictionaryCard(title: "來源", values: report.bySource)
                        ReportDictionaryCard(title: "原因 Top 5", values: report.byReason)
                    }
                    if !report.trend.isEmpty {
                        TrendCard(values: report.trend)
                    }
                } else {
                    EmptyPanel(title: "尚未產生報告", message: "選擇期間後按「重新產生」。")
                }
            }
            .padding(28)
        }
        .task(id: client.selectedAccountID) { await client.buildReport(period: period) }
    }

    private func exportReport() {
        guard let url = savePanel(fileExtension: "json", name: "teleShield-report.json") else { return }
        Task { await client.exportReport(path: url.path) }
    }
}

private struct ReportDictionaryCard: View {
    let title: String
    let values: [String: Int]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(.headline)
            if values.isEmpty { Text("無資料").foregroundStyle(.secondary) }
            ForEach(values.keys.sorted(), id: \.self) { key in
                HStack { Text(key); Spacer(); Text("\(values[key] ?? 0)").fontWeight(.semibold).monospacedDigit() }
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 14))
    }
}

private struct TrendCard: View {
    let values: [String: Int]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("每日趨勢").font(.headline)
            let maxValue = max(values.values.max() ?? 1, 1)
            ForEach(values.keys.sorted(), id: \.self) { key in
                HStack(spacing: 10) {
                    Text(key).font(.caption.monospaced()).frame(width: 90, alignment: .leading)
                    RoundedRectangle(cornerRadius: 4)
                        .fill(.blue.gradient)
                        .frame(width: max(4, CGFloat(values[key] ?? 0) / CGFloat(maxValue) * 300), height: 14)
                    Text("\(values[key] ?? 0)").font(.caption.monospacedDigit())
                }
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 14))
    }
}

private struct BlockRecordsView: View {
    @ObservedObject var client: CoreClient
    @State private var query = ""
    @State private var source = "all"

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            PageHeader(title: "封鎖記錄", subtitle: "查找私訊與群組處理歷史，必要時匯出交接或稽核") {
                Menu("匯出") {
                    Button("JSON") { export("json") }
                    Button("CSV") { export("csv") }
                }
            }
            HStack {
                TextField("搜尋姓名、ID、原因", text: $query)
                Picker("來源", selection: $source) {
                    Text("全部").tag("all")
                    Text("私訊").tag("private")
                    Text("群組").tag("group")
                }
                .frame(width: 120)
                Button("重新整理") { Task { await client.fetchBlockRecords(query: query, source: source) } }
            }
            List(client.blockRecords) { record in
                HStack(alignment: .top, spacing: 12) {
                    Text(record.time.replacingOccurrences(of: "T", with: " ")).font(.caption.monospaced()).frame(width: 175, alignment: .leading)
                    Text(record.source == "group" ? "群組" : "私訊").font(.caption.bold()).foregroundStyle(record.source == "group" ? .orange : .blue).frame(width: 45, alignment: .leading)
                    Text(record.userID).font(.caption.monospaced()).frame(width: 100, alignment: .leading)
                    Text(record.name).fontWeight(.medium).frame(width: 150, alignment: .leading)
                    Text(record.reason).foregroundStyle(.secondary).lineLimit(2)
                }
            }
            .listStyle(.inset)
        }
        .padding(28)
        .task(id: client.selectedAccountID) { await client.fetchBlockRecords(query: query, source: source) }
    }

    private func export(_ format: String) {
        guard let url = savePanel(fileExtension: format, name: "teleShield-blocks.\(format)") else { return }
        Task { await client.exportBlocks(path: url.path, query: query, source: source, format: format) }
    }
}

private struct GroupsView: View {
    @ObservedObject var client: CoreClient

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            PageHeader(title: "群組管理", subtitle: "只掃描你有管理權限且明確啟用的群組") {
                Button("從 Telegram 重新讀取") { Task { await client.discoverGroups() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(!client.canModifySelectedAccount || client.isBusy)
            }
            if client.operationJobID != nil {
                ProgressView("正在讀取群組…")
            }
            List(client.groups) { group in
                HStack {
                    Image(systemName: "person.3.fill").foregroundStyle(.blue)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(group.title).fontWeight(.medium)
                        Text("ID \(group.groupID)  \(group.username.isEmpty ? "" : "@\(group.username)")")
                            .font(.caption.monospaced()).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text(group.permission.isEmpty ? "管理員／建立者" : group.permission).font(.caption).foregroundStyle(.secondary)
                    Toggle("啟用", isOn: Binding(get: { group.enabled }, set: { value in Task { await client.setGroupEnabled(group.id, enabled: value) } }))
                        .toggleStyle(.switch)
                        .frame(width: 100)
                }
            }
            .listStyle(.inset)
        }
        .padding(28)
        .task { await client.refreshAccountData() }
    }
}

private struct SettingsView: View {
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

private struct ScanStepper: View {
    let title: String
    @Binding var value: Int
    let range: ClosedRange<Int>

    var body: some View {
        Stepper("\(title)：\(value)", value: $value, in: range)
    }
}

private struct AccountsView: View {
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

private struct LoginSheet: View {
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
                Link("MTProto API 註冊", destination: URL(string: "https://my.telegram.org/auth?to=apps")!)
                    .font(.callout.weight(.semibold))
                Button("關閉") { dismiss() }
            }
            Divider()
            Text("API 憑證只會交給本機 sidecar；請勿把 Hash 或 2FA 密碼貼到聊天或記錄中。")
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
        .onDisappear { cleanupAfterDismissal() }
    }

    private func cleanupAfterDismissal() {
        guard !cleanupStarted else { return }
        cleanupStarted = true
        Task {
            if client.authFlowID != nil {
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

private struct AccountRow: View {
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

private struct CurrentAccountBar: View {
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

private struct AccountSwitcher: View {
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

private struct AccountStatusBadge: View {
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
        if ready { return "防護中" }
        if running { return "啟動中" }
        return "已停止"
    }

    var statusColor: Color {
        if !configured { return .secondary }
        if let error, !error.isEmpty { return .red }
        if ready { return .green }
        if running { return .orange }
        return .secondary
    }
}

private struct MetricCard: View {
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

private struct EmptyPanel: View {
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
        .onReceive(NotificationCenter.default.publisher(for: .teleShieldOpenMainWindow)) { _ in
            openMainWindow()
        }
    }

    private func openMainWindow() {
        openWindow(id: "main")
        DispatchQueue.main.async {
            NSApp.activate(ignoringOtherApps: true)
            NSApp.windows.first { $0.title == "TeleShield" }?.makeKeyAndOrderFront(nil)
        }
    }
}

private func openPanel(extensions: [String]) -> URL? {
    let panel = NSOpenPanel()
    panel.canChooseFiles = true
    panel.canChooseDirectories = false
    panel.allowsMultipleSelection = false
    panel.allowedFileTypes = extensions
    return panel.runModal() == .OK ? panel.url : nil
}

private func savePanel(fileExtension: String, name: String) -> URL? {
    let panel = NSSavePanel()
    panel.nameFieldStringValue = name
    panel.allowedFileTypes = [fileExtension]
    return panel.runModal() == .OK ? panel.url : nil
}

