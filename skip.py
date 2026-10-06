# -*- coding: utf-8 -*-
"""
原神自动跳剧情 + 自动选选项。

机制：
  前台检查 → 定位游戏窗口客户区 → 模板匹配判定界面状态：
    A. 对话 + 选项 → 鼠标点击第一个选项
    B. 对话 + 无选项 → 每 200ms 按空格推进台词
    C. 非对话 → 不做任何输入（不影响正常游玩）

热键：
  F8   开始 / 停止
  F9   退出程序
  说明：脚本会自动请求管理员权限（原神常以管理员运行，普通权限下热键收不到、注入被拦截）

使用：
  python skip.py              正常运行（首次弹 UAC 请点"是"）
  python skip.py --debug      诊断模式：每秒打印匹配度/判定/开关状态
  python skip.py --selfcheck  自检：用样本验证三态判定，不注入任何按键

依赖：pip install opencv-python numpy mss keyboard rapidocr-onnxruntime Pillow pywebview
"""
import ctypes
from ctypes import wintypes
import sys
import time
import os
import threading
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np

# ---------------------------- 配置 ----------------------------
SPACE_SC = 0x39          # 空格键扫描码（推进对话）
MIN_INTERVAL = 0.2       # 两次按键的最小间隔（200ms 限频）
TALK_THRESHOLD = 0.80    # 对话判定阈值（实测：对话 0.817 稳定 / 误判 0.703，0.8 分割干净）
POLL_INTERVAL = 0.02     # 截图轮询间隔（50fps）
HOTKEY_TOGGLE = "f8"     # 开始/停止
HOTKEY_EXIT = "f9"       # 退出

# 对话判定 ROI：左上角 1/3 × 1/8 区域
TALK_ROI = (0.0, 0.0, 1 / 3, 1 / 8)

# 选项判定：
#   OptionIcon = icon_option.png 模板 + @option ROI + 阈值0.75 + 多目标NMS匹配
#   @option = rect(cw/2, ch/12, cw-cw/2-cw/6, ch-ch/12-10) = x50~83.3%, y8.3~91.7%
#   每个匹配点 = 一个选项气泡 → 匹配点数 = 选项数
#   ExclamationIcon = icon_exclamation.png 同 ROI，存在则优先点击
OPTION_ROI = (0.5, 1 / 12, 0.5 + 1 / 3, 1 - 1 / 12)   # (x0,y0,x1,y1) 千分比 = (0.5, 0.083, 0.833, 0.917)
OPTION_ICON_THRESHOLD = 0.90   # 阈值 0.9：实测真实选项 0.955/0.985，误判 <0.8，余量充足
OPTION_ICON_TEMPLATE = "icon_option.png"
EXCLAMATION_TEMPLATE = "icon_exclamation.png"
OPTION_CLICK_COOLDOWN = 0.8    # 点击选项后的冷却（防连点；冷却期内不空格，防止误确认）

# 选项文字关键词决策链：
#   气泡图标匹配到选项后，OCR 识别右侧选项文字；文字命中暂停列表 → 本对话场景整体静默
#   （不点选项、不按空格），避免误操作 NPC 功能菜单（凯瑟琳/铁匠/尘歌壶阿圆/声望等）。
# 内置列表为基础关键词；可编辑 pause_options.json（JSON 字符串数组）追加。
DEFAULT_PAUSE_KEYWORDS = ["凯瑟琳", "铁匠", "阿圆", "声望"]
PAUSE_OPTIONS_FILE = "pause_options.json"
OCR_ROI = (0.5, 0.08, 1.0, 0.92)      # 选项文字识别区（右侧，与 @option 对齐）
OCR_COOLDOWN = 3.0                    # 气泡出现后 OCR 一次，3 秒内不重复（RapidOCR 约 0.3~1.2s）
OCR_PAUSE_DURATION = 30.0             # 命中暂停关键词后静默 30 秒；气泡消失自动解除

TALK_TEMPLATE = "disabled_ui.png"  # 对话界面 UI 资产（左上角）
BASE_DIR = Path(__file__).resolve().parent

# ---------------------------- 管理员权限（UAC 提权） ----------------------------
def ensure_admin() -> bool:
    """
    非管理员时弹 UAC 以管理员身份重启自己（返回 False 表示已转交新进程，本进程退出）。
    原因：原神常以管理员权限运行，普通权限下
      1) keyboard 库全局热键收不到游戏内按键（F8 开关不响应）；
      2) keybd_event/mouse_event 注入被 Windows UIPI 静默拦截（按了没效果）。
    注意：ShellExecuteW 的 lpDirectory 必须传脚本目录，否则新进程工作目录错误可能启动失败。
    """
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return True
    except Exception:
        return True  # 判断失败时按当前权限继续，不阻塞运行
    script = str(Path(sys.argv[0]).resolve())
    args = [a if " " not in a else f'"{a}"' for a in sys.argv[1:]]
    params = f'"{script}"' + (f" {' '.join(args)}" if args else "")
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, str(Path(script).parent), 1)
    if ret <= 32:
        print(f"[错误] 提权重启失败（ShellExecuteW 返回 {ret}）。\n"
              f"请手动操作：关闭本窗口，右键“以管理员身份运行”再执行。")
        time.sleep(5)
        return False
    print("已触发管理员权限重启，新窗口即将打开（UAC 点击“是”后请留意新窗口，不要关闭旧窗口直到新窗口出现）")
    time.sleep(4)
    return False


# ---------------------------- 前台检测 ----------------------------
GAME_PROCESS_NAMES = {"yuanshen", "genshinimpact", "genshin impact cloud game", "genshin impact cloud"}
GAME_TITLE_KEYWORDS = ("原神", "genshin", "yuanshen", "云·原神")


def get_foreground_process_name() -> Optional[str]:
    """返回当前前台窗口的进程名（小写），失败返回 None"""
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return None
            return buf.value.rsplit("\\", 1)[-1].lower()
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return None


def get_foreground_window_title() -> Optional[str]:
    """返回当前前台窗口的标题，失败返回 None"""
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None
        buf = ctypes.create_unicode_buffer(512)
        n = ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
        return buf.value[:n] if n else None
    except Exception:
        return None


def is_genshin_foreground() -> bool:
    """原神窗口是否在前台（必须前台才允许触发按键）。
    判定：① 前台进程名 ∈ 白名单；② 未命中回退窗口标题包含关键词（覆盖进程名被修改的情况）"""
    name = get_foreground_process_name()
    if name and name in GAME_PROCESS_NAMES:
        return True
    title = get_foreground_window_title()
    if title:
        t = title.lower()
        return any(k in t for k in GAME_TITLE_KEYWORDS)
    return False


# ---------------------------- 输入注入 ----------------------------
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_KEYUP = 0x0002
_user32 = ctypes.WinDLL("user32", use_last_error=True)
# 显式声明参数类型：numpy 标量(numpy.int64 等)无法直接转 ctypes 参数，必须 int() 强转
_user32.SetCursorPos.argtypes = [wintypes.INT, wintypes.INT]
_user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
_user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p]

# DPI 感知：mss 截图用物理像素，而 GetClientRect/ClientToScreen 的返回值
# 取决于进程 DPI 感知模式。不设置时 Windows 会返回虚拟化（逻辑）坐标，
# 高 DPI 缩放下与截图坐标错位。统一为物理像素。
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass


def get_active_genshin_client_rect() -> Optional[Tuple[int, int, int, int]]:
    """前台是原神时，返回其客户区在屏幕上的矩形 (left, top, right, bottom)。
    全屏 = 整个屏幕；窗口化 = 窗口客户区（不含标题栏/边框）。
    窗口化时游戏画面不占满屏幕，所有 ROI 必须按窗口内坐标计算，
    否则匹配位置全错（Bug 根因：截图全屏 + 全屏百分比 ROI）。"""
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None
        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        pt = wintypes.POINT(0, 0)
        ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt))
        return (pt.x, pt.y, pt.x + rect.right, pt.y + rect.bottom)
    except Exception:
        return None


def tap_key(sc: int, hold: float = 0.03):
    """按下并抬起一个硬件扫描码（keybd_event，等价 AHK Send "{sc0x}"）"""
    sc = int(sc)  # numpy 标量强转
    _user32.keybd_event(sc, sc, KEYEVENTF_SCANCODE, 0)
    time.sleep(hold)
    _user32.keybd_event(sc, sc, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP, 0)


def click_abs(x: int, y: int):
    """在屏幕绝对坐标 (x,y) 左键单击（SetCursorPos + mouse_event）"""
    x, y = int(x), int(y)  # numpy 标量强转
    _user32.SetCursorPos(x, y)
    time.sleep(0.03)
    _user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
    time.sleep(0.04)
    _user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP


# ---------------------------- 界面检测 ----------------------------
class TemplateMatcher:
    """多尺度模板匹配：模板是小图标，游戏窗口分辨率变化时图标大小会变，
    因此把模板按多种尺度放大/缩小后逐一匹配，取最高置信度。
    支持 ROI 限定：只匹配左上角区域，避免全局误匹配。"""

    def __init__(self, template_path: Path, threshold: float):
        tpl = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            raise FileNotFoundError(f"找不到模板文件: {template_path}")
        self.tpl = tpl
        self.threshold = threshold
        self.scales = [0.6, 0.8, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
        self._cache = {}

    def _scaled(self, s: float):
        key = s
        if key not in self._cache:
            th, tw = self.tpl.shape[:2]
            nw, nh = max(1, int(tw * s)), max(1, int(th * s))
            self._cache[key] = cv2.resize(self.tpl, (nw, nh), interpolation=cv2.INTER_AREA)
        return self._cache[key]

    def detect(self, frame_bgr, roi: Optional[Tuple[float, float, float, float]] = None) -> Tuple[bool, float]:
        """返回 (是否命中, 最高匹配度)。roi=(x0,y0,x1,y1) 千分比，限定匹配区域"""
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        if roi is not None:
            h, w = gray.shape[:2]
            x0, y0 = int(w * roi[0]), int(h * roi[1])
            x1, y1 = int(w * roi[2]), int(h * roi[3])
            gray = gray[y0:y1, x0:x1]
        best_val = -1.0
        for s in self.scales:
            t = self._scaled(s)
            th, tw = t.shape[:2]
            if tw > gray.shape[1] or th > gray.shape[0]:
                continue
            res = cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)
            mx = cv2.minMaxLoc(res)[1]
            if mx > best_val:
                best_val = mx
        return best_val >= self.threshold, best_val


def _iou(x1, y1, w1, h1, x2, y2, w2, h2) -> float:
    """两矩形 IoU"""
    ix = max(0, min(x1 + w1, x2 + w2) - max(x1, x2))
    iy = max(0, min(y1 + h1, y2 + h2) - max(y1, y2))
    inter = ix * iy
    union = w1 * h1 + w2 * h2 - inter
    return inter / union if union else 0.0


class IconMultiMatcher:
    """模板匹配 + 阈值 + NMS 多目标。
    返回所有匹配点（每个 = 一个选项气泡），按 Y 升序排列。"""

    def __init__(self, template_path: Path, threshold: float):
        tpl = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            raise FileNotFoundError(f"找不到模板文件: {template_path}")
        self.tpl = tpl
        self.threshold = threshold
        self._cache = {}

    def _scaled(self, s: float):
        if s not in self._cache:
            th, tw = self.tpl.shape[:2]
            nw, nh = max(1, int(tw * s)), max(1, int(th * s))
            self._cache[s] = cv2.resize(self.tpl, (nw, nh), interpolation=cv2.INTER_AREA)
        return self._cache[s]

    def find(self, frame_bgr, roi: Tuple[float, float, float, float]):
        """在 ROI（千分比）内找所有匹配点，返回 [(x, y, w, h, score), ...]（窗口内坐标，按 Y 升序）。
        单尺度匹配：模板按当前帧高度 /1080 缩放，不扫多尺度——
        多尺度会在同一图标的多档缩放上重复命中（实测剧情选项 11 点、凯瑟琳 35 点），
        单尺度每个图标恰好一个匹配点，匹配点数 = 选项数，且消除误匹配源。"""
        h, w = frame_bgr.shape[:2]
        x0, y0 = int(w * roi[0]), int(h * roi[1])
        x1, y1 = int(w * roi[2]), int(h * roi[3])
        gray = cv2.cvtColor(frame_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        scale = h / 1080.0  # 按窗口高度缩放
        t = self._scaled(scale)
        th, tw = t.shape[:2]
        if tw <= gray.shape[1] and th <= gray.shape[0]:
            res = cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res >= self.threshold)
            hits = [(xx, yy, tw, th, float(res[yy, xx])) for yy, xx in zip(ys, xs)]
        else:
            hits = []
        if not hits:
            return []
        # NMS：分数降序，抑制 IoU>=0.5 的邻近候选。
        # 注意：hits 坐标是 ROI 内坐标，NMS 必须在同一坐标系比较，最后再统一加 ROI 偏移。
        hits.sort(key=lambda r: -r[4])
        kept = []
        for x, y, ww, hh, sc in hits:
            if any(_iou(x, y, ww, hh, kx, ky, kw, kh) >= 0.5 for kx, ky, kw, kh, _ in kept):
                continue
            kept.append((x, y, ww, hh, sc))
        kept = [(x + x0, y + y0, ww, hh, sc) for x, y, ww, hh, sc in kept]
        kept.sort(key=lambda r: r[1])  # Y 升序：第一个 = 最上面的选项
        return kept


def detect_option(frame_bgr, option_matcher=None, excl_matcher=None):
    """检测选项：
    1) 感叹号图标存在 → 优先（返回 (True, 'exclamation', 匹配点)）
    2) 选项气泡图标多目标匹配 → 每个匹配点 = 一个选项（返回 (True, 'option', 匹配点列表)）
    3) 都没有 → (False, None, [])
    """
    if excl_matcher is not None:
        ex = excl_matcher.find(frame_bgr, OPTION_ROI)
        if ex:
            return True, "exclamation", ex
    if option_matcher is not None:
        ops = option_matcher.find(frame_bgr, OPTION_ROI)
        if ops:
            return True, "option", ops
    return False, None, []


# ---------------------------- 主循环 ----------------------------
class DialogSkipper:
    def __init__(self):
        self.talk = TemplateMatcher(BASE_DIR / "assets" / TALK_TEMPLATE, TALK_THRESHOLD)
        self.option_matcher = IconMultiMatcher(BASE_DIR / "assets" / OPTION_ICON_TEMPLATE, OPTION_ICON_THRESHOLD)
        self.excl_matcher = IconMultiMatcher(BASE_DIR / "assets" / EXCLAMATION_TEMPLATE, OPTION_ICON_THRESHOLD)
        self.running = False
        self._stop = threading.Event()
        self._thread = None
        self.last_press = 0.0
        self.last_option_click = 0.0
        self.ocr_engine = None       # RapidOCR 懒加载
        self._pauses = None          # 暂停关键词缓存
        self.ocr_cooldown_until = 0.0
        self.pause_until = 0.0

    def _pause_keywords(self):
        """内置暂停关键词 + 脚本目录 pause_options.json 追加（用户可编辑）"""
        if self._pauses is None:
            kws = list(DEFAULT_PAUSE_KEYWORDS)
            try:
                p = BASE_DIR / "data" / PAUSE_OPTIONS_FILE
                if p.exists():
                    extra = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(extra, list):
                        kws += [str(x) for x in extra if x]
            except Exception:
                pass
            self._pauses = kws
        return self._pauses

    def _ocr_texts(self, frame):
        """OCR 选项文字区，返回文字列表。无 RapidOCR 依赖时返回空（退化为直接点第一个）。"""
        if self.ocr_engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
                self.ocr_engine = RapidOCR()
            except Exception:
                return []
        h, w = frame.shape[:2]
        x0, y0 = int(w * OCR_ROI[0]), int(h * OCR_ROI[1])
        x1, y1 = int(w * OCR_ROI[2]), int(h * OCR_ROI[3])
        try:
            res, _ = self.ocr_engine(frame[y0:y1, x0:x1])
            texts = [t for _, t, _ in (res or [])]
            return texts
        except Exception:
            return []

    def toggle(self):
        self.running = not self.running
        import traceback
        stack = traceback.format_stack()
        src = "unknown"
        for line in stack[-4:-1]:
            if "keyboard" in line or "hotkey" in line:
                src = "热键F8"
            elif "pywebview" in line or "js_api" in line or "_UiApi" in line:
                src = "网页按钮"
        print(f"[开关] 已开启：对话自动空格推进，选项自动点击第一个（来源={src}）" if self.running
              else f"[开关] 已停止（来源={src}）")

    def stop(self):
        self.running = False
        self._stop.set()
        # 关闭控制窗（若有）→ webview.start 返回 → 程序退出（F9 热键 / 网页 × 均走此）
        try:
            if getattr(self, "_webview_window", None) is not None:
                self._webview_window.destroy()
                print("[UI] 控制窗已关闭")
        except Exception as e:
            print(f"[警告] 控制窗关闭失败: {e}")

    # ---------------------------- pywebview 控制窗（HTML/CSS 渲染，100% 复刻 ui.html 设计稿） ----------------------------
    # 窗口 = 深色圆角面板：网页背景 #1b1e24 不透明铺满 200x100，四角由 SetWindowRgn 裁圆（14px，
    # 与 .card border-radius 重合）。不用 pywebview 官方 transparent（Windows 不支持且鼠标事件失效）。
    # WS_EX_NOACTIVATE：点击不抢焦点（原神不失焦）；实验已验证点击/JS 交互完全正常。

    class _UiApi:
        """pywebview JS bridge：网页按钮 → Python 动作（toggle/close/drag/get_state）"""

        def __init__(self, owner):
            self._o = owner

        def toggle(self):
            self._o.toggle()

        def close(self):
            self._o.stop()

        def drag(self, dx, dy):
            self._o._ui_drag(dx, dy)

        def get_state(self):
            return self._o.running

    def _build_webview(self):
        """创建 pywebview 置顶无边框小窗（ui.html）。窗口先在屏幕外创建（WebView2 正常渲染，
        用户看不到任何画面），loaded 后加样式/圆角并移到屏幕内 → 首帧即深色圆角面板，无白屏"""
        import webview
        win = webview.create_window(
            "原神跳一跳", url=str(BASE_DIR / "ui.html"), width=184, height=88,
            x=-2000, y=-2000, frameless=True, easy_drag=False, on_top=True,
            js_api=self._UiApi(self))
        self._webview_window = win

        def _on_loaded():
            try:
                form = win.native
                import clr  # noqa: F401（pywebview 依赖 pythonnet，本回调在主线程执行）
                from System.Drawing import Size
                hwnd = form.Handle.ToInt32()
                self._ui_hwnd = hwnd
                GWL_EXSTYLE = -20
                WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x8000000
                style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                style |= WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
                # 窗口四角裁圆（14px；网页 .card border-radius 独立调整，窗口形状不动）
                rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, 201, 101, 14, 14)
                ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
                # 屏幕外 → 屏幕内：loaded 事件触发时 WebView2 首帧可能尚未提交（实测移入瞬间
                # 有 ~50ms 白屏），延迟 250ms 等深色首帧真正画完，再移到屏幕内 → 首帧即深色面板
                import threading
                from System import Action

                def _do_move():
                    try:
                        win.move(120, 120)
                    except Exception:
                        pass

                def _move_later():
                    try:
                        form.BeginInvoke(Action(_do_move))
                    except Exception:
                        _do_move()

                threading.Timer(0.25, _move_later).start()
                print("[UI] 控制窗已就绪（深色圆角面板 200x100，14px）")
            except Exception as e:
                print(f"[警告] 控制窗样式设置失败: {e}")

        win.events.loaded += _on_loaded
        return win

    def _ui_sync(self):
        """检测线程调用：running 变化时同步到网页 JS（F8 热键切换 → UI 跟着变）"""
        if getattr(self, "_ui_state", None) != self.running:
            self._ui_state = self.running
            try:
                if getattr(self, "_webview_window", None) is not None:
                    self._webview_window.evaluate_js(
                        "setState(" + ("true" if self.running else "false") + ")")
            except Exception:
                pass

    def _ui_drag(self, dx, dy):
        """网页拖拽增量移动窗口（Win32 SetWindowPos 直接移窗，不依赖 CLR 属性）"""
        try:
            hwnd = getattr(self, "_ui_hwnd", None)
            if not hwnd:
                return
            r = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
            ctypes.windll.user32.SetWindowPos(
                hwnd, 0, r.left + int(dx), r.top + int(dy), 0, 0,
                0x0004 | 0x0010)  # SWP_NOSIZE | SWP_NOZORDER
        except Exception as e:
            print(f"[警告] 拖动失败: {e}")


    def run(self):
        import keyboard  # 延迟导入，selfcheck 模式不依赖

        keyboard.add_hotkey(HOTKEY_TOGGLE, self.toggle)
        keyboard.add_hotkey(HOTKEY_EXIT, self.stop)
        print(f"原神跳一跳已就绪（F8 开关 / F9 退出 / 置顶控制窗）\n"
              f"对话阈值 {self.talk.threshold:.2f}，选项图标阈值 {OPTION_ICON_THRESHOLD}，按键间隔 {MIN_INTERVAL*1000:.0f}ms，"
              f"暂停关键词 {len(self._pause_keywords())} 条")

        # 检测主循环 → 后台线程；主线程跑 pywebview UI 事件循环（阻塞直到窗口关闭）
        import threading
        self._detect_thread = threading.Thread(target=self._detect_loop, daemon=True)
        self._detect_thread.start()

        try:
            import webview
            self._build_webview()
            webview.start(debug=False)
        except Exception as e:
            print(f"[警告] 控制窗启动失败（{e}），仅使用 F8/F9 热键")
            self._stop.wait()
        print("已退出")

    def _detect_loop(self):
        """检测主循环（后台线程）：前台检查 → 截图 → 对话/选项判定 → 空格/点击；同步 UI 状态"""
        import mss
        sct = mss.mss()
        debug = "--debug" in sys.argv
        last_log = 0.0
        # 启动时清空旧的诊断图（diag 仅 debug 排查用，不留历史，避免积累占空间）
        if debug:
            try:
                d = BASE_DIR / "data" / "diag"
                if d.exists():
                    for f in d.glob("*.png"):
                        f.unlink(missing_ok=True)
            except Exception:
                pass
        while not self._stop.is_set():
            # UI 状态同步（F8 热键切换 → 网页开关跟着变）
            self._ui_sync()
            # 前台检查：原神不在前台 → 完全不动作
            if not is_genshin_foreground():
                if debug and time.time() - last_log >= 1.0:
                    last_log = time.time()
                    print(f"[debug] 原神不在前台，跳过本帧（进程={get_foreground_process_name()} "
                          f"标题={get_foreground_window_title()}）开关={'开' if self.running else '关'}")
                time.sleep(POLL_INTERVAL)
                continue

            # 窗口定位：截【窗口客户区】而非全屏，ROI 全部按窗口内坐标计算
            # （窗口化时游戏画面不占满屏幕，全屏百分比 ROI 会全部错位）
            rect = get_active_genshin_client_rect()
            if rect is None:
                time.sleep(POLL_INTERVAL)
                continue
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            if w <= 0 or h <= 0:
                time.sleep(POLL_INTERVAL)
                continue

            try:
                shot = sct.grab({"left": rect[0], "top": rect[1], "width": w, "height": h})
                frame = np.asarray(shot)[:, :, :3]  # BGRA -> BGR
            except Exception as e:
                print(f"[错误] 截图失败: {e}")
                time.sleep(1)
                continue

            is_talk, tv = self.talk.detect(frame, TALK_ROI)
            now = time.time()

            if is_talk and self.running:
                has_opt, opt_type, matches = detect_option(
                    frame, self.option_matcher, self.excl_matcher)
                now = time.time()

                if has_opt:
                    # 有选项：OCR 检查是否命中暂停关键词（凯瑟琳/铁匠/阿圆/声望等 NPC 功能菜单）
                    if now >= self.ocr_cooldown_until:
                        texts = self._ocr_texts(frame)
                        self.ocr_cooldown_until = now + OCR_COOLDOWN
                        paused = any(
                            any(k in t for k in self._pause_keywords()) for t in texts)
                        if paused:
                            self.pause_until = now + OCR_PAUSE_DURATION
                            if debug:
                                print(f"[debug] {time.strftime('%H:%M:%S')} 选项文字命中暂停关键词"
                                      f"（OCR: {texts[:3]}…）→ 本场景静默，不点不空格")
                        else:
                            self.pause_until = 0
                    # 静默期内：不点选项也不按空格（NPC 功能菜单）
                    if now < self.pause_until:
                        pass
                    # 非静默：感叹号优先点感叹号；气泡则点第一个（最上面）气泡
                    elif now - self.last_option_click >= OPTION_CLICK_COOLDOWN:
                        m = matches[0]  # Y 升序 → 第一个 = 最上面
                        if opt_type == "option":
                            # 点击选项行中部偏前：图标右缘 + 2% 窗口宽（进入文字区前部）。
                            # 不要用固定大偏移：图标匹配点位置随模板而定（新模板在 x~66%），
                            # 固定 25% 窗口宽会把点击点推到选项文字尾部。
                            cx = m[0] + m[2] + int(w * 0.02)
                            cy = m[1] + m[3] // 2
                        else:
                            cx, cy = m[0] + m[2] // 2, m[1] + m[3] // 2
                        click_abs(cx + rect[0], cy + rect[1])
                        self.last_option_click = now
                        if debug:
                            print(f"[debug] {time.strftime('%H:%M:%S')} 检测到选项({opt_type},{len(matches)}个) "
                                  f"→ 点击第一个 ({cx + rect[0]},{cy + rect[1]}) 分数={m[4]:.3f}")
                else:
                    # 气泡消失 → 场景已变，解除暂停静默
                    if now < self.pause_until:
                        self.pause_until = 0
                    # 对话文本：空格推进（200ms 限频）
                    if now - self.last_option_click >= OPTION_CLICK_COOLDOWN and now - self.last_press >= MIN_INTERVAL:
                        tap_key(SPACE_SC)
                        self.last_press = now
                        if debug:
                            print(f"[debug] {time.strftime('%H:%M:%S')} 对话中(自动={tv:.3f}) → 按空格推进")

                # 诊断图：debug 模式下，凡检测到选项就存档（绿框=选项气泡匹配点）
                if debug and has_opt and time.time() - last_log >= 1.0:
                    last_log = time.time()
                    try:
                        os.makedirs(BASE_DIR / "data" / "diag", exist_ok=True)
                        anno = frame.copy()
                        for m in matches:
                            cv2.rectangle(anno, (m[0], m[1]), (m[0] + m[2], m[1] + m[3]), (0, 255, 0), 2)
                        cv2.imwrite(str(BASE_DIR / "data" / "diag" / f"opt_{int(time.time())}.png"), anno)
                    except Exception:
                        pass

            if debug and time.time() - last_log >= 1.0:
                last_log = time.time()
                has_opt, opt_type, matches = detect_option(frame, self.option_matcher, self.excl_matcher) if is_talk else (False, None, [])
                best = f"{max(m[4] for m in matches):.3f}" if matches else "-"
                state = "选项界面" if (is_talk and has_opt) else ("对话文本" if is_talk else "非对话")
                print(f"[debug] 自动={tv:.3f} 选项={'有' if has_opt else '无'}({opt_type or '-'},{len(matches)},最高分={best}) "
                      f"状态={state} 窗口=({w}x{h}@{rect[0]},{rect[1]}) 开关={'开' if self.running else '关'}")
            time.sleep(POLL_INTERVAL)



