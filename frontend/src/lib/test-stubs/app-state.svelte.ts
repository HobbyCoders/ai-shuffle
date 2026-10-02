/**
 * Test stub for SvelteKit's `$app/state` virtual module (aliased in
 * vitest.config.ts). `page` is plain reactive state: tests set
 * `page.state = {...}` to simulate shallow-routing history (Back/Forward).
 */
export const page = $state({
	url: new URL('http://localhost/'),
	params: {} as Record<string, string>,
	route: { id: null as string | null },
	status: 200,
	error: null as unknown,
	data: {} as Record<string, unknown>,
	form: null as unknown,
	state: {} as Record<string, unknown>
});

export const navigating = $state({ current: null as unknown });
export const updated = $state({ current: false, check: async () => false });
