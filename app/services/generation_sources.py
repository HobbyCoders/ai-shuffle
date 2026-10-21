"""On-demand source catalog and execution adapters for unified generation.

Native subscription capability tables are authoritative. An unknown price is
never converted to zero. API accounts without a declared media schema remain
visible but unavailable; discovering a text model is not media verification.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import httpx

from app.core.generation_resolver import Candidate, RunContext, UNKNOWN_PRICE, price_total
from app.db.async_database import run_sync


class Sources:
    def __init__(self, worker, context: RunContext, *, local_only=False):
        self.node = worker
        self.context = context
        self.local_only = local_only
        from app.core.generation_confirmations import Confirmations
        self.confirmations = Confirmations()

    def local_worker(self):
        from app.network import runners
        return runners.self_device_id() or "local"

    async def fal_key(self):
        if self.context.api_user_id:
            from app.core import fal_credential
            return await run_sync(fal_credential.api_key, self.context.api_user_id)
        if self.context.owner:
            from app.mcp.ai_tools_sdk import _get_fal_api_key
            return await run_sync(_get_fal_api_key)
        return None

    async def catalog(self, kind, *, source=None, model=None, query=None, limit=15):
        entries = await self.local_catalog(kind, source=source, model=model, query=query, limit=limit)
        if self.context.owner and not self.local_only:
            from app.network.generation import catalog
            entries.extend(await catalog(kind, source=source, model=model, query=query, limit=limit))
        return entries

    async def local_catalog(self, kind, *, source=None, model=None, query=None, limit=15):
        from app.core import native_generation_settings as native
        from app.db import model_providers
        from app.services.fal_catalog import catalog_service
        from app.services.fal_schema import schema_service

        entries = []
        worker = self.local_worker()
        if source in (None, "fal", "fal.ai", "fal-ai") and await self.fal_key():
            if catalog_service.is_stale:
                await catalog_service.refresh(await self.fal_key())
            models = catalog_service.search(output_type=kind, q=query)
            # Only text generation endpoints belong in these four tools. The
            # editing/transform tools keep their existing schemas and handlers.
            models = [m for m in models if m.category.startswith("text-to-") and (not model or m.endpoint_id == model)]
            for m in models[:max(1, min(limit, 50))]:
                schema = await schema_service.get_schema(m.endpoint_id, await self.fal_key())
                options = {f.name: f.default for f in schema.fields if not f.is_primary_text and f.default is not None} if schema else {}
                price = await self.fal_price(m.endpoint_id, options)
                entries.append(Candidate("fal", "fal-ai", worker, m.endpoint_id, kind, False,
                                         options=options, price=price, schema=schema.to_dict() if schema else None,
                                         available=schema is not None, reason=None if schema else "Model schema is unavailable."))
        if not self.context.owner:
            return entries
        rows = await run_sync(model_providers.list_usable_model_providers)
        for row in rows:
            try:
                provider_kind = native.provider_kind(row)
            except ValueError:
                continue
            name = {"grok": "supergrok", "chatgpt": "chatgpt", "openai": "openai", "xai": "xai"}[provider_kind]
            account = str(row["id"])
            if source and source not in (name, account):
                continue
            capability = native.capabilities(provider_kind).get(kind) or {}
            combination = capability.get("verified_combination")
            if combination and capability.get("available"):
                if model and combination["model"] != model:
                    continue
                entries.append(Candidate(name, account, worker, combination["model"], kind, True,
                                         options={k: v for k, v in combination.items() if k != "model"},
                                         schema=capability))
            elif source or model:
                entries.append(Candidate(name, account, worker, model or "unavailable", kind, False,
                                         available=False, reason=capability.get("reason") or "No verified media schema is available for this account."))
        return entries

    async def fal_price(self, model, options):
        """Read provider pricing; only flat item/request prices are bounded here.

        Per-second, megapixel, token and provider-specific formulas need a real
        estimate adapter. Do not assume a different resolution costs the same.
        """
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                response = await client.get("https://api.fal.ai/v1/models/pricing", params={"endpoint_id": model},
                                            headers={"Authorization": "Key " + (await self.fal_key() or "")})
                if response.status_code != 200:
                    return dict(UNKNOWN_PRICE)
                rows = response.json().get("prices", [])
            row = next((r for r in rows if r.get("endpoint_id") == model), {})
            if row.get("currency") != "USD" or row.get("unit") not in ("image", "request", "inference"):
                return dict(UNKNOWN_PRICE)
            amount = Decimal(str(row["unit_price"]))
            count = options.get("num_images", 1)
            if type(count) is not int or not 1 <= count <= 100 or not amount.is_finite() or amount < 0:
                return dict(UNKNOWN_PRICE)
            return {"currency": "USD", "unit": "item", "amount": str(amount), "total": str(amount * count), "status": "estimated"}
        except (httpx.HTTPError, ValueError, TypeError, KeyError, InvalidOperation):
            return dict(UNKNOWN_PRICE)

    async def prepare(self, candidate, options, prompt):
        if candidate.worker != self.local_worker():
            from app.network.generation import prepare
            return await prepare(candidate, options, prompt, self.context)
        if candidate.source == "fal":
            from app.services.fal_schema import schema_service
            schema = await schema_service.get_schema(candidate.model, await self.fal_key())
            if schema is None:
                raise ValueError("The model's declared options are unavailable.")
            values = schema.validate_options(options, prompt)
            if self.context.chat_mode:
                from app.core.session_modes import chat_mode_read_permitted
                for key, value in values.items():
                    if (isinstance(value, str) and any(hint in key for hint in ("image", "file", "audio", "video"))
                            and value and not value.startswith(("https://", "http://", "data:"))
                            and not chat_mode_read_permitted("Read", {"file_path": value})):
                        raise ValueError("Chat generation may read local media only from chat uploads.")
            values.pop(schema.primary_text_field)
            return replace(candidate, options=values, price=await self.fal_price(candidate.model, values))
        from app.core import native_generation_settings as native
        if "model" in options:
            raise ValueError("Pass the model separately, not in options.")
        values = {**options, "model": candidate.model}
        kind = "grok" if candidate.source == "supergrok" else "chatgpt"
        try:
            if candidate.kind == "image":
                parsed = native.NativeImageSettings.model_validate(values)
                native.validate(kind, parsed)
            elif candidate.kind == "video":
                parsed = native.NativeVideoSettings.model_validate(values)
                native.validate_generation(kind, native.NativeGenerationSettings(image=None, video=parsed))
            else:
                raise ValueError("No verified subscription route for this kind.")
        except ValueError:
            raise ValueError("Unsupported or not yet verified subscription combination.") from None
        return replace(candidate, options=options)

    async def reserve(self, candidate, context):
        if candidate.worker != self.local_worker():
            # The receiving worker must reserve the freshly checked quote;
            # reserving here too would count the same remote submission twice.
            return True
        from app.network.generation import reserve
        return await reserve(price_total(candidate.price))

    async def execute(self, candidate, prompt, context):
        try:
            if candidate.worker != self.local_worker():
                from app.network.generation import execute
                return await execute(candidate, prompt, context)
            return await self.execute_local(candidate, prompt, context)
        except asyncio.CancelledError:
            raise
        except Exception:
            return {"success": False, "error": "Generation did not confirm an outcome. The budget remains reserved; no fallback or automatic retry was made."}

    @staticmethod
    def _artifact_result(output, kind, session_id):
        from pathlib import Path
        from app.core.artifact_store import store_bytes

        files = output.get("outputs") or [output]
        urls = []
        for item in files:
            path = Path(item["file_path"])
            if not path.is_file() or path.stat().st_size > 100 * 1024 * 1024:
                raise ValueError("Generated media is unavailable or exceeds the artifact limit.")
            artifact = store_bytes(path.read_bytes(), mime=item.get("mime_type"), filename=path.name, created_by_session=session_id)
            suffix = "/video.mp4" if item.get("mime_type") == "video/mp4" else ""
            urls.append({"url": f"/api/v1/artifacts/{artifact['hash']}{suffix}?" + urlencode({"session_id": session_id})})
        key = {"image": "image_url", "video": "video_url", "audio": "audio_url", "3d": "output_url"}[kind]
        return {"success": True, key: urls[0]["url"], "output_url": urls[0]["url"], "outputs": urls}

    async def execute_local(self, candidate, prompt, context):
        if candidate.source == "fal":
            from app.services.fal_schema import schema_service
            schema = await schema_service.get_schema(candidate.model, await self.fal_key())
            values = schema.validate_options(candidate.options, prompt)
            if not await self.fal_key():
                return {"success": False, "error": "fal.ai is disabled or its account is unavailable."}
            output = await self.node.call("runFalModel", {"endpoint_id": candidate.model, "input": values, "output_type": candidate.kind})
            if output.get("success") and context.session_id:
                return await run_sync(self._artifact_result, output, candidate.kind, context.session_id)
            return output
        # Save session-owned artifacts for native routes, including requests
        # executed on an account-holding peer. Never return a peer-local path.
        if not context.owner or not context.session_id:
            return {"success": False, "error": "A saved owner session is required for native generation."}
        from app.core.artifact_store import store_bytes
        from app.core.native_generation_settings import NativeImageSettings, NativeVideoSettings
        from app.core.provider import native_images, native_video, subscription_images, supergrok_images
        options = {"model": candidate.model, **candidate.options}
        warning = None
        if candidate.kind == "video":
            output = await native_video.generate(candidate.account, prompt, NativeVideoSettings(**options))
            if not output.get("success"):
                return output
            artifact = await run_sync(store_bytes, output.pop("data"), mime="video/mp4", filename="generated-video.mp4", created_by_session=context.session_id)
            return {"success": True, "video_url": f"/api/v1/artifacts/{artifact['hash']}/video.mp4?" + urlencode({"session_id": context.session_id})}
        if candidate.source == "supergrok":
            items, warning = await supergrok_images.generate(candidate.account, prompt, NativeImageSettings(**options))
        else:
            from fluid.messages import Message, Text
            from fluid.providers.base import ProviderRequest
            from app.db import database
            row = await run_sync(database.get_model_provider, candidate.account, False)
            text_model = subscription_images._text_model(row)
            client, http = await subscription_images._open_client(candidate.account)
            tool = {"type": "image_generation", "output_format": options["format"], "partial_images": 0}
            if candidate.model != "default":
                tool["model"] = candidate.model
            for key in ("size", "quality"):
                if options.get(key) not in (None, "auto"):
                    tool[key] = options[key]
            items, usage = [], {}
            try:
                warning = await native_images.stream_images(client, ProviderRequest(model=text_model,
                    messages=[Message("user", [Text(prompt)])], tools=[tool], extra={"tool_choice": {"type": "image_generation"}}), items, usage)
            finally:
                if http is not None:
                    await http.aclose()
        if not items:
            return {"success": False, "error": warning or "The provider returned no image."}
        saved = await run_sync(native_images._save_artifacts, context.session_id, items)
        urls = [f"/api/v1/artifacts/{digest}?" + urlencode({"session_id": context.session_id}) for digest, _ in saved]
        return {"success": True, "image_url": urls[0], "images": [{"image_url": url} for url in urls], "warning": warning}
