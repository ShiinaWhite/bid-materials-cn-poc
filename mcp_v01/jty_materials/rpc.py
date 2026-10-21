"""Bounded stdio JSON-RPC client. Children are always reaped."""
import json
import os
import queue
import subprocess
import threading


class Rpc:
    def __init__(self, command, env=None, timeout=15, cwd=None):
        self.timeout = timeout
        self.id = 0
        self.messages = queue.Queue()
        self.errors = []
        self.p = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env, cwd=cwd,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        def read():
            for line in self.p.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    self.errors.append("invalid server JSON")
            self.messages.put(None)
        def errors():
            for line in self.p.stderr:
                self.errors.append(line.decode("utf-8", "replace").strip())
        threading.Thread(target=read, daemon=True).start()
        threading.Thread(target=errors, daemon=True).start()
        try:
            self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "jty-local-check", "version": "0.1.0"}})
            self.call("notifications/initialized", {}, notification=True)
        except BaseException:
            self.close()
            raise

    def call(self, method, params=None, notification=False):
        self.id += 1
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            msg["id"] = self.id
        self.p.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        self.p.stdin.flush()
        if notification:
            return
        while True:
            answer = self.messages.get(timeout=self.timeout)
            if answer is None:
                raise RuntimeError("MCP process exited: " + str(self.errors[-3:]))
            if answer.get("id") == msg["id"]:
                if "error" in answer:
                    raise ValueError(str(answer["error"]))
                return answer["result"]

    def tool(self, name, arguments):
        result = self.call("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise ValueError(str(result.get("content")))
        if "structuredContent" in result:
            return result["structuredContent"]
        return json.loads(result["content"][0]["text"])

    def close(self):
        if self.p.poll() is None:
            self.p.stdin.close()
            try:
                self.p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.p.terminate()
                self.p.wait(timeout=5)
        self.p.stdout.close()
        self.p.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
