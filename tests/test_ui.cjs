const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(require('path').join(__dirname,'../app/static/index.html'),'utf8'),code=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements=new Map(),memory=new Map(),spoken=[],mediaCalls=[],tracks=[],plot=[];
const context2d={scale(){},clearRect(){},beginPath(){},moveTo(x,y){if(this.strokeStyle==='#98edca')plot.push(['move',x,y])},lineTo(x,y){if(this.strokeStyle==='#98edca')plot.push(['line',x,y])},stroke(){},fillText(){}};
function make(id=''){const attrs={};const e={id,value:'',checked:false,disabled:false,hidden:false,children:[],dataset:{},clientWidth:800,readyState:0,open:false,textContent:'',classList:{toggle(){},add(){},remove(){}},replaceChildren(){this.children=[]},append(...v){this.children.push(...v)},get options(){return this.children},setAttribute(k,v){attrs[k]=String(v)},getAttribute(k){return attrs[k]??null},removeAttribute(k){delete attrs[k]},showModal(){this.open=true},close(){this.open=false},play:async()=>{},getContext:()=>context2d};if(id)elements.set(id,e);return e}
for(const match of html.matchAll(/\bid="([^"]+)"/g))make(match[1]);const $=id=>{assert(elements.has(id),'Missing DOM id '+id);return elements.get(id)};
for(const id of ['workbench','results','history','captureShell','countdown'])$(id).hidden=true;
$('facing').value='user';$('delay').value='3';$('depth').value='110';$('guideToggle').checked=true;$('mirrorToggle').checked=true;$('piFeed').dataset.src='/video/pi';$('phoneFeed').dataset.src='/video/phone';
const cap={pi_side:'left',pi_lens:'standard',pi_model:'가상 카메라 · UI 검사',phone_model:'가상 휴대폰 · UI 검사',phone_lens:'front',phone_facing:'user',method:'test-only',rules:{depth:110,max_gap:1.25}};
const channel=()=>({camera:true,detected:false,measurement:{valid:false},angles:{},guide:{ready:false,reason:'not_detected',message:'구도 확인'},facing:'user'});
let status={phase:'idle',running:false,elapsed:0,countdown:0,participant:null,count:0,model_ready:true,channels:{pi:channel(),phone:channel()},capture:cap,current:{},both_connected:true},who='';
const saved={id:'SIMULATED-UI-ONLY',participant:'SIM_A',started_at:'2026-11-06T00:00:00Z',status:'completed',elapsed:5,conditions:cap,condition_key:'test',rules:{max_gap:1.25},summary:{count:0,mean_rom:null,mean_duration:null,mean_score:null,scored_repetitions:0},quality:{pi:{held_frames:1,missing_markers:1},phone:{held_frames:0,missing_markers:0}},repetitions:[],samples:[{channel:'pi',t:0,valid:true,knee:170},{channel:'pi',t:1,valid:false,knee:null},{channel:'pi',t:2,valid:true,knee:100},{channel:'pi',t:2.3,valid:true,knee:165}]};
async function fetch(path,o={}){const body=o.body?JSON.parse(o.body):{};let data;
if(path==='/api/participants'){who=body.participant;data={participant:who}}
else if(path==='/api/settings'){if(o.method==='POST')Object.assign(cap,body);data=cap}
else if(path.startsWith('/api/baseline?'))data={baseline:null};
else if(path==='/api/status')data=status;
else if(path==='/api/start'){status={...status,phase:'countdown',countdown:3,participant:who};data=status}
else if(path==='/api/cancel'){status={...status,phase:'idle',countdown:0};data=status}
else if(path==='/api/stop'){status={...status,phase:'saved',running:false};data={session:saved,status}}
else if(path==='/api/sessions/'+saved.id)data=saved;
else if(path.startsWith('/api/sessions?'))data={sessions:[]};
else throw Error('Unexpected API '+path);
return {ok:true,json:async()=>data}}
const speech={cancel(){},speak(u){spoken.push(u.text)}};
const sandbox={console,assert,Date,Math,Error,Set,Number,encodeURIComponent,setTimeout:()=>1,clearTimeout(){},localStorage:{getItem:k=>memory.get(k)||null,setItem:(k,v)=>memory.set(k,v)},document:{getElementById:$,createElement:()=>make(),body:{classList:{add(){},remove(){},toggle(){}}}},window:{isSecureContext:true,devicePixelRatio:1,addEventListener(){},speechSynthesis:speech},SpeechSynthesisUtterance:function(t){this.text=t},navigator:{userAgent:'Desktop UI simulation',mediaDevices:{getUserMedia:async options=>{mediaCalls.push(options);const t={stop(){this.stopped=true},getSettings:()=>({facingMode:options.video.facingMode.exact})};tracks.push(t);return {getTracks:()=>[t],getVideoTracks:()=>[t]}}}},fetch,crypto:{randomUUID:()=> 'SIMULATED-SENDER'}};sandbox.globalThis=sandbox;
vm.createContext(sandbox);vm.runInContext(code,sandbox);
const evaluate=s=>vm.runInContext(s,sandbox);
(async()=>{
await new Promise(setImmediate);
assert.equal($('captureShell').hidden,true);assert.equal($('piFeed').getAttribute('src'),null);
$('participantInput').value='SIM_A';$('ackGuide').checked=true;await $('enter').onclick();assert.equal($('workbench').hidden,false);assert.equal(memory.get('focus.participant'),'SIM_A');assert.equal(memory.get('focus.guide.research.v1'),'yes');
await $('start').onclick();assert.equal($('captureShell').hidden,false);assert.equal($('piFeed').getAttribute('src'),'/video/pi');assert.equal($('countdown').hidden,true);await $('captureRestart').onclick();assert(spoken.includes('3'));assert.equal($('countdown').hidden,false);
await $('cancelCountdown').onclick();assert.equal($('countdown').hidden,true);
status={...status,phase:'measuring',running:true,count:0,current:{pi:{valid:false},phone:{valid:false}}};evaluate('show('+JSON.stringify(status)+')');assert(spoken.some(x=>x.includes('측정을 시작')));
status={...status,count:1};evaluate('show('+JSON.stringify(status)+')');assert(spoken.includes('1회'));
await $('stop').onclick();assert.equal($('results').hidden,false);assert.equal($('captureShell').hidden,true);assert.equal($('piFeed').getAttribute('src'),null);assert.equal($('repRows').children.length,0);assert.equal(plot.filter(x=>x[0]==='move').length,2);assert.equal(plot.filter(x=>x[0]==='line').length,1);assert(spoken.some(x=>x.includes('결과를 저장')));
$('again').onclick();await new Promise(setImmediate);await $('phone').onclick();for(let i=0;i<8;i++)await new Promise(setImmediate);assert.equal(mediaCalls[0].video.facingMode.exact,'user');$('facing').value='environment';await $('facing').onchange();assert.equal(tracks[0].stopped,true);assert.equal(mediaCalls[1].video.facingMode.exact,'environment');$('phone').onclick();assert.equal(tracks[1].stopped,true);
console.log('SIMULATED UI: entry/separate preferences, hidden cameras, stacked capture/countdown/cancel, speech events, DB-result contract/zero/null, graph gaps, front/rear switch PASS');
})().catch(e=>{console.error(e);process.exitCode=1});
