"""
预警确认(ACK)回调服务器 + CLI工具
===================================
功能:
  1. 轻量HTTP服务器: 监听localhost:9876，处理钉钉ActionCard按钮回调
  2. CLI工具: 命令行手动确认预警

用法:
  # 启动回调服务器(后台运行)
  python -m trading_system.notify.alert_ack_server serve

  # CLI手动确认
  python -m trading_system.notify.alert_ack_server confirm 159611 "R8-趋势级别下降"
  python -m trading_system.notify.alert_ack_server status
  python -m trading_system.notify.alert_ack_server list
"""

import os
import sys
import json
import logging
import argparse
import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# 确保项目根目录在sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

try:
    from notify.alert_ack import (
        confirm_by_token, record_ack, get_ack_record, get_ack_summary,
        _load_state, _today_str, cleanup_expired_tokens
    )
except ImportError:
    from trading_system.notify.alert_ack import (
        confirm_by_token, record_ack, get_ack_record, get_ack_summary,
        _load_state, _today_str, cleanup_expired_tokens
    )

logger = logging.getLogger(__name__)


# ============================================================
# 一、HTTP回调服务器
# ============================================================

class ACKHandler(BaseHTTPRequestHandler):
    """处理钉钉ActionCard按钮回调"""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/ack/status":
            state = _load_state()
            self._respond(200, state)
        elif path.startswith("/ack/"):
            token = path.split("/ack/")[-1].strip("/")
            if token == "all":
                # "全部已处理"按钮 —— 需从参数获取标的列表
                codes = params.get("codes", [""])[0]
                if codes:
                    results = []
                    for code in codes.split(","):
                        code = code.strip()
                        if code:
                            state = _load_state()
                            pending = state.get("pending_tokens", {})
                            # 找到该标的最新令牌
                            for t, info in pending.items():
                                if info.get("code") == code:
                                    result = confirm_by_token(t)
                                    results.append(result)
                                    break
                    self._respond(200, {"msg": "批量确认完成", "results": results})
                else:
                    self._respond(200, {"msg": "请在钉钉中点击对应标的的确认按钮"})
            else:
                result = confirm_by_token(token)
                if result.get("success"):
                    # 返回成功页面(可在浏览器中显示)
                    self._respond_html(200, f"""
                    <html><head><meta charset="utf-8"><title>预警确认</title></head>
                    <body style="font-family:'Microsoft YaHei';text-align:center;padding:50px">
                        <h1 style="color:#52c41a">✅ 确认成功</h1>
                        <p style="font-size:18px">{result.get('code')} {result.get('rule')}</p>
                        <p style="color:#666">当日该标的同规则预警已静默，不再重复推送</p>
                        <p style="color:#999;font-size:12px;margin-top:30px">此页面可关闭</p>
                    </body></html>
                    """)
                else:
                    self._respond_html(200, f"""
                    <html><head><meta charset="utf-8"><title>确认失败</title></head>
                    <body style="font-family:'Microsoft YaHei';text-align:center;padding:50px">
                        <h1 style="color:#faad14">⚠️ {result.get('msg')}</h1>
                        <p style="color:#666">请使用命令行工具手动确认</p>
                        <p style="color:#999;font-size:12px">python -m trading_system.notify.alert_ack_server confirm &lt;code&gt; &lt;rule&gt;</p>
                    </body></html>
                    """)
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _respond_html(self, code, html):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        logger.info(f"[ACK回调] {args[0]}")


def start_ack_server(host="0.0.0.0", port=9876):
    """启动ACK回调服务器(阻塞)"""
    try:
        server = HTTPServer((host, port), ACKHandler)
        logger.info(f"[ACK] 回调服务器启动: http://{host}:{port}/ack/<token>")
        print(f"[ACK] 回调服务器已启动: http://{host}:{port}/ack/<token>")
        print(f"[ACK] 按 Ctrl+C 停止")
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("[ACK] 回调服务器停止")
        print("\n[ACK] 服务器已停止")
    except Exception as e:
        logger.error(f"[ACK] 回调服务器异常: {e}")
        print(f"[ACK] 启动失败: {e}")


# ============================================================
# 二、CLI工具
# ============================================================

def cli_confirm(args):
    """CLI: 手动确认预警"""
    code = args.code
    rule = args.rule
    level = args.level or "critical"
    urgency = args.urgency or 75

    ok = record_ack(code, rule, level, urgency)
    if ok:
        print(f"[OK] 已确认: {code} {rule} (级别:{level}, 紧急度:{urgency})")
        print(f"   当日该标的同规则预警将静默，不再重复推送")
    else:
        print(f"[FAIL] 确认失败，请检查日志")


def cli_status(args):
    """CLI: 查看今日ACK状态"""
    state = _load_state()
    today = _today_str()

    if state.get("date") != today:
        print(f"[{today}] 暂无确认记录")
        return

    ack_records = state.get("ack_records", {})
    if not ack_records:
        print(f"[{today}] 暂无确认记录")
        return

    print(f"今日预警确认记录 ({today}):")
    print("-" * 60)
    for code, info in ack_records.items():
        print(f"  {code}: {info.get('rule', '')} | "
              f"级别:{info.get('level', '')} | "
              f"紧急度:{info.get('urgency_score', 0)} | "
              f"确认时间:{info.get('ack_time', '')} | "
              f"已处理:{info.get('ack_qty', 0)}股")
    print("-" * 60)

    # 待处理令牌
    pending = state.get("pending_tokens", {})
    if pending:
        print(f"\n待确认令牌: {len(pending)}个")
        for token, info in list(pending.items())[:5]:
            print(f"  {token}: {info.get('code', '')} {info.get('rule', '')} "
                  f"(过期:{info.get('expires', '')})")


def cli_list_pending(args):
    """CLI: 列出待确认令牌"""
    state = _load_state()
    pending = state.get("pending_tokens", {})
    cleanup_expired_tokens()

    if not pending:
        print("无待确认令牌")
        return

    print(f"待确认令牌 ({len(pending)}个):")
    for token, info in pending.items():
        print(f"  {token} -> {info.get('code', '')} {info.get('rule', '')} "
              f"[{info.get('level', '')}] 紧急度{info.get('urgency_score', 0)}")


def cli_serve(args):
    """CLI: 启动回调服务器"""
    host = args.host or "127.0.0.1"
    port = args.port or 9876
    start_ack_server(host, port)


def main():
    parser = argparse.ArgumentParser(description="预警确认(ACK)工具")
    sub = parser.add_subparsers(dest="command")

    # confirm
    p_confirm = sub.add_parser("confirm", help="手动确认预警")
    p_confirm.add_argument("code", help="标的代码(如159611)")
    p_confirm.add_argument("rule", help="规则名称(如R8-趋势级别下降)")
    p_confirm.add_argument("--level", default="critical", help="预警级别")
    p_confirm.add_argument("--urgency", type=int, default=75, help="紧急度")

    # status
    sub.add_parser("status", help="查看今日确认状态")

    # list
    sub.add_parser("list", help="列出待确认令牌")

    # serve
    p_serve = sub.add_parser("serve", help="启动回调服务器")
    p_serve.add_argument("--host", default="0.0.0.0", help="监听地址(0.0.0.0接受外部连接)")
    p_serve.add_argument("--port", type=int, default=9876, help="监听端口")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.command == "confirm":
        cli_confirm(args)
    elif args.command == "status":
        cli_status(args)
    elif args.command == "list":
        cli_list_pending(args)
    elif args.command == "serve":
        cli_serve(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
