"""Qt dashboard for the local system monitor."""
import csv
from datetime import datetime, timedelta
from functools import lru_cache
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

from PySide6.QtCore import QAbstractTableModel, QLocale, QModelIndex, QRectF, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
                               QPushButton, QPlainTextEdit, QScrollArea, QSizePolicy,
                               QTabWidget, QTableView, QVBoxLayout, QWidget)

import updater
from monitor import database, size
from network import probe_target, save_probe_target, save_watchlist, watch_codes, watchlist

COLORS = ["#76ccff", "#c8a2ff", "#6fe1bd", "#ffbb76"]
LEVELS = {0: "Emergency", 1: "Alert", 2: "Critical", 3: "Error", 4: "Warning", 5: "Notice", 6: "Info", 7: "Debug"}
THEME = """
QMainWindow, QWidget#base, QScrollArea, QWidget#scrollBody {background:#10151e; color:#e7edf7;}
QWidget {color:#e7edf7; font-size:13px;}
QLabel#title {font-size:27px; font-weight:700; color:#f4f7fc;}
QLabel#section {font-size:17px; font-weight:650;}
QLabel#muted {color:#9baac0;}
QLabel#metricTitle {color:#adbbce; font-size:13px; font-weight:600;}
QLabel#metricValue {font-size:26px; font-weight:700;}
QLabel#live {background:#15322c; color:#82e6c3; padding:7px 13px; border-radius:12px; font-weight:650;}
QLabel#waiting {background:#352b21; color:#ffd099; padding:7px 13px; border-radius:12px; font-weight:650;}
QFrame#card {background:#191f2b; border:1px solid #2c3546; border-radius:12px;}
QTabWidget::pane {border:0; border-top:1px solid #30394a;}
QTabBar::tab {background:#10151e; color:#a4b2c6; padding:12px 19px; margin-right:5px;}
QTabBar::tab:selected {color:#eaf5ff; border-bottom:3px solid #76ccff;}
QTabBar::tab:hover {background:#1b2431;}
QPushButton, QLineEdit, QComboBox {background:#202938; border:1px solid #35435a; border-radius:6px; padding:7px 9px; min-height:18px;}
QPushButton:hover {background:#2c3c52;}
QPushButton:disabled {color:#788499;}
QLineEdit:focus, QComboBox:focus {border-color:#76ccff;}
QComboBox QAbstractItemView {background:#202938; selection-background-color:#36506e;}
QScrollBar:vertical {background:#1a2330; width:12px; margin:0;}
QScrollBar::handle:vertical {background:#455774; min-height:22px; border-radius:4px; margin:2px;}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {height:0;}
QTableView, QPlainTextEdit {background:#171e29; alternate-background-color:#1b2330; border:1px solid #2e394b; border-radius:6px; gridline-color:#2e394b; selection-background-color:#304866; color:#e0e8f4;}
QHeaderView::section {background:#222c3b; color:#b8c8dd; padding:7px; border:0; border-right:1px solid #2e394b;}
QScrollArea {border:0;}
"""


def label(text, role=""):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        widget.setObjectName(role)
    return widget


def local_time(value):
    return datetime.fromtimestamp(value).strftime("%b %d  %H:%M:%S")


def rate(value):
    return size(value) + "/s" if value is not None else "—"


def percent(value):
    return f"{value:.1f}%" if value is not None else "—"


def celsius(value):
    return f"{value:.1f} °C" if value is not None else "—"


@lru_cache(maxsize=256)
def country_name(code):
    if not code:
        return "Unknown"
    if code == "Local":
        return "Local / non-public"
    territory = QLocale.codeToTerritory(code)
    return f"{QLocale.territoryToString(territory)} ({code})" if territory != QLocale.Country.AnyTerritory else code


def duration(seconds):
    return str(timedelta(seconds=int(seconds)))


class TableModel(QAbstractTableModel):
    def __init__(self, headers, formats=None, severity=None):
        super().__init__()
        self.headers = headers
        self.formats = formats or {}
        self.severity = severity
        self.rows = []
        self.order = (-1, Qt.SortOrder.AscendingOrder)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        value = self.rows[index.row()][index.column()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            if value is None:
                return "—"
            formatter = self.formats.get(index.column())
            return formatter(value) if formatter else str(value)
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == self.severity:
            return QColor("#ff9b9b" if value <= 3 else "#ffcb92" if value == 4 else "#82c6f5")
        if role == Qt.ItemDataRole.ForegroundRole and value == "Watchlist":
            return QColor("#ffcb92")
        if role == Qt.ItemDataRole.ForegroundRole and value == "failed":
            return QColor("#ff9b9b")
        return None

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = list(rows)
        self.apply_sort()
        self.endResetModel()

    def apply_sort(self):
        col, order = self.order
        if col < 0 or not self.rows:
            return
        # Keep unsupported readings at the end in either sort direction.
        complete = [row for row in self.rows if row[col] is not None]
        missing = [row for row in self.rows if row[col] is None]
        complete.sort(key=lambda row: row[col].casefold() if isinstance(row[col], str) else row[col],
                      reverse=order == Qt.SortOrder.DescendingOrder)
        self.rows = complete + missing

    def sort(self, column, order):
        self.order = (column, order)
        self.beginResetModel()
        self.apply_sort()
        self.endResetModel()


def make_table(headers, formats=None, sort_col=0, descending=False, widths=None, severity=None):
    view = QTableView()
    model = TableModel(headers, formats, severity)
    view.setModel(model)
    view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QTableView.SelectionMode.SingleSelection)
    view.setAlternatingRowColors(True)
    view.verticalHeader().hide()
    view.verticalHeader().setDefaultSectionSize(27)
    view.setShowGrid(False)
    view.setWordWrap(False)
    header = view.horizontalHeader()
    header.setStretchLastSection(True)
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    for i, width in enumerate(widths or []):
        view.setColumnWidth(i, width)
    view.setSortingEnabled(True)
    view.sortByColumn(sort_col, Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder)
    return view, model


