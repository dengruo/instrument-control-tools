"""
外部控制客户端 —— 通过本机接口操作正在运行的“信号发生器控制”和“示波器控制”程序

命令行用法：
  python remote.py gen                       列出信号发生器程序支持的方法
  python remote.py scope                     列出示波器程序支持的方法
  python remote.py gen status
  python remote.py gen apply ch=1 func=SIN freq=1000 amp=2      （没给的参数保持现值）
  python remote.py gen output ch=1 on=true
  python remote.py scope measure ch=1 items='["FREQ","VPP"]'
  python remote.py scope set what=trig_source value=CH2
  python remote.py scope set what=ch_scale ch=1 value=0.5
  python remote.py scope screenshot          （返回 PNG 路径）
  python remote.py scope sweep f1=100 f2=100000 n=20 amp=2 cout=2
  参数写成 名字=值；值会按 JSON 解析（数字、true/false、列表），解析不了就当文字。
  加 --full 显示完整的长数组（默认长数组只显示点数和范围）。

在 Python 里用：
  from remote import gen, scope
  gen("apply", ch=1, freq=1000)
  scope("measure", ch=1)
"""
import json
import sys
import urllib.error
import urllib.request

PORTS = {"gen": 17601, "scope": 17602, "psu": 17603}
NAMES = {"gen": "信号发生器控制", "scope": "示波器控制", "psu": "直流电源控制"}


class RemoteError(Exception):
    pass


def _url(target):
    if target not in PORTS:
        raise RemoteError(f"目标只能是 {list(PORTS)}")
    return f"http://127.0.0.1:{PORTS[target]}"


def info(target):
    try:
        with urllib.request.urlopen(_url(target) + "/", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RemoteError(f"连不上“{NAMES[target]}”程序（它开着吗？）：{e.reason}")


def call(target, method, timeout=1900, **args):
    body = json.dumps({"method": method, "args": args}).encode("utf-8")
    req = urllib.request.Request(_url(target) + "/call", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        resp = json.loads(e.read().decode("utf-8") or "{}")
    except urllib.error.URLError as e:
        raise RemoteError(f"连不上“{NAMES[target]}”程序（它开着吗？）：{e.reason}")
    if not resp.get("ok"):
        raise RemoteError(resp.get("error", "未知错误"))
    return resp.get("result")


def gen(method, **args):
    return call("gen", method, **args)


def scope(method, **args):
    return call("scope", method, **args)


def psu(method, **args):
    return call("psu", method, **args)


def _brief(x, full):
    if full:
        return x
    if isinstance(x, list) and len(x) > 20 and all(isinstance(v, (int, float)) or v is None for v in x):
        vals = [v for v in x if v is not None]
        return f"<{len(x)} 个数，最小 {min(vals):.6g}，最大 {max(vals):.6g}>" if vals else f"<{len(x)} 个空值>"
    if isinstance(x, dict):
        return {k: _brief(v, full) for k, v in x.items()}
    if isinstance(x, list):
        return [_brief(v, full) for v in x]
    return x


def _parse(v):
    try:
        return json.loads(v)
    except ValueError:
        return v


def main(argv):
    full = "--full" in argv
    argv = [a for a in argv if a != "--full"]
    if not argv or argv[0] not in PORTS:
        print(__doc__)
        return 1
    target = argv[0]
    try:
        if len(argv) == 1:
            i = info(target)
            print(f"{i['name']}  后台控制：{'允许' if i['enabled'] else '已关闭'}")
            for m, desc in i["methods"].items():
                print(f"  {m:16} {desc}")
            return 0
        args = {}
        for kv in argv[2:]:
            if "=" not in kv:
                raise RemoteError(f"参数要写成 名字=值：{kv}")
            k, v = kv.split("=", 1)
            args[k] = _parse(v)
        res = call(target, argv[1], **args)
        print(json.dumps(_brief(res, full), ensure_ascii=False, indent=1))
        return 0
    except RemoteError as e:
        print(f"失败：{e}")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
