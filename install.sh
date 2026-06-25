#!/bin/bash
# Whisper TransWithAI 一键安装 (macOS)
# 用法: ./install.sh   (在本仓库目录里运行)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "==> 安装目录: $DIR"

# 1) Homebrew
if ! command -v brew >/dev/null 2>&1; then
  echo "✗ 未检测到 Homebrew。请先安装(粘贴到终端):"
  echo '   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  echo "安装完成后重新运行本脚本。"
  exit 1
fi
BREW_PREFIX="$(brew --prefix)"

# 2) 系统依赖: ffmpeg(转码/合成) + python@3.13 与 tkinter(Tk 9, 界面)
echo "==> 安装 ffmpeg 和 python-tk@3.13 (已装会跳过)..."
brew install ffmpeg python-tk@3.13

# 3) 找到带 tkinter 的 python3.13
PY313="$BREW_PREFIX/opt/python@3.13/bin/python3.13"
[ -x "$PY313" ] || PY313="$BREW_PREFIX/bin/python3.13"
[ -x "$PY313" ] || PY313="$(command -v python3.13 || true)"
[ -x "$PY313" ] || { echo "✗ 找不到 python3.13"; exit 1; }
echo "==> 使用 Python: $PY313"
"$PY313" -c "import tkinter" 2>/dev/null || { echo "✗ 该 python 缺 tkinter, 请确认 python-tk@3.13 已装"; exit 1; }

# 4) 创建虚拟环境并安装依赖
echo "==> 创建虚拟环境 $DIR/.venv 并安装依赖(约几分钟)..."
"$PY313" -m venv "$DIR/.venv"
"$DIR/.venv/bin/python" -m pip install --upgrade pip -q
"$DIR/.venv/bin/python" -m pip install -r "$DIR/requirements.txt"

# 5) 生成命令行包装(自定位, 不写死路径)
make_wrapper() {  # $1=文件名 $2=目标py
  cat > "$DIR/$1" <<EOF
#!/bin/bash
D="\$(cd "\$(dirname "\$0")" && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:\$PATH"
exec "\$D/.venv/bin/python" "\$D/$2" "\$@"
EOF
  chmod +x "$DIR/$1"
}
make_wrapper sub subtitle.py
make_wrapper subtitle-gui subtitle_gui.py

# 6) 生成 .app(放到 ~/Applications, 免 sudo; Launchpad/Spotlight 可见)
APP="$HOME/Applications/Whisper TransWithAI.app"
echo "==> 生成应用: $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
[ -f "$DIR/AppIcon.icns" ] && cp "$DIR/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Whisper TransWithAI</string>
  <key>CFBundleDisplayName</key><string>Whisper TransWithAI</string>
  <key>CFBundleIdentifier</key><string>com.transwithai.whisper</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>run</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
EOF
cat > "$APP/Contents/MacOS/run" <<EOF
#!/bin/bash
exec "$DIR/subtitle-gui"
EOF
chmod +x "$APP/Contents/MacOS/run"
LSR="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
[ -x "$LSR" ] && "$LSR" -f "$APP" >/dev/null 2>&1 || true

echo ""
echo "✅ 安装完成!"
echo "   • 在「应用程序 / Launchpad / Spotlight」找:Whisper TransWithAI"
echo "   • 或终端运行:$DIR/subtitle-gui"
echo "   • 首次处理日语视频会自动下载模型(约 3GB),请联网等待。"
