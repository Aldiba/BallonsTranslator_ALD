"""
现代字体选择器 — 带搜索（模糊 + 全拼 + 拼音首字母 + 多词）和分块渲染的预览列表。

设计要点:
- FontIndex: 启动时一次性构建搜索键（原名 + 全拼 + 首字母），避免每次展开重算。
- FontPickerPopup: 无边框弹出层，顶部搜索框 + 左侧"最近使用"边栏 + 主字体列表。
  "最近使用"独立成栏，避免它占用主列表顶部而与"滚动定位当前字体"冲突。
- FontItemDelegate: 只为可见行创建带该字体的 QFont 渲染预览 → 千级字体不卡。
"""

from typing import List, Optional, Callable

from qtpy.QtCore import Qt, QAbstractListModel, QModelIndex, QSize, Signal, QEvent
from qtpy.QtGui import QFont, QColor, QPainter, QFontMetrics
from qtpy.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QListView, QLabel, QWidget,
    QStyledItemDelegate, QStyle, QApplication, QFrame,
)

from .custom_widget.helper import isDarkTheme

RECENT_MAX_VISIBLE = 6   # 最近使用区域最多显示的字体数
RECENT_TITLE = '最近使用'
ALL_TITLE = '全部字体'

# 模型 role
TYPE_ROLE = Qt.ItemDataRole.UserRole       # 'header' / 'font'
NAME_ROLE = Qt.ItemDataRole.UserRole + 1   # 字体名

# ---------------------------------------------------------------------------
# 拼音/搜索键构建
# ---------------------------------------------------------------------------

_PY_PINYIN = None


def _load_pypinyin():
    """延迟加载 pypinyin（已安装进项目 py310），模块级缓存避免重复 import。

    若环境缺失 pypinyin（万一未安装），回退为无拼音：全拼/首字母为空，
    仅靠原名匹配，不影响功能。
    """
    global _PY_PINYIN
    if _PY_PINYIN is None:
        try:
            from pypinyin import lazy_pinyin, Style
            _PY_PINYIN = (lazy_pinyin, Style)
        except ImportError:
            _PY_PINYIN = (None, None)
    return _PY_PINYIN


class SearchKey:
    """一个字体的搜索键：多种归一化形式，供加权匹配。"""

    __slots__ = ('name_lower', 'pinyin_concat', 'pinyin_spaced', 'initials')

    def __init__(self, family: str):
        self.name_lower = family.lower()
        lazy_pinyin, Style = _load_pypinyin()
        if lazy_pinyin is None:
            # 无 pypinyin 回退：无拼音形式
            self.pinyin_concat = ''
            self.pinyin_spaced = ''
            self.initials = ''
            return
        py = lazy_pinyin(family)                 # ['si','yuan','hei','ti']
        self.pinyin_concat = ''.join(py).lower()  # 'siyuanheiti'
        spaced = ' '.join(py).lower()             # 'si yuan hei ti'
        while '  ' in spaced:                     # 归并 Latin-汉字 处的双空格
            spaced = spaced.replace('  ', ' ')
        self.pinyin_spaced = spaced
        self.initials = ''.join(lazy_pinyin(family, style=Style.FIRST_LETTER)).lower()  # 'syht'


def build_search_key(family: str) -> SearchKey:
    return SearchKey(family)


def contains_cjk(s: str) -> bool:
    """名称中是否含汉字（用于排序时把中文字体置顶）。"""
    return any('一' <= c <= '鿿' for c in s)


def match_score(query: str, sk: SearchKey) -> int:
    """加权打分。越大越靠前；0 表示不匹配。

    权重设计：原名 > 全拼前缀 > 全拼包含 > 首字母 > 分词全命中。
    """
    q = query
    if not q:
        return 1  # 任意项都"弱匹配"，排序交给 query() 的分区逻辑
    name = sk.name_lower
    concat = sk.pinyin_concat
    spaced = sk.pinyin_spaced

    # 1. 原名精确/前缀/包含
    if name == q:
        return 1000
    if name.startswith(q):
        return 900
    if q in name:
        return 800

    # 2. 全拼前缀（含边界）：'siyuan' 或 'yuan h' 提前
    if concat.startswith(q):
        return 700
    if spaced.startswith(q):
        return 690

    # 3. 全拼包含（连续 + 空格形式都算）
    if q in concat:
        return 600
    if q in spaced:
        return 590

    # 4. 首字母缩写：精确/前缀/包含
    if sk.initials == q:
        return 500
    if sk.initials.startswith(q):
        return 450
    if q in sk.initials:
        return 400

    # 5. 多词查询：每个词都需在某一种形式里命中（顺序无关）
    tokens = [t for t in q.split() if t]
    if len(tokens) > 1:
        ok = 0
        for t in tokens:
            if t in name or t in concat or t in spaced or t in sk.initials:
                ok += 1
        if ok == len(tokens):
            return 300 + min(len(tokens), 5)
    return 0


