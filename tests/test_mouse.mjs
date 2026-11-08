import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const context=vm.createContext({});
vm.runInContext(fs.readFileSync(new URL('../static/mouse-filter.js',import.meta.url),'utf8'),context);
const Filter=context.TerminalMouseFilter;
let cases=0;
for(const sequence of ['\x1b[?1003;1006h','\x1b[?9h','\x1b[?1001h','\x1b[?1002l']){
  for(let split=0;split<=sequence.length;split++){
    const filter=new Filter(false);
    assert.equal(filter.feed(sequence.slice(0,split))+filter.feed(sequence.slice(split)),'');cases++;
  }
}
const mixed='A\x1b[?1003;1049;1006hB';
for(let split=0;split<=mixed.length;split++){
  const filter=new Filter(false);
  assert.equal(filter.feed(mixed.slice(0,split))+filter.feed(mixed.slice(split)),'A\x1b[?1049hB');cases++;
}
const filter=new Filter(false);
assert.equal(filter.input('\x1b[<35;120;40M'),'');
assert.equal(filter.input('\x1b[M !!!'),'!');
assert.equal(filter.input('printf hello\r'),'printf hello\r');
assert.equal(filter.feed('\x1b]0;title\x1b[?1003h\x07'),'\x1b]0;title\x1b[?1003h\x07');
assert.equal(filter.feed('\x1bPkeep\x1b[?1003h\x1b\\'),'\x1bPkeep\x1b[?1003h\x1b\\');
const remote=new Filter(true);
assert.equal(remote.feed('\x1b[?1003;1006h'),'\x1b[?1003;1006h');
assert.equal(remote.input('\x1b[<35;120;40M'),'\x1b[<35;120;40M');
assert.ok(remote.setRemote(false).endsWith('l'));
assert.equal(remote.input('\x1b[<35;120;40M'),'');
for(const sequence of ['\x1b[?47h','\x1b[?47l','\x1b[?1047h','\x1b[?1049h','\x1b[?1049l','\x1b[?1003;1049;1006h']){
  for(let split=0;split<=sequence.length;split++){
    const local=new Filter(false,{keepScrollback:true});
    assert.equal(local.feed(sequence.slice(0,split))+local.feed(sequence.slice(split)),'');cases++;
  }
}
const scrolling=new Filter(true,{keepScrollback:true});
assert.equal(scrolling.feed('中文\x1b[?1049;1003;25h│🙂'),'中文\x1b[?1003;25h│🙂');
assert.equal(scrolling.feed('\x1b[?1049l\x1b[31m彩色\x1b[0m'),'\x1b[31m彩色\x1b[0m');
assert.equal(scrolling.feed('\x1b]0;\x1b[?1049h\x07'),'\x1b]0;\x1b[?1049h\x07');
const wheelOnly=new Filter(false,{keepScrollback:true});
assert.equal(wheelOnly.wheel(true,15,6,31,29),'');
assert.equal(wheelOnly.feed('\x1b[?1000;1006h'),'');
assert.equal(wheelOnly.wheel(true,15,6,31,29),'\x1b[<64;15;6M');
assert.equal(wheelOnly.wheel(false,112,36,90,70),'\x1b[<65;112;36M');
assert.equal(wheelOnly.input('\x1b[<35;15;6M'),''); // pointer motion stays local
assert.equal(wheelOnly.snapshotModes(),'\x1b[?1000;1006h');
wheelOnly.feed('\x1b[?1006l');
assert.equal(wheelOnly.wheel(true,15,6,31,29),'\x1b[M`/&');
assert.equal(wheelOnly.wheel(false,200,100,110,120),'\x1b[Ma\x7f\x7f');
wheelOnly.feed('\x1b[?1016h');
assert.equal(wheelOnly.wheel(false,15,6,31,29),'\x1b[<65;31;29M');
wheelOnly.feed('\x1bc');
assert.equal(wheelOnly.snapshotModes(),'');
wheelOnly.feed('\x1b[?9h');
assert.equal(wheelOnly.acceptsWheel(),false);
console.log('PASS mouse reporting filter: '+(cases+9)+' checks');
