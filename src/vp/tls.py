"""TLS material for `ipps://` profiles: self-signed certs and a local CA.

Two ways to get a certificate the client can be made to accept:

* `self-signed`: one self-signed cert per printer. A client that pins or trusts
  it must be given that exact file (`<tls-dir>/<profile>/cert.pem`), and a client
  that validates against the system store will (correctly) refuse it.
* `ca`: a local CA (`<tls-dir>/ca/ca.pem`) signs a leaf cert per printer. Install
  the CA once on the test device and every profile is trusted.
* `files`: bring your own `cert` and `key` (paths relative to the repo root).

Keys never leave the (gitignored) tls directory. They protect nothing real.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

MODES = ("self-signed", "ca", "files")
DEFAULT_SANS = ("localhost", "127.0.0.1", "::1", "10.0.2.2")  # 10.0.2.2: Android emulator -> host
LEAF_DAYS = 397
CA_DAYS = 3650


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _name(common_name: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "virtual-printer (dev only)"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def _san_entry(value: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(value))
    except ValueError:
        return x509.DNSName(value)


def _write_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)


def _write_cert(path: Path, cert: x509.Certificate) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def ensure_ca(tls_dir: Path) -> tuple[Path, Path]:
    """Create the local CA on first use; return (cert, key) paths."""
    d = tls_dir / "ca"
    cert_p, key_p = d / "ca.pem", d / "ca.key"
    if cert_p.exists() and key_p.exists():
        return cert_p, key_p
    d.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = _name("virtual-printer local CA")
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - dt.timedelta(minutes=5))
        .not_valid_after(_now() + dt.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    _write_key(key_p, key)
    _write_cert(cert_p, cert)
    return cert_p, key_p


def _cert_sans(cert: x509.Certificate) -> set[str]:
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return set()
    return {str(v) for v in ext.get_values_for_type(x509.DNSName)} | {
        str(v) for v in ext.get_values_for_type(x509.IPAddress)
    }


def _reusable(cert_p: Path, key_p: Path, sans: set[str], issuer: x509.Name | None) -> bool:
    """An existing cert is kept (so devices that trust it keep trusting it) while it still fits."""
    if not (cert_p.exists() and key_p.exists()):
        return False
    try:
        cert = x509.load_pem_x509_certificate(cert_p.read_bytes())
    except ValueError:
        return False
    if cert.not_valid_after_utc < _now() + dt.timedelta(days=7):
        return False
    if issuer is not None and cert.issuer != issuer:
        return False
    return sans <= _cert_sans(cert)


def issue(
    tls_dir: Path, profile_name: str, mode: str, extra_sans: list[str] | tuple[str, ...] = ()
) -> tuple[Path, Path]:
    """Create (or reuse) the server cert of one profile; return (cert, key) paths."""
    if mode not in ("self-signed", "ca"):
        raise ValueError(f"cannot issue a certificate for mode {mode!r}")
    sans = {*DEFAULT_SANS, *extra_sans}
    d = tls_dir / profile_name
    cert_p, key_p = d / "cert.pem", d / "key.pem"
    ca_cert = ca_key = None
    if mode == "ca":
        ca_cert_p, ca_key_p = ensure_ca(tls_dir)
        ca_cert = x509.load_pem_x509_certificate(ca_cert_p.read_bytes())
        ca_key = serialization.load_pem_private_key(ca_key_p.read_bytes(), None)
    if _reusable(cert_p, key_p, sans, ca_cert.subject if ca_cert else None):
        return cert_p, key_p
    d.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = _name(f"[VIRTUAL] {profile_name}")
    issuer = ca_cert.subject if ca_cert else subject
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - dt.timedelta(minutes=5))
        .not_valid_after(_now() + dt.timedelta(days=LEAF_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([_san_entry(s) for s in sorted(sans)]), critical=False
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    )
    signer = ca_key if ca_key is not None else key
    assert isinstance(signer, ec.EllipticCurvePrivateKey)
    # strict verifiers (Python 3.13+, Android) want an AKI on the leaf
    builder = builder.add_extension(
        x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical=False
    )
    cert = builder.sign(signer, hashes.SHA256())
    _write_key(key_p, key)
    _write_cert(cert_p, cert)
    return cert_p, key_p


def server_context(cert: Path, key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


def context_for(
    profile: dict, tls_dir: Path, root: Path, extra_sans: list[str] | tuple[str, ...] = ()
) -> ssl.SSLContext | None:
    """The server SSL context a profile asks for (`options.tls`), or None for plain ipp://."""
    spec = profile.get("options", {}).get("tls")
    if not spec:
        return None
    if spec["mode"] == "files":
        return server_context(root / spec["cert"], root / spec["key"])
    cert, key = issue(tls_dir, profile["name"], spec["mode"], extra_sans)
    return server_context(cert, key)
