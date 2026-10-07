#!/usr/bin/env python3
"""POSTOFFICE_PANEL_BASE_URL：面板对外地址（Red 先行，写于实现之前）。

冻结契约：
  启动校验：`postoffice panel` 读取环境变量 POSTOFFICE_PANEL_BASE_URL。
    - 未设置或空字符串 → 当作未设置，面板照常启动（仍然只绑 127.0.0.1）；
    - 必须是带 scheme+host 的 http/https URL；没有 scheme、ftp:// 等其他 scheme、
      带 user:pass@ 身份信息、只有 scheme 没有 host → 面板进程立即以非零退出，
      stderr 给出包含 "BASE_URL" 的中文短句。
  POST 鉴权 Origin 矩阵（设置了 BASE_URL 时）：
    允许 = {无 Origin 头, http://127.0.0.1:<port>, http://localhost:<port>,
            由 BASE_URL 推出的精确 origin（scheme://host，非默认端口才带 :port）}
    其余一律 403：scheme 不匹配、后缀伪装（evil-<host>）、别的 host、缺端口的裸 host、
    端口不对的同 host。
  面板仍然只绑 127.0.0.1，启动行打印 http://127.0.0.1:<port>/。
  深链是纯客户端行为：服务端只保证 /api/letter 对深链携带的 box+id 照常可用。

本文件只写测试不改实现，预期在当前树上失败（RED）：现在的面板完全忽略该环境变量
（坏值照常启动、外来 origin 一律 403）；纯回归锁除外。
全部在 mktemp 的临时 POSTOFFICE_HOME 里跑，只连 127.0.0.1，绝不碰真实数据目录。
"""
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path

PO = str(Path(__file__).resolve().parent.parent / "postoffice")
BASE = Path(tempfile.mkdtemp(prefix="po_panelO_"))

BAD_BASE_URLS = {
    "没有 scheme": "test-machine.ts.net",
    "ftp scheme": "ftp://test-machine.ts.net",
    "带用户凭据": "https://user:pass@test-machine.ts.net",
    "只有 scheme 没有 host": "https://",
}

CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def env_for(home, **extra):
    env = dict(os.environ)
    env["POSTOFFICE_HOME"] = str(home)
    env["POSTOFFICE_NO_NOTIFY"] = "1"
    env["POSTOFFICE_POLL"] = "1"
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env.pop("CLAUDE_CODE_HOST_SESSION_ID", None)
    env.pop("POSTOFFICE_PANEL_BASE_URL", None)      # 缺省保证未设置；要测就显式传
    env.pop("POSTOFFICE_SLACK_WEBHOOK", None)
    env.update(extra)
    return env


