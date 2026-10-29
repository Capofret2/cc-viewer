// 语音输入的录音处理器（js/voice.js 加载，跑在音频线程里）：把麦克风的原生采样率降到讯飞要的 16kHz，
// 转成 16 位整数，每攒够 40ms（640 个采样）交给页面一帧，顺带这一帧的音量（均方根）给按钮上的波形。
// 降采样把落进同一个输出采样的输入取平均（比隔几个取一个抗混叠好些）；48k、44.1k 都能用。
// 页面发 'flush' 时把不满一帧的尾巴也交出去，免得最后一个字被截掉
const RATE = 16000, FRAME = 640;

class Pcm16k extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / RATE;
    this.pos = 0; this.acc = 0; this.n = 0;
    this.out = new Int16Array(FRAME); this.k = 0; this.sq = 0;
    this.port.onmessage = e => { if (e.data === 'flush') this.emit(); };
  }
  emit() {
    if (!this.k) return;
    const pcm = this.out.slice(0, this.k);
    this.port.postMessage({ pcm: pcm.buffer, level: Math.sqrt(this.sq / this.k) }, [pcm.buffer]);
    this.k = 0; this.sq = 0;
  }
  process(inputs) {
    const ch = inputs[0]?.[0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.acc += ch[i]; this.n++; this.pos += 1;
      if (this.pos < this.ratio) continue;
      this.pos -= this.ratio;
      const s = Math.max(-1, Math.min(1, this.acc / this.n));
      this.acc = 0; this.n = 0;
      this.out[this.k++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      this.sq += s * s;
      if (this.k === FRAME) this.emit();
    }
    return true;
  }
}
registerProcessor('pcm16k', Pcm16k);
