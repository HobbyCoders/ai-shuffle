// Production composer + STT services; only transport and capture are mocked.
export function installSttFixture() {
 const capability = { supported: true, model: 'openai/gpt-live-transcribe', model_name: 'GPT Live Transcribe', partials: true, sample_rate: 24000, realtime_only: true };
 let scenario = 'success';
 let stoppedTracks = 0;
 let releaseMic: (() => void) | null = null;
 const requests: string[] = [];
 const originalFetch = window.fetch;
 window.fetch = async (input, init) => {
  const url = String(input); requests.push(url);
  if (url.includes('/transcribe/stream/capability')) return Response.json(capability);
  return originalFetch(input, init);
 };
 class Socket {
  static OPEN = 1; static last: Socket;
  readyState = 1; bufferedAmount = 0;
  onmessage?: (event: { data: string }) => void;
  constructor(url: string) {
   if (!url.includes('/transcribe/stream')) return;
   Socket.last = this;
   queueMicrotask(() => {
    if (scenario === 'reject') {
     this.emit({ type: 'error', fatal: true, message: 'GPT Live Transcribe is unavailable for this account.' });
     this.emit({ type: 'closed' });
    } else this.emit({ type: 'ready', ...capability });
   });
  }
  emit(message: unknown) { this.onmessage?.({ data: JSON.stringify(message) }); }
  send(raw: unknown) {
   if (typeof raw !== 'string' || JSON.parse(raw).type !== 'stop') return;
   this.emit({ type: 'final', item_id: 'a', text: 'Hello world.' });
   this.emit({ type: 'closed' });
  }
  close() { this.readyState = 3; }
  addEventListener() {} removeEventListener() {}
 }
 const node = () => ({ connect() {}, disconnect() {}, gain: { value: 0 }, frequencyBinCount: 128, getByteFrequencyData() {} });
 class Audio {
  state = 'running'; sampleRate = 24000; destination = {};
  createMediaStreamSource = node; createAnalyser = node; createScriptProcessor = node; createGain = node;
  async close() {} async resume() {}
 }
 Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
  getUserMedia: () => new Promise(resolve => {
   const release = () => resolve({ getTracks: () => [{ stop() { stoppedTracks++; } }] });
   if (scenario === 'reject') releaseMic = release;
   else release();
  })
 } });
 window.WebSocket = Socket as unknown as typeof WebSocket;
 window.AudioContext = Audio as unknown as typeof AudioContext;
 Object.assign(window, { sttFixture: {
  requests,
  scenario(value: string) { scenario = value; },
  emit(message: unknown) { Socket.last.emit(message); },
  releaseMic() { releaseMic?.(); releaseMic = null; },
  get stoppedTracks() { return stoppedTracks; }
 } });
}
