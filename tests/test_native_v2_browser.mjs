// Real Chromium check. Requires the isolated preview on :8768 and CDP on :9228.
import assert from 'node:assert/strict';

const pages=await (await fetch('http://127.0.0.1:9228/json')).json();
const ws=new WebSocket(pages.find(page=>page.type==='page').webSocketDebuggerUrl);
await new Promise(resolve=>ws.addEventListener('open',resolve,{once:true}));
let sequence=0;const pending=new Map();
ws.addEventListener('message',event=>{const message=JSON.parse(event.data);if(!pending.has(message.id))return;const call=pending.get(message.id);pending.delete(message.id);message.error?call.reject(Error(JSON.stringify(message.error))):call.resolve(message.result);});
const call=(method,params={})=>new Promise((resolve,reject)=>{const id=++sequence;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
const evaluate=async expression=>{const result=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));return result.result.value;};
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));

await call('Page.enable');
await call('Emulation.setDeviceMetricsOverride',{width:1366,height:1024,deviceScaleFactor:1,mobile:false});
await call('Page.navigate',{url:'http://127.0.0.1:8768'});
for(let n=0;n<40&&!(await evaluate("document.getElementById('connection')?.textContent==='Connected'"));++n)await sleep(100);
assert.equal(await evaluate('ready&&!audioReady'),true);
await evaluate("view('edit');soundSection('osc')");

// A local knob event must retarget the plot synchronously, then animate rather
// than teleport. The HTTP poll is intentionally not involved in this check.
const animation=await evaluate(`(()=>{
  const input=fields.attack1.input, before=[...envelopeCurrent[1]];
  input.value=Math.min(1000,Number(input.value)+400);
  input.dispatchEvent(new Event('input'));
  return {before,current:[...envelopeCurrent[1]],target:[...envelopeTarget[1]]};
})()`);
assert.deepEqual(animation.current,animation.before);
assert.notDeepEqual(animation.target,animation.before);
await sleep(34);
const during=await evaluate('({current:[...envelopeCurrent[1]],target:[...envelopeTarget[1]]})');
assert.notDeepEqual(during.current,animation.before);
assert.notDeepEqual(during.current,during.target);
await sleep(250);
const finished=await evaluate('Math.max(...envelopeCurrent[1].map((value,index)=>Math.abs(value-envelopeTarget[1][index])))');
assert.ok(finished<.1);

// Touch highlight exists only while the finger owns the gesture.
const point=await evaluate("(()=>{const input=fields.release1.input;input.scrollIntoView({block:'center'});const r=input.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()");
await call('Emulation.setTouchEmulationEnabled',{enabled:true,maxTouchPoints:5});
await call('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{...point,id:1}]});
assert.equal(await evaluate("fields.release1.input.parentElement.getAttribute('data-active')"),'true');
await call('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
assert.equal(await evaluate("fields.release1.input.parentElement.getAttribute('data-active')"),'false');

// Existing musical DSP controls work without MIDI/audio and reconcile through
// the same state owner. Frequency controls retain useful logarithmic travel.
for(const [key,value] of [['piano_room_size',.72],['reverb_highcut',6400],['delay_time',417],['delay_highcut',9200]]){
  await evaluate(`(()=>{const input=fields[${JSON.stringify(key)}].input;input.value=toSlider(${JSON.stringify(key)},${value});input.dispatchEvent(new Event('input'));})()`);
}
await sleep(900);
const status=await (await fetch('http://127.0.0.1:8768/status')).json();
assert.equal(status.values.piano_room_size,.72);
assert.ok(Math.abs(status.values.reverb_highcut-6400)<10);
assert.equal(status.values.delay_time,417);
assert.ok(Math.abs(status.values.delay_highcut-9200)<25);

for(const [width,height] of [[1366,1024],[390,844]]){
  await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<500});
  for(const section of ['osc','piano','reverb','delay']){
    await evaluate(`view('edit');soundSection('${section}');window.scrollTo(0,0)`);
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
  }
}
console.log('PASS: real Chrome smooth immediate envelopes, released touch highlight, device-free musical controls, tablet/phone overflow');
ws.close();
