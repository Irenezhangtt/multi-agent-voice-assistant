"""One real Claude/MCP/Deepgram TTS probe; text input, not a microphone benchmark.

Can load a frozen prior source tree in a separate process for an honest baseline.
"""
import argparse
import asyncio
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
import inspect
import json
from pathlib import Path
import sys
from time import perf_counter


ROOT=Path(__file__).resolve().parents[1]
UTTERANCE='Please prepare a cleaning appointment for Alex, phone 2025550199, November 3, 2026 at 2 PM. Independently check fictional Aetna insurance, member MISSING999, for cleaning. Do not create the appointment until I confirm after your review.'


async def probe(args):
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    source=args.source_root.resolve()
    sys.path.insert(0,str(source))
    from langsmith import Client, trace, tracing_context
    from voice_agent.config import Settings
    from voice_agent.runtime import AgentRuntime
    from voice_agent.speech import DeepgramSpeech
    from evals.langsmith_cloud import verify_run, trace_tree
    settings=replace(Settings.from_env(),data_dir=args.output.parent/(args.output.stem+'-state'))
    if settings.data_dir.exists() or args.output.exists():
        raise ValueError('Use a fresh probe output and state directory')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    runtime=AgentRuntime(settings,clock=lambda:datetime.fromisoformat('2026-10-24T09:00:00-07:00'))
    speech=DeepgramSpeech(settings)
    client=Client(timeout_ms=15000)
    queue=asyncio.Queue()
    loop=asyncio.get_running_loop()
    times={}
    published=[]
    start=perf_counter()
    def accept(text):
        times.setdefault('first_verified_reply_seconds',perf_counter()-start)
        published.append(text)
        queue.put_nowait(text)
    async def texts():
        while True:
            text=await queue.get()
            if text is None:return
            yield text
    async def audio():
        if hasattr(speech,'synthesize_stream'):
            async for chunk in speech.synthesize_stream(texts()):yield chunk
        else:
            async for text in texts():
                async for chunk in speech.synthesize(text):yield chunk
    supports_callback='on_reply' in inspect.signature(runtime.execute).parameters
    async def business():
        kwargs={'on_reply':lambda text:loop.call_soon_threadsafe(accept,text)} if supports_callback else {}
        response=await asyncio.to_thread(runtime.execute,row,**kwargs)
        times['business_complete_seconds']=perf_counter()-start
        remaining=response['state']['reply']
        for text in published:remaining=remaining.replace(text,'',1)
        if remaining.strip():accept(remaining.strip())
        queue.put_nowait(None)
        return response
    async def consume():
        size=0
        async for chunk in audio():
            times.setdefault('first_tts_audio_seconds',perf_counter()-start)
            size+=len(chunk)
        times['tts_complete_seconds']=perf_counter()-start
        return size
    result={'label':args.output.stem,'source_root':str(source),'input':UTTERANCE,
            'scope':'host latency probe: real Claude + MCP + temporary SQL + hosted Deepgram TTS; text input excludes STT and client playback',
            'versions':settings.versions(),'supports_early_verified_reply':supports_callback,
            'source_sha256':{name:sha256((source/'voice_agent'/name).read_bytes()).hexdigest() for name in ('multi_agent.py','voice.py','speech.py')}}
    tasks=[]
    try:
        call_id=runtime.create_call()['state']['call_id']
        row=runtime.submit(call_id,UTTERANCE)
        with tracing_context(enabled=True,client=client,metadata={'eval_suite':'voice_latency_probe','label':args.output.stem}):
            with trace('latency_pipeline_probe',client=client,inputs={'utterance':UTTERANCE},metadata={'label':args.output.stem}) as root:
                tasks=[asyncio.create_task(business()),asyncio.create_task(consume())]
                response,size=await asyncio.wait_for(asyncio.gather(*tasks),120)
                with runtime.store.connect() as db:
                    model_calls=db.execute("SELECT count(*) FROM execution_steps WHERE step_key LIKE '%:model:%'").fetchone()[0]
                result.update(times,pcm_bytes=size,model_calls=model_calls,reply=response['state']['reply'],status=response['state']['status'],
                              parallel_dispatch=any(e['type']=='parallel_dispatch' for e in response['state']['events']),
                              outcomes=runtime.store.outcomes(call_id),source_trace_id=response['trace_id'])
                root.end(outputs=result)
        client.flush(timeout=30)
        run,proof=verify_run(client,root.id)
        result['cloud']={'root':proof,'tree':trace_tree(client,run)}
    except Exception as error:
        result.update(error_type=type(error).__name__)
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
    finally:
        runtime.close()
        await speech.close()
        args.output.write_text(json.dumps(result,indent=2,default=str)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k in {'label','error_type','first_verified_reply_seconds','first_tts_audio_seconds','business_complete_seconds','model_calls','parallel_dispatch'}}))
    return 1 if result.get('error_type') else 0


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root',type=Path,default=ROOT)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    raise SystemExit(asyncio.run(probe(args)))


if __name__=='__main__':main()