# ---------------------------------------------------------------------------
# 字体索引
# ---------------------------------------------------------------------------

class FontIndex:
    """一次性构建的字体搜索索引。"""

    def __init__(self, families: List[str], recent: Optional[List[str]] = None):
        self.families = list(families)
        self.search_keys = {f: build_search_key(f) for f in self.families}
        self.recent = [f for f in (recent or []) if f in self.search_keys]

    def query(self, text: str) -> List[tuple]:
        """返回 (kind, value) 列表，kind ∈ {'header', 'font'}。

        主列表不再含"最近使用"区（移至独立边栏），因此全部字体可直接滚动定位。
        空查询: 全部字体(汉字类置顶)。搜索: 相关度排序的结果(recent 优先)。
        """
        q = text.strip().lower()
        recent_set = set(self.recent)
        recent_rank = {f: i for i, f in enumerate(self.recent)}

        items: List[tuple] = []
        if not q:
            cjk = sorted((f for f in self.families if contains_cjk(f)), key=lambda s: s.lower())
            other = sorted((f for f in self.families if not contains_cjk(f)), key=lambda s: s.lower())
            items.extend(('font', f) for f in cjk + other)
        else:
            scored = []
            for fam in self.families:
                s = match_score(q, self.search_keys[fam])
                if s <= 0:
                    continue
                is_recent = 0 if fam in recent_set else 1
                rr = recent_rank.get(fam, 999)
                scored.append((is_recent, rr, -s, fam.lower(), fam))
            scored.sort(key=lambda t: (t[0], t[1], t[2], t[3]))
            items.extend(('font', t[4]) for t in scored)
        return items

    def recent_list(self) -> List[str]:
        """最近使用区要显示的字体（上限 RECENT_MAX_VISIBLE）。"""
        return [f for f in self.recent[:RECENT_MAX_VISIBLE] if f in self.search_keys]


# ---------------------------------------------------------------------------
# 列表模型
# ---------------------------------------------------------------------------

class FontListModel(QAbstractListModel):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[tuple] = []   # (kind, value); kind ∈ {'header','font'}

    def set_items(self, items: List[tuple]):
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        kind, value = self._items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return value
        if role == TYPE_ROLE:
            return kind
        if role == NAME_ROLE:
            return value if kind == 'font' else None
        return None


# ---------------------------------------------------------------------------
# 委托 — 只为可见行用各自字体渲染预览
# ---------------------------------------------------------------------------

class FontItemDelegate(QStyledItemDelegate):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._font_cache = {}
        self.row_height = 34
        self.header_height = 22

    def _font_for(self, family: str) -> QFont:
        f = self._font_cache.get(family)
        if f is None:
            f = QFont(family)
            f.setPointSize(12)
            f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
            self._font_cache[family] = f
        return f

    def sizeHint(self, option, index) -> QSize:
        if index.data(TYPE_ROLE) == 'header':
            return QSize(option.rect.width(), self.header_height)
        return QSize(option.rect.width(), self.row_height)

    def paint(self, painter: QPainter, option, index):
        painter.save()
        kind = index.data(TYPE_ROLE)
        if kind == 'header':
            self._paint_header(painter, option, index)
            painter.restore()
            return
        family = index.data(NAME_ROLE) or ''
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)

        from utils import shared as C
        dark = isDarkTheme()

        if selected:
            painter.fillRect(option.rect, QColor(30, 147, 229, 80 if dark else 55))
        elif hover:
            painter.fillRect(option.rect, QColor(30, 147, 229, 45 if dark else 25))

        rect = option.rect.adjusted(10, 0, -10, 0)
        painter.setPen(QColor(255, 255, 255, 30) if dark else QColor(0, 0, 0, 20))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())

        painter.setFont(self._font_for(family))
        if selected:
            text_color = QColor(150, 200, 255) if dark else QColor(20, 90, 160)
        else:
            text_color = QColor(*C.FOREGROUND_FONTCOLOR)
        painter.setPen(text_color)
        fm = QFontMetrics(self._font_for(family))
        elided = fm.elidedText(family, Qt.TextElideMode.ElideRight, rect.width() - 8)
        painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)

        painter.restore()

    def _paint_header(self, painter: QPainter, option, index):
        title = index.data(Qt.ItemDataRole.DisplayRole) or ''
        dark = isDarkTheme()
        # 淡背景 + 标题
        if dark:
            painter.fillRect(option.rect, QColor(40, 45, 55, 200))
            painter.setPen(QColor(120, 130, 160))
        else:
            painter.fillRect(option.rect, QColor(0, 0, 0, 18))
            painter.setPen(QColor(120, 120, 120))
        painter.setFont(QFont('Microsoft YaHei UI', 9, QFont.Weight.Bold))
        rect = option.rect.adjusted(10, 0, -10, 0)
        painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, title)


