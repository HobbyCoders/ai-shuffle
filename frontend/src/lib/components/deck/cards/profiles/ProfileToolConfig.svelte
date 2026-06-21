<script lang="ts">
	/**
	 * ProfileToolConfig — Tool configuration section (SDK tools + AI tools)
	 * Studio-v2 grouped fields: a mode select, expandable category cards, and
	 * per-tool selection chips. All real handlers are preserved.
	 */
	import { Wrench, Settings, Sparkles, ChevronDown, Check } from 'lucide-svelte';

	interface ToolInfo {
		name: string;
		description: string;
		mcp_server?: string;
	}

	interface ToolCategory {
		id: string;
		name: string;
		tools: ToolInfo[];
	}

	interface AITool {
		id: string;
		name: string;
		description: string;
		category: string;
		available: boolean;
		providers: string[];
		active_provider: string | null;
	}

	interface AIToolCategory {
		id: string;
		name: string;
		tools: AITool[];
	}

	interface Props {
		toolSelectionMode: 'all' | 'none' | 'allow' | 'disallow';
		allowedTools: string[];
		disallowedTools: string[];
		enabledAITools: string[];
		sdkToolCategories: ToolCategory[];
		aiToolCategories: AIToolCategory[];
		aiToolsLoading: boolean;
		enabledSdkToolCount: number;
		aiToolCount: number;
		effectiveToolCount: number;
		expandedSections: Record<string, boolean>;
		onToolSelectionModeChange: (mode: 'all' | 'none' | 'allow' | 'disallow') => void;
		onToggleTool: (toolName: string) => void;
		onToggleCategory: (category: { name: string; tools: { name: string }[] }) => void;
		onToggleAITool: (toolId: string) => void;
		onToggleAIToolCategory: (category: AIToolCategory) => void;
		onToggleSection: (id: string) => void;
		getCategorySelectionState: (category: { name: string; tools: { name: string }[] }) => 'all' | 'some' | 'none';
		getAICategorySelectionState: (category: AIToolCategory) => 'all' | 'some' | 'none';
	}

	let {
		toolSelectionMode,
		allowedTools,
		disallowedTools,
		enabledAITools,
		sdkToolCategories,
		aiToolCategories,
		aiToolsLoading,
		enabledSdkToolCount,
		aiToolCount,
		effectiveToolCount,
		expandedSections,
		onToolSelectionModeChange,
		onToggleTool,
		onToggleCategory,
		onToggleAITool,
		onToggleAIToolCategory,
		onToggleSection,
		getCategorySelectionState,
		getAICategorySelectionState
	}: Props = $props();
</script>

<div class="set-group">
	<div class="set-grouphead"><h3><Wrench />Tool access</h3><p>Control which tools this profile can use during conversations.</p></div>
	<div class="set-row">
		<div class="lab"><h4>Mode</h4><p>Allow everything, block everything, or curate a list.</p></div>
		<div class="ctl">
			<select
				class="sel"
				value={toolSelectionMode}
				onchange={(e) => onToolSelectionModeChange((e.target as HTMLSelectElement).value as 'all' | 'none' | 'allow' | 'disallow')}
			>
				<option value="all">All Tools Allowed</option>
				<option value="none">Disable All Tools</option>
				<option value="allow">Allow Only Selected</option>
				<option value="disallow">Disallow Selected</option>
			</select>
		</div>
	</div>
</div>

