"""Host tool factories backed by Fluid, not an agent SDK or an MCP transport.

The ``sdk`` discriminator is retained for existing in-process config consumers;
it never selects a client or launches a process. Fluid dispatches these tools.
"""
from fluid.tools.inproc import create_inproc_server, tool as tool


def create_tool_server(*, name, version="1.0.0", tools=None):
    return {"type": "sdk", "name": name,
            "instance": create_inproc_server(name, tools or [], version=version)}
