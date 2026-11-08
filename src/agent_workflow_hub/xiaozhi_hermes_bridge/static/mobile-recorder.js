/* Browser capture only: mono signed PCM16 at 16 kHz, no external codecs/models. */
class LingxiRecorder extends AudioWorkletProcessor {
  constructor() {
    super(); this.buffer = []; this.sum = 0; this.count = 0; this.position = 0; this.finished = false;
    this.port.onmessage = ({data}) => { if (data === 'finish') { this.flush(); this.finished = true; this.port.postMessage('finished'); } };
  }
  flush() {
    if (!this.buffer.length) return;
    const bytes = new ArrayBuffer(this.buffer.length * 2), view = new DataView(bytes);
    this.buffer.forEach((x, i) => view.setInt16(i * 2, Math.max(-32768, Math.min(32767, Math.round(x * 32767))), true));
    this.port.postMessage(bytes, [bytes]); this.buffer = [];
  }
  process(inputs) {
    if (this.finished) return false;
    const values = inputs[0]?.[0];
    if (values) for (const x of values) {
      this.sum += x; this.count++; this.position += 16000;
      if (this.position >= sampleRate) {
        this.position -= sampleRate; this.buffer.push(this.sum / this.count); this.sum = 0; this.count = 0;
        if (this.buffer.length >= 640) this.flush();
      }
    }
    return true;
  }
}
registerProcessor('lingxi-recorder', LingxiRecorder);
