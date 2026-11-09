#!/usr/bin/env python3
"""data-slim License 校验模块（随 skill 分发，仅验证，不能签发）

验证用户在 Pro 功能入口出示的 License Key，返回 tier / 有效期。
零联网：完全本地用 Ed25519 公钥验证签名，不打电话、不泄露用户信息。

重要边界（详见 PRO_PLAN.md）：
- 本地校验是「软控制」——懂技术的用户可 patch 掉校验调用；
  要「硬锁」必须走 B 通道（服务端 run 执行重逻辑，用户拿不到代码）。
- 现有 L1–L4 永久免费，不追溯收费；Pro 仅面向未来新增的高级能力。
"""
import base64
import datetime
import json
import os
import sys

try:
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    from cryptography.exceptions import InvalidSignature
except ImportError:
    raise SystemExit("[data-slim] License 校验需要 cryptography: pip install cryptography")

# 默认公钥（随 skill 发布时由 license_issue.py 生成并嵌入）。
# 若同目录存在 public_key.pem 则以文件为准（便于换钥不改代码）。
DEFAULT_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEA/rIOzXf/7D90pl8uzO5Wxah0TnVTu7Rj6no0NpfvBSQ=
-----END PUBLIC KEY-----
"""

TIERS = ("free", "pro", "team")


class LicenseError(Exception):
    pass


def _load_pub() -> bytes:
    here = os.path.dirname(os.path.abspath(__file__))
    f = os.path.join(here, "public_key.pem")
    if os.path.exists(f):
        with open(f, "rb") as fh:
            return fh.read()
    return DEFAULT_PUBLIC_KEY.encode()


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def verify(license_key: str) -> dict:
    """验证 License Key，返回 payload dict {tier,email,iat,exp,nonce}。

    失败抛 LicenseError（签名无效 / 过期 / 格式错 / 等级未知）。
    """
    if not license_key or "." not in license_key:
        raise LicenseError("License Key 格式不正确")
    payload_b64, sig_b64 = license_key.split(".", 1)
    try:
        payload_bytes = _b64url_decode(payload_b64)
        sig = _b64url_decode(sig_b64)
    except Exception:
        raise LicenseError("License Key 解码失败")
    try:
        pub = load_pem_public_key(_load_pub())
        pub.verify(sig, payload_b64.encode())
    except InvalidSignature:
        raise LicenseError("License Key 签名无效（伪造或被篡改）")
    except Exception:
        raise LicenseError("公钥加载失败")
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        raise LicenseError("License Key 内容损坏")
    exp = payload.get("exp")
    if exp:
        try:
            exp_d = datetime.datetime.strptime(exp, "%Y-%m-%d").date()
            if exp_d < datetime.date.today():
                raise LicenseError(f"License Key 已于 {exp} 过期")
        except ValueError:
            raise LicenseError("License Key 有效期格式错误")
    if payload.get("tier") not in TIERS:
        raise LicenseError("License Key 等级未知")
    return payload


def tier_of(license_key: str) -> str:
    """便捷：返回 tier 字符串；无效 / 过期 / 缺失一律降级为 'free'。"""
    try:
        return verify(license_key).get("tier", "free")
    except LicenseError:
        return "free"


if __name__ == "__main__":
    # 命令行自检：python3 license.py "<key>"  -> 打印 tier/有效期/状态
    if len(sys.argv) < 2:
        print("用法: python3 license.py \"<license_key>\"")
        sys.exit(1)
    key = sys.argv[1]
    try:
        p = verify(key)
        print(f"[data-slim] License 有效 | tier={p['tier']} | email={p.get('email')} | 到期={p['exp']}")
    except LicenseError as e:
        print(f"[data-slim] License 无效: {e}")
        sys.exit(2)
