// Real Chromium check. Requires the isolated preview on :8768 and CDP on :9228.
import assert from 'node:assert/strict';

const pages=await (await fetch('http://127.0.0.1:9228/json')).json();
const ws=new WebSocket(pages.find(page=>page.type==='page').webSocketDebuggerUrl);
await new Promise(resolve=>ws.addEventListener('open',resolve,{once:true}));
let sequence=0;const pending=new Map();
ws.addEventListener('message',event=>{const message=JSON.parse(event.data);if(!pending.has(message.id))return;const call=pending.get(message.id);pending.delete(message.id);message.error?call.reject(Error(JSON.stringify(message.error))):call.resolve(message.result);});
const call=(method,params={})=>new Promise((resolve,reject)=>{const id=++sequence;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
const evaluate=async expression=>{const result=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));return result.result.value;};
const inPage=(fn,...args)=>evaluate('('+fn.toString()+')(...'+JSON.stringify(args)+')');
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const statusOfInstrument=async()=>await (await fetch('http://127.0.0.1:8768/status')).json();

// Every publicly editable Sound parameter has a representative wire value.
// The local controller is the same state owner that serves the instrument.
const soundControls={
  osc:{wave1:2,wave2:1,envelope_link:0,volume_link:0,attack1:720,decay1:880,sustain1:62,release1:594,
    attack2:440,decay2:920,sustain2:71,release2:330,pan1:-.21,pan2:.24,detune:.15,spread:.62,
    cutoff:3850,resonance:1.72,filter_slope:1,piano_filter:1,cutoff_min:75,cutoff_max:15800},
  piano:{piano_voice:2,piano_tone:.65,piano_room:.32,piano_reverb:.19,piano_delay:.22,piano_room_size:.75,piano_room_damp:.43,piano_lowcut:115},
  lfo:{lfo1_shape:6,lfo1_target:3,lfo1_division:2,lfo1_key_sync:1,lfo1_rate:2.73,lfo1_depth:.28,wheel_depth:.38,
    lfo2_shape:2,lfo2_target:1,lfo2_division:7,lfo2_key_sync:1,lfo2_rate:.47,lfo2_depth:.58},
  reverb:{reverb:2,wet:.51,shimmer:1,shimmer_mix:.24,osc1_reverb:.38,osc2_reverb:.57,reverb_decay:3.4,
    reverb_damp:.48,reverb_predelay:37,reverb_lowcut:190,reverb_highcut:6900},
  delay:{delay_wet:.36,delay_feedback:.47,delay_time:322,delay_lowcut:85,delay_highcut:7300,
    bpm:118,delay_division:6}
};
async function waitFor(expression,description){
  for(let i=0;i<80;i++){
    if(await evaluate(expression))return;
    await sleep(25);
  }
  assert.fail(description);
}

await call('Page.enable');
await call('Emulation.setDeviceMetricsOverride',{width:1366,height:1024,deviceScaleFactor:1,mobile:false});
await call('Page.navigate',{url:'http://127.0.0.1:8768'});
for(let n=0;n<40&&!(await evaluate("document.getElementById('connection')?.textContent==='Connected'"));++n)await sleep(100);
assert.equal(await evaluate('ready&&!audioReady'),true);
assert.equal(await evaluate('activePage'),'check','an unconfigured instrument opens Soundcheck first');
assert.deepEqual(await evaluate("[fields.piano_voice.input.value,fields.lfo1_depth.input.value,fields.lfo2_depth.input.value,fields.wheel_depth.input.value].map(Number)"),[0,0,0,0],'fresh instrument opens with Grand and no modulation');
assert.deepEqual(await evaluate("Array.from(fields.piano_voice.input.options,o=>o.textContent)"),['Grand','Rhodes','Suitcase','Legend EP 2']);
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

