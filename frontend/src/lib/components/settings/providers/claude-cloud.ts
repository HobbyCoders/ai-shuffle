/**
 * The "Cloud provider or gateway" Claude method's form logic, kept out of the
 * component so it can be tested: which fields a method and alternative show,
 * what a save sends, and when Save is allowed.
 *
 * Secrets are write-only. The worker returns only whether each is stored; a
 * secret field left blank keeps the stored value when the method is the same,
 * so the form never needs the value back.
 */
import type {
	ClaudeCloudConfig,
	ClaudeCloudField,
	ClaudeCloudMethod,
	ClaudeCloudMethodId
} from '$lib/api/claude-auth';

export function methodOf(config: ClaudeCloudConfig | null, id: ClaudeCloudMethodId | null): ClaudeCloudMethod | null {
	if (!config || !id) return null;
	return config.methods.find((m) => m.id === id) ?? null;
}

/** The saved method's stored secret, by field name. */
function stored(config: ClaudeCloudConfig | null, method: ClaudeCloudMethod, field: ClaudeCloudField): boolean {
	return !!config && config.method === method.id && !!config.secrets[field.name]?.set;
}

/**
 * The alternative to show first: the one the saved values use, else the
 * method's first. Null when the method has no alternatives.
 */
export function initialChoice(config: ClaudeCloudConfig | null, method: ClaudeCloudMethod): string | null {
	if (!method.choices.length) return null;
	if (config?.method === method.id) {
		for (const c of method.choices) {
			const used = method.fields.some(
				(f) => f.choice === c.id && (f.secret ? stored(config, method, f) : !!config.values[f.name])
			);
			if (used) return c.id;
		}
	}
	return method.choices[0].id;
}

/** Fields of the method that belong to no alternative or to the chosen one. */
export function visibleFields(method: ClaudeCloudMethod, choice: string | null): ClaudeCloudField[] {
	return method.fields.filter((f) => !f.choice || f.choice === choice);
}

/** What the inputs start with: the saved non-secret values, secrets always blank. */
export function initialInputs(config: ClaudeCloudConfig | null, method: ClaudeCloudMethod): Record<string, string> {
	const out: Record<string, string> = {};
	for (const f of method.fields) {
		out[f.name] = !f.secret && config?.method === method.id ? (config.values[f.name] ?? '') : '';
	}
	return out;
}

/**
 * The values a save sends: the visible fields only, trimmed, blanks left out
 * (a blank secret keeps the stored one; a blank non-secret is not set).
 * Pasted JSON keeps its inner whitespace.
 */
export function buildValues(
	method: ClaudeCloudMethod,
	choice: string | null,
	inputs: Record<string, string>
): Record<string, string> {
	const out: Record<string, string> = {};
	for (const f of visibleFields(method, choice)) {
		const value = (inputs[f.name] ?? '').trim();
		if (value) out[f.name] = value;
	}
	return out;
}

/** Labels of required visible fields that are neither typed nor stored. */
export function missingFields(
	config: ClaudeCloudConfig | null,
	method: ClaudeCloudMethod,
	choice: string | null,
	inputs: Record<string, string>
): string[] {
	return visibleFields(method, choice)
		.filter((f) => f.required && !(inputs[f.name] ?? '').trim() && !(f.secret && stored(config, method, f)))
		.map((f) => f.label);
}

/** Save is allowed once the warning is acknowledged and every required value is there. */
export function canSave(
	config: ClaudeCloudConfig | null,
	method: ClaudeCloudMethod | null,
	choice: string | null,
	inputs: Record<string, string>,
	acknowledged: boolean
): boolean {
	return !!method && acknowledged && missingFields(config, method, choice, inputs).length === 0;
}

/** The input's placeholder: a stored secret says so, other fields show an example. */
export function placeholderFor(
	config: ClaudeCloudConfig | null,
	method: ClaudeCloudMethod,
	field: ClaudeCloudField
): string {
	if (field.secret && stored(config, method, field)) return 'Stored. Leave blank to keep.';
	if (field.kind === 'url') return 'https://';
	if (field.kind === 'region') return method.id === 'vertex' ? 'us-east5' : 'us-east-1';
	if (field.kind === 'json') return 'Paste the service account key file';
	if (field.kind === 'resource') return 'my-resource';
	return field.required ? '' : 'Optional';
}

/**
 * The worker's warning, shown before the method can be saved or turned on
 * and kept on the page while it is on. Used until the worker's own copy loads.
 */
export const CLOUD_WARNING =
	'Agents on this worker can run shell commands and can read these credentials, including the Vertex AI key file on this worker\'s disk, with their own tools and in terminals. A project\'s or your own .claude/settings.json can still change where Claude connects, and this worker\'s proxy and certificate settings still apply. Use an account with the smallest permissions that work, and only on a worker you trust.';
export const GATEWAY_WARNING = 'All Claude traffic from this worker goes to the address you enter. The browser tool also receives this address and token.';
