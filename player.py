import os
import sys
import platform
import threading
import json
import logging
import socket
import warnings
import tempfile
import shutil
import subprocess
import re
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse, unquote

warnings.filterwarnings("ignore", category=DeprecationWarning)

try:
    import winreg
    HAS_WINREG = True
except ImportError:
    HAS_WINREG = False

VLC_MANUAL_PATH = ""

CONTEXT_MENU_EXTS = [
    ".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm",
    ".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".ts",
    ".m3u8", ".rmvb", ".3gp", ".mpg", ".mpeg", ".vob", ".m4v",
]

DATA_DIR = Path.home() / ".universal_player"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "player.log"

logger = logging.getLogger("UniversalPlayer")
logger.setLevel(logging.DEBUG)
logger.propagate = False

if not logger.handlers:
    fmt = logging.Formatter("[%(asctime)s] [%(levelname)-7s] [%(threadName)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=2*1024*1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt); fh.setLevel(logging.DEBUG); logger.addHandler(fh)
    ch = logging.StreamHandler(); ch.setFormatter(fmt); ch.setLevel(logging.INFO); logger.addHandler(ch)


def log_sep(title=""):
    logger.info("=" * 60)
    if title: logger.info(title)
    logger.info("=" * 60)


log_sep(f"启动 万能播放器  系统={platform.system()} {platform.release()}  Python={sys.version.split()[0]}")


def setup_vlc_path():
    if platform.system() != "Windows": return
    cands = []
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        for d in (base, os.path.join(base, "_internal"), os.path.join(base, "_MEIPASS")):
            if d and os.path.exists(os.path.join(d, "libvlc.dll")):
                cands.insert(0, d)
    if VLC_MANUAL_PATH: cands.append(VLC_MANUAL_PATH)
    if os.environ.get("VLC_HOME"): cands.append(os.environ["VLC_HOME"])
    cands += [r"C:\Program Files\VideoLAN\VLC", r"C:\Program Files (x86)\VideoLAN\VLC"]
    for d in cands:
        if d and os.path.exists(os.path.join(d, "libvlc.dll")):
            os.environ["PYTHON_VLC_MODULE_PATH"] = d
            os.environ["PATH"] = d + ";" + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                globals()["_vlc_dll_handle"] = os.add_dll_directory(d)
            logger.info(f"✅ 已找到 VLC：{d}")
            return d
    logger.warning("⚠️ 未自动找到 VLC")
    return None


setup_vlc_path()

try:
    import vlc
    logger.info(f"VLC 版本：{vlc.libvlc_get_version().decode(errors='ignore')}")
except ImportError:
    logger.critical("未安装 python-vlc"); sys.exit(1)
except FileNotFoundError as e:
    logger.critical(f"❌ 找不到 libvlc.dll：{e}"); sys.exit(1)

try:
    import yt_dlp
    HAS_YTDLP = True
    logger.info(f"yt-dlp 版本：{yt_dlp.version.__version__}")
except ImportError:
    HAS_YTDLP = False
    logger.warning("ℹ️ 未安装 yt-dlp")

FFMPEG_PATH = shutil.which("ffmpeg")
if not FFMPEG_PATH and getattr(sys, "frozen", False):
    base = os.path.dirname(sys.executable)
    for d in (base, os.path.join(base, "_internal")):
        p = os.path.join(d, "ffmpeg.exe")
        if os.path.exists(p):
            FFMPEG_PATH = p; break

ARIA2C_PATH = shutil.which("aria2c")
if not ARIA2C_PATH and getattr(sys, "frozen", False):
    base = os.path.dirname(sys.executable)
    for d in (base, os.path.join(base, "_internal")):
        p = os.path.join(d, "aria2c.exe")
        if os.path.exists(p):
            ARIA2C_PATH = p; break

if FFMPEG_PATH: logger.info(f"ffmpeg 路径：{FFMPEG_PATH}")
else: logger.warning("⚠️ 未找到 ffmpeg")

if ARIA2C_PATH: logger.info(f"✅ aria2c 路径：{ARIA2C_PATH}")
else: logger.info("ℹ️ 未找到 aria2c，将使用 yt-dlp 内建多线程下载")

from PySide6.QtCore import Qt, QTimer, Signal, QObject, QUrl
from PySide6.QtGui import QAction, QIcon, QPixmap, QColor, QPainter, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QSlider, QLabel, QFileDialog, QFrame, QMessageBox,
    QSizePolicy, QComboBox, QInputDialog, QListWidget, QListWidgetItem,
    QSplitter, QTabWidget, QAbstractItemView, QMenu, QSystemTrayIcon,
    QDialog, QDialogButtonBox, QLineEdit, QCheckBox, QFormLayout,
    QPlainTextEdit, QSpinBox, QToolButton, QScrollArea, QDialogButtonBox
)

try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import (
        QWebEngineUrlRequestInterceptor, QWebEngineUrlRequestInfo,
        QWebEngineSettings, QWebEnginePage,
    )
    HAS_WEBENGINE = True
except ImportError as e:
    HAS_WEBENGINE = False
    logger.warning(f"⚠️ 未安装 PySide6-WebEngine：{e}")

_vlc_instance = None

def get_vlc_instance():
    global _vlc_instance
    if _vlc_instance is None:
        _vlc_instance = vlc.Instance()
    return _vlc_instance


PLAYLIST_FILE = DATA_DIR / "playlist.json"
FAVORITES_FILE = DATA_DIR / "favorites.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
BOOKMARKS_FILE = DATA_DIR / "browser_bookmarks.json"

DEFAULT_SETTINGS = {
    "proxy_enabled": False,
    "proxy_url": "http://127.0.0.1:7890",
    "proxy_fallback": True,
    "close_to_tray": True,
    "minimize_to_tray": False,
    "log_level": "INFO",
    "log_max_lines": 3000,
    "youtube_download_mode": True,
    "youtube_max_height": 1080,
    "record_dir": str(Path.home() / "Videos" / "UniversalPlayerRecordings"),
    "record_ask_path": False,
    "download_dir": str(Path.home() / "Downloads" / "UniversalPlayer"),
    "download_threads": 16,
    "download_ask_threads": True,
    "browser_home": "https://www.bing.com",
    "browser_user_agent": "",
    "browser_disable_gpu": False,
    "browser_show_bookmarks": True,
    "browser_manual_navigation": False,
    "browser_sniff_mode": "m3u8",
}


DEFAULT_BOOKMARKS = [
    {"title": "B站",     "url": "https://www.bilibili.com"},
    {"title": "YouTube", "url": "https://www.youtube.com"},
    {"title": "爱奇艺",   "url": "https://www.iqiyi.com"},
    {"title": "腾讯视频", "url": "https://v.qq.com"},
    {"title": "优酷",    "url": "https://www.youku.com"},
    {"title": "芒果TV",  "url": "https://www.mgtv.com"},
    {"title": "抖音",    "url": "https://www.douyin.com"},
    {"title": "快手",    "url": "https://www.kuaishou.com"},
    {"title": "CCTV",   "url": "https://tv.cctv.com"},
    {"title": "微博",    "url": "https://weibo.com"},
    {"title": "知乎",    "url": "https://www.zhihu.com"},
    {"title": "豆瓣",    "url": "https://www.douban.com"},
]


_RES_PATTERNS = [
    (re.compile(r"(?:^|[/_\-.?&])3840[xX]2160(?:[/_\-.?&]|$)"), 2160),
    (re.compile(r"(?:^|[/_\-.?&])2560[xX]1440(?:[/_\-.?&]|$)"), 1440),
    (re.compile(r"(?:^|[/_\-.?&])1920[xX]1080(?:[/_\-.?&]|$)"), 1080),
    (re.compile(r"(?:^|[/_\-.?&])1280[xX]720(?:[/_\-.?&]|$)"), 720),
    (re.compile(r"(?:^|[/_\-.?&])854[xX]480(?:[/_\-.?&]|$)"), 480),
    (re.compile(r"(?:^|[/_\-.?&])640[xX]360(?:[/_\-.?&]|$)"), 360),
    (re.compile(r"(?:^|[/_\-.?&])2160[pP]?(?:[/_\-.?&]|$)"), 2160),
    (re.compile(r"(?:^|[/_\-.?&])1440[pP]?(?:[/_\-.?&]|$)"), 1440),
    (re.compile(r"(?:^|[/_\-.?&])1080[pP]?(?:[/_\-.?&]|$)"), 1080),
    (re.compile(r"(?:^|[/_\-.?&])720[pP]?(?:[/_\-.?&]|$)"), 720),
    (re.compile(r"(?:^|[/_\-.?&])480[pP]?(?:[/_\-.?&]|$)"), 480),
    (re.compile(r"(?:^|[/_\-.?&])360[pP]?(?:[/_\-.?&]|$)"), 360),
    (re.compile(r"(?:^|[/_\-.?&])240[pP]?(?:[/_\-.?&]|$)"), 240),
    (re.compile(r"\b4k\b", re.IGNORECASE), 2160),
    (re.compile(r"\buhd\b", re.IGNORECASE), 2160),
    (re.compile(r"\bfhd\b", re.IGNORECASE), 1080),
    (re.compile(r"\bhd\b", re.IGNORECASE), 720),
    (re.compile(r"\bsd\b", re.IGNORECASE), 480),
]


def guess_resolution(url):
    if not url: return 0
    low = url.lower(); best = 0
    for pat, res in _RES_PATTERNS:
        if pat.search(low) and res > best: best = res
    return best


def load_json(path, default):
    try:
        if path.exists(): return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e: logger.error(f"读取 {path} 失败：{e}")
    return default


def save_json(path, data):
    try: path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e: logger.error(f"保存 {path} 失败：{e}")


def load_settings():
    s = DEFAULT_SETTINGS.copy()
    s.update(load_json(SETTINGS_FILE, {}))
    return s


def save_settings(s): save_json(SETTINGS_FILE, s)


def load_bookmarks():
    if BOOKMARKS_FILE.exists():
        data = load_json(BOOKMARKS_FILE, None)
        if isinstance(data, list): return data
    save_json(BOOKMARKS_FILE, DEFAULT_BOOKMARKS)
    return list(DEFAULT_BOOKMARKS)


def save_bookmarks(bms): save_json(BOOKMARKS_FILE, bms)


def apply_log_level(name):
    levels = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
              "WARNING": logging.WARNING, "ERROR": logging.ERROR}
    lvl = levels.get(name.upper(), logging.INFO)
    for h in logger.handlers:
        if isinstance(h, RotatingFileHandler): h.setLevel(logging.DEBUG)
        elif isinstance(h, logging.StreamHandler): h.setLevel(lvl)
    logger.info(f"日志级别设置为：{name}")


def test_proxy(proxy_url, timeout=1.5):
    if not proxy_url: return False
    try:
        parsed = urlparse(proxy_url)
        host, port = parsed.hostname, parsed.port
        if not host or not port: return False
        with socket.create_connection((host, port), timeout=timeout): return True
    except Exception as e:
        logger.warning(f"代理不可用 {proxy_url}：{e}")
        return False


def make_tray_icon():
    pm = QPixmap(64, 64); pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(60, 120, 220)); p.setPen(QColor(30, 60, 120))
    p.drawRoundedRect(4, 4, 56, 56, 12, 12)
    p.setBrush(QColor(255, 255, 255)); p.setPen(Qt.PenStyle.NoPen)
    from PySide6.QtCore import QPoint as _QPoint
    p.drawPolygon([_QPoint(24, 18), _QPoint(24, 46), _QPoint(46, 32)])
    p.end(); return QIcon(pm)


def sanitize_filename(name, max_len=60):
    invalid = '<>:"/\\|?*'
    cleaned = "".join(c for c in name if c not in invalid and ord(c) >= 32)
    return (cleaned.strip(" .")[:max_len].strip()) or "download"


def suggest_filename_from_url(url):
    try:
        parsed = urlparse(url)
        name = unquote(Path(parsed.path).name)
        if name and "." in name:
            return sanitize_filename(name, 80)
    except Exception:
        pass
    return "download"


def get_app_cmd_template():
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return f'"{exe}" "%1"', f"{exe},0"
    else:
        return f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}" "%1"', f"{sys.executable},0"


def register_context_menu():
    if not HAS_WINREG: return 0, ["仅支持 Windows"]
    cmd, icon = get_app_cmd_template(); ok = 0; errors = []
    for ext in CONTEXT_MENU_EXTS:
        try:
            key = f"Software\\Classes\\SystemFileAssociations\\{ext}\\shell\\UniversalPlayer"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "用万能播放器播放")
                winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key + "\\command") as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, cmd)
            ok += 1
        except Exception as e: errors.append(f"{ext}: {e}")
    return ok, errors


def unregister_context_menu():
    if not HAS_WINREG: return 0, ["仅支持 Windows"]
    ok = 0; errors = []
    for ext in CONTEXT_MENU_EXTS:
        try:
            key = f"Software\\Classes\\SystemFileAssociations\\{ext}\\shell\\UniversalPlayer"
            try: winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key + "\\command")
            except FileNotFoundError: pass
            try: winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            except FileNotFoundError: pass
            ok += 1
        except Exception as e: errors.append(f"{ext}: {e}")
    return ok, errors


def is_context_menu_registered():
    if not HAS_WINREG: return False
    try:
        key = "Software\\Classes\\SystemFileAssociations\\.mp4\\shell\\UniversalPlayer"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key): return True
    except FileNotFoundError: return False
    except Exception: return False


# ============================================================
# ★ 下载选项对话框（自选分段）
# ============================================================
class DownloadOptionsDialog(QDialog):
    def __init__(self, default_threads=16, url="", parent=None, mode="quick"):
        super().__init__(parent)
        self.setWindowTitle("下载选项")
        self.setMinimumWidth(440)
        self.mode = mode

        form = QFormLayout(self)

        # 链接预览
        short = url[:80] + ("..." if len(url) > 80 else "") if url else "（无）"
        url_label = QLabel(short)
        url_label.setStyleSheet("color:#666; font-size:11px;")
        url_label.setWordWrap(True)
        form.addRow("下载链接", url_label)

        # 分段数
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 64)
        self.spin_threads.setValue(max(1, min(64, int(default_threads))))
        self.spin_threads.setSuffix(" 并发")
        form.addRow("并发分段数", self.spin_threads)

        # 预设按钮
        preset_row = QWidget()
        preset_layout = QHBoxLayout(preset_row)
        preset_layout.setContentsMargins(0, 0, 0, 0)
        preset_layout.setSpacing(4)
        for label, val in [("单线程 1", 1), ("均衡 8", 8), ("推荐 16", 16), ("极速 32", 32), ("极限 64", 64)]:
            b = QPushButton(label)
            b.setFixedHeight(24)
            b.clicked.connect(lambda checked=False, v=val: self.spin_threads.setValue(v))
            preset_layout.addWidget(b)
        preset_layout.addStretch()
        form.addRow("快速预设", preset_row)

        # 提示
        tip = QLabel(
            "ℹ️ 分段越多越快，但可能被服务器限流。\n"
            "   m3u8 分片流：16 已足够；mp4 直链：装 aria2c 后 32 更快。"
        )
        tip.setStyleSheet("color:#888;font-size:11px;")
        tip.setWordWrap(True)
        form.addRow(tip)

        # 记住选项
        self.chk_remember = QCheckBox("记住这个选择（下次不再询问）")
        self.chk_remember.setChecked(False)
        form.addRow(self.chk_remember)

        # 引擎信息
        engine = "aria2c" if ARIA2C_PATH else "yt-dlp 内建"
        info = QLabel(f"当前下载引擎：{engine}")
        info.setStyleSheet("color:#3a9;font-size:11px;")
        form.addRow(info)

        # 按钮
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("开始下载" if mode == "quick" else "下一步")
        ok_btn.setDefault(True)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def get_values(self):
        return self.spin_threads.value(), self.chk_remember.isChecked()


