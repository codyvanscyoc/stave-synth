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
    assert.equal(await evaluate('document.documentElement.scrollWidth<=document.documentElement.clientWidth'),true);
  }
}
// Verify the actual layout at phone and tablet sizes, all four work areas.
await call('Accessibility.enable');
for(const [width,height,phone] of [[320,844,true],[390,844,true],[768,1024,false],[844,390,true],[1024,768,false],[1366,1024,false]]){
  await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:phone});
  for(const tab of ['stage','check','edit','system']){
    await evaluate(`view('${tab}');window.scrollTo(0,0)`);
    assert.equal(await evaluate('document.documentElement.scrollWidth<=document.documentElement.clientWidth'),true,`${width} ${tab} horizontal overflow`);
  }
  await evaluate("view('stage');bank('mix')");
  const visible=()=>evaluate("Array.from(document.querySelectorAll('#mixer label.performance')).filter(n=>n.getBoundingClientRect().width).map(n=>n.dataset.control)");
  const undersized=await evaluate(`[...document.querySelectorAll('nav button,.header-actions button,#stage-panel button,#mixer input,#tone-controls input,#mobile-master input,#mobile-master button')].filter(el=>{
    const b=el.getBoundingClientRect();return b.width>0&&(b.width<43.9||b.height<43.9);
  }).map(el=>el.id||el.getAttribute('aria-label')||el.textContent)`);
  assert.deepEqual(undersized,[],`${width}×${height} Stage touch targets`);
  if(phone){
    assert.deepEqual(await visible(),['osc1','osc2','piano','master']);
    await evaluate("document.getElementById('bank-fx').click()");
    assert.deepEqual(await visible(),['piano','cutoff','wet','master']);
    assert.ok(await evaluate("document.getElementById('mobile-mute').getBoundingClientRect().height>=44"));
    await evaluate("window.scrollTo(0,document.body.scrollHeight)");
    assert.ok(await evaluate("document.getElementById('mobile-mute').getBoundingClientRect().bottom<=visualViewport.height+1"));
  }else assert.equal((await visible()).length,6);
  await evaluate("window.scrollTo(0,0)");
  const tree=(await call('Accessibility.getFullAXTree')).nodes.filter(node=>!node.ignored);
  const ax=(role,name)=>tree.find(node=>node.role?.value===role&&node.name?.value===name);
  for(const tab of ['Stage','Sound','Soundcheck','System'])assert.ok(ax('button',tab),tab+' missing in AX tree');
  const property=(node,key)=>node?.properties?.find(p=>p.name===key)?.value.value;
  const cutoff=ax('slider','Synth filter cutoff · Hz'),master=ax('slider','Master');
  assert.match(cutoff.description?.value,/Hz$/);assert.equal(cutoff.value.value,await evaluate('lastValues.cutoff'));assert.equal(property(cutoff,'orientation'),'vertical');
  assert.match(master.description?.value,/%$/);assert.equal(master.value.value,await evaluate('Math.round(lastValues.master*100)'));
  if(phone)assert.match(ax('slider','Master volume').description?.value,/%$/);
  assert.ok(ax('button','Mute'),'Mute missing in AX tree');
}
// Synthetic status, local only. Capture interactions without a MIDI/audio device.
await call('Page.addScriptToEvaluateOnNewDocument',{source:`
window.posted=[];const rawFetch=window.fetch.bind(window);
window.fetch=async function(path,opts){
  if(opts?.method==='POST'){window.posted.push([path,JSON.parse(opts.body)]);return new Response(JSON.stringify(path==='/midi-learn'?{ok:true,serial:0,lease_seconds:6}:{queued:1}));}
  const r=await rawFetch(path,opts);if(path!=='/status')return r;
  const d=await r.json();d.stale=false;d.exited=null;d.status={routed:true,blocks:500,fault:0,midi_connected:true};
  Object.assign(d.runtime,{restoring:false,preparation:false,midi_source:'Keys:MIDI',audio_left:'USB:Left',audio_right:'USB:Right',
    devices:{midi:['Keys:MIDI'],audio:['USB:Left','USB:Right']},presets:[{name:'Verse'},{name:'Chorus'},null,null,null],active_preset:0});
  return new Response(JSON.stringify(d));
};`});
await call('Page.reload');
for(let i=0;i<50&&!(await evaluate("document.getElementById('audio-status')?.textContent==='AUDIO on'"));++i)await sleep(100);
async function click(id){
  const p=await evaluate(`(()=>{const el=document.getElementById(${JSON.stringify(id)});el.scrollIntoView({block:'center'});const b=el.getBoundingClientRect();return {x:b.x+b.width/2,y:b.y+b.height/2};})()`);
  await call('Input.dispatchMouseEvent',{type:'mousePressed',...p,button:'left',clickCount:1});
  await call('Input.dispatchMouseEvent',{type:'mouseReleased',...p,button:'left',clickCount:1});
}
await click('midi-map');await sleep(50);await click('volume-link-stage');await sleep(60);
assert.equal(await evaluate('midiTarget'),'volume_link');assert.deepEqual(await evaluate("posted.filter(([path])=>path!=='/midi-learn')"),[],'learning a switch does not change the sound');
await evaluate("document.getElementById('freeze-toggle').focus()");
for(const type of ['keyDown','keyUp'])await call('Input.dispatchKeyEvent',{type,key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
await sleep(50);assert.equal(await evaluate('midiTarget'),'freeze');assert.deepEqual(await evaluate("posted.filter(([path])=>path!=='/midi-learn')"),[]);
await click('midi-map-done');
await click('preset-store');
await evaluate("document.getElementById('preset-destination').value='1';document.getElementById('preset-destination').dispatchEvent(new Event('change'))");
assert.deepEqual(await evaluate("posted.filter(([path])=>path!=='/midi-learn')"),[],'choosing the occupied store destination leaves the live sound alone');
console.log('PASS: native Chrome controls, touch, calibrated soundcheck, four phone faders, six tablet faders, mobile Master, tabs, keyboard and pointer Learn, independent preset store');
ws.close();
