const $=id=>document.getElementById(id);
let health, socket, context, stream, inputNode, source, mute, generation=0, nextAt=0, sources=new Set(), connected=false, starting=false, clockTimer, startedAt, sampleRemainder=null, awaitingInterrupt=false, completedTraceId=null;
async function request(path,options={}){const r=await fetch(path,options);const b=await r.json();if(!r.ok)throw Error(typeof b.detail==='string'?b.detail:'请求失败');return b;}
function status(text){$('status').textContent=text;}
function clearAudio(){for(const s of sources){try{s.stop();}catch{}}sources.clear();nextAt=0;sampleRemainder=null;}
function interrupt(){clearAudio();awaitingInterrupt=true;if(socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify({type:'interrupt'}));status('正在聆听…');}
function playback(data){
  if(data.generation!==generation||!context||awaitingInterrupt)return;
  let bytes=Uint8Array.from(atob(data.data),c=>c.charCodeAt(0));
  if(sampleRemainder!==null){const joined=new Uint8Array(bytes.length+1);joined[0]=sampleRemainder;joined.set(bytes,1);bytes=joined;sampleRemainder=null;}
  if(bytes.length%2){sampleRemainder=bytes[bytes.length-1];bytes=bytes.slice(0,-1);}
  if(!bytes.length)return;
  if(nextAt-context.currentTime>60){interrupt();$('error').textContent='播放队列过长，已停止回复。';return;}
  const view=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength), buffer=context.createBuffer(1,bytes.length/2,16000), pcm=buffer.getChannelData(0);
  for(let i=0;i<pcm.length;i++)pcm[i]=view.getInt16(i*2,true)/32768;
  const player=context.createBufferSource();player.buffer=buffer;player.connect(context.destination);
  nextAt=Math.max(context.currentTime+.03,nextAt);sources.add(player);
  player.onended=()=>{sources.delete(player);player.disconnect();if(!sources.size&&connected)status('正在聆听…');};
  player.start(nextAt);nextAt+=buffer.duration;status('Agent 正在回复…');
}
async function stop(){
  connected=false;starting=false;clearAudio();clearInterval(clockTimer);$('orb').classList.remove('live');
  if(socket){const previous=socket;socket=null;previous.onclose=null;previous.onmessage=null;if(previous.readyState===WebSocket.OPEN)previous.send(JSON.stringify({type:'stop'}));previous.close();}
  if(inputNode){inputNode.port.onmessage=null;inputNode.disconnect();inputNode=null;}
  source?.disconnect();source=null;mute?.disconnect();mute=null;
  stream?.getTracks().forEach(t=>t.stop());stream=null;
  if(context){await context.close().catch(()=>{});context=null;}
  $('start').disabled=!health?.speech.configured;$('stop').disabled=true;$('interrupt').disabled=true;status('通话已暂停，可继续上次通话');
}
async function check(){
  try{
    health=await request('/health');const speech=health.speech;
    $('engine').textContent=`Deepgram STT/TTS · ${speech.language} · ${health.mode==='claude'?(health.architecture==='multi-agent'?'Claude · Multi-agent':'Claude · Workflow'):'Mock 意图识别'}`;
    $('configuration').textContent=speech.configured?'语音凭据已配置，尚未验证云端连接。点击“开始语音”并允许麦克风。'+(health.mode==='mock'?'当前意图识别为 Mock，请在 .env 设置 AGENT_MODE=claude 以使用 Claude。':''):'语音尚未启用：请在本机 .env 配置 DEEPGRAM_API_KEY，然后重启服务。';
    $('start').disabled=!speech.configured||connected||starting;
  }catch(e){$('error').textContent=e.message;}
}
$('start').onclick=async()=>{
  if(starting||connected)return;starting=true;awaitingInterrupt=false;$('start').disabled=true;$('stop').disabled=false;$('error').textContent='';status('正在连接语音服务…');
  try{
    if(!navigator.mediaDevices?.getUserMedia||!window.AudioWorkletNode)throw Error('需要支持 AudioWorklet 的浏览器，并在 localhost 或 HTTPS 中打开。');
    context=new AudioContext();await context.resume();
    stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
    if(!starting){await stop();return;}
    await context.audioWorklet.addModule('/assets/pcm-worklet.js');
    let call;const savedId=localStorage.getItem('voice-call-id');
    if(savedId){try{call=await request('/api/calls/'+encodeURIComponent(savedId));}catch{localStorage.removeItem('voice-call-id');}}
    call=call||await request('/api/calls',{method:'POST'});const callId=call.state.call_id;localStorage.setItem('voice-call-id',callId);$('call-id').textContent='CALL / '+callId.slice(0,8);
    $('trace').textContent=JSON.stringify({call_id:callId,mode:health.mode},null,2);$('transcript').textContent='请直接说话。';$('reply').textContent=call.state.reply||'等待你的需求。';
    const protocol=location.protocol==='https:'?'wss:':'ws:';socket=new WebSocket(`${protocol}//${location.host}/api/calls/${callId}/voice`);
    socket.onmessage=event=>{
      const data=JSON.parse(event.data);
      if(data.type==='ready'){
        connected=true;starting=false;generation=0;startedAt=Date.now();$('orb').classList.add('live');$('stop').disabled=false;$('interrupt').disabled=false;
        source=context.createMediaStreamSource(stream);inputNode=new AudioWorkletNode(context,'pcm-input');mute=context.createGain();mute.gain.value=0;source.connect(inputNode).connect(mute).connect(context.destination);
        inputNode.port.onmessage=event=>{if(!connected)return;const msg=event.data;if(msg.type==='pcm'&&socket?.readyState===WebSocket.OPEN){if(socket.bufferedAmount>128000){$('error').textContent='音频网络拥塞，已结束通话。';void stop();return;}socket.send(msg.buffer);}else if(msg.type==='speech_started')interrupt();};
        clockTimer=setInterval(()=>{const seconds=Math.floor((Date.now()-startedAt)/1000);$('timer').textContent=String(Math.floor(seconds/60)).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0');},1000);status('正在聆听…');
      }else if(data.type==='transcript'){$('transcript').textContent=data.text+(data.final?'':' …');if(data.final){$('reply').textContent='';completedTraceId=null;}}
      else if(data.type==='interrupt'){generation=data.generation;awaitingInterrupt=false;clearAudio();status('正在聆听…');}
      else if(data.type==='reply_chunk'&&data.generation===generation&&!awaitingInterrupt&&data.trace_id!==completedTraceId){$('reply').textContent+=($('reply').textContent?'\n':'')+data.text;}
      else if(data.type==='turn_result'){completedTraceId=data.response.trace_id;$('reply').textContent=data.response.state.reply;$('trace').textContent=JSON.stringify(data.response,null,2);}
      else if(data.type==='audio_start'){generation=data.generation;sampleRemainder=null;}
      else if(data.type==='audio')playback(data);
      else if(data.type==='status'&&data.status==='resuming')status('正在恢复上次任务…');
      else if(data.type==='status'&&data.status==='thinking')status('正在处理你的需求…');
      else if(data.type==='status'&&!sources.size)status('正在聆听…');
      else if(data.type==='error'){$('error').textContent=data.message;if(data.code==='tts_error'){clearAudio();status('语音合成失败，业务结果见下方');}}
    };
    socket.onerror=()=>{$('error').textContent='语音连接失败，请检查配置和服务日志。';};
    socket.onclose=()=>{void stop();};
  }catch(e){$('error').textContent=e.name==='NotAllowedError'?'麦克风权限未获允许，请在浏览器中开启后重试。':e.message;await stop();}
};
$('new-call').onclick=async()=>{await stop();localStorage.removeItem('voice-call-id');$('call-id').textContent='';$('reply').textContent='等待新的通话。';$('transcript').textContent='';status('点击开始语音，建立新通话');};
$('stop').onclick=()=>void stop();$('interrupt').onclick=interrupt;$('refresh').onclick=check;
window.addEventListener('pagehide',()=>{stream?.getTracks().forEach(t=>t.stop());socket?.close();});
check();
