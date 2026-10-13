#!/usr/bin/env python3
"""Export the existing Android key encrypted exclusively for Play enrollment.

Passwords never appear in logs; PEPK needs password arguments on the ephemeral
runner, so its output and exceptions are deliberately not echoed. The artifact
contains only Google's encryptedPrivateKey payload and the public certificate.
"""
import argparse
import base64
import hashlib
import os
from pathlib import Path
import ssl
import subprocess
import zipfile

PEPK_SHA256 = "aaccc0774b240aa5304bdad2a49865e92f229ca73209ecb6eaafe75dc858e24e"
RECIPIENT_SHA256 = "271491bb884e99b7e4fa209c1c5bb4a2e14b6cc3a93cfae13fa96ce00befa527"
SIGNING_SHA256 = "04d5c1d60ec49baba61e0201a003c6a0b5f68d9a9e146c5ee5cddbbb343757c2"


def run_quiet(args):
    try:
        result = subprocess.run(args, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Signing tool could not run; no credentials were logged") from None
    if result.returncode:
        raise RuntimeError("Signing tool failed; output withheld to protect credentials")
    return result.stdout


def certificate_hash(pem):
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem.decode("ascii"))).hexdigest()


def export_key(pepk, recipient, work, *, recipient_hash=RECIPIENT_SHA256,
               signing_hash=SIGNING_SHA256):
    required = ("KEYSTORE_BASE64", "KEYSTORE_PASSWORD", "KEY_ALIAS", "KEY_PASSWORD")
    if any(not os.environ.get(name) for name in required):
        raise ValueError("All four existing Android signing secrets are required")
    if hashlib.sha256(pepk.read_bytes()).hexdigest() != PEPK_SHA256:
        raise ValueError("PEPK digest mismatch; refusing to expose signing secrets to an unreviewed tool")
    if hashlib.sha256(recipient.read_bytes()).hexdigest() != recipient_hash:
        raise ValueError("Recipient digest mismatch; refusing export to a different encryption key")
    # Dedicated scratch directory, never a source-tree or home directory.
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    keystore = work / "signing.jks"
    archive = work / "encrypted-key.zip"
    # Do not overwrite another invocation's output or signing material.
    if keystore.exists() or archive.exists():
        raise ValueError("Enrollment scratch files already exist; clean them before retrying")
    try:
        with keystore.open("xb") as stream:
            os.chmod(keystore, 0o600)
            encoded = "".join(os.environ["KEYSTORE_BASE64"].split())
            stream.write(base64.b64decode(encoded, validate=True))
        certificate = run_quiet([
            "keytool", "-exportcert", "-rfc", "-keystore", str(keystore),
            "-storepass:env", "KEYSTORE_PASSWORD", "-alias", os.environ["KEY_ALIAS"],
        ])
        if certificate_hash(certificate) != signing_hash:
            raise ValueError("Signing certificate does not match the existing released app")
        run_quiet([
            "java", "-jar", str(pepk), "--keystore=" + str(keystore),
            "--alias=" + os.environ["KEY_ALIAS"],
            "--keystore-pass=" + os.environ["KEYSTORE_PASSWORD"],
            "--key-pass=" + os.environ["KEY_PASSWORD"],
            "--output=" + str(archive), "--include-cert", "--rsa-aes-encryption",
            "--encryption-key-path=" + str(recipient),
        ])
        with zipfile.ZipFile(archive) as z:
            if sorted(z.namelist()) != ["certificate.pem", "encryptedPrivateKey"]:
                raise ValueError("Unexpected PEPK archive contents")
            if certificate_hash(z.read("certificate.pem")) != signing_hash:
                raise ValueError("Exported certificate does not match the existing app")
            encrypted = z.read("encryptedPrivateKey")
            if len(encrypted) < 512 or b"PRIVATE KEY" in encrypted:
                raise ValueError("PEPK encryption output failed validation")
        os.chmod(archive, 0o600)
        return archive
    except Exception:
        archive.unlink(missing_ok=True)
        raise
    finally:
        keystore.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pepk", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    args = parser.parse_args()
    recipient = Path(__file__).with_name("play-encryption-public-key.pem")
    try:
        export_key(args.pepk, recipient, args.work)
    except Exception:
        # Neither exception repr nor chained command arguments may reach CI logs.
        print("Play key export failed. Verify the pinned tool, recipient, existing signing secrets, and empty scratch directory.")
        return 1
    print("Encrypted enrollment archive verified. No unencrypted private key is exported.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
