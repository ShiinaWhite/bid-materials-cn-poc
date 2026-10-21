@echo off
rem 投标素材库 WebUI 启动脚本（Windows）
rem 双击运行：约 2 秒后自动用默认浏览器打开 http://127.0.0.1:8800/
rem 关闭本窗口（或按 Ctrl+C）即停止服务。
rem 如需局域网访问：把下面的 HOST 改为 0.0.0.0（请自行评估网络安全边界）。
setlocal
cd /d "%~dp0"
set HOST=127.0.0.1
set PORT=8800
echo 正在启动投标素材库 WebUI (http://%HOST%:%PORT%/)
echo 关闭本窗口即停止服务。
start "" /min cmd /c "timeout /t 2 /nobreak >nul & start http://%HOST%:%PORT%/"
python -B mcp_v01\webui_server.py --host %HOST% --port %PORT%
echo.
echo 服务已停止。
pause
