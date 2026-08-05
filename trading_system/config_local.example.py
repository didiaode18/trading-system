# -*- coding: utf-8 -*-
"""
本地私有配置模板（config_local.example.py）
==============================================
使用方法：
    复制本文件为 config_local.py（同目录），然后填写实际值。
    config_local.py 已加入 .gitignore，不会提交到仓库。

所有敏感信息（邮箱授权码、Webhook、API密钥等）都应放在 config_local.py，
不要写入 config.py。
"""

# ============================================================
# 必填：QQ邮箱SMTP授权码
# 获取方式：登录QQ邮箱 → 设置 → 账户 → POP3/SMTP服务 → 开启 → 生成授权码
# 注意：是16位授权码，不是QQ密码
# ============================================================
EMAIL_AUTH_CODE = "在此填写16位QQ邮箱授权码"

# ============================================================
# 可选：覆盖发件人/收件人（默认使用config.py中的值）
# ============================================================
# EMAIL_SENDER = "your_qq@qq.com"
# EMAIL_RECEIVER = "your_qq@qq.com"
# EMAIL_SMTP_HOST = "smtp.qq.com"
# EMAIL_SMTP_PORT = 465

# ============================================================
# 可选：钉钉/企业微信机器人Webhook
# ============================================================
# DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=xxx"
# WECHAT_WORK_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"

# ============================================================
# 可选：账户资金等个性化覆盖
# ============================================================
# TOTAL_CAPITAL = 500000
# AVAILABLE_CASH = 10000
