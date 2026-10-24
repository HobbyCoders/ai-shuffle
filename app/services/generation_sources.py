"""On-demand source catalog and execution adapters for unified generation.

Native subscription capability tables are authoritative. An unknown price is
never converted to zero. API accounts without a declared media schema remain
visible but unavailable; discovering a text model is not media verification.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from app.core.generation_resolver import Candidate, DO_NOT_RETRY, INCLUDED_PRICE, RunContext, UNKNOWN_PRICE, price_total
from app.db.async_database import run_sync

# Only these kinds have per-account stored Native generation settings.
NATIVE_GROUPS = ("image", "video")


async def stored_native_group(account, kind):
    """The account's stored Native generation group for ``kind``, or None.

    ``native_generation_settings.load`` applies the #377 recovery rules (an
    unreadable row keeps the verified image default but disables video; a
    missing row keeps provider defaults). None means disabled: never run it.
    """
    from app.core import native_generation_settings as native
    try:
        settings = await run_sync(native.load, account)
    except ValueError:
        return None
    return getattr(settings, kind, None) if kind in NATIVE_GROUPS else None


class Sources:
    def __init__(self, worker, context: RunContext, *, local_only=False, serve_peer=False):
        self.node = worker
        self.context = context
        self.local_only = local_only
        # True while executing for another worker: its paths are not ours to share.
        self.serve_peer = serve_peer
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
            from app.core import fal_credential
            return await run_sync(fal_credential.api_key)
        return None

    async def catalog(self, kind, *, source=None, model=None, query=None, limit=15, prompt=None, worker=None):
        here = self.local_worker()
        entries = []
        if worker in (None, here):
            entries = await self.local_catalog(kind, source=source, model=model, query=query, limit=limit, prompt=prompt)
        # A request pinned to this worker never waits on peers.
        if self.context.owner and not self.local_only and worker != here:
            from app.network.generation import catalog
            entries.extend(await catalog(kind, source=source, model=model, query=query, limit=limit, prompt=prompt, worker=worker))
        return entries

    async def local_catalog(self, kind, *, source=None, model=None, query=None, limit=15, prompt=None):
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
            # Discovery offers text generation endpoints only; an exactly named
            # model (runFalModel, image-to-video, audio-to-audio...) keeps any category.
            models = [m for m in models if (m.endpoint_id == model if model else m.category.startswith("text-to-"))]
            for m in models[:max(1, min(limit, 50))]:
                schema = await schema_service.get_schema(m.endpoint_id, await self.fal_key())
                options = {f.name: f.default for f in schema.fields if not f.is_primary_text and f.default is not None} if schema else {}
                price = await self.fal_price(m.endpoint_id, options, prompt=prompt)
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
                # Disabled groups remain visible for the all-account catalog,
                # but cannot be selected or submitted.
                stored = await stored_native_group(account, kind)
                combination = stored.model_dump() if stored else combination
                if model and combination["model"] != model:
                    continue
                if (query and query.casefold()
                        not in f"{name} {account} {combination['model']}".casefold()):
                    continue
                # Plan images carry no per-item charge on the verified route.
                price = dict(INCLUDED_PRICE) if kind == "image" and name in ("chatgpt", "supergrok") else dict(UNKNOWN_PRICE)
                if kind == "image" and name in ("openai", "xai"):
                    from app.core.native_image_verification import verified
                    price = verified(name).price(prompt)
                if name == "supergrok" and kind == "video":
                    from app.core.provider.native_video import estimate_price
                    price = estimate_price(native.NativeVideoSettings(**combination))
                entries.append(Candidate(
                    name, account, worker, combination["model"], kind, True,
                    options={k: v for k, v in combination.items() if k != "model"},
                    schema=capability, price=price, available=stored is not None,
                    reason=None if stored else
                    "Disabled in this account's Native generation settings.",
                ))
            elif not model or source:
                # Unsupported accounts are visible in discovery, but must not
                # invent a match for a model explicitly named on another source.
                entries.append(Candidate(name, account, worker, model or "unavailable", kind, False,
                                         available=False, reason=capability.get("reason") or "No verified media schema is available for this account."))
        return entries

    async def fal_price(self, model, options, *, prompt=None):
        """Read declared provider rates and resolve quantities with its schema."""
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                response = await client.get("https://api.fal.ai/v1/models/pricing", params={"endpoint_id": model},
                                            headers={"Authorization": "Key " + (await self.fal_key() or "")})
                if response.status_code != 200:
                    return dict(UNKNOWN_PRICE)
                rows = response.json().get("prices", [])
            row = next((r for r in rows if isinstance(r, dict) and r.get("endpoint_id") == model), {})
            if row.get("currency") != "USD" or not isinstance(row.get("unit"), str):
                return dict(UNKNOWN_PRICE)
            rate = Decimal(str(row["unit_price"]))
            from app.services.fal_schema import schema_service
            schema = await schema_service.get_schema(model, await self.fal_key())
            if schema is None or not rate.is_finite() or rate < 0:
                return dict(UNKNOWN_PRICE)
            quantity, count = schema.pricing_quantity(row["unit"], options, prompt)
            amount = rate * quantity
            return {"currency": "USD", "unit": "item", "amount": str(amount), "total": str(amount * count), "status": "estimated"}
        except (httpx.HTTPError, ValueError, TypeError, KeyError, ArithmeticError, AttributeError):
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
            return replace(candidate, options=values, price=await self.fal_price(candidate.model, values, prompt=prompt))
        from app.core import native_generation_settings as native
        if "model" in options:
            raise ValueError("Pass the model separately, not in options.")
        values = {**options, "model": candidate.model}
        kind = "grok" if candidate.source == "supergrok" else candidate.source
        if await stored_native_group(candidate.account, candidate.kind) is None:
            raise ValueError("This kind is disabled in the account's Native generation settings.")
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
        if candidate.source in ("openai", "xai"):
            from app.core.native_image_verification import verified
            return replace(
                candidate, options=options, price=verified(candidate.source).price(prompt),
            )
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
            return {"success": False, "error": "Generation did not confirm an outcome. The budget remains reserved; no fallback or automatic retry was made. " + DO_NOT_RETRY}

    @staticmethod
    def _artifact_result(output, kind, session_id, with_paths=False):
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
            urls.append({"url": f"/api/v1/artifacts/{artifact['hash']}{suffix}?" + urlencode({"session_id": session_id}),
                         # Workspace paths let editImage/imageToVideo chain, as before.
                         **({"file_path": str(path)} if with_paths else {})})
        key = {"image": "image_url", "video": "video_url", "audio": "audio_url", "3d": "output_url"}[kind]
        result = {"success": True, key: urls[0]["url"], "output_url": urls[0]["url"], "outputs": urls}
        if with_paths:
            result["file_path"] = urls[0]["file_path"]
        return result

    async def execute_local(self, candidate, prompt, context):
        if candidate.source == "fal":
            from app.services.fal_schema import schema_service
            schema = await schema_service.get_schema(candidate.model, await self.fal_key())
            values = schema.validate_options(candidate.options, prompt)
            if not await self.fal_key():
                return {"success": False, "error": "fal.ai is disabled or its account is unavailable."}
            if (self.serve_peer or context.chat_mode) and not context.session_id:
                # Only a session artifact may leave here; raw Node output holds
                # this worker's paths. Refused before anything is submitted.
                return {"success": False, "error": "A saved session is required for this generation; nothing was submitted."}
            output = await self.node.call("runFalModel", {"endpoint_id": candidate.model, "input": values, "output_type": candidate.kind})
            if output.get("success") and context.session_id:
                return await run_sync(self._artifact_result, output, candidate.kind, context.session_id,
                                      not context.chat_mode and not self.serve_peer)
            return output
        # Save session-owned artifacts for native routes, including requests
        # executed on an account-holding peer. Never return a peer-local path.
        if not context.owner or not context.session_id:
            return {"success": False, "error": "A saved owner session is required for native generation."}
        stored = await stored_native_group(candidate.account, candidate.kind)
        if stored is None or stored.model_dump() != {"model": candidate.model, **candidate.options}:
            return {"success": False, "error": "Native generation settings changed or disable this kind; nothing was submitted."}
        from app.core.artifact_store import store_bytes
        from app.core.native_generation_settings import NativeImageSettings, NativeVideoSettings
        from app.core.provider import native_images, native_video, subscription_images, supergrok_images
        options = {"model": candidate.model, **candidate.options}
        warning = None
        if candidate.kind == "video":
            observed = {}
            try:
                output = await native_video.generate(candidate.account, prompt, NativeVideoSettings(**options), usage=observed)
            finally:
                # Preserve a provider-reported overrun even on cancellation or
                # failed downloads; do not authorize another generation call.
                if "reported_cost_usd" in observed:
                    cost = Decimal(observed["reported_cost_usd"])
                    estimate = price_total(candidate.price)
                    if cost.is_finite() and estimate is not None and cost > estimate:
                        from app.network.generation import record_overrun
                        await record_overrun(cost - estimate)
            if "reported_cost_usd" in observed:
                output["price"] = {"currency": "USD", "unit": "item", "amount": observed["reported_cost_usd"],
                                   "total": observed["reported_cost_usd"], "status": "reported"}
            if not output.get("success"):
                return output
            artifact = await run_sync(store_bytes, output.pop("data"), mime="video/mp4", filename="generated-video.mp4", created_by_session=context.session_id)
            return {**output, "video_url": f"/api/v1/artifacts/{artifact['hash']}/video.mp4?" + urlencode({"session_id": context.session_id})}
        if candidate.source == "supergrok":
            items, warning = await supergrok_images.generate(candidate.account, prompt, NativeImageSettings(**options))
        elif candidate.source in ("openai", "xai"):
            from app.core.provider import api_images
            items, warning = await api_images.generate(
                candidate.account, candidate.source, prompt, NativeImageSettings(**options),
            )
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
