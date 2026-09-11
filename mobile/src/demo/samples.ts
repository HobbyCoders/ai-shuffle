/** Local sample data only. No transport, credentials, storage, or provider client. */
export interface DemoMessage { role: 'user' | 'assistant'; content: string }
export const DEMO_REPLY = 'Preset demo reply — no AI model was called. Your message stayed in this demonstration. In a connected session, the worker you choose would process your request.';
export const samples = [
  { title: 'Welcome to AI-Shuffle', messages: [
    { role: 'user', content: 'What does this phone app do?' },
    { role: 'assistant', content: 'AI-Shuffle is a **remote client** for workers you connect. This is an **offline sample**, not a connected worker.\n\nYou can browse these sample conversations and try composing a message. Replies here are fixed text, not AI output.' }
  ] },
  { title: 'Example project notes', messages: [
    { role: 'user', content: 'Show the example checklist.' },
    { role: 'assistant', content: '## Sample checklist\n\n- Connect a worker you own.\n- Open a conversation.\n- Choose which optional phone tools to enable.\n\nThese are bundled example notes, not files from your account. Live AI, voice calls, remote commands, and worker files are not available in this offline demonstration.' }
  ] }
] satisfies { title: string; messages: DemoMessage[] }[];
export function freshMessages(index: number): DemoMessage[] {
  return samples[index].messages.map(message => ({ ...message }));
}
export function sendDemoMessage(messages: DemoMessage[], draft: string): DemoMessage[] {
  const content = draft.trim().slice(0, 2000);
  if (!content || messages.length >= 42) return messages;
  return [...messages, { role: 'user', content }, { role: 'assistant', content: DEMO_REPLY }];
}
