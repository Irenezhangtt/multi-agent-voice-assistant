const $=id=>document.getElementById(id);
let health, socket, context, stream, inputNode, source, mute, generation=0, nextAt=0, sources=new Set(), connected=false, starting=false, clockTimer, startedAt, sampleRemainder=null, awaitingInterrupt=false, completedTraceId=null;
async function request(path,options={}){const r=await fetch(path,options);const b=await r.json();if(!r.ok)throw Error(typeof b.detail==='string'&&!/[\u3400-\u9fff]/.test(b.detail)?b.detail:`Request failed (HTTP ${r.status}). Start a new call or check the service log.`);return b;}
function voiceError(data){const messages={speech_not_configured:'Voice is not configured. Add a Deepgram API key and restart the service.',speech_init_failed:'Could not initialize voice. Check your configuration and service logs.',already_connected:'This call is already open in another voice session.',voice_error:'The voice connection ended unexpectedly. Reconnect or check the service logs.',turn_error:'The request could not be completed. Check its saved state before retrying.',tts_error:'Speech synthesis failed. Your verified business result is still available.'};return messages[data.code]||(/[\u3400-\u9fff]/.test(data.message||'')?'Voice request failed. Check the service logs.':data.message)||'Voice request failed.';}
function status(text){$('status').textContent=text;}
function clearAudio(){for(const s of sources){try{s.stop();}catch{}}sources.clear();nextAt=0;sampleRemainder=null;}
function interrupt(){clearAudio();awaitingInterrupt=true;if(socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify({type:'interrupt'}));status('Listening…');}
function playback(data){
  if(data.generation!==generation||!context||awaitingInterrupt)return;
  let bytes=Uint8Array.from(atob(data.data),c=>c.charCodeAt(0));
  if(sampleRemainder!==null){const joined=new Uint8Array(bytes.length+1);joined[0]=sampleRemainder;joined.set(bytes,1);bytes=joined;sampleRemainder=null;}
  if(bytes.length%2){sampleRemainder=bytes[bytes.length-1];bytes=bytes.slice(0,-1);}
  if(!bytes.length)return;
  if(nextAt-context.currentTime>60){interrupt();$('error').textContent='Playback stopped because the audio queue was too long.';return;}
  const view=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength), buffer=context.createBuffer(1,bytes.length/2,16000), pcm=buffer.getChannelData(0);
  for(let i=0;i<pcm.length;i++)pcm[i]=view.getInt16(i*2,true)/32768;
  const player=context.createBufferSource();player.buffer=buffer;player.connect(context.destination);
  nextAt=Math.max(context.currentTime+.03,nextAt);sources.add(player);
  player.onended=()=>{sources.delete(player);player.disconnect();if(!sources.size&&connected)status('Listening…');};
  player.start(nextAt);nextAt+=buffer.duration;status('Assistant is speaking…');
}
async function stop(){
  connected=false;starting=false;clearAudio();clearInterval(clockTimer);$('orb').classList.remove('live');
  if(socket){const previous=socket;socket=null;previous.onclose=null;previous.onmessage=null;if(previous.readyState===WebSocket.OPEN)previous.send(JSON.stringify({type:'stop'}));previous.close();}
  if(inputNode){inputNode.port.onmessage=null;inputNode.disconnect();inputNode=null;}
  source?.disconnect();source=null;mute?.disconnect();mute=null;
  stream?.getTracks().forEach(t=>t.stop());stream=null;
  if(context){await context.close().catch(()=>{});context=null;}
  $('start').disabled=!health?.speech.configured;$('stop').disabled=true;$('interrupt').disabled=true;status('Call paused. Resume whenever you are ready.');
}
async function check(){
  try{
    health=await request('/health');const speech=health.speech;
    $('engine').textContent=`Deepgram STT/TTS · ${speech.language} · ${health.mode==='claude'?(health.architecture==='multi-agent'?'Claude · Multi-agent':'Claude · Workflow'):'Demo mode'}`;
    $('configuration').textContent=speech.configured?'Voice is configured. Start a call and allow microphone access to connect.'+(health.mode==='mock'?' Demo mode uses deterministic responses.':''):'Voice is not configured. Add DEEPGRAM_API_KEY to your local .env and restart the service.';
    $('start').disabled=!speech.configured||connected||starting;
  }catch(e){$('error').textContent=e.message;}
}
$('start').onclick=async()=>{
  if(starting||connected)return;starting=true;awaitingInterrupt=false;$('start').disabled=true;$('stop').disabled=false;$('error').textContent='';status('Connecting to voice…');
  try{
    if(!navigator.mediaDevices?.getUserMedia||!window.AudioWorkletNode)throw Error('Use a browser that supports AudioWorklet, on localhost or HTTPS.');
    context=new AudioContext();await context.resume();
    stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
    if(!starting){await stop();return;}
    await context.audioWorklet.addModule('/assets/pcm-worklet.js');
    let call;const savedId=localStorage.getItem('voice-call-id');
    if(savedId){try{call=await request('/api/calls/'+encodeURIComponent(savedId));}catch{localStorage.removeItem('voice-call-id');}}
    call=call||await request('/api/calls',{method:'POST'});const callId=call.state.call_id;localStorage.setItem('voice-call-id',callId);$('call-id').textContent='CALL / '+callId.slice(0,8);
    $('trace').textContent=JSON.stringify({call_id:callId,mode:health.mode},null,2);$('transcript').textContent='Go ahead. We are listening.';$('reply').textContent=call.state.reply||'Ready to help.';
    const protocol=location.protocol==='https:'?'wss:':'ws:';socket=new WebSocket(`${protocol}//${location.host}/api/calls/${callId}/voice`);
    socket.onmessage=event=>{
      const data=JSON.parse(event.data);
      if(data.type==='ready'){
        connected=true;starting=false;generation=0;startedAt=Date.now();$('orb').classList.add('live');$('stop').disabled=false;$('interrupt').disabled=false;
        source=context.createMediaStreamSource(stream);inputNode=new AudioWorkletNode(context,'pcm-input');mute=context.createGain();mute.gain.value=0;source.connect(inputNode).connect(mute).connect(context.destination);
        inputNode.port.onmessage=event=>{if(!connected)return;const msg=event.data;if(msg.type==='pcm'&&socket?.readyState===WebSocket.OPEN){if(socket.bufferedAmount>128000){$('error').textContent='The audio connection is congested. The call has been paused.';void stop();return;}socket.send(msg.buffer);}else if(msg.type==='speech_started')interrupt();};
        clockTimer=setInterval(()=>{const seconds=Math.floor((Date.now()-startedAt)/1000);$('timer').textContent=String(Math.floor(seconds/60)).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0');},1000);status('Listening…');
      }else if(data.type==='transcript'){$('transcript').textContent=data.text+(data.final?'':' …');if(data.final){$('reply').textContent='';completedTraceId=null;}}
      else if(data.type==='interrupt'){generation=data.generation;awaitingInterrupt=false;clearAudio();status('Listening…');}
      else if(data.type==='reply_chunk'&&data.generation===generation&&!awaitingInterrupt&&data.trace_id!==completedTraceId){$('reply').textContent+=($('reply').textContent?'\n':'')+data.text;}
      else if(data.type==='turn_result'){completedTraceId=data.response.trace_id;$('reply').textContent=data.response.state.reply;$('trace').textContent=JSON.stringify(data.response,null,2);}
      else if(data.type==='audio_start'){generation=data.generation;sampleRemainder=null;}
      else if(data.type==='audio')playback(data);
      else if(data.type==='status'&&data.status==='resuming')status('Resuming your previous task…');
      else if(data.type==='status'&&data.status==='thinking')status('Working on your request…');
      else if(data.type==='status'&&!sources.size)status('Listening…');
      else if(data.type==='error'){$('error').textContent=voiceError(data);if(data.code==='tts_error'){clearAudio();status('Audio unavailable. See the verified result below.');}}
    };
    socket.onerror=()=>{$('error').textContent='Could not connect to voice. Check the configuration and service logs.';};
    socket.onclose=()=>{void stop();};
  }catch(e){$('error').textContent=e.name==='NotAllowedError'?'Microphone access was denied. Enable it in your browser and try again.':e.message;await stop();}
};
$('new-call').onclick=async()=>{await stop();localStorage.removeItem('voice-call-id');$('call-id').textContent='';$('reply').textContent='Ready for a new call.';$('transcript').textContent='';status('Select Start / resume voice to begin a new call.');};
$('stop').onclick=()=>void stop();$('interrupt').onclick=interrupt;$('refresh').onclick=check;
window.addEventListener('pagehide',()=>{stream?.getTracks().forEach(t=>t.stop());socket?.close();});
check();
