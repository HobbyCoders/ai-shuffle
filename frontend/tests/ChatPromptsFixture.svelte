<script lang="ts">
	/**
	 * The in-chat question and confirmation cards beside the established chat
	 * surfaces they sit with (the question tray, a permission prompt, tool rows),
	 * every state on one page, plus the Attach a project sheet. Components run
	 * unchanged; tests/vite.chat-prompts.config.js answers their requests.
	 */
	import QuestionTray from '../src/lib/components/chat/QuestionTray.svelte';
	import ToolUseMessage from '../src/lib/components/chat/ToolUseMessage.svelte';
	import PermissionRequest from '../src/lib/components/PermissionRequest.svelte';
	import ApprovalRequestCard from '../src/lib/components/chat/ApprovalRequestCard.svelte';
	import ApprovalOutcome from '../src/lib/components/chat/ApprovalOutcome.svelte';
	import GenerationChoiceCard from '../src/lib/components/chat/GenerationChoiceCard.svelte';
	import GenerationSavedChoice from '../src/lib/components/chat/GenerationSavedChoice.svelte';
	import AttachProjectSheet from '../src/lib/components/layout/AttachProjectSheet.svelte';
	import GenerationChoiceDetail from '../src/lib/components/settings/models/GenerationChoiceDetail.svelte';
	import '../src/lib/components/settings/shell/settings.css';
	import type { GenerationOption } from '../src/lib/utils/generation-choice';
	import type { ApprovalCardData } from '../src/lib/utils/assistant-confirm';

	let { only = null, sheetOpen = false }: { only?: string | null; sheetOpen?: boolean } = $props();
	let open = $state(false);
	$effect.pre(() => {
		if (sheetOpen) open = true;
	});
	const show = (section: string) => !only || only === section;

	const question = {
		request_id: 'q1',
		tool_use_id: 't1',
		questions: [
			{
				question: 'Which branch should the release notes target?',
				header: 'Branch',
				multiSelect: false,
				options: [
					{ label: 'main', description: 'The next stable release' },
					{ label: 'edge', description: 'The nightly channel' }
				]
			}
		]
	};

	const base = { question: 'May I run this?', chat_session_id: 'c1', attempt: 1 };
	const approvals: { data: ApprovalCardData; active?: boolean }[] = [
		{ data: { ...base, request_id: 'a1', action: 'run `git push origin fix/attach-sheet`', asker: 'Release helper', status: 'pending' }, active: true },
		{ data: { ...base, request_id: 'a2', action: 'delete `scratch/old-screens`', asker: 'Cleanup agent', status: 'pending' } },
		{ data: { ...base, request_id: 'a3', action: 'restart the build worker', asker: 'Ops agent', status: 'approved' } },
		{ data: { ...base, request_id: 'a4', action: 'run `rm -rf build/`', status: 'declined', attempt: 2 } },
		{ data: { ...base, request_id: 'a5', action: '', question: 'Send the weekly summary to the team?', status: 'expired' } },
		{ data: { ...base, request_id: 'a6', action: 'open a pull request', asker: 'Docs agent', status: 'cancelled' } }
	];
	const outcomes = [
		{ decision: 'approved', action: 'restart the build worker', asker: 'Ops agent', request_id: 'a3' },
		{ decision: 'declined', action: 'run `rm -rf build/`', request_id: 'a4' },
		{ decision: 'asked_again', action: 'run `git push origin fix/attach-sheet`', asker: 'Release helper', request_id: 'a1' }
	];

	const FAL: GenerationOption = {
		source: 'fal',
		account: 'fal-ai',
		worker: 'worker-a',
		model: 'fal-ai/flux/dev',
		kind: 'image',
		options: { image_size: 'square', num_images: 1 },
		verified: false,
		price: { currency: 'USD', unit: 'item', amount: '0.025', total: '0.025', status: 'estimated' },
		quick_settings: { image_size: 'square', num_images: 1 },
		schema: {
			fields: [
				{ name: 'prompt', type: 'string', is_primary_text: true },
				{ name: 'image_size', type: 'enum', title: 'Image size', enum: ['square', 'landscape_16_9'], default: 'square', description: 'Output aspect ratio' },
				{ name: 'num_images', type: 'integer', title: 'Images', minimum: 1, maximum: 4, default: 1 },
				{ name: 'guidance_scale', type: 'number', title: 'Guidance', minimum: 1, maximum: 20, default: 3.5 },
				{ name: 'enable_safety_checker', type: 'boolean', default: true },
				{ name: 'seed', type: 'integer' }
			]
		},
		available: true,
		reason: null,
		label: 'Same as last time'
	};
	const GROK: GenerationOption = {
		source: 'supergrok',
		account: 'grok-1',
		worker: 'worker-b',
		model: 'grok-imagine-image',
		kind: 'image',
		options: { size: '1k', format: 'jpeg' },
		verified: true,
		price: { currency: 'USD', unit: 'item', amount: '0', total: '0', status: 'included' },
		quick_settings: { size: '1k', format: 'jpeg' },
		schema: {
			fields: {
				size: [
					{ value: '1k', verified: true, selectable: true },
					{ value: '2k', verified: false, selectable: false, reason: 'not yet verified' }
				],
				format: [{ value: 'jpeg', verified: true, selectable: true }]
			}
		},
		available: true,
		reason: null
	};
	const OPENAI: GenerationOption = {
		...GROK,
		source: 'openai',
		account: 'api-a',
		model: 'gpt-image-2',
		verified: false,
		price: { currency: 'USD', unit: 'item', amount: null, total: null, status: 'unknown' },
		available: false,
		reason: 'No verified media schema is available for this account.',
		schema: null
	};
	const options = [FAL, GROK, OPENAI];
	const answered = 'Use this: {"source":"fal-ai","model":"fal-ai/flux/dev"} at $0.03';
	const saved = {
		kind: 'image' as const,
		choice: { source: 'supergrok', account: 'grok-1', model: 'grok-imagine-image', options: { size: '1k' } },
		price: GROK.price
	};