class Spark(QWidget):
    def __init__(self, color, ceiling=None):
        super().__init__()
        self.color = QColor(color)
        self.ceiling = ceiling
        self.points = []
        self.reference = []
        self.reference_color = None
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_values(self, points, reference=()):
        self.points = points
        self.reference = reference
        self.update()

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(2, 4, -2, -4)
        for ratio in (0, 0.5, 1):
            line = rect.top() + rect.height() * ratio
            painter.setPen(QPen(QColor("#303b50"), 1))
            painter.drawLine(rect.left(), line, rect.right(), line)
        values = [value for series in (self.points, self.reference) for _, value in series if value is not None]
        if not values:
            painter.setPen(QColor("#97a7be"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Collecting history…")
            return
        series = [points for points in (self.points, self.reference) if points]
        start = min(points[0][0] for points in series)
        end = max(points[-1][0] for points in series)
        scale = self.ceiling or max(1, max(values))
        reference_pen = QPen(self.reference_color or QColor("#adbbce"), 1, Qt.PenStyle.DashLine)
        plotted = [(self.reference, reference_pen), (self.points, QPen(self.color, 2))]
        if self.reference_color is not None:
            plotted.reverse()
        for points, pen in plotted:
            path = QPainterPath()
            painter.setPen(pen)
            previous = None
            for stamp, value in points:
                if value is None:
                    previous = None
                    continue
                x = rect.left() + rect.width() * (stamp-start) / max(1, end-start)
                y = rect.bottom() - rect.height() * min(1, max(0, value / scale))
                if previous is None or stamp - previous > 75:
                    path.moveTo(x, y)
                    painter.drawPoint(x, y)
                else:
                    path.lineTo(x, y)
                previous = stamp
            painter.drawPath(path)


def metric_cards(definitions):
    layout = QGridLayout()
    cards = []
    for i, (title, color, ceiling) in enumerate(definitions):
        card = QFrame(objectName="card")
        contents = QVBoxLayout(card)
        contents.setContentsMargins(13, 10, 13, 9)
        contents.addWidget(label(title, "metricTitle"))
        value = label("—", "metricValue")
        value.setStyleSheet(f"color:{color}")
        contents.addWidget(value)
        chart = Spark(color, ceiling)
        contents.addWidget(chart)
        cards.append((value, chart))
        layout.addWidget(card, 0, i)
    return layout, cards


def network_cards():
    layout, cards = metric_cards((("Receive", COLORS[0], None), ("Send", COLORS[2], None),
                                  ("Timeouts", COLORS[3], None), ("Packet Drops", COLORS[1], None)))
    cards[2][1].setToolTip("TCP retransmission timer expirations per history interval (about 15 seconds).")
    cards[3][1].setToolTip("Packets dropped by local interfaces per history interval (about 15 seconds).")
    return layout, cards


def add_probe_cards(layout):
    charts, cards = metric_cards((("Latency", COLORS[0], None), ("Probe Loss", COLORS[0], 100)))
    for (value, chart), description in zip(cards, ("Mean round-trip time of replies in each round.",
                                                   "Percentage of three ping requests without a reply.")):
        value.setStyleSheet("color:#e7edf7; font-size:20px")
        chart.reference_color = QColor(COLORS[3])
        chart.setToolTip(description + " Router (solid blue) and internet (dashed orange).")
    layout.addLayout(charts)
    caption = label("Router (solid blue)  •  Internet (dashed orange)  •  Collecting probe history…", "muted")
    caption.setWordWrap(True)
    layout.addWidget(caption)
    return cards, caption


class UpdateBanner(QWidget):
    finished = Signal(str, object)

    def __init__(self, app, state, autostart):
        super().__init__()
        self.app = app
        self.state = state
        self.autostart = autostart
        self.offer = None
        self.busy = False
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.notice = label("", "section")
        row.addWidget(self.notice)
        self.button = QPushButton("Update")
        self.button.clicked.connect(self.install)
        self.button.hide()
        row.addWidget(self.button)
        row.addStretch()
        self.hide()
        self.finished.connect(self.complete, Qt.ConnectionType.QueuedConnection)
        previous = updater.read_json(state / "update-result.json")
        if isinstance(previous, dict):
            try:
                (state / "update-result.json").unlink(missing_ok=True)
            except OSError:
                pass
            self.show_result("Update failed" if previous.get("error") else f"Updated to {previous.get('version')}",
                             previous.get("error", ""))
        self.timer = QTimer(self, interval=30 * 60 * 1000)
        self.timer.timeout.connect(self.check)
        self.timer.start()
        QTimer.singleShot(1200, self.check)

    def start(self, action, work):
        self.busy = True

        def run():
            try:
                self.finished.emit(action, work())
            except Exception as error:
                self.finished.emit(action, error)

        threading.Thread(target=run, daemon=True).start()

    def check(self):
        if not self.busy and (self.app / "version.json").is_file():
            self.start("check", lambda: updater.check_update(self.app))

    def install(self):
        if self.busy or self.offer is None:
            return
        offer = self.offer
        self.notice.setText("Downloading update…")
        self.button.setEnabled(False)
        self.start("install", lambda: updater.stage_update(self.app, offer))

    def show_result(self, text, detail=""):
        self.notice.setText(text)
        self.notice.setToolTip(detail)
        self.setToolTip(detail)
        self.show()

    @Slot(str, object)
    def complete(self, action, result):
        self.busy = False
        if action == "check":
            if not isinstance(result, Exception):
                self.offer = result
                self.button.setVisible(result is not None)
                if result is not None:
                    self.show_result("Update Available", f"Version {result['version']}")
            return
        if not isinstance(result, Exception):
            try:
                self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
                updater.hand_off(result, self.app, self.state, self.autostart)
            except OSError as error:
                result = error
            else:
                QApplication.quit()
                return
        self.show_result(f"Update failed · {result}"[:180], str(result))
        self.button.setEnabled(True)


class Window(QMainWindow):
    def __init__(self, state, autostart, launcher):
        super().__init__()
        self.state = state
        self.launcher = launcher
        self.snapshot = {}
        self.settings = {}
        self.history_tick = 0
        self.traffic_history_tick = 0
        self.event_version = None
        self.collector = None
        self.autostart = autostart
        self.start_attempted = False
        self.setWindowTitle("Nobara System Monitor")
        self.resize(1200, 805)
        self.setMinimumSize(780, 570)
        base = QWidget(objectName="base")
        self.setCentralWidget(base)
        outer = QVBoxLayout(base)
        outer.setContentsMargins(22, 17, 22, 13)
        outer.setSpacing(10)
        top = QHBoxLayout()
        brand = QVBoxLayout()
        app_dir = Path(launcher).resolve().parent
        title_row = QHBoxLayout()
        title_row.addWidget(label("Nobara System Monitor", "title"))
        try:
            version = updater.installed_version(app_dir)
        except ValueError:
            pass
        else:
            title_row.addWidget(label(f"v{version}", "muted"))
        title_row.addStretch()
        brand.addLayout(title_row)
        self.subtitle = label("Live telemetry and system events", "muted")
        brand.addWidget(self.subtitle)
        top.addLayout(brand)
        top.addStretch()
        self.live = label("Starting", "waiting")
        top.addWidget(self.live)
        self.pause = QPushButton("Pause display")
        self.pause.setCheckable(True)
        self.pause.toggled.connect(self.toggle_pause)
        top.addWidget(self.pause)
        self.start_button = QPushButton("Start collector")
        self.start_button.clicked.connect(self.start_collector)
        top.addWidget(self.start_button)
        outer.addLayout(top)
        outer.addWidget(UpdateBanner(app_dir, state, autostart))
        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self.refresh_tab)
        outer.addWidget(self.tabs, 1)
        self.overview()
        self.processes()
        self.service_page()
        self.hardware()
        self.events()
        self.traffic_page()
        footer = QHBoxLayout()
        self.status = label("Waiting for collector telemetry…", "muted")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        footer.addWidget(self.status, 1)
        folder = QPushButton("Log folder")
        folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.state))))
        footer.addWidget(folder)
        outer.addLayout(footer)
        self.timer = QTimer(self, interval=2000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def add_page(self, name, scrollable=False):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(9)
        if scrollable:
            page.setObjectName("scrollBody")
            scroll = QScrollArea(widgetResizable=True)
            scroll.setWidget(page)
            self.tabs.addTab(scroll, name)
        else:
            self.tabs.addTab(page, name)
        return layout

    def overview(self):
        layout = self.add_page("Overview", scrollable=True)
        toolbar = QHBoxLayout()
        toolbar.addWidget(label("System activity", "section"))
        toolbar.addStretch()
        self.uptime = label("", "muted")
        toolbar.addWidget(self.uptime)
        self.range = QComboBox()
        for name, seconds in [("Last hour", 3600), ("Last 6 hours", 21600), ("Last 24 hours", 86400), ("Last 7 days", 604800)]:
            self.range.addItem(name, seconds)
        self.range.currentIndexChanged.connect(self.refresh_history)
        toolbar.addWidget(self.range)
        layout.addLayout(toolbar)
        cards, self.cards = metric_cards(zip(
            ("CPU utilization", "RAM in use", "Disk writes", "Full I/O pressure"), COLORS, (100, 100, None, 100)))
        layout.addLayout(cards)
        gpu_header = QHBoxLayout()
        gpu_header.addWidget(label("GPU activity", "section"))
        gpu_header.addStretch()
        self.gpu_history_caption = label("Waiting for NVIDIA telemetry", "muted")
        gpu_header.addWidget(self.gpu_history_caption)
        self.live_gpu_names = []
        self.gpu_slot = QComboBox()
        self.gpu_slot.setMinimumContentsLength(12)
        self.gpu_slot.setVisible(False)
        self.gpu_slot.currentIndexChanged.connect(self.gpu_changed)
        gpu_header.addWidget(self.gpu_slot)
        layout.addLayout(gpu_header)
        cards, self.gpu_cards = metric_cards(zip(
            ("Utilization", "Temperature", "VRAM used / total", "Power draw"), COLORS, (100, 100, None, None)))
        self.gpu_cards[2][0].setStyleSheet(f"color:{COLORS[2]}; font-size:21px")
        self.gpu_cards[2][1].setToolTip("Solid line: VRAM used. Dashed line: total VRAM.")
        layout.addLayout(cards)
        layout.addWidget(label("Network activity", "section"))
        cards, self.overview_network_cards = network_cards()
        layout.addLayout(cards)
        self.network_health_caption = label("Collecting packet drop and link loss history…", "muted")
        self.network_health_caption.setWordWrap(True)
        layout.addWidget(self.network_health_caption)
        self.overview_probes, self.overview_probe_caption = add_probe_cards(layout)
        self.coverage = label("Checking available telemetry…", "muted")
        self.coverage.setWordWrap(True)
        layout.addWidget(self.coverage)
        layout.addWidget(label("Recent faults and warnings", "section"))
        table, self.recent = make_table(["Last seen", "Severity", "Category", "Occurrences", "Message"],
                                       {0: local_time, 1: LEVELS.get}, descending=True,
                                       widths=[130, 85, 130, 95], severity=1)
        table.setMinimumHeight(155)
        layout.addWidget(table, 1)
        table.doubleClicked.connect(lambda _: self.tabs.setCurrentIndex(4))

    def processes(self):
        layout = self.add_page("Processes")
        self.proc_search = QLineEdit(placeholderText="Filter by name, PID, user or state")
        self.proc_search.textChanged.connect(self.show_processes)
        layout.addWidget(self.proc_search)
        self.proc_caption = label("CPU usage is per process. 100% equals one logical core.", "muted")
        self.proc_caption.setWordWrap(True)
        layout.addWidget(self.proc_caption)
        view, self.proc_model = make_table(["PID", "Process", "User", "CPU", "Memory", "Disk write", "State"],
                                          {3: percent, 4: size, 5: rate}, 3, True,
                                          [90, 250, 125, 85, 110, 125])
        layout.addWidget(view, 1)

    def service_page(self):
        layout = self.add_page("Services")
        toolbar = QHBoxLayout()
        self.unit_search = QLineEdit(placeholderText="Filter services, sockets and timers")
        self.unit_search.textChanged.connect(self.show_services)
        toolbar.addWidget(self.unit_search, 1)
        self.unit_filter = QComboBox()
        self.unit_filter.addItems(["All states", "Active", "Failed", "Inactive"])
        self.unit_filter.currentTextChanged.connect(self.show_services)
        toolbar.addWidget(self.unit_filter)
        layout.addLayout(toolbar)
        self.unit_status = label("Checking systemd…", "muted")
        self.unit_status.setWordWrap(True)
        layout.addWidget(self.unit_status)
        view, self.unit_model = make_table(["Scope", "Unit", "Load", "Active", "Substate", "Description"],
                                          sort_col=1, widths=[76, 250, 80, 85, 100])
        layout.addWidget(view, 1)

    def hardware(self):
        layout = self.add_page("Hardware")
        scroller = QScrollArea(widgetResizable=True)
        inner = QWidget(objectName="scrollBody")
        body = QVBoxLayout(inner)
        body.setContentsMargins(0, 0, 8, 0)
        self.gpu_caption = label("Checking GPU telemetry…", "muted")
        self.gpu_caption.setWordWrap(True)
        self.hw_models = {}
        data = [
            ("NVIDIA graphics", "gpu", ["GPU", "Driver", "Utilization", "Temperature", "VRAM used", "VRAM total", "Power"],
             {2: percent, 3: celsius, 4: lambda x: f"{x:g} MiB", 5: lambda x: f"{x:g} MiB", 6: lambda x: f"{x:g} W"}),
            ("Display adapters", "drm", ["Card", "Vendor", "PCI address", "Utilization", "Clock"],
             {3: percent, 4: lambda x: f"{x:,} MHz"}),
            ("Storage activity", "disk", ["Device", "Model", "Read", "Write", "Busy", "Avg. write latency"],
             {2: rate, 3: rate, 4: percent, 5: lambda x: f"{x:.2f} ms"}),
            ("Filesystems", "mounts", ["Mount", "Device", "Format", "Space used", "Free"], {3: percent, 4: size}),
            ("Network interfaces", "net", ["Interface", "Link", "Receive", "Send", "New errors / sample"], {2: rate, 3: rate}),
            ("Temperature sensors", "temperatures", ["Driver", "Sensor", "Current", "High", "Critical"],
             {2: celsius, 3: celsius, 4: celsius}),
            ("Fans", "fans", ["Driver", "Sensor", "Speed"], {2: lambda x: f"{x:,} RPM"}),
            ("Logical CPU cores", "cores", ["Core", "Utilization"], {1: percent}),
            ("Hardware inventory", "inventory", ["Group", "Device", "Details"], {}),
        ]
        for title, key, headers, formats in data:
            body.addWidget(label(title, "section"))
            if key == "gpu":
                body.addWidget(self.gpu_caption)
            table, model = make_table(headers, formats, widths=[170, 230] if key in ("gpu", "inventory") else None)
            table.setMinimumHeight(250 if key in ("temperatures", "inventory") else 120)
            body.addWidget(table)
            self.hw_models[key] = model
        scroller.setWidget(inner)
        layout.addWidget(scroller, 1)

    def events(self):
        layout = self.add_page("Events")
        filters = QHBoxLayout()
        self.event_search = QLineEdit(placeholderText="Search message, source or category")
        self.event_search.textChanged.connect(self.show_events)
        filters.addWidget(self.event_search, 1)
        self.severity = QComboBox()
        self.severity.addItems(["All severities", "Warnings and errors", "Errors only", "Informational"])
        self.severity.currentTextChanged.connect(self.show_events)
        filters.addWidget(self.severity)
        self.category = QComboBox()
        self.category.addItems(["All categories", "Application", "CPU", "Graphics", "Input / USB", "Kernel", "Memory",
                                "Monitor", "Network", "Power / Firmware", "Services", "Storage", "System / Apps", "Thermals"])
        self.category.currentTextChanged.connect(self.show_events)
        filters.addWidget(self.category)
        export = QPushButton("Export CSV")
        export.clicked.connect(self.export_events)
        filters.addWidget(export)
        layout.addLayout(filters)
        self.event_caption = label("Select an event to see its full details.", "muted")
        layout.addWidget(self.event_caption)
        table, self.event_model = make_table(
            ["Last seen", "Severity", "Category", "Source", "Occurrences", "Message"], {0: local_time, 1: LEVELS.get},
            descending=True, widths=[130, 80, 125, 140, 95], severity=1)
        self.event_table = table
        table.clicked.connect(self.event_detail)
        table.selectionModel().currentRowChanged.connect(lambda current, _: self.event_detail(current))
        layout.addWidget(table, 3)
        self.detail = QPlainTextEdit(readOnly=True)
        self.detail.setPlaceholderText("Event details appear here. Repeated messages are grouped by source and message.")
        self.detail.setMaximumHeight(145)
        layout.addWidget(self.detail, 1)

    def traffic_page(self):
        layout = self.add_page("Network Traffic", scrollable=True)
        header = QHBoxLayout()
        header.addWidget(label("Interface activity", "section"))
        header.addStretch()
        self.traffic_range = QComboBox()
        for title, seconds in (("Last hour", 3600), ("Last 24 hours", 86400), ("Last 7 days", 604800)):
            self.traffic_range.addItem(title, seconds)
        self.traffic_range.currentIndexChanged.connect(self.refresh_traffic_history)
        header.addWidget(self.traffic_range)
        layout.addLayout(header)
        cards, self.traffic_cards = network_cards()
        layout.addLayout(cards)
        self.traffic_health_caption = label("Collecting packet drop and link loss history…", "muted")
        self.traffic_health_caption.setWordWrap(True)
        layout.addWidget(self.traffic_health_caption)
        self.traffic_probes, self.traffic_probe_caption = add_probe_cards(layout)
        targets = QHBoxLayout()
        targets.addWidget(label("Internet probe IP", "muted"))
        self.probe_edit = QLineEdit(probe_target(self.state))
        self.probe_edit.setMaximumWidth(260)
        self.probe_edit.setPlaceholderText("Disabled")
        self.probe_edit.setToolTip("Enter an IPv4 or IPv6 address. Clear to disable the internet probe.")
        self.probe_edit.returnPressed.connect(self.apply_probe_target)
        targets.addWidget(self.probe_edit)
        save_target = QPushButton("Apply target")
        save_target.clicked.connect(self.apply_probe_target)
        targets.addWidget(save_target)
        targets.addStretch()
        layout.addLayout(targets)
        self.traffic_status = label("Waiting for socket data…", "muted")
        self.traffic_status.setWordWrap(True)
        layout.addWidget(self.traffic_status)
        controls = QHBoxLayout()
        self.peer_search = QLineEdit(placeholderText="Search IP, port, process or country")
        self.peer_search.textChanged.connect(self.show_traffic)
        controls.addWidget(self.peer_search, 1)
        self.peer_view = QComboBox()
        self.peer_view.addItems(["Live peers", "Recent peers"])
        self.peer_view.currentIndexChanged.connect(self.show_traffic)
        controls.addWidget(self.peer_view)
        self.peer_filter = QComboBox()
        self.peer_filter.addItems(["All destinations", "Watchlist only", "New in last 24h", "Unknown country"])
        self.peer_filter.currentIndexChanged.connect(self.show_traffic)
        controls.addWidget(self.peer_filter)
        layout.addLayout(controls)
        options = QHBoxLayout()
        self.peer_summary = label("No observations yet", "muted")
        options.addWidget(self.peer_summary, 1)
        options.addWidget(label("Country watchlist", "muted"))
        self.watch_edit = QLineEdit(" ".join(sorted(watchlist(self.state))))
        self.watch_edit.setPlaceholderText("e.g. KR, KP")
        self.watch_edit.setMaximumWidth(130)
        self.watch_edit.setToolTip("Enter two-letter country codes separated by commas or spaces. Blank clears the list.")
        self.watch_edit.returnPressed.connect(self.apply_watchlist)
        options.addWidget(self.watch_edit)
        apply = QPushButton("Apply")
        apply.clicked.connect(self.apply_watchlist)
        options.addWidget(apply)
        layout.addLayout(options)
        view, self.peer_model = make_table(
            ["Last seen", "Remote IP", "Port", "Country", "Process", "Protocol", "Sent", "Received", "Flag", "First seen"],
            {0: local_time, 3: country_name, 6: size, 7: size, 9: local_time}, descending=True,
            widths=[125, 147, 55, 150, 125, 73, 82, 82, 82, 125])
        self.peer_table = view
        view.setMinimumHeight(200)
        view.selectionModel().currentRowChanged.connect(lambda current, _: self.peer_detail(current))
        layout.addWidget(view, 1)
        self.peer_text = label("Select a peer for socket details. Country is an IP estimate. Traffic totals are sampled TCP bytes.", "muted")
        self.peer_text.setWordWrap(True)
        self.peer_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.peer_text)

    def apply_watchlist(self):
        value = self.watch_edit.text().strip()
        tokens = [token for token in value.replace(",", " ").split() if token]
        codes = watch_codes(value)
        if any(token.upper() not in codes for token in tokens):
            self.status.setText("Enter two-letter country codes, separated by commas or spaces.")
            return
        try:
            save_watchlist(self.state, codes)
        except OSError as error:
            self.status.setText(f"Couldn't save the watchlist · {error}")
            return
        self.watch_edit.setText(" ".join(sorted(codes)))
        self.show_traffic()
        self.status.setText("Country watchlist saved")

    def apply_probe_target(self):
        try:
            target = save_probe_target(self.state, self.probe_edit.text())
        except ValueError:
            self.status.setText("Enter an IPv4 or IPv6 address, or leave the field blank to disable the internet probe.")
            return
        except OSError as error:
            self.status.setText(f"Couldn't save the internet probe target · {error}")
            return
        self.probe_edit.setText(target)
        self.refresh_traffic_history()

    def refresh_traffic_history(self, *_):
        self.traffic_history_tick = time.monotonic()
        cutoff = int(time.time() - self.traffic_range.currentData())
        self.refresh_network_charts(self.traffic_cards, self.traffic_health_caption, cutoff)
        self.refresh_probe_charts(self.traffic_probes, self.traffic_probe_caption, cutoff)

    def refresh_probe_charts(self, cards, caption, cutoff):
        readings = {"router": [], "internet": []}
        try:
            with database(True) as db:
                rows = db.execute("SELECT ts,kind,target,latency,loss,status FROM network_probe_history "
                                  "WHERE ts>=? ORDER BY ts", (cutoff,))
                for row in rows:
                    readings[row["kind"]].append(row)
        except sqlite3.Error:
            pass
        current = {kind: rows[-1] if rows else None for kind, rows in readings.items()}
        configured = probe_target(self.state)
        internet = current["internet"]
        if internet is None or internet["target"] != configured:
            current["internet"] = None
        targets = []
        for kind, title, style in (("router", "Router", "solid blue"), ("internet", "Internet", "dashed orange")):
            row = current[kind]
            target = row["target"] if row else configured if kind == "internet" else ""
            status = row["status"] if row else "Waiting for samples"
            if kind == "internet" and not configured:
                status = "Disabled"
            details = " · ".join(part for part in (target, status) if part)
            targets.append(f"{title} ({style})  {details}")
        caption.setText("  •  ".join(targets))
        for key, (metric, chart), unit in zip(("latency", "loss"), cards, (" ms", "%")):
            series = []
            values = []
            for kind, rows in readings.items():
                row = current[kind]
                latest = row[key] if row else None
                values.append(f"{latest:.1f}{unit}" if latest is not None else "—")
                series.append([(point["ts"], point[key] if row and point["target"] == row["target"] else None)
                               for point in rows])
            metric.setText(" / ".join(values))
            chart.set_values(*series)

    def refresh_network_charts(self, cards, caption, cutoff):
        traffic_rows, health_rows = [], []
        try:
            with database(True) as db:
                traffic_rows = db.execute("SELECT ts,received,sent FROM traffic_history "
                                          "WHERE ts>=? ORDER BY ts", (cutoff,)).fetchall()
                health_rows = db.execute("SELECT ts,timeouts,drops,carrier_losses FROM network_health_history "
                                         "WHERE ts>=? ORDER BY ts", (cutoff,)).fetchall()
        except sqlite3.Error:
            # Older collector databases can still supply Receive and Send history.
            pass
        for column, (_, chart) in enumerate(cards[:2], 1):
            chart.set_values([(row[0], row[column]) for row in traffic_rows])
        for column, (metric, chart) in enumerate(cards[2:], 1):
            latest = health_rows[-1][column] if health_rows else None
            metric.setText(f"{latest:,}" if latest is not None else "—")
            chart.set_values([(row[0], row[column]) for row in health_rows])
        if health_rows:
            drops = sum(row[2] for row in health_rows)
            losses = sum(row[3] for row in health_rows)
            caption.setText(f"Recorded in selected range  •  Packet drops {drops:,}  •  Physical link losses {losses:,}")
        else:
            caption.setText("Collecting packet drop and link loss history…")

    def show_interface_rates(self, cards):
        for column, (widget, _) in enumerate(cards[:2], 2):
            widget.setText(rate(sum(row[column] for row in self.snapshot["net"] if row[0] != "lo")))

    def show_traffic(self, *_):
        snapshot = self.snapshot
        if "net" in snapshot:
            self.show_interface_rates(self.traffic_cards)
        info = snapshot.get("traffic", {})
        if info:
            self.traffic_status.setText(f"{info['coverage']}  •  {info['geo']}  •  {snapshot.get('scope', '')}")
        else:
            self.traffic_status.setText("Waiting for the updated collector’s remote socket data.")
        if time.monotonic() - self.traffic_history_tick > 12:
            self.refresh_traffic_history()
        current = self.peer_table.currentIndex()
        selection = tuple(self.peer_model.rows[current.row()][i] for i in (1, 2, 4, 5)) if current.isValid() else None
        live = self.peer_view.currentIndex() == 0
        cutoff = time.time() - 86400
        codes = watchlist(self.state)
        search = self.peer_search.text().strip().casefold()
        match_filter = self.peer_filter.currentIndex()
        try:
            with database(True) as db:
                rows = db.execute("SELECT * FROM network_peers WHERE " + ("last=?" if live else "last>=?")
                                  + " ORDER BY last DESC",
                                  (snapshot.get("ts", 0) if live else cutoff - 6 * 86400,)).fetchall()
        except sqlite3.Error:
            rows = []
        results = []
        for entry in rows:
            code = entry["country"]
            first = entry["first"]
            watched = code in codes
            is_new = first >= cutoff
            if (match_filter == 1 and not watched) or (match_filter == 2 and not is_new) or (match_filter == 3 and code):
                continue
            flag = "Watchlist" if watched else "New" if is_new else ""
            protocol = entry["protocol"].upper()
            if search and not any(search in word.casefold() for word in (
                    entry["host"], str(entry["port"]), country_name(code), entry["process"], protocol)):
                continue
            results.append((entry["last"], entry["host"], entry["port"], code, entry["process"] or "Unknown",
                            protocol, entry["sent"], entry["received"], flag, first, entry["pids"], entry["states"]))
        self.peer_model.set_rows(results[:2000])
        display_limit = "  •  Showing the newest 2,000" if len(results) > 2000 else ""
        self.peer_summary.setText(f"{len(results):,} matching peers  •  {sum(row[8] == 'Watchlist' for row in results):,} watchlist matches{display_limit}")
        for i, row in enumerate(self.peer_model.rows):
            if tuple(row[j] for j in (1, 2, 4, 5)) == selection:
                self.peer_table.selectRow(i)
                break
        else:
            self.peer_text.setText("Select a peer for socket details. Country is an IP estimate. Traffic totals are sampled TCP bytes.")

    def peer_detail(self, index):
        if not index.isValid():
            return
        last, host, port, country, process, protocol, sent, received, flag, first, pids, states = self.peer_model.rows[index.row()]
        address = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        self.peer_text.setText(f"{protocol} {address}  •  {country_name(country)}  •  {process}"
                               f"  •  Last PID(s) {pids or 'unknown'}  •  Last state {states or 'unknown'}")

    def start_collector(self):
        self.start_attempted = True
        if self.collector and self.collector.poll() is None:
            return
        try:
            self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (self.state / "collector-startup.log").open("wb") as output:
                self.collector = subprocess.Popen(
                    [sys.executable, str(self.launcher), "--collect", "--state-dir", str(self.state)],
                    stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
            self.start_button.setEnabled(False)
            QTimer.singleShot(4500, lambda: self.start_button.setEnabled(True))
        except OSError as exc:
            self.status.setText(f"Collector couldn't start · {exc}")

    def toggle_pause(self, paused):
        self.pause.setText("Resume display" if paused else "Pause display")
        if paused:
            self.live.setText("Display paused")
            self.live.setObjectName("waiting")
            self.live.style().polish(self.live)
        else:
            self.refresh()

    def refresh(self):
        if self.pause.isChecked():
            return
        try:
            self.snapshot = json.loads((self.state / "live.json").read_text())
        except (OSError, ValueError):
            self.snapshot = {}
        stamp = self.snapshot.get("ts", 0)
        age = time.time() - stamp
        live = -5 <= age <= 12
        self.live.setText("Collector live" if live else "Collector waiting" if not stamp else "Telemetry stale")
        self.live.setObjectName("live" if live else "waiting")
        self.live.style().polish(self.live)
        self.start_button.setVisible(not live)
        if not live and self.autostart and not self.start_attempted:
            self.start_collector()
        try:
            with database(True) as db:
                self.settings = dict(db.execute("SELECT key,value FROM settings"))
                version = tuple(db.execute("SELECT max(last),sum(repeats),count(*) FROM events").fetchone())
        except sqlite3.Error:
            version = None
        journal = self.settings.get("journal_status", "Starting")
        self.status.setText(f"Sample {local_time(stamp) if stamp else 'pending'}  •  Journal {journal[:105]}")
        self.status.setToolTip(journal)
        index = self.tabs.currentIndex()
        if index != 4 or version != self.event_version:
            self.refresh_tab(index)
        self.event_version = version

    def refresh_tab(self, index):
        if index == 0 and hasattr(self, "recent"):
            self.show_overview()
        elif index == 1:
            self.show_processes()
        elif index == 2:
            self.show_services()
        elif index == 3:
            self.show_hardware()
        elif index == 4:
            self.show_events()
        elif index == 5:
            self.show_traffic()

    def show_overview(self):
        s = self.snapshot
        if s:
            values = [
                percent(s.get("cpu")),
                percent(s.get("memory")),
                rate(sum(row[3] for row in s.get("disk", []))),
                percent(s.get("pressure", {}).get("io.full")),
            ]
            for (metric, _), value in zip(self.cards, values):
                metric.setText(value)
            self.show_gpu_metrics()
            if "net" in s:
                self.show_interface_rates(self.overview_network_cards)
            self.subtitle.setText(f"{len(s['processes'])} processes  •  {len(s['services'])} systemd units  •  {len(s['disk'])} drives")
            self.uptime.setText(f"Uptime {duration(s['uptime'])}")
            alerts = "  •  Current alerts " + ", ".join(s["alerts"]) if s["alerts"] else ""
            self.coverage.setText(f"{s['scope']}  •  Kernel journal {self.settings.get('kernel_journal', 'checking').lower()}"
                                  f"  •  NVIDIA telemetry {'live' if s['gpu'] else 'unavailable'}"
                                  f"  •  RAM {size(s['ram_used'])} / {size(s['ram_total'])}  •  Swap {percent(s['swap'])}{alerts}")
        if time.monotonic() - self.history_tick > 12:
            self.refresh_history()
        try:
            with database(True) as db:
                self.recent.set_rows(db.execute("SELECT last,severity,category,repeats,message FROM events WHERE severity<=4 ORDER BY last DESC LIMIT 35").fetchall())
        except sqlite3.Error:
            self.recent.set_rows([])

    def refresh_history(self):
        self.history_tick = time.monotonic()
        cutoff = int(time.time() - self.range.currentData())
        try:
            with database(True) as db:
                rows = db.execute("SELECT ts,cpu,memory,disk_write,io_full FROM samples WHERE ts>=? ORDER BY ts", (cutoff,)).fetchall()
        except sqlite3.Error:
            rows = []
        for column, (_, chart) in enumerate(self.cards, 1):
            chart.set_values([(row[0], row[column]) for row in rows])
        self.refresh_gpu_history(cutoff)
        self.refresh_network_charts(self.overview_network_cards, self.network_health_caption, cutoff)
        self.refresh_probe_charts(self.overview_probes, self.overview_probe_caption, cutoff)

    def show_gpu_metrics(self):
        gpus = self.snapshot.get("gpu", [])
        names = [row[0] for row in gpus]
        if names and names != self.live_gpu_names:
            self.set_gpu_names(list(enumerate(names)))
        self.live_gpu_names = names
        slot = self.gpu_slot.currentData()
        metric = gpus[slot] if slot is not None and slot < len(gpus) else None
        if metric:
            used, total = (size(value * 1024**2) if value is not None else "—" for value in metric[4:6])
            values = (percent(metric[2]), celsius(metric[3]), f"{used} / {total}",
                      f"{metric[6]:.1f} W" if metric[6] is not None else "—")
        else:
            values = ("—",) * 4
        for (widget, _), value in zip(self.gpu_cards, values):
            widget.setText(value)
        self.gpu_history_caption.setText("NVIDIA telemetry live" if metric else "Live NVIDIA telemetry unavailable")
        self.gpu_history_caption.setToolTip(self.snapshot.get("gpu_status", ""))

    def set_gpu_names(self, names):
        old = self.gpu_slot.currentData()
        self.gpu_slot.blockSignals(True)
        self.gpu_slot.clear()
        for slot, name in names:
            self.gpu_slot.addItem(name, slot)
        selected = self.gpu_slot.findData(old)
        if selected >= 0:
            self.gpu_slot.setCurrentIndex(selected)
        self.gpu_slot.setVisible(bool(names))
        self.gpu_slot.blockSignals(False)

    def gpu_changed(self, *_):
        self.show_gpu_metrics()
        self.refresh_history()

    def refresh_gpu_history(self, cutoff):
        rows = []
        try:
            with database(True) as db:
                if not self.live_gpu_names:
                    names = db.execute("SELECT slot,name FROM gpu_history WHERE ts>=? "
                                       "GROUP BY slot HAVING ts=max(ts) ORDER BY slot", (cutoff,)).fetchall()
                    names = [tuple(row) for row in names]
                    if names != [(self.gpu_slot.itemData(i), self.gpu_slot.itemText(i)) for i in range(self.gpu_slot.count())]:
                        self.set_gpu_names(names)
                rows = db.execute("SELECT ts,utilization,temperature,vram_used,vram_total,power FROM gpu_history "
                                  "WHERE ts>=? AND slot=? ORDER BY ts",
                                  (cutoff, self.gpu_slot.currentData())).fetchall()
        except sqlite3.Error:
            pass
        for column, (_, chart) in zip((1, 2, 3, 5), self.gpu_cards):
            capacity = [(row[0], row[4]) for row in rows] if column == 3 else ()
            chart.set_values([(row[0], row[column]) for row in rows], capacity)

    def show_processes(self, *_):
        rows = self.snapshot.get("processes", [])
        query = self.proc_search.text().strip().casefold()
        filtered = [row for row in rows if not query or any(query in str(row[i]).casefold() for i in (0, 1, 2, 6))]
        self.proc_model.set_rows(filtered)
        self.proc_caption.setText(f"{len(filtered)} of {len(rows)} visible processes  •  100% CPU equals one logical core  •  A dash means the reading is unavailable")

    def show_services(self, *_):
        rows = self.snapshot.get("services", [])
        query = self.unit_search.text().strip().casefold()
        state = self.unit_filter.currentText().lower()
        self.unit_model.set_rows(row for row in rows if (not query or query in " ".join(row).casefold())
                                 and (state == "all states" or row[3] == state))
        self.unit_status.setText(f"System manager {self.snapshot.get('system_status', 'checking')}"
                                 f"  •  User manager {self.snapshot.get('user_status', 'checking')}")

    def show_hardware(self):
        self.gpu_caption.setText(f"NVIDIA telemetry · {self.snapshot.get('gpu_status', 'Checking')}")
        for key, model in self.hw_models.items():
            rows = self.snapshot.get(key, [])
            model.set_rows(enumerate(rows) if key == "cores" else rows)

    def event_query(self, limit):
        where = []
        args = []
        severity = self.severity.currentIndex()
        if severity:
            where.append({1: "severity<=4", 2: "severity<=3", 3: "severity>=5"}[severity])
        if self.category.currentIndex():
            where.append("category=?")
            args.append(self.category.currentText())
        query = self.event_search.text().strip().lower()
        if query:
            where.append("instr(lower(message || ' ' || source || ' ' || category), ?) > 0")
            args.append(query)
        conditions = " WHERE " + " AND ".join(where) if where else ""
        with database(True) as db:
            return db.execute("SELECT last,severity,category,source,repeats,message,first,id FROM events"
                              + conditions + " ORDER BY last DESC LIMIT ?", (*args, limit)).fetchall()

    def show_events(self, *_):
        current = self.event_table.currentIndex()
        selected = self.event_model.rows[current.row()][7] if current.isValid() else None
        try:
            rows = self.event_query(1501)
        except sqlite3.Error:
            rows = []
        self.event_model.set_rows(rows[:1500])
        caption = "1,500 most recent matches shown. CSV includes all retained matches." if len(rows) > 1500 else f"{len(rows):,} matching event groups"
        self.event_caption.setText(caption)
        for i, row in enumerate(self.event_model.rows):
            if row[7] == selected:
                self.event_table.selectRow(i)
                break
        else:
            self.detail.clear()

    def event_detail(self, index):
        if not index.isValid():
            return
        last, severity, category, source, count, message, first, _ = self.event_model.rows[index.row()]
        self.detail.setPlainText(f"{LEVELS.get(severity, 'Unknown')}  •  {category}  •  {source}\n"
                                 f"First {local_time(first)}  /  Last {local_time(last)}  /  {count:,} occurrence(s)\n\n{message}")

    def export_events(self):
        name, _ = QFileDialog.getSaveFileName(self, "Export matching events", "nobara-events.csv", "CSV files (*.csv)")
        if not name:
            return
        try:
            rows = self.event_query(-1)
            with open(name, "w", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                writer.writerow(["Last seen", "Severity", "Category", "Source", "Occurrences", "Message", "First seen"])
                for last, severity, category, source, count, message, first, _ in rows:
                    # Keep journal text inert when opened in a spreadsheet.
                    safe = [("'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value)
                            for value in (category, source, message)]
                    writer.writerow([datetime.fromtimestamp(last).astimezone().isoformat(), LEVELS.get(severity, severity),
                                     safe[0], safe[1], count, safe[2], datetime.fromtimestamp(first).astimezone().isoformat()])
            self.status.setText(f"Exported {len(rows):,} event groups")
        except (OSError, sqlite3.Error) as error:
            self.status.setText(f"Export failed · {error}")


def run(state, autostart, launcher):
    application = QApplication([sys.argv[0]])
    application.setApplicationName("Nobara System Monitor")
    application.setDesktopFileName("nobara-system-monitor")
    application.setWindowIcon(QIcon(str(launcher.with_name("nobara-system-monitor.png"))))
    application.setStyle("Fusion")
    application.setFont(QFont("Inter", 10))
    application.setStyleSheet(THEME)
    window = Window(state, autostart, launcher)
    window.show()
    return application.exec()
