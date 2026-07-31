import SwiftUI
import UniformTypeIdentifiers

struct HistoryScanView: View {
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
                    Button("停止掃描") { client.cancelScan() }
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

struct ListManagementView: View {
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

struct RulesView: View {
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

struct ReportView: View {
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

struct ReportDictionaryCard: View {
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

struct TrendCard: View {
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

struct BlockRecordsView: View {
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

struct GroupsView: View {
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