await inPage(()=>{
  window.soundWrites=[];
  const original=window.fetch.bind(window);
  window.fetch=async(path,options)=>{
    const reply=await original(path,options);
    if(options?.method==='POST')soundWrites.push({path,data:JSON.parse(options.body),status:reply.status});
    return reply;
  };
});
async function openSound(section,advanced){
  await inPage((section,advanced)=>{
    document.getElementById('nav-edit').click();
    document.getElementById('sound-tab-'+section).click();
    const toggle=document.getElementById('sound-advanced');
    if((toggle.getAttribute('aria-pressed')==='true')!==advanced)toggle.click();
  },section,advanced);
}
async function editSound(panel,key,target){
  const before=await evaluate('soundWrites.length');
  const result=await inPage((panel,key,target)=>{
    const label=document.querySelector(panel+' [data-control="'+key+'"], '+panel+' [data-map-key="'+key+'"]');
    const input=label?.querySelector('select,input');
    if(!input||!input.getBoundingClientRect().width||input.disabled)return null;
    input.value=toSlider(key,target);
    const value=fromSlider(key,input.value);
    input.dispatchEvent(new Event(input.tagName==='SELECT'?'change':'input',{bubbles:true}));
    return value;
  },panel,key,target);
  assert.notEqual(result,null,panel+' must contain an enabled visible '+key);
  await waitFor('soundWrites.length>'+before,'Missing '+key+' request');
  const writes=await inPage(index=>soundWrites.slice(index),before);
  assert.deepEqual(writes,[{path:'/control',data:{key,value:result},status:202}],panel+' '+key+' sends one accepted edit');
  assert.equal((await statusOfInstrument()).values[key],result,key+' acknowledged by controller');
  await evaluate('poll()');
  return result;
}
async function verifyEditorLayout(width,height,section,advanced){
  if(section==='system')await evaluate("document.getElementById('nav-system').click()");
  else await openSound(section,advanced);
  const issues=await inPage((section,landscape)=>{
    const issues=[],root=document.getElementById(section==='system'?'edit-global':'sound-'+section);
    const rect=el=>el.getBoundingClientRect();
    const textBox=el=>{
      const range=new Range();range.selectNodeContents(el);return range.getBoundingClientRect();
    };
    const visible=el=>rect(el).width>0;
    if(document.documentElement.scrollWidth>document.documentElement.clientWidth)issues.push('horizontal page overflow');
    if(!visible(root))issues.push('missing selected section');
    if(section==='osc'){
      const plots=root.querySelectorAll('.envelope-plot');
      if(plots.length!==2)issues.push('both oscillators need an envelope display');
      for(const svg of plots){
        const trace=svg.querySelector('polyline'),box=rect(svg);
        const points=Array.from(trace.points,p=>p.matrixTransform(trace.getScreenCTM()));
        if(points.length!==5||points.some((p,i)=>i>0&&p.x<=points[i-1].x)){
          issues.push('envelope stages must progress from attack to release');continue;
        }
        const [start,peak,decay,sustain,end]=points;
        if(box.height<70||start.y-peak.y<58)issues.push('too little room for the envelope contour');
        if(Math.abs(start.y-end.y)>1||Math.abs(decay.y-sustain.y)>1||decay.y<peak.y-.5||decay.y>end.y+.5)
          issues.push('envelope levels drift from their attack, sustain or release positions');
        if(points.some(p=>p.x<box.left||p.x>box.right||p.y<box.top||p.y>box.bottom))
          issues.push('envelope trace is clipped');
      }
    }
    if(section==='system'){
      const knobs=root.querySelectorAll('.knob-dial'),sound=document.querySelector('#edit-env1 .knob-dial');
      if(knobs.length!==4)issues.push('Global Tone must include three EQ dials and the low-cut frequency dial');
      const ticks=el=>getComputedStyle(el,'::before').backgroundImage;
      if(!ticks(sound).includes('conic-gradient')||Array.from(knobs).some(dial=>ticks(dial)!==ticks(sound)))
        issues.push('System and Sound should use the same graduated dial face');
    }
    for(const input of root.querySelectorAll('label select,label input[type=range]')){
      if(!visible(input))continue;
      const label=input.closest('label'),key=label.dataset.control||label.dataset.mapKey;
      const name=label.querySelector('.control-name'),dial=label.querySelector('.knob-dial');
      if(!input.getAttribute('aria-label'))issues.push(key+' missing accessible name');
      if(rect(input).width<43.9||rect(input).height<43.9)issues.push(key+' touch area below 44px');
      if(!dial){
        if(label.closest('.contour,.motion-module')){
          const legend=textBox(name),box=rect(input);
          if(!legend.height||legend.bottom>box.top-2)issues.push(key+' label touches selector');
        }
        continue;
      }
      const readout=label.querySelector('output'),bounds=rect(label),face=rect(dial),ink=textBox(name);
      if(!ink.height||!rect(readout).height)issues.push(key+' missing visible name or value');
      const separated=(a,b)=>a.bottom<=b.top-2||b.bottom<=a.top-2||a.right<=b.left-2||b.right<=a.left-2;
      if(!separated(ink,face)||!separated(textBox(readout),face))issues.push(key+' text touches dial');
      if(ink.left<bounds.left-.5||ink.right>bounds.right+.5)issues.push(key+' label escapes its column');
      if(landscape&&label.closest('.contour')){
        const r=new Range();r.selectNodeContents(name);
        if(new Set([...r.getClientRects()].map(box=>Math.round(box.top))).size>1)issues.push(key+' wraps in shared filter');
      }
      input.scrollIntoView({block:'center',inline:'nearest'});
      const box=rect(dial);
      if(document.elementFromPoint(box.x+box.width/2,box.y+box.height/2)!==input)issues.push(key+' dial center misses its input');
    }
    if(landscape&&section==='osc'){
      const [left,right]=[...document.querySelectorAll('#edit-link select')].map(rect);
      if(Math.abs(left.top-right.top)>2||right.left-left.right<8)issues.push('link selectors need one row and a gap');
      const filter=document.getElementById('edit-filter');
      const cells=[...filter.querySelectorAll('label')].filter(visible).map(rect);
      if(cells.length<2||cells.some(b=>Math.abs(b.top-cells[0].top)>1||Math.abs(b.width-cells[0].width)>2))
        issues.push('shared filter controls need equal columns in one row');
    }
    if(section==='lfo'){
      const [a,b]=Array.from(root.querySelectorAll('.motion-module'),rect);
      if(landscape&&(a.top!==b.top||a.right+7>b.left))issues.push('independent LFOs need two separate columns');
      if(!landscape&&a.bottom+7>b.top)issues.push('LFOs overlap on the phone');
      for(const trace of root.querySelectorAll('.motion-trace path')){
        if(trace.getTotalLength()<90)issues.push('waveform missing');
      }
      const tabs=Array.from(document.querySelectorAll('.sound-selector button'),rect);
      if(tabs.some((tab,i)=>tab.width<44||tab.height<44||i>0&&tab.left<tabs[i-1].right+2))issues.push('Sound tab targets overlap');
    }
    return issues;
  },section,width>=768&&width>height);
  assert.deepEqual(issues,[],[width,height,section,advanced?'advanced':'normal'].join(' '));
}
await call('Accessibility.enable');
for(const [width,height] of [[1024,650],[1180,820],[1366,1024],[390,844],[320,690]]){
  await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:true});
  for(const advanced of [false,true])for(const section of Object.keys(soundControls)){
    await verifyEditorLayout(width,height,section,advanced);
    if(width===1024&&advanced){
      const wanted=await inPage(section=>[...document.querySelectorAll('#sound-'+section+' label input,#sound-'+section+' label select')].filter(el=>el.getBoundingClientRect().width).map(el=>({
        name:el.getAttribute('aria-label'),role:el.tagName==='SELECT'?'combobox':'slider',
        value:el.tagName==='INPUT'?document.getElementById(el.getAttribute('aria-describedby')).textContent:null
      })),section);
      const tree=(await call('Accessibility.getFullAXTree')).nodes.filter(node=>!node.ignored);
      for(const {name,role,value} of wanted){
        const entry=tree.find(node=>node.role?.value===role&&node.name?.value===name);
        assert.ok(entry,section+' '+name+' missing from browser accessibility tree');
        if(value!==null)assert.ok(entry.description?.value.includes(value),name+' exposes its formatted readout');
      }
    }
  }
  await verifyEditorLayout(width,height,'system',false);
}
assert.deepEqual(await evaluate('soundWrites'),[],'navigation and Advanced never send edits');
await inPage(()=>{
  document.getElementById('nav-system').click();
  document.getElementById('system-advanced').click();
  document.getElementById('fine-controls').click();
});
assert.deepEqual(await evaluate("[localStorage.getItem('stave-show-advanced'),localStorage.getItem('stave-fine-controls'),document.getElementById('sound-advanced').getAttribute('aria-pressed')]"),['0','1','false']);
await call('Emulation.setDeviceMetricsOverride',{width:1024,height:768,deviceScaleFactor:1,mobile:true});
// Exercise the five Global Tone controls through the browser's keyboard handling.
await click('nav-system');
const eqEdits=[
  ['master_low','ArrowRight',39,.1],['master_mid','ArrowLeft',37,-.1],
  ['master_high','ArrowRight',39,.1],['master_lowcut','ArrowDown',40,1],
  ['master_lowcut_hz','ArrowRight',39,81]
];
for(const [key,direction,keyCode,expected] of eqEdits){
  const before=await evaluate('soundWrites.length');
  await inPage(key=>{
    const input=document.querySelector('#edit-global [data-control="'+key+'"] input,#edit-global [data-control="'+key+'"] select');
    input.scrollIntoView({block:'center'});input.focus();
  },key);
  for(const type of ['keyDown','keyUp']){
    await call('Input.dispatchKeyEvent',{type,key:direction,code:direction,windowsVirtualKeyCode:keyCode});
  }
  await waitFor('soundWrites.length>'+before,key+' keyboard edit');
  assert.deepEqual(await inPage(i=>soundWrites.slice(i),before),[{path:'/control',data:{key,value:expected},status:202}]);
  assert.equal((await statusOfInstrument()).values[key],expected,key+' controller value');
  await evaluate('poll()');
  if(key!=='master_lowcut'){
    assert.ok(await inPage((key,expected)=>{
      const field=fields[key];
      const position=(Number(field.input.value)-Number(field.input.min))/(Number(field.input.max)-Number(field.input.min));
      return field.out.textContent===formatted(key,expected)&&field.input.getAttribute('aria-valuetext')===field.out.textContent&&
        Math.abs(parseFloat(field.dial.style.getPropertyValue('--knob-turn'))-(270*position-135))<.01;
    },key,expected),key+' readout, accessible value and pointer follow the input');
  }
}
const eqPoint=await inPage(()=>{
  const input=fields.master_mid.input;input.scrollIntoView({block:'center'});
  const r=input.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};
});
const beforeTouch=await evaluate('soundWrites.length');
await call('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{...eqPoint,id:1}]});
assert.equal(await evaluate("fields.master_mid.input.closest('label').dataset.active"),'true');
await call('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:eqPoint.x+64,y:eqPoint.y,id:1}]});
await call('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
await waitFor('soundWrites.length>'+beforeTouch,'System mid EQ touch adjustment');
assert.ok((await statusOfInstrument()).values.master_mid>0,'drag to boost the mid band');
assert.equal(await evaluate("fields.master_mid.input.closest('label').dataset.active"),'false');

for(const [section,keys] of Object.entries(soundControls)){
  await openSound(section,true);
  const present=await inPage(section=>[...document.querySelectorAll('#sound-'+section+' label')].filter(label=>label.getBoundingClientRect().width&&label.querySelector('input,select')).map(label=>label.dataset.control||label.dataset.mapKey),section);
  assert.deepEqual(present.filter(key=>key!=='freeze').sort(),Object.keys(keys).sort(),section+' control inventory');
  for(const [key,target] of Object.entries(keys))await editSound('#sound-'+section,key,target);
}
await openSound('lfo',false);
assert.ok(await evaluate("fields.lfo1_rate.out.textContent==='2.73 Hz'&&fields.wheel_depth.out.textContent==='38%'"));
assert.ok(await evaluate("elem('lfo1-trace').getAttribute('d')===lfoTraces[6]&&elem('lfo2-trace').getAttribute('d')===lfoTraces[2]"),'each LFO has its own waveform preview');
await click('lfo-tempo');
assert.ok(await evaluate("document.activeElement===fields.bpm.input&&elem('sound-delay').dataset.open==='true'"),'shared tempo opens the editable delay clock');
await evaluate("view('stage')");
for(const [section,panel,key,value] of [['osc','#mixer','cutoff',3150],['piano','#tone-controls','piano_tone',.71],['reverb','#mixer','wet',.62]]){
  const wire=await editSound(panel,key,value);
  const readouts=await inPage((section,key,expected)=>{
    const f=document.querySelector('#sound-'+section+' label[data-control="'+key+'"]');
    return [f.querySelector('output').textContent,fields[key].out.textContent,formatted(key,expected)];
  },section,key,wire);
  assert.equal(readouts[0],readouts[2],key+' Sound dial reflects Stage');
  assert.equal(readouts[1],readouts[2],key+' Stage and Sound agree');
}
await openSound('osc',false);
const attack2=(await statusOfInstrument()).values.attack2;
await editSound('#sound-osc','attack1',930);
assert.equal((await statusOfInstrument()).values.attack2,attack2,'independent envelope preserves the other voice');
await editSound('#sound-osc','envelope_link',1);
const linkedAttack=await editSound('#sound-osc','attack1',1230);
assert.equal((await statusOfInstrument()).values.attack2,linkedAttack,'linked envelopes agree in the engine');
assert.equal(await evaluate('fields.attack2.out.textContent'),await evaluate('fields.attack1.out.textContent'),'OSC 2 readout follows linked envelope edits');
await evaluate("view('stage')");
await editSound('#mixer','osc1',.35);
await editSound('#mixer','osc2',.49);
assert.equal((await statusOfInstrument()).values.osc1,.35,'OSC 2 edits preserve independent OSC 1');
await openSound('osc',false);
await editSound('#sound-osc','volume_link',1);
await evaluate("view('stage')");
await editSound('#mixer','osc1',.40);
assert.ok(Math.abs((await statusOfInstrument()).values.osc2-.54)<1e-7,'linked OSC levels preserve the original offset');
await openSound('osc',false);
for(const key of ['octave1','octave2']){
  const previous=(await statusOfInstrument()).values[key];
  await inPage(key=>document.querySelector('#sound-osc [data-stepper="'+key+'"] button:last-child').click(),key);
  await waitFor('!busy','octave update finished');
  assert.equal((await statusOfInstrument()).values[key],previous+1);
  await evaluate('poll()');
  assert.ok(await inPage(key=>[...document.querySelectorAll('[data-stepper="'+key+'"] output')].every(el=>el.textContent===formatted(key,lastValues[key])),key),'Sound and Stage agree on '+key);
}
async function soundInputPoint(key){
  return inPage(key=>{
    const input=document.querySelector('#sound-osc label[data-control="'+key+'"] input,#sound-osc label[data-map-key="'+key+'"] input');
    input.scrollIntoView({block:'center'});
    const r=input.getBoundingClientRect();
    return {x:r.x+r.width/2,y:r.y+r.height/2};
  },key);
}
const cutoffPoint=await soundInputPoint('cutoff');
const cutoffBefore=(await statusOfInstrument()).values.cutoff;
await call('Input.dispatchMouseEvent',{type:'mousePressed',...cutoffPoint,button:'left',clickCount:1});
await call('Input.dispatchMouseEvent',{type:'mouseMoved',x:cutoffPoint.x+30,y:cutoffPoint.y,button:'left',buttons:1});
await call('Input.dispatchMouseEvent',{type:'mouseReleased',x:cutoffPoint.x+30,y:cutoffPoint.y,button:'left',clickCount:1});
await waitFor('!busy','pointer cutoff committed');
const cutoffDragged=(await statusOfInstrument()).values.cutoff;
assert.ok(cutoffDragged>cutoffBefore,'dragging the dial right opens the actual filter');
const beforeArrow=await evaluate('soundWrites.length');
for(const type of ['keyDown','keyUp'])await call('Input.dispatchKeyEvent',{type,key:'ArrowLeft',code:'ArrowLeft',windowsVirtualKeyCode:37});
await waitFor('!busy','keyboard cutoff committed');
const cutoffKeyed=(await statusOfInstrument()).values.cutoff;
assert.ok(cutoffKeyed<cutoffDragged,'keyboard fine adjustment closes the filter');
assert.deepEqual(await inPage(index=>soundWrites.slice(index),beforeArrow),[{path:'/control',data:{key:'cutoff',value:cutoffKeyed},status:202}],'one keyboard adjustment submits once');
await evaluate('poll()');
assert.equal(await evaluate('lastValues.cutoff'),cutoffKeyed);
assert.equal(await evaluate('fields.cutoff.out.textContent'),await evaluate("document.querySelector('#edit-filter [data-map-key=cutoff] output').textContent"),'Stage and Sound display the same cutoff');

await openSound('delay',false);
await click('tap-tempo');
assert.match(await evaluate("document.getElementById('tap-readout').textContent"),/again/i);
await sleep(500);
await click('tap-tempo');
await waitFor('!busy','tempo committed');
const tapped=(await statusOfInstrument()).values.bpm;
assert.ok(tapped>=100&&tapped<=125,'second physical tap sets tempo, got '+tapped);

// Only the isolated preview's store is touched. Round-trip oscillator and
// effects values from the Sound editor through the regular Stage preset UI.
await evaluate("view('stage');poll()");
await click('preset-store');
await evaluate("document.getElementById('preset-name').value='Browser voice'");
await click('preset-confirm');
await waitFor("soundWrites.some(w=>w.path==='/preset-save'&&w.status===202)",'preset save');
await evaluate('poll()');
await openSound('osc',false);
await editSound('#sound-osc','cutoff',800);
await openSound('delay',false);
await editSound('#sound-delay','bpm',87);
await openSound('piano',false);
await editSound('#sound-piano','piano_voice',3);
await openSound('lfo',false);
await editSound('#sound-lfo','lfo1_shape',3);
await editSound('#sound-lfo','lfo2_depth',0);
await editSound('#sound-lfo','wheel_depth',0);
await evaluate("view('stage');document.querySelector('[data-preset]').click()");
await waitFor("soundWrites.some(w=>w.path==='/preset-load'&&w.status===202)",'preset recall');
const recalled=await statusOfInstrument();
assert.equal(recalled.values.cutoff,cutoffKeyed);
assert.equal(recalled.values.bpm,tapped);
assert.equal(recalled.values.piano_voice,2);
for(const [key,value] of Object.entries(soundControls.lfo))assert.equal(recalled.values[key],value,'preset recalls '+key);
await evaluate('poll()');
assert.ok(await evaluate("fields.piano_voice.out.textContent==='Suitcase'&&elem('lfo1-trace').getAttribute('d')===lfoTraces[6]&&fields.lfo2_depth.out.textContent==='58%'"),'preset changes repaint panel controls');
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
  d.values.shimmer=1;d.values.freeze=1;d.values.transpose=window.stageTranspose??0;
  if(window.voiceMask!==undefined)d.status.piano_voice_mask=window.voiceMask;
  if(window.mappedCutoff){
    d.values.cutoff=window.mappedCutoff;
    d.runtime.midi_maps={cutoff:74};
    d.status.midi_mapped_serial=controlOrder.map(key=>key==='cutoff'?(window.mappedSerial||12):0);
  }
  if(window.mappedEq){
    d.values.master_mid=2.3;
    d.runtime.midi_maps={master_mid:23};
    d.status.midi_mapped_serial=controlOrder.map(key=>key==='master_mid'?14:0);
    d.status.pickup_waiting=['master_mid'];d.status.pickup_directions={master_mid:'down'};
  }
  if(window.mappedModulation){
    Object.assign(d.values,{piano_voice:1,lfo2_shape:4,wheel_depth:.76});
    d.runtime.midi_maps={piano_voice:20,lfo2_shape:21,wheel_depth:22};
    d.status.midi_mapped_serial=controlOrder.map(key=>Object.hasOwn(d.runtime.midi_maps,key)?15:0);
  }
  if(window.haltAudio)d.stale=true;
  if(window.missingDevices){
    d.runtime.preparation=true;
    d.runtime.devices={midi:[],audio:[]};
    d.runtime.missing_routes={midi_source:'Keys:MIDI',audio_left:'USB:Left',audio_right:'USB:Right'};
    d.status={};
    d.stale=true;
  }
  return new Response(JSON.stringify(d));
};`});
await call('Page.reload');
for(let i=0;i<50&&!(await evaluate("document.getElementById('audio-status')?.textContent==='AUDIO on'"));++i)await sleep(100);
async function click(id){
  const p=await evaluate(`(()=>{const el=document.getElementById(${JSON.stringify(id)});el.scrollIntoView({block:'center'});const b=el.getBoundingClientRect();return {x:b.x+b.width/2,y:b.y+b.height/2};})()`);
  await call('Input.dispatchMouseEvent',{type:'mousePressed',...p,button:'left',clickCount:1});
  await call('Input.dispatchMouseEvent',{type:'mouseReleased',...p,button:'left',clickCount:1});
}
// Keep the playable controls together even when Safari's bars reduce a tablet's usable height.
async function verifyLandscapeStage(width,height){
  await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:true});
  await evaluate("view('stage');window.scrollTo(0,0)");
  const layout=await evaluate(`(()=>{
    const box=selector=>document.querySelector(selector).getBoundingClientRect();
    const output=document.querySelector('#transpose-stepper output');
    const ink=new Range();ink.selectNodeContents(output);
    const inkRect=ink.getBoundingClientRect(),nav=box('nav'),shimmer=box('#shimmer-toggle'),freeze=box('#freeze-toggle');
    const tracks=[...document.querySelectorAll('#mixer input[type=range]')].map(node=>node.getBoundingClientRect());
    const readouts=[...document.querySelectorAll('#mixer label.performance > output')].map(node=>node.getBoundingClientRect());
    const aligned=rects=>rects.length===6&&rects.every(r=>Math.abs(r.top-rects[0].top)<1&&Math.abs(r.bottom-rects[0].bottom)<1);
    const pads=[...document.querySelectorAll('#bed-keys button'),document.getElementById('bed-stop')];
    const reachable=pads.every(button=>{
      const rect=button.getBoundingClientRect();
      if(rect.width<43.9||rect.height<43.9||rect.top<0||rect.bottom>visualViewport.height+1)return false;
      return document.elementFromPoint(rect.x+rect.width/2,rect.y+rect.height/2)===button;
    });
    return {
      gapLeft:inkRect.left-box('#transpose-stepper button:first-of-type').right,
      gapRight:box('#transpose-stepper button:last-of-type').left-inkRect.right,
      fader:box('#mixer [data-control=osc1] input').height,
      switchesAligned:Math.min(shimmer.top,freeze.top)>=nav.top-3&&Math.max(shimmer.bottom,freeze.bottom)<=nav.bottom+3,
      reachable,pads:pads.length,channelsAligned:aligned(tracks)&&aligned(readouts)
    };
  })()`);
  const screen=width+'×'+height;
  assert.ok(layout.gapLeft>=7&&layout.gapRight>=7,screen+' transpose readout needs space on both sides');
  assert.ok(layout.switchesAligned,screen+' Shimmer and Freeze protrude from the header');
  assert.ok(layout.fader>(height>=768?220:190),screen+' fader travel');
  assert.ok(layout.channelsAligned,screen+' the six fader rails and their readouts must line up');
  assert.equal(layout.pads,13,screen+' should have all twelve keys and Stop');
  assert.ok(layout.reachable,screen+' all twelve keys and Stop must be reachable without scrolling');
}
for(const [width,height] of [[1024,650],[1024,768],[1180,700],[1180,820],[1366,768]]){
  await verifyLandscapeStage(width,height);
}
assert.equal(await evaluate("document.getElementById('shimmer-toggle').innerText"),'Shimmer on');
assert.equal(await evaluate("document.getElementById('freeze-toggle').innerText"),'UNFREEZE');
for(const transpose of [-24,24]){
  await evaluate(`window.stageTranspose=${transpose};poll()`);
  assert.equal(await evaluate("document.querySelector('#transpose-stepper output').innerText"),transpose>0?'+24 st':'-24 st');
  await verifyLandscapeStage(1024,650);
}
await evaluate("window.missingDevices=true;poll()");
await verifyLandscapeStage(1024,650);
assert.equal(await evaluate("document.getElementById('notice').innerText"),'','missing devices do not cover the Stage');
assert.equal(await evaluate("document.getElementById('midi-status').innerText"),'MIDI offline');
assert.equal(await evaluate("document.getElementById('audio-status').innerText"),'AUDIO offline');
await click('nav-check');
assert.match(await evaluate("document.getElementById('check-summary').innerText"),/Missing keyboard.*Keys:MIDI.*USB:Left.*USB:Right/);
await evaluate("window.missingDevices=false;poll()");
await click('nav-stage');
await click('midi-map');await sleep(50);await click('volume-link-stage');await sleep(60);
assert.equal(await evaluate('midiTarget'),'volume_link');assert.deepEqual(await evaluate("posted.filter(([path])=>path!=='/midi-learn')"),[],'learning a switch does not change the sound');
await evaluate("document.getElementById('freeze-toggle').focus()");
for(const type of ['keyDown','keyUp'])await call('Input.dispatchKeyEvent',{type,key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
await sleep(50);assert.equal(await evaluate('midiTarget'),'freeze');assert.deepEqual(await evaluate("posted.filter(([path])=>path!=='/midi-learn')"),[]);
await openSound('osc',true);
const learnPoint=await soundInputPoint('cutoff');
await call('Input.dispatchMouseEvent',{type:'mousePressed',...learnPoint,button:'left',clickCount:1});
await call('Input.dispatchMouseEvent',{type:'mouseReleased',...learnPoint,button:'left',clickCount:1});
await waitFor("midiTarget==='cutoff'",'Learn picks the shared filter from its Sound dial');
assert.deepEqual(await evaluate("posted.filter(([path])=>path!=='/midi-learn')"),[],'selecting a Sound target leaves its sound unchanged');
for(const [section,keys] of [['piano',['piano_voice']],['lfo',Object.keys(soundControls.lfo)]]){
  await openSound(section,false);
  for(const key of keys){
    await inPage(key=>fields[key].input.click(),key);
    await waitFor('midiTarget==='+JSON.stringify(key),'Learn can arm '+key);
  }
}
assert.deepEqual(await evaluate("posted.filter(([path])=>path!=='/midi-learn')"),[],'piano and LFO Learn target selection leaves the sound unchanged');
await click('midi-map-done');
await openSound('osc',true);
const mappedCount=await evaluate('posted.length');
await evaluate('window.mappedCutoff=3948;poll()');
await waitFor("document.querySelector('#edit-filter [data-map-key=cutoff] output').textContent===formatted('cutoff',3948)",'mapped MIDI filter followed by the Sound dial');
assert.equal(await evaluate("fields.cutoff.out.textContent"),await evaluate("formatted('cutoff',3948)"));
assert.equal(await evaluate('posted.length'),mappedCount,'mapped MIDI updates are displayed without an echo write');
const interrupted=await soundInputPoint('cutoff');
await evaluate('window.mappedCutoff=2480;window.mappedSerial=13;poll()');
await call('Input.dispatchMouseEvent',{type:'mousePressed',...interrupted,button:'left',clickCount:1});
await sleep(100);
await call('Input.dispatchMouseEvent',{type:'mouseReleased',...interrupted,button:'left',clickCount:1});
await evaluate('window.mappedCutoff=1890;poll()');
await waitFor("fields.cutoff.out.textContent===formatted('cutoff',1890)",'releasing a dial during a MIDI animation allows subsequent status changes to repaint');
await click('nav-system');
const beforeMappedEq=await evaluate('posted.length');
await evaluate('window.mappedCutoff=null;window.mappedEq=true;poll()');
await waitFor("fields.master_mid.out.textContent==='+2.3 dB'",'System dial follows a mapped MIDI EQ move');
const eqBadge=await inPage(()=>{
  const label=fields.master_mid.input.closest('label'),style=getComputedStyle(label,'::after');
  return [label.dataset.midiCc,label.dataset.pickup,style.position,style.content.includes('23')];
});
assert.deepEqual(eqBadge,['23','true','static',true],'EQ pickup badge has a separate row');
assert.equal(await evaluate('posted.length'),beforeMappedEq,'incoming System MIDI never echoes a write');
await evaluate('window.mappedEq=false;poll()');
const beforeMappedLfo=await evaluate('posted.length');
await evaluate('window.voiceMask=5;poll()');
assert.deepEqual(await evaluate('Array.from(fields.piano_voice.input.options,o=>[o.disabled,o.textContent])'),[[false,'Grand'],[true,'Rhodes · unavailable'],[false,'Suitcase'],[true,'Legend EP 2 · unavailable']],'live SoundFont inventory marks only missing voices');
await evaluate('window.voiceMask=undefined;poll()');
assert.ok(await evaluate('Array.from(fields.piano_voice.input.options).every(o=>!o.disabled)'), 'without inventory the piano control offers every model');
await evaluate('window.mappedModulation=true;poll()');
assert.deepEqual(await evaluate("[fields.piano_voice.input.value,fields.lfo2_shape.input.value,elem('lfo2-trace').getAttribute('d')===lfoTraces[4]]"),['1','4',true],'MIDI selects discrete models and waveforms immediately');
await waitFor("fields.wheel_depth.out.textContent==='76%'",'wheel depth dial follows hardware');
assert.equal(await evaluate('posted.length'),beforeMappedLfo,'incoming piano and LFO MIDI never echo writes');
await evaluate('window.mappedModulation=false;poll()');
await openSound('reverb',true);
await inPage(()=>{
  const f=document.querySelector('#sound-reverb [data-control=freeze] select');
  if(f.disabled)throw Error('Freeze should be enabled with routed audio');
  f.value='0';f.dispatchEvent(new Event('change',{bubbles:true}));
});
await waitFor("posted.some(([path,data])=>path==='/control'&&data.key==='freeze'&&data.value===0)",'Freeze command');
await evaluate('window.haltAudio=true;poll()');
assert.ok(await evaluate("[...document.querySelectorAll('#edit-panel label input,#edit-panel label select,#edit-panel [data-stepper] button,#tap-tempo')].every(el=>el.disabled)"),'stale instrument disables every Sound edit');
const idleCount=await evaluate('posted.length');
await evaluate("fields.freeze.input.dispatchEvent(new Event('change'));fields.piano_voice.input.dispatchEvent(new Event('change'));fields.lfo1_depth.input.dispatchEvent(new Event('input'));elem('tap-tempo').click()");
assert.equal(await evaluate('posted.length'),idleCount,'stale sound surface cannot send');
await evaluate("window.haltAudio=false;window.mappedCutoff=null;view('stage');poll()");
const beforeStore=await evaluate('posted.length');
await click('preset-store');
await evaluate("document.getElementById('preset-destination').value='1';document.getElementById('preset-destination').dispatchEvent(new Event('change'))");
assert.equal(await evaluate('posted.length'),beforeStore,'choosing the occupied store destination leaves the live sound alone');
const parameterCount=Object.values(soundControls).reduce((count,group)=>count+Object.keys(group).length,0);
// Serve a beta-5 control inventory to this client and check discoverability.
await call('Page.addScriptToEvaluateOnNewDocument',{source:`
const fetchAllControls=window.fetch;
window.fetch=async(path,options)=>{
  const response=await fetchAllControls(path,options);
  if(path!=='/controls'&&path!=='/status')return response;
  const data=await response.json(),values=path==='/controls'?data:data.values;
  for(const key of Object.keys(values))if(/^lfo[12]_/.test(key)||key==='wheel_depth'||key==='piano_voice')delete values[key];
  return new Response(JSON.stringify(data));
};`});
await call('Page.reload');
await waitFor("typeof ready!=='undefined'&&ready&&!fields.piano_voice",'legacy controller initializes');
await openSound('piano',false);
assert.ok(await evaluate("elem('sound-tab-lfo').hidden&&!elem('edit-piano-voice').getBoundingClientRect().width&&fields.piano_tone.input.disabled===false"),'legacy controller retains the original piano and Sound tabs');
console.log('PASS: '+parameterCount+' state-backed Sound controls, 50 responsive Sound layouts, five System EQ layouts, piano/LFO MIDI Learn and hardware feedback, preset recall and legacy control inventory, Stage, Soundcheck, stale guards and pickup gestures.');
ws.close();
