import Combine
import Foundation

@MainActor
final class CoreClient: ObservableObject {
    @Published private(set) var status: CoreStatus?
    @Published private(set) var details: AccountDetails?
    @Published private(set) var startupEnabled = false
    @Published private(set) var connectionMessage = "尚未啟動"
    @Published private(set) var lastLog = ""
    @Published private(set) var errorMessage: String?
    @Published private(set) var eventLog: [EventLogEntry] = []
    @Published private(set) var isBusy = false
    @Published private(set) var busyOperation: String?

    @Published private(set) var authFlowID: String?
    @Published private(set) var authChallengeKind: String?
    @Published private(set) var authDeliveryMessage = ""
    @Published private(set) var authInProgress = false
    @Published private(set) var authenticatedAccountID: String?

    @Published private(set) var whitelist: [ListEntry] = []
    @Published private(set) var blacklist: [ListEntry] = []
    @Published private(set) var learnedPatterns = LearnedPatterns.empty
    @Published private(set) var groups: [ManagedGroup] = []
    @Published private(set) var blockRecords: [BlockRecord] = []
    @Published private(set) var report: Report?
    @Published private(set) var scanSettings = ScanSettings.defaults
    @Published private(set) var scanJobID: String?
    @Published private(set) var scanProgress: [String] = []
    @Published private(set) var scanResult: ScanResult?
    @Published private(set) var operationJobID: String?

    private var process: Process?
    private var inputHandle: FileHandle?
    private var stdoutBuffer = Data()
    private var nextRequestID = 1
    private var pending: [Int: CheckedContinuation<Data, Error>] = [:]
    private var backgroundStartupHandled = false
    private var scanStartInFlight = false
    private var stdoutReadHandle: FileHandle?
    private var stderrReadHandle: FileHandle?

    var selectedAccount: AccountSummary? { status?.selectedAccount }
    var selectedAccountID: String? { status?.activeAccountID ?? status?.selectedAccount.accountID }
    var helperIsRunning: Bool { process?.isRunning == true }
    var hasActiveScan: Bool { scanJobID != nil || scanStartInFlight }
    var canModifySelectedAccount: Bool {
        guard let selectedAccount else { return false }
        return !selectedAccount.running && !authInProgress && operationJobID == nil
    }

    func launch() {
        guard process == nil else { return }
        guard let helperPath = resolveHelperPath() else {
            errorMessage = "找不到 TeleShieldCore。請從 DMG 啟動完整的 TeleShield.app。"
            connectionMessage = "sidecar 不存在"
            return
        }

        let stdinPipe = Pipe()
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        let child = Process()
        child.executableURL = URL(fileURLWithPath: helperPath)
        child.arguments = ["--stdio"]
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        environment["TELESHIELD_STARTUP_APP"] = Bundle.main.bundlePath
        child.environment = environment
        child.standardInput = stdinPipe
        child.standardOutput = stdoutPipe
        child.standardError = stderrPipe

        let stdoutReadHandle = stdoutPipe.fileHandleForReading
        let stderrReadHandle = stderrPipe.fileHandleForReading
        self.stdoutReadHandle = stdoutReadHandle
        self.stderrReadHandle = stderrReadHandle
        stdoutReadHandle.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            Task { @MainActor [weak self] in self?.consume(data) }
        }
        stderrReadHandle.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            let message = String(data: data, encoding: .utf8) ?? "sidecar stderr"
            Task { @MainActor [weak self] in self?.appendLog(message, level: "stderr") }
        }
        child.terminationHandler = { [weak self] terminatedProcess in
            let status = terminatedProcess.terminationStatus
            Task { @MainActor [weak self] in self?.sidecarTerminated(exitCode: status) }
        }

        do {
            try child.run()
        } catch {
            errorMessage = "無法啟動 Python sidecar：\(error.localizedDescription)"
            connectionMessage = "sidecar 啟動失敗"
            return
        }

        process = child
        inputHandle = stdinPipe.fileHandleForWriting
        connectionMessage = "sidecar 已啟動"
        errorMessage = nil
        Task { [weak self] in await self?.refresh() }
    }

    func refresh() async {
        guard helperIsRunning else { return }
        do {
            let data = try await request(method: "get_status")
            status = try decodeResult(CoreStatus.self, from: data)
            connectionMessage = "已連線"
            errorMessage = nil
            await refreshAccountData()
            let startupData = try await request(method: "get_startup_status")
            startupEnabled = try decodeResult(StartupStatus.self, from: startupData).enabled
            await handleBackgroundLaunchIfNeeded()
        } catch {
            present(error: error)
        }
    }

    func refreshAccountData() async {
        guard helperIsRunning else { return }
        do {
            let data = try await request(method: "get_account_details", params: accountParams())
            let details = try decodeResult(AccountDetails.self, from: data)
            self.details = details
            scanSettings = details.scanSettings
            learnedPatterns = details.learnedPatterns
            groups = details.managedGroups
        } catch {
            present(error: error)
        }
    }

    func selectAccount(_ accountID: String) async {
        await runBusy("切換帳號") {
            _ = try await self.request(method: "select_account", params: ["account_id": .string(accountID)])
            await self.refresh()
        }
    }

    func createAccount() async -> String? {
        do {
            let data = try await request(method: "create_account")
            let result = try decodeResult(JSONValue.self, from: data)
            guard case .object(let object) = result, case .string(let accountID)? = object["id"] else {
                throw CoreClientError(message: "新增帳號回傳缺少 id")
            }
            await refresh()
            await selectAccount(accountID)
            return accountID
        } catch {
            present(error: error)
            return nil
        }
    }

    func removeAccount(_ accountID: String, deleteFiles: Bool = true) async -> Bool {
        guard !isBusy else { return false }
        isBusy = true
        busyOperation = "移除帳號"
        defer { isBusy = false; busyOperation = nil }
        do {
            _ = try await request(
                method: "remove_account",
                params: ["account_id": .string(accountID), "delete_files": .bool(deleteFiles)]
            )
            await refresh()
            return true
        } catch {
            present(error: error)
            return false
        }
    }

    func startAll() async {
        guard let accounts = status?.accounts else { return }
        await runBusy("啟動全部帳號") {
            for account in accounts where account.configured && !account.running {
                _ = try await self.request(method: "start_protection", params: ["account_id": .string(account.id)])
            }
            await self.refresh()
        }
    }

    func stopAll() async {
        await runBusy("停止全部帳號") {
            _ = try await self.request(method: "stop_all")
            await self.refresh()
        }
    }

    func setAutoStart(accountID: String?) async {
        do {
            var params: [String: JSONValue] = [:]
            if let accountID { params["account_id"] = .string(accountID) }
            _ = try await request(method: "set_auto_start", params: params)
            await refreshAccountData()
        } catch { present(error: error) }
    }

    func setStartup(_ enabled: Bool) async {
        do {
            let data = try await request(method: "set_startup", params: ["enabled": .bool(enabled)])
            startupEnabled = try decodeResult(StartupStatus.self, from: data).enabled
        } catch { present(error: error) }
    }

    func startProtection() async { await performProtectionAction("start_protection") }
    func stopProtection() async { await performProtectionAction("stop_protection") }

    private func performProtectionAction(_ method: String) async {
        await runBusy(method == "start_protection" ? "啟動防護" : "停止防護") {
            _ = try await self.request(method: method, params: self.accountParams())
            await self.refresh()
        }
    }

    func startAuthentication(apiID: String, apiHash: String, phone: String, accountID: String? = nil) async {
        guard let numericAPIID = Int(apiID.trimmingCharacters(in: .whitespacesAndNewlines)), numericAPIID > 0 else {
            errorMessage = "API ID 必須是正整數"
            return
        }
        guard !apiHash.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !phone.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            errorMessage = "API Hash 與手機號碼不可為空"
            return
        }
        isBusy = true
        authInProgress = true
        authenticatedAccountID = nil
        errorMessage = nil
        defer { isBusy = false }
        do {
            var params: [String: JSONValue] = [
                "api_id": .int(numericAPIID),
                "api_hash": .string(apiHash),
                "phone": .string(phone),
            ]
            if let accountID { params["account_id"] = .string(accountID) }
            let data = try await request(method: "start_auth", params: params)
            let result = try decodeResult(AuthStartResult.self, from: data)
            authFlowID = result.flowID
            connectionMessage = "等待 Telegram 驗證"
        } catch {
            authInProgress = false
            present(error: error)
        }
    }

    func submitAuthCode(_ value: String) async { await submitAuthValue(value, method: "submit_auth_code") }
    func submitAuthPassword(_ value: String) async { await submitAuthValue(value, method: "submit_auth_password") }

    func cancelAuthentication() async {
        guard let flowID = authFlowID else { return }
        do {
            _ = try await request(method: "cancel_auth", params: ["flow_id": .string(flowID)])
            for _ in 0..<20 {
                if authFlowID != flowID { break }
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
        } catch { present(error: error) }
        authInProgress = false
        self.authFlowID = nil
        authChallengeKind = nil
    }

    private func submitAuthValue(_ value: String, method: String) async {
        guard let authFlowID, !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        do {
            _ = try await request(method: method, params: ["flow_id": .string(authFlowID), "value": .string(value)])
        } catch { present(error: error) }
    }

    func fetchList(_ listType: String, query: String = "") async {
        do {
            let data = try await request(method: "list_entries", params: [
                "list_type": .string(listType),
                "query": .string(query),
            ].merging(accountParams() ?? [:]) { _, new in new })
            let rows = try decodeResult([ListEntry].self, from: data)
            if listType == "whitelist" { whitelist = rows } else { blacklist = rows }
        } catch { present(error: error) }
    }

    func upsertList(_ listType: String, userID: String, username: String, reason: String) async {
        do {
            var params = accountParams() ?? [:]
            params["list_type"] = .string(listType)
            params["user_id"] = .string(userID)
            params["username"] = .string(username)
            params["reason"] = .string(reason)
            _ = try await request(method: "upsert_list_entry", params: params)
            await fetchList(listType)
            await refresh()
        } catch { present(error: error) }
    }

    func removeListEntry(_ listType: String, userID: String) async {
        do {
            var params = accountParams() ?? [:]
            params["list_type"] = .string(listType)
            params["user_id"] = .string(userID)
            _ = try await request(method: "remove_list_entry", params: params)
            await fetchList(listType)
            await refresh()
        } catch { present(error: error) }
    }

    func importList(_ listType: String, path: String, replace: Bool) async {
        do {
            var params = accountParams() ?? [:]
            params["list_type"] = .string(listType)
            params["path"] = .string(path)
            params["replace"] = .bool(replace)
            _ = try await request(method: "import_list", params: params)
            await fetchList(listType)
            await refresh()
        } catch { present(error: error) }
    }

    func exportList(_ listType: String, path: String, format: String) async {
        do {
            var params = accountParams() ?? [:]
            params["list_type"] = .string(listType)
            params["path"] = .string(path)
            params["fmt"] = .string(format)
            _ = try await request(method: "export_list", params: params)
            appendLog("已匯出 \(listType) 名單", level: "info")
        } catch { present(error: error) }
    }

    func learn(_ text: String) async {
        do {
            var params = accountParams() ?? [:]
            params["text"] = .string(text)
            _ = try await request(method: "learn_text", params: params)
            await refreshAccountData()
        } catch { present(error: error) }
    }

    func removeLearnedPattern(kind: String, value: String) async {
        do {
            var params = accountParams() ?? [:]
            params["kind"] = .string(kind)
            params["value"] = .string(value)
            _ = try await request(method: "remove_learned_pattern", params: params)
            await refreshAccountData()
        } catch { present(error: error) }
    }

    func buildReport(period: String) async {
        do {
            var params = accountParams() ?? [:]
            params["period"] = .string(period)
            let data = try await request(method: "build_report", params: params)
            report = try decodeResult(Report.self, from: data)
        } catch { present(error: error) }
    }

    func exportReport(path: String) async {
        guard let report else { return }
        do {
            let data = try JSONEncoder.pretty.encode(report)
            try data.write(to: URL(fileURLWithPath: path), options: .atomic)
            appendLog("已匯出報告", level: "info")
        } catch { present(error: error) }
    }

    func fetchBlockRecords(query: String = "", source: String = "all") async {
        do {
            var params = accountParams() ?? [:]
            params["query"] = .string(query)
            params["source"] = .string(source)
            params["limit"] = .int(500)
            let data = try await request(method: "get_block_records", params: params)
            blockRecords = try decodeResult([BlockRecord].self, from: data)
        } catch { present(error: error) }
    }

    func exportBlocks(path: String, query: String, source: String, format: String) async {
        do {
            var params = accountParams() ?? [:]
            params["path"] = .string(path)
            params["query"] = .string(query)
            params["source"] = .string(source)
            params["fmt"] = .string(format)
            _ = try await request(method: "export_blocks", params: params)
            appendLog("已匯出封鎖記錄", level: "info")
        } catch { present(error: error) }
    }

    func discoverGroups() async {
        guard canModifySelectedAccount else { return }
        do {
            let data = try await request(method: "discover_groups", params: accountParams())
            operationJobID = try decodeResult(JobStartResult.self, from: data).jobID
        } catch { present(error: error) }
    }

    func setGroupEnabled(_ groupID: String, enabled: Bool) async {
        do {
            var params = accountParams() ?? [:]
            params["group_id"] = .string(groupID)
            params["enabled"] = .bool(enabled)
            _ = try await request(method: "set_group_enabled", params: params)
            groups = groups.map { group in
                group.id == groupID
                    ? ManagedGroup(groupID: group.groupID, title: group.title, username: group.username, permission: group.permission, enabled: enabled)
                    : group
            }
        } catch { present(error: error) }
    }

    func updateScanSettings(_ settings: ScanSettings) async {
        do {
            let updates: [String: JSONValue] = [
                "private_dialog_limit": .int(settings.privateDialogLimit),
                "private_message_limit": .int(settings.privateMessageLimit),
                "private_days": .int(settings.privateDays),
                "group_dialog_limit": .int(settings.groupDialogLimit),
                "group_message_limit": .int(settings.groupMessageLimit),
                "group_days": .int(settings.groupDays),
            ]
            var params = accountParams() ?? [:]
            params["updates"] = .object(updates)
            let data = try await request(method: "update_scan_settings", params: params)
            scanSettings = try decodeResult(ScanSettings.self, from: data)
            appendLog("掃描設定已儲存", level: "info")
        } catch { present(error: error) }
    }

    func startScan(scope: String, dryRun: Bool) async {
        guard !hasActiveScan else { return }
        guard selectedAccount?.running != true else {
            errorMessage = "請先停止即時防護，再掃描同一帳號的歷史訊息"
            return
        }
        scanStartInFlight = true
        defer { scanStartInFlight = false }
        do {
            scanProgress.removeAll()
            scanResult = nil
            var params = accountParams() ?? [:]
            params["scope"] = .string(scope)
            params["dry_run"] = .bool(dryRun)
            let data = try await request(method: "start_scan", params: params)
            scanJobID = try decodeResult(JobStartResult.self, from: data).jobID
        } catch { present(error: error) }
    }

    func cancelScan() async {
        guard let scanJobID else { return }
        do {
            _ = try await request(method: "cancel_scan", params: ["job_id": .string(scanJobID)])
        } catch { present(error: error) }
    }

    func addFindings(_ findings: [ScanFinding], to listType: String) async {
        await runBusy("加入名單") {
            for finding in findings {
                var params = self.accountParams() ?? [:]
                params["list_type"] = .string(listType)
                params["user_id"] = .string(finding.userID)
                params["username"] = .string(finding.name)
                params["reason"] = .string("歷史掃描：\(finding.reason)")
                _ = try await self.request(method: "upsert_list_entry", params: params)
            }
            await self.refresh()
        }
    }

    func logout(removeCredentials: Bool) async {
        guard canModifySelectedAccount else { return }
        do {
            var params = accountParams() ?? [:]
            params["remove_credentials"] = .bool(removeCredentials)
            let data = try await request(method: "logout", params: params)
            operationJobID = try decodeResult(JobStartResult.self, from: data).jobID
        } catch { present(error: error) }
    }

    func clearSession(removeCredentials: Bool) async {
        guard canModifySelectedAccount else { return }
        do {
            var params = accountParams() ?? [:]
            params["remove_credentials"] = .bool(removeCredentials)
            _ = try await request(method: "clear_session", params: params)
            await refresh()
        } catch { present(error: error) }
    }

    func refreshOCR() async {
        do {
            let data = try await request(method: "get_ocr_status")
            let ocr = try decodeResult(OCRStatus.self, from: data)
            if let current = status {
                status = CoreStatus(activeAccountID: current.activeAccountID, selectedAccount: current.selectedAccount, accounts: current.accounts, ocr: ocr)
            }
        } catch { present(error: error) }
    }

    func shutdownGracefully() async {
        guard helperIsRunning else { return }
        do {
            _ = try await request(method: "shutdown")
            for _ in 0..<30 {
                if !helperIsRunning { return }
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
        } catch {
            present(error: error)
        }
        if helperIsRunning {
            process?.terminate()
        }
    }

    func shutdown() {
        Task { await shutdownGracefully() }
    }

    private func handleBackgroundLaunchIfNeeded() async {
        guard !backgroundStartupHandled,
              ProcessInfo.processInfo.arguments.contains("--background") else { return }
        backgroundStartupHandled = true
        guard let autoStartAccountID = details?.autoStartAccountID else { return }
        if selectedAccountID != autoStartAccountID {
            await selectAccount(autoStartAccountID)
        }
        await startProtection()
    }

    private func accountParams() -> [String: JSONValue]? {
        guard let accountID = selectedAccountID, !accountID.isEmpty else { return nil }
        return ["account_id": .string(accountID)]
    }

    private func runBusy(_ operation: String, _ action: @escaping () async throws -> Void) async {
        guard !isBusy else { return }
        isBusy = true
        busyOperation = operation
        defer { isBusy = false; busyOperation = nil }
        do { try await action() } catch { present(error: error) }
    }

    private func request(method: String, params: [String: JSONValue]? = nil) async throws -> Data {
        guard process?.isRunning == true, let inputHandle else {
            throw CoreClientError(message: "Python sidecar 尚未啟動")
        }
        let requestID = nextRequestID
        nextRequestID += 1
        var data = try JSONEncoder().encode(RPCRequest(id: requestID, method: method, params: params))
        data.append(0x0A)
        return try await withCheckedThrowingContinuation { continuation in
            pending[requestID] = continuation
            do { try inputHandle.write(contentsOf: data) }
            catch {
                pending.removeValue(forKey: requestID)
                continuation.resume(throwing: error)
            }
        }
    }

    private func decodeResult<Result: Decodable>(_ type: Result.Type, from data: Data) throws -> Result {
        let response = try JSONDecoder().decode(RPCResponse<Result>.self, from: data)
        guard response.ok else { throw CoreClientError(message: response.error?.message ?? "sidecar 回傳未知錯誤") }
        guard let result = response.result else { throw CoreClientError(message: "sidecar 回傳缺少 result") }
        return result
    }

    private func consume(_ data: Data) {
        stdoutBuffer.append(data)
        while let newline = stdoutBuffer.firstIndex(of: 0x0A) {
            let line = stdoutBuffer.subdata(in: 0..<newline)
            stdoutBuffer.removeSubrange(0...newline)
            guard !line.isEmpty else { continue }
            handleLine(line)
        }
    }

    private func handleLine(_ line: Data) {
        guard let object = try? JSONSerialization.jsonObject(with: line), let dictionary = object as? [String: Any] else {
            appendLog(String(data: line, encoding: .utf8) ?? "sidecar 傳回無法解析的資料", level: "error")
            return
        }
        if let event = dictionary["event"] as? String {
            handleEvent(event, dictionary: dictionary)
            return
        }
        guard let requestID = dictionary["id"] as? Int, let continuation = pending.removeValue(forKey: requestID) else { return }
        continuation.resume(returning: line)
    }

    private func handleEvent(_ event: String, dictionary: [String: Any]) {
        switch event {
        case "log":
            appendLog(dictionary["message"] as? String ?? "", level: dictionary["level"] as? String ?? "info")
        case "status":
            let state = dictionary["state"] as? String ?? "unknown"
            connectionMessage = "listener：\(state)"
            if state == "error" { errorMessage = dictionary["error"] as? String ?? "listener 發生錯誤" }
            Task { [weak self] in await self?.refresh() }
        case "auth_delivery":
            authDeliveryMessage = dictionary["message"] as? String ?? ""
            appendLog(authDeliveryMessage, level: "info")
        case "auth_challenge":
            authFlowID = dictionary["flow_id"] as? String ?? authFlowID
            authChallengeKind = dictionary["kind"] as? String
            authInProgress = true
            connectionMessage = authChallengeKind == "password" ? "需要 Telegram 兩步驟驗證" : "需要 Telegram 驗證碼"
        case "auth_succeeded":
            authInProgress = false
            authenticatedAccountID = dictionary["account_id"] as? String
            authFlowID = nil
            authChallengeKind = nil
            connectionMessage = "Telegram 登入成功"
            Task { [weak self] in await self?.refresh() }
        case "auth_failed":
            authInProgress = false
            authenticatedAccountID = nil
            authFlowID = nil
            authChallengeKind = nil
            let error = dictionary["error"] as? [String: Any]
            errorMessage = error?["message"] as? String ?? "Telegram 登入失敗"
            connectionMessage = "登入失敗"
        case "scan_progress":
            if let message = dictionary["message"] as? String {
                scanProgress.append(message)
                appendLog(message, level: "info")
            }
        case "scan_finished":
            if let result = decodeEventResult(ScanResult.self, dictionary: dictionary["result"]) {
                scanResult = result
            }
            scanJobID = nil
            Task { [weak self] in await self?.refresh() }
        case "scan_failed":
            scanJobID = nil
            errorMessage = eventError(dictionary) ?? "歷史掃描失敗"
        case "groups_finished":
            groups = decodeEventResult([ManagedGroup].self, dictionary: dictionary["result"]) ?? groups
            operationJobID = nil
        case "groups_failed":
            operationJobID = nil
            errorMessage = eventError(dictionary) ?? "群組讀取失敗"
        case "account_operation_finished":
            operationJobID = nil
            appendLog("帳號操作完成", level: "info")
            Task { [weak self] in await self?.refresh() }
        case "account_operation_failed":
            operationJobID = nil
            errorMessage = eventError(dictionary) ?? "帳號操作失敗"
        default:
            appendLog(event, level: "info")
        }
    }

    private func decodeEventResult<Result: Decodable>(_ type: Result.Type, dictionary: Any?) -> Result? {
        guard let dictionary, JSONSerialization.isValidJSONObject(dictionary) else { return nil }
        guard let data = try? JSONSerialization.data(withJSONObject: dictionary) else { return nil }
        return try? JSONDecoder().decode(Result.self, from: data)
    }

    private func eventError(_ dictionary: [String: Any]) -> String? {
        (dictionary["error"] as? [String: Any])?["message"] as? String
    }

    private func appendLog(_ message: String, level: String) {
        guard !message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        lastLog = message
        eventLog.append(EventLogEntry(level: level, message: message))
        if eventLog.count > 300 { eventLog.removeFirst(eventLog.count - 300) }
    }

    private func sidecarTerminated(exitCode: Int32) {
        stdoutReadHandle?.readabilityHandler = nil
        stderrReadHandle?.readabilityHandler = nil
        stdoutReadHandle = nil
        stderrReadHandle = nil
        let message = exitCode == 0 ? "sidecar 已停止" : "sidecar 已停止（exit \(exitCode)）"
        connectionMessage = message
        process = nil
        inputHandle = nil
        let error = CoreClientError(message: message)
        pending.values.forEach { $0.resume(throwing: error) }
        pending.removeAll()
    }

    private func present(error: Error) {
        errorMessage = error.localizedDescription
        connectionMessage = "sidecar 通訊失敗"
        appendLog(error.localizedDescription, level: "error")
    }

    private func resolveHelperPath() -> String? {
        if let override = ProcessInfo.processInfo.environment["TELESHIELD_CORE_PATH"], FileManager.default.isExecutableFile(atPath: override) { return override }
        let bundle = Bundle.main.bundleURL
        let candidates = [
            bundle.appendingPathComponent("Contents/Helpers/TeleShieldCore/TeleShieldCore"),
            bundle.appendingPathComponent("Helpers/TeleShieldCore/TeleShieldCore"),
            bundle.deletingLastPathComponent().appendingPathComponent("TeleShieldCore"),
        ]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0.path) }?.path
    }
}

private struct StartupStatus: Codable { let enabled: Bool }

private extension JSONEncoder {
    static var pretty: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return encoder
    }
}