# ============================================================
# 下载管理器
# ============================================================
class DownloadManager:
    def __init__(self, threads=16):
        self.thread = None
        self.output_file = None
        self.start_time = None
        self.source = None
        self.title = None
        self.threads = threads

        self._stop_requested = False
        self._done = False
        self._error = None
        self._final_file = None

        self.progress_percent = 0.0
        self.progress_speed = ""
        self.progress_eta = ""
        self.progress_downloaded = 0
        self.progress_total = 0

    def is_downloading(self):
        return self.thread is not None and self.thread.is_alive()

    def elapsed_seconds(self):
        if self.start_time:
            return int((datetime.now() - self.start_time).total_seconds())
        return 0

    def current_size(self):
        return self.progress_downloaded

    def start(self, url, output_file, title=None, proxy=None, headers=None, threads=None):
        if self.is_downloading():
            return False, "已有下载进行中"
        if not HAS_YTDLP:
            return False, "下载需要 yt-dlp（请运行：pip install yt-dlp）"

        if threads:
            self.threads = max(1, min(64, int(threads)))

        self.source = url
        self.output_file = output_file
        self.title = title or output_file
        self.start_time = datetime.now()
        self._stop_requested = False
        self._done = False
        self._error = None
        self._final_file = None
        self.progress_percent = 0.0
        self.progress_speed = ""
        self.progress_eta = ""
        self.progress_downloaded = 0
        self.progress_total = 0

        self.thread = threading.Thread(
            target=self._worker,
            args=(url, output_file, proxy, headers),
            daemon=True,
            name="Downloader",
        )
        self.thread.start()
        return True, output_file

    def _worker(self, url, output_file, proxy, headers):
        try:
            out_p = Path(output_file)
            out_dir = out_p.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            out_base = out_p.stem
            out_tmpl = str(out_dir / f"{out_base}.%(ext)s")

            opts = {
                "outtmpl": out_tmpl,
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "concurrent_fragment_downloads": self.threads,
                "retries": 10,
                "fragment_retries": 10,
                "socket_timeout": 30,
                "progress_hooks": [self._progress_hook],
            }
            if proxy:
                opts["proxy"] = proxy
            if headers:
                opts["http_headers"] = dict(headers)

            if ARIA2C_PATH:
                opts["external_downloader"] = {
                    "http": ARIA2C_PATH, "https": ARIA2C_PATH, "m3u8": ARIA2C_PATH,
                }
                opts["external_downloader_args"] = {
                    ARIA2C_PATH: [
                        "-x", str(self.threads),
                        "-s", str(self.threads),
                        "-k", "1M",
                        "--file-allocation=none",
                        "--summary-interval=0",
                        "--console-log-level=warn",
                    ]
                }

            logger.info(f"下载启动：threads={self.threads} aria2c={'yes' if ARIA2C_PATH else 'no'}")

            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

            self._final_file = self._find_final_file(out_dir, out_base, output_file)
            logger.info(f"下载完成：{self._final_file}")

        except Exception as e:
            if self._stop_requested:
                self._error = "用户已停止"
            else:
                self._error = str(e)
                logger.exception(f"下载失败：{e}")
        finally:
            self._done = True

    def _find_final_file(self, out_dir, out_base, original_output):
        if os.path.exists(original_output):
            return original_output
        candidates = list(out_dir.glob(f"{out_base}.*"))
        candidates = [c for c in candidates if not c.name.endswith((".part", ".ytdl", ".temp"))]
        if not candidates:
            return original_output
        candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        return str(candidates[0])

    def _progress_hook(self, d):
        if self._stop_requested:
            raise Exception("__USER_STOP__")
        try:
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                if total > 0:
                    self.progress_percent = downloaded / total * 100.0
                    self.progress_total = total
                self.progress_downloaded = downloaded
                self.progress_speed = (d.get("_speed_str") or "").strip()
                self.progress_eta = (d.get("_eta_str") or "").strip()
            elif status == "finished":
                self.progress_percent = 100.0
                self.progress_speed = "合并中..."
                self.progress_eta = ""
        except Exception as e:
            if "__USER_STOP__" in str(e):
                raise

    def stop(self, timeout=8):
        if not self.is_downloading():
            return False, "没有正在进行的下载"
        self._stop_requested = True
        self.thread.join(timeout=timeout)
        out = self._final_file or self.output_file
        size = 0
        if out and os.path.exists(out):
            size = os.path.getsize(out)
        self.thread = None
        self._stop_requested = False
        return True, f"{out}\n\n（已停止，文件大小 {size/1024/1024:.2f} MB）"

    def get_status_text(self):
        if self.is_downloading():
            if self.progress_total > 0:
                return f"{self.progress_percent:.1f}%  {self.progress_speed}  剩余 {self.progress_eta}"
            else:
                return f"{self.progress_speed}  {self.progress_eta}"
        if self._done:
            if self._error:
                return f"失败：{self._error[:80]}"
            return "✅ 完成"
        return ""


if HAS_WEBENGINE:
    class ManualNavPage(QWebEnginePage):
        def __init__(self, profile, parent, confirm_callback=None):
            super().__init__(profile, parent)
            self.confirm_callback = confirm_callback
        def acceptNavigationRequest(self, url, nav_type, is_main_frame):
            try:
                if (is_main_frame and
                    nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked):
                    if self.confirm_callback:
                        if not self.confirm_callback(url.toString()): return False
            except Exception as e: logger.warning(f"导航拦截异常：{e}")
            return super().acceptNavigationRequest(url, nav_type, is_main_frame)


if HAS_WEBENGINE:
    class StreamSniffer(QWebEngineUrlRequestInterceptor):
        M3U8_EXTS = (".m3u8",)
        ALL_EXTS = (".m3u8", ".mp4", ".flv", ".ts", ".mkv", ".webm", ".mpd", ".mov")
        def __init__(self, callback, parent=None, mode="m3u8"):
            super().__init__(parent); self.callback = callback
            self._seen = set(); self.mode = mode
        def set_mode(self, mode): self.mode = mode
        def interceptRequest(self, info):
            try:
                url = info.requestUrl().toString()
                if not url: return
                low = url.lower().split("?")[0].split("#")[0]
                exts = self.M3U8_EXTS if self.mode == "m3u8" else self.ALL_EXTS
                matched = low.endswith(exts)
                if not matched and self.mode == "all":
                    try:
                        if info.resourceType() == QWebEngineUrlRequestInfo.ResourceType.ResourceTypeMedia:
                            matched = True
                    except Exception: pass
                if matched and url not in self._seen:
                    self._seen.add(url); self.callback(url)
            except Exception as e: logger.warning(f"嗅探异常：{e}")


