#!/bin/bash
# 飞递 Feidi - macOS/Linux 启动脚本

cd "$(dirname "$0")"

echo ""
echo "=================== 飞递 Feidi ==================="
echo ""
echo "  正在启动传输服务..."
echo ""

# 尝试用 python3 或 python 启动
if command -v python3 &> /dev/null; then
    python3 transfer.py "$@"
elif command -v python &> /dev/null; then
    python transfer.py "$@"
else
    echo ""
    echo "[错误] 未找到可用的 Python 3.9+，请先安装"
    echo "macOS 可用: brew install python3"
    echo "Linux 可用: sudo apt install python3"
    echo ""
    read -p "按 Enter 退出..."
    exit 1
fi

echo ""
echo "飞递 Feidi 已停止。"
read -p "按 Enter 退出..."
