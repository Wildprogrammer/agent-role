"""Generate a private LAN CA and server certificate; never install system trust."""
from __future__ import annotations

import argparse
import ipaddress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def create(output: Path, hosts: list[str]):
    if not output.is_absolute() or output.exists():
        raise ValueError("请选择尚不存在的绝对输出目录，避免覆盖已有证书")
    names = []
    for host in hosts:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            if not host or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in host):
                raise ValueError("host 必须是 IP 或 DNS 名称") from None
            names.append(x509.DNSName(host))
    if not names:
        raise ValueError("至少需要一个 host")
    now = datetime.now(timezone.utc)
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Lingxi Private LAN CA")])
    root = (x509.CertificateBuilder().subject_name(root_name).issuer_name(root_name)
        .public_key(root_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, None, None), critical=True)
        .sign(root_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Lingxi Mobile")]))
        .issuer_name(root_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=90))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False)
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, None, None), critical=True)
        .sign(root_key, hashes.SHA256()))
    output.mkdir(parents=True)
    for name, value in (("lingxi-ca.crt", root.public_bytes(serialization.Encoding.PEM)),
                        ("server.crt", cert.public_bytes(serialization.Encoding.PEM)),
                        ("server.key", key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))):
        with (output/name).open("xb") as stream:
            stream.write(value)
    # CA private key is deliberately not persisted; regenerate a dedicated CA
    # when replacing this certificate rather than reusing a general trust key.
    print("手机仅安装 lingxi-ca.crt；server.key 只留在电脑。证书有效期90天。")
    print("CA SHA256: " + root.fingerprint(hashes.SHA256()).hex())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--host", action="append", required=True)
    args = parser.parse_args()
    create(args.output_dir, args.host)
