#!/usr/bin/env python3
"""Round-trip the real PEPK producer with disposable signing/recipient keys."""
import base64
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.keywrap import aes_key_unwrap_with_padding
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("exporter", Path(__file__).with_name("export-play-key.py"))
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pepk = Path(os.environ["PEPK_JAR"])
        # The private recipient here is disposable TEST data only. Google's
        # corresponding private key is never available to the workflow.
        cls.recipient_private = rsa.generate_private_key(public_exponent=65537, key_size=3072)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="shuffle-enrollment-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recipient = self.root / "recipient.pem"
        self.recipient.write_bytes(self.recipient_private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        self.recipient_hash = hashlib.sha256(self.recipient.read_bytes()).hexdigest()
        keystore = self.root / "fixture.jks"
        self.password = "disposable=fixture-password"
        env = {**os.environ, "FIXTURE_PASSWORD": self.password}
        subprocess.run([
            "keytool", "-genkeypair", "-noprompt", "-alias", "fixture", "-keyalg", "RSA",
            "-keysize", "2048", "-validity", "2", "-dname", "CN=Disposable test only",
            "-storetype", "JKS", "-keystore", str(keystore),
            "-storepass:env", "FIXTURE_PASSWORD", "-keypass:env", "FIXTURE_PASSWORD",
        ], env=env, check=True, capture_output=True)
        cert = subprocess.run([
            "keytool", "-exportcert", "-rfc", "-alias", "fixture", "-keystore", str(keystore),
            "-storepass:env", "FIXTURE_PASSWORD",
        ], env=env, check=True, capture_output=True).stdout
        self.certificate = x509.load_pem_x509_certificate(cert)
        self.signing_hash = exporter.certificate_hash(cert)
        self.work = self.root / "work"
        self.secrets = {"KEYSTORE_BASE64": base64.b64encode(keystore.read_bytes()).decode(),
                        "KEYSTORE_PASSWORD": self.password, "KEY_PASSWORD": self.password,
                        "KEY_ALIAS": "fixture"}
        self.patch = patch.dict(os.environ, self.secrets)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def export(self, **kwargs):
        return exporter.export_key(self.pepk, self.recipient, self.work,
                                   recipient_hash=self.recipient_hash,
                                   signing_hash=kwargs.get("signing_hash", self.signing_hash))

    def test_real_pepk_round_trip_preserves_signing_key(self):
        archive = self.export()
        self.assertFalse((self.work / "signing.jks").exists())
        self.assertEqual([p.name for p in self.work.iterdir()], ["encrypted-key.zip"])
        with zipfile.ZipFile(archive) as z:
            self.assertEqual(sorted(z.namelist()), ["certificate.pem", "encryptedPrivateKey"])
            self.assertEqual(exporter.certificate_hash(z.read("certificate.pem")), self.signing_hash)
            encrypted = z.read("encryptedPrivateKey")
        rsa_bytes = self.recipient_private.key_size // 8
        aes_key = self.recipient_private.decrypt(encrypted[:rsa_bytes], padding.OAEP(
            mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))
        recovered = serialization.load_der_private_key(
            aes_key_unwrap_with_padding(aes_key, encrypted[rsa_bytes:]), password=None)
        self.assertEqual(recovered.public_key().public_numbers(), self.certificate.public_key().public_numbers())

    def test_wrapped_base64_secret_is_supported(self):
        encoded = self.secrets["KEYSTORE_BASE64"]
        wrapped = "\n".join(encoded[i:i + 76] for i in range(0, len(encoded), 76)) + "\n"
        with patch.dict(os.environ, {"KEYSTORE_BASE64": wrapped}):
            self.assertTrue(self.export().is_file())
        self.assertFalse((self.work / "signing.jks").exists())

    def test_rejects_wrong_signing_identity_and_cleans_up(self):
        with self.assertRaisesRegex(ValueError, "Signing certificate"):
            self.export(signing_hash="0" * 64)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_recipient_is_pinned_before_decoding_secrets(self):
        self.recipient.write_text("replaced recipient")
        with self.assertRaisesRegex(ValueError, "Recipient digest mismatch"):
            self.export()
        self.assertFalse(self.work.exists())

    def test_tool_is_pinned_before_decoding_secrets(self):
        self.pepk = self.root / "tampered.jar"
        self.pepk.write_text("not Google's reviewed PEPK binary")
        with self.assertRaisesRegex(ValueError, "PEPK digest mismatch"):
            self.export()
        self.assertFalse(self.work.exists())

    def test_missing_secret_is_fail_closed(self):
        for name in self.secrets:
            with self.subTest(name=name), patch.dict(os.environ, {name: ""}):
                with self.assertRaisesRegex(ValueError, "All four"):
                    self.export()
        self.assertFalse(self.work.exists())

    def test_wrong_password_never_leaks_and_cleans_up(self):
        with patch.dict(os.environ, {"KEY_PASSWORD": "wrong-secret-password"}):
            with self.assertRaises(RuntimeError) as raised:
                self.export()
            self.assertNotIn(self.password, str(raised.exception))
            self.assertNotIn("wrong-secret-password", str(raised.exception))
        self.assertEqual(list(self.work.iterdir()), [])

    def test_existing_artifact_is_not_overwritten(self):
        self.work.mkdir()
        previous = self.work / "encrypted-key.zip"
        previous.write_bytes(b"preserve existing artifact")
        with self.assertRaisesRegex(ValueError, "already exist"):
            self.export()
        self.assertEqual(previous.read_bytes(), b"preserve existing artifact")


class WorkflowTests(unittest.TestCase):
    def test_manual_private_main_only_and_narrow_artifact(self):
        workflow = yaml.load((ROOT / ".github/workflows/android-play-enrollment.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        inputs = workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(list(inputs), ["confirm_existing_key_export"])
        self.assertEqual(inputs["confirm_existing_key_export"]["default"], "false")
        export = workflow["jobs"]["export"]
        for condition in ("github.event_name == 'workflow_dispatch'", "github.ref == 'refs/heads/main'",
                          "github.event.repository.private", "inputs.confirm_existing_key_export"):
            self.assertIn(condition, export["if"])
        self.assertEqual(export["needs"], "checks")
        artifact = next(s["with"] for s in export["steps"] if s.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(artifact["path"], "${{ runner.temp }}/play-enrollment/encrypted-key.zip")
        self.assertEqual(artifact["retention-days"], "1")
        self.assertEqual(export["steps"][-1]["if"], "always()")
        self.assertIn("signing.jks", export["steps"][-1]["run"])
        self.assertEqual(hashlib.sha256((ROOT / "android/ci/play-encryption-public-key.pem").read_bytes()).hexdigest(), exporter.RECIPIENT_SHA256)
        # PR checks do not receive any real signing secrets.
        self.assertNotIn("secrets.", str(workflow["jobs"]["checks"]))


if __name__ == "__main__":
    unittest.main()