# ---------------------------------------------------------------------------
# 弹出选择器
# ---------------------------------------------------------------------------

class FontPickerPopup(QDialog):

    font_selected = Signal(str)

    def __init__(self, families: List[str], recent: Optional[List[str]] = None,
                 current_font: str = '', parent=None):
        super().__init__(parent)
        # 注意: 不能用 Qt.WindowType.Popup —— 在 Windows 上 Popup 窗口会禁用 IME，
        # 导致搜索框无法输入中文。Tool 标志也含 Popup 位，同样不行。
        # 用无边框、非模态的顶层窗口，自绘"点击外部关闭"（见 eventFilter）。
        # 非模态很关键: 模态窗口会拦截窗口外的点击，事件到不了应用级过滤器。
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setModal(False)
        self.setFixedWidth(480)
        self.setFixedHeight(440)

        self.index = FontIndex(families, recent)
        self._current_font = current_font
        self._press_outside = False  # 外部按下标记，松手时据此决定是否关闭

        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText(self.tr('Search fonts / 拼音首字母…'))
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._on_search)
        self.search_edit.installEventFilter(self)

        # ---- 主列表（全部字体，用于滚动定位当前字体） ----
        self.model = FontListModel(self)
        self.view = QListView(self)
        self.view.setModel(self.model)
        self.view.setItemDelegate(FontItemDelegate(self.view))
        self.view.setUniformItemSizes(True)
        self.view.setMouseTracking(True)
        self.view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.clicked.connect(self._on_item_clicked)
        self.view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)

        # ---- 左侧边栏：最近使用 ----
        self.recent_title = QLabel(RECENT_TITLE, self)
        self.recent_model = FontListModel(self)
        self.recent_view = QListView(self)
        self.recent_view.setObjectName('RecentFontView')
        self.recent_view.setModel(self.recent_model)
        self.recent_view.setItemDelegate(FontItemDelegate(self.recent_view))
        self.recent_view.setUniformItemSizes(True)
        self.recent_view.setMouseTracking(True)
        self.recent_view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.recent_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.recent_view.clicked.connect(self._on_item_clicked)
        self.recent_view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.recent_view.setFixedWidth(150)

        sidebar = QFrame(self)
        sidebar.setObjectName('RecentSidebar')
        sidebar.setFixedWidth(158)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(0, 0, 6, 0)
        side_layout.setSpacing(4)
        side_layout.addWidget(self.recent_title)
        side_layout.addWidget(self.recent_view)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(sidebar)
        row.addWidget(self.view)

        self.status_label = QLabel(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(self.search_edit)
        layout.addLayout(row)
        layout.addWidget(self.status_label)

        self._apply_theme()
        self._fill_recent()
        self._populate('')

    def _apply_theme(self):
        dark = isDarkTheme()
        if dark:
            bg = '#191d24'
            border = '#535671'
            fg = '#8e99b1'
            dim = '#6a7290'
        else:
            bg = 'whitesmoke'
            border = '#b3b6bf'
            fg = '#5d5d5f'
            dim = '#888888'
        self.setStyleSheet(f"""
            FontPickerPopup {{ background-color: {bg}; border: 1px solid {border}; border-radius: 6px; }}
            FontPickerPopup QLineEdit {{
                background-color: {bg}; color: {fg};
                border: 1px solid {border}; border-radius: 4px;
                height: 32px; padding-left: 8px; font-size: 14px;
            }}
            FontPickerPopup QLineEdit:focus {{ border: 1px solid rgb(30, 147, 229); }}
            FontPickerPopup QListView {{
                background-color: {bg}; color: {fg};
                border: 1px solid {border}; border-radius: 4px; outline: none;
            }}
            FontPickerPopup QFrame#RecentSidebar {{
                border-right: 1px solid {border};
            }}
        """)
        self.recent_title.setStyleSheet(
            f'color: {dim}; font-size: 11px; font-weight: bold; padding: 2px 6px;')
        self.status_label.setStyleSheet(f'color: {dim}; font-size: 11px; padding: 2px 6px;')

    def _fill_recent(self):
        """填充左侧"最近使用"边栏。"""
        rec = self.index.recent_list()
        self.recent_model.set_items([('font', f) for f in rec])
        self.recent_title.setVisible(bool(rec))

    def _populate(self, text: str):
        items = self.index.query(text)
        self.model.set_items(items)
        font_count = sum(1 for k, _ in items if k == 'font')
        self.status_label.setText(self.tr('{} fonts').format(font_count))
        # 主列表定位到第一行（无 header，首行即字体）
        first = self._first_font_row()
        if first >= 0:
            idx = self.model.index(first, 0)
            self.view.setCurrentIndex(idx)
            self.view.scrollTo(idx)

    def _first_font_row(self) -> int:
        for row in range(self.model.rowCount()):
            if self.model.index(row, 0).data(TYPE_ROLE) == 'font':
                return row
        return -1

    def _on_search(self, text: str):
        self._populate(text)

    def _on_item_clicked(self, index: QModelIndex):
        if index.data(TYPE_ROLE) != 'font':
            return
        fam = index.data(NAME_ROLE)
        if fam:
            self._accept_font(fam)

    def _accept_font(self, family: str):
        self.font_selected.emit(family)
        self.accept()

    def keyPressEvent(self, e):
        # 弹窗级别 Esc 关闭（搜索框失焦时也能用）
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(e)

    def eventFilter(self, obj, event):
        et = event.type()
        if self.isVisible():
            if et == QEvent.Type.MouseButtonPress:
                # 只记录按下位置是否在弹窗外，松手时才关闭
                try:
                    gp = event.globalPosition().toPoint()
                except Exception:
                    gp = event.globalPos()
                self._press_outside = not self.geometry().contains(gp)
                return False
            elif et == QEvent.Type.MouseButtonRelease:
                try:
                    gp = event.globalPosition().toPoint()
                except Exception:
                    gp = event.globalPos()
                # 按下在外部且松手仍在外部 → 关闭
                if self._press_outside and not self.geometry().contains(gp):
                    self.close()
                self._press_outside = False
                return False
        if obj is self.search_edit and et == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Down:
                self._move_selection(1)
                return True
            elif key == Qt.Key.Key_Up:
                self._move_selection(-1)
                return True
            elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                idx = self.view.currentIndex()
                fam = idx.data(NAME_ROLE) if idx.isValid() and idx.data(TYPE_ROLE) == 'font' else None
                if fam:
                    self._accept_font(fam)
                return True
            elif key == Qt.Key.Key_Escape:
                self.close()
                return True
        return super().eventFilter(obj, event)

    def _move_selection(self, delta: int):
        cur = self.view.currentIndex()
        row = cur.row() if cur.isValid() else self._first_font_row()
        n = self.model.rowCount()
        if n == 0:
            return
        row = row + delta
        # 跳过 header 行，只落在 font 行上
        while 0 <= row < n:
            if self.model.index(row, 0).data(TYPE_ROLE) == 'font':
                break
            row += delta
        if not 0 <= row < n:
            return
        new_idx = self.model.index(row, 0)
        self.view.setCurrentIndex(new_idx)
        self.view.scrollTo(new_idx)

    def showEvent(self, e):
        super().showEvent(e)
        self._press_outside = False
        self._apply_theme()
        self.search_edit.setFocus()
        if self.search_edit.text():
            self.search_edit.clear()
        # 定位到当前字体（clear 可能触发 _on_search，故放在其后）
        self._reveal_current()
        # 无边框窗口丢失了 Popup 的"点击外部自动关闭"，用应用级过滤恢复
        QApplication.instance().installEventFilter(self)

    def _reveal_current(self):
        """滚动并高亮当前字体所在行（在"全部字体"主列表中定位）。"""
        items = self.index.query('')
        row = -1
        for i, (kind, val) in enumerate(items):
            if kind == 'font' and val == self._current_font:
                row = i
                break
        if row >= 0:
            idx = self.model.index(row, 0)
            self.view.setCurrentIndex(idx)
            self.view.scrollTo(idx, QListView.ScrollHint.PositionAtCenter)

    def hideEvent(self, e):
        super().hideEvent(e)
        QApplication.instance().removeEventFilter(self)
