<script lang="ts">
	import './deck/cards/card-design-system.css';

	export interface PermissionRequestData {
		request_id: string;
		tool_name: string;
		tool_input: Record<string, unknown>;
		queue_position?: number;
		queue_total?: number;
	}

	interface Props {
		request: PermissionRequestData;
		isFirst?: boolean;
		queueCount?: number;
		compact?: boolean;
		onRespond?: (detail: {
			request_id: string;
			decision: 'allow' | 'deny';
			remember?: 'none' | 'session' | 'profile';
			pattern?: string;
		}) => void;
	}

	let { request, isFirst = true, queueCount = 1, compact = false, onRespond }: Props = $props();

	let showDetails = $state(false);
	let rememberChoice = $state<'none' | 'session' | 'profile'>('none');
	let customPattern = $state('');
	let isEditingPattern = $state(false);

	// Tool display configuration - uses theme CSS variables for colors
	// Maps tools to semantic theme colors: success, warning, info, primary, destructive
	const toolConfig: Record<string, {
		icon: string;
		themeColor: 'success' | 'warning' | 'info' | 'primary' | 'destructive' | 'muted';
		label: string;
	}> = {
		Bash: {
			icon: 'M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z',
			themeColor: 'success',
			label: 'Run Command'
		},
		Read: {
			icon: 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z',
			themeColor: 'info',
			label: 'Read File'
		},
		Write: {
			icon: 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z',
			themeColor: 'warning',
			label: 'Write File'
		},
		Edit: {
			icon: 'M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z',
			themeColor: 'warning',
			label: 'Edit File'
		},
		Glob: {
			icon: 'M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z',
			themeColor: 'primary',
			label: 'Find Files'
		},
		Grep: {
			icon: 'M10 21h7a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v11m0 5l4.879-4.879m0 0a3 3 0 104.243-4.242 3 3 0 00-4.243 4.242z',
			themeColor: 'primary',
			label: 'Search Content'
		},
		WebFetch: {
			icon: 'M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9',
			themeColor: 'info',
			label: 'Fetch URL'
		},
		WebSearch: {
			icon: 'M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z',
			themeColor: 'info',
			label: 'Web Search'
		},
		Task: {
			icon: 'M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z',
			themeColor: 'primary',
			label: 'Run Agent'
		},
		NotebookEdit: {
			icon: 'M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253',
			themeColor: 'destructive',
			label: 'Edit Notebook'
		}
	};

	function getToolConfig(name: string) {
		return toolConfig[name] || {
			icon: 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065zM15 12a3 3 0 11-6 0 3 3 0 016 0z',
			themeColor: 'muted' as const,
			label: name
		};
	}

	// Get CSS variable for theme color
	function getThemeColorVar(color: string): string {
		return `var(--${color})`;
	}

	// Get tool summary for display
	function getToolSummary(): string {
		const input = request.tool_input;
		switch (request.tool_name) {
			case 'Bash':
				return String(input.command || '');
			case 'Read':
			case 'Write':
			case 'Edit':
				return String(input.file_path || '');
			case 'Glob':
			case 'Grep':
				return String(input.path || input.pattern || '');
			case 'WebFetch':
				return String(input.url || '');
			case 'WebSearch':
				return String(input.query || '');
			case 'Task':
				return String(input.description || input.prompt || '').substring(0, 100);
			default:
				return JSON.stringify(input).substring(0, 100);
		}
	}

	// Format tool input for detailed display
	function formatToolInput(input: Record<string, unknown>): { key: string; value: string; truncated: boolean }[] {
		const result: { key: string; value: string; truncated: boolean }[] = [];
		const maxLength = 500;

		for (const [key, value] of Object.entries(input)) {
			let displayValue: string;
			let truncated = false;

			if (typeof value === 'string') {
				if (value.length > maxLength) {
					displayValue = value.substring(0, maxLength);
					truncated = true;
				} else {
					displayValue = value;
				}
			} else if (typeof value === 'object' && value !== null) {
				const json = JSON.stringify(value, null, 2);
				if (json.length > maxLength) {
					displayValue = json.substring(0, maxLength);
					truncated = true;
				} else {
					displayValue = json;
				}
			} else {
				displayValue = String(value);
			}

			result.push({ key, value: displayValue, truncated });
		}

		return result;
	}

	// Get suggested pattern based on tool type
	function getSuggestedPattern(): string {
		const input = request.tool_input;

		switch (request.tool_name) {
			case 'Bash': {
				const cmd = (input.command as string) || '';
				const firstWord = cmd.split(' ')[0];
				return `${firstWord} *`;
			}
			case 'Read':
			case 'Write':
			case 'Edit': {
				const path = (input.file_path as string) || '';
				const dir = path.substring(0, path.lastIndexOf('/'));
				return dir ? `${dir}/*` : '*';
			}
			case 'Glob':
			case 'Grep': {
				const path = (input.path as string) || '';
				return path || '*';
			}
			case 'WebFetch': {
				const url = (input.url as string) || '';
				try {
					const parsed = new URL(url);
					return `${parsed.origin}/*`;
				} catch {
					return '*';
				}
			}
			default:
				return '*';
		}
	}

	// Get the pattern to use (custom or suggested)
	function getActivePattern(): string {
		return customPattern || getSuggestedPattern();
	}

	function handleAllow() {
		onRespond?.({
			request_id: request.request_id,
			decision: 'allow',
			remember: rememberChoice,
			pattern: rememberChoice !== 'none' ? getActivePattern() : undefined
		});
	}

	function handleDeny() {
		onRespond?.({
			request_id: request.request_id,
			decision: 'deny',
			remember: rememberChoice,
			pattern: rememberChoice !== 'none' ? getActivePattern() : undefined
		});
	}

	// Keyboard shortcuts
	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey && !isEditingPattern) {
			e.preventDefault();
			handleAllow();
		} else if (e.key === 'Escape') {
			e.preventDefault();
			handleDeny();
		}
	}

	const config = $derived(getToolConfig(request.tool_name));
	const summary = $derived(getToolSummary());
	const formattedInput = $derived(formatToolInput(request.tool_input));
	const suggestedPattern = $derived(getSuggestedPattern());
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- Permission Request Panel -->
<div
	class="card-system permission-panel rounded-2xl border shadow-lg overflow-hidden backdrop-blur-sm"
	style="
		border-color: var(--border);
		background: var(--card);
	"
	role="dialog"
	aria-labelledby="permission-title"