class BookmarkButton(QToolButton):
    def __init__(self, bookmark, parent=None):
        super().__init__(parent)
        self.bookmark = bookmark
        self.setText(bookmark.get("title", "书签")[:12])
        self.setToolTip(f"{bookmark.get('title')}\n{bookmark.get('url')}")
        self.setAutoRaise(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        self.setStyleSheet(
            "QToolButton{padding:2px 8px; border:1px solid transparent; border-radius:3px;}"
            "QToolButton:hover{background:#3a3a3a; border:1px solid #555;}")
    def _show_menu(self, pos):
        menu = QMenu(self)
        a_open = menu.addAction("打开"); a_edit = menu.addAction("编辑...")
        a_del = menu.addAction("删除"); menu.addSeparator()
        a_copy = menu.addAction("复制地址")
        act = menu.exec(self.mapToGlobal(pos))
        if act == a_open: self.clicked.emit()
        elif act == a_edit: self._edit()
        elif act == a_del: self._delete()
        elif act == a_copy: QApplication.clipboard().setText(self.bookmark.get("url", ""))
    def _edit(self):
        title, ok1 = QInputDialog.getText(self, "编辑书签", "名称：", text=self.bookmark.get("title", ""))
        if not ok1: return
        url, ok2 = QInputDialog.getText(self, "编辑书签", "网址：", text=self.bookmark.get("url", ""))
        if not ok2: return
        self.bookmark["title"] = title.strip() or url
        self.bookmark["url"] = url.strip()
        self.setText(self.bookmark["title"][:12])
        self.setToolTip(f"{self.bookmark['title']}\n{self.bookmark['url']}")
        p = self.parent()
        while p and not isinstance(p, BrowserTab): p = p.parent()
        if isinstance(p, BrowserTab): p.save_bookmarks_now()
    def _delete(self):
        p = self.parent()
        while p and not isinstance(p, BrowserTab): p = p.parent()
        if isinstance(p, BrowserTab): p.remove_bookmark(self.bookmark)


class BrowserTab(QWidget):
    stream_found = Signal(str)
    stream_download = Signal(str)
    stream_download_quick = Signal(str)   # 直接用默认值
    stream_download_saveas = Signal(str)
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.view = None; self.sniffer = None
        self._initialized = False; self._home_loaded = False
        self.bookmarks = load_bookmarks()
        self._sniff_items = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        self._main_layout = layout
        if not HAS_WEBENGINE:
            msg = QLabel("未安装 PySide6-Addons")
            msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
            msg.setStyleSheet("color: #666; font-size: 14px; padding: 40px;")
            layout.addWidget(msg); return
        self.placeholder = QLabel("<div style='text-align:center'><h2>🌐 网页模式</h2></div>")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet("color:#aaa; background:#1e1e1e; padding:40px;")
        layout.addWidget(self.placeholder, stretch=1)

    def _ensure_initialized(self):
        if self._initialized or not HAS_WEBENGINE: return
        self._initialized = True
        while self._main_layout.count():
            item = self._main_layout.takeAt(0)
            w = item.widget()
            if w: w.deleteLater()

        toolbar = QWidget()
        tl = QHBoxLayout(toolbar); tl.setContentsMargins(4, 4, 4, 0); tl.setSpacing(4)
        self.btn_back = QToolButton(); self.btn_back.setText("←")
        self.btn_back.clicked.connect(lambda: self.view.back() if self.view else None); tl.addWidget(self.btn_back)
        self.btn_forward = QToolButton(); self.btn_forward.setText("→")
        self.btn_forward.clicked.connect(lambda: self.view.forward() if self.view else None); tl.addWidget(self.btn_forward)
        self.btn_reload = QToolButton(); self.btn_reload.setText("⟳")
        self.btn_reload.clicked.connect(lambda: self.view.reload() if self.view else None); tl.addWidget(self.btn_reload)
        self.btn_home = QToolButton(); self.btn_home.setText("⌂")
        self.btn_home.clicked.connect(self.go_home); tl.addWidget(self.btn_home)
        self.url_edit = QLineEdit(); self.url_edit.setPlaceholderText("输入网址，回车加载")
        self.url_edit.returnPressed.connect(self.navigate_to_input)
        tl.addWidget(self.url_edit, stretch=1)
        self.btn_star = QToolButton(); self.btn_star.setText("☆")
        self.btn_star.clicked.connect(self.add_current_bookmark); tl.addWidget(self.btn_star)
        self.btn_go = QPushButton("转到"); self.btn_go.clicked.connect(self.navigate_to_input); tl.addWidget(self.btn_go)
        self.btn_play_url = QPushButton("→ VLC 播放")
        self.btn_play_url.clicked.connect(self.play_current_url_with_vlc); tl.addWidget(self.btn_play_url)
        self._main_layout.addWidget(toolbar)

        self.bookmark_bar = QWidget()
        bbl = QHBoxLayout(self.bookmark_bar); bbl.setContentsMargins(4, 2, 4, 2); bbl.setSpacing(4)
        self.btn_toggle_bar = QToolButton(); self.btn_toggle_bar.setText("★")
        self.btn_toggle_bar.setCheckable(True)
        self.btn_toggle_bar.setChecked(self.settings.get("browser_show_bookmarks", True))
        self.btn_toggle_bar.clicked.connect(self.toggle_bookmark_bar); bbl.addWidget(self.btn_toggle_bar)
        self.bookmark_scroll = QScrollArea()
        self.bookmark_scroll.setWidgetResizable(True)
        self.bookmark_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.bookmark_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.bookmark_scroll.setFixedHeight(32)
        self.bookmark_scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self.bookmark_container = QWidget()
        self.bookmark_layout = QHBoxLayout(self.bookmark_container)
        self.bookmark_layout.setContentsMargins(0, 0, 0, 0); self.bookmark_layout.setSpacing(2)
        self.bookmark_layout.addStretch()
        self.bookmark_scroll.setWidget(self.bookmark_container)
        bbl.addWidget(self.bookmark_scroll, stretch=1)
        self.btn_add_bm = QToolButton(); self.btn_add_bm.setText("+")
        self.btn_add_bm.clicked.connect(self.add_bookmark_dialog); bbl.addWidget(self.btn_add_bm)
        self.btn_manage_bm = QToolButton(); self.btn_manage_bm.setText("⋯")
        self.btn_manage_bm.clicked.connect(self.show_bookmark_menu); bbl.addWidget(self.btn_manage_bm)
        self._main_layout.addWidget(self.bookmark_bar)
        self.bookmark_bar.setVisible(self.settings.get("browser_show_bookmarks", True))
        self.refresh_bookmark_buttons()

        self.view = QWebEngineView()
        manual = self.settings.get("browser_manual_navigation", False)
        if manual:
            try:
                profile = self.view.page().profile()
                custom_page = ManualNavPage(profile, self.view, self._confirm_navigation)
                self.view.setPage(custom_page)
            except Exception as e: logger.warning(f"启用手动跳转失败：{e}")
        self.view.urlChanged.connect(self._on_url_changed)
        ua = self.settings.get("browser_user_agent", "").strip()
        if ua: self.view.page().profile().setHttpUserAgent(ua)
        sniff_mode = self.settings.get("browser_sniff_mode", "m3u8")
        self.sniffer = StreamSniffer(self._on_stream_sniffed, mode=sniff_mode)
        self.view.page().profile().setUrlRequestInterceptor(self.sniffer)
        s = self.view.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.ScreenCaptureEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        self._main_layout.addWidget(self.view, stretch=1)

        sniff_box = QWidget()
        sbl = QVBoxLayout(sniff_box); sbl.setContentsMargins(4, 4, 4, 4); sbl.setSpacing(2)
        head = QHBoxLayout()
        self.sniff_title = QLabel(); self._update_sniff_title(); head.addWidget(self.sniff_title)
        head.addStretch()
        btn_clear = QPushButton("清空"); btn_clear.clicked.connect(self.clear_sniffed); head.addWidget(btn_clear)
        sbl.addLayout(head)
        self.sniff_list = QListWidget(); self.sniff_list.setMaximumHeight(140)
        self.sniff_list.itemDoubleClicked.connect(self._play_sniffed)
        self.sniff_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sniff_list.customContextMenuRequested.connect(self._sniff_context_menu)
        sbl.addWidget(self.sniff_list)
        self._main_layout.addWidget(sniff_box)

    def _update_sniff_title(self):
        mode = self.settings.get("browser_sniff_mode", "m3u8")
        if mode == "m3u8":
            self.sniff_title.setText("🎯 嗅探到的 m3u8（双击播放，右键下载）")
        else:
            self.sniff_title.setText("🎯 嗅探到的媒体流（双击播放，右键下载）")

    def _confirm_navigation(self, url):
        try:
            res = QMessageBox.question(self, "网页跳转确认",
                f"是否跳转到新页面？\n\n{url[:200]}{'...' if len(url) > 200 else ''}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            return res == QMessageBox.StandardButton.Yes
        except Exception as e: logger.warning(f"确认跳转异常：{e}"); return True

    def set_manual_navigation(self, enabled):
        if not self.view or not HAS_WEBENGINE: return
        try:
            profile = self.view.page().profile()
            if enabled:
                new_page = ManualNavPage(profile, self.view, self._confirm_navigation)
            else:
                new_page = QWebEnginePage(profile, self.view)
            if self.sniffer: profile.setUrlRequestInterceptor(self.sniffer)
            self.view.setPage(new_page)
        except Exception as e: logger.warning(f"切换手动跳转失败：{e}")

    def set_sniff_mode(self, mode):
        self.settings["browser_sniff_mode"] = mode
        if self.sniffer:
            self.sniffer.set_mode(mode)
            self.clear_sniffed()
        self._update_sniff_title()

    def _on_stream_sniffed(self, url):
        if not self.sniff_list: return
        for u, _ in self._sniff_items:
            if u == url: return
        res = guess_resolution(url)
        self._sniff_items.append((url, res))
        self._sniff_items.sort(key=lambda x: x[1], reverse=True)
        if len(self._sniff_items) > 100: self._sniff_items = self._sniff_items[:100]
        self._rebuild_sniff_list()

    def _rebuild_sniff_list(self):
        if not self.sniff_list: return
        self.sniff_list.clear()
        for url, res in self._sniff_items:
            label = f"[{res}p]  {url}" if res > 0 else f"[?p]  {url}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, url); item.setToolTip(url)
            if res >= 1080: item.setForeground(QColor("#7CFC00"))
            elif res >= 720: item.setForeground(QColor("#FFD700"))
            elif res > 0: item.setForeground(QColor("#AAAAAA"))
            self.sniff_list.addItem(item)

    def _sniff_context_menu(self, pos):
        item = self.sniff_list.itemAt(pos)
        if not item: return
        url = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        a_play = menu.addAction("▶ 用 VLC 播放")
        menu.addSeparator()
        a_dl_ask = menu.addAction("⚡ 下载（自选分段）")
        a_dl_quick = menu.addAction("⚡ 直接下载（默认分段）")
        a_dl_save = menu.addAction("⬇ 另存为...")
        menu.addSeparator()
        a_copy = menu.addAction("复制地址")
        a_browser = menu.addAction("在浏览器打开")
        menu.addSeparator()
        a_del = menu.addAction("从列表删除")
        act = menu.exec(self.sniff_list.mapToGlobal(pos))
        if act == a_play: self.stream_found.emit(url)
        elif act == a_dl_ask: self.stream_download.emit(url)
        elif act == a_dl_quick: self.stream_download_quick.emit(url)
        elif act == a_dl_save: self.stream_download_saveas.emit(url)
        elif act == a_copy:
            QApplication.clipboard().setText(url); self.statusBarMessage("已复制")
        elif act == a_browser:
            if self.view: self.view.setUrl(QUrl(url))
        elif act == a_del:
            for i, (u, r) in enumerate(self._sniff_items):
                if u == url: del self._sniff_items[i]; break
            self._rebuild_sniff_list()

    def refresh_bookmark_buttons(self):
        while self.bookmark_layout.count() > 1:
            item = self.bookmark_layout.takeAt(0)
            w = item.widget()
            if w: w.deleteLater()
        for bm in self.bookmarks:
            btn = BookmarkButton(bm, self.bookmark_container)
            btn.clicked.connect(lambda checked=False, u=bm.get("url", ""): self._nav(u))
            self.bookmark_layout.insertWidget(self.bookmark_layout.count() - 1, btn)

    def _nav(self, url):
        if not self.view or not url: return
        if not url.startswith(("http://", "https://", "file://")): url = "https://" + url
        self.view.setUrl(QUrl(url))

    def toggle_bookmark_bar(self):
        visible = self.btn_toggle_bar.isChecked()
        self.bookmark_bar.setVisible(visible)
        self.settings["browser_show_bookmarks"] = visible
        save_settings(self.settings)

    def add_current_bookmark(self):
        if not self.view: return
        url = self.view.url().toString()
        if not url: return
        title = self.view.title() or url
        for bm in self.bookmarks:
            if bm.get("url") == url: QMessageBox.information(self, "提示", "已收藏"); return
        name, ok = QInputDialog.getText(self, "添加到收藏栏", "名称：", text=title[:20])
        if not ok: return
        name = name.strip() or title[:20]
        self.bookmarks.append({"title": name, "url": url})
        save_bookmarks(self.bookmarks); self.refresh_bookmark_buttons()
        self.statusBarMessage(f"⭐ 已收藏：{name}")

    def add_bookmark_dialog(self):
        title, ok1 = QInputDialog.getText(self, "添加书签", "名称：")
        if not ok1 or not title.strip(): return
        url, ok2 = QInputDialog.getText(self, "添加书签", "网址：", text="https://")
        if not ok2 or not url.strip(): return
        self.bookmarks.append({"title": title.strip(), "url": url.strip()})
        save_bookmarks(self.bookmarks); self.refresh_bookmark_buttons()

    def remove_bookmark(self, bookmark):
        if QMessageBox.question(self, "确认", f"删除书签『{bookmark.get('title')}』？") != QMessageBox.StandardButton.Yes: return
        try: self.bookmarks.remove(bookmark)
        except ValueError: pass
        save_bookmarks(self.bookmarks); self.refresh_bookmark_buttons()

    def save_bookmarks_now(self): save_bookmarks(self.bookmarks)

    def show_bookmark_menu(self):
        menu = QMenu(self)
        a1 = menu.addAction("添加当前页面"); a1.triggered.connect(self.add_current_bookmark)
        a2 = menu.addAction("手动添加..."); a2.triggered.connect(self.add_bookmark_dialog)
        menu.addSeparator()
        a3 = menu.addAction("打开书签管理..."); a3.triggered.connect(self.open_bookmark_manager)
        menu.addSeparator()
        a4 = menu.addAction("恢复默认书签"); a4.triggered.connect(self.reset_bookmarks)
        menu.addSeparator()
        a5 = menu.addAction("导出书签（JSON）"); a5.triggered.connect(self.export_bookmarks)
        a6 = menu.addAction("导入书签（JSON）"); a6.triggered.connect(self.import_bookmarks)
        menu.exec(self.btn_manage_bm.mapToGlobal(self.btn_manage_bm.rect().bottomLeft()))

    def open_bookmark_manager(self):
        dlg = BookmarkManagerDialog(self.bookmarks, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.bookmarks = dlg.get_bookmarks()
            save_bookmarks(self.bookmarks); self.refresh_bookmark_buttons()

    def reset_bookmarks(self):
        if QMessageBox.question(self, "确认", "恢复为默认书签？") != QMessageBox.StandardButton.Yes: return
        self.bookmarks = list(DEFAULT_BOOKMARKS)
        save_bookmarks(self.bookmarks); self.refresh_bookmark_buttons()

    def export_bookmarks(self):
        if not self.bookmarks: QMessageBox.information(self, "提示", "书签为空"); return
        path, _ = QFileDialog.getSaveFileName(self, "导出书签",
            str(Path.home() / "browser_bookmarks.json"), "JSON (*.json)")
        if not path: return
        try:
            Path(path).write_text(json.dumps(self.bookmarks, ensure_ascii=False, indent=2), encoding="utf-8")
            QMessageBox.information(self, "成功", f"已导出到：\n{path}")
        except Exception as e: QMessageBox.warning(self, "失败", str(e))

    def import_bookmarks(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入书签", "", "JSON (*.json)")
        if not path: return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, list): raise ValueError("格式错误")
            for item in data:
                if isinstance(item, dict) and "url" in item:
                    self.bookmarks.append({"title": item.get("title", item["url"]), "url": item["url"]})
            save_bookmarks(self.bookmarks); self.refresh_bookmark_buttons()
            QMessageBox.information(self, "成功", f"已导入 {len(data)} 条")
        except Exception as e: QMessageBox.warning(self, "失败", str(e))

    def statusBarMessage(self, msg):
        w = self.window()
        if hasattr(w, "statusBar"): w.statusBar().showMessage(msg)

    def lazy_load_home(self):
        self._ensure_initialized()
        if self._home_loaded or not self.view: return
        self._home_loaded = True
        QTimer.singleShot(200, self.go_home)

    def go_home(self):
        if not self.view: return
        self.view.setUrl(QUrl(self.settings.get("browser_home", "https://www.bing.com")))

    def navigate_to_input(self):
        if not self.view: return
        text = self.url_edit.text().strip()
        if not text: return
        if not text.startswith(("http://", "https://", "file://")):
            if "." in text and " " not in text: text = "https://" + text
            else: text = "https://www.bing.com/search?q=" + text
        self.view.setUrl(QUrl(text))

    def _on_url_changed(self, qurl):
        if self.url_edit and not self.url_edit.hasFocus():
            self.url_edit.setText(qurl.toString())

    def _play_sniffed(self, item):
        url = item.data(Qt.ItemDataRole.UserRole)
        if url: self.stream_found.emit(url)

    def play_current_url_with_vlc(self):
        if not self.view: return
        url = self.view.url().toString()
        if url: self.stream_found.emit(url)

    def clear_sniffed(self):
        self._sniff_items.clear()
        if self.sniff_list: self.sniff_list.clear()
        if self.sniffer and hasattr(self.sniffer, "_seen"): self.sniffer._seen.clear()


class BookmarkManagerDialog(QDialog):
    def __init__(self, bookmarks, parent=None):
        super().__init__(parent)
        self.setWindowTitle("书签管理"); self.resize(700, 480)
        self.bookmarks = [dict(b) for b in bookmarks]
        layout = QVBoxLayout(self)
        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)
        self._reload_list()
        btn_row = QHBoxLayout()
        b = QPushButton("上移"); b.clicked.connect(lambda: self._move(-1)); btn_row.addWidget(b)
        b = QPushButton("下移"); b.clicked.connect(lambda: self._move(1)); btn_row.addWidget(b)
        btn_row.addStretch()
        b = QPushButton("添加"); b.clicked.connect(self._add); btn_row.addWidget(b)
        b = QPushButton("编辑"); b.clicked.connect(self._edit); btn_row.addWidget(b)
        b = QPushButton("删除"); b.clicked.connect(self._del); btn_row.addWidget(b)
        layout.addLayout(btn_row)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept); btns.rejected.connect(self.reject)
        layout.addWidget(btns)
    def _reload_list(self):
        self.list_widget.clear()
        for bm in self.bookmarks:
            item = QListWidgetItem(f"{bm.get('title','')}   ——   {bm.get('url','')}")
            item.setData(Qt.ItemDataRole.UserRole, bm); self.list_widget.addItem(item)
    def _move(self, delta):
        i = self.list_widget.currentRow()
        if i < 0: return
        j = i + delta
        if j < 0 or j >= len(self.bookmarks): return
        self.bookmarks[i], self.bookmarks[j] = self.bookmarks[j], self.bookmarks[i]
        self._reload_list(); self.list_widget.setCurrentRow(j)
    def _add(self):
        title, ok1 = QInputDialog.getText(self, "添加", "名称：")
        if not ok1 or not title.strip(): return
        url, ok2 = QInputDialog.getText(self, "添加", "网址：", text="https://")
        if not ok2 or not url.strip(): return
        self.bookmarks.append({"title": title.strip(), "url": url.strip()}); self._reload_list()
    def _edit(self):
        i = self.list_widget.currentRow()
        if i < 0: return
        bm = self.bookmarks[i]
        title, ok1 = QInputDialog.getText(self, "编辑", "名称：", text=bm.get("title", ""))
        if not ok1: return
        url, ok2 = QInputDialog.getText(self, "编辑", "网址：", text=bm.get("url", ""))
        if not ok2: return
        bm["title"] = title.strip() or url.strip(); bm["url"] = url.strip(); self._reload_list()
    def _del(self):
        i = self.list_widget.currentRow()
        if i < 0: return
        if QMessageBox.question(self, "确认", "删除？") != QMessageBox.StandardButton.Yes: return
        del self.bookmarks[i]; self._reload_list()
    def get_bookmarks(self): return self.bookmarks


class RecordingManager:
    def __init__(self):
        self.process = None; self.output_file = None
        self.log_file = None; self.start_time = None; self.source = None
    def is_recording(self): return self.process is not None and self.process.poll() is None
    def elapsed_seconds(self):
        if self.start_time: return int((datetime.now() - self.start_time).total_seconds())
        return 0
    def start(self, source, output_file, proxy=None, headers=None):
        if self.is_recording(): return False, "已有录制进行中"
        if not FFMPEG_PATH: return False, "未找到 ffmpeg"
        cmd = [FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "warning"]
        if proxy and source.startswith(("http://", "https://")): cmd += ["-http_proxy", proxy]
        if headers:
            header_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            cmd += ["-headers", header_str]
        cmd += ["-i", source, "-c", "copy", output_file]
        log_path = output_file + ".ffmpeg.log"
        try: self.log_file = open(log_path, "w", encoding="utf-8", errors="ignore")
        except Exception: self.log_file = None
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                stdout=self.log_file if self.log_file else subprocess.DEVNULL,
                stderr=subprocess.STDOUT, creationflags=creation_flags)
            self.output_file = output_file; self.start_time = datetime.now(); self.source = source
            return True, output_file
        except Exception as e:
            self.process = None
            if self.log_file:
                try: self.log_file.close()
                except Exception: pass
                self.log_file = None
            return False, str(e)
    def stop(self, timeout=6):
        if not self.process: return False, "没有正在进行的录制"
        proc = self.process; out = self.output_file; log_file = self.log_file
        self.process = None; self.output_file = None; self.log_file = None
        self.start_time = None; self.source = None
        stopped_clean = False
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.write(b"q"); proc.stdin.flush()
                try: proc.stdin.close()
                except Exception: pass
        except Exception: pass
        try: proc.wait(timeout=timeout); stopped_clean = True
        except subprocess.TimeoutExpired:
            try: proc.terminate(); proc.wait(timeout=3); stopped_clean = True
            except subprocess.TimeoutExpired:
                try: proc.kill(); proc.wait(timeout=2)
                except Exception: pass
        try:
            if log_file: log_file.close()
        except Exception: pass
        size = 0
        if out and os.path.exists(out): size = os.path.getsize(out)
        status = "优雅停止" if stopped_clean else "强制停止"
        return True, f"{out}\n\n（{status}，文件大小 {size/1024/1024:.2f} MB）"


class LogViewerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("运行日志"); self.resize(900, 600)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        b = QPushButton("刷新"); b.clicked.connect(self.reload); top.addWidget(b)
        b = QPushButton("打开日志目录"); b.clicked.connect(self.open_log_folder); top.addWidget(b)
        b = QPushButton("清空日志文件"); b.clicked.connect(self.clear_log_file); top.addWidget(b)
        top.addWidget(QLabel("显示最大行数："))
        self.spin_lines = QSpinBox(); self.spin_lines.setRange(100, 100000)
        self.spin_lines.setValue(3000); self.spin_lines.valueChanged.connect(self.reload)
        top.addWidget(self.spin_lines)
        self.chk_auto = QCheckBox("自动刷新"); self.chk_auto.setChecked(True); top.addWidget(self.chk_auto)
        top.addStretch(); layout.addLayout(top)
        self.text = QPlainTextEdit(); self.text.setReadOnly(True)
        f = QFont("Consolas"); f.setStyleHint(QFont.StyleHint.Monospace); f.setPointSize(9)
        self.text.setFont(f); layout.addWidget(self.text)
        bottom = QHBoxLayout(); bottom.addStretch()
        b = QPushButton("关闭"); b.clicked.connect(self.close); bottom.addWidget(b)
        layout.addLayout(bottom)
        self.auto_timer = QTimer(self); self.auto_timer.setInterval(2000)
        self.auto_timer.timeout.connect(self._auto); self.auto_timer.start()
        self.reload()
    def _auto(self):
        if self.chk_auto.isChecked() and self.isVisible(): self.reload()
    def reload(self):
        try:
            if not LOG_FILE.exists(): self.text.setPlainText("（暂无日志）"); return
            n = self.spin_lines.value()
            with LOG_FILE.open("r", encoding="utf-8", errors="ignore") as f: lines = f.readlines()
            if len(lines) > n: lines = lines[-n:]; prefix = f"（仅显示最后 {n} 行）\n\n"
            else: prefix = ""
            self.text.setPlainText(prefix + "".join(lines))
            sb = self.text.verticalScrollBar(); sb.setValue(sb.maximum())
        except Exception as e: self.text.setPlainText(f"读取日志失败：{e}")
    def open_log_folder(self):
        try:
            if platform.system() == "Windows": os.startfile(str(LOG_DIR))
            elif platform.system() == "Darwin": os.system(f'open "{LOG_DIR}"')
            else: os.system(f'xdg-open "{LOG_DIR}"')
        except Exception as e: QMessageBox.warning(self, "错误", str(e))
    def clear_log_file(self):
        if QMessageBox.question(self, "确认", "清空日志？") != QMessageBox.StandardButton.Yes: return
        try:
            for h in logger.handlers:
                if isinstance(h, RotatingFileHandler): h.close()
            LOG_FILE.write_text("", encoding="utf-8")
            for h in list(logger.handlers):
                if isinstance(h, RotatingFileHandler): logger.removeHandler(h)
            fh = RotatingFileHandler(LOG_FILE, maxBytes=2*1024*1024, backupCount=5, encoding="utf-8")
            fmt = logging.Formatter("[%(asctime)s] [%(levelname)-7s] [%(threadName)s] %(message)s",
                                    datefmt="%Y-%m-%d %H:%M:%S")
            fh.setFormatter(fmt); fh.setLevel(logging.DEBUG); logger.addHandler(fh); self.reload()
        except Exception as e: QMessageBox.warning(self, "错误", str(e))


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置"); self.setMinimumWidth(580)
        self.settings = settings.copy()
        form = QFormLayout(self)
        self.chk_proxy = QCheckBox("启用代理（yt-dlp + VLC + 浏览器）")
        self.chk_proxy.setChecked(self.settings.get("proxy_enabled", False)); form.addRow(self.chk_proxy)
        self.edit_proxy = QLineEdit(self.settings.get("proxy_url", ""))
        self.edit_proxy.setPlaceholderText("http://127.0.0.1:7890"); form.addRow("代理地址", self.edit_proxy)
        self.chk_fallback = QCheckBox("代理不可用时自动回退直连")
        self.chk_fallback.setChecked(self.settings.get("proxy_fallback", True)); form.addRow(self.chk_fallback)
        b = QPushButton("测试代理连通性"); b.clicked.connect(self.test_proxy_now); form.addRow(b)

        self.chk_dl = QCheckBox("YouTube 下载合并模式（推荐）")
        self.chk_dl.setChecked(self.settings.get("youtube_download_mode", True)); form.addRow(self.chk_dl)
        self.combo_height = QComboBox(); self.combo_height.addItems(["360", "480", "720", "1080", "1440", "2160"])
        self.combo_height.setCurrentText(str(self.settings.get("youtube_max_height", 1080)))
        form.addRow("下载最大分辨率", self.combo_height)

        self.edit_record_dir = QLineEdit(self.settings.get("record_dir",
            str(Path.home() / "Videos" / "UniversalPlayerRecordings")))
        dir_row = QWidget(); dl = QHBoxLayout(dir_row); dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(self.edit_record_dir, stretch=1)
        b1 = QPushButton("浏览..."); b1.clicked.connect(self.browse_record_dir); dl.addWidget(b1)
        b2 = QPushButton("打开"); b2.clicked.connect(self.open_record_dir); dl.addWidget(b2)
        form.addRow("录制保存目录", dir_row)
        self.chk_ask_path = QCheckBox("每次录制前询问保存位置")
        self.chk_ask_path.setChecked(self.settings.get("record_ask_path", False)); form.addRow(self.chk_ask_path)

        self.edit_download_dir = QLineEdit(self.settings.get("download_dir",
            str(Path.home() / "Downloads" / "UniversalPlayer")))
        dir_row2 = QWidget(); dl2 = QHBoxLayout(dir_row2); dl2.setContentsMargins(0, 0, 0, 0)
        dl2.addWidget(self.edit_download_dir, stretch=1)
        b3 = QPushButton("浏览..."); b3.clicked.connect(self.browse_download_dir); dl2.addWidget(b3)
        b4 = QPushButton("打开"); b4.clicked.connect(self.open_download_dir); dl2.addWidget(b4)
        form.addRow("快速下载保存目录", dir_row2)

        # 默认分段数
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 64)
        self.spin_threads.setValue(int(self.settings.get("download_threads", 16)))
        form.addRow("默认并发分段数", self.spin_threads)

        # ★ 是否每次下载询问
        self.chk_ask_threads = QCheckBox("下载前询问并发分段数（推荐）")
        self.chk_ask_threads.setChecked(self.settings.get("download_ask_threads", True))
        form.addRow(self.chk_ask_threads)

        info = QLabel("ℹ️ 勾选后在点下载时会弹窗让你自选分段数；取消勾选则直接用上面的默认值")
        info.setStyleSheet("color:#888;font-size:11px;")
        info.setWordWrap(True)
        form.addRow(info)

        eng_info = QLabel(f"当前下载引擎：{'aria2c（推荐）' if ARIA2C_PATH else 'yt-dlp 内建'}")
        eng_info.setStyleSheet("color:#3a9;font-size:11px;")
        form.addRow(eng_info)

        self.edit_browser_home = QLineEdit(self.settings.get("browser_home", "https://www.bing.com"))
        form.addRow("浏览器主页", self.edit_browser_home)
        self.edit_browser_ua = QLineEdit(self.settings.get("browser_user_agent", ""))
        form.addRow("浏览器 User-Agent", self.edit_browser_ua)
        self.chk_disable_gpu = QCheckBox("禁用浏览器 GPU 加速")
        self.chk_disable_gpu.setChecked(self.settings.get("browser_disable_gpu", False)); form.addRow(self.chk_disable_gpu)
        self.chk_show_bm = QCheckBox("显示收藏栏")
        self.chk_show_bm.setChecked(self.settings.get("browser_show_bookmarks", True)); form.addRow(self.chk_show_bm)
        self.chk_manual_nav = QCheckBox("点击网页链接时手动确认跳转")
        self.chk_manual_nav.setChecked(self.settings.get("browser_manual_navigation", False))
        form.addRow(self.chk_manual_nav)
        self.combo_sniff = QComboBox()
        self.combo_sniff.addItems(["只显示 m3u8（推荐）", "显示所有媒体流"])
        mode = self.settings.get("browser_sniff_mode", "m3u8")
        self.combo_sniff.setCurrentIndex(0 if mode == "m3u8" else 1)
        form.addRow("嗅探栏内容", self.combo_sniff)
        self.chk_close_tray = QCheckBox("关闭时最小化到托盘")
        self.chk_close_tray.setChecked(self.settings.get("close_to_tray", True)); form.addRow(self.chk_close_tray)
        self.chk_min_tray = QCheckBox("最小化时隐藏到托盘")
        self.chk_min_tray.setChecked(self.settings.get("minimize_to_tray", False)); form.addRow(self.chk_min_tray)
        self.combo_log = QComboBox(); self.combo_log.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.combo_log.setCurrentText(self.settings.get("log_level", "INFO")); form.addRow("控制台日志级别", self.combo_log)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept); btns.rejected.connect(self.reject); form.addRow(btns)
    def browse_record_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择录制目录", self.edit_record_dir.text() or str(Path.home()))
        if d: self.edit_record_dir.setText(d)
    def open_record_dir(self):
        d = self.edit_record_dir.text().strip()
        if not d: return
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
            if platform.system() == "Windows": os.startfile(d)
            elif platform.system() == "Darwin": os.system(f'open "{d}"')
            else: os.system(f'xdg-open "{d}"')
        except Exception as e: QMessageBox.warning(self, "错误", str(e))
    def browse_download_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择下载目录", self.edit_download_dir.text() or str(Path.home()))
        if d: self.edit_download_dir.setText(d)
    def open_download_dir(self):
        d = self.edit_download_dir.text().strip()
        if not d: return
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
            if platform.system() == "Windows": os.startfile(d)
            elif platform.system() == "Darwin": os.system(f'open "{d}"')
            else: os.system(f'xdg-open "{d}"')
        except Exception as e: QMessageBox.warning(self, "错误", str(e))
    def test_proxy_now(self):
        url = self.edit_proxy.text().strip()
        if not url: QMessageBox.information(self, "提示", "请填写代理地址"); return
        if test_proxy(url): QMessageBox.information(self, "测试结果", f"✅ 代理可用：{url}")
        else: QMessageBox.warning(self, "测试结果", f"❌ 代理不可用：{url}")
    def get_settings(self):
        self.settings["proxy_enabled"] = self.chk_proxy.isChecked()
        self.settings["proxy_url"] = self.edit_proxy.text().strip()
        self.settings["proxy_fallback"] = self.chk_fallback.isChecked()
        self.settings["youtube_download_mode"] = self.chk_dl.isChecked()
        self.settings["youtube_max_height"] = int(self.combo_height.currentText())
        self.settings["record_dir"] = (self.edit_record_dir.text().strip() or
            str(Path.home() / "Videos" / "UniversalPlayerRecordings"))
        self.settings["record_ask_path"] = self.chk_ask_path.isChecked()
        self.settings["download_dir"] = (self.edit_download_dir.text().strip() or
            str(Path.home() / "Downloads" / "UniversalPlayer"))
        self.settings["download_threads"] = int(self.spin_threads.value())
        self.settings["download_ask_threads"] = self.chk_ask_threads.isChecked()
        self.settings["browser_home"] = self.edit_browser_home.text().strip() or "https://www.bing.com"
        self.settings["browser_user_agent"] = self.edit_browser_ua.text().strip()
        self.settings["browser_disable_gpu"] = self.chk_disable_gpu.isChecked()
        self.settings["browser_show_bookmarks"] = self.chk_show_bm.isChecked()
        self.settings["browser_manual_navigation"] = self.chk_manual_nav.isChecked()
        self.settings["browser_sniff_mode"] = "m3u8" if self.combo_sniff.currentIndex() == 0 else "all"
        self.settings["close_to_tray"] = self.chk_close_tray.isChecked()
        self.settings["minimize_to_tray"] = self.chk_min_tray.isChecked()
        self.settings["log_level"] = self.combo_log.currentText()
        return self.settings


