# 「原神跳一跳」打包 EXE 全流程

> 目标：把 skip.py 打成可分发 exe（给别人用，无需装 Python / 无需配环境）。
> 状态：白屏已修复、功能已验收。以下按顺序执行，睡醒后从阶段 0 开始。

---

## 阶段 0：代码适配（已部分完成，打包时还需一步）

目录已重组为 `assets\`（模板图片）+ `data\`（pause_options.json / skip_debug.log / diag）+ `samples\`（自检截图），代码路径已全部切换。**打包时**还需最后一步：

1. **BASE_DIR 双模定位**（PyInstaller 打包后才需要，源码模式无需）：
   ```python
   BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
   ```
   `data\` 和 `samples\` 目录已天然外置（exe 同目录），`pause_options.json` / 日志 / diag 均已写入 `data\`，符合外置设计；改完 BASE_DIR 后跑 `python skip.py --selfcheck` 确认无回归，再进阶段 1。

---

## 阶段 1：干净打包环境（别用 conda base，体积会爆炸）

```powershell
# 用 python 3.9 建独立 venv（打包产物干净、体积小）
D:\Miniconda3\python.exe -m venv D:\genshin-skip-build\venv
D:\genshin-skip-build\venv\Scripts\activate

pip install opencv-python mss keyboard rapidocr-onnxruntime==1.4.4 Pillow pywebview==6.2.1 pyinstaller
```

> 依赖与运行时完全一致：opencv-python / mss / keyboard / rapidocr-onnxruntime 1.4.4 / Pillow / pywebview 6.2.1（自带 pythonnet）。

---

## 阶段 2：打包（PyInstaller）

用 onedir 还是 onefile：

| 模式 | 优点 | 缺点 |
|---|---|---|
| **onedir**（推荐） | 启动快、pywebview/pythonnet 兼容性最好、排错容易 | 是一个文件夹，分发时整文件夹打包 zip |
| onefile | 单文件好分发 | 启动慢（每次解压）、pythonnet/pywebview 偶发兼容问题、杀软误报率更高 |

建议先用 onedir 打通，需要单文件再说。

### 打包命令（onedir 版）

```powershell
pyinstaller --noconfirm --clean --name "原神跳一跳" --windowed ^
  --icon icon.ico ^
  --add-data "ui.html;." ^
  --add-data "assets\disabled_ui.png;assets" ^
  --add-data "assets\icon_option.png;assets" ^
  --add-data "assets\icon_exclamation.png;assets" ^
  --add-data "data\pause_options.json;data" ^
  --hidden-import webview.platforms.winforms ^
  --hidden-import rapidocr_onnxruntime ^
  skip.py
```

要点说明：
- **`--windowed`**：无控制台窗口（否则每次启动闪黑框）
- **`--add-data`**：网页 UI、`assets\` 3 个模板 png、`data\pause_options.json` 默认文件打进去（`pause_options.json` 用户在 exe 旁 `data\` 里改，打包的是内置默认）
- **pywebview/pythonnet**：如果报缺模块，补 `--hidden-import`；pywebview 官方打包说明要求 pythonnet 的 DLL 处理（打包后实测，缺什么补什么）
- **rapidocr**：`rapidocr_onnxruntime` 自带 onnx 模型在包内，PyInstaller 一般自动收集；若报模型找不到，补 `--collect-data rapidocr_onnxruntime`
- **图标**：做一个 256×256 的 .ico（可以用 ui 的深色面板风格）

### 管理员权限

两种方式二选一（**推荐方式 A**）：
- **方式 A（manifest，推荐）**：PyInstaller 加 `--uac-admin` → exe 双击直接弹 UAC，跳过代码自提权那段
- **方式 B（保留现状）**：不加 --uac-admin，靠代码里 ensure_admin() 弹 UAC 重启自己（已验证可用）

> 注意：原神必须管理员权限才收得到注入按键，所以 exe 必须带管理员运行，这是硬需求。

---

## 阶段 3：打包产物测试清单（缺一不可）

在 **没有装 Python 的机器（或干净虚拟机）** 上测：

1. 在 exe 同目录建 `samples\` 放入 4 张原神全屏截图（`talk.png` 对话中 / `option.png` 选项界面 / `normal.png` 非对话 / `pause.png` 凯瑟琳界面），运行 `原神跳一跳.exe --selfcheck` → 应输出"自检通过 ✓"（此时会弹 UAC，正常）
2. 双击 exe → 无控制台黑框、无白屏、深色圆角面板出现、可拖动
3. F8 开关 / F9 退出生效（管理员下热键才能收）
4. 真机进原神实测三态：对话空格推进 / 选项自动点击 / 凯瑟琳菜单静默
5. 改 `data\pause_options.json` 加关键词 → 重启后生效（验证外置生效）
6. `data\diag\` 出现诊断图、`data\skip_debug.log` 有启动日志（验证外置写入）

---

## 阶段 4：分发

- **onedir**：把 `dist\原神跳一跳\` 整个文件夹打成 zip（内含 exe + _internal）
- 附一份 **使用说明.txt**（写给使用者）：
  - 需要 Windows 10/11（自带 WebView2 运行时；Win7/8 需装 WebView2 Runtime）
  - 首次运行会弹 UAC 管理员确认，必须点是
  - F8 开/关、F9 退出；游戏前台时才工作
  - 暂停关键词在 `data\pause_options.json` 可自行编辑
  - 杀软可能误报（keyboard + 键鼠注入 + 管理员权限），需加白名单或信任

---

## 风险与注意

- **杀软误报**：本项目特征（键盘钩子/模拟键鼠/提权）极易被 360、火绒等报毒，这是此类工具的常态，不是打包 bug；需在使用说明里提前告知
- **pywebview + pythonnet 打包兼容**：这是 PyInstaller 的老大难，阶段 2 若报错按"缺什么补什么"处理（hidden-import / collect-data / 手动拷 DLL）
- **路径含中文**：exe 名"原神跳一跳"和资源路径含中文，PyInstaller 一般没问题，但打包机和工作目录尽量避免带空格/中文的深层路径
- **onefile 体积**：rapidocr 模型 + opencv + pythonnet 会让单文件约 150-300MB，属正常

---

## 待办（睡醒后找我，从阶段 0 开始）

- [ ] 阶段 0：BASE_DIR / pause_options / 日志外置三处适配 + selfcheck 回归
- [ ] 阶段 1：建 venv 装依赖
- [ ] 阶段 2：PyInstaller 打包（先 onedir）
- [ ] 阶段 3：产物测试清单逐项过
- [ ] 阶段 4：zip 分发 + 使用说明
