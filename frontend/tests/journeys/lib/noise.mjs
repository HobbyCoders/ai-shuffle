// Anomalies every journey tolerates because the dev instance itself causes
// them, never the UI under test. Each has a reason a reviewer can check.
// A journey adds its own rules after these (for a known app bug outside its
// subject, labelled `known bug #NNN`). Never allow `reversal` or `remount`.

export const DEV_INSTANCE_NOISE = [
	{
		kind: 'failed_requests',
		status: 403,
		url: /\/preferences(\/|$)/,
		reason: 'the preview cookie may not write preferences (devloop safety rule)'
	},
	{
		kind: 'failed_requests',
		status: 403,
		url: /\/model-providers\/refresh-capabilities(\?|$)/,
		reason: 'the preview cookie may not change providers (devloop safety rule)'
	},
	{
		kind: 'failed_requests',
		status: 403,
		url: /\/terminals\/targets(\?|$)/,
		reason: 'the preview cookie may not use terminals (require_terminal_owner refuses preview access by design)'
	},
	{
		kind: 'failed_requests',
		status: 502,
		url: /\/git\/fetch(\?|$)/,
		reason: 'background fetch: the seeded repository has no remote to fetch from'
	},
	{
		kind: 'failed_requests',
		status: 401,
		url: /\/github\/pulls(\?|$)/,
		reason: 'the dev instance has no GitHub sign-in'
	}
];
