import { expect, test } from 'claude-code/testing'
import { defangMarkers, defangToolMessage } from '../hooks/tool_output.ts'

// The pytest runner fills this corpus with the native helper's exact answers.
const nativeCases: [string, string][] = /* NATIVE_CASES */ []

test('marker normalization stays identical to the native boundary', () => {
  for (const [input, expected] of nativeCases) {
    expect(defangMarkers(input)).toBe(expected)
    expect(defangMarkers(expected)).toBe(expected)
  }
})

test('only tool-result content changes; role, metadata and media stay put', () => {
  const raw = nativeCases[1][0]
  const expected = nativeCases[1][1]
  const image = { type: 'image', source: { type: 'base64', data: 'AAECAwQ=', media_type: 'image/png' } }
  const message = { role: 'user', content: [
    { type: 'text', text: raw },
    { type: 'tool_result', tool_use_id: 'toolu_1', is_error: true, content: [
      { type: 'text', text: raw }, image, { type: 'resource', resource: { text: raw } },
    ] },
  ] }
  expect(defangToolMessage(message)).toEqual({ ...message, content: [
    message.content[0],
    { ...message.content[1], content: [
      { type: 'text', text: expected }, image, { type: 'resource', resource: { text: expected } },
    ] },
  ] })
  expect(message.content[0].text).toBe(raw)
  expect(message.content[1].content[0].text).toBe(raw)
  expect(defangToolMessage({ content: [null, 7, { type: 'tool_result' }] })).toEqual(
    { content: [null, 7, { type: 'tool_result' }] })
})
