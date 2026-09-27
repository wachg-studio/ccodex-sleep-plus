@echo off
rem ccodex-sleep-plus 启动脚本：最小化窗口运行网关，自动接入 Codex 并打开面板
cd /d %~dp0
start "ccodex-sleep-plus" /min python sleep_plus.py serve
