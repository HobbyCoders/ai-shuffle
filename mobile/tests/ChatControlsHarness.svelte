<script lang="ts">
	import { Menu, CircleCheck } from 'lucide-svelte';
	import ChatInput from '../../frontend/src/lib/components/chat/ChatInput.svelte';
	import RunningTasksList from '../../frontend/src/lib/components/chat/RunningTasksList.svelte';
	import BannerHost from '../../frontend/src/lib/components/BannerHost.svelte';
	import { activeSession } from '../../frontend/src/lib/stores/active-session';
	import type { PermissionRequestData } from '../../frontend/src/lib/components/PermissionRequest.svelte';
	import type { UserQuestionData } from '../../frontend/src/lib/components/UserQuestion.svelte';
	import ConnectedWorkerBar from '../src/shell/ConnectedWorkerBar.svelte';
	import MobilePromptDock from '../src/shell/MobilePromptDock.svelte';

	let { permissions: initialPermissions, questions: initialQuestions, assistant, title }: {
		permissions: PermissionRequestData[]; questions: UserQuestionData[]; assistant: boolean; title: string;
	} = $props();

	// The fixture seeds the queue once; answers then remove items locally.
	// svelte-ignore state_referenced_locally
	let permissions = $state(initialPermissions);
	// svelte-ignore state_referenced_locally
	let questions = $state(initialQuestions);
	let browserOpen = $state(false);
	const answers: unknown[] = [];
	(window as unknown as { fixtureAnswers: unknown[] }).fixtureAnswers = answers;
</script>

<BannerHost />

<div class="app-shell">
	<ConnectedWorkerBar />
	<header class="topbar">
		<button type="button" class="icon-btn" aria-label="Open menu"><Menu size={22} /></button>
		<div class="title"><strong>{title}</strong><span>{assistant ? 'Your assistant' : 'ai-shuffle'}</span></div>
		{#if !assistant}<button type="button" class="git-btn" aria-label="Git: up to date"><CircleCheck size={16} />Up to date</button>{/if}
	</header>
	<main class="chat-view">
		<div class="transcript">
			{#each $activeSession.messages as m (m.id)}
				<div class={(m as { type?: string }).type === 'user' ? 'msg-user' : 'msg-text'}>{m.content}</div>
			{/each}
			<div class="tool-row"><b>Read</b><code>desktop/electron-builder.yml</code></div>
			<div class="tool-row"><b>Grep</b><code>"linux" in desktop/</code></div>
			{#if !assistant}<RunningTasksList sessionId={$activeSession.sessionId ?? null} />{/if}
		</div>
		<div class="input-slot input-wrapper">
			<MobilePromptDock
				sessionId={$activeSession.sessionId}
				{permissions}
				{questions}
				working
				connected
				onPermission={(d) => { answers.push(d); setTimeout(() => (permissions = permissions.filter((p) => p.request_id !== d.request_id)), 1000); }}
				onQuestion={(d) => { answers.push(d); setTimeout(() => (questions = questions.filter((q) => q.request_id !== d.request_id)), 1000); }}
			/>
			<ChatInput compact assistantMode={assistant} onBrowserToggle={() => (browserOpen = !browserOpen)} {browserOpen} />
		</div>
	</main>
</div>

<style>
	:global(body) { margin: 0; background: var(--background); color: var(--foreground); font-family: var(--font-sans, Inter, system-ui, sans-serif); }
	.app-shell { display: flex; flex-direction: column; height: 100dvh; height: var(--app-viewport-height, 100dvh); overflow: hidden; }
	.topbar { display: grid; grid-template-columns: 44px minmax(0, 1fr) auto; align-items: center; gap: 4px; padding: 4px 10px 6px 6px; border-bottom: 1px solid var(--border-subtle); }
	.icon-btn { width: 44px; height: 44px; display: grid; place-items: center; border: 0; background: transparent; color: var(--foreground); }
	.title { min-width: 0; display: flex; flex-direction: column; }
	.title strong { font-size: 14.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.title span { font-size: 12px; color: var(--muted-foreground); }
	.git-btn { min-height: 44px; padding: 0 10px; display: flex; align-items: center; gap: 6px; border: 1px solid var(--border); border-radius: var(--radius); background: transparent; color: var(--muted-foreground); font: inherit; font-size: 12.5px; }
	.chat-view { position: relative; flex: 1; min-height: 0; display: flex; flex-direction: column; }
	.transcript { flex: 1; min-height: 0; overflow-y: auto; padding: 14px 16px 10px; display: flex; flex-direction: column; gap: 12px; }
	.msg-user { align-self: flex-end; max-width: 84%; padding: 10px 13px; border-radius: 16px; background: var(--muted); font-size: 14px; }
	.msg-text { font-size: 14px; line-height: 1.55; }
	.tool-row { display: flex; gap: 8px; font-size: 12.5px; color: var(--muted-foreground); }
	.tool-row b { color: var(--foreground); font-weight: 550; }
	.tool-row code { font-family: var(--font-mono); font-size: 12px; color: var(--foreground); }
	.input-slot { flex-shrink: 0; }
</style>