# ---------------------------- 自检模式（不注入按键） ----------------------------
def selfcheck():
    """用真实截图验证判定 + OCR 暂停关键词决策链。样本放在项目 samples\ 目录（4 张 1280x720 全屏截图）"""
    samples_dir = BASE_DIR / "samples"
    samples = [
        ("选项界面(应→点选项)", samples_dir / "option.png", "OPTION"),
        ("对话无选项(应→空格)", samples_dir / "talk.png", "TALK"),
        ("非对话(应→无动作)", samples_dir / "normal.png", "NORMAL"),
        ("凯瑟琳菜单(应→静默)", samples_dir / "pause.png", "PAUSE"),
    ]
    missing = [p for _, p, _ in samples if not p.exists()]
    if missing:
        print("自检样本缺失（samples\\ 目录下没有对应截图），请准备 4 张 1280x720 原神全屏截图放入 samples\\：")
        print("  talk.png    对话进行中（无选项气泡，左上角有「自动」按钮）")
        print("  option.png  对话选项界面（有选项气泡）")
        print("  normal.png  非对话（大世界/菜单，无对话框）")
        print("  pause.png   凯瑟琳每日委托奖励界面（多选项菜单）")
        print("放好后重新运行 --selfcheck")
        return 1
    talk = TemplateMatcher(BASE_DIR / "assets" / TALK_TEMPLATE, TALK_THRESHOLD)
    ok = True
    opt_matcher = IconMultiMatcher(BASE_DIR / "assets" / OPTION_ICON_TEMPLATE, OPTION_ICON_THRESHOLD)
    excl_matcher = IconMultiMatcher(BASE_DIR / "assets" / EXCLAMATION_TEMPLATE, OPTION_ICON_THRESHOLD)
    sk = DialogSkipper()
    for name, path, expect in samples:
        img = cv2.imread(str(path))
        if img is None:
            print(f"{name}: 样本读取失败 {path} → ✗")
            ok = False
            continue
        is_talk, tv = talk.detect(img, TALK_ROI)
        has_opt, opt_type, matches = detect_option(img, opt_matcher, excl_matcher) if is_talk else (False, None, [])
        paused = False
        if is_talk and has_opt:
            texts = sk._ocr_texts(img)
            paused = any(any(k in t for k in sk._pause_keywords()) for t in texts)
        if expect == "PAUSE":
            state = "PAUSE" if paused else "OPTION"
        else:
            state = "OPTION" if (is_talk and has_opt and not paused) else ("TALK" if is_talk else "NORMAL")
        hit = (state == expect)
        ok = ok and hit
        print(f"{name}: 自动={tv:.3f} 选项={'有' if has_opt else '无'}({opt_type or '-'},{len(matches)}) "
              f"{'暂停=命中' if paused else '暂停=无'} 判定={state} {'✓' if hit else '✗ 与预期不符'}")
    print("自检" + ("通过 ✓" if ok else "未通过 ✗ 请检查阈值"))
    return 0 if ok else 1


if __name__ == "__main__":
    import traceback
    LOG = BASE_DIR / "data" / "skip_debug.log"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S")
                    + f" 启动 admin={ctypes.windll.shell32.IsUserAnAdmin() and '是' or '否'} args={sys.argv[1:]}\n")
    except Exception:
        pass
    if "--selfcheck" in sys.argv:
        sys.exit(selfcheck())
    if not ensure_admin():   # 非管理员 → 弹 UAC 提权重启，本进程退出
        sys.exit(0)
    try:
        DialogSkipper().run()
    except Exception:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                traceback.print_exc(file=f)
        except Exception:
            pass
        raise
