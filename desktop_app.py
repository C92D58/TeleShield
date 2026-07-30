"""TeleShield desktop application.

The desktop shell keeps the existing Telethon core in a background QThread,
so closing the window hides it while the Telegram listener remains connected.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Optional

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QInputDialog,
    QHeaderView,
    QSpinBox,
)

from desktop_platform import is_start_on_login_enabled, set_start_on_login
from teleshield import (
    authenticate,
    build_report,
    clear_local_session,
    create_account,
    discover_managed_groups,
    ensure_account_registry,
    export_block_records,
    export_list_entries,
    get_active_account_id,
    get_auto_start_account_id,
    get_block_records,
    get_learned_patterns,
    get_ocr_status,
    get_scan_settings,
    import_list_entries,
    learn_text,
    listen,
    list_entries,
    list_accounts,
    load_config,
    logout_account,
    remove_account,
    remove_learned_pattern,
    remove_list_entry,
    save_config,
    scan_history,
    set_active_account,
    set_auto_start_account,
    set_managed_group_enabled,
    update_scan_settings,
    upsert_list_entry,
)


class SetupWorker(QThread):
    """Run Telegram login away from the Qt UI thread."""

    code_required = Signal()
    password_required = Signal()
    code_delivery = Signal(str)
    completed = Signal(bool, str)

    def __init__(self, api_id: str, api_hash: str, phone: str, account_id: Optional[str] = None):
        super().__init__()
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self.account_id = account_id
        self._code_event = threading.Event()
        self._password_event = threading.Event()
        self._code: Optional[str] = None
        self._password: Optional[str] = None
        self.result_success: Optional[bool] = None

    def provide_code(self, value: str) -> None:
        self._code = value.strip()
        self._code_event.set()

    def provide_password(self, value: str) -> None:
        self._password = value
        self._password_event.set()

    def cancel_prompt(self) -> None:
        self._code = ""
        self._password = ""
        self._code_event.set()
        self._password_event.set()

    async def _get_code(self) -> str:
        self.code_required.emit()
        await asyncio.to_thread(self._code_event.wait)
        return self._code or ""

    async def _get_password(self) -> str:
        self.password_required.emit()
        await asyncio.to_thread(self._password_event.wait)
        return self._password or ""

    def run(self) -> None:
        try:
            me = asyncio.run(
                authenticate(
                    self.api_id,
                    self.api_hash,
                    self.phone,
                    self._get_code,
                    self._get_password,
                    self.code_delivery.emit,
                    account_id=self.account_id,
                )
            )
            username = f"@{me.username}" if me.username else "無 username"
            self.result_success = True
            self.completed.emit(True, f"登入成功：{me.first_name or ''} ({username})")
        except Exception as exc:  # surfaced in the setup dialog
            self.result_success = False
            self.completed.emit(False, str(exc))


class ListenerWorker(QThread):
    """Own an asyncio loop for the long-running Telethon listener."""

    started_ok = Signal()
    stopped = Signal(str)

    def __init__(self, account_id: str):
        super().__init__()
        self.account_id = account_id
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._stop_requested = False
        self._ready = False

    def _on_ready(self) -> None:
        self._ready = True
        self.started_ok.emit()

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._stop_event = asyncio.Event()
        self._ready = False
        if self._stop_requested:
            self._stop_event.set()

        try:
            success = loop.run_until_complete(listen(self._stop_event, self._on_ready, self.account_id))
            if not self._ready:
                self.stopped.emit("防護未啟動：請先完成 Telegram 登入")
            elif success:
                self.stopped.emit("防護已停止")
            else:
                self.stopped.emit("防護因連線錯誤停止")
        except Exception as exc:
            self.stopped.emit(f"防護停止：{exc}")
        finally:
            loop.close()
            self._loop = None
            self._stop_event = None

    def stop(self) -> None:
        self._stop_requested = True
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)


class HistoryScanWorker(QThread):
    """Run a bounded history scan without blocking the Qt event loop."""

    progress = Signal(str)
    completed = Signal(dict)
    failed = Signal(str)

    def __init__(self, scope: str, dry_run: bool, account_id: str):
        super().__init__()
        self.scope = scope
        self.dry_run = dry_run
        self.account_id = account_id
        self._cancel_event = threading.Event()

    def run(self) -> None:
        try:
            result = asyncio.run(
                scan_history(
                    self.scope,
                    dry_run=self.dry_run,
                    progress_callback=self.progress.emit,
                    cancel_event=self._cancel_event,
                    account_id=self.account_id,
                )
            )
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))

    def cancel(self) -> None:
        self._cancel_event.set()


def format_scan_result(result: dict) -> str:
    """Turn a scan result into a concise, non-sensitive UI summary."""
    scope = "私訊" if result.get("scope") == "private" else "群組"
    mode = "試運行（沒有執行封鎖／踢除）" if result.get("dry_run") else "已執行處理"
    lines = [
        f"{scope}掃描完成：{mode}",
        f"掃描訊息：{result.get('messages_scanned', 0)}",
        f"發現疑似廣告：{result.get('matched', 0)}",
        f"已封鎖／踢除：{result.get('acted', 0)}",
    ]
    if result.get("groups_found"):
        lines.append(f"可管理群組：{result['groups_found']}")
    if result.get("cancelled"):
        lines.append("狀態：使用者已取消")
    errors = result.get("errors", [])
    if errors:
        lines.append(f"錯誤／跳過：{len(errors)}")
    findings = result.get("findings", [])
    if findings:
        lines.append("")
        lines.append("預覽結果：")
        for finding in findings[:50]:
            location = f" [{finding['group']}]" if finding.get("group") else ""
            lines.append(
                f"• {finding.get('name') or finding.get('user_id')}{location}"
                f"：{finding.get('reason', '')[:80]}"
            )
        if len(findings) > 50:
            lines.append(f"…另有 {len(findings) - 50} 筆未展開")
    return "\n".join(lines)


class SetupDialog(QDialog):
    """GUI version of the original --setup flow."""

    def __init__(self, parent: Optional[QWidget] = None, account_id: str = "", temporary_account: bool = False):
        super().__init__(parent)
        self.account_id = account_id
        self.temporary_account = temporary_account
        self._login_succeeded = False
        self._deferred_reject = False
        self._deferred_accept = False
        self._cleanup_connected = False
        self.setWindowTitle("TeleShield Telegram 登入")
        self.setModal(True)
        self.resize(520, 230)
        self.worker: Optional[SetupWorker] = None

        form = QFormLayout()
        self.api_id = QLineEdit()
        self.api_hash = QLineEdit()
        self.phone = QLineEdit()
        self.api_id.setPlaceholderText("例如 1234567")
        self.api_hash.setPlaceholderText("從 my.telegram.org/apps 取得")
        self.phone.setPlaceholderText("例如 +886...")
        form.addRow("API ID", self.api_id)
        form.addRow("API Hash", self.api_hash)
        form.addRow("手機號碼", self.phone)

        hint = QLabel(
            "首次登入會要求 Telegram 驗證碼；如果帳號啟用兩步驟驗證，"
            "也會在下一步要求密碼。"
        )
        hint.setWordWrap(True)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        self.buttons.accepted.connect(self.start_login)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addLayout(form)
        layout.addWidget(self.buttons)

    def start_login(self) -> None:
        if not self.api_id.text().strip().isdigit():
            QMessageBox.warning(self, "設定錯誤", "API ID 必須是數字。")
            return
        if not self.api_hash.text().strip() or not self.phone.text().strip():
            QMessageBox.warning(self, "設定錯誤", "請填寫 API Hash 和手機號碼。")
            return

        self.buttons.setEnabled(False)
        self._cleanup_connected = False
        self._deferred_reject = False
        self._deferred_accept = False
        self._login_succeeded = False
        self.worker = SetupWorker(
            self.api_id.text().strip(),
            self.api_hash.text().strip(),
            self.phone.text().strip(),
            self.account_id,
        )
        self.worker.code_required.connect(self.ask_code)
        self.worker.code_delivery.connect(self.show_code_delivery)
        self.worker.password_required.connect(self.ask_password)
        self.worker.completed.connect(self.login_completed)
        self.worker.start()

    def show_code_delivery(self, delivery: str) -> None:
        labels = {
            "App": "Telegram App（已登入的其他裝置）",
            "Sms": "SMS 簡訊",
            "Call": "語音電話",
            "FlashCall": "閃電電話",
            "MissedCall": "未接來電",
        }
        method, separator, details = delivery.partition("；")
        destination = labels.get(method, method)
        detail_text = f"\n{details}" if separator and details else ""
        QMessageBox.information(
            self,
            "驗證碼已請求",
            f"Telegram 回報的投遞方式：{destination}。{detail_text}\n\n"
            "請先查看其他已登入裝置的 Telegram，尤其是官方「Telegram」服務訊息；"
            "第三方登入不一定會收到 SMS。",
        )

    def ask_code(self) -> None:
        value, ok = QInputDialog.getText(
            self,
            "Telegram 驗證碼",
            "請輸入 Telegram 收到的驗證碼（不一定是 SMS）：",
        )
        if ok and self.worker:
            self.worker.provide_code(value)
        elif self.worker:
            self.worker.cancel_prompt()

    def ask_password(self) -> None:
        value, ok = QInputDialog.getText(
            self,
            "Telegram 兩步驟驗證",
            "請輸入兩步驟驗證密碼：",
            QLineEdit.EchoMode.Password,
        )
        if ok and self.worker:
            self.worker.provide_password(value)
        elif self.worker:
            self.worker.cancel_prompt()

    def _remove_temporary_account_when_safe(self) -> bool:
        if not self.temporary_account or not self.account_id or self._login_succeeded:
            return True
        if self.worker and self.worker.isRunning():
            if not self._cleanup_connected:
                self._cleanup_connected = True
                self.worker.finished.connect(self._worker_finished_cleanup)
            return False
        if not remove_account(self.account_id):
            QMessageBox.warning(self, "清理失敗", "登入失敗，但臨時帳號資料未能完整移除；請稍後從帳號管理中刪除。")
        return True

    def _worker_finished_cleanup(self) -> None:
        if self.worker and self.worker.result_success is True:
            self._login_succeeded = True
            if self._deferred_accept:
                self._deferred_accept = False
                super().accept()
            elif self._deferred_reject:
                self._deferred_reject = False
                super().reject()
            else:
                self.buttons.setEnabled(True)
            return
        if self._remove_temporary_account_when_safe():
            self.buttons.setEnabled(True)
            if self._deferred_reject:
                super().reject()

    def login_completed(self, success: bool, message: str) -> None:
        worker_running = bool(self.worker and self.worker.isRunning())
        self.buttons.setEnabled(not worker_running)
        if success:
            self._login_succeeded = True
            QMessageBox.information(self, "登入完成", message)
            if self.worker and self.worker.isRunning():
                self._deferred_accept = True
                if not self._cleanup_connected:
                    self._cleanup_connected = True
                    self.worker.finished.connect(self._worker_finished_cleanup)
            else:
                self.accept()
        else:
            self._remove_temporary_account_when_safe()
            QMessageBox.critical(self, "登入失敗", message)
            if self._deferred_reject and not (self.worker and self.worker.isRunning()):
                super().reject()

    def reject(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.cancel_prompt()
            self._deferred_reject = True
            if not self.worker.wait(2000):
                if not self._cleanup_connected:
                    self._cleanup_connected = True
                    self.worker.finished.connect(self._worker_finished_cleanup)
                QMessageBox.warning(self, "登入仍在停止", "登入工作尚未完全停止；視窗會在安全清理後關閉。")
                return
        if self.worker and self.worker.result_success is True:
            self._login_succeeded = True
        if not self._remove_temporary_account_when_safe():
            return
        super().reject()


class HistoryScanDialog(QDialog):
    """Preview and optionally apply a bounded history scan."""

    def __init__(self, parent: Optional[QWidget] = None, account_id: str = ""):
        super().__init__(parent)
        self.account_id = account_id
        self.setWindowTitle("掃描既有 Telegram 訊息")
        self.setModal(True)
        self.resize(650, 520)
        self.worker: Optional[HistoryScanWorker] = None
        self._closing = False
        self.last_result: Optional[dict] = None

        self.scope_combo = QComboBox()
        scan_settings = get_scan_settings(account_id=self.account_id)
        self.scope_combo.addItem(
            f"私訊（{scan_settings['private_dialog_limit']} 對話／每個 {scan_settings['private_message_limit']} 則／{scan_settings['private_days']} 天）",
            "private",
        )
        self.scope_combo.addItem(
            f"群組（{scan_settings['group_dialog_limit']} 對話／每個 {scan_settings['group_message_limit']} 則／{scan_settings['group_days']} 天）",
            "group",
        )
        self.preview_checkbox = QCheckBox("試運行：只預覽，不封鎖／踢除（建議先使用）")
        self.preview_checkbox.setChecked(True)
        self.status_label = QLabel("預設為安全預覽；確認結果後可取消勾選再執行。")
        self.status_label.setWordWrap(True)
        self.progress_view = QTextEdit()
        self.progress_view.setReadOnly(True)
        self.progress_view.setPlaceholderText("掃描進度與結果會顯示在這裡。")
        self.add_blacklist_button = QPushButton("將發現項目加入黑名單")
        self.add_whitelist_button = QPushButton("將發現項目加入白名單")
        self.add_blacklist_button.setEnabled(False)
        self.add_whitelist_button.setEnabled(False)
        self.add_blacklist_button.clicked.connect(lambda: self.add_findings_to_list("blacklist"))
        self.add_whitelist_button.clicked.connect(lambda: self.add_findings_to_list("whitelist"))

        self.start_button = QPushButton("開始預覽")
        self.cancel_button = QPushButton("關閉")
        self.preview_checkbox.toggled.connect(
            lambda enabled: self.start_button.setText("開始預覽" if enabled else "開始處理")
        )
        self.start_button.clicked.connect(self.start_scan)
        self.cancel_button.clicked.connect(self.cancel_or_close)

        form = QFormLayout()
        form.addRow("掃描範圍", self.scope_combo)
        form.addRow("執行模式", self.preview_checkbox)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "歷史掃描會使用「管理中心」中保存的範圍；掃描期間請先停止即時防護，"
            "避免同一個 Telegram Session 同時被兩個背景工作使用。"
        ))
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_view)
        findings_actions = QHBoxLayout()
        findings_actions.addWidget(self.add_blacklist_button)
        findings_actions.addWidget(self.add_whitelist_button)
        layout.addLayout(findings_actions)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

    def start_scan(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        dry_run = self.preview_checkbox.isChecked()
        scope = self.scope_combo.currentData()
        if not dry_run:
            action = "封鎖私訊發送者" if scope == "private" else "踢除群組廣告發送者"
            answer = QMessageBox.question(
                self,
                "確認執行危險操作",
                f"這次掃描會實際{action}。\n\n確定要繼續嗎？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._closing = False
        self.scope_combo.setEnabled(False)
        self.preview_checkbox.setEnabled(False)
        self.start_button.setEnabled(False)
        self.cancel_button.setText("停止掃描")
        self.cancel_button.setEnabled(True)
        self.progress_view.clear()
        self.last_result = None
        self.add_blacklist_button.setEnabled(False)
        self.add_whitelist_button.setEnabled(False)
        self.status_label.setText("掃描中…")
        self.worker = HistoryScanWorker(scope, dry_run, self.account_id)
        self.worker.progress.connect(self.append_progress)
        self.worker.completed.connect(self.scan_completed)
        self.worker.failed.connect(self.scan_failed)
        self.worker.start()

    def append_progress(self, message: str) -> None:
        self.progress_view.append(message)
        self.status_label.setText(message)

    def _reset_controls(self) -> None:
        self.scope_combo.setEnabled(True)
        self.preview_checkbox.setEnabled(True)
        self.start_button.setEnabled(True)
        self.start_button.setText("重新掃描")
        self.cancel_button.setText("關閉")
        self.cancel_button.setEnabled(True)

    def scan_completed(self, result: dict) -> None:
        self._reset_controls()
        self.last_result = result
        has_findings = bool(result.get("findings"))
        self.add_blacklist_button.setEnabled(has_findings)
        self.add_whitelist_button.setEnabled(has_findings)
        summary = format_scan_result(result)
        self.progress_view.append("\n" + summary)
        self.status_label.setText("掃描完成" if not result.get("cancelled") else "掃描已取消")
        if self._closing:
            self.accept()

    def scan_failed(self, message: str) -> None:
        self._reset_controls()
        self.status_label.setText("掃描失敗")
        self.progress_view.append(f"\n❌ 掃描失敗：{message}")
        if self._closing:
            self.reject()

    def add_findings_to_list(self, list_type: str) -> None:
        findings = (self.last_result or {}).get("findings", [])
        user_ids = sorted({str(item.get("user_id")) for item in findings if item.get("user_id") is not None})
        if not user_ids:
            QMessageBox.information(self, "沒有可加入的項目", "這次掃描沒有可加入名單的使用者 ID。")
            return
        label = "黑名單" if list_type == "blacklist" else "白名單"
        answer = QMessageBox.question(
            self,
            "確認加入名單",
            f"確定要將 {len(user_ids)} 位發現的使用者加入{label}嗎？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for user_id in user_ids:
            upsert_list_entry(list_type, user_id, reason="history-scan", account_id=self.account_id)
        self.progress_view.append(f"✅ 已將 {len(user_ids)} 位使用者加入{label}。")

    def cancel_or_close(self) -> None:
        if self.worker and self.worker.isRunning():
            self._closing = True
            self.worker.cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("正在停止掃描…")
            return
        self.reject()

    def reject(self) -> None:
        if self.worker and self.worker.isRunning():
            self._closing = True
            self.worker.cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("正在停止掃描…")
            return
        super().reject()


class TrendChartWidget(QWidget):
    """Small dependency-free bar chart for the report tab."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.values = {}
        self.setMinimumHeight(150)

    def set_values(self, values: dict) -> None:
        self.values = dict(values or {})
        self.update()

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#f8fafc"))
        if not self.values:
            painter.setPen(QColor("#64748b"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "這段期間沒有封鎖記錄")
            painter.end()
            return
        width = max(1, self.width() - 40)
        height = max(1, self.height() - 45)
        maximum = max(self.values.values())
        slot = width / max(1, len(self.values))
        bar_width = max(8, int(slot * 0.62))
        for index, (day, count) in enumerate(self.values.items()):
            bar_height = int(height * count / maximum) if maximum else 0
            x = 25 + int(index * slot + (slot - bar_width) / 2)
            y = 12 + height - bar_height
            painter.setBrush(QColor("#2563eb"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(x, y, bar_width, bar_height)
            painter.setPen(QColor("#334155"))
            painter.drawText(x, y - 4, bar_width, 18, Qt.AlignmentFlag.AlignCenter, str(count))
            painter.drawText(x - 10, 18 + height, bar_width + 20, 20, Qt.AlignmentFlag.AlignCenter, day[-5:])
        painter.end()


class AsyncOperationWorker(QThread):
    """Run one Telethon coroutine without blocking the Qt event loop."""

    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, operation: Callable[[], Coroutine[Any, Any, object]]):
        super().__init__()
        self.operation = operation

    def run(self) -> None:
        try:
            self.completed.emit(asyncio.run(self.operation()))
        except Exception as exc:
            self.failed.emit(str(exc))


class ManagementDialog(QDialog):
    """Management center for rules, reports, records, lists and account state."""

    account_changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None, network_enabled: bool = True, account_id: str = ""):
        super().__init__(parent)
        self.setWindowTitle("TeleShield 管理中心")
        self.resize(980, 720)
        self.account_id = account_id
        self.network_enabled = network_enabled
        self.group_worker: Optional[AsyncOperationWorker] = None
        self.logout_worker: Optional[AsyncOperationWorker] = None

        self.tabs = QTabWidget()
        self.tabs.addTab(self._lists_tab(), "完整名單")
        self.tabs.addTab(self._rules_tab(), "學習規則")
        self.tabs.addTab(self._reports_tab(), "報告")
        self.tabs.addTab(self._logs_tab(), "封鎖記錄")
        self.tabs.addTab(self._groups_tab(), "群組管理")
        self.tabs.addTab(self._settings_tab(), "掃描／OCR／帳號")
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)

    def _workers_running(self) -> bool:
        return any(
            worker is not None and worker.isRunning()
            for worker in (self.group_worker, self.logout_worker)
        )

    def _operation_available(self) -> bool:
        if not self._workers_running():
            return True
        QMessageBox.warning(
            self,
            "操作仍在進行",
            "同一個 Telegram 帳號一次只能執行一個背景操作，請等目前操作完成。",
        )
        return False

    def _set_network_operation_controls(self, enabled: bool) -> None:
        enabled = bool(enabled and self.network_enabled)
        for name in (
            "discover_groups_button",
            "logout_button",
            "clear_session_button",
            "clear_all_button",
        ):
            button = getattr(self, name, None)
            if button is not None:
                button.setEnabled(enabled)

    def _close_blocked_by_worker(self) -> bool:
        if not self._workers_running():
            return False
        QMessageBox.warning(
            self,
            "操作仍在進行",
            "Telegram 背景操作尚未完成；請等它結束後再關閉管理中心。",
        )
        return True

    def closeEvent(self, event) -> None:
        if self._close_blocked_by_worker():
            event.ignore()
            return
        event.accept()

    def reject(self) -> None:
        if self._close_blocked_by_worker():
            return
        super().reject()

    @staticmethod
    def _stretch_table(table: QTableWidget) -> None:
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

    def _lists_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        controls = QHBoxLayout()
        self.list_type_combo = QComboBox()
        self.list_type_combo.addItem("白名單", "whitelist")
        self.list_type_combo.addItem("黑名單", "blacklist")
        self.list_search = QLineEdit()
        self.list_search.setPlaceholderText("搜尋 ID、username、原因…")
        self.list_type_combo.currentIndexChanged.connect(self.refresh_lists)
        self.list_search.textChanged.connect(self.refresh_lists)
        controls.addWidget(QLabel("名單"))
        controls.addWidget(self.list_type_combo)
        controls.addWidget(self.list_search, 1)
        refresh = QPushButton("重新整理")
        refresh.clicked.connect(self.refresh_lists)
        controls.addWidget(refresh)
        layout.addLayout(controls)

        self.list_table = QTableWidget(0, 4)
        self.list_table.setHorizontalHeaderLabels(["User ID", "Username", "加入日期", "原因"])
        self._stretch_table(self.list_table)
        self.list_table.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.list_table.itemSelectionChanged.connect(self._populate_list_form)
        layout.addWidget(self.list_table)

        form = QFormLayout()
        self.list_user_id = QLineEdit()
        self.list_username = QLineEdit()
        self.list_reason = QLineEdit("desktop")
        form.addRow("User ID", self.list_user_id)
        form.addRow("Username", self.list_username)
        form.addRow("原因", self.list_reason)
        layout.addLayout(form)
        actions = QHBoxLayout()
        add = QPushButton("新增／更新")
        remove = QPushButton("移除選取項目")
        batch_remove = QPushButton("批次移除選取")
        import_button = QPushButton("匯入 JSON／CSV")
        export_button = QPushButton("匯出 JSON／CSV")
        add.clicked.connect(self.save_list_entry)
        remove.clicked.connect(self.delete_list_entry)
        batch_remove.clicked.connect(self.delete_selected_list_entries)
        import_button.clicked.connect(self.import_lists)
        export_button.clicked.connect(self.export_lists)
        actions.addWidget(add)
        actions.addWidget(remove)
        actions.addWidget(batch_remove)
        actions.addStretch()
        actions.addWidget(import_button)
        actions.addWidget(export_button)
        layout.addLayout(actions)
        self.refresh_lists()
        return tab

    def refresh_lists(self) -> None:
        if not hasattr(self, "list_table"):
            return
        list_type = self.list_type_combo.currentData()
        try:
            rows = list_entries(list_type, self.list_search.text(), account_id=self.account_id)
        except Exception as exc:
            QMessageBox.warning(self, "名單讀取失敗", str(exc))
            return
        self.list_table.setRowCount(0)
        for row in rows:
            index = self.list_table.rowCount()
            self.list_table.insertRow(index)
            for column, key in enumerate(("user_id", "username", "added", "reason")):
                self.list_table.setItem(index, column, QTableWidgetItem(str(row.get(key, ""))))

    def _populate_list_form(self) -> None:
        row = self.list_table.currentRow()
        if row < 0:
            return
        self.list_user_id.setText(self.list_table.item(row, 0).text())
        self.list_username.setText(self.list_table.item(row, 1).text())
        self.list_reason.setText(self.list_table.item(row, 3).text())

    def save_list_entry(self) -> None:
        try:
            upsert_list_entry(
                self.list_type_combo.currentData(),
                self.list_user_id.text(),
                self.list_username.text(),
                self.list_reason.text(),
                account_id=self.account_id,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "名單資料錯誤", str(exc))
            return
        self.refresh_lists()

    def delete_list_entry(self) -> None:
        selected_rows = sorted({index.row() for index in self.list_table.selectedIndexes()})
        user_id = self.list_user_id.text().strip()
        if len(selected_rows) == 1:
            user_id = self.list_table.item(selected_rows[0], 0).text()
        if not user_id:
            QMessageBox.information(self, "尚未選取", "請先選取要移除的名單項目。")
            return
        removed = remove_list_entry(self.list_type_combo.currentData(), user_id, account_id=self.account_id)
        if not removed:
            QMessageBox.information(self, "找不到項目", "這個 ID 不在目前名單中。")
        self.refresh_lists()

    def delete_selected_list_entries(self) -> None:
        user_ids = sorted({
            self.list_table.item(row, 0).text()
            for row in {index.row() for index in self.list_table.selectedIndexes()}
            if self.list_table.item(row, 0)
        })
        if not user_ids:
            QMessageBox.information(self, "尚未選取", "請先選取一個或多個名單項目。")
            return
        answer = QMessageBox.question(
            self,
            "確認批次移除",
            f"確定要移除 {len(user_ids)} 筆名單項目嗎？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        list_type = self.list_type_combo.currentData()
        for user_id in user_ids:
            remove_list_entry(list_type, user_id, account_id=self.account_id)
        self.refresh_lists()

    def import_lists(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "匯入名單", "", "JSON／CSV (*.json *.csv)")
        if not path:
            return
        try:
            count = import_list_entries(path, self.list_type_combo.currentData(), replace=False, account_id=self.account_id)
            self.refresh_lists()
            QMessageBox.information(self, "匯入完成", f"已匯入 {count} 筆名單項目。")
        except Exception as exc:
            QMessageBox.critical(self, "匯入失敗", str(exc))

    def export_lists(self) -> None:
        list_type = self.list_type_combo.currentData()
        path, _ = QFileDialog.getSaveFileName(self, "匯出名單", f"{list_type}.json", "JSON／CSV (*.json *.csv)")
        if not path:
            return
        try:
            count = export_list_entries(path, list_type, account_id=self.account_id)
            QMessageBox.information(self, "匯出完成", f"已匯出 {count} 筆名單項目。")
        except Exception as exc:
            QMessageBox.critical(self, "匯出失敗", str(exc))

    def _rules_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        hint = QLabel("貼上一則確定是廣告的訊息，核心會抽取關鍵詞／聯絡方式／網址模式。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.learning_text = QTextEdit()
        self.learning_text.setPlaceholderText("例如：加微信 spam_account，投資穩賺…")
        layout.addWidget(self.learning_text)
        learn_button = QPushButton("學習這則廣告")
        learn_button.clicked.connect(self.learn_sample)
        layout.addWidget(learn_button)
        self.rules_table = QTableWidget(0, 2)
        self.rules_table.setHorizontalHeaderLabels(["類型", "規則"])
        self._stretch_table(self.rules_table)
        layout.addWidget(self.rules_table)
        remove = QPushButton("刪除選取規則")
        remove.clicked.connect(self.delete_rule)
        layout.addWidget(remove)
        self.refresh_rules()
        return tab

    def refresh_rules(self) -> None:
        if not hasattr(self, "rules_table"):
            return
        rules = get_learned_patterns(account_id=self.account_id)
        self.rules_table.setRowCount(0)
        for kind, label in (("keywords", "關鍵詞"), ("patterns", "正則／模式")):
            for value in rules[kind]:
                row = self.rules_table.rowCount()
                self.rules_table.insertRow(row)
                self.rules_table.setItem(row, 0, QTableWidgetItem(label))
                self.rules_table.setItem(row, 1, QTableWidgetItem(value))

    def learn_sample(self) -> None:
        try:
            result = learn_text(self.learning_text.toPlainText(), account_id=self.account_id)
        except ValueError as exc:
            QMessageBox.warning(self, "學習失敗", str(exc))
            return
        self.learning_text.clear()
        self.refresh_rules()
        QMessageBox.information(
            self,
            "學習完成",
            f"新增關鍵詞 {len(result['added_keywords'])} 個、模式 {len(result['added_patterns'])} 個。",
        )

    def delete_rule(self) -> None:
        row = self.rules_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "尚未選取", "請先選取要刪除的規則。")
            return
        kind = "keywords" if self.rules_table.item(row, 0).text() == "關鍵詞" else "patterns"
        value = self.rules_table.item(row, 1).text()
        remove_learned_pattern(kind, value, account_id=self.account_id)
        self.refresh_rules()

    def _reports_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        controls = QHBoxLayout()
        self.report_period = QComboBox()
        self.report_period.addItem("過去 24 小時", "day")
        self.report_period.addItem("過去 7 天", "week")
        self.report_period.addItem("全部記錄", "all")
        refresh = QPushButton("重新產生")
        export = QPushButton("匯出 JSON")
        refresh.clicked.connect(self.refresh_report)
        export.clicked.connect(self.export_report)
        controls.addWidget(QLabel("期間"))
        controls.addWidget(self.report_period)
        controls.addWidget(refresh)
        controls.addWidget(export)
        controls.addStretch()
        layout.addLayout(controls)
        self.report_view = QTextEdit()
        self.report_view.setReadOnly(True)
        layout.addWidget(self.report_view)
        self.report_chart = TrendChartWidget()
        layout.addWidget(self.report_chart)
        self.report_period.currentIndexChanged.connect(self.refresh_report)
        self.refresh_report()
        return tab

    def refresh_report(self) -> None:
        if not hasattr(self, "report_view"):
            return
        result = build_report(self.report_period.currentData(), account_id=self.account_id)
        lines = [
            f"{result['label']}：共 {result['total']} 筆處理記錄",
            "",
            "來源：" + ("、".join(f"{key} {value}" for key, value in result["by_source"].items()) or "無"),
            "熱門原因：" + ("、".join(f"{key} ({value})" for key, value in result["by_reason"].items()) or "無"),
            "",
            "每日趨勢：",
        ]
        lines.extend(f"  {day}: {count}" for day, count in result["trend"].items())
        self.report_view.setPlainText("\n".join(lines))
        self.report_chart.set_values(result["trend"])

    def export_report(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "匯出報告", "teleshield-report.json", "JSON (*.json)")
        if not path:
            return
        result = build_report(self.report_period.currentData(), account_id=self.account_id)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(result, handle, indent=2, ensure_ascii=False)
            QMessageBox.information(self, "匯出完成", "報告已匯出。")
        except OSError as exc:
            QMessageBox.critical(self, "匯出失敗", str(exc))

    def _logs_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        controls = QHBoxLayout()
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText("搜尋名稱、ID、原因…")
        self.log_source = QComboBox()
        self.log_source.addItem("全部來源", "all")
        self.log_source.addItem("私訊", "private")
        self.log_source.addItem("群組", "group")
        refresh = QPushButton("重新整理")
        export_json = QPushButton("匯出 JSON")
        export_csv = QPushButton("匯出 CSV")
        refresh.clicked.connect(self.refresh_logs)
        export_json.clicked.connect(lambda: self.export_logs("json"))
        export_csv.clicked.connect(lambda: self.export_logs("csv"))
        self.log_search.textChanged.connect(self.refresh_logs)
        self.log_source.currentIndexChanged.connect(self.refresh_logs)
        controls.addWidget(self.log_search, 1)
        controls.addWidget(self.log_source)
        controls.addWidget(refresh)
        controls.addWidget(export_json)
        controls.addWidget(export_csv)
        layout.addLayout(controls)
        self.log_table = QTableWidget(0, 5)
        self.log_table.setHorizontalHeaderLabels(["時間", "來源", "User ID", "名稱", "原因"])
        self._stretch_table(self.log_table)
        layout.addWidget(self.log_table)
        self.refresh_logs()
        return tab

    def refresh_logs(self) -> None:
        if not hasattr(self, "log_table"):
            return
        rows = get_block_records(self.log_search.text(), self.log_source.currentData(), account_id=self.account_id)
        self.log_table.setRowCount(0)
        for record in rows:
            row = self.log_table.rowCount()
            self.log_table.insertRow(row)
            values = [record.get("time", ""), record.get("source", ""), record.get("user_id", ""), record.get("name", ""), record.get("reason", "")]
            for column, value in enumerate(values):
                self.log_table.setItem(row, column, QTableWidgetItem(str(value)))

    def export_logs(self, fmt: str) -> None:
        suffix = "csv" if fmt == "csv" else "json"
        path, _ = QFileDialog.getSaveFileName(self, "匯出封鎖記錄", f"teleshield-block-log.{suffix}", f"{suffix.upper()} (*.{suffix})")
        if not path:
            return
        try:
            count = export_block_records(path, self.log_search.text(), self.log_source.currentData(), fmt, account_id=self.account_id)
            QMessageBox.information(self, "匯出完成", f"已匯出 {count} 筆封鎖記錄。")
        except Exception as exc:
            QMessageBox.critical(self, "匯出失敗", str(exc))

    def _groups_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.group_hint = QLabel(
            "只列出目前帳號有管理權限的群組；停用個別群組後，即時防護與歷史群組掃描都會跳過它。"
        )
        self.group_hint.setWordWrap(True)
        layout.addWidget(self.group_hint)
        actions = QHBoxLayout()
        self.discover_groups_button = QPushButton("從 Telegram 重新讀取")
        toggle = QPushButton("切換選取群組")
        self.discover_groups_button.clicked.connect(self.discover_groups)
        toggle.clicked.connect(self.toggle_group)
        actions.addWidget(self.discover_groups_button)
        actions.addWidget(toggle)
        actions.addStretch()
        layout.addLayout(actions)
        self.group_table = QTableWidget(0, 5)
        self.group_table.setHorizontalHeaderLabels(["群組", "Username", "ID", "權限", "狀態"])
        self._stretch_table(self.group_table)
        layout.addWidget(self.group_table)
        if not self.network_enabled:
            self.discover_groups_button.setEnabled(False)
            self.group_hint.setText("即時防護正在使用 Telegram Session；請先停止防護，再重新讀取群組。")
        self.refresh_groups()
        return tab

    def refresh_groups(self) -> None:
        if not hasattr(self, "group_table"):
            return
        groups = load_config(self.account_id).get("managed_groups", [])
        self.group_table.setRowCount(0)
        for group in groups:
            row = self.group_table.rowCount()
            self.group_table.insertRow(row)
            values = [
                group.get("title", ""),
                f"@{group.get('username')}" if group.get("username") else "",
                group.get("id", ""),
                "創建者" if group.get("is_creator") else "管理員",
                "啟用" if group.get("enabled", True) else "停用",
            ]
            for column, value in enumerate(values):
                self.group_table.setItem(row, column, QTableWidgetItem(str(value)))

    def discover_groups(self) -> None:
        if not self._operation_available():
            return
        self._set_network_operation_controls(False)
        self.group_hint.setText("正在讀取群組與管理員權限…")
        self.group_worker = AsyncOperationWorker(lambda: discover_managed_groups(self.account_id))
        self.group_worker.completed.connect(self.groups_discovered)
        self.group_worker.failed.connect(self.groups_failed)
        self.group_worker.start()

    def groups_discovered(self, groups: object) -> None:
        self._set_network_operation_controls(True)
        self.refresh_groups()
        self.group_hint.setText(f"已讀取 {len(groups) if isinstance(groups, list) else 0} 個可管理群組。")

    def groups_failed(self, message: str) -> None:
        self._set_network_operation_controls(True)
        self.group_hint.setText("群組讀取失敗")
        QMessageBox.warning(self, "群組讀取失敗", message)

    def toggle_group(self) -> None:
        row = self.group_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "尚未選取", "請先選取一個群組。")
            return
        group_id = self.group_table.item(row, 2).text()
        currently_enabled = self.group_table.item(row, 4).text() == "啟用"
        set_managed_group_enabled(group_id, not currently_enabled, account_id=self.account_id)
        self.refresh_groups()

    def _settings_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        scan_box = QGroupBox("歷史掃描範圍")
        scan_form = QFormLayout(scan_box)
        self.scan_spins = {}
        labels = {
            "private_dialog_limit": "私訊對話數上限",
            "private_message_limit": "每個私訊最多幾則",
            "private_days": "私訊最近幾天",
            "group_dialog_limit": "群組對話數上限",
            "group_message_limit": "每個群組最多幾則",
            "group_days": "群組最近幾天",
        }
        settings = get_scan_settings(account_id=self.account_id)
        for key, label in labels.items():
            spin = QSpinBox()
            spin.setRange(1, 365 if key.endswith("days") else 100)
            spin.setValue(settings[key])
            self.scan_spins[key] = spin
            scan_form.addRow(label, spin)
        save_scan_button = QPushButton("儲存掃描設定")
        save_scan_button.clicked.connect(self.save_scan_settings)
        scan_form.addRow(save_scan_button)
        layout.addWidget(scan_box)

        ocr_box = QGroupBox("圖片 OCR")
        ocr_layout = QHBoxLayout(ocr_box)
        self.ocr_status_label = QLabel()
        ocr_refresh = QPushButton("重新檢查")
        ocr_refresh.clicked.connect(self.refresh_ocr_status)
        ocr_layout.addWidget(self.ocr_status_label, 1)
        ocr_layout.addWidget(ocr_refresh)
        layout.addWidget(ocr_box)
        self.refresh_ocr_status()

        account_box = QGroupBox("Telegram 帳號／Session")
        account_layout = QVBoxLayout(account_box)
        account_layout.addWidget(QLabel("登出會撤銷 Telegram Session；刪除本機 Session 不會輸出任何驗證碼或密碼。"))
        account_actions = QHBoxLayout()
        self.logout_button = QPushButton("登出 Telegram（保留 API 設定）")
        self.clear_session_button = QPushButton("只刪除本機 Session")
        self.clear_all_button = QPushButton("刪除 Session 與 API 設定")
        self.logout_button.clicked.connect(lambda: self.logout(False))
        self.clear_session_button.clicked.connect(lambda: self.clear_local(False))
        self.clear_all_button.clicked.connect(lambda: self.clear_local(True))
        account_actions.addWidget(self.logout_button)
        account_actions.addWidget(self.clear_session_button)
        account_actions.addWidget(self.clear_all_button)
        account_layout.addLayout(account_actions)
        layout.addWidget(account_box)
        if not self.network_enabled:
            self.logout_button.setEnabled(False)
            self.clear_session_button.setEnabled(False)
            self.clear_all_button.setEnabled(False)
        layout.addStretch()
        return tab

    def save_scan_settings(self) -> None:
        update_scan_settings({key: spin.value() for key, spin in self.scan_spins.items()}, account_id=self.account_id)
        QMessageBox.information(self, "設定已儲存", "歷史掃描設定已更新。")

    def refresh_ocr_status(self) -> None:
        status = get_ocr_status()
        if status["available"] and status["bundled"]:
            text = "✅ OCR 可用（已使用應用程式內建 Tesseract，中文／英文）"
        elif status["available"]:
            text = "✅ OCR 可用（使用系統 Tesseract；中文語言包請自行確認）"
        else:
            text = "⚠️ 找不到 Tesseract；圖片廣告辨識目前停用，文字廣告仍可正常處理。"
        self.ocr_status_label.setText(text)
        self.ocr_status_label.setWordWrap(True)

    def logout(self, remove_credentials: bool) -> None:
        if not self._operation_available():
            return
        answer = QMessageBox.question(
            self,
            "確認登出",
            "這會讓目前 Telegram Session 登出，確定要繼續嗎？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_network_operation_controls(False)
        self.logout_worker = AsyncOperationWorker(lambda: logout_account(remove_credentials, account_id=self.account_id))
        self.logout_worker.completed.connect(self.logout_completed)
        self.logout_worker.failed.connect(self.logout_failed)
        self.logout_worker.start()

    def logout_completed(self, logged_out: object) -> None:
        self._set_network_operation_controls(True)
        self.account_changed.emit()
        QMessageBox.information(self, "登出完成", "Telegram Session 已清除；請重新登入以恢復防護。")

    def logout_failed(self, message: str) -> None:
        self._set_network_operation_controls(True)
        QMessageBox.critical(self, "登出失敗", message)

    def clear_local(self, remove_credentials: bool) -> None:
        if not self._operation_available():
            return
        label = "Session 與 API 設定" if remove_credentials else "本機 Session"
        answer = QMessageBox.question(
            self,
            "確認刪除",
            f"確定要刪除{label}嗎？這不會刪除 Telegram 雲端帳號。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            clear_local_session(remove_credentials=remove_credentials, account_id=self.account_id)
        except Exception as exc:
            QMessageBox.critical(self, "刪除失敗", str(exc))
            return
        self.account_changed.emit()
        QMessageBox.information(self, "已刪除", f"{label}已從本機移除。")


class MainWindow(QMainWindow):
    def __init__(self, background: bool = False):
        super().__init__()
        self.background_start = background
        self.allow_close = False
        try:
            ensure_account_registry()
        except Exception as exc:
            self.registry_error = str(exc)
        else:
            self.registry_error = ""
        self.listeners: dict[str, ListenerWorker] = {}
        self.active_account_id: Optional[str] = get_active_account_id()
        self._updating_accounts = False
        self._updating_auto_start = False
        self.setWindowTitle("TeleShield")
        self.resize(720, 540)

        self._build_ui()
        self._build_tray()
        self.refresh_accounts()
        self.refresh_status()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_status)
        self.refresh_timer.start(3000)

        self.auto_start_checkbox.setChecked(is_start_on_login_enabled())

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)

        title = QLabel("🛡️ TeleShield")
        title.setStyleSheet("font-size: 24px; font-weight: 700;")
        subtitle = QLabel("Telegram 個人帳號廣告防護")
        subtitle.setStyleSheet("color: #666;")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        account_box = QGroupBox("Telegram 多帳號")
        account_layout = QHBoxLayout(account_box)
        self.account_combo = QComboBox()
        self.account_combo.currentIndexChanged.connect(self.select_account)
        self.add_account_button = QPushButton("新增帳號")
        self.remove_account_button = QPushButton("移除目前帳號")
        self.start_all_button = QPushButton("全部啟動")
        self.stop_all_button = QPushButton("全部停止")
        self.add_account_button.clicked.connect(self.add_account)
        self.remove_account_button.clicked.connect(self.remove_current_account)
        self.start_all_button.clicked.connect(self.start_all_protection)
        self.stop_all_button.clicked.connect(self.stop_all_protection)
        account_layout.addWidget(QLabel("管理帳號"))
        account_layout.addWidget(self.account_combo, 1)
        account_layout.addWidget(self.add_account_button)
        account_layout.addWidget(self.remove_account_button)
        account_layout.addWidget(self.start_all_button)
        account_layout.addWidget(self.stop_all_button)
        layout.addWidget(account_box)

        status_box = QGroupBox("目前狀態")
        status_layout = QGridLayout(status_box)
        self.status_label = QLabel("尚未啟動")
        self.account_label = QLabel("尚未登入")
        self.count_label = QLabel("")
        status_layout.addWidget(QLabel("防護"), 0, 0)
        status_layout.addWidget(self.status_label, 0, 1)
        status_layout.addWidget(QLabel("帳號"), 1, 0)
        status_layout.addWidget(self.account_label, 1, 1)
        status_layout.addWidget(QLabel("名單／封鎖"), 2, 0)
        status_layout.addWidget(self.count_label, 2, 1)
        layout.addWidget(status_box)

        actions = QHBoxLayout()
        self.setup_button = QPushButton("登入 Telegram")
        self.start_button = QPushButton("開始防護")
        self.stop_button = QPushButton("停止防護")
        self.history_scan_button = QPushButton("掃描既有訊息")
        self.management_button = QPushButton("管理中心")
        self.start_button.clicked.connect(self.start_protection)
        self.stop_button.clicked.connect(self.stop_protection)
        self.setup_button.clicked.connect(self.open_setup)
        self.history_scan_button.clicked.connect(self.open_history_scan)
        self.management_button.clicked.connect(self.open_management)
        actions.addWidget(self.setup_button)
        actions.addWidget(self.start_button)
        actions.addWidget(self.stop_button)
        actions.addWidget(self.history_scan_button)
        actions.addWidget(self.management_button)
        layout.addLayout(actions)

        tabs = QTabWidget()
        tabs.addTab(self._list_tab(), "黑名單／白名單")
        tabs.addTab(self._settings_tab(), "設定")
        layout.addWidget(tabs)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("事件摘要會顯示在這裡。詳細封鎖記錄保存在本機資料目錄。")
        layout.addWidget(self.log_view)
        self.setCentralWidget(root)

    def _list_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        hint = QLabel("輸入 Telegram numeric user ID；設定會立即套用到背景防護。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.user_id_edit = QLineEdit()
        self.user_id_edit.setPlaceholderText("例如 123456789")
        grid = QGridLayout()
        grid.addWidget(QLabel("使用者 ID"), 0, 0)
        grid.addWidget(self.user_id_edit, 0, 1, 1, 2)

        for row, list_type, label in (
            (1, "whitelist", "白名單"),
            (2, "blacklist", "黑名單"),
        ):
            add_button = QPushButton(f"加入{label}")
            remove_button = QPushButton(f"移除{label}")
            add_button.clicked.connect(lambda _=False, kind=list_type: self.update_list(kind, "add"))
            remove_button.clicked.connect(lambda _=False, kind=list_type: self.update_list(kind, "remove"))
            grid.addWidget(add_button, row, 0)
            grid.addWidget(remove_button, row, 1)
            grid.addWidget(QLabel(f"目前：{label}"), row, 2)
        layout.addLayout(grid)

        refresh = QPushButton("重新整理")
        refresh.clicked.connect(self.refresh_status)
        layout.addWidget(refresh)
        layout.addStretch()
        return tab

    def _settings_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.auto_start_checkbox = QCheckBox("登入系統時自動啟動 TeleShield")
        self.auto_start_account_combo = QComboBox()
        self.auto_start_account_combo.currentIndexChanged.connect(self.update_auto_start_account)
        self.auto_start_checkbox.toggled.connect(self.update_startup)
        layout.addWidget(self.auto_start_checkbox)
        layout.addWidget(QLabel("啟動後自動開始防護的帳號"))
        layout.addWidget(self.auto_start_account_combo)
        layout.addWidget(
            QLabel(
                "可選擇一個已登入帳號在下次啟動時自動開始防護；選擇「不自動啟動」即可停用。\n"
                "其他帳號仍可在程式內按「全部啟動」或個別啟動。\n"
                "關閉主視窗只會縮到系統匣，背景防護不會停止。"
            )
        )
        layout.addStretch()
        return tab

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.make_tray_icon(), self)
        self.tray.setToolTip("TeleShield 防護中")
        menu = QMenu(self)
        show_action = QAction("開啟 TeleShield", self)
        start_action = QAction("開始防護", self)
        stop_action = QAction("停止防護", self)
        exit_action = QAction("結束程式", self)
        show_action.triggered.connect(self.show_window)
        start_action.triggered.connect(self.start_protection)
        stop_action.triggered.connect(self.stop_protection)
        exit_action.triggered.connect(self.quit_application)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(start_action)
        menu.addAction(stop_action)
        menu.addSeparator()
        menu.addAction(exit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()

    @staticmethod
    def make_tray_icon() -> QIcon:
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#2563eb"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(2, 2, 28, 28)
        painter.setPen(QColor("white"))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "T")
        painter.end()
        return QIcon(pixmap)

    def tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_window()

    def show_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    @staticmethod
    def _format_account_label(record: dict) -> str:
        name = record.get("display_name") or ""
        username = f"@{record.get('username')}" if record.get("username") else ""
        identity = name or username or (f"ID {record.get('user_id')}" if record.get("user_id") else "未登入帳號")
        if username and name:
            identity = f"{name} ({username})"
        return f"{identity} · {record.get('id', '')}"

    def refresh_accounts(self) -> None:
        if not hasattr(self, "account_combo"):
            return
        records = list_accounts()
        ids = [str(record.get("id")) for record in records if record.get("id")]
        if self.active_account_id not in ids:
            self.active_account_id = ids[0] if ids else None
            if self.active_account_id:
                try:
                    set_active_account(self.active_account_id)
                except Exception:
                    pass
        self._updating_accounts = True
        try:
            self.account_combo.clear()
            for record in records:
                self.account_combo.addItem(self._format_account_label(record), record.get("id"))
            if self.active_account_id:
                index = self.account_combo.findData(self.active_account_id)
                if index >= 0:
                    self.account_combo.setCurrentIndex(index)
        finally:
            self._updating_accounts = False
        self.refresh_auto_start_accounts()
        has_account = bool(self.active_account_id)
        self.remove_account_button.setEnabled(has_account)
        self.start_all_button.setEnabled(bool(records))
        self.stop_all_button.setEnabled(bool(self.listeners))

    def refresh_auto_start_accounts(self) -> None:
        if not hasattr(self, "auto_start_account_combo"):
            return
        selected = get_auto_start_account_id()
        records = list_accounts()
        self._updating_auto_start = True
        try:
            self.auto_start_account_combo.clear()
            self.auto_start_account_combo.addItem("不自動啟動", None)
            for record in records:
                self.auto_start_account_combo.addItem(self._format_account_label(record), record.get("id"))
            index = self.auto_start_account_combo.findData(selected)
            self.auto_start_account_combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self._updating_auto_start = False

    def update_auto_start_account(self, index: int) -> None:
        if self._updating_auto_start or index < 0:
            return
        account_id = self.auto_start_account_combo.itemData(index)
        try:
            set_auto_start_account(str(account_id) if account_id else None)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "自動啟動設定失敗", str(exc))
            self.refresh_auto_start_accounts()
            return
        if account_id:
            self.log_view.append(f"✅ 下次啟動將自動開始帳號 {account_id} 的防護")
        else:
            self.log_view.append("⏹ 已停用啟動時自動防護")

    def select_account(self, index: int) -> None:
        if self._updating_accounts or index < 0:
            return
        account_id = self.account_combo.itemData(index)
        if not account_id:
            return
        self.active_account_id = str(account_id)
        try:
            set_active_account(self.active_account_id)
        except ValueError:
            return
        self.log_view.append(f"✅ 已切換管理帳號：{self.account_combo.currentText()}")
        self.refresh_status()

    def add_account(self) -> None:
        record = create_account()
        dialog = SetupDialog(self, account_id=record["id"], temporary_account=True)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.active_account_id = record["id"]
            set_active_account(record["id"])
            self.refresh_accounts()
            self.refresh_status()
            self.log_view.append("✅ 已新增並登入 Telegram 帳號")
        else:
            self.refresh_accounts()

    def remove_current_account(self) -> None:
        account_id = self.active_account_id
        if not account_id:
            return
        worker = self.listeners.get(account_id)
        if worker and worker.isRunning():
            answer = QMessageBox.question(
                self,
                "帳號正在防護",
                "這個帳號目前正在防護；移除前必須先停止它。確定繼續嗎？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            worker.stop()
            if not worker.wait(10000):
                QMessageBox.warning(self, "停止逾時", "帳號仍在防護中，沒有刪除任何本機資料。")
                return
        answer = QMessageBox.question(
            self,
            "確認移除帳號",
            "這會刪除該帳號的本機 Session、設定、名單、群組與歷史記錄；其他帳號不會受到影響。確定嗎？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not remove_account(account_id, delete_files=True):
            QMessageBox.warning(self, "移除失敗", "本機帳號資料未能完整刪除，帳號仍保留在索引中。")
            return
        self.listeners.pop(account_id, None)
        self.active_account_id = get_active_account_id()
        self.refresh_accounts()
        self.refresh_status()
        self.log_view.append("✅ 已移除目前帳號的本機資料")

    def _start_account_protection(self, account_id: str) -> bool:
        existing = self.listeners.get(account_id)
        if existing and existing.isRunning():
            return False
        if not load_config(account_id).get("api_id"):
            return False
        worker = ListenerWorker(account_id)
        worker.started_ok.connect(lambda account_id=account_id: self.listener_started(account_id))
        worker.stopped.connect(lambda message, account_id=account_id: self.listener_stopped(account_id, message))
        self.listeners[account_id] = worker
        worker.start()
        return True

    def start_all_protection(self, only_auto: bool = False) -> None:
        records = list_accounts()
        started = 0
        missing = 0
        for record in records:
            account_id = str(record.get("id"))
            cfg = load_config(account_id)
            if only_auto and not cfg.get("auto_start_protection"):
                continue
            if cfg.get("api_id"):
                started += int(self._start_account_protection(account_id))
            else:
                missing += 1
        self.refresh_status()
        if missing:
            self.log_view.append(f"⚠️ 有 {missing} 個帳號尚未完成登入，已跳過")
        if started:
            self.log_view.append(f"✅ 已啟動 {started} 個帳號的防護")

    def stop_all_protection(self) -> None:
        running = [worker for worker in self.listeners.values() if worker.isRunning()]
        for worker in running:
            worker.stop()
        timed_out = [worker.account_id for worker in running if not worker.wait(10000)]
        if timed_out:
            QMessageBox.warning(self, "停止逾時", f"有 {len(timed_out)} 個帳號尚未停止，請稍後再試。")
        self.refresh_status()

    def refresh_status(self) -> None:
        cfg = load_config(self.active_account_id)
        running = bool(
            self.active_account_id
            and self.listeners.get(self.active_account_id)
            and self.listeners[self.active_account_id].isRunning()
        )
        running_count = sum(1 for worker in self.listeners.values() if worker.isRunning())
        if cfg.get("user_id"):
            username = f"@{cfg['username']}" if cfg.get("username") else "無 username"
            self.account_label.setText(f"{username} (ID: {cfg['user_id']})")
            self.setup_button.setText("重新登入 Telegram")
        elif self.active_account_id:
            self.account_label.setText("帳號尚未登入")
            self.setup_button.setText("登入 Telegram")
        else:
            self.account_label.setText("尚未新增帳號")
            self.setup_button.setText("新增 Telegram 帳號")

        self.status_label.setText("防護中" if running else "已停止")
        if running_count > 1:
            self.status_label.setText(f"防護中（目前帳號；共 {running_count} 個）")
        self.start_button.setEnabled(bool(self.active_account_id) and not running)
        self.stop_button.setEnabled(running)
        self.history_scan_button.setEnabled(bool(self.active_account_id) and not running)
        self.count_label.setText(
            f"白 {len(cfg.get('whitelist', {}))} / 黑 {len(cfg.get('blacklist', {}))} / "
            f"私訊封鎖 {cfg.get('blocked_count', 0)} / 群組踢除 {cfg.get('kicked_count', 0)}"
        )
        self.stop_all_button.setEnabled(running_count > 0)
        self.tray.setToolTip(f"TeleShield 防護中（{running_count} 個帳號）" if running_count else "TeleShield")

    def open_setup(self) -> None:
        if self.active_account_id:
            account_id = self.active_account_id
            worker = self.listeners.get(account_id)
            if worker and worker.isRunning():
                QMessageBox.information(
                    self,
                    "請先停止目前帳號的即時防護",
                    "重新登入會替換 Telegram Session；請先停止這個帳號的防護，避免兩個 client 同時使用同一個 Session。",
                )
                return
            temporary = False
        else:
            record = create_account()
            account_id = record["id"]
            temporary = True
        dialog = SetupDialog(self, account_id=account_id, temporary_account=temporary)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.active_account_id = account_id
            set_active_account(account_id)
            self.refresh_accounts()
            self.refresh_status()
            self.log_view.append("✅ Telegram 登入設定完成")
        else:
            self.refresh_accounts()
            self.refresh_status()

    def open_management(self) -> None:
        account_id = self.active_account_id
        if not account_id:
            QMessageBox.information(self, "尚未新增帳號", "請先新增並登入 Telegram 帳號。")
            self.open_setup()
            account_id = self.active_account_id
            if not account_id:
                return
        worker = self.listeners.get(account_id)
        dialog = ManagementDialog(
            self,
            network_enabled=not bool(worker and worker.isRunning()),
            account_id=account_id,
        )
        dialog.account_changed.connect(self.refresh_status)
        dialog.account_changed.connect(self.refresh_accounts)
        dialog.exec()
        self.refresh_status()

    def start_protection(self) -> None:
        if not self.active_account_id:
            self.open_setup()
            return
        account_id = self.active_account_id
        if self.listeners.get(account_id) and self.listeners[account_id].isRunning():
            return
        if not load_config(account_id).get("api_id"):
            QMessageBox.information(self, "尚未登入", "請先登入目前選取的 Telegram 帳號。")
            self.open_setup()
            if not load_config(account_id).get("api_id"):
                return
        if self._start_account_protection(account_id):
            self.status_label.setText("啟動中")

    def listener_started(self, account_id: str) -> None:
        if account_id == self.active_account_id:
            self.status_label.setText("防護中")
        self.log_view.append(f"[{self.now()}] ✅ 帳號 {account_id} 背景防護已啟動")
        self.refresh_accounts()
        self.refresh_status()

    def stop_protection(self) -> None:
        account_id = self.active_account_id
        worker = self.listeners.get(account_id) if account_id else None
        if not worker or not worker.isRunning():
            return
        self.status_label.setText("停止中")
        worker.stop()
        if not worker.wait(10000):
            QMessageBox.warning(self, "停止逾時", "Telegram 連線尚未回應，請稍後再試。")

    def open_history_scan(self) -> None:
        account_id = self.active_account_id
        if not account_id:
            QMessageBox.information(self, "尚未新增帳號", "請先新增並登入 Telegram 帳號。")
            self.open_setup()
            account_id = self.active_account_id
            if not account_id:
                return
        worker = self.listeners.get(account_id)
        if worker and worker.isRunning():
            QMessageBox.information(
                self,
                "請先停止目前帳號的即時防護",
                "歷史掃描需要獨佔這個帳號的 Telegram Session；其他帳號的防護不會受到影響。",
            )
            return
        if not load_config(account_id).get("api_id"):
            QMessageBox.information(self, "尚未登入", "請先登入目前選取的 Telegram 帳號。")
            self.open_setup()
            if not load_config(account_id).get("api_id"):
                return
        dialog = HistoryScanDialog(self, account_id=account_id)
        dialog.exec()
        self.refresh_status()

    def listener_stopped(self, account_id: str, message: str) -> None:
        self.log_view.append(f"[{self.now()}] {account_id}: {message}")
        self.refresh_accounts()
        self.refresh_status()

    def update_list(self, list_type: str, action: str) -> None:
        user_id = self.user_id_edit.text().strip()
        if not user_id or not user_id.lstrip("-").isdigit():
            QMessageBox.warning(self, "使用者 ID 錯誤", "請輸入 numeric Telegram user ID。")
            return
        try:
            if action == "add":
                upsert_list_entry(list_type, user_id, reason="desktop", account_id=self.active_account_id)
                self.log_view.append(f"✅ 已加入 {list_type}: {user_id}")
            else:
                remove_list_entry(list_type, user_id, account_id=self.active_account_id)
                self.log_view.append(f"✅ 已從 {list_type} 移除: {user_id}")
        except ValueError as exc:
            QMessageBox.warning(self, "名單更新失敗", str(exc))
            return
        self.user_id_edit.clear()
        self.refresh_status()

    def update_startup(self, enabled: bool) -> None:
        try:
            set_start_on_login(enabled)
        except Exception as exc:
            self.auto_start_checkbox.blockSignals(True)
            self.auto_start_checkbox.setChecked(not enabled)
            self.auto_start_checkbox.blockSignals(False)
            QMessageBox.warning(self, "開機啟動設定失敗", str(exc))
            return
        self.log_view.append(f"{'✅ 已啟用' if enabled else '⏹ 已停用'} 開機自動啟動")

    def maybe_auto_start(self) -> None:
        account_id = get_auto_start_account_id()
        if not account_id:
            if self.background_start:
                self.tray.showMessage(
                    "TeleShield",
                    "已在系統匣啟動；目前沒有設定自動啟動的 Telegram 帳號。",
                    QSystemTrayIcon.MessageIcon.Information,
                    5000,
                )
            return
        cfg = load_config(account_id)
        if not cfg.get("api_id"):
            self.log_view.append(f"⚠️ 自動啟動帳號 {account_id} 尚未完成登入，已跳過")
            return

        def start_selected_account() -> None:
            self.active_account_id = account_id
            try:
                set_active_account(account_id)
            except ValueError:
                return
            self.refresh_accounts()
            self._start_account_protection(account_id)
            self.refresh_status()

        QTimer.singleShot(250, start_selected_account)

    def closeEvent(self, event) -> None:
        if self.allow_close:
            event.accept()
            return
        self.hide()
        self.tray.showMessage(
            "TeleShield",
            "視窗已隱藏，防護仍在背景執行。從系統匣重新開啟或選擇「結束」。",
            QSystemTrayIcon.MessageIcon.Information,
            4000,
        )
        event.ignore()

    def quit_application(self) -> None:
        self.allow_close = True
        self.stop_all_protection()
        self.tray.hide()
        QApplication.instance().quit()

    @staticmethod
    def now() -> str:
        return datetime.now().strftime("%H:%M:%S")


def main() -> int:
    parser = argparse.ArgumentParser(description="TeleShield desktop application")
    parser.add_argument("--background", action="store_true", help="start hidden in the system tray")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("TeleShield")
    app.setOrganizationName("TeleShield")
    app.setQuitOnLastWindowClosed(False)

    window = MainWindow(background=args.background)
    window.maybe_auto_start()
    if not args.background:
        window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