{#if toolSelectionMode !== 'none'}
	<div class="set-group">
		<div class="set-grouphead"><h3><Settings />Tool categories</h3><p>Browse and manage tools by category.</p></div>
		<div class="cat-list">
			<!-- Built-in SDK Tools -->
			<button type="button" class="cat-card" onclick={() => onToggleSection('sdk')}>
				<div class="cat-icon"><Wrench size={17} /></div>
				<div class="cat-info">
					<span class="cat-name">Built-in tools</span>
					<span class="cat-desc">{sdkToolCategories.map((c) => c.name).join(', ')}</span>
				</div>
				<span class="pill-s acc">{enabledSdkToolCount}</span>
				<ChevronDown size={16} class={expandedSections.sdk ? 'cat-chev open' : 'cat-chev'} />
			</button>
			{#if expandedSections.sdk}
				<div class="cat-body">
					{#if toolSelectionMode !== 'all'}
						{#each sdkToolCategories as category}
							{#if category.tools.length > 0}
								{@const state = getCategorySelectionState(category)}
								<div class="catgroup">
									<div class="catgroup-head">
										<span class="catgroup-name">{category.name} <span class="hint">{category.tools.length}</span></span>
										<button type="button" class="lnk" onclick={() => onToggleCategory(category)}>{state === 'all' ? 'Clear' : 'Select all'}</button>
									</div>
									<div class="chips-select">
										{#each category.tools as tool}
											{@const sel = toolSelectionMode === 'allow' ? allowedTools.includes(tool.name) : disallowedTools.includes(tool.name)}
											<button type="button" class="chsel" class:on={sel} title={tool.description} onclick={() => onToggleTool(tool.name)}>
												{#if sel}<Check size={12} />{/if}
												{tool.name}
											</button>
										{/each}
									</div>
								</div>
							{/if}
						{/each}
						<div class="sel-count">
							{#if toolSelectionMode === 'allow'}{allowedTools.length} tool{allowedTools.length !== 1 ? 's' : ''} allowed
							{:else}{disallowedTools.length} tool{disallowedTools.length !== 1 ? 's' : ''} blocked{/if}
						</div>
					{:else}
						<div class="ed-note"><Wrench /><span>All built-in tools are available in this mode.</span></div>
					{/if}
				</div>
			{/if}

			<!-- AI Tools -->
			<button type="button" class="cat-card" onclick={() => onToggleSection('ai')}>
				<div class="cat-icon"><Sparkles size={17} /></div>
				<div class="cat-info">
					<span class="cat-name">AI tools</span>
					<span class="cat-desc">Image generation, video, 3D models, web search</span>
				</div>
				<span class="pill-s acc">{aiToolCount}</span>
				<ChevronDown size={16} class={expandedSections.ai ? 'cat-chev open' : 'cat-chev'} />
			</button>
			{#if expandedSections.ai}
				<div class="cat-body">
					{#if aiToolsLoading}
						<div class="ed-note"><span>Loading AI tools…</span></div>
					{:else if aiToolCategories.length === 0}
						<div class="ed-note"><span>No AI tools available. Configure API keys in Settings.</span></div>
					{:else if toolSelectionMode !== 'all'}
						{#each aiToolCategories as category}
							{#if category.tools.length > 0}
								{@const state = getAICategorySelectionState(category)}
								<div class="catgroup">
									<div class="catgroup-head">
										<span class="catgroup-name">{category.name} <span class="hint">{category.tools.length}</span></span>
										<button type="button" class="lnk" onclick={() => onToggleAIToolCategory(category)}>{state === 'all' ? 'Clear' : 'Select all'}</button>
									</div>
									<div class="chips-select">
										{#each category.tools as tool}
											{@const sel = enabledAITools.includes(tool.id)}
											<button type="button" class="chsel" class:on={sel} disabled={!tool.available} title={tool.available ? tool.description : 'No API key configured'} onclick={() => onToggleAITool(tool.id)}>
												{#if sel}<Check size={12} />{/if}
												{tool.name}{#if !tool.available}<span class="na"> · no key</span>{/if}
											</button>
										{/each}
									</div>
								</div>
							{/if}
						{/each}
					{:else}
						<div class="ed-note"><Sparkles /><span>All AI tools are available in this mode.</span></div>
					{/if}
				</div>
			{/if}
		</div>
	</div>
{/if}

<div class="ed-note">
	<Wrench />
	<span>{effectiveToolCount} tools enabled for this profile.</span>
</div>

<style>
	.cat-list {
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding: 14px 18px 18px;
	}

	/* Top-level category card (expandable) */
	.cat-card {
		display: flex;
		align-items: center;
		gap: 12px;
		width: 100%;
		padding: 12px 14px;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 10px;
		cursor: pointer;
		text-align: left;
		color: var(--foreground);
		transition: background 0.12s, border-color 0.12s;
	}
	.cat-card:hover {
		background: var(--muted);
	}
	.cat-icon {
		width: 34px;
		height: 34px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--muted);
		border-radius: 8px;
		color: var(--foreground);
		flex-shrink: 0;
	}
	.cat-info {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.cat-name {
		font-size: 13.5px;
		font-weight: 600;
	}
	.cat-desc {
		font-size: 11.5px;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.cat-card :global(.cat-chev) {
		color: var(--muted-foreground);
		flex-shrink: 0;
		transition: transform 0.18s;
	}
	.cat-card :global(.cat-chev.open) {
		transform: rotate(180deg);
	}

	/* Expanded body */
	.cat-body {
		display: flex;
		flex-direction: column;
		gap: 14px;
		padding: 14px;
		margin-top: -4px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: color-mix(in oklch, var(--muted) 35%, transparent);
	}
	.catgroup {
		display: flex;
		flex-direction: column;
		gap: 9px;
	}
	.catgroup-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
	}
	.catgroup-name {
		font-size: 12.5px;
		font-weight: 600;
		color: var(--foreground);
		display: inline-flex;
		align-items: center;
		gap: 7px;
	}
	.lnk {
		background: none;
		border: none;
		padding: 0;
		font: inherit;
		font-size: 12px;
		font-weight: 600;
		color: var(--foreground);
		cursor: pointer;
	}
	.lnk:hover {
		text-decoration: underline;
	}
	.sel-count {
		font-size: 12px;
		color: var(--muted-foreground);
		padding-top: 12px;
		border-top: 1px solid var(--border);
	}
	.na {
		font-size: 11px;
		opacity: 0.8;
	}
</style>
