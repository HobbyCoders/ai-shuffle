"""An in-memory account hub implementing docs/network/hub-api.md.

Routed in tests by patching ``app.core.account_hub.call`` so the real
``app.network.hub`` client code runs against it. Stores exactly what the
real hub stores: device descriptions, the feed of ciphertext envelopes,
pending approvals and the public recovery wrap. It never sees a key.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from fastapi import HTTPException


class FakeHub:
    def __init__(self, *, quota_ops: Optional[int] = None):
        self.network: Optional[dict] = None
        self.devices: Dict[str, dict] = {}
        self.feed: List[dict] = []
        self.seen: set = set()
        self.approvals: Dict[str, dict] = {}
        self.recovery: Optional[dict] = None
        self.quota_ops = quota_ops
        self.calls: List[tuple] = []
        self.combined_poll = True
        # Which server id is talking: tests set this before switching workers.
        self.current_server: str = "server-a"

    # -- transport shim -------------------------------------------------
    def call(self, binding, method, suffix, body=None, *, credential=None, max_bytes=16384, timeout=None):
        server = binding["server_id"]
        self.calls.append((server, method, suffix))
        assert suffix.startswith("network"), suffix
        route = suffix[len("network"):].lstrip("/")
        path, _, query = route.partition("?")
        params = dict(p.split("=") for p in query.split("&") if p)
        handler = {
            ("POST", ""): self.register,
            ("GET", ""): self.info,
            ("GET", "pending"): self.pending,
            ("GET", "approval"): self.approval,
            ("POST", "ops"): self.push,
            ("GET", "feed"): self.pull,
            ("POST", "recovery"): self.set_recovery,
        }.get((method, path))
        if handler is None:
            if method == "POST" and path.startswith("devices/") and path.endswith("/approve"):
                return self.approve(server, path.split("/")[1], body or {})
            if method == "POST" and path.startswith("devices/") and path.endswith("/reject"):
                return self.reject(server, path.split("/")[1])
            raise HTTPException(503, "Account hub authorization unavailable")
        return handler(server, body or {}, params)

    # -- routes -----------------------------------------------------------
    def register(self, server, body, params):
        device = body["device"]
        device_id = device["device_id"]
        if self.network is None:
            if not body.get("network_id"):
                self.network = {"network_id": None, "founder_device_id": None, "generation": 0, "name": None}
                self.devices[device_id] = {**device, "server_id": server, "status": "pending"}
                return {"status": "pending", "network_hub_id": None}
            self.network = {"network_id": body["network_id"], "founder_device_id": body["founder_device_id"],
                            "generation": int(body.get("generation") or 0), "name": None}
            self.devices[device_id] = {**device, "server_id": server, "status": "active"}
            if body.get("recovery"):
                self.recovery = body["recovery"]
            return {"status": "active", "network_hub_id": "nh_1", "quota": self._quota()}
        if body.get("network_id") and self.network["network_id"] is None:
            # A pending worker founded the account's network locally and now names it.
            self.network.update({"network_id": body["network_id"], "founder_device_id": body["founder_device_id"],
                                 "generation": int(body.get("generation") or 0)})
        if body.get("network_id") and body["network_id"] == self.network["network_id"]:
            self.devices[device_id] = {**device, "server_id": server, "status": "active"}
            self.network["generation"] = max(self.network["generation"], int(body.get("generation") or 0))
            if body.get("recovery"):
                self.recovery = body["recovery"]
            return {"status": "active", "network_hub_id": "nh_1", "quota": self._quota()}
        if body.get("network_id"):
            # A worker with its own network cannot enter another account's network unasked.
            return {"status": "conflict", "network_id": self.network["network_id"], "network_hub_id": "nh_1"}
        existing = self.devices.get(device_id)
        if existing and existing["status"] == "active" and not body.get("network_id"):
            # Enrolled before but lost its state: needs approval again.
            existing["status"] = "pending"
        if existing is None:
            self.devices[device_id] = {**device, "server_id": server, "status": "pending"}
        elif existing["status"] == "approved":
            return {"status": "approved", "network_hub_id": "nh_1"}
        return {"status": self.devices[device_id]["status"], "network_hub_id": "nh_1"}

    def info(self, server, body, params):
        return {"network_id": (self.network or {}).get("network_id"), "name": (self.network or {}).get("name"),
                "founder_device_id": (self.network or {}).get("founder_device_id"),
                "generation": (self.network or {}).get("generation", 0), "recovery": self.recovery,
                "devices": [self._public(d) for d in self.devices.values()], "cursor_head": len(self.feed),
                "quota": self._quota()}

    def pending(self, server, body, params):
        return {"devices": [self._public(d) for d in self.devices.values() if d["status"] == "pending"]}

    def approval(self, server, body, params):
        mine = [d for d in self.devices.values() if d["server_id"] == server]
        if not mine:
            return {"status": "unknown"}
        device = mine[-1]
        response = self.approvals.pop(device["device_id"], None)
        if response is not None:
            device["status"] = "active"
            return {"status": "approved", "response": response}
        return {"status": device["status"]}

    def approve(self, server, device_id, body):
        device = self.devices.get(device_id)
        if device is None or device["status"] != "pending":
            raise HTTPException(404, "No such pending device")
        device["status"] = "approved"
        self.approvals[device_id] = body["response"]
        return {"ok": True}

    def reject(self, server, device_id):
        device = self.devices.get(device_id)
        if device is not None:
            device["status"] = "rejected"
        return {"ok": True}

    def push(self, server, body, params):
        keys = {(env['origin_device_id'], env['origin_incarnation'], env['origin_seq']) for env in body.get('ops') or []}
        if self.quota_ops is not None and len(self.feed) + len(keys - self.seen) > self.quota_ops:
            return {"accepted": 0, "cursor": len(self.feed), "quota_exceeded": True,
                    "quota_reason": "ops", "quota": self._quota()}
        accepted = 0
        for env in body.get("ops") or []:
            key = (env["origin_device_id"], env["origin_incarnation"], env["origin_seq"])
            if key in self.seen:
                continue
            if self.quota_ops is not None and len(self.feed) >= self.quota_ops:
                return {"accepted": accepted, "cursor": len(self.feed), "quota_exceeded": True, "quota": self._quota()}
            self.seen.add(key)
            self.feed.append(json.loads(json.dumps(env)))
            accepted += 1
        return {"accepted": accepted, "cursor": len(self.feed), "quota": self._quota()}

    def pull(self, server, body, params):
        cursor = int(params.get("cursor", 0))
        limit = int(params.get("limit", 500))
        page = self.feed[cursor:cursor + limit]
        new_cursor = cursor + len(page)
        result = {"ops": page, "cursor": new_cursor, "more": new_cursor < len(self.feed)}
        if self.combined_poll and params.get("include_pending") == "1":
            result.update(pending_devices=self.pending(server, body, params)["devices"], quota=self._quota())
        return result

    def set_recovery(self, server, body, params):
        self.recovery = body["recovery"]
        return {"ok": True}

    # -- helpers ---------------------------------------------------------------
    def _quota(self):
        return {"ops": len(self.feed), "limit": self.quota_ops}

    @staticmethod
    def _public(device):
        return {k: device.get(k) for k in ("device_id", "signing_public", "encryption_public", "name", "platform",
                                            "kind", "status", "incarnation", "version", "legacy_instance_id")}
