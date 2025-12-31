<script lang="ts">
	import { MessageSquare, History, FolderOpen, User, Bot, Key, Files } from 'lucide-svelte';
	import WelcomeCard from './WelcomeCard.svelte';

	interface Props {
		onCreateCard: (type: string) => void;
		isApiUser?: boolean;
	}

	let { onCreateCard, isApiUser = false }: Props = $props();

	// Cards available to admin users
	const adminCards = [
		{
			type: 'chat',
			label: 'CHAT',
			description: 'Start a conversation',
			icon: MessageSquare,
			shortcut: '⌘N',
			rotation: -10,
			offsetY: 24
		},
		{
			type: 'recent-sessions',
			label: 'RECENT',
			description: 'Recent conversations',
			icon: History,
			shortcut: '⌘R',
			rotation: -5,
			offsetY: 8
		},
		{
			type: 'project',
			label: 'PROJECTS',
			description: 'Manage workspaces',
			icon: FolderOpen,
			shortcut: '⌘P',
			rotation: 0,
			offsetY: 0
		},
		{
			type: 'profile',
			label: 'PROFILES',
			description: 'Configure agents',
			icon: User,
			shortcut: '⌘⇧P',
			rotation: 5,
			offsetY: 8
		},
		{
			type: 'subagent',
			label: 'SUBAGENTS',
			description: 'Specialized assistants',
			icon: Bot,
			shortcut: '⌘⇧S',
			rotation: 10,
			offsetY: 24
		}
	];

	// Cards available to API users (limited set)
	const apiUserCards = [
		{
			type: 'chat',
			label: 'CHAT',
			description: 'Start a conversation',
			icon: MessageSquare,
			shortcut: '⌘N',
			rotation: -8,
			offsetY: 16
		},
		{
			type: 'recent-sessions',
			label: 'RECENT',
			description: 'Recent conversations',
			icon: History,
			shortcut: '⌘R',
			rotation: 0,
			offsetY: 0
		},
		{
			type: 'file-browser',
			label: 'FILES',
			description: 'Browse project files',
			icon: Files,
			shortcut: '⌘F',
			rotation: 0,
			offsetY: 0
		},
		{
			type: 'user-settings',
			label: 'SETTINGS',
			description: 'API keys & profile',
			icon: Key,
			shortcut: '⌘,',
			rotation: 8,
			offsetY: 16
		}
	];

	const cards = $derived(isApiUser ? apiUserCards : adminCards);
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
