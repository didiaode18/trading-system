@echo off
REM 可视化平台启动脚本
echo ========================================
echo 量化交易系统可视化平台 V7.0
echo ========================================
echo.
echo 8大页面模块：
echo   1. 驾驶舱总览
echo   2. 因子监控面板
echo   3. 风控实时大屏
echo   4. 选股可视化
echo   5. 持仓管理
echo   6. 回测分析中心
echo   7. 策略生命周期
echo   8. 数据中心
echo.
echo 启动中...
echo.

cd /d "%~dp0"
streamlit run trading_system/dashboard/app.py

pause
