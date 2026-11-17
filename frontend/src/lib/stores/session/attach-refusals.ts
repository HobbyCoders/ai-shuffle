import { SvelteMap } from 'svelte/reactivity';

/** Late attach failures belong to the chat, not the sheet that has unmounted. */
export const attachRefusals = new SvelteMap<string, string>();
