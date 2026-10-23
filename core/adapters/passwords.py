"""PasswordHasher 的 scrypt 实现（批次 16a，只用标准库）。格式：scrypt$n$r$p$盐(base64)$哈希(base64)。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

N, R, P, LENGTH = 2 ** 15, 8, 1, 32
_MAXMEM = 64 * 1024 * 1024


class ScryptHasher:
    def __init__(self, *, n: int = N, r: int = R, p: int = P) -> None:
        self._n, self._r, self._p = n, r, p

    def hash(self, password: str) -> str:
        salt = os.urandom(16)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=self._n, r=self._r, p=self._p,
                                dklen=LENGTH, maxmem=_MAXMEM)
        return "$".join(["scrypt", str(self._n), str(self._r), str(self._p),
                         base64.b64encode(salt).decode(), base64.b64encode(digest).decode()])

    def verify(self, password: str, password_hash: str) -> bool:
        try:
            kind, n, r, p, salt, digest = password_hash.split("$")
            if kind != "scrypt":
                return False
            expected = base64.b64decode(digest, validate=True)
            actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt, validate=True), n=int(n),
                                    r=int(r), p=int(p), dklen=len(expected), maxmem=_MAXMEM)
        except (ValueError, TypeError, AttributeError):
            return False
        return hmac.compare_digest(actual, expected)
