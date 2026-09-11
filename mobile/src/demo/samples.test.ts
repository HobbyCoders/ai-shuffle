import { describe, it, expect } from 'vitest';
import { samples, freshMessages, sendDemoMessage, DEMO_REPLY } from './samples';

describe('offline sample conversations', () => {
  it('creates independent resettable data, not persistent sessions', () => {
    const messages = freshMessages(0);
    messages[0].content = 'changed';
    expect(freshMessages(0)[0].content).toBe(samples[0].messages[0].content);
  });
  it('uses an explicit preset reply without reflecting user input as AI output', () => {
    const messages = sendDemoMessage(freshMessages(0), '  test message  ');
    expect(messages.at(-2)).toEqual({ role: 'user', content: 'test message' });
    expect(messages.at(-1)).toEqual({ role: 'assistant', content: DEMO_REPLY });
    expect(DEMO_REPLY).toContain('no AI model was called');
  });
  it('ignores empty input and bounds the local transcript', () => {
    const messages = freshMessages(0);
    expect(sendDemoMessage(messages, '  ')).toBe(messages);
    let limited = messages;
    for (let n = 0; n < 100; n++) limited = sendDemoMessage(limited, 'x');
    expect(limited).toHaveLength(42);
    expect(sendDemoMessage(messages, 'x'.repeat(3000)).at(-2)?.content).toHaveLength(2000);
  });
});
