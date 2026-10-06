#!/usr/bin/env python3
import importlib.util
import json
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT_PATH = ROOT / "telegram_flights_fast_bot.py"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _RequestsShim:
    class exceptions:
        class Timeout(Exception):
            pass

    @staticmethod
    def get(url, params=None, timeout=35, **kwargs):
        try:
            if params:
                url = f"{url}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return _Resp(payload)
        except socket.timeout as exc:
            raise _RequestsShim.exceptions.Timeout(str(exc)) from exc

    @staticmethod
    def post(url, json=None, timeout=15, **kwargs):
        try:
            body = None
            headers = {}
            if json is not None:
                body = __import__("json").dumps(json).encode("utf-8")
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = __import__("json").loads(resp.read().decode("utf-8"))
            return _Resp(payload)
        except socket.timeout as exc:
            raise _RequestsShim.exceptions.Timeout(str(exc)) from exc


def load_bot_module():
    spec = importlib.util.spec_from_file_location("telegram_flights_fast_bot_runtime", BOT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    module = load_bot_module()
    module.requests = _RequestsShim
    module.main()


if __name__ == "__main__":
    main()