>
	<!-- Compact Header -->
	<div class="flex items-center gap-3 px-4 py-3 border-b border-border/40">
		<!-- Tool Icon -->
		<div
			class="flex-shrink-0 w-9 h-9 rounded-xl border flex items-center justify-center shadow-sm"
			style="
				background: color-mix(in oklch, var(--muted) 50%, var(--card));
				border-color: var(--border);
			"
		>
			<svg class="w-4.5 h-4.5 text-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={config.icon} />
			</svg>
		</div>

		<!-- Title -->
		<div class="flex-1 min-w-0">
			<h3 id="permission-title" class="text-sm font-semibold text-foreground flex items-center gap-2">
				{config.label}
				<span
					class="inline-flex items-center px-1.5 py-0.5 text-[10px] font-medium rounded-md border"
					style="
						background: color-mix(in oklch, var(--muted-foreground) 15%, transparent);
						color: var(--muted-foreground);
						border-color: color-mix(in oklch, var(--muted-foreground) 20%, transparent);
					"
				>
					Permission
				</span>
			</h3>
			<p class="text-xs text-muted-foreground truncate">{request.tool_name}</p>
		</div>

		<!-- Queue Badge -->
		{#if queueCount > 1}
			<div
				class="flex-shrink-0 flex items-center gap-1.5 px-2 py-1 rounded-lg border"
				style="
					background: color-mix(in oklch, var(--muted-foreground) 10%, transparent);
					border-color: color-mix(in oklch, var(--muted-foreground) 20%, transparent);
				"
			>
				<span class="relative flex h-2 w-2">
					<span class="animate-ping absolute inline-flex h-full w-full rounded-full opacity-75" style="background: color-mix(in oklch, var(--muted-foreground) 60%, transparent);"></span>
					<span class="relative inline-flex rounded-full h-2 w-2" style="background: var(--muted-foreground);"></span>
				</span>
				<span class="text-xs font-medium" style="color: var(--muted-foreground);">{queueCount}</span>
			</div>
		{/if}
	</div>

	<!-- Command/Action Summary -->
	<div class="px-4 py-3">
		<div class="relative group">
			<div class="text-sm text-foreground font-mono bg-card/60 rounded-xl px-3 py-2.5 border border-border/40 break-all leading-relaxed">
				{#if request.tool_name === 'Bash'}
					<span class="text-muted-foreground select-none">$ </span>
				{/if}
				{summary || 'No details available'}
			</div>
		</div>

		<!-- Expand Details Toggle -->
		<button
			type="button"
			onclick={() => showDetails = !showDetails}
			class="mt-2 text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 transition-colors"
		>
			<svg
				class="w-3.5 h-3.5 transition-transform duration-200 {showDetails ? 'rotate-90' : ''}"
				fill="none"
				stroke="currentColor"
				viewBox="0 0 24 24"
			>
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
			</svg>
			{showDetails ? 'Hide details' : 'Show details'}
		</button>

		<!-- Expanded Details -->
		{#if showDetails}
			<div class="mt-3 space-y-2 animate-expand">
				{#each formattedInput as { key, value, truncated }}
					<div class="text-sm">
						<span class="text-[10px] uppercase tracking-wider text-muted-foreground font-medium">{key}</span>
						<pre class="mt-1 p-2.5 bg-card/80 rounded-lg text-xs text-foreground overflow-x-auto whitespace-pre-wrap break-all font-mono border border-border/40 max-h-32 overflow-y-auto">{value}{#if truncated}<span class="text-muted-foreground">… (truncated)</span>{/if}</pre>
					</div>
				{/each}
			</div>
		{/if}
	</div>

	<!-- Remember Options - Always Visible -->
	<div class="px-4 py-3 border-t border-border/40 bg-card/30">
		<div class="space-y-2">
			<!-- Just Once -->
			<label class="remember-option {rememberChoice === 'none' ? 'selected' : ''}">
				<input
					type="radio"
					name="remember-{request.request_id}"
					value="none"
					bind:group={rememberChoice}
					class="sr-only"
				/>
				<div class="flex items-center gap-3">
					<div class="radio-indicator">
						{#if rememberChoice === 'none'}
							<div class="radio-dot"></div>
						{/if}
					</div>
					<div class="flex-1">
						<span class="text-sm font-medium text-foreground">Just this once</span>
					</div>
				</div>
			</label>

			<!-- This Session -->
			<label class="remember-option {rememberChoice === 'session' ? 'selected' : ''}">
				<input
					type="radio"
					name="remember-{request.request_id}"
					value="session"
					bind:group={rememberChoice}
					class="sr-only"
				/>
				<div class="flex items-center gap-3">
					<div class="radio-indicator">
						{#if rememberChoice === 'session'}
							<div class="radio-dot"></div>
						{/if}
					</div>
					<div class="flex-1 flex items-center justify-between gap-2">
						<span class="text-sm font-medium text-foreground">This session</span>
						{#if rememberChoice === 'session'}
							<div class="pattern-display">
								{#if isEditingPattern}
									<input
										type="text"
										bind:value={customPattern}
										placeholder={suggestedPattern}
										onfocus={() => isEditingPattern = true}
										onblur={() => isEditingPattern = false}
										onkeydown={(e) => e.key === 'Enter' && (isEditingPattern = false)}
										class="pattern-input"
									/>
								{:else}
									<button
										type="button"
										onclick={() => isEditingPattern = true}
										class="pattern-badge"
									>
										<code>{customPattern || suggestedPattern}</code>
										<svg class="w-3 h-3 ml-1 opacity-60" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
										</svg>
									</button>
								{/if}
							</div>
						{/if}
					</div>
				</div>
			</label>

			<!-- Always -->
			<label class="remember-option {rememberChoice === 'profile' ? 'selected' : ''}">
				<input
					type="radio"
					name="remember-{request.request_id}"
					value="profile"
					bind:group={rememberChoice}
					class="sr-only"
				/>
				<div class="flex items-center gap-3">
					<div class="radio-indicator">
						{#if rememberChoice === 'profile'}
							<div class="radio-dot"></div>
						{/if}
					</div>
					<div class="flex-1 flex items-center justify-between gap-2">
						<span class="text-sm font-medium text-foreground">Always allow</span>
						{#if rememberChoice === 'profile'}
							<div class="pattern-display">
								{#if isEditingPattern}
									<input
										type="text"
										bind:value={customPattern}
										placeholder={suggestedPattern}
										onfocus={() => isEditingPattern = true}
										onblur={() => isEditingPattern = false}
										onkeydown={(e) => e.key === 'Enter' && (isEditingPattern = false)}
										class="pattern-input"
									/>
								{:else}
									<button
										type="button"
										onclick={() => isEditingPattern = true}
										class="pattern-badge"
									>
										<code>{customPattern || suggestedPattern}</code>
										<svg class="w-3 h-3 ml-1 opacity-60" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
										</svg>
									</button>
								{/if}
							</div>
						{/if}
					</div>
				</div>
			</label>
		</div>

		<!-- Pattern Help Text -->
		{#if rememberChoice !== 'none'}
			<p class="mt-2 text-[11px] text-muted-foreground">
				Use <code class="px-1 py-0.5 bg-muted/50 rounded text-[10px]">*</code> as wildcard to match multiple inputs
			</p>
		{/if}
	</div>

	<!-- Action Buttons -->
	<div class="px-4 py-3 border-t border-border/40 bg-card/20">
		<div class="flex gap-2">
			<!-- Deny Button -->
			<button
				type="button"
				onclick={handleDeny}
				class="deny-btn"
			>
				Deny
			</button>

			<!-- Allow Button -->
			<button
				type="button"
				onclick={handleAllow}
				class="allow-btn"
			>
				{rememberChoice === 'none' ? 'Allow' : rememberChoice === 'session' ? 'Allow (Session)' : 'Allow Always'}
			</button>
		</div>

		<!-- Keyboard Hints -->
		<div class="mt-2 flex items-center justify-center gap-3 text-[10px] text-muted-foreground">
			<span class="flex items-center gap-1">
				<kbd class="px-1.5 py-0.5 bg-muted/50 rounded text-[9px] font-mono">Enter</kbd>
				<span>Allow</span>
			</span>
			<span class="flex items-center gap-1">
				<kbd class="px-1.5 py-0.5 bg-muted/50 rounded text-[9px] font-mono">Esc</kbd>
				<span>Deny</span>
			</span>
		</div>
	</div>
</div>

<style>
	.permission-panel {
		animation: panel-enter 0.25s cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes panel-enter {
		from {
			opacity: 0;
			transform: translateY(8px) scale(0.98);
		}
		to {
			opacity: 1;
			transform: translateY(0) scale(1);
		}
	}

	.animate-expand {
		animation: expand 0.2s ease-out;
	}

	@keyframes expand {
		from {
			opacity: 0;
			transform: translateY(-8px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	/* Remember Option Styling */
	.remember-option {
		display: block;
		padding: 0.625rem 0.75rem;
		border-radius: var(--radius, 0.75rem);
		border: 1px solid var(--border);
		background: var(--card);
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.remember-option:hover {
		border-color: var(--accent);
		background: color-mix(in oklch, var(--accent) 8%, var(--card));
	}

	.remember-option.selected {
		border-color: var(--selection-ring);
		background: color-mix(in oklch, var(--selection) 8%, var(--card));
		box-shadow: var(--selection-shadow-subtle);
	}

	.remember-option:focus-within {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.radio-indicator {
		width: 1rem;
		height: 1rem;
		border-radius: 50%;
		border: 2px solid var(--muted-foreground);
		display: flex;
		align-items: center;
		justify-content: center;
		transition: all 0.15s ease;
		flex-shrink: 0;
	}

	.remember-option.selected .radio-indicator {
		border-color: var(--selection);
	}

	.radio-dot {
		width: 0.5rem;
		height: 0.5rem;
		border-radius: 50%;
		background: var(--primary);
		animation: dot-pop 0.15s ease;
	}

	@keyframes dot-pop {
		from {
			transform: scale(0);
		}
		to {
			transform: scale(1);
		}
	}

	/* Pattern Display */
	.pattern-display {
		flex-shrink: 0;
	}

	.pattern-badge {
		display: inline-flex;
		align-items: center;
		padding: 0.25rem 0.5rem;
		font-size: 0.7rem;
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: 0.5rem;
		color: var(--foreground);
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.pattern-badge:hover {
		border-color: var(--accent);
		box-shadow: 0 0 12px color-mix(in oklch, var(--accent) 25%, transparent);
	}

	.pattern-badge:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.pattern-badge code {
		font-family: var(--font-mono);
	}

	.pattern-input {
		width: 120px;
		padding: 0.25rem 0.5rem;
		font-size: 0.7rem;
		font-family: var(--font-mono);
		background: var(--input);
		border: 1px solid var(--ring);
		border-radius: 0.5rem;
		color: var(--foreground);
	}

	.pattern-input:focus {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.pattern-input::placeholder {
		color: var(--muted-foreground);
	}

	/* Action Buttons */
	.deny-btn {
		flex: 1 1 0%;
		padding: 0.625rem 1rem;
		font-size: 0.875rem;
		font-weight: 500;
		border-radius: var(--radius, 0.75rem);
		background: var(--secondary);
		color: var(--secondary-foreground);
		border: 1px solid var(--border);
		transition: all 0.15s ease;
	}

	.deny-btn:hover {
		background: color-mix(in oklch, var(--secondary) 80%, var(--foreground));
		border-color: color-mix(in oklch, var(--border) 80%, var(--foreground));
	}

	.deny-btn:active {
		transform: scale(0.98);
	}

	.deny-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.allow-btn {
		flex: 1 1 0%;
		padding: 0.625rem 1.25rem;
		font-size: 0.875rem;
		font-weight: 500;
		border-radius: var(--radius, 0.75rem);
		background: var(--button-primary-gradient, var(--primary));
		color: var(--primary-foreground);
		border: 1px solid transparent;
		box-shadow: var(--shadow-s);
		transition: all 0.15s ease;
	}

	.allow-btn:hover {
		filter: brightness(1.1);
		box-shadow: var(--shadow-m), 0 0 20px var(--glow-color-soft);
	}

	.allow-btn:active {
		transform: scale(0.98);
	}

	.allow-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	@media (min-width: 640px) {
		.deny-btn {
			flex: 0 0 auto;
		}
	}

	/* Mobile optimizations */
	@media (max-width: 639px) {
		.permission-panel {
			border-radius: 1.25rem 1.25rem 0 0;
			border-bottom: none;
		}

		.pattern-input {
			width: 100px;
		}
	}
</style>
