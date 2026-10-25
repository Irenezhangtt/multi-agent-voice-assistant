// Offline check of actual browser capture code, with a simulated AudioWorklet host.
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
for(const rate of [16000,44100,48000]) {
  let Processor;const messages=[];
  const scope={sampleRate:rate,AudioWorkletProcessor:class{constructor(){this.port={postMessage:m=>messages.push(m)};}},registerProcessor:(name,cls)=>{Processor=cls;}};
  vm.runInNewContext(fs.readFileSync('voice_agent/static/pcm-worklet.js','utf8'),scope);
  const instance=new Processor();
  for(let offset=0;offset<rate;offset+=128) {
    const input=new Float32Array(Math.min(128,rate-offset));input.fill(.25);instance.process([[input]]);
  }
  const frames=messages.filter(m=>m.type==='pcm');
  assert.equal(frames.length,50,`${rate}: expected 1s of PCM16 in 50 x 20ms frames`);
  assert.equal(frames[0].buffer.byteLength,640);
  assert.equal(new DataView(frames[0].buffer).getInt16(0,true),8192);
  assert(messages.some(m=>m.type==='speech_started'));
}
console.log('AudioWorklet resampling passed: 16kHz / 44.1kHz / 48kHz → mono PCM16 16kHz');