class VideoFrame(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.double_click_callback = None
        self.drag_move_callback = None
        self.context_menu_callback = None
        self._drag_pos = None
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.setMouseTracking(True)
    def mouseDoubleClickEvent(self, event):
        if self.double_click_callback: self.double_click_callback()
        super().mouseDoubleClickEvent(event)
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.drag_move_callback:
            self._drag_pos = event.globalPosition().toPoint(); event.accept()
        else: super().mousePressEvent(event)
    def mouseMoveEvent(self, event):
        if self._drag_pos and (event.buttons() & Qt.MouseButton.LeftButton) and self.drag_move_callback:
            delta = event.globalPosition().toPoint() - self._drag_pos
            self.drag_move_callback(delta)
            self._drag_pos = event.globalPosition().toPoint(); event.accept()
        else: super().mouseMoveEvent(event)
    def mouseReleaseEvent(self, event):
        self._drag_pos = None; super().mouseReleaseEvent(event)
    def contextMenuEvent(self, event):
        if self.context_menu_callback:
            self.context_menu_callback(event.globalPos()); event.accept()
        else: super().contextMenuEvent(event)


class SeekSlider(QSlider):
    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setMouseTracking(True); self.duration_ms = 0
        self._preview = QLabel(self)
        self._preview.setStyleSheet("background:#222;color:#fff;padding:2px 6px;border-radius:4px;font-size:11px;")
        self._preview.hide()
        self._dragging = False
        self.seek_callback = None; self.drag_live_callback = None
        self.context_menu_callback = None
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
    def set_duration(self, ms): self.duration_ms = max(0, ms)
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.maximum() > 0:
            self._dragging = True
            v = self._val(event.position().x()); self.setValue(v)
            if self.drag_live_callback: self.drag_live_callback(v)
            self._show_preview(event.position().x(), v)
        super().mousePressEvent(event)
    def mouseMoveEvent(self, event):
        if self.maximum() > 0:
            v = self._val(event.position().x())
            if self._dragging:
                self.setValue(v)
                if self.drag_live_callback: self.drag_live_callback(v)
            self._show_preview(event.position().x(), v)
        super().mouseMoveEvent(event)
    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            if self.seek_callback: self.seek_callback(self.value())
        self._preview.hide(); super().mouseReleaseEvent(event)
    def contextMenuEvent(self, event):
        if self.context_menu_callback:
            self.context_menu_callback(event.globalPos()); event.accept()
        else: super().contextMenuEvent(event)
    def leaveEvent(self, event):
        self._preview.hide(); super().leaveEvent(event)
    def _val(self, x):
        if self.maximum() <= 0: return 0
        return int(max(0.0, min(1.0, x / max(1, self.width()))) * self.maximum())
    def _show_preview(self, x, v):
        self._preview.setText(self._fmt(v)); self._preview.adjustSize()
        px = int(x - self._preview.width() / 2)
        px = max(0, min(self.width() - self._preview.width(), px))
        self._preview.move(px, -self._preview.height() - 4); self._preview.show()
    @staticmethod
    def _fmt(ms):
        if ms < 0: ms = 0
        s = ms // 1000; m, s = divmod(s, 60); h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


class RightClickWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.context_menu_callback = None
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
    def contextMenuEvent(self, event):
        if self.context_menu_callback:
            self.context_menu_callback(event.globalPos()); event.accept()
        else: super().contextMenuEvent(event)


class WorkerSignals(QObject):
    resolved = Signal(str, str, dict)
    failed = Signal(str)
    status = Signal(str)


class UniversalPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        logger.info("创建新播放器窗口")
        self.setWindowTitle("万能播放器")
        self.resize(1360, 860)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.settings = load_settings()
        apply_log_level(self.settings.get("log_level", "INFO"))

        self.instance = get_vlc_instance()
        self.player = self.instance.media_player_new()
        self.player.audio_set_volume(80)

        self.current_target = None; self.current_title = None
        self.current_rate = 1.0
        self.pip_mode = False
        self._normal_geometry = None; self._normal_flags = None
        self._side_panel_was_visible = True
        self._vlc_bound = False; self._really_quit = False
        self._audio_tracks_loaded = False; self._track_retry = 0

        self.recording_manager = RecordingManager()
        self.record_timer = QTimer(self)
        self.record_timer.setInterval(1000)
        self.record_timer.timeout.connect(self.update_record_ui)
        self._current_stream_headers = {}

        self.download_manager = DownloadManager(threads=self.settings.get("download_threads", 16))
        self.download_timer = QTimer(self)
        self.download_timer.setInterval(1000)
        self.download_timer.timeout.connect(self.update_download_ui)

        self.timer = QTimer(self); self.timer.setInterval(500)
        self.timer.timeout.connect(self.update_ui)

        self.signals = WorkerSignals()
        self.signals.resolved.connect(self.on_url_resolved)
        self.signals.failed.connect(self.on_url_failed)
        self.signals.status.connect(self.on_status_message)

        self.playlist_data = load_json(PLAYLIST_FILE, [])
        self.favorites_data = load_json(FAVORITES_FILE, [])

        self.init_ui(); self.init_tray()
        self.load_playlist_to_ui(); self.load_favorites_to_ui()

    def init_ui(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("文件")
        a = QAction("打开文件...", self); a.setShortcut("Ctrl+O"); a.triggered.connect(self.open_file); file_menu.addAction(a)
        a = QAction("打开网络链接...", self); a.setShortcut("Ctrl+U"); a.triggered.connect(self.prompt_open_url); file_menu.addAction(a)
        file_menu.addSeparator()
        a = QAction("新建播放器窗口", self); a.setShortcut("Ctrl+N"); a.triggered.connect(self.open_new_window); file_menu.addAction(a)
        file_menu.addSeparator()
        a = QAction("退出", self); a.setShortcut("Ctrl+Q"); a.triggered.connect(self.quit_app); file_menu.addAction(a)

        view_menu = menubar.addMenu("视图")
        a = QAction("全屏", self); a.setShortcut("F"); a.triggered.connect(self.toggle_fullscreen); view_menu.addAction(a)
        a = QAction("画中画", self); a.setShortcut("Ctrl+P"); a.triggered.connect(self.toggle_pip); view_menu.addAction(a)
        a = QAction("显示/隐藏播放列表", self); a.setShortcut("Ctrl+L"); a.triggered.connect(self.toggle_playlist_panel); view_menu.addAction(a)
        a = QAction("视频/网页 切换", self); a.setShortcut("Ctrl+T"); a.triggered.connect(self.toggle_video_web); view_menu.addAction(a)
        a = QAction("查看运行日志...", self); a.setShortcut("Ctrl+G"); a.triggered.connect(self.open_log_viewer); view_menu.addAction(a)

        rec_menu = menubar.addMenu("录制")
        a = QAction("开始/停止录制", self); a.setShortcut("Ctrl+R"); a.triggered.connect(self.toggle_recording); rec_menu.addAction(a)
        a = QAction("截图", self); a.setShortcut("Ctrl+Shift+S"); a.triggered.connect(self.take_snapshot); rec_menu.addAction(a)
        a = QAction("选择录制目录...", self); a.setShortcut("Ctrl+Shift+R"); a.triggered.connect(self.choose_record_dir); rec_menu.addAction(a)
        a = QAction("打开录制目录", self); a.triggered.connect(self.open_record_folder); rec_menu.addAction(a)

        dl_menu = menubar.addMenu("下载")
        a = QAction("⚡ 下载（自选分段）", self)
        a.setShortcut("Ctrl+Shift+D"); a.triggered.connect(self.quick_download_current); dl_menu.addAction(a)
        a = QAction("⚡ 直接下载（默认分段）", self)
        a.setShortcut("Ctrl+Alt+Shift+D"); a.triggered.connect(self.quick_download_current_noask); dl_menu.addAction(a)
        a = QAction("⬇ 另存为...", self); a.triggered.connect(self.download_current_stream_saveas); dl_menu.addAction(a)
        a = QAction("从 URL 下载...", self); a.triggered.connect(self.download_from_url_dialog); dl_menu.addAction(a)
        dl_menu.addSeparator()
        a = QAction("停止当前下载", self); a.triggered.connect(self.stop_download); dl_menu.addAction(a)
        a = QAction("选择下载目录...", self); a.triggered.connect(self.choose_download_dir); dl_menu.addAction(a)
        a = QAction("打开下载目录", self); a.triggered.connect(self.open_download_folder); dl_menu.addAction(a)

        tool_menu = menubar.addMenu("工具")
        a = QAction("注册右键菜单（资源管理器）", self); a.triggered.connect(self.do_register_context_menu); tool_menu.addAction(a)
        a = QAction("取消注册右键菜单", self); a.triggered.connect(self.do_unregister_context_menu); tool_menu.addAction(a)
        a = QAction("检查右键菜单状态", self); a.triggered.connect(self.check_context_menu_status); tool_menu.addAction(a)
        tool_menu.addSeparator()
        a = QAction("打开书签管理...", self); a.triggered.connect(self.open_bookmark_manager_from_menu); tool_menu.addAction(a)

        set_menu = menubar.addMenu("设置")
        a = QAction("代理和后台设置...", self); a.setShortcut("Ctrl+,"); a.triggered.connect(self.open_settings_dialog); set_menu.addAction(a)

        help_menu = menubar.addMenu("帮助")
        a = QAction("关于", self); a.triggered.connect(self.show_about); help_menu.addAction(a)

        central = QWidget(); self.setCentralWidget(central)
        outer = QVBoxLayout(central); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)

        self.url_bar = QWidget()
        ul = QHBoxLayout(self.url_bar); ul.setContentsMargins(6, 6, 6, 0); ul.setSpacing(6)
        ul.addWidget(QLabel("链接"))
        self.url_combo = QComboBox(); self.url_combo.setEditable(True)
        self.url_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.url_combo.lineEdit().setPlaceholderText("粘贴 m3u8 / mp4 / rtsp / YouTube 链接，回车播放")
        self.url_combo.lineEdit().returnPressed.connect(self.play_from_url_bar)
        ul.addWidget(self.url_combo, stretch=1)
        self.btn_play_url = QPushButton("播放链接"); self.btn_play_url.clicked.connect(self.play_from_url_bar); ul.addWidget(self.btn_play_url)
        self.btn_download_url = QPushButton("⚡ 下载")
        self.btn_download_url.setToolTip("下载链接栏里的流（弹窗自选分段）")
        self.btn_download_url.clicked.connect(self.download_from_url_bar)
        ul.addWidget(self.btn_download_url)
        self.btn_add_url_to_playlist = QPushButton("加入列表"); self.btn_add_url_to_playlist.clicked.connect(self.add_url_to_playlist); ul.addWidget(self.btn_add_url_to_playlist)
        outer.addWidget(self.url_bar)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(self.splitter, stretch=1)

        self.left_tabs = QTabWidget()
        self.left_tabs.currentChanged.connect(self.on_left_tab_changed)

        video_tab = RightClickWidget()
        video_tab.context_menu_callback = lambda gp: self.show_video_context_menu(gp)
        vl = QVBoxLayout(video_tab); vl.setContentsMargins(0, 0, 0, 0); vl.setSpacing(0)

        self.video_frame = VideoFrame()
        self.video_frame.setStyleSheet("background-color: black;")
        self.video_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_frame.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.video_frame.double_click_callback = self.toggle_fullscreen
        self.video_frame.drag_move_callback = self.on_video_drag
        self.video_frame.context_menu_callback = self.show_video_context_menu
        vl.addWidget(self.video_frame, stretch=1)

        self.progress_bar = RightClickWidget()
        self.progress_bar.context_menu_callback = lambda gp: self.show_video_context_menu(gp)
        pl = QHBoxLayout(self.progress_bar)
        pl.setContentsMargins(6, 6, 6, 0); pl.setSpacing(6)
        self.progress = SeekSlider(Qt.Orientation.Horizontal)
        self.progress.setRange(0, 0); self.progress.setMinimumHeight(22)
        self.progress.seek_callback = self.on_seek_finished
        self.progress.drag_live_callback = self.on_seek_live
        self.progress.context_menu_callback = self.show_video_context_menu
        pl.addWidget(self.progress, stretch=1)
        self.time_label = QLabel("00:00 / 00:00"); pl.addWidget(self.time_label)
        vl.addWidget(self.progress_bar)

        self.control_bar = RightClickWidget()
        self.control_bar.context_menu_callback = lambda gp: self.show_video_context_menu(gp)
        cl = QHBoxLayout(self.control_bar); cl.setContentsMargins(6, 2, 6, 4); cl.setSpacing(6)
        self.btn_open = QPushButton("打开"); self.btn_open.clicked.connect(self.open_file); cl.addWidget(self.btn_open)
        self.btn_play = QPushButton("播放"); self.btn_play.clicked.connect(self.toggle_play); cl.addWidget(self.btn_play)
        self.btn_stop = QPushButton("停止"); self.btn_stop.clicked.connect(self.stop); cl.addWidget(self.btn_stop)

        self.btn_record = QPushButton("● 录制")
        self.btn_record.setStyleSheet("color: #d00; font-weight: bold;")
        self.btn_record.clicked.connect(self.toggle_recording)
        self.btn_record.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.btn_record.customContextMenuRequested.connect(self.on_record_btn_context_menu)
        cl.addWidget(self.btn_record)

        self.record_label = QLabel("")
        self.record_label.setStyleSheet("color: #d00; font-weight: bold;")
        cl.addWidget(self.record_label)

        self.btn_download = QPushButton("⚡ 下载")
        self.btn_download.setToolTip("下载当前播放的流（弹窗自选分段）")
        self.btn_download.clicked.connect(self.quick_download_current)
        cl.addWidget(self.btn_download)

        self.download_label = QLabel("")
        self.download_label.setStyleSheet("color: #3a9; font-weight: bold; font-family: Consolas;")
        cl.addWidget(self.download_label)

        self.btn_prev = QPushButton("上一个"); self.btn_prev.clicked.connect(self.play_prev_in_playlist); cl.addWidget(self.btn_prev)
        self.btn_next = QPushButton("下一个"); self.btn_next.clicked.connect(self.play_next_in_playlist); cl.addWidget(self.btn_next)

        cl.addWidget(QLabel("倍速"))
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["0.25x", "0.5x", "0.75x", "1.0x", "1.25x", "1.5x", "2.0x", "3.0x", "4.0x"])
        self.speed_combo.setCurrentText("1.0x"); self.speed_combo.setFixedWidth(70)
        self.speed_combo.currentTextChanged.connect(self.on_speed_changed); cl.addWidget(self.speed_combo)

        cl.addWidget(QLabel("音轨"))
        self.audio_track_combo = QComboBox(); self.audio_track_combo.setFixedWidth(150)
        self.audio_track_combo.addItem("（未加载）", -1)
        self.audio_track_combo.currentIndexChanged.connect(self.on_audio_track_changed)
        cl.addWidget(self.audio_track_combo)
        self.btn_refresh_tracks = QPushButton("刷新"); self.btn_refresh_tracks.clicked.connect(self.refresh_audio_tracks); cl.addWidget(self.btn_refresh_tracks)

        cl.addWidget(QLabel("音量"))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100); self.volume_slider.setValue(80); self.volume_slider.setFixedWidth(90)
        self.volume_slider.valueChanged.connect(self.player.audio_set_volume)
        cl.addWidget(self.volume_slider)

        self.btn_pip = QPushButton("画中画"); self.btn_pip.clicked.connect(self.toggle_pip); cl.addWidget(self.btn_pip)
        self.btn_fullscreen = QPushButton("全屏"); self.btn_fullscreen.clicked.connect(self.toggle_fullscreen); cl.addWidget(self.btn_fullscreen)
        self.btn_menu = QPushButton("☰")
        self.btn_menu.setToolTip("菜单（和视频画面右键一样）")
        self.btn_menu.setFixedWidth(32)
        self.btn_menu.clicked.connect(self._on_menu_button_clicked)
        cl.addWidget(self.btn_menu)
        vl.addWidget(self.control_bar)

        self.left_tabs.addTab(video_tab, "📺 视频")

        self.browser_tab = BrowserTab(self.settings)
        self.browser_tab.stream_found.connect(self.on_stream_from_browser)
        self.browser_tab.stream_download.connect(self.quick_download_stream)
        self.browser_tab.stream_download_quick.connect(lambda u: self.quick_download_stream(u, ask=False))
        self.browser_tab.stream_download_saveas.connect(self.saveas_download_stream)
        self.left_tabs.addTab(self.browser_tab, "🌐 网页")

        self.splitter.addWidget(self.left_tabs)

        self.side_panel = QWidget()
        sl = QVBoxLayout(self.side_panel); sl.setContentsMargins(4, 4, 4, 4); sl.setSpacing(4)
        self.tabs = QTabWidget()
        pl_tab = QWidget(); pll = QVBoxLayout(pl_tab); pll.setContentsMargins(2, 2, 2, 2); pll.setSpacing(4)
        pl_tb = QHBoxLayout()
        b = QPushButton("添加文件"); b.clicked.connect(self.add_files_to_playlist); pl_tb.addWidget(b)
        b = QPushButton("删除"); b.clicked.connect(self.remove_selected_from_playlist); pl_tb.addWidget(b)
        b = QPushButton("清空"); b.clicked.connect(self.clear_playlist); pl_tb.addWidget(b)
        pl_tb.addStretch(); pll.addLayout(pl_tb)
        self.playlist_widget = QListWidget()
        self.playlist_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.playlist_widget.itemDoubleClicked.connect(self.on_playlist_double_clicked)
        self.playlist_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.playlist_widget.customContextMenuRequested.connect(self.on_playlist_context_menu)
        pll.addWidget(self.playlist_widget)
        self.tabs.addTab(pl_tab, "播放列表")

        fav_tab = QWidget(); fl = QVBoxLayout(fav_tab); fl.setContentsMargins(2, 2, 2, 2); fl.setSpacing(4)
        ftb = QHBoxLayout()
        b = QPushButton("收藏当前"); b.clicked.connect(self.add_current_to_favorites); ftb.addWidget(b)
        b = QPushButton("删除"); b.clicked.connect(self.remove_selected_favorites); ftb.addWidget(b)
        ftb.addStretch(); fl.addLayout(ftb)
        self.favorites_widget = QListWidget()
        self.favorites_widget.itemDoubleClicked.connect(self.on_favorites_double_clicked)
        self.favorites_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.favorites_widget.customContextMenuRequested.connect(self.on_favorites_context_menu)
        fl.addWidget(self.favorites_widget)
        self.tabs.addTab(fav_tab, "收藏夹")

        sl.addWidget(self.tabs)
        self.side_panel.setMinimumWidth(260)
        self.splitter.addWidget(self.side_panel)
        self.splitter.setStretchFactor(0, 1); self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([1000, 320])
        self.statusBar().showMessage("就绪")

    def _on_menu_button_clicked(self):
        gp = self.btn_menu.mapToGlobal(self.btn_menu.rect().bottomLeft())
        self.show_video_context_menu(gp)

    def open_bookmark_manager_from_menu(self):
        if hasattr(self, "browser_tab"): self.browser_tab.open_bookmark_manager()

    def _build_output_filename(self, url, title=None):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if title and title != url and not title.startswith(("http://", "https://")):
            base = sanitize_filename(title, 60)
            if not base.lower().endswith((".mp4", ".mkv", ".ts", ".webm", ".flv")):
                base += ".mp4"
        else:
            base = suggest_filename_from_url(url)
            if "." not in base:
                base += ".mp4"
        download_dir = Path(self.settings.get("download_dir",
            str(Path.home() / "Downloads" / "UniversalPlayer")))
        download_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(base).stem
        suffix = Path(base).suffix or ".mp4"
        output = download_dir / f"{stem}_{ts}{suffix}"
        return str(output)

    def download_from_url_bar(self):
        url = self.url_combo.currentText().strip()
        if not url:
            QMessageBox.information(self, "提示", "链接栏为空")
            return
        if self.url_combo.findText(url) == -1: self.url_combo.addItem(url)
        self.quick_download_stream(url)

    def quick_download_current(self):
        if self.download_manager.is_downloading():
            QMessageBox.information(self, "提示", "已有下载进行中")
            return
        if not self.current_target:
            QMessageBox.information(self, "提示", "当前没有播放内容")
            return
        self.quick_download_stream(self.current_target, self.current_title)

    def quick_download_current_noask(self):
        if self.download_manager.is_downloading():
            QMessageBox.information(self, "提示", "已有下载进行中")
            return
        if not self.current_target:
            QMessageBox.information(self, "提示", "当前没有播放内容")
            return
        self.quick_download_stream(self.current_target, self.current_title, ask=False)

    def download_current_stream_saveas(self):
        if not self.current_target:
            QMessageBox.information(self, "提示", "当前没有播放内容")
            return
        self.saveas_download_stream(self.current_target, self.current_title)

    def download_from_url_dialog(self):
        text, ok = QInputDialog.getText(self, "从 URL 下载", "请输入流地址（m3u8/mp4 等）：")
        if ok and text.strip():
            self.quick_download_stream(text.strip())

    # ============================================================
    # 下载入口 —— 支持自选分段
    # ============================================================
    def _ask_download_threads(self, url):
        """弹出分段选择对话框，返回 (threads, 是否继续)"""
        if not self.settings.get("download_ask_threads", True):
            return self.settings.get("download_threads", 16), True

        default = self.settings.get("download_threads", 16)
        dlg = DownloadOptionsDialog(default, url, self, mode="quick")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None, False
        threads, remember = dlg.get_values()
        if remember:
            self.settings["download_threads"] = threads
            save_settings(self.settings)
            self.statusBar().showMessage(
                f"已记住分段数 {threads}，以后不再询问（可在设置里改回）")
            logger.info(f"记住下载分段数：{threads}")
        return threads, True

    def quick_download_stream(self, url, title=None, ask=True):
        """ask=True 时弹窗自选分段；ask=False 时直接用默认值"""
        if not url:
            return
        if self.download_manager.is_downloading():
            QMessageBox.information(self, "提示", "已有下载进行中")
            return
        if not HAS_YTDLP:
            QMessageBox.warning(self, "缺少 yt-dlp",
                "下载需要 yt-dlp。\n\n安装：pip install yt-dlp")
            return

        # 询问分段（自选）
        if ask and self.settings.get("download_ask_threads", True):
            threads, proceed = self._ask_download_threads(url)
            if not proceed:
                self.statusBar().showMessage("已取消下载")
                return
        else:
            threads = self.settings.get("download_threads", 16)

        output_file = self._build_output_filename(url, title)
        proxy = self.get_effective_proxy()
        headers = self._current_stream_headers if url == self.current_target else None

        ok, result = self.download_manager.start(url, output_file,
                                                 title=title or Path(output_file).name,
                                                 proxy=proxy, headers=headers,
                                                 threads=threads)
        if ok:
            self.download_timer.start()
            self.btn_download.setText("■ 停止下载")
            self.btn_download.setStyleSheet("color: white; background: #090; font-weight: bold;")
            self.statusBar().showMessage(
                f"⚡ {threads} 线程下载中 → {output_file}"
                + ("  （aria2c 加速）" if ARIA2C_PATH else "")
            )
            logger.info(f"下载：{url[:100]} → {output_file}  线程={threads}")
        else:
            QMessageBox.warning(self, "下载失败", result)

    def saveas_download_stream(self, url, title=None, ask=True):
        if not url:
            return
        if self.download_manager.is_downloading():
            QMessageBox.information(self, "提示", "已有下载进行中")
            return
        if not HAS_YTDLP:
            QMessageBox.warning(self, "缺少 yt-dlp",
                "下载需要 yt-dlp。\n\n安装：pip install yt-dlp")
            return

        # 先选分段
        if ask and self.settings.get("download_ask_threads", True):
            threads, proceed = self._ask_download_threads(url)
            if not proceed:
                self.statusBar().showMessage("已取消下载")
                return
        else:
            threads = self.settings.get("download_threads", 16)

        # 再选保存位置
        default_path = self._build_output_filename(url, title)
        output_file, _ = QFileDialog.getSaveFileName(
            self, "另存为", default_path,
            "MP4 视频 (*.mp4);;MKV 视频 (*.mkv);;MPEG-TS (*.ts);;所有文件 (*.*)")
        if not output_file:
            self.statusBar().showMessage("已取消下载")
            return
        if not Path(output_file).suffix:
            output_file += ".mp4"
        try: Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, "无法创建目录", str(e)); return

        proxy = self.get_effective_proxy()
        headers = self._current_stream_headers if url == self.current_target else None

        ok, result = self.download_manager.start(url, output_file,
                                                 title=title or Path(output_file).name,
                                                 proxy=proxy, headers=headers,
                                                 threads=threads)
        if ok:
            self.download_timer.start()
            self.btn_download.setText("■ 停止下载")
            self.btn_download.setStyleSheet("color: white; background: #090; font-weight: bold;")
            self.statusBar().showMessage(f"⬇ {threads} 线程下载中 → {output_file}")
        else:
            QMessageBox.warning(self, "下载失败", result)

    def stop_download(self):
        if not self.download_manager.is_downloading():
            self.statusBar().showMessage("当前没有下载任务")
            return
        self.statusBar().showMessage("正在停止下载...")
        QApplication.processEvents()
        ok, result = self.download_manager.stop()
        self.download_timer.stop()
        self.download_label.setText("")
        self.btn_download.setText("⚡ 下载")
        self.btn_download.setStyleSheet("")
        if ok:
            self.statusBar().showMessage("✅ 下载已停止")
            QMessageBox.information(self, "下载已停止", f"文件位置：\n\n{result}")
        else:
            QMessageBox.warning(self, "停止失败", result)

    def update_download_ui(self):
        if not self.download_manager.is_downloading():
            self.download_label.setText("")
            if self.btn_download.text() != "⚡ 下载":
                self.btn_download.setText("⚡ 下载")
                self.btn_download.setStyleSheet("")
                self.download_timer.stop()
                final = self.download_manager._final_file
                err = self.download_manager._error
                if err and "用户已停止" not in err:
                    self.statusBar().showMessage(f"❌ 下载失败：{err[:80]}")
                elif final and os.path.exists(final):
                    try:
                        size_mb = os.path.getsize(final) / 1024 / 1024
                        self.statusBar().showMessage(f"✅ 下载完成：{final}  （{size_mb:.2f} MB）")
                    except Exception:
                        self.statusBar().showMessage(f"✅ 下载完成：{final}")
            return
        s = self.download_manager.elapsed_seconds()
        h, rem = divmod(s, 3600); m, sec = divmod(rem, 60)
        tstr = f"{h:02d}:{m:02d}:{sec:02d}" if h > 0 else f"{m:02d}:{sec:02d}"
        status = self.download_manager.get_status_text()
        size_mb = self.download_manager.current_size() / 1024 / 1024
        threads = self.download_manager.threads
        self.download_label.setText(f"⬇ {tstr}  {threads}线程  {status}  {size_mb:.1f}MB")

    def choose_download_dir(self):
        current = self.settings.get("download_dir",
            str(Path.home() / "Downloads" / "UniversalPlayer"))
        d = QFileDialog.getExistingDirectory(self, "选择下载目录", current)
        if d:
            self.settings["download_dir"] = d; save_settings(self.settings)
            self.statusBar().showMessage(f"下载目录已设为：{d}")

    def open_download_folder(self):
        d = Path(self.settings.get("download_dir",
            str(Path.home() / "Downloads" / "UniversalPlayer")))
        try:
            d.mkdir(parents=True, exist_ok=True)
            if platform.system() == "Windows": os.startfile(str(d))
            elif platform.system() == "Darwin": os.system(f'open "{d}"')
            else: os.system(f'xdg-open "{d}"')
        except Exception as e:
            QMessageBox.warning(self, "错误", str(e))

    def show_video_context_menu(self, global_pos):
        menu = QMenu(self)
        if self.player.is_playing():
            a = menu.addAction("⏸ 暂停"); a.triggered.connect(self.toggle_play)
        else:
            a = menu.addAction("▶ 播放"); a.triggered.connect(self.toggle_play)
        a = menu.addAction("⏹ 停止"); a.triggered.connect(self.stop)
        menu.addSeparator()
        a = menu.addAction("⏮ 上一个"); a.triggered.connect(self.play_prev_in_playlist)
        a = menu.addAction("⏭ 下一个"); a.triggered.connect(self.play_next_in_playlist)
        menu.addSeparator()
        speed_menu = menu.addMenu("倍速")
        for spd in ["0.25x", "0.5x", "0.75x", "1.0x", "1.25x", "1.5x", "2.0x", "3.0x", "4.0x"]:
            act = speed_menu.addAction(spd); act.setCheckable(True)
            act.setChecked(spd == f"{self.current_rate}x")
            act.triggered.connect(lambda checked=False, s=spd: self.speed_combo.setCurrentText(s))
        if self.audio_track_combo.count() > 1:
            track_menu = menu.addMenu("音轨")
            for i in range(self.audio_track_combo.count()):
                text = self.audio_track_combo.itemText(i)
                act = track_menu.addAction(text); act.setCheckable(True)
                act.setChecked(i == self.audio_track_combo.currentIndex())
                act.triggered.connect(lambda checked=False, idx=i: self.audio_track_combo.setCurrentIndex(idx))
        menu.addSeparator()

        if self.current_target:
            a = menu.addAction("⚡ 下载（自选分段）")
            a.triggered.connect(self.quick_download_current)
            a = menu.addAction("⚡ 直接下载（默认分段）")
            a.triggered.connect(self.quick_download_current_noask)
            a = menu.addAction("⬇ 另存为...")
            a.triggered.connect(self.download_current_stream_saveas)
        else:
            a = menu.addAction("⚡ 从 URL 下载...")
            a.triggered.connect(self.download_from_url_dialog)

        menu.addSeparator()
        if self.recording_manager.is_recording():
            a = menu.addAction("■ 停止录制"); a.triggered.connect(self.stop_recording)
        else:
            a = menu.addAction("● 开始录制"); a.triggered.connect(self.start_recording)
        a = menu.addAction("📷 截图"); a.triggered.connect(self.take_snapshot)
        menu.addSeparator()
        a = menu.addAction("退出画中画" if self.pip_mode else "🖼 画中画"); a.triggered.connect(self.toggle_pip)
        a = menu.addAction("退出全屏" if self.isFullScreen() else "⛶ 全屏"); a.triggered.connect(self.toggle_fullscreen)
        a = menu.addAction("显示/隐藏播放列表"); a.triggered.connect(self.toggle_playlist_panel)
        menu.addSeparator()
        if self.current_target:
            a = menu.addAction("📋 复制当前地址"); a.triggered.connect(self.copy_current_url)
        a = menu.addAction("📂 打开下载目录"); a.triggered.connect(self.open_download_folder)
        a = menu.addAction("⚙ 设置..."); a.triggered.connect(self.open_settings_dialog)
        menu.exec(global_pos)

    def take_snapshot(self):
        if not self.player.get_media(): QMessageBox.information(self, "提示", "当前没有播放内容"); return
        rec_dir = Path(self.settings.get("record_dir", str(Path.home() / "Videos" / "UniversalPlayerRecordings")))
        snap_dir = rec_dir / "snapshots"
        try: snap_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e: QMessageBox.warning(self, "无法创建截图目录", str(e)); return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = str(snap_dir / f"snapshot_{ts}.png")
        try:
            result = self.player.video_take_snapshot(0, filename, 0, 0)
            if result == 0: self.statusBar().showMessage(f"✅ 截图已保存：{filename}")
            else: QMessageBox.warning(self, "截图失败", "VLC 返回非零")
        except Exception as e: QMessageBox.warning(self, "截图失败", str(e))

    def copy_current_url(self):
        if self.current_target:
            QApplication.clipboard().setText(self.current_target)
            self.statusBar().showMessage("已复制到剪贴板")

    def do_register_context_menu(self):
        ok, errors = register_context_menu()
        if ok == 0 and errors: QMessageBox.warning(self, "注册失败", "\n".join(errors[:5])); return
        msg = f"✅ 已为 {ok} 种文件类型注册右键菜单。"
        if not getattr(sys, "frozen", False): msg += "\n\n⚠️ 当前是开发模式（非 exe）。"
        QMessageBox.information(self, "注册成功", msg)

    def do_unregister_context_menu(self):
        ok, errors = unregister_context_menu()
        QMessageBox.information(self, "已取消", f"已从 {ok} 种文件类型移除。")

    def check_context_menu_status(self):
        if is_context_menu_registered(): QMessageBox.information(self, "状态", "✅ 已注册")
        else: QMessageBox.information(self, "状态", "❌ 未注册")

    def init_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None; return
        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip("万能播放器")
        menu = QMenu()
        a = menu.addAction("显示主窗口"); a.triggered.connect(self.restore_from_tray)
        a = menu.addAction("播放 / 暂停"); a.triggered.connect(self.toggle_play)
        a = menu.addAction("停止"); a.triggered.connect(self.stop)
        menu.addSeparator()
        a = menu.addAction("⚡ 下载（自选分段）"); a.triggered.connect(self.quick_download_current)
        a = menu.addAction("⚡ 直接下载（默认分段）"); a.triggered.connect(self.quick_download_current_noask)
        a = menu.addAction("停止下载"); a.triggered.connect(self.stop_download)
        a = menu.addAction("打开下载目录"); a.triggered.connect(self.open_download_folder)
        menu.addSeparator()
        a = menu.addAction("开始/停止录制"); a.triggered.connect(self.toggle_recording)
        a = menu.addAction("查看日志"); a.triggered.connect(self.open_log_viewer)
        menu.addSeparator()
        a = menu.addAction("退出"); a.triggered.connect(self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick: self.restore_from_tray()

    def restore_from_tray(self):
        self.showNormal(); self.raise_(); self.activateWindow()

    def hide_to_tray(self):
        self.hide()
        if self.tray:
            self.tray.showMessage("万能播放器", "已最小化到托盘。",
                                  QSystemTrayIcon.MessageIcon.Information, 2000)

    def quit_app(self):
        self._really_quit = True; self.close(); QApplication.quit()

    def open_log_viewer(self):
        dlg = LogViewerDialog(self)
        dlg.spin_lines.setValue(self.settings.get("log_max_lines", 3000))
        dlg.exec()

    def open_settings_dialog(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            old = self.settings.copy()
            self.settings = dlg.get_settings()
            save_settings(self.settings)
            self.download_manager.threads = self.settings.get("download_threads", 16)
            if old.get("log_level") != self.settings.get("log_level"):
                apply_log_level(self.settings.get("log_level", "INFO"))
            if hasattr(self, "browser_tab") and self.browser_tab.bookmark_bar:
                vis = self.settings.get("browser_show_bookmarks", True)
                self.browser_tab.bookmark_bar.setVisible(vis)
                self.browser_tab.btn_toggle_bar.setChecked(vis)
            if (hasattr(self, "browser_tab") and hasattr(self.browser_tab, "view")
                    and self.browser_tab.view is not None):
                if old.get("browser_manual_navigation") != self.settings.get("browser_manual_navigation"):
                    self.browser_tab.set_manual_navigation(self.settings.get("browser_manual_navigation", False))
                if old.get("browser_sniff_mode") != self.settings.get("browser_sniff_mode"):
                    self.browser_tab.set_sniff_mode(self.settings.get("browser_sniff_mode", "m3u8"))
            msg = f"设置已保存（默认分段={self.settings.get('download_threads',16)}"
            msg += "，每次询问" if self.settings.get("download_ask_threads", True) else "，不询问"
            msg += "）"
            self.statusBar().showMessage(msg)

    def get_effective_proxy(self):
        if not self.settings.get("proxy_enabled"): return None
        url = self.settings.get("proxy_url", "").strip()
        if not url: return None
        if test_proxy(url): return url
        if self.settings.get("proxy_fallback", True): return None
        return url

    def toggle_recording(self):
        if self.recording_manager.is_recording(): self.stop_recording()
        else: self.start_recording()

    def choose_record_dir(self):
        current = self.settings.get("record_dir", str(Path.home() / "Videos" / "UniversalPlayerRecordings"))
        d = QFileDialog.getExistingDirectory(self, "选择录制目录", current)
        if d:
            self.settings["record_dir"] = d; save_settings(self.settings)
            self.statusBar().showMessage(f"录制目录已设为：{d}")

    def open_record_folder(self):
        rec_dir = Path(self.settings.get("record_dir", str(Path.home() / "Videos" / "UniversalPlayerRecordings")))
        try:
            rec_dir.mkdir(parents=True, exist_ok=True)
            if platform.system() == "Windows": os.startfile(str(rec_dir))
            elif platform.system() == "Darwin": os.system(f'open "{rec_dir}"')
            else: os.system(f'xdg-open "{rec_dir}"')
        except Exception as e: QMessageBox.warning(self, "错误", str(e))

    def on_record_btn_context_menu(self, pos):
        menu = QMenu(self)
        if self.recording_manager.is_recording():
            a = menu.addAction("停止录制"); a.triggered.connect(self.stop_recording)
        else:
            a = menu.addAction("开始录制"); a.triggered.connect(self.start_recording)
        menu.addSeparator()
        a = menu.addAction("选择录制目录..."); a.triggered.connect(self.choose_record_dir)
        a = menu.addAction("打开录制目录"); a.triggered.connect(self.open_record_folder)
        menu.exec(self.btn_record.mapToGlobal(pos))

    def start_recording(self):
        if not self.current_target: QMessageBox.information(self, "提示", "没有正在播放的内容"); return
        if not FFMPEG_PATH: QMessageBox.warning(self, "缺少 ffmpeg", "录制需要 ffmpeg。"); return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_title = sanitize_filename(self.current_title or "recording", 60)
        if self.settings.get("record_ask_path", False):
            default_dir = self.settings.get("record_dir", str(Path.home() / "Videos" / "UniversalPlayerRecordings"))
            try: Path(default_dir).mkdir(parents=True, exist_ok=True)
            except Exception: pass
            default_path = str(Path(default_dir) / f"{safe_title}_{ts}.mp4")
            output_file, _ = QFileDialog.getSaveFileName(self, "选择录制保存位置", default_path,
                "MP4 视频 (*.mp4);;MKV 视频 (*.mkv);;MPEG-TS 流 (*.ts);;所有文件 (*.*)")
            if not output_file: self.statusBar().showMessage("已取消录制"); return
            if not Path(output_file).suffix: output_file += ".mp4"
        else:
            rec_dir = Path(self.settings.get("record_dir", str(Path.home() / "Videos" / "UniversalPlayerRecordings")))
            try: rec_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e: QMessageBox.warning(self, "无法创建录制目录", str(e)); return
            output_file = str(rec_dir / f"{safe_title}_{ts}.mp4")
        try: Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        except Exception as e: QMessageBox.warning(self, "无法创建目录", str(e)); return
        proxy = self.get_effective_proxy()
        headers = self._current_stream_headers or None
        ok, result = self.recording_manager.start(self.current_target, output_file, proxy=proxy, headers=headers)
        if ok:
            self.record_timer.start()
            self.btn_record.setText("■ 停止录制")
            self.btn_record.setStyleSheet("color: white; background: #d00; font-weight: bold;")
            self.statusBar().showMessage(f"🔴 录制中 → {output_file}")
        else: QMessageBox.warning(self, "录制失败", result)

    def stop_recording(self):
        self.statusBar().showMessage("正在停止录制，请稍候...")
        QApplication.processEvents()
        ok, result = self.recording_manager.stop()
        self.record_timer.stop(); self.record_label.setText("")
        self.btn_record.setText("● 录制")
        self.btn_record.setStyleSheet("color: #d00; font-weight: bold;")
        if ok:
            self.statusBar().showMessage("✅ 录制已保存")
            QMessageBox.information(self, "录制完成", f"文件已保存：\n\n{result}")
        else: QMessageBox.warning(self, "停止失败", result)

    def update_record_ui(self):
        if not self.recording_manager.is_recording():
            self.record_label.setText("")
            if self.btn_record.text() != "● 录制":
                self.btn_record.setText("● 录制")
                self.btn_record.setStyleSheet("color: #d00; font-weight: bold;")
                self.record_timer.stop()
            return
        s = self.recording_manager.elapsed_seconds()
        h, rem = divmod(s, 3600); m, sec = divmod(rem, 60)
        tstr = f"{h:02d}:{m:02d}:{sec:02d}" if h > 0 else f"{m:02d}:{sec:02d}"
        self.record_label.setText(f"● REC {tstr}")

    def showEvent(self, event):
        super().showEvent(event)
        if not self._vlc_bound:
            self._rebind_vlc(); self._vlc_bound = True

    def _rebind_vlc(self):
        try:
            wid = int(self.video_frame.winId())
            if sys.platform.startswith("win"): self.player.set_hwnd(wid)
            elif sys.platform.startswith("linux"): self.player.set_xwindow(wid)
            elif sys.platform == "darwin": self.player.set_nsobject(wid)
            try:
                self.player.video_set_mouse_input(False)
                self.player.video_set_key_input(False)
            except Exception: pass
        except Exception as e: logger.warning(f"VLC 重绑失败：{e}")

    def on_left_tab_changed(self, idx):
        if idx == 0: QTimer.singleShot(50, self._rebind_vlc)
        elif idx == 1:
            if hasattr(self, "browser_tab"): self.browser_tab.lazy_load_home()

    def toggle_video_web(self):
        idx = self.left_tabs.currentIndex()
        self.left_tabs.setCurrentIndex(1 - idx)

    def on_stream_from_browser(self, url):
        logger.info(f"浏览器 → VLC：{url[:120]}")
        self.left_tabs.setCurrentIndex(0)
        self.url_combo.setCurrentText(url)
        if self.url_combo.findText(url) == -1: self.url_combo.addItem(url)
        if self.needs_ytdlp(url):
            if not HAS_YTDLP: QMessageBox.warning(self, "缺少依赖", "请先安装 yt-dlp"); return
            self.statusBar().showMessage(f"正在解析：{url}")
            self.btn_play_url.setEnabled(False)
            threading.Thread(target=self.resolve_with_ytdlp, args=(url,), daemon=True).start()
        else:
            self._current_stream_headers = {}
            title = url.split("/")[-1].split("?")[0] or url
            self.play_media(url, title)

    def open_file(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择媒体文件", "",
            "媒体文件 (*.mp4 *.mkv *.avi *.mov *.flv *.wmv *.webm *.mp3 *.flac *.wav *.m4a *.aac *.ogg *.ts *.m3u8 *.rmvb *.3gp);;所有文件 (*.*)")
        if not paths: return
        for p in paths: self.add_to_playlist(p, os.path.basename(p))
        self.play_media(paths[0], os.path.basename(paths[0]))

    def prompt_open_url(self):
        text, ok = QInputDialog.getText(self, "打开网络链接", "请输入 URL：")
        if ok and text.strip():
            self.url_combo.setCurrentText(text.strip()); self.play_from_url_bar()

    def play_from_url_bar(self):
        url = self.url_combo.currentText().strip()
        if not url: return
        if self.url_combo.findText(url) == -1: self.url_combo.addItem(url)
        if self.needs_ytdlp(url):
            if not HAS_YTDLP: QMessageBox.warning(self, "缺少依赖", "请先安装 yt-dlp"); return
            self.statusBar().showMessage(f"正在解析：{url}")
            self.btn_play_url.setEnabled(False)
            threading.Thread(target=self.resolve_with_ytdlp, args=(url,), daemon=True).start()
        else:
            self._current_stream_headers = {}
            self.play_media(url, url)

    def add_url_to_playlist(self):
        url = self.url_combo.currentText().strip()
        if not url: QMessageBox.information(self, "提示", "请先填写 URL"); return
        if self.url_combo.findText(url) == -1: self.url_combo.addItem(url)
        self.add_to_playlist(url, url)

    @staticmethod
    def needs_ytdlp(url):
        low = url.lower()
        direct_ext = (".m3u8", ".mpd", ".mp4", ".mkv", ".flv", ".ts",
                      ".mov", ".webm", ".avi", ".mp3", ".aac", ".flac")
        direct_scheme = ("rtsp://", "rtmp://", "rtp://", "udp://", "mms://")
        if low.split("?")[0].endswith(direct_ext): return False
        if low.startswith(direct_scheme): return False
        sites = ("youtube.com", "youtu.be", "bilibili.com", "b23.tv",
                 "vimeo.com", "twitter.com", "x.com", "facebook.com",
                 "twitch.tv", "dailymotion.com", "tiktok.com", "douyin.com")
        return any(s in low for s in sites) or low.startswith("http")

    def resolve_with_ytdlp(self, url):
        proxy = self.get_effective_proxy()
        use_download = self.settings.get("youtube_download_mode", True) and FFMPEG_PATH
        if self.settings.get("youtube_download_mode", True) and not FFMPEG_PATH:
            self.signals.status.emit("⚠️ 未找到 ffmpeg，改用流式模式")
            use_download = False
        if use_download: self._resolve_by_download(url, proxy)
        else: self._resolve_by_stream(url, proxy)

    def _resolve_by_stream(self, url, proxy):
        try:
            candidates = ["b[ext=mp4][height<=720]/b[ext=mp4]/b[height<=720]/b", "b", "worst"]
            last_err = None
            for fmt in candidates:
                try:
                    opts = {"quiet": True, "no_warnings": True, "format": fmt, "noplaylist": True}
                    if proxy: opts["proxy"] = proxy
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                        if "entries" in info: info = info["entries"][0]
                        title = info.get("title", url)
                        stream_url = info.get("url")
                        if stream_url:
                            headers = info.get("http_headers") or {}
                            self.signals.resolved.emit(stream_url, title, headers); return
                        last_err = f"格式 {fmt} 未返回地址"
                except Exception as e: last_err = str(e)
            self.signals.failed.emit(f"流式失败：{last_err}")
        except Exception as e: self.signals.failed.emit(str(e))

    def _resolve_by_download(self, url, proxy):
        try:
            cache_dir = Path(tempfile.gettempdir()) / "universal_player_cache"
            cache_dir.mkdir(exist_ok=True)
            max_h = self.settings.get("youtube_max_height", 1080)
            fmt = (f"bv*[height<={max_h}][ext=mp4]+ba[ext=m4a]/bv*[height<={max_h}]+ba/b[height<={max_h}]/b")
            out_tmpl = str(cache_dir / "%(title).80s.%(ext)s")
            def hook(d):
                if d["status"] == "downloading":
                    self.signals.status.emit(f"下载中 {d.get('_percent_str','').strip()}  "
                                             f"{d.get('_speed_str','').strip()}  剩余 {d.get('_eta_str','').strip()}")
                elif d["status"] == "finished":
                    self.signals.status.emit("下载完成，合并中...")
            opts = {"quiet": True, "no_warnings": True, "format": fmt,
                    "noplaylist": True, "outtmpl": out_tmpl,
                    "merge_output_format": "mp4", "progress_hooks": [hook],
                    "windowsfilenames": True, "retries": 3, "fragment_retries": 3,
                    "concurrent_fragment_downloads": self.settings.get("download_threads", 16)}
            if proxy: opts["proxy"] = proxy
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if "entries" in info: info = info["entries"][0]
                title = info.get("title", url)
                filepath = None
                rd = info.get("requested_downloads")
                if rd: filepath = rd[0].get("filepath")
                if not filepath:
                    safe_title = info.get("title", "video")[:80]
                    for f in cache_dir.glob(f"{safe_title}.*"):
                        if f.suffix in (".mp4", ".mkv", ".webm"):
                            filepath = str(f); break
                if not filepath or not os.path.exists(filepath):
                    raise RuntimeError(f"下载完成但未找到文件：{cache_dir}")
                self.signals.resolved.emit(filepath, title, {})
        except Exception as e:
            self.signals.failed.emit(f"下载失败：{e}\n\n详见日志（Ctrl+G）")

    def on_url_resolved(self, stream_url, title, headers=None):
        self._current_stream_headers = headers or {}
        self.btn_play_url.setEnabled(True)
        self.statusBar().showMessage(f"已解析：{title}")
        self.play_media(stream_url, title)

    def on_url_failed(self, err):
        self.btn_play_url.setEnabled(True)
        self.statusBar().showMessage("解析失败")
        QMessageBox.warning(self, "解析失败", f"无法解析该链接：\n\n{err}")

    def on_status_message(self, msg): self.statusBar().showMessage(msg)

    def play_media(self, target, title):
        if not target.startswith(("http://", "https://", "rtsp://", "rtmp://",
                                  "rtp://", "udp://", "mms://")):
            if not os.path.exists(target): QMessageBox.warning(self, "错误", "文件不存在"); return
        self.left_tabs.setCurrentIndex(0)
        media = self.instance.media_new(target)
        if target.startswith(("http://", "https://", "rtsp://", "rtmp://")):
            media.add_option(":network-caching=1500")
            media.add_option(":live-caching=1500")
        proxy = self.get_effective_proxy()
        if proxy and target.startswith(("http://", "https://")):
            media.add_option(f":http-proxy={proxy}")
        self.player.set_media(media); self.player.play()
        self.player.set_rate(self.current_rate)
        try:
            self.player.video_set_mouse_input(False)
            self.player.video_set_key_input(False)
        except Exception: pass
        self.current_target = target; self.current_title = title
        self.setWindowTitle(f"万能播放器 - {title}")
        self.statusBar().showMessage(f"正在播放：{title}")
        self.timer.start(); self.btn_play.setText("暂停")
        self.highlight_playlist_item(target)
        self._audio_tracks_loaded = False; self._track_retry = 0
        self.audio_track_combo.blockSignals(True)
        self.audio_track_combo.clear()
        self.audio_track_combo.addItem("（加载中…）", -1)
        self.audio_track_combo.blockSignals(False)
        QTimer.singleShot(1200, self.refresh_audio_tracks)

    def highlight_playlist_item(self, target):
        for i in range(self.playlist_widget.count()):
            if self.playlist_widget.item(i).data(Qt.ItemDataRole.UserRole) == target:
                self.playlist_widget.setCurrentRow(i); return

    def toggle_play(self):
        if self.player.is_playing():
            self.player.pause(); self.btn_play.setText("播放")
        else:
            self.player.play(); self.btn_play.setText("暂停")
        try:
            self.player.video_set_mouse_input(False)
            self.player.video_set_key_input(False)
        except Exception: pass

    def stop(self):
        self.player.stop(); self.timer.stop()
        self.progress.setValue(0); self.progress.set_duration(0)
        self.time_label.setText("00:00 / 00:00")
        self.btn_play.setText("播放"); self.setWindowTitle("万能播放器")
        self.statusBar().showMessage("已停止")
        self.audio_track_combo.blockSignals(True)
        self.audio_track_combo.clear()
        self.audio_track_combo.addItem("（未加载）", -1)
        self.audio_track_combo.blockSignals(False)
        self._audio_tracks_loaded = False

    def on_seek_live(self, value):
        length = self.player.get_length()
        if length > 0:
            self.time_label.setText(f"{self.ms_to_str(value)} / {self.ms_to_str(length)}")

    def on_seek_finished(self, value):
        self.player.set_time(int(value))
        self.statusBar().showMessage(f"跳转到 {self.ms_to_str(value)}")

    def update_ui(self):
        if self.player.is_playing():
            length = self.player.get_length(); time = self.player.get_time()
            if length > 0:
                self.progress.set_duration(length); self.progress.setMaximum(length)
                if not self.progress._dragging:
                    self.progress.setValue(time)
                    self.time_label.setText(f"{self.ms_to_str(time)} / {self.ms_to_str(length)}")
            else:
                self.progress.set_duration(0); self.progress.setMaximum(0)
                if not self.progress._dragging:
                    self.time_label.setText(f"{self.ms_to_str(time)} / LIVE")
            self.btn_play.setText("暂停")
            if not self._audio_tracks_loaded:
                self._track_retry += 1
                if self._track_retry >= 3:
                    self._track_retry = 0
                    try: tracks = self.player.audio_get_track_description()
                    except Exception: tracks = None
                    if tracks: self.refresh_audio_tracks()
        else:
            state = self.player.get_state()
            if state == vlc.State.Ended:
                if not self.play_next_in_playlist(auto=True): self.stop()
            else: self.btn_play.setText("播放")

    @staticmethod
    def ms_to_str(ms):
        if ms < 0: ms = 0
        s = ms // 1000; m, s = divmod(s, 60); h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

    def on_speed_changed(self, text):
        try: rate = float(text.rstrip("x"))
        except ValueError: return
        self.current_rate = rate; self.player.set_rate(rate)
        self.statusBar().showMessage(f"播放速度：{rate}x")

    def _change_speed(self, delta):
        nr = max(0.25, min(4.0, round(self.current_rate + delta, 2)))
        text = f"{nr}x"
        idx = self.speed_combo.findText(text)
        if idx < 0:
            self.speed_combo.addItem(text); idx = self.speed_combo.count() - 1
        self.speed_combo.setCurrentIndex(idx)

    def refresh_audio_tracks(self):
        if not self.player.get_media(): return
        try: tracks = self.player.audio_get_track_description()
        except Exception: tracks = None
        self.audio_track_combo.blockSignals(True); self.audio_track_combo.clear()
        if not tracks:
            self.audio_track_combo.addItem("（无音轨）", -1)
            self.audio_track_combo.blockSignals(False); return
        current_id = self.player.audio_get_track(); matched = -1
        for tid, name in tracks:
            if tid is None: continue
            label = name.decode("utf-8", errors="ignore").strip() if isinstance(name, bytes) else str(name).strip()
            if not label: label = f"音轨 {tid}"
            label = self._pretty_track_name(label)
            self.audio_track_combo.addItem(label, tid)
            if tid == current_id: matched = self.audio_track_combo.count() - 1
        if matched >= 0: self.audio_track_combo.setCurrentIndex(matched)
        self.audio_track_combo.blockSignals(False)
        self._audio_tracks_loaded = True

    @staticmethod
    def _pretty_track_name(name):
        low = name.lower()
        mp = [
            (("chinese", "chi", "zho", "zh", "mandarin", "cantonese", "国语", "中文", "粤语"), "中文"),
            (("english", "eng", "en ", "en-", "英语", "英文"), "英文"),
            (("japanese", "jpn", "ja ", "日语", "日文"), "日文"),
            (("korean", "kor", "ko ", "韩语", "韩文"), "韩文"),
            (("french", "fra", "fre", "fr ", "法语"), "法文"),
            (("spanish", "spa", "es ", "西班牙语"), "西班牙文"),
            (("german", "deu", "ger", "de ", "德语"), "德文"),
            (("russian", "rus", "ru ", "俄语"), "俄文"),
        ]
        for keys, zh in mp:
            if any(k in low for k in keys): return f"{zh}（{name}）"
        return name

    def on_audio_track_changed(self, index):
        if index < 0: return
        tid = self.audio_track_combo.itemData(index)
        if tid is None or tid == -1: return
        try: self.player.audio_set_track(int(tid))
        except Exception as e: logger.exception(f"切换音轨失败：{e}")

    def _cycle_audio_track(self, delta):
        if self.audio_track_combo.count() <= 1:
            self.statusBar().showMessage("当前只有一条音轨"); return
        idx = (self.audio_track_combo.currentIndex() + delta) % self.audio_track_combo.count()
        self.audio_track_combo.setCurrentIndex(idx)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            self.menuBar().setVisible(True)
            self.progress_bar.setVisible(True)
            self.control_bar.setVisible(True)
            self.url_bar.setVisible(True)
            self.side_panel.setVisible(self._side_panel_was_visible)
            self.statusBar().setVisible(True)
            self.btn_fullscreen.setText("全屏")
        else:
            self._side_panel_was_visible = self.side_panel.isVisible()
            self.showFullScreen()
            self.menuBar().setVisible(False)
            self.progress_bar.setVisible(False)
            self.control_bar.setVisible(False)
            self.url_bar.setVisible(False); self.side_panel.setVisible(False)
            self.statusBar().setVisible(False)
            self.btn_fullscreen.setText("退出全屏")

    def toggle_pip(self):
        if self.pip_mode:
            self.setWindowFlags(self._normal_flags)
            if self._normal_geometry: self.setGeometry(self._normal_geometry)
            self.menuBar().setVisible(True); self.url_bar.setVisible(True)
            self.progress_bar.setVisible(True)
            self.control_bar.setVisible(True); self.side_panel.setVisible(True)
            self.statusBar().setVisible(True); self.show()
            self.btn_pip.setText("画中画"); self.pip_mode = False
        else:
            self._normal_geometry = self.geometry(); self._normal_flags = self.windowFlags()
            self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint |
                                Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
            self.menuBar().setVisible(False); self.url_bar.setVisible(False)
            self.side_panel.setVisible(False); self.statusBar().setVisible(False)
            self.progress_bar.setVisible(True)
            self.control_bar.setVisible(True)
            self.resize(560, 340); self.show()
            self.btn_pip.setText("退出画中画"); self.pip_mode = True

    def on_video_drag(self, delta):
        if self.pip_mode: self.move(self.pos() + delta)

    def keyPressEvent(self, event):
        key = event.key(); mods = event.modifiers()
        ctrl = mods & Qt.KeyboardModifier.ControlModifier
        shift = mods & Qt.KeyboardModifier.ShiftModifier
        alt = mods & Qt.KeyboardModifier.AltModifier
        if key == Qt.Key.Key_Escape:
            if self.isFullScreen(): self.toggle_fullscreen()
            elif self.pip_mode: self.toggle_pip()
        elif key == Qt.Key.Key_Space: self.toggle_play()
        elif key == Qt.Key.Key_Left: self.player.set_time(max(0, self.player.get_time() - 5000))
        elif key == Qt.Key.Key_Right: self.player.set_time(self.player.get_time() + 5000)
        elif key == Qt.Key.Key_Up:
            v = min(100, self.player.audio_get_volume() + 5)
            self.player.audio_set_volume(v); self.volume_slider.setValue(v)
        elif key == Qt.Key.Key_Down:
            v = max(0, self.player.audio_get_volume() - 5)
            self.player.audio_set_volume(v); self.volume_slider.setValue(v)
        elif key == Qt.Key.Key_F: self.toggle_fullscreen()
        elif key == Qt.Key.Key_BracketLeft: self._change_speed(-0.25)
        elif key == Qt.Key.Key_BracketRight: self._change_speed(0.25)
        elif key == Qt.Key.Key_A and ctrl and shift: self._cycle_audio_track(-1)
        elif key == Qt.Key.Key_A and ctrl: self._cycle_audio_track(1)
        elif key == Qt.Key.Key_P and ctrl: self.toggle_pip()
        elif key == Qt.Key.Key_N and ctrl: self.open_new_window()
        elif key == Qt.Key.Key_Comma and ctrl: self.open_settings_dialog()
        elif key == Qt.Key.Key_G and ctrl: self.open_log_viewer()
        elif key == Qt.Key.Key_S and ctrl and shift: self.take_snapshot()
        elif key == Qt.Key.Key_D and ctrl and shift and alt: self.quick_download_current_noask()
        elif key == Qt.Key.Key_D and ctrl and shift: self.quick_download_current()
        elif key == Qt.Key.Key_D and ctrl: self.add_current_to_favorites()
        elif key == Qt.Key.Key_R and ctrl and shift: self.choose_record_dir()
        elif key == Qt.Key.Key_R and ctrl: self.toggle_recording()
        elif key == Qt.Key.Key_T and ctrl: self.toggle_video_web()
        else: super().keyPressEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls(): event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls: return
        first_t = None; first_title = None
        for u in urls:
            if u.toString().startswith("file://"):
                p = u.toLocalFile()
                self.add_to_playlist(p, os.path.basename(p))
                if first_t is None: first_t, first_title = p, os.path.basename(p)
            else:
                url = u.toString(); self.add_to_playlist(url, url)
                if first_t is None: first_t, first_title = url, url
        if first_t: self.play_media(first_t, first_title)

    def add_files_to_playlist(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "添加媒体文件", "",
            "媒体文件 (*.mp4 *.mkv *.avi *.mov *.flv *.wmv *.webm *.mp3 *.flac *.wav *.m4a *.aac *.ogg *.ts *.m3u8 *.rmvb *.3gp);;所有文件 (*.*)")
        for p in paths: self.add_to_playlist(p, os.path.basename(p))

    def add_to_playlist(self, target, title):
        item = QListWidgetItem(title)
        item.setData(Qt.ItemDataRole.UserRole, target); item.setToolTip(target)
        self.playlist_widget.addItem(item)
        self.playlist_data.append({"target": target, "title": title})
        save_json(PLAYLIST_FILE, self.playlist_data)

    def load_playlist_to_ui(self):
        self.playlist_widget.clear()
        for e in self.playlist_data:
            item = QListWidgetItem(e.get("title", e["target"]))
            item.setData(Qt.ItemDataRole.UserRole, e["target"]); item.setToolTip(e["target"])
            self.playlist_widget.addItem(item)

    def remove_selected_from_playlist(self):
        rows = sorted([self.playlist_widget.row(i) for i in
                       self.playlist_widget.selectedItems()], reverse=True)
        for r in rows:
            self.playlist_widget.takeItem(r)
            if 0 <= r < len(self.playlist_data): del self.playlist_data[r]
        save_json(PLAYLIST_FILE, self.playlist_data)

    def clear_playlist(self):
        if QMessageBox.question(self, "确认", "清空播放列表？") == QMessageBox.StandardButton.Yes:
            self.playlist_widget.clear(); self.playlist_data = []
            save_json(PLAYLIST_FILE, self.playlist_data)

    def on_playlist_double_clicked(self, item):
        self.play_media(item.data(Qt.ItemDataRole.UserRole), item.text())

    def on_playlist_context_menu(self, pos):
        item = self.playlist_widget.itemAt(pos)
        if not item: return
        url = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        a1 = menu.addAction("▶ 播放")
        menu.addSeparator()
        a2a = menu.addAction("⚡ 下载（自选分段）")
        a2q = menu.addAction("⚡ 直接下载（默认分段）")
        a2s = menu.addAction("⬇ 另存为...")
        menu.addSeparator()
        a3 = menu.addAction("加入收藏")
        a4 = menu.addAction("从列表删除")
        act = menu.exec(self.playlist_widget.mapToGlobal(pos))
        if act == a1: self.play_media(url, item.text())
        elif act == a2a: self.quick_download_stream(url, item.text(), ask=True)
        elif act == a2q: self.quick_download_stream(url, item.text(), ask=False)
        elif act == a2s: self.saveas_download_stream(url, item.text())
        elif act == a3:
            self.favorites_data.append({"target": url, "title": item.text()})
            save_json(FAVORITES_FILE, self.favorites_data); self.load_favorites_to_ui()
        elif act == a4:
            row = self.playlist_widget.row(item); self.playlist_widget.takeItem(row)
            if 0 <= row < len(self.playlist_data): del self.playlist_data[row]
            save_json(PLAYLIST_FILE, self.playlist_data)

    def play_next_in_playlist(self, auto=False):
        row = self.playlist_widget.currentRow()
        if row < 0 or row >= self.playlist_widget.count() - 1:
            if not auto: self.statusBar().showMessage("已经是最后一个")
            return False
        it = self.playlist_widget.item(row + 1)
        self.playlist_widget.setCurrentRow(row + 1)
        self.play_media(it.data(Qt.ItemDataRole.UserRole), it.text())
        return True

    def play_prev_in_playlist(self):
        row = self.playlist_widget.currentRow()
        if row <= 0: self.statusBar().showMessage("已经是第一个"); return
        it = self.playlist_widget.item(row - 1)
        self.playlist_widget.setCurrentRow(row - 1)
        self.play_media(it.data(Qt.ItemDataRole.UserRole), it.text())

    def add_current_to_favorites(self):
        if not self.current_target: QMessageBox.information(self, "提示", "当前没有播放内容"); return
        for f in self.favorites_data:
            if f["target"] == self.current_target:
                QMessageBox.information(self, "提示", "已在收藏夹中"); return
        self.favorites_data.append({"target": self.current_target, "title": self.current_title or self.current_target})
        save_json(FAVORITES_FILE, self.favorites_data); self.load_favorites_to_ui()
        self.statusBar().showMessage("已加入收藏")

    def load_favorites_to_ui(self):
        self.favorites_widget.clear()
        for e in self.favorites_data:
            item = QListWidgetItem(e.get("title", e["target"]))
            item.setData(Qt.ItemDataRole.UserRole, e["target"]); item.setToolTip(e["target"])
            self.favorites_widget.addItem(item)

    def remove_selected_favorites(self):
        rows = sorted([self.favorites_widget.row(i) for i in
                       self.favorites_widget.selectedItems()], reverse=True)
        for r in rows:
            self.favorites_widget.takeItem(r)
            if 0 <= r < len(self.favorites_data): del self.favorites_data[r]
        save_json(FAVORITES_FILE, self.favorites_data)

    def on_favorites_double_clicked(self, item):
        self.play_media(item.data(Qt.ItemDataRole.UserRole), item.text())

    def on_favorites_context_menu(self, pos):
        item = self.favorites_widget.itemAt(pos)
        if not item: return
        url = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        a1 = menu.addAction("播放")
        a2a = menu.addAction("⚡ 下载（自选分段）")
        a2q = menu.addAction("⚡ 直接下载（默认分段）")
        a2s = menu.addAction("⬇ 另存为...")
        a3 = menu.addAction("加入播放列表")
        a4 = menu.addAction("删除")
        act = menu.exec(self.favorites_widget.mapToGlobal(pos))
        if act == a1: self.play_media(url, item.text())
        elif act == a2a: self.quick_download_stream(url, item.text(), ask=True)
        elif act == a2q: self.quick_download_stream(url, item.text(), ask=False)
        elif act == a2s: self.saveas_download_stream(url, item.text())
        elif act == a3: self.add_to_playlist(url, item.text())
        elif act == a4:
            row = self.favorites_widget.row(item); self.favorites_widget.takeItem(row)
            if 0 <= row < len(self.favorites_data): del self.favorites_data[row]
            save_json(FAVORITES_FILE, self.favorites_data)

    def toggle_playlist_panel(self):
        self.side_panel.setVisible(not self.side_panel.isVisible())

    def open_new_window(self):
        w = UniversalPlayer()
        off = len(_all_windows) * 30
        w.move(self.x() + off, self.y() + off); w.show()
        _all_windows.append(w)

    def show_about(self):
        info = (
            "万能播放器\n"
            "基于 PySide6 + VLC + yt-dlp + ffmpeg + Chromium\n\n"
            f"下载引擎：{'aria2c（16+ 线程加速）' if ARIA2C_PATH else 'yt-dlp 内建多线程'}\n"
            f"默认分段：{self.settings.get('download_threads', 16)}\n"
            f"每次询问分段：{'是' if self.settings.get('download_ask_threads', True) else '否'}\n\n"
            f"日志目录：{LOG_DIR}"
        )
        QMessageBox.about(self, "关于", info)

    def changeEvent(self, event):
        if (event.type() == event.Type.WindowStateChange and self.isMinimized()
                and self.settings.get("minimize_to_tray", False) and self.tray):
            QTimer.singleShot(0, self.hide_to_tray)
        super().changeEvent(event)

    def closeEvent(self, event):
        if not self._really_quit and self.settings.get("close_to_tray", True) and self.tray:
            event.ignore(); self.hide_to_tray(); return
        if self.recording_manager.is_recording(): self.recording_manager.stop()
        if self.download_manager.is_downloading(): self.download_manager.stop()
        self.player.stop(); self.timer.stop()
        if self.tray: self.tray.hide()
        if self in _all_windows: _all_windows.remove(self)
        super().closeEvent(event)


def install_exception_hook():
    def hook(t, v, tb):
        logger.critical("未捕获异常", exc_info=(t, v, tb))
        sys.__excepthook__(t, v, tb)
    sys.excepthook = hook


_all_windows = []


def main():
    install_exception_hook()
    media_arg = None
    for arg in sys.argv[1:]:
        if not arg.startswith("-") and os.path.isfile(arg):
            media_arg = arg; break
    early_settings = load_settings()
    flags = []
    if HAS_WEBENGINE and early_settings.get("proxy_enabled"):
        pxy = early_settings.get("proxy_url", "").strip()
        if pxy and test_proxy(pxy): flags.append(f"--proxy-server={pxy}")
    if early_settings.get("browser_disable_gpu", False):
        flags += ["--disable-gpu", "--disable-software-rasterizer", "--disable-gpu-compositing"]
    if flags:
        existing = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (existing + " " + " ".join(flags)).strip()
    app = QApplication(sys.argv)
    app.setApplicationName("万能播放器")
    app.setQuitOnLastWindowClosed(False)
    player = UniversalPlayer()
    player.show()
    _all_windows.append(player)
    if media_arg:
        QTimer.singleShot(500, lambda: player.play_media(media_arg, os.path.basename(media_arg)))
    code = app.exec()
    sys.exit(code)


if __name__ == "__main__":
    main()