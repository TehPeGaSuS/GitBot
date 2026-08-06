"""
Configuration loader.  Reads config.json and provides typed access.
"""

import json5
import logging

log = logging.getLogger(__name__)


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
        self.bind: str = data.get("bind", "")   # local IP to bind outbound connection to


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
            data = json5.load(f)

        self.networks = [NetworkConfig(n) for n in data.get("networks", [])]
        self.webhook = WebhookConfig(data.get("webhook", {}))
        self.shlink = ShlinkConfig(data.get("shlink", {}))
        self.db_path: str = data.get("db_path", "data/gitbot.db")
        self.rss_interval: int = data.get("rss_interval", 300)   # seconds
        self.log_level: str = data.get("log_level", "INFO")
        self.auth_password: str = data.get("auth_password", "")  # global session auth password

    def reload(self):
        self.__init__(self.path)
