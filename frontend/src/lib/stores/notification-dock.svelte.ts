/**
 * The one place notifications are drawn (#1823).
 *
 * Every notice that pops over the page (page banners, the connection banner,
 * computer use, a call in another chat, the restart notice, license and
 * sign-in notices, the install prompt) renders through a NotificationSlot,
 * which hands its content to the mounted NotificationDock. MainPanel mounts the
 * anchored dock under its header, sized to the chat column; RootLayout mounts a
 * fallback that shows only on pages without MainPanel (setup, pairing).
 *
 * With no dock mounted at all (a component rendered on its own in a unit test)
 * a slot draws its content in place.
 */
import { untrack, type Snippet } from 'svelte';

/** Lower draws first. Live page banners (notify.banner) sit at `banners`. */
export const DOCK_ORDER = {
	connection: 10,
	computerUse: 20,
	voiceCall: 30,
	restart: 40,
	banners: 50,
	account: 60,
	install: 70,
} as const;

export interface DockEntry {
	id: number;
	order: number;
	render: Snippet;
}

let nextId = 0;

class NotificationDockRegistry {
	entries = $state<DockEntry[]>([]);
	/** MainPanel docks mounted (under the page header). */
	anchored = $state(0);
	/** RootLayout fallback docks mounted. */
	fallbacks = $state(0);

	/** Whether any dock will draw a slot's content. */
	get hasHost(): boolean {
		return this.anchored > 0 || this.fallbacks > 0;
	}

	/** Entries in draw order: by `order`, then by arrival. */
	get sorted(): DockEntry[] {
		return [...this.entries].sort((a, b) => a.order - b.order || a.id - b.id);
	}

	add(order: number, render: Snippet): number {
		const id = ++nextId;
		// Called from an effect: never let the caller depend on the list it changes.
		untrack(() => { this.entries = [...this.entries, { id, order, render }]; });
		return id;
	}

	remove(id: number): void {
		untrack(() => { this.entries = this.entries.filter((e) => e.id !== id); });
	}

	mount(kind: 'anchored' | 'fallback'): () => void {
		untrack(() => { this[kind === 'anchored' ? 'anchored' : 'fallbacks'] += 1; });
		return () => untrack(() => { this[kind === 'anchored' ? 'anchored' : 'fallbacks'] -= 1; });
	}
}

export const notificationDock = new NotificationDockRegistry();
