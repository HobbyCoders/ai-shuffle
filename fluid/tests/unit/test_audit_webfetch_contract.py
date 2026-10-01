"""Audit regressions: HTTP data is recoverable; safety barriers still abort."""
import httpx
import pytest

from fluid.codemode import HostBridge, Projection, SubprocessKernel
from fluid.permissions.engine import PermissionEngine
from fluid.tools.builtins import read, web
from fluid.tools.registry import ToolCtx, ToolRegistry


@pytest.mark.parametrize("status", [401, 404, 429, 500])
async def test_http_error_allows_independent_read_through_kernel(tmp_path, status):
    registry = ToolRegistry()
    registry.register_builtin(web.FETCH_SPEC, web.fetch_handler)
    registry.register_builtin(read.SPEC, read.handler)
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    ctx = ToolCtx(cwd=str(tmp_path), extra={"web_transport": transport})
    bridge = HostBridge(registry, PermissionEngine("never_ask"), ctx=ctx)
    (tmp_path / "present.txt").write_text("independent evidence")
    kernel = SubprocessKernel()
    await kernel.start()
    try:
        await kernel.set_vfs(Projection(registry.list_tools()).sources())
        result = await kernel.exec(
            "from tools import WebFetch, Read\n"
            "error = WebFetch(url='https://example.com/missing')\n"
            "print(error)\nprint(Read(file_path='present.txt'))\n",
            timeout=8, on_host_call=bridge.on_host_call,
        )
        assert result.error is None, result.error
        assert f"HTTP {status}" in result.stdout
        assert "independent evidence" in result.stdout
    finally:
        await kernel.stop()

@pytest.mark.parametrize("kind", ["private", "dns_private", "permission", "timeout"])
async def test_webfetch_safety_barriers_abort_without_later_read(tmp_path, monkeypatch, kind):
    calls = []
    def transport(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})

    ctx = ToolCtx(cwd=str(tmp_path), extra={"web_transport": httpx.MockTransport(transport)})
    registry = ToolRegistry()
    registry.register_builtin(web.FETCH_SPEC, web.fetch_handler)
    registry.register_builtin(read.SPEC, read.handler)
    if kind in {"dns_private", "timeout"}:
        async def fail(url, **kwargs):
            if kind == "dns_private":
                raise web.BlockedAddress("host resolves to a private address")
            raise TimeoutError("transport timed out")
        # Monkeypatching only the vetted transport seam preserves handler/gate.
        monkeypatch.setattr(web, "_get", fail)
    engine = PermissionEngine("always_ask" if kind == "permission" else "never_ask")
    bridge = HostBridge(registry, engine, ctx=ctx)
    (tmp_path / "present.txt").write_text("must not be read")
    url = "https://127.0.0.1/private" if kind == "private" else "https://example.com/page"
    kernel = SubprocessKernel()
    await kernel.start()
    try:
        await kernel.set_vfs(Projection(registry.list_tools()).sources())
        result = await kernel.exec(
            "from tools import WebFetch, Read\n"
            f"WebFetch(url={url!r})\nprint(Read(file_path='present.txt'))",
            timeout=8, on_host_call=bridge.on_host_call,
        )
        assert result.error is not None
        assert "must not be read" not in result.stdout
        assert not ctx.read_files
        if kind in {"private", "permission", "dns_private", "timeout"}:
            assert not calls
    finally:
        await kernel.stop()