</script>

<main class="fixture">
	{#if show('existing')}
		<section>
			<h2 class="label">Existing: tool rows, permission prompt, question tray</h2>
			<ToolUseMessage name="Bash" input={{ command: 'npm run check', description: 'Type-check the frontend' }} result="svelte-check found 0 errors" status="complete" />
			<ToolUseMessage name="Read" input={{ file_path: 'frontend/src/app.css' }} status="running" />
			<div class="gap"></div>
			<PermissionRequest request={{ request_id: 'p1', tool_name: 'Bash', tool_input: { command: 'git push origin fix/attach-sheet' }, remember_scopes: ['none', 'session', 'profile'] }} />
			<div class="gap"></div>
			<QuestionTray requests={[question]} onRespond={() => {}} />
		</section>
	{/if}

	{#if show('approvals')}
		<section>
			<h2 class="label">Approvals asked in words</h2>
			<div class="stack">
				{#each approvals as item (item.data.request_id)}
					<ApprovalRequestCard data={item.data} active={item.active ?? false} />
				{/each}
				{#each outcomes as data (data.decision)}
					<ApprovalOutcome {data} />
				{/each}
			</div>
		</section>
	{/if}

	{#if show('generation')}
		<section>
			<h2 class="label">Generation choice</h2>
			<div class="stack">
				<GenerationChoiceCard kind="image" {options} tool="generateImage" prompt="a lighthouse at dusk, oil painting" toolId="t-open" answeredBy={null} />
				<GenerationChoiceCard kind="image" {options} tool="generateImage" prompt="a lighthouse at dusk" toolId="t-done" answeredBy={answered} />
				<div>
					<ToolUseMessage name="generateImage" input={{ prompt: 'a lighthouse at dusk' }} result="Saved image.png" status="complete" />
					<GenerationSavedChoice {saved} />
				</div>
			</div>
		</section>
	{/if}

	{#if only === 'settings'}
		<section class="settings-detail">
			<h2 class="label">Settings, Default models: image</h2>
			<!-- The settings shell's classes carry its row and list styles. -->
			<div class="settings-shell" style="display: block; height: auto"><div class="st-content"><GenerationChoiceDetail kind="image" /></div></div>
		</section>
	{/if}

	<section>
		<h2 class="label">Attach a project</h2>
		<button type="button" class="open-sheet" onclick={() => (open = true)}>Open the attach sheet</button>
	</section>
</main>

<AttachProjectSheet {open} sessionId="fixture" agentName="Juniper" blockedReason={null} onClose={() => (open = false)} />

<style>
	:global(body) {
		background: var(--background);
		color: var(--foreground);
		font-family: var(--font-sans, 'Inter Variable', sans-serif);
	}
	.fixture {
		max-width: 768px;
		margin: 0 auto;
		padding: 24px 16px 80px;
		display: grid;
		gap: 32px;
	}
	.label {
		margin: 0 0 12px;
		font-size: 11px;
		font-weight: 600;
		color: var(--muted-foreground);
	}
	.settings-detail {
		min-height: 640px;
	}
	.stack {
		display: grid;
		gap: 12px;
	}
	.gap {
		height: 12px;
	}
	.open-sheet {
		height: 36px;
		padding: 0 14px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--card);
		color: var(--foreground);
		font: inherit;
		font-size: 13px;
		cursor: pointer;
	}
</style>
