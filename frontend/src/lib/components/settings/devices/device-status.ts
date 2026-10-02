/** Status words and tones for the Shuffle account row and its detail header. */
import type { StatusTone } from '$lib/settings/types';
import type { AccountFlow } from './account-flow.svelte';

export interface AccountStatus {
	tone: StatusTone;
	/** Header status text. */
	text: string;
	/** One-line list summary. */
	summary: string;
}

/** Short remote access state for facts and summaries. */
export function remoteShort(flow: AccountFlow): string {
	switch (flow.connection?.status) {
		case 'connected':
			return 'On';
		case 'connecting':
			return 'Turning on';
		case 'reconnecting':
			return 'Reconnecting';
		case 'limited':
			return 'Off, limit reached';
		case 'locked':
		case 'unavailable':
			return 'Unavailable';
		default:
			return flow.signedIn ? 'Off' : '';
	}
}

export function accountStatus(flow: AccountFlow): AccountStatus {
	const c = flow.connection;
	if (!c) {
		if (flow.statusError) return { tone: 'err', text: 'Account status unavailable', summary: 'Status unavailable' };
		return { tone: 'standby', text: 'Checking account', summary: 'Checking' };
	}
	if (!c.signed_in) return { tone: 'off', text: 'Not signed in', summary: 'Not signed in' };
	const user = flow.profile ? `@${flow.profile.account.username}` : '';
	const remote = remoteShort(flow).toLowerCase();
	const tone: StatusTone = flow.refusal || c.status === 'limited' ? 'warn' : c.status === 'connected' ? 'ok' : 'busy';
	let text = 'Signed in';
	if (flow.profile) text = `Signed in as ${user}`;
	else if (flow.refusal) text = 'Signed in, remote access off';
	else if (c.profile_ready === false) text = 'Signed in, finishing connection';
	return { tone, text, summary: [user || 'Signed in', remote ? `remote access ${remote}` : ''].filter(Boolean).join(', ') };
}
