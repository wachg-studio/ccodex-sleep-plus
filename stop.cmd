@echo off
rem ccodex-sleep-plus 停止脚本：恢复 Codex 配置并结束网关进程
cd /d %~dp0
python sleep_plus.py stop
pause
