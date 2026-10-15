"""Read-before-write Play internal activation of an already-signed bundle.

One edit per attempt; retry after a lost commit response discovers the committed
bundle/hash/track instead of allocating another version or uploading blindly.
No production track changes, key material files, or signing operations.
"""
import json
import os

PACKAGE = "com.hobbycoders.aishuffle"
BASE = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/" + PACKAGE
UPLOAD = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications/" + PACKAGE


class Play:
    def __init__(self, session=None):
        if os.environ.get("PLAY_SIGNING_READY") != "true" or os.environ.get("ANDROID_WORKERS_READY") != "true":
            raise ValueError("Play signing enrollment and compatible workers must be confirmed")
        if session is None:
            from google.oauth2.service_account import Credentials
            from google.auth.transport.requests import AuthorizedSession
            credentials = Credentials.from_service_account_info(
                json.loads(os.environ["GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"]),
                scopes=["https://www.googleapis.com/auth/androidpublisher"])
            session = AuthorizedSession(credentials)
        self.session = session

    def request(self, method, url, **kwargs):
        response = self.session.request(method, url, timeout=180, **kwargs)
        if not 200 <= response.status_code < 300:
            raise ValueError(f"Play {method} failed (HTTP {response.status_code}); retry the same promotion")
        return response.json() if response.content else {}

    def inspect(self, edit, record):
        code = record["play"]["version_code"]
        digest = next(a["digest"][7:] for a in record["assets"] if a["id"] == record["play"]["asset_id"])
        bundles = self.request("GET", f"{BASE}/edits/{edit}/bundles").get("bundles", [])
        bundle = next((b for b in bundles if int(b["versionCode"]) == code), None)
        if bundle and bundle.get("sha256", "").lower() != digest:
            raise ValueError("Play version code already belongs to different bytes")
        tracks = self.request("GET", f"{BASE}/edits/{edit}/tracks").get("tracks", [])
        for track in tracks:
            for release in track.get("releases", []):
                if any(int(c) > code for c in release.get("versionCodes", [])):
                    raise ValueError("Obsolete Play version cannot be activated")
        internal = next((t for t in tracks if t["track"] == "internal"), {})
        complete = any(r.get("status") == "completed" and r.get("versionCodes") == [str(code)]
                       for r in internal.get("releases", []))
        return bundle, bool(bundle and complete)

    def matches(self, record):
        edit = self.request("POST", BASE + "/edits", json={})["id"]
        try:
            _, complete = self.inspect(edit, record)
            return complete
        finally:
            self.request("DELETE", f"{BASE}/edits/{edit}")

    def write(self, record, path):
        edit = self.request("POST", BASE + "/edits", json={})["id"]
        bundle, complete = self.inspect(edit, record)
        if complete:
            self.request("DELETE", f"{BASE}/edits/{edit}")
            return
        if bundle is None:
            with path.open("rb") as data:
                bundle = self.request("POST", f"{UPLOAD}/edits/{edit}/bundles?uploadType=media",
                                      data=data, headers={"Content-Type": "application/octet-stream"})
            expected = next(a["digest"][7:] for a in record["assets"] if a["id"] == record["play"]["asset_id"])
            if int(bundle["versionCode"]) != record["play"]["version_code"] or bundle.get("sha256", "").lower() != expected:
                raise ValueError("Play upload read-back does not match staged bundle/code")
        self.request("PUT", f"{BASE}/edits/{edit}/tracks/internal", json={"track": "internal", "releases": [
            {"name": record["tag"], "versionCodes": [str(record["play"]["version_code"])], "status": "completed"}]})
        self.request("POST", f"{BASE}/edits/{edit}:validate", json={})
        self.request("POST", f"{BASE}/edits/{edit}:commit", json={})
        # protocol.activate performs a NEW edit/GET read-back even after success.
