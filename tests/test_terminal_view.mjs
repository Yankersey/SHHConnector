import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const context=vm.createContext({});
vm.runInContext(fs.readFileSync(new URL('../static/terminal-view.js',import.meta.url),'utf8'),context);
const view=context.TerminalView;
assert.equal(view.resolveTheme('auto',false),'light');
assert.equal(view.resolveTheme('auto',true),'dark');
assert.equal(view.resolveTheme('light',true),'light');
assert.equal(view.resolveTheme('dark',false),'dark');
assert.equal(view.palette('light').background,'#ffffff');
assert.equal(view.palette('dark').background,'#000000');
const changed=view.palette('light');changed.background='#ff0000';assert.equal(view.palette('light').background,'#ffffff');

function terminal(lines){return {buffer:{normal:{length:lines.length,getLine:i=>lines[i]}}};}
function line(text,isWrapped=false){return {isWrapped,translateToString:trim=>trim?text.trimEnd():text};}
const unicode=terminal([line('中文 │ café é 🙂'),line('自动换行 '),line('继续',true),line(''),line('')]);
assert.equal(view.history(unicode),'中文 │ café é 🙂\n自动换行 继续');
assert.equal(view.history(unicode,'掉线前的中文'),'掉线前的中文\n中文 │ café é 🙂\n自动换行 继续');
assert.equal(view.history(terminal([])),'');
const many=Array.from({length:20003},(_,i)=>line('中文第 '+i+' 行'));
assert.ok(!view.history(terminal(many)).includes('中文第 2 行'));
assert.ok(view.history(terminal(many)).startsWith('中文第 3 行'));
assert.ok(view.history(terminal([line('新连接')]),many.map((_,i)=>'旧记录 '+i).join('\n')).split('\n').length<=20000);
const bounded=view.history(terminal([line('🙂'+'中'.repeat(2097151))]));
assert.equal(bounded,'中'.repeat(2097151));

let wheel,removed=false,disposed=false,history=0,live=0,changes=0,remoteMouse=false,historyOpen=false;
const calls=[];
const term={rows:20,buffer:{active:{type:'normal',baseY:300,viewportY:300}},scrollLines:n=>calls.push(['lines',n]),scrollPages:n=>calls.push(['pages',n]),scrollToTop:()=>calls.push(['top']),onScroll:()=>({dispose:()=>{disposed=true;}})};
const element={addEventListener:(name,listener,options)=>{assert.equal(name,'wheel');assert.equal(options.capture,true);assert.equal(options.passive,false);wheel=listener;},removeEventListener:()=>{removed=true;}};
const controller=view.installScroll(term,element,{remoteMouse:()=>remoteMouse,historyOpen:()=>historyOpen,lineHeight:()=>20,openHistory:()=>{history++;},changed:()=>{changes++;},live:()=>{live++;}});
function event(values={}){return {deltaY:0,deltaMode:0,type:'keydown',shiftKey:false,ctrlKey:false,metaKey:false,altKey:false,preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;},...values};}
let e=event({deltaY:-80});wheel(e);assert.deepEqual(calls.pop(),['lines',-4]);assert.ok(e.prevented&&e.stopped);
wheel(event({deltaY:60}));assert.deepEqual(calls.pop(),['lines',3]);
wheel(event({deltaY:-3,deltaMode:1}));assert.deepEqual(calls.pop(),['lines',-3]);
wheel(event({deltaY:1,deltaMode:2}));assert.deepEqual(calls.pop(),['lines',20]);
const before=calls.length;wheel(event({deltaY:.5}));wheel(event({deltaY:.5}));assert.equal(calls.length,before);
term.buffer.active.type='alternate';e=event({deltaY:-60});wheel(e);assert.equal(history,1);assert.ok(e.prevented);assert.equal(calls.length,before);
e=event({deltaY:60});wheel(e);assert.ok(e.prevented);assert.equal(history,1);
remoteMouse=true;e=event({deltaY:-60});wheel(e);assert.ok(!e.prevented);assert.equal(history,1);
e=event({deltaY:-60,shiftKey:true});wheel(e);assert.equal(history,2);assert.ok(e.prevented);
historyOpen=true;e=event({deltaY:-60});wheel(e);assert.ok(!e.prevented);assert.equal(history,2);historyOpen=false;
remoteMouse=false;term.buffer.active.type='normal';
e=event({key:'PageUp',shiftKey:true});assert.ok(controller.key(e));assert.deepEqual(calls.pop(),['pages',-1]);assert.ok(e.prevented);
assert.ok(controller.key(event({key:'PageDown',shiftKey:true})));assert.deepEqual(calls.pop(),['pages',1]);
assert.ok(controller.key(event({key:'Home',shiftKey:true})));assert.deepEqual(calls.pop(),['top']);
assert.ok(controller.key(event({key:'End',shiftKey:true})));assert.equal(live,1);
assert.equal(controller.key(event({key:'PageUp'})),false);
assert.equal(controller.key(event({key:'PageUp',shiftKey:true,ctrlKey:true})),false);
assert.ok(changes>=6);
controller.dispose();assert.ok(removed&&disposed);

// Use the shipped renderer as well as mocks: screen enters an alternate
// display on attachment even when its remote shell is printing ordinary logs.
Object.assign(context,{console,setTimeout,clearTimeout,setInterval,clearInterval,TextDecoder,TextEncoder,navigator:{userAgent:'node-tests',platform:'Linux'}});
context.self=context;context.window=context;
vm.runInContext(fs.readFileSync(new URL('../static/vendor/xterm.js',import.meta.url),'utf8'),context);
vm.runInContext(fs.readFileSync(new URL('../static/mouse-filter.js',import.meta.url),'utf8'),context);
const actual=new context.Terminal({cols:80,rows:24,scrollback:20000,allowProposedAPI:true});
const filter=new context.TerminalMouseFilter(false,{keepScrollback:true}),decoder=new TextDecoder();
const output='\x1b[?1049h\x1b[2J\x1b[H'+Array.from({length:300},(_,i)=>'\x1b[32m第 '+i+' 行 │ 中文 café é 🙂\x1b[0m\r\n').join('');
const encoded=new TextEncoder().encode(output);let rendered='';
for(let i=0;i<encoded.length;i+=7)rendered+=filter.feed(decoder.decode(encoded.slice(i,i+7),{stream:true}));
rendered+=filter.feed(decoder.decode());
await new Promise(resolve=>actual.write(rendered,resolve));
assert.equal(actual.buffer.active.type,'normal');
assert.ok(actual.buffer.normal.baseY>250);
const previous=view.history(actual);
assert.ok(previous.includes('第 0 行 │ 中文 café é 🙂'));
assert.ok(previous.includes('第 299 行 │ 中文 café é 🙂'));
assert.ok(!previous.includes('\ufffd'));
assert.ok(!previous.includes('\x1b'));
// A new transport resets terminal state, while the review snapshot retains
// previously received Unicode without contacting screen's lossy hardcopy path.
actual.reset();await new Promise(resolve=>actual.write('重新连接后的中文\r\n',resolve));
const reconnected=view.history(actual,previous);
assert.ok(reconnected.includes('第 0 行 │ 中文 café é 🙂'));
assert.ok(reconnected.includes('重新连接后的中文'));
actual.dispose();
console.log('PASS terminal Unicode history, theme and scroll interactions');
