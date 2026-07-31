import SwiftUI

struct OverviewView: View {
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
        .task(id: client.selectedAccountID) {
            await client.fetchBlockRecords(query: "", source: "all")
        }
    }
}

struct ProtectionView: View {
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

struct ProtectionSummary: View {
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

struct OnboardingCard: View {
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

struct EventLogCard: View {
    @ObservedObject var client: CoreClient

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label("最近活動", systemImage: "list.bullet.rectangle")
                    .font(.headline)
                Spacer()
                Text("目前帳號的最新封鎖記錄")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }
            if !client.blockRecords.isEmpty {
                ForEach(Array(client.blockRecords.prefix(12))) { record in
                    HStack(alignment: .top, spacing: 8) {
                        Circle().fill(.red).frame(width: 7, height: 7).padding(.top, 5)
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 6) {
                                Text(record.name.isEmpty ? record.userID : record.name)
                                    .font(.callout.weight(.medium))
                                Text(record.source == "group" ? "群組" : "私訊")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Text(record.reason.isEmpty ? "未記錄原因" : record.reason)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                        }
                        Spacer()
                        Text(record.time.replacingOccurrences(of: "T", with: " "))
                            .font(.caption2.monospaced())
                            .foregroundStyle(.tertiary)
                    }
                }
            } else if client.eventLog.isEmpty {
                Text("尚無封鎖活動。啟動防護、掃描或管理名單後，操作紀錄會顯示在這裡。")
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


