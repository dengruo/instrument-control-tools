"""
本机控制接口（给脚本等外部程序用）—— 两个控制程序共用

  · 只监听 127.0.0.1（本机），局域网里的其他电脑连不上
  · 协议：HTTP + JSON
        GET  /            → 程序名、是否允许外部控制、可用方法列表
        POST /call        → {"method": "名字", "args": {...}}
                          ← {"ok": true, "result": ...}  或  {"ok": false, "error": "原因"}
  · 真正和仪器打交道的事情由各程序自己排队执行（不会和界面的操作同时发命令）
  · 界面上可以随时取消“允许外部控制”，之后的请求一律拒绝

客户端见 C:\\测试工具\\remote.py
"""
import json
import math
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import numpy as np
except ImportError:          # pragma: no cover
    np = None

GEN_PORT = 17601
SCOPE_PORT = 17602
PSU_PORT = 17603


def to_jsonable(x):
    """把 numpy 数组、NaN 等转换成 JSON 能表示的东西"""
    if np is not None:
        if isinstance(x, np.ndarray):
            return [to_jsonable(v) for v in x.tolist()]
        if isinstance(x, np.generic):
            x = x.item()
    if isinstance(x, float):
        return None if (math.isnan(x) or math.isinf(x)) else x
    if isinstance(x, dict):
        return {str(k): to_jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [to_jsonable(v) for v in x]
    return x


class ControlServer:
    def __init__(self, name, port, dispatch, methods, log=None):
        """
        dispatch(method, args) -> 结果；出错时抛异常（异常文字会原样返回给调用方）
        methods: {方法名: 一句说明}
        log(text): 把收到的请求写到界面日志（会在 HTTP 线程里调用，界面那边要自己转到主线程）
        """
        self.name, self.port, self.dispatch, self.methods, self.log = name, port, dispatch, methods, log
        self.enabled = True
        self.httpd = None
        self.error = None

    def start(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):          # 不往控制台打访问日志
                pass

            def _send(self, code, obj):
                body = json.dumps(to_jsonable(obj), ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send(200, {"name": server.name, "enabled": server.enabled, "methods": server.methods})

            def do_POST(self):
                if self.path.rstrip("/") != "/call":
                    return self._send(404, {"ok": False, "error": "只支持 POST /call"})
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    req = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                    method, args = req.get("method"), req.get("args") or {}
                except Exception as e:
                    return self._send(400, {"ok": False, "error": f"请求格式不对：{e}"})
                if not server.enabled:
                    return self._send(423, {"ok": False, "error": "界面上已关闭“允许后台控制”，请求被拒绝"})
                if method not in server.methods:
                    return self._send(404, {"ok": False, "error": f"没有这个方法：{method}",
                                            "methods": list(server.methods)})
                try:
                    result = server.dispatch(method, args)
                    self._send(200, {"ok": True, "result": result})
                except Exception as e:
                    detail = traceback.format_exc().strip().splitlines()[-1]
                    self._send(200, {"ok": False, "error": str(e) or detail})

        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        except OSError as e:
            self.error = f"端口 {self.port} 被占用或无法监听：{e}"
            return False
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return True

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass


def call_on(post, fn, timeout=600):
    """把 fn 交给另一个线程执行（post(callable) 负责排队），等它做完再把结果/异常带回来"""
    done = threading.Event()
    box = {}

    def wrapper():
        try:
            box["result"] = fn()
        except BaseException as e:   # noqa: BLE001
            box["error"] = e
        finally:
            done.set()
    post(wrapper)
    if not done.wait(timeout):
        raise TimeoutError(f"{timeout} 秒内没有完成")
    if "error" in box:
        raise box["error"]
    return box.get("result")
