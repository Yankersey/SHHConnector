import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const context=vm.createContext({console,setTimeout,clearTimeout,setInterval,clearInterval,TextDecoder,TextEncoder,navigator:{userAgent:'node-test',platform:'Linux'}});
context.self=context;context.window=context;
for(const path of ['vendor/xterm.js','vendor/addon-serialize.js','mouse-filter.js','terminal-view.js','sharing.js'])vm.runInContext(fs.readFileSync(new URL('../static/'+path,import.meta.url),'utf8'),context);
const flush=term=>new Promise(resolve=>term.write('',resolve));
function frame(text,seq,snapshot=false){
  const bytes=new TextEncoder().encode(text),out=new Uint8Array(bytes.length+9),header=new DataView(out.buffer);
  header.setUint8(0,snapshot?1:0);header.setUint32(1,Math.floor(seq/4294967296));header.setUint32(5,seq%4294967296);out.set(bytes,9);return out.buffer;
}
function client(){
  const term=new context.Terminal({cols:40,rows:24,scrollback:20000,allowProposedAPI:true});
  const serializer=new context.SerializeAddon.SerializeAddon();term.loadAddon(serializer);
  let filter=new context.TerminalMouseFilter(false,{keepScrollback:true});const sent=[],roles=[];
  const sharing=new context.TerminalSharing(term,serializer,{filter:()=>filter,send:message=>sent.push(message),reset:()=>{filter=new context.TerminalMouseFilter(false,{keepScrollback:true});},geometry(){},role:(data,replaying)=>roles.push({control:data.control,replaying})});
  sharing.message({type:'replay_start',cols:40,rows:24});sharing.message({type:'replay_end',seq:0});sharing.message({type:'sharing',control:true});
  return {term,serializer,sharing,sent,roles};
}
const source=client();await flush(source.term);
const output='\x1b[?1049h\x1b[2J\x1b[H'+Array.from({length:700},(_,i)=>'\x1b[32m第 '+i+' 行 │ 中文 café é 🙂\x1b[0m\r\n').join('');
source.sharing.output(frame(output,1));await flush(source.term);
source.sharing.message({type:'snapshot_request',nonce:'fixture',seq:1,cols:40,rows:24});await flush(source.term);
const snapshot=source.sent.find(message=>message.type==='snapshot');assert.ok(snapshot);assert.equal(snapshot.seq,1);assert.ok(snapshot.text.includes('中文'));
const viewer=client();await flush(viewer.term);
viewer.sharing.message({type:'replay_start',cols:40,rows:24});viewer.sharing.output(frame(snapshot.text,1,true));viewer.sharing.message({type:'replay_end',seq:1});viewer.sharing.message({type:'sharing',control:false});await flush(viewer.term);
assert.equal(context.TerminalView.history(viewer.term),context.TerminalView.history(source.term));
for(const index of [0,100,699]){
  const a=source.term.buffer.normal.getLine(index),b=viewer.term.buffer.normal.getLine(index);assert.equal(a.translateToString(true),b.translateToString(true));assert.equal(a.getCell(0).getFgColor(),b.getCell(0).getFgColor());
}
assert.equal(viewer.roles.at(-1).control,false);assert.equal(viewer.roles.at(-1).replaying,false);
// Apply a controller's size change in stream order before the next frame.
for(const client of [source,viewer]){client.sharing.message({type:'geometry',seq:2,cols:28,rows:18});client.sharing.output(frame('\x1b[2J\x1b[H手机接管后的中文🙂\r\n\x1b[31m红色界面',3));await flush(client.term);}
assert.equal(viewer.term.cols,28);assert.equal(viewer.term.rows,18);
assert.equal(context.TerminalView.history(viewer.term),context.TerminalView.history(source.term));
assert.equal(source.sharing.seq,3);assert.equal(viewer.sharing.seq,3);
assert.ok(!context.TerminalView.history(viewer.term).includes('\ufffd'));
assert.ok(viewer.sent.some(message=>message.type==='ack'));
source.term.dispose();viewer.term.dispose();
console.log('PASS shared terminal Unicode/color snapshot, ordered resize, replay and readonly role');
