# AI Shuffle Documentation

This folder contains local documentation for the main technologies used in the AI Shuffle application. Having documentation locally eliminates the need for constant web fetching during development.

## Table of Contents

### Core AI Stack

#### [Claude Agent SDK](./claude-agent-sdk/)
Python SDK for building production AI agents that autonomously read files, run commands, and execute tools.

- [Overview](./claude-agent-sdk/overview.md) - Architecture, concepts, and getting started
- [API Reference](./claude-agent-sdk/api-reference.md) - Functions, classes, and configuration options
- [Examples](./claude-agent-sdk/examples.md) - Code patterns and use cases

#### [Model Context Protocol (MCP)](./mcp/)
Open protocol for integrating LLMs with external data sources and tools.

- [Overview](./mcp/overview.md) - Core concepts, servers, clients, and transports
- [API Reference](./mcp/api-reference.md) - FastMCP, decorators, and client APIs
- [Examples](./mcp/examples.md) - Server implementations and client usage

### Frontend Stack

#### [Svelte 5](./svelte5/)
Modern reactive UI framework with runes-based reactivity.

- [Overview](./svelte5/overview.md) - Runes, migration from Svelte 4, and best practices
- [API Reference](./svelte5/api-reference.md) - $state, $derived, $effect, $props, and template syntax
- [Examples](./svelte5/examples.md) - Components, forms, and patterns

#### [SvelteKit](./sveltekit/)
Full-stack framework for building web applications with Svelte.

- [Overview](./sveltekit/overview.md) - Routing, data loading, forms, and deployment
- [API Reference](./sveltekit/api-reference.md) - Load functions, actions, hooks, and navigation
- [Examples](./sveltekit/examples.md) - Authentication, APIs, and layouts

#### [Tailwind CSS](./tailwind/)
Utility-first CSS framework for rapid UI development.

- [Overview](./tailwind/overview.md) - Core concepts, utilities, and responsive design
- [API Reference](./tailwind/api-reference.md) - Configuration and utility class reference
- [Examples](./tailwind/examples.md) - Components, layouts, and patterns

#### [TipTap](./tiptap/)
Headless rich text editor framework built on ProseMirror.

- [Overview](./tiptap/overview.md) - Architecture, extensions, and setup
- [API Reference](./tiptap/api-reference.md) - Editor API, commands, and extension creation
- [Examples](./tiptap/examples.md) - Toolbars, custom extensions, and integrations

## Quick Links

### Getting Started Guides

| Technology | Start Here |
|------------|-----------|
| Claude Agent SDK | [Overview](./claude-agent-sdk/overview.md) |
| MCP | [Overview](./mcp/overview.md) |
| Svelte 5 | [Overview](./svelte5/overview.md) |
| SvelteKit | [Overview](./sveltekit/overview.md) |
| Tailwind CSS | [Overview](./tailwind/overview.md) |
| TipTap | [Overview](./tiptap/overview.md) |

### Common Tasks

| Task | Documentation |
|------|--------------|
| Create a custom AI tool | [Claude Agent SDK Examples](./claude-agent-sdk/examples.md) |
| Build an MCP server | [MCP Examples](./mcp/examples.md) |
| Use Svelte 5 runes | [Svelte 5 API Reference](./svelte5/api-reference.md) |
| Handle form submissions | [SvelteKit Examples](./sveltekit/examples.md) |
| Style components | [Tailwind Examples](./tailwind/examples.md) |
| Build a rich text editor | [TipTap Examples](./tiptap/examples.md) |

## Documentation Sources

This documentation was compiled from Context7 with the following sources:

- **Claude Agent SDK**: [Anthropic Platform Docs](https://platform.claude.com/docs/en/agent-sdk)
- **MCP Python SDK**: [GitHub Repository](https://github.com/modelcontextprotocol/python-sdk)
- **Svelte 5**: [Svelte Documentation](https://svelte.dev/docs)
- **SvelteKit**: [SvelteKit Documentation](https://kit.svelte.dev/docs)
- **Tailwind CSS v3**: [Tailwind Documentation](https://v3.tailwindcss.com/docs)
- **TipTap**: [TipTap Documentation](https://tiptap.dev/docs)

## Updating Documentation

To update this documentation with the latest information:

1. Use Context7 to query for updated documentation
2. Replace the relevant `.md` files with new content
3. Update this README if new sections are added

## File Structure

```
docs/
├── README.md                    # This file
├── claude-agent-sdk/
│   ├── overview.md
│   ├── api-reference.md
│   └── examples.md
├── mcp/
│   ├── overview.md
│   ├── api-reference.md
│   └── examples.md
├── svelte5/
│   ├── overview.md
│   ├── api-reference.md
│   └── examples.md
├── sveltekit/
│   ├── overview.md
│   ├── api-reference.md
│   └── examples.md
├── tailwind/
│   ├── overview.md
│   ├── api-reference.md
│   └── examples.md
└── tiptap/
    ├── overview.md
    ├── api-reference.md
    └── examples.md
```
