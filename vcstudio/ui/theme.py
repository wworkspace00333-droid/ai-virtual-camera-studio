"""Dark professional theme."""

BG = "#0f1115"
PANEL = "#171a21"
PANEL_2 = "#1e222b"
BORDER = "#2a2f3a"
TEXT = "#e6e8ec"
MUTED = "#8b93a3"
ACCENT = "#4f8cff"
OK = "#3fb950"
WARN = "#d29922"
ERR = "#f85149"

QSS = f"""
* {{ font-size: 13px; color: {TEXT}; }}
QMainWindow, QWidget#page, QStackedWidget {{ background: {BG}; }}
QWidget {{ background: transparent; }}
QListWidget#nav {{ background: {PANEL}; selection-background-color: {PANEL_2}; selection-color: {TEXT}; border: none; border-right: 1px solid {BORDER}; padding: 8px 6px; outline: 0; }}
QListWidget#nav::item {{ padding: 9px 12px; border-radius: 6px; margin: 1px 0; color: {MUTED}; }}
QListWidget#nav::item:hover {{ background: {PANEL_2}; color: {TEXT}; }}
QListWidget#nav::item:selected, QListWidget#nav::item:selected:active, QListWidget#nav::item:selected:!active {{ background: #1f2d4a; color: {TEXT}; border-left: 3px solid {ACCENT}; }}
QLabel#h1 {{ font-size: 22px; font-weight: 600; }}
QLabel#h2 {{ font-size: 15px; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; }}
QFrame#card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px; }}
QFrame#placeholder {{ background: {PANEL}; border: 1px dashed {BORDER}; border-radius: 10px; }}
QPushButton {{ background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px 14px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {MUTED}; border-color: {PANEL_2}; }}
QPushButton#primary {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QPushButton#danger {{ background: {ERR}33; border-color: {ERR}; }}
QToolButton {{ background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px 12px; }}
QToolButton:hover {{ border-color: {ACCENT}; }}
QToolButton:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QPushButton#primary:disabled, QPushButton#danger:disabled {{ background: {PANEL_2}; border-color: {BORDER}; color: {MUTED}; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QKeySequenceEdit, QPlainTextEdit, QTableWidget, QListWidget {{
    background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px 6px; selection-background-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: {PANEL_2}; border: 1px solid {BORDER}; }}
QHeaderView::section {{ background: {PANEL}; border: none; border-bottom: 1px solid {BORDER}; padding: 6px; color: {MUTED}; }}
QTableWidget {{ gridline-color: {BORDER}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 8px; top: -1px; }}
QTabBar::tab {{ background: {PANEL}; padding: 7px 14px; border: 1px solid {BORDER}; border-bottom: none; border-top-left-radius: 6px; border-top-right-radius: 6px; color: {MUTED}; }}
QTabBar::tab:selected {{ background: {PANEL_2}; color: {TEXT}; }}
QStatusBar {{ background: {PANEL}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
QScrollBar:vertical {{ background: {BG}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QCheckBox::indicator, QRadioButton::indicator, QTreeView::indicator, QTableView::indicator, QListView::indicator {{
    width: 14px; height: 14px; border: 1px solid #5a6375; background: {PANEL_2}; }}
QCheckBox::indicator, QTreeView::indicator, QTableView::indicator, QListView::indicator {{ border-radius: 3px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover, QTreeView::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked, QTreeView::indicator:checked, QTableView::indicator:checked, QListView::indicator:checked {{
    background: {ACCENT}; border-color: {ACCENT}; image: none; }}
QRadioButton::indicator:checked {{ background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
    stop:0 white, stop:0.35 white, stop:0.45 {ACCENT}, stop:1 {ACCENT}); border-color: {ACCENT}; }}
QTreeView::indicator:indeterminate {{ background: {ACCENT}66; border-color: {ACCENT}; }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ background: {PANEL}; border-color: {BORDER}; }}
QTreeWidget {{ background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 6px; }}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: white; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
QToolTip {{ background: {PANEL_2}; border: 1px solid {BORDER}; color: {TEXT}; }}
"""
