<script lang="ts">
	import { createEventDispatcher } from 'svelte';

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
	}

	let { request, isFirst = true, queueCount = 1, compact = false }: Props = $props();

	const dispatch = createEventDispatcher<{
		respond: {
			request_id: string;
			decision: 'allow' | 'deny';
			remember?: 'none' | 'session' | 'profile';
			pattern?: string;
		};
	}>();

	let showDetails = $state(false);
	let showRememberOptions = $state(false);
	let rememberChoice = $state<'none' | 'session' | 'profile'>('none');
	let customPattern = $state('');

	// Tool display info with better icons and colors
	const toolInfo: Record<string, { icon: string; bgColor: string; textColor: string; borderColor: string; label: string }> = {
		Bash: {
			icon: 'terminal',
			bgColor: 'bg-emerald-500/10',
			textColor: 'text-emerald-400',
			borderColor: 'border-emerald-500/30',
			label: 'Run Command'
		},
		Read: {
			icon: 'file-text',
			bgColor: 'bg-blue-500/10',
			textColor: 'text-blue-400',
			borderColor: 'border-blue-500/30',
			label: 'Read File'
		},
		Write: {
			icon: 'file-plus',
			bgColor: 'bg-amber-500/10',
			textColor: 'text-amber-400',
			borderColor: 'border-amber-500/30',
			label: 'Write File'
		},
		Edit: {
			icon: 'edit-3',
			bgColor: 'bg-orange-500/10',
			textColor: 'text-orange-400',
			borderColor: 'border-orange-500/30',
			label: 'Edit File'
		},
		Glob: {
			icon: 'search',
			bgColor: 'bg-purple-500/10',
			textColor: 'text-purple-400',
			borderColor: 'border-purple-500/30',
			label: 'Find Files'
		},
		Grep: {
			icon: 'file-search',
			bgColor: 'bg-violet-500/10',
			textColor: 'text-violet-400',
			borderColor: 'border-violet-500/30',
			label: 'Search Content'
		},
		WebFetch: {
			icon: 'globe',
			bgColor: 'bg-cyan-500/10',
			textColor: 'text-cyan-400',
			borderColor: 'border-cyan-500/30',
			label: 'Fetch URL'
		},
		WebSearch: {
			icon: 'search',
			bgColor: 'bg-sky-500/10',
			textColor: 'text-sky-400',
			borderColor: 'border-sky-500/30',
			label: 'Web Search'
		},
		Task: {
			icon: 'cpu',
			bgColor: 'bg-indigo-500/10',
			textColor: 'text-indigo-400',
			borderColor: 'border-indigo-500/30',
			label: 'Run Agent'
		},
		NotebookEdit: {
			icon: 'book-open',
			bgColor: 'bg-rose-500/10',
			textColor: 'text-rose-400',
			borderColor: 'border-rose-500/30',
			label: 'Edit Notebook'
		}
	};

	function getToolInfo(name: string) {
		return toolInfo[name] || {
			icon: 'tool',
			bgColor: 'bg-gray-500/10',
			textColor: 'text-gray-400',
			borderColor: 'border-gray-500/30',
			label: name
		};
	}

	// Get a brief summary of what the tool will do
	function getToolSummary(): string {
		const input = request.tool_input;
		switch (request.tool_name) {
			case 'Bash':
				return String(input.command || '').substring(0, 100);
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
				return String(input.description || input.prompt || '').substring(0, 80);
			default:
				return JSON.stringify(input).substring(0, 80);
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

	function handleAllow() {
		dispatch('respond', {
			request_id: request.request_id,
			decision: 'allow',
			remember: rememberChoice,
			pattern: rememberChoice !== 'none' ? (customPattern || getSuggestedPattern()) : undefined
		});
	}

	function handleDeny() {
		dispatch('respond', {
			request_id: request.request_id,
			decision: 'deny',
			remember: rememberChoice,
			pattern: rememberChoice !== 'none' ? (customPattern || getSuggestedPattern()) : undefined
		});
	}

	function handleAllowAlways() {
		dispatch('respond', {
			request_id: request.request_id,
			decision: 'allow',
			remember: 'profile',
			pattern: getSuggestedPattern()
		});
	}

	const info = $derived(getToolInfo(request.tool_name));
	const summary = $derived(getToolSummary());
	const formattedInput = $derived(formatToolInput(request.tool_input));
	const suggestedPattern = $derived(getSuggestedPattern());
</script>

<!-- Main Permission Card -->
<div class="permission-card rounded-xl border-2 border-amber-500/40 bg-gradient-to-b from-amber-500/5 to-transparent shadow-lg shadow-amber-500/5 overflow-hidden">
	<!-- Header Bar -->
	<div class="flex items-center gap-3 px-4 py-3 bg-amber-500/10 border-b border-amber-500/20">
		<!-- Tool Icon -->
		<div class="flex-shrink-0 w-10 h-10 rounded-lg {info.bgColor} {info.borderColor} border flex items-center justify-center">
			{#if info.icon === 'terminal'}
				<svg class="w-5 h-5 {info.textColor}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
				</svg>
			{:else if info.icon === 'file-text' || info.icon === 'file-plus'}
				<svg class="w-5 h-5 {info.textColor}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
				</svg>
			{:else if info.icon === 'edit-3'}
				<svg class="w-5 h-5 {info.textColor}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
				</svg>
			{:else if info.icon === 'search' || info.icon === 'file-search'}
				<svg class="w-5 h-5 {info.textColor}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
				</svg>
			{:else if info.icon === 'globe'}
				<svg class="w-5 h-5 {info.textColor}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9" />
				</svg>
			{:else if info.icon === 'cpu'}
				<svg class="w-5 h-5 {info.textColor}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" />
				</svg>
			{:else if info.icon === 'book-open'}
				<svg class="w-5 h-5 {info.textColor}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" />
				</svg>
			{:else}
				<svg class="w-5 h-5 {info.textColor}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
				</svg>
			{/if}
		</div>

		<!-- Title & Badge -->
		<div class="flex-1 min-w-0">
			<div class="flex items-center gap-2 flex-wrap">
				<span class="font-semibold text-foreground">{info.label}</span>
				<span class="px-2 py-0.5 text-xs rounded-full bg-amber-500/20 text-amber-400 font-medium">
					Permission Required
				</span>
			</div>
			<div class="text-xs text-muted-foreground mt-0.5 truncate">
				{request.tool_name}
			</div>
		</div>

		<!-- Queue Badge (mobile-friendly) -->
		{#if queueCount > 1}
			<div class="flex-shrink-0 px-2.5 py-1 rounded-full bg-amber-500/20 text-amber-300 text-xs font-medium">
				{queueCount}
			</div>
		{/if}
	</div>

	<!-- Summary -->
	<div class="px-4 py-3">
		<div class="text-sm text-foreground/90 font-mono bg-muted/30 rounded-lg px-3 py-2 break-all">
			{summary || 'No details available'}
		</div>

		<!-- Expand/Collapse Details Button -->
		<button
			type="button"
			onclick={() => showDetails = !showDetails}
			class="mt-2 text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 transition-colors"
		>
			<svg class="w-3.5 h-3.5 transition-transform {showDetails ? 'rotate-90' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
			</svg>
			{showDetails ? 'Hide' : 'Show'} full details
		</button>

		<!-- Expanded Details -->
		{#if showDetails}
			<div class="mt-3 space-y-2 animate-in slide-in-from-top-2 duration-200">
				{#each formattedInput as { key, value, truncated }}
					<div class="text-sm">
						<span class="text-muted-foreground text-xs uppercase tracking-wide">{key}</span>
						<pre class="mt-1 p-2.5 bg-muted/40 rounded-lg text-xs text-foreground overflow-x-auto whitespace-pre-wrap break-all font-mono border border-border/50">{value}{#if truncated}<span class="text-muted-foreground">... (truncated)</span>{/if}</pre>
					</div>
				{/each}
			</div>
		{/if}
	</div>

	<!-- Remember Options -->
	{#if showRememberOptions}
		<div class="px-4 py-3 border-t border-border/50 bg-muted/10 animate-in slide-in-from-top-2 duration-200">
			<div class="text-xs font-medium text-muted-foreground mb-2">Remember this choice:</div>
			<div class="grid grid-cols-1 sm:grid-cols-3 gap-2">
				<label class="flex items-center gap-2 p-2 rounded-lg bg-muted/30 border border-border/50 cursor-pointer hover:bg-muted/50 transition-colors {rememberChoice === 'none' ? 'ring-1 ring-primary' : ''}">
					<input
						type="radio"
						name="remember-{request.request_id}"
						value="none"
						bind:group={rememberChoice}
						class="w-4 h-4 text-primary"
					/>
					<div>
						<div class="text-sm font-medium">Just once</div>
						<div class="text-xs text-muted-foreground">Ask again next time</div>
					</div>
				</label>
				<label class="flex items-center gap-2 p-2 rounded-lg bg-muted/30 border border-border/50 cursor-pointer hover:bg-muted/50 transition-colors {rememberChoice === 'session' ? 'ring-1 ring-primary' : ''}">
					<input
						type="radio"
						name="remember-{request.request_id}"
						value="session"
						bind:group={rememberChoice}
						class="w-4 h-4 text-primary"
					/>
					<div>
						<div class="text-sm font-medium">This session</div>
						<div class="text-xs text-muted-foreground">Until chat ends</div>
					</div>
				</label>
				<label class="flex items-center gap-2 p-2 rounded-lg bg-muted/30 border border-border/50 cursor-pointer hover:bg-muted/50 transition-colors {rememberChoice === 'profile' ? 'ring-1 ring-primary' : ''}">
					<input
						type="radio"
						name="remember-{request.request_id}"
						value="profile"
						bind:group={rememberChoice}
						class="w-4 h-4 text-primary"
					/>
					<div>
						<div class="text-sm font-medium">Always</div>
						<div class="text-xs text-muted-foreground">Save to profile</div>
					</div>
				</label>
			</div>

			{#if rememberChoice !== 'none'}
				<div class="mt-3">
					<label class="text-xs text-muted-foreground">Pattern to match:</label>
					<div class="flex gap-2 mt-1">
						<input
							type="text"
							bind:value={customPattern}
							placeholder={suggestedPattern}
							class="flex-1 px-3 py-2 text-sm bg-input border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary/50 font-mono"
						/>
					</div>
					<div class="text-xs text-muted-foreground mt-1">
						Use <code class="px-1 py-0.5 bg-muted rounded">*</code> as wildcard
					</div>
				</div>
			{/if}
		</div>
	{/if}

	<!-- Action Buttons -->
	<div class="px-4 py-3 border-t border-border/50 bg-muted/5">
		<!-- Mobile: Stack buttons vertically, Desktop: Horizontal -->
		<div class="flex flex-col sm:flex-row gap-2 sm:items-center sm:justify-between">
			<!-- Options Toggle -->
			<button
				type="button"
				onclick={() => showRememberOptions = !showRememberOptions}
				class="text-sm text-muted-foreground hover:text-foreground flex items-center gap-1.5 transition-colors order-2 sm:order-1"
			>
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4" />
				</svg>
				{showRememberOptions ? 'Hide options' : 'Options'}
			</button>

			<!-- Primary Action Buttons -->
			<div class="flex gap-2 order-1 sm:order-2">
				<!-- Deny Button -->
				<button
					type="button"
					onclick={handleDeny}
					class="flex-1 sm:flex-none px-4 py-2.5 sm:py-2 text-sm font-medium rounded-lg
						bg-red-500/10 text-red-400 border border-red-500/30
						hover:bg-red-500/20 hover:border-red-500/50
						active:bg-red-500/30
						transition-all duration-150"
				>
					Deny
				</button>

				<!-- Allow Button -->
				<button
					type="button"
					onclick={handleAllow}
					class="flex-1 sm:flex-none px-5 py-2.5 sm:py-2 text-sm font-medium rounded-lg
						bg-emerald-500/10 text-emerald-400 border border-emerald-500/30
						hover:bg-emerald-500/20 hover:border-emerald-500/50
						active:bg-emerald-500/30
						transition-all duration-150"
				>
					Allow
				</button>

				<!-- Allow Always (shown when queue > 1) -->
				{#if queueCount > 1 && isFirst}
					<button
						type="button"
						onclick={handleAllowAlways}
						class="hidden sm:flex px-4 py-2 text-sm font-medium rounded-lg
							bg-emerald-600/20 text-emerald-300 border border-emerald-500/40
							hover:bg-emerald-600/30 hover:border-emerald-500/60
							active:bg-emerald-600/40
							transition-all duration-150 items-center gap-1.5"
					>
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
						</svg>
						Allow All Similar
					</button>
				{/if}
			</div>
		</div>

		<!-- Mobile: Allow All Similar as full-width button -->
		{#if queueCount > 1 && isFirst}
			<button
				type="button"
				onclick={handleAllowAlways}
				class="sm:hidden mt-2 w-full px-4 py-2.5 text-sm font-medium rounded-lg
					bg-emerald-600/20 text-emerald-300 border border-emerald-500/40
					hover:bg-emerald-600/30 hover:border-emerald-500/60
					active:bg-emerald-600/40
					transition-all duration-150 flex items-center justify-center gap-1.5"
			>
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
				</svg>
				Allow All Similar ({queueCount})
			</button>
		{/if}
	</div>
</div>

<style>
	.permission-card {
		animation: pulse-glow 2s ease-in-out infinite;
	}

	@keyframes pulse-glow {
		0%, 100% {
			box-shadow: 0 0 0 0 rgba(245, 158, 11, 0),
						0 4px 6px -1px rgba(245, 158, 11, 0.05);
		}
		50% {
			box-shadow: 0 0 20px 0 rgba(245, 158, 11, 0.15),
						0 4px 6px -1px rgba(245, 158, 11, 0.1);
		}
	}

	.animate-in {
		animation: animate-in 0.2s ease-out;
	}

	.slide-in-from-top-2 {
		--tw-enter-translate-y: -0.5rem;
	}

	@keyframes animate-in {
		from {
			opacity: 0;
			transform: translateY(var(--tw-enter-translate-y, 0));
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}
</style>
