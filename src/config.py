"""
Configuration loader.  Reads config.json and provides typed access.
"""

import json
import logging
import os

log = logging.getLogger(__name__)


def _strip_json_comments(text: str) -> str:
    """Strip // line comments and /* */ block comments from JSONC text.

    String-aware: only strips comment markers found outside of JSON string
    literals, so "http://example.com" and the like are left untouched.
    """
    out = []
    i = 0
    n = len(text)
    in_string = False
    escape = False

    while i < n:
        ch = text[i]

        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue

        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue

        out.append(ch)
        i += 1

    return "".join(out)


class NetworkConfig:
    def __init__(self, data: dict):
        self.name: str = data["name"]           # e.g. "libera"
        self.host: str = data["host"]
        self.port: int = data.get("port", 6697)
        self.tls: bool = data.get("tls", True)
        self.nick: str = data["nick"]
        self.username: str = data.get("username", data["nick"])
        self.realname: str = data.get("realname", "Gitbot")
        self.password: str = data.get("password", "")
        self.sasl_password: str = data.get("sasl_password", "")
        self.channels: list = data.get("channels", [])
        self.admins: list = data.get("admins", [])   # nick!user@host masks
        self.command_prefix: str = data.get("command_prefix", "!")


class ShlinkConfig:
    def __init__(self, data: dict):
        self.url: str = data.get("url", "")           # e.g. "https://s.example.com"
        self.api_key: str = data.get("api_key", "")   # Shlink REST API key

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_key)


class WebhookConfig:
    def __init__(self, data: dict):
        self.host: str = data.get("host", "127.0.0.1")
        self.port: int = data.get("port", 8765)
        self.secret: str = data.get("secret", "")     # optional HMAC secret


class Config:
    def __init__(self, path: str):
        self.path = path
        with open(path) as f:
            data = json.loads(_strip_json_comments(f.read()))

        self.networks = [NetworkConfig(n) for n in data.get("networks", [])]
        self.webhook = WebhookConfig(data.get("webhook", {}))
        self.shlink = ShlinkConfig(data.get("shlink", {}))
        self.db_path: str = data.get("db_path", "data/gitbot.db")
        self.rss_interval: int = data.get("rss_interval", 300)   # seconds
        self.log_level: str = data.get("log_level", "INFO")
        self.auth_password: str = data.get("auth_password", "")  # global session auth password

    def reload(self):
        self.__init__(self.path)
