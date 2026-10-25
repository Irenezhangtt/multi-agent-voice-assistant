// Capture mono audio, average-resample to 16kHz, send little-endian PCM16 in 20ms frames.
class PCMInput extends AudioWorkletProcessor {
  constructor() { super(); this.phase=0; this.sum=0; this.count=0; this.frame=[]; this.energy=0; this.frames=0; this.wasSpeaking=false; }
  process(inputs) {
    const input=inputs[0]?.[0]; if(!input) return true;
    let energy=0;
    for(const value of input) {
      energy+=value*value; this.sum+=value; this.count++; this.phase+=16000;
      if(this.phase>=sampleRate) {
        this.phase-=sampleRate;
        const v=Math.max(-1,Math.min(1,this.sum/this.count));
        this.frame.push(Math.round(v*(v<0?32768:32767))); this.sum=0; this.count=0;
        if(this.frame.length===320) {
          const buffer=new ArrayBuffer(640), view=new DataView(buffer);
          for(let i=0;i<320;i++) view.setInt16(i*2,this.frame[i],true);
          this.port.postMessage({type:'pcm',buffer},[buffer]); this.frame=[];
        }
      }
    }
    if(Math.sqrt(energy/input.length)>.02) this.frames++; else this.frames=0;
    const speaking=this.frames>Math.ceil(sampleRate*.08/input.length);
    if(speaking&&!this.wasSpeaking) this.port.postMessage({type:'speech_started'});
    this.wasSpeaking=speaking;
    return true;
  }
}
registerProcessor('pcm-input',PCMInput);
