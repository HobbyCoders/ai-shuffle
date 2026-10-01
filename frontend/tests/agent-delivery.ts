import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';
// Real prompts from app/core/turn_scheduler.py (tests/core/test_agent_delivery_frontend_contract.py).
import producer from '../src/lib/utils/__fixtures__/agent-delivery-producer.json';

// No request leaves this fixture: the transcript is the real reducer's output
// for the real producer prompts, rendered by the production MessageArea.
window.fetch = async () => new Response('{}', { status: 404, headers: { 'Content-Type': 'application/json' } });
class LocalSocket {
	static OPEN = 1; static CLOSED = 3; readyState = 3;
	send() {} close() {} addEventListener() {} removeEventListener() {}
}
window.WebSocket = LocalSocket as unknown as typeof WebSocket;
window.EventSource = LocalSocket as unknown as typeof EventSource;

const [{ activeSession }, { reduceAll }, { default: MessageArea }] = await Promise.all([
	import('../src/lib/stores/active-session'),
	import('../src/lib/stores/session/v2/reducer'),
	import('../src/lib/components/chat/MessageArea.svelte'),
]);

let seq = 0;
const event = (type: string, fields: Record<string, unknown>) => ({
	type, id: `evt-${++seq}`, seq, session_id: 'fixture-session',
	created_at: new Date(Date.UTC(2026, 9, 1, 12, 0, seq)).toISOString(), ...fields,
});
const turn = (id: string, source: string, prompt: string, reply: string) => [
	event('turn.started', { turn_id: id, source }),
	event('message.user', { turn_id: id, content: prompt }),
	event('message.assistant', { turn_id: id, kind: 'text', content: reply }),
	event('turn.completed', { turn_id: id }),
];
const state = reduceAll([
	...turn('t1', 'user', 'Split the investigation across two workers and report back.', 'Started two background workers.'),
	...turn('t2', 'promotion', producer.prompts.promotion, 'Read both worker notes.'),
	...turn('t3', 'unpark', producer.prompts.unpark_with_held, 'worker-a finished; continuing.'),
	...turn('t4', 'agent_message', producer.prompts.urgent_with_held, 'Yes, edit control.py.'),
	...turn('t5', 'agent_message', producer.prompts.idle_single, 'Thanks.'),
] as never);

activeSession.current.sessionId = 'fixture-session';
activeSession.current.messages = state.messages;
activeSession.current.isLoadingSession = false;
// Any store update republishes the state object the component reads.
activeSession.setDraft('');
(window as unknown as { fixture: unknown }).fixture = { messages: state.messages.length };
mount(MessageArea, { target: document.getElementById('app')! });
