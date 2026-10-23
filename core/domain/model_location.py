"""批次 21a：数据档位与服务位置。规格见 docs/spec-38-model-eval.md 第二节。

正式数据（profile = "formal"）只允许发往本地服务；样例数据（"sample"，缺省）可以使用在线模型作开发备用，
界面上显示提示。是否“本地”只按地址判断，不做 DNS 解析：
- 主机为 localhost、回环地址、私有地址（10/8、172.16/12、192.168/16）、链路本地地址、IPv6 唯一本地地址（fc00::/7）；
- 或主机名不含点（局域网内的机器名）、以 .local / .lan / .internal 结尾；
- 或在额外允许的主机名单中（HAINA_LOCAL_HOSTS，逗号分隔，不区分大小写）。
"""
from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

PROFILES = ("sample", "formal")
LOCAL_SUFFIXES = (".local", ".lan", ".internal")


def parse_profile(value: str | None) -> str:
    """None 或去空白后为空 → "sample"；其他值去空白、转小写后必须是 PROFILES 之一，否则 ValueError。"""
    text = (value or "").strip().lower()
    if not text:
        return "sample"
    if text not in PROFILES:
        raise ValueError("HAINA_DATA_PROFILE 只能是 sample 或 formal")
    return text


def parse_hosts(value: str | None) -> frozenset[str]:
    """逗号分隔的主机名单：去空白、转小写、去掉空项。"""
    return frozenset(item.strip().lower() for item in (value or "").split(",") if item.strip())


def url_host(url: str) -> str | None:
    """URL 的主机（小写，IPv6 不带方括号）；没有协议或主机 → None。"""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    return host.lower()


def is_local_host(host: str, extra: Iterable[str] = ()) -> bool:
    host = host.lower().rstrip(".")
    if host == "localhost" or host in {h.lower() for h in extra}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host or host.endswith(LOCAL_SUFFIXES)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback or address.is_link_local:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return any(address in ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    return address in ipaddress.ip_network("fc00::/7")


def is_local_url(url: str, extra: Iterable[str] = ()) -> bool:
    """无法解析出主机的 URL 视为非本地。"""
    host = url_host(url)
    return host is not None and is_local_host(host, extra)


@dataclass(frozen=True)
class LocationReport:
    profile: str
    online: tuple[str, ...]        # 不是本地地址的服务名（按传入顺序）

    @property
    def model_location(self) -> str:
        """供界面显示："online"（有任何在线服务）或 "local"。"""
        return "online" if self.online else "local"

    def problems(self) -> list[str]:
        """正式数据档位下每个在线服务一条错误；样例档位没有错误。"""
        if self.profile != "formal":
            return []
        return [f"正式数据档位只允许本地服务：{name} 不是本地地址（可在 HAINA_LOCAL_HOSTS 中登记局域网主机名）"
                for name in self.online]


def check_locations(profile: str, services: Mapping[str, str | None], extra: Iterable[str] = ()) -> LocationReport:
    """services：服务名（环境变量名）→ URL；值为 None 或空白的跳过（未配置的服务不参与判断）。"""
    hosts = frozenset(h.lower() for h in extra)
    online = tuple(name for name, url in services.items() if url and url.strip() and not is_local_url(url, hosts))
    return LocationReport(parse_profile(profile), online)
