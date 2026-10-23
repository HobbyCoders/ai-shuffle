/**
 * The composer's per-model records are plain module state, not runes. This
 * counter is what the Model tuning page reads to know they changed: it bumps
 * on a local save, on another device's change (`composerPreferences.onRemote`)
 * and on a context-cap change (`contextCapRevision`).
 */
import { composerPreferences, contextCapRevision } from '$lib/stores/composer-preferences';

let revision = $state(0);

export const tuningRevision = {
	get value(): number {
		return revision;
	},
	bump(): void {
		revision += 1;
	},
	/** Follow outside changes until the returned stop is called. */
	follow(): () => void {
		const stopRemote = composerPreferences.onRemote(() => (revision += 1));
		const stopCap = contextCapRevision.subscribe(() => (revision += 1));
		return () => {
			stopRemote();
			stopCap();
		};
	}
};