def run_po(*args, home, stdin=None, timeout=60):
    return subprocess.run([sys.executable, PO, *args], capture_output=True, text=True,
                          env=env_for(home), input=stdin, timeout=timeout)


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class BaseUrlStartup(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp(prefix="h_", dir=BASE))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def spawn_panel(self, base_url):
        port = free_port()
        proc = subprocess.Popen(
            [sys.executable, PO, "panel", "--no-open", "--port", str(port)],
            env=env_for(self.home, POSTOFFICE_PANEL_BASE_URL=base_url),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return proc, port

    def wait_exit(self, proc, secs=10):
        deadline = time.time() + secs
        while time.time() < deadline:
            if proc.poll() is not None:
                return proc.returncode
            time.sleep(0.1)
        return None

    def test_malformed_base_url_exits_non_zero_with_a_chinese_message(self):
        for name, value in BAD_BASE_URLS.items():
            with self.subTest(case=name, value=value):
                proc, port = self.spawn_panel(value)
                try:
                    rc = self.wait_exit(proc)
                    if rc is None:
                        proc.kill()
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait()
                _out, err = proc.communicate()
                if rc is None:
                    self.fail(f"BASE_URL={value!r} 必须被拒绝，"
                              f"面板进程却一直活着（被当成了未设置？）")
                self.assertNotEqual(rc, 0, f"BASE_URL={value!r} 必须非零退出")
                err = err.decode("utf-8", errors="replace")
                self.assertIn("BASE_URL", err, f"错误消息必须点名 BASE_URL：{err!r}")
                self.assertRegex(err, CJK_RE, f"错误消息必须是中文短句：{err!r}")

    def test_empty_base_url_is_treated_as_unset(self):
        proc, port = self.spawn_panel("")
        try:
            deadline = time.time() + 15
            ready = False
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                    conn.request("GET", "/api/state")
                    resp = conn.getresponse()
                    body = resp.read()
                    conn.close()
                    if resp.status == 200:
                        ready = True
                        break
                except OSError:
                    time.sleep(0.2)
            self.assertTrue(ready, "空字符串必须当作未设置：面板要照常起来")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc.communicate()          # 关掉管道，免得 ResourceWarning


class BaseUrlOriginMatrix(unittest.TestCase):
    """BASE_URL=https://test-machine.ts.net（默认端口，origin 不带 :port）。"""

    maxDiff = None
    BASE_URL = "https://test-machine.ts.net"

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp(prefix="h_", dir=BASE))
        out = run_po("add", "chief", "--notify", "--who", "面板来源测试", home=cls.home)
        if out.returncode != 0:
            raise RuntimeError(out.stdout + out.stderr)
        cls.port = free_port()
        cls.startup = cls.home / "panel_startup.log"
        cls.startup_handle = open(cls.startup, "wb")
        cls.proc = subprocess.Popen(
            [sys.executable, PO, "panel", "--no-open", "--port", str(cls.port)],
            env=env_for(cls.home, POSTOFFICE_PANEL_BASE_URL=cls.BASE_URL),
            stdout=cls.startup_handle, stderr=subprocess.DEVNULL)
        cls.startup_handle.close()          # 只留子进程的写端
        deadline = time.time() + 15
        line_ready = http_ready = False
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                raise RuntimeError("面板进程提前退出")
            if not line_ready:
                line_ready = f"http://127.0.0.1:{cls.port}/" in \
                    cls.startup.read_text(encoding="utf-8", errors="replace")
            if not http_ready:
                try:
                    status, _ = cls.raw("GET", "/api/state")
                    http_ready = status == 200
                except OSError:
                    pass
            if line_ready and http_ready:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("面板没有在 15 秒内起来")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait()
        shutil.rmtree(cls.home, ignore_errors=True)

    # -- helpers ----------------------------------------------------------
    @classmethod
    def raw(cls, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    @classmethod
    def jpost(cls, path, obj, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        status, data = cls.raw("POST", path, body, headers)
        try:
            return status, json.loads(data.decode("utf-8"))
        except ValueError:
            return status, data.decode("utf-8", errors="replace")

    @classmethod
    def jget(cls, path):
        status, data = cls.raw("GET", path)
        try:
            return status, json.loads(data.decode("utf-8"))
        except ValueError:
            return status, data.decode("utf-8", errors="replace")

    # -- tests ------------------------------------------------------------
    def test_panel_still_binds_loopback_and_says_so(self):
        text = self.startup.read_text(encoding="utf-8", errors="replace")
        self.assertIn(f"http://127.0.0.1:{self.port}/", text,
                      f"启动行必须仍然是 127.0.0.1 回环地址：{text!r}")
        status, _ = self.raw("GET", "/api/state")
        self.assertEqual(status, 200, "设置 BASE_URL 后 /api/state 必须经 127.0.0.1 照常可达")

    def test_origin_matrix_for_post_status(self):
        payload = {"name": "chief", "status": "online"}
        allowed = [None,
                   f"http://127.0.0.1:{self.port}",
                   f"http://localhost:{self.port}",
                   self.BASE_URL]
        for origin in allowed:
            with self.subTest(origin=origin):
                status, d = self.jpost("/api/status", payload, origin=origin)
                self.assertEqual(status, 200, f"origin={origin!r} 必须放行：{d}")
                self.assertEqual(d, {"ok": True, "error": ""}, f"origin={origin!r}: {d}")
        denied = ["http://test-machine.ts.net",                # scheme 不匹配
                  "https://evil-test-machine.ts.net",          # 后缀伪装
                  "https://other.ts.net",                      # 别的 host
                  "https://random.example",                    # 随机坏 origin
                  "https://test-machine.ts.net:8443"]          # 同 host 但端口不对
        for origin in denied:
            with self.subTest(origin=origin):
                status, d = self.jpost("/api/status", payload, origin=origin)
                self.assertEqual(status, 403, f"origin={origin!r} 必须拒绝：{d}")
                self.assertIs(d.get("ok"), False, f"origin={origin!r}: {d}")

    def test_deep_link_letter_id_served_by_api_letter(self):
        out = run_po("send", "chief", "chief", "深链对照", "回复",
                     home=self.home, stdin="正文\n")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        lid = re.search(r"编号：(\S+)", out.stdout).group(1)
        status, d = self.jget("/api/letter?box=chief&id=" + urllib.parse.quote(lid, safe=""))
        self.assertEqual(status, 200, d)
        self.assertIs(d["ok"], True)
        self.assertEqual((d["box"], d["id"], d["subject"]), ("chief", lid, "深链对照"))


class BaseUrlWithExplicitPort(unittest.TestCase):
    """BASE_URL 带非默认端口：origin 必须带同样的 :port，裸 host 拒绝。"""

    maxDiff = None
    BASE_URL = "https://panel.example.com:8443"

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp(prefix="h_", dir=BASE))
        out = run_po("add", "chief", "--notify", "--who", "面板端口测试", home=cls.home)
        if out.returncode != 0:
            raise RuntimeError(out.stdout + out.stderr)
        cls.port = free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, PO, "panel", "--no-open", "--port", str(cls.port)],
            env=env_for(cls.home, POSTOFFICE_PANEL_BASE_URL=cls.BASE_URL),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 15
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                raise RuntimeError("面板进程提前退出")
            try:
                status, _ = cls.raw("GET", "/api/state")
                if status == 200:
                    break
            except OSError:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("面板没有在 15 秒内起来")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait()
        shutil.rmtree(cls.home, ignore_errors=True)

    @classmethod
    def raw(cls, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    @classmethod
    def jpost(cls, path, obj, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        status, data = cls.raw("POST", path, body, headers)
        try:
            return status, json.loads(data.decode("utf-8"))
        except ValueError:
            return status, data.decode("utf-8", errors="replace")

    def test_exact_port_origin_allowed_and_bare_host_denied(self):
        payload = {"name": "chief", "status": "online"}
        allowed = [None,
                   f"http://127.0.0.1:{self.port}",
                   f"http://localhost:{self.port}",
                   self.BASE_URL]
        for origin in allowed:
            with self.subTest(origin=origin):
                status, d = self.jpost("/api/status", payload, origin=origin)
                self.assertEqual(status, 200, f"origin={origin!r} 必须放行：{d}")
                self.assertEqual(d, {"ok": True, "error": ""}, f"origin={origin!r}: {d}")
        denied = ["https://panel.example.com",          # 缺端口
                  "http://panel.example.com:8443"]      # scheme 不匹配
        for origin in denied:
            with self.subTest(origin=origin):
                status, d = self.jpost("/api/status", payload, origin=origin)
                self.assertEqual(status, 403, f"origin={origin!r} 必须拒绝：{d}")
                self.assertIs(d.get("ok"), False, f"origin={origin!r}: {d}")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
