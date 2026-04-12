<script lang="ts">
	/**
	 * ProfileToolConfig — Tool configuration section (SDK tools + AI tools)
	 */
	import { Wrench, Settings, Sparkles, ChevronRight } from 'lucide-svelte';

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

<div class="pf-section">
	<div class="pf-section-header">
		<Wrench size={16} />
		<h3>Tool Access</h3>
	</div>
	<p class="pf-desc">Control which tools this profile can access during conversations.</p>
	<select class="pf-select" value={toolSelectionMode} onchange={(e) => {
		const mode = (e.target as HTMLSelectElement).value as 'all' | 'none' | 'allow' | 'disallow';
		onToolSelectionModeChange(mode);
	}}>
		<option value="all">All Tools Allowed</option>
		<option value="none">Disable All Tools</option>
		<option value="allow">Allow Only Selected</option>
		<option value="disallow">Disallow Selected</option>
	</select>
</div>

{#if toolSelectionMode !== 'none'}
<div class="pf-section">
	<div class="pf-section-header">
		<Settings size={16} />
		<h3>Tool Categories</h3>
	</div>
	<p class="pf-desc">Browse and manage tools by category. Toggle entire categories on or off.</p>
	<div class="pf-category-list">
		<!-- Built-in SDK Tools -->
		<button type="button" class="pf-category-card" onclick={() => onToggleSection('sdk')}>
			<div class="pf-category-icon">
				<Wrench size={18} />
			</div>
			<div class="pf-category-info">
				<span class="pf-category-name">Built-in Tools</span>
				<span class="pf-category-desc">{sdkToolCategories.map(c => c.name).join(', ')}</span>
			</div>
			<span class="pf-category-count">{enabledSdkToolCount}</span>
			<ChevronRight size={16} class="pf-category-chevron" />
		</button>

		{#if expandedSections.sdk}
			<div class="pf-category-expanded">
				{#if toolSelectionMode !== 'all'}
					<div class="tools-list">
						{#each sdkToolCategories as category}
							{#if category.tools.length > 0}
								{@const state = getCategorySelectionState(category)}
								<div class="tool-category">
									<button type="button" class="category-header" onclick={() => onToggleCategory(category)}>
										<div class="category-checkbox" class:checked={state !== 'none'} class:partial={state === 'some'}>
											{#if state === 'all'}
												<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" /></svg>
											{:else if state === 'some'}
												<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 12h14" /></svg>
											{/if}
										</div>
										<span class="category-name">{category.name}</span>
										<span class="category-count">{category.tools.length}</span>
									</button>
									<div class="category-tools">
										{#each category.tools as tool}
											{@const isSelected = toolSelectionMode === 'allow' ? allowedTools.includes(tool.name) : disallowedTools.includes(tool.name)}
											<label class="tool-item">
												<input type="checkbox" checked={isSelected} onchange={() => onToggleTool(tool.name)} class="tool-checkbox" />
												<span class="tool-name" title={tool.description}>{tool.name}</span>
											</label>
										{/each}
									</div>
								</div>
							{/if}
						{/each}
					</div>
					<div class="selection-count">
						{#if toolSelectionMode === 'allow'}{allowedTools.length} tool{allowedTools.length !== 1 ? 's' : ''} allowed
						{:else}{disallowedTools.length} tool{disallowedTools.length !== 1 ? 's' : ''} blocked{/if}
					</div>
				{:else}
					<p class="pf-desc" style="padding: 12px 16px;">All built-in tools are available.</p>
				{/if}
			</div>
		{/if}

		<!-- AI Tools -->
		<button type="button" class="pf-category-card" onclick={() => onToggleSection('ai')}>
			<div class="pf-category-icon">
				<Sparkles size={18} />
			</div>
			<div class="pf-category-info">
				<span class="pf-category-name">AI Tools</span>
				<span class="pf-category-desc">Image generation, video, 3D models, web search</span>
			</div>
			<span class="pf-category-count">{aiToolCount}</span>
			<ChevronRight size={16} class="pf-category-chevron" />
		</button>

		{#if expandedSections.ai}
			<div class="pf-category-expanded">
				{#if aiToolsLoading}
					<div class="loading-state">Loading AI tools...</div>
				{:else if aiToolCategories.length === 0}
					<div class="card-empty-state"><p class="card-empty-description">No AI tools available. Configure API keys in Settings.</p></div>
				{:else if toolSelectionMode !== 'all'}
					<div class="tools-list">
						{#each aiToolCategories as category}
							{#if category.tools.length > 0}
								{@const state = getAICategorySelectionState(category)}
								<div class="tool-category">
									<button type="button" class="category-header" onclick={() => onToggleAIToolCategory(category)}>
										<div class="category-checkbox" class:checked={state !== 'none'} class:partial={state === 'some'}>
											{#if state === 'all'}
												<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" /></svg>
											{:else if state === 'some'}
												<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 12h14" /></svg>
											{/if}
										</div>
										<span class="category-name">{category.name}</span>
										<span class="category-count">{category.tools.length}</span>
									</button>
									<div class="ai-category-tools">
										{#each category.tools as tool}
											{@const isSelected = enabledAITools.includes(tool.id)}
											<label class="ai-tool-row" class:disabled={!tool.available}>
												<input type="checkbox" checked={isSelected} disabled={!tool.available} onchange={() => onToggleAITool(tool.id)} class="tool-checkbox" />
												<div class="ai-tool-content">
													<span class="tool-name">{tool.name}</span>
													{#if !tool.available}<span class="card-badge card-badge--warning">No API key</span>{/if}
													<p class="ai-tool-description">{tool.description}</p>
												</div>
											</label>
										{/each}
									</div>
								</div>
							{/if}
						{/each}
					</div>
				{:else}
					<p class="pf-desc" style="padding: 12px 16px;">All AI tools are available.</p>
				{/if}
			</div>
		{/if}

	</div>
</div>
{/if}

<!-- Tools Summary Bar -->
<div class="pf-summary-bar">
	<div class="pf-summary-left">
		<Wrench size={14} />
		<span>{effectiveToolCount} tools enabled</span>
	</div>
	<span class="pf-summary-link">Manage individually &rarr;</span>
</div>

<style>
	.pf-section {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.pf-section-header {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--text-primary);
	}

	.pf-section-header h3 {
		font-size: var(--text-md, 0.875rem);
		font-weight: 600;
		margin: 0;
		color: var(--text-primary);
	}

	.pf-desc {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
		margin: 0;
	}

	.pf-select {
		width: 100%;
		padding: 10px 14px;
		padding-right: 36px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		color: var(--text-primary);
		font-size: var(--text-md, 0.875rem);
		font-family: inherit;
		cursor: pointer;
		outline: none;
		appearance: none;
		background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%239090A8' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E");
		background-repeat: no-repeat;
		background-position: right 12px center;
		background-size: 16px;
		transition: border-color 200ms, box-shadow 200ms;
	}

	.pf-select:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	/* Category Cards */
	.pf-category-list {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.pf-category-card {
		display: flex;
		align-items: center;
		gap: 12px;
		width: 100%;
		padding: 14px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		cursor: pointer;
		transition: all 200ms;
		color: var(--text-primary);
		text-align: left;
	}

	.pf-category-card:hover {
		border-color: color-mix(in oklch, var(--primary) 30%, transparent);
		background: var(--surface-hover);
	}

	.pf-category-icon {
		width: 36px;
		height: 36px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		border-radius: 8px;
		color: var(--primary);
		flex-shrink: 0;
	}

	.pf-category-info {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.pf-category-name {
		font-size: var(--text-md, 0.875rem);
		font-weight: 500;
	}

	.pf-category-desc {
		font-size: var(--text-xs, 0.6875rem);
		color: var(--text-secondary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.pf-category-count {
		font-size: var(--text-md, 0.875rem);
		font-weight: 600;
		color: var(--primary);
		padding: 2px 8px;
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		border-radius: 6px;
	}

	.pf-category-card :global(.pf-category-chevron) {
		color: var(--text-secondary);
		flex-shrink: 0;
	}

	.pf-category-expanded {
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		overflow: hidden;
		margin-top: -4px;
	}

	/* Summary Bar */
	.pf-summary-bar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 12px 16px;
		background: color-mix(in oklch, var(--primary) 3%, transparent);
		border: 1px solid color-mix(in oklch, var(--primary) 20%, transparent);
		border-radius: var(--radius-md, 10px);
	}

	.pf-summary-left {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
	}

	.pf-summary-left :global(svg) {
		color: var(--primary);
	}

	.pf-summary-link {
		font-size: var(--text-xs, 0.6875rem);
		font-weight: 500;
		color: var(--text-secondary);
		background: none;
		border: none;
		padding: 0;
		cursor: pointer;
		transition: color 120ms;
	}

	.pf-summary-link:hover {
		color: var(--primary);
	}

	/* Tool details */
	.tools-list {
		max-height: 256px;
		overflow-y: auto;
		padding-right: 8px;
		margin-top: 12px;
		scrollbar-width: thin;
		scrollbar-color: var(--border-default) transparent;
	}

	.tools-list::-webkit-scrollbar {
		width: 6px;
	}

	.tools-list::-webkit-scrollbar-track {
		background: transparent;
	}

	.tools-list::-webkit-scrollbar-thumb {
		background-color: var(--border-default);
		border-radius: 3px;
	}

	.tool-category {
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		overflow: hidden;
		margin-bottom: 8px;
	}

	.category-header {
		display: flex;
		align-items: center;
		gap: 12px;
		width: 100%;
		padding: 12px;
		background: var(--surface-1);
		border: none;
		color: var(--text-primary);
		font-size: var(--text-md, 0.875rem);
		cursor: pointer;
		transition: background 200ms;
	}

	.category-header:hover {
		background: var(--surface-hover);
	}

	.category-checkbox {
		width: 16px;
		height: 16px;
		border-radius: 4px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--surface-1);
		border: 1px solid var(--border-default);
		transition: all 100ms;
	}

	.category-checkbox.checked,
	.category-checkbox.partial {
		background: var(--primary);
		border-color: var(--primary);
	}

	.check-icon {
		width: 12px;
		height: 12px;
		color: var(--primary-foreground);
	}

	.category-name {
		flex: 1;
		text-align: left;
		font-weight: 500;
	}

	.category-count {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
	}

	.category-tools {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 4px;
		padding: 8px;
		background: var(--surface-0);
	}

	@media (max-width: 480px) {
		.category-tools {
			grid-template-columns: 1fr;
		}
	}

	.tool-item {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 6px 8px;
		border-radius: var(--radius-sm, 6px);
		cursor: pointer;
		transition: background 100ms;
	}

	.tool-item:hover {
		background: var(--surface-1);
	}

	.tool-checkbox {
		width: 14px;
		height: 14px;
		flex-shrink: 0;
		accent-color: var(--primary);
	}

	.tool-name {
		font-size: var(--text-base, 0.8125rem);
		color: var(--text-primary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.selection-count {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
		margin-top: 12px;
		padding: 12px 16px 0;
		border-top: 1px solid var(--border-subtle);
	}

	/* AI tool rows */
	.ai-category-tools {
		display: flex;
		flex-direction: column;
		padding: 8px;
		background: var(--surface-0);
	}

	.ai-tool-row {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 6px 8px;
		border-radius: var(--radius-sm, 6px);
		cursor: pointer;
		transition: background 100ms;
	}

	.ai-tool-row:hover {
		background: var(--surface-1);
	}

	.ai-tool-row.disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.ai-tool-content {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: center;
		gap: 6px;
		overflow: hidden;
	}

	.ai-tool-description {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		flex: 1;
		min-width: 0;
	}

	.ai-tool-description::before {
		content: "\2014";
		margin-right: 6px;
	}

	.loading-state {
		text-align: center;
		padding: 32px;
		color: var(--text-secondary);
		font-size: var(--text-md, 0.875rem);
	}
</style>
