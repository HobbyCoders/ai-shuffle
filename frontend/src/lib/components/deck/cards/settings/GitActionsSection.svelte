<script lang="ts">
	/**
	 * GitActionsSection — customize the messages the git action buttons
	 * (Commit / Push / Pull / Create PR / Finish / Ship It) send to the agent.
	 *
	 * Each action has a built-in default prompt shaped by the structured options
	 * (merge method, pull strategy, …). A custom message here replaces that
	 * default entirely and supports {branch}, {default_branch} and {pr}
	 * placeholders. Persistence rides the existing git_action_settings blob.
	 */
	import { onMount } from 'svelte';
	import { GitBranch, GitCommitHorizontal, GitPullRequest, ArrowUp, ArrowDown, Rocket } from 'lucide-svelte';
	import { gitActionPreferences, type GitActionPreferences } from '$lib/stores/gitActionPreferences.svelte';
	import { getDefaultGitActionPrompt, GIT_ACTION_KEYS, type GitActionKey, type GitPromptContext } from '$lib/utils/git-action-prompts';

	let prefs = $state<GitActionPreferences>(gitActionPreferences.current);

	onMount(() => {
		// The store is synced app-wide in +layout; just mirror it locally.
		// (No load() here: this section remounts on every tab switch, and a
		// re-fired server fetch could clobber an in-progress edit on resolution.)
		const unsub = gitActionPreferences.subscribe((v) => { prefs = v; });
		return unsub;
	});

	interface ActionMeta {
		key: GitActionKey;
		label: string;
		description: string;
		icon: typeof GitBranch;
	}

	// Keyed by GitActionKey so a missing action is a compile error. The rendered
	// list is derived from GIT_ACTION_KEYS (the persisted display order).
	const ACTION_META = {
		commit: {
			label: 'Commit',
			description: 'Stages and commits the current changes (and optionally pushes or opens a PR next).',
			icon: GitCommitHorizontal,
		},
		push: {
			label: 'Push',
			description: 'Pushes the current branch to its remote.',
			icon: ArrowUp,
		},
		pull: {
			label: 'Pull',
			description: 'Pulls the latest changes from the remote into your branch.',
			icon: ArrowDown,
		},
		createPR: {
			label: 'Create PR',
			description: 'Pushes the branch and opens a pull request into the default branch.',
			icon: GitPullRequest,
		},
		finish: {
			label: 'Finish (merge PR)',
			description: 'Merges this branch’s pull request into the default branch.',
			icon: GitBranch,
		},
		shipIt: {
			label: 'Ship It',
			description: 'Commit, push, open/merge the PR and clean up — end to end.',
			icon: Rocket,
		},
	} satisfies Record<GitActionKey, Omit<ActionMeta, 'key'>>;

	const ACTIONS: ActionMeta[] = GIT_ACTION_KEYS.map((key) => ({ key, ...ACTION_META[key] }));

	// Generic preview context: no branch / no PR so placeholders resolve to their
	// honest fallbacks. Structured prefs still shape the shown default.
	const previewCtx = $derived<GitPromptContext>({
		branch: null,
		defaultBranch: 'main',
		needsPull: false,
		isOnDefaultBranch: false,
		branchPRNumber: null,
		prefs,
	});

	function defaultPrompt(key: GitActionKey): string {
		return getDefaultGitActionPrompt(key, previewCtx);
	}

	function customValue(key: GitActionKey): string {
		return prefs.customMessages[key] ?? '';
	}

	function hasCustom(key: GitActionKey): boolean {
		return customValue(key).trim().length > 0;
	}

	function onInput(key: GitActionKey, value: string) {
		gitActionPreferences.setCustomMessage(key, value);
	}

	function resetToDefault(key: GitActionKey) {
		gitActionPreferences.setCustomMessage(key, '');
	}
</script>

<div class="set-group">
	<div class="set-grouphead">
		<h3><GitBranch />Git action messages</h3>
		<p>Customize what each git button sends to the agent. Leave a field empty to use the built-in message.</p>
	</div>

	<div class="ga-hint">
		<p>
			Placeholders you can use in a custom message:
			<code>{'{branch}'}</code> the current branch,
			<code>{'{default_branch}'}</code> the repo’s default branch,
			<code>{'{pr}'}</code> this branch’s pull request.
		</p>
		<p class="ga-hint-note">
			The structured options above (merge method, pull strategy, next step…) only shape the built-in
			messages. A custom message replaces the built-in one entirely.
		</p>
	</div>

	{#each ACTIONS as action (action.key)}
		<div class="set-row stack">
			<div class="lab">
				<h4><action.icon />{action.label}</h4>
				<p>{action.description}</p>
			</div>
			<div class="ctl">
				<textarea
					class="fld"
					rows="3"
					value={customValue(action.key)}
					placeholder={defaultPrompt(action.key)}
					oninput={(e) => onInput(action.key, e.currentTarget.value)}
					aria-label={`Custom message for ${action.label}`}
				></textarea>
				<div class="ga-foot">
					{#if hasCustom(action.key)}
						<span class="ga-badge">Custom</span>
					{:else}
						<span class="ga-badge muted">Using default</span>
					{/if}
					<button
						type="button"
						class="ed-btn sm"
						onclick={() => resetToDefault(action.key)}
						disabled={!hasCustom(action.key)}
						title="Clear the custom message and use the built-in default"
					>Reset to default</button>
				</div>
			</div>
		</div>
	{/each}
</div>

<style>
	.ga-hint {
		padding: 12px 18px 4px;
	}
	.ga-hint p {
		margin: 0 0 6px;
		font-size: 12.5px;
		line-height: 1.5;
		color: var(--muted-foreground);
	}
	.ga-hint-note {
		margin-top: 0;
	}
	.ga-hint code {
		font-family: var(--font-mono, monospace);
		font-size: 11.5px;
		padding: 1px 5px;
		border-radius: 5px;
		background: var(--muted);
		color: var(--foreground);
	}
	.ga-foot {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 10px;
		margin-top: 8px;
	}
	.ga-badge {
		font-family: var(--font-mono, monospace);
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		padding: 2px 7px;
		border-radius: 999px;
		color: var(--primary-foreground);
		background: var(--primary);
	}
	.ga-badge.muted {
		color: var(--muted-foreground);
		background: var(--muted);
	}
</style>
