import { onMount } from 'svelte';
import { resourceSync, type ResourceName } from './resource-sync';

/** Subscribe only for this mounted view; no remounts or navigation side effects. */
export function watchResources(names: readonly ResourceName[], refresh: () => void | Promise<unknown>) {
	onMount(() => resourceSync.subscribe(names, refresh));
}
