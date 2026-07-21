@echo off
chcp 65001 >nul
echo ================================================
echo   高胜率A股交易操作系统 - 一键迁移初始化
echo ================================================
echo.
echo [1/3] 安装Python依赖...
pip install -r requirements.txt -q
echo.
echo [2/3] 初始化系统（数据库+历史数据）...
python setup.py
echo.
echo [3/3] 完成！
echo.
echo 使用说明:
echo   1. 编辑 trading_system\holdings.json 填入你的持仓
echo   2. 运行: python -m trading_system.main
echo.
pause
