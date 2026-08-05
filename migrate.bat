@echo off
chcp 65001 >nul
echo ================================================
echo   操盘密码A股交易操作系统 - 一键迁移初始化
echo   详细步骤见 migrate_guide.md
echo ================================================
echo.
echo [1/4] 安装Python依赖...
pip install -r requirements.txt -q
echo.
echo [2/4] 初始化系统（数据库+历史数据）...
python setup.py
echo.
echo [3/4] 环境自检...
python setup_check.py
echo.
echo [4/4] 完成！
echo.
echo 使用说明:
echo   1. 复制 trading_system\config_local.example.py 为 config_local.py 并填写邮箱授权码
echo   2. 编辑 holdings.json 填入你的持仓
echo   3. 运行: python run.py status  查看系统状态
echo   4. 运行: python run.py auto    启动自动调度
echo.
pause
