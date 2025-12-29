<script lang="ts">
	import { MessageSquare, Terminal, FolderOpen, User, Bot } from 'lucide-svelte';
	import WelcomeCard from './WelcomeCard.svelte';

	interface Props {
		onCreateCard: (type: string) => void;
	}

	let { onCreateCard }: Props = $props();

	const cards = [
		{
			type: 'chat',
			label: 'CHAT',
			description: 'Start a conversation',
			icon: MessageSquare,
			shortcut: '⌘N',
			rotation: -10
		},
		{
			type: 'terminal',
			label: 'TERMINAL',
			description: 'Open command line',
			icon: Terminal,
			shortcut: '⌘T',
			rotation: -5
		},
		{
			type: 'project',
			label: 'PROJECTS',
			description: 'Manage workspaces',
			icon: FolderOpen,
			shortcut: '⌘P',
			rotation: 0
		},
		{
			type: 'profile',
			label: 'PROFILES',
			description: 'Configure agents',
			icon: User,
			shortcut: '⌘⇧P',
			rotation: 5
		},
		{
			type: 'subagent',
			label: 'SUBAGENTS',
			description: 'Specialized assistants',
			icon: Bot,
			shortcut: '⌘⇧S',
			rotation: 10
		}
	];
</script>

<div class="cards-container">
	{#each cards as card, i}
		<WelcomeCard {...card} index={i} onclick={() => onCreateCard(card.type)} />
	{/each}
</div>

<style>
	.cards-container {
		display: flex;
		gap: 1.75rem;
		perspective: 1200px;
		margin-top: 0.5rem;
	}

	@media (max-width: 640px) {
		.cards-container {
			flex-direction: column;
			gap: 1rem;
			align-items: center;
		}
	}
</style>
