<script lang="ts">
	/**
	 * McpManagerCard - MCP Server management card for The Deck
	 *
	 * Features:
	 * - View and manage global MCP servers
	 * - Add/edit/remove MCP server configurations
	 * - View discovered MCP tools from all servers
	 * - Server type support: stdio, sse, http
	 */
	import BaseCard from './BaseCard.svelte';
	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
	import type { DeckCard } from './types';
	import { onMount } from 'svelte';
	import {
		mcp,
		globalMcpServers,
		mcpTools,
		mcpLoading,
		mcpError,
		mcpDisplayStats
	} from '$lib/stores/mcp';
	import { McpServerList, McpServerForm, McpToolList } from '$lib/components/mcp';
	import type { McpServerInfo } from '$lib/api/mcp';
	import { Server, AlertCircle, X, Plus } from 'lucide-svelte';
	import './card-design-system.css';

	interface Props {
		card: DeckCard;
		onClose: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		mobile?: boolean;
		embedded?: boolean;
	}

	let {
		card,
		onClose,
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd,
		mobile = false,
		embedded = false
	}: Props = $props();

	// MCP server form state
	let showMcpServerForm = $state(false);
	let editingMcpServer = $state<McpServerInfo | null>(null);

	// Load data on mount
	onMount(() => {
		mcp.loadGlobalServers();
		mcp.loadTools();
	});

	// MCP handlers
	function handleAddMcpServer() {
		editingMcpServer = null;
		showMcpServerForm = true;
	}

	function handleEditMcpServer(server: McpServerInfo) {
		editingMcpServer = server;
		showMcpServerForm = true;
	}

	async function handleSaveMcpServer(config: any) {
		try {
			if (editingMcpServer) {
				await mcp.updateGlobalServer(editingMcpServer.name, config);
			} else {
				await mcp.addGlobalServer(config);
			}
			showMcpServerForm = false;
			editingMcpServer = null;
		} catch (e) {
			console.error('Failed to save MCP server:', e);
		}
	}

	async function handleRemoveMcpServer(serverName: string) {
		try {
			await mcp.removeGlobalServer(serverName);
			// Refresh tools list and servers after successful removal
			await Promise.all([mcp.loadTools(true), mcp.loadGlobalServers()]);
		} catch (e: any) {
			console.error('Failed to remove MCP server:', e?.detail || e);
		}
	}
</script>

{#snippet mcpMainContent()}
	<!-- Content -->
	<div class="mcp-content">
		{#if $mcpError}
			<div class="mcp-error">
				<AlertCircle size={16} />
				<span>{$mcpError}</span>
				<button onclick={() => mcp.clearError()} class="error-close">
					<X size={14} />
				</button>
			</div>
		{/if}

		<div class="mcp-sections">
			<!-- Global MCP Servers -->
			<div class="mcp-section">
				<div class="section-header">
					<div class="section-title-row">
						<Server size={16} />
						<h3>Global MCP Servers</h3>
					</div>
					<p class="section-description">
						MCP servers available to all projects
					</p>
				</div>
				<McpServerList
					servers={$globalMcpServers}
					scope="user"
					loading={$mcpLoading}
					onAdd={handleAddMcpServer}
					onEdit={handleEditMcpServer}
					onRemove={handleRemoveMcpServer}
				/>
			</div>

			<!-- MCP Tools -->
			<div class="mcp-section">
				<McpToolList
					tools={$mcpTools}
					loading={$mcpLoading}
					onRefresh={() => mcp.loadTools(true)}
					onRemoveServer={handleRemoveMcpServer}
				/>
			</div>
		</div>
	</div>

	<!-- MCP Server Form Modal -->
	<McpServerForm
		show={showMcpServerForm}
		server={editingMcpServer}
		scope="user"
		loading={$mcpLoading}
		onSave={handleSaveMcpServer}
		onClose={() => {
			showMcpServerForm = false;
			editingMcpServer = null;
		}}
	/>
{/snippet}

{#if embedded}
	<FeaturePageLayout
		icon={Server}
		title="MCP Servers"
		subtitle="Manage Model Context Protocol server connections"
		itemCount={$mcpDisplayStats.globalCount}
		itemLabel="servers"
	>
		{#snippet headerActions()}
			<button class="fp-btn fp-btn--primary" onclick={handleAddMcpServer} title="Add Server">
				<Plus size={14} />
				<span class="hidden sm:inline">Add Server</span>
			</button>
		{/snippet}
		{#snippet children()}
			<div class="mcp-card card-system" style="position: relative;">
				{@render mcpMainContent()}
			</div>
		{/snippet}
	</FeaturePageLayout>
{:else}
	<BaseCard
		{card}
		{onClose}
		{onMaximize}
		{onFocus}
		{onMove}
		{onResize}
		{onDragEnd}
		{onResizeEnd}
	>
		{#snippet children()}
			<div class="mcp-card card-system" class:mobile class:maximized={card.maximized}>
				<!-- Header -->
				<div class="mcp-header">
					<div class="header-title">
						<Server size={18} />
						<h2>MCP Servers</h2>
					</div>
					<p class="header-description">
						Manage Model Context Protocol server connections
					</p>
				</div>

				{@render mcpMainContent()}

				<!-- Footer -->
				<div class="mcp-footer">
					<div class="footer-info">
						{$mcpDisplayStats.toolCount} tools discovered
					</div>
					<div class="footer-stats">
						{$mcpDisplayStats.globalCount} servers configured
					</div>
				</div>
			</div>
		{/snippet}
	</BaseCard>
{/if}

<style>
	.mcp-card {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
	}

	/* Header */
	.mcp-header {
		padding: 16px;
		border-bottom: 1px solid var(--border);
		background: var(--surface-1);
		flex-shrink: 0;
	}

	.header-title {
		display: flex;
		align-items: center;
		gap: 10px;
		color: var(--foreground);
	}

	.header-title h2 {
		font-size: 1rem;
		font-weight: 600;
		margin: 0;
	}

	.header-description {
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		margin: 4px 0 0 0;
	}

	/* Content */
	.mcp-content {
		flex: 1;
		overflow-y: auto;
		padding: 16px;
		background: var(--surface-0);
	}

	.mcp-error {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 16px;
		padding: 12px;
		border-radius: 8px;
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
		font-size: 0.875rem;
	}

	.error-close {
		margin-left: auto;
		padding: 4px;
		background: transparent;
		border: none;
		color: var(--destructive);
		cursor: pointer;
		border-radius: 4px;
		transition: opacity 150ms ease;
	}

	.error-close:hover {
		opacity: 0.7;
	}

	.mcp-sections {
		display: flex;
		flex-direction: column;
		gap: 24px;
	}

	.mcp-section {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 16px;
	}

	.section-header {
		margin-bottom: 16px;
	}

	.section-title-row {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--foreground);
		font-weight: 500;
	}

	.section-title-row h3 {
		font-size: 0.9375rem;
		margin: 0;
	}

	.section-description {
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		margin-top: 4px;
	}

	/* Footer */
	.mcp-footer {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 12px 16px;
		border-top: 1px solid var(--border);
		background: var(--surface-1);
		flex-shrink: 0;
	}

	.footer-info,
	.footer-stats {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	/* Mobile adjustments */
	.mcp-card.mobile .mcp-header {
		padding: 12px;
	}

	.mcp-card.mobile .mcp-content {
		padding: 12px;
	}

	.mcp-card.mobile .mcp-section {
		padding: 12px;
	}


</style>
