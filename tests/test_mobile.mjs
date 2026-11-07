import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const elements=new Map();
function node(){const classes=new Set();return {children:[],dataset:{},style:{},classList:{add:n=>classes.add(n),remove:n=>classes.delete(n),contains:n=>classes.has(n),toggle(n,on){const next=on??!classes.has(n);next?classes.add(n):classes.delete(n);return next;}},setAttribute(k,v){this[k]=v;},replaceChildren(){this.children=[];},append(n){this.children.push(n);},addEventListener(name,fn){this[name]=fn;},querySelectorAll(){return this.children;}};}
for(const id of ['#mobile-menu','#mobile-scrim','#mobile-terminal','#mobile-new','#mobile-keys'])elements.set(id,node());
const rootStyle={setProperty(k,v){this[k]=v;},removeProperty(k){delete this[k];}};
const document={body:node(),documentElement:{style:rootStyle},querySelector:s=>elements.get(s),createElement:node};
const media={matches:true,addEventListener(name,fn){this[name]=fn;}};
let resized=0,selected='',sent='',keyboard=0,created=0;
const viewport={height:620,addEventListener(name,fn){this[name]=fn;}};
const context=vm.createContext({document,matchMedia:()=>media,visualViewport:viewport,innerHeight:844,addEventListener(){}});
vm.runInContext(fs.readFileSync(new URL('../static/mobile.js',import.meta.url),'utf8'),context);
const view=context.ScreenDeskMobile;
assert.equal(view.keySequence('Ctrl+C'),'\x03');assert.equal(view.keySequence('Ctrl+D'),'\x04');assert.equal(view.keySequence('Ctrl+A'),'\x01');assert.equal(view.keySequence('Tab'),'\t');assert.equal(view.keySequence('Escape'),'\x1b');
assert.equal(view.keySequence('Up'),'\x1b[A');assert.equal(view.keySequence('Left',true),'\x1bOD');assert.equal(view.keySequence('unknown'),'');
const mobile=view.install({select:id=>{selected=id;},newWindow:()=>created++,key:key=>{sent=key;},keyboard:()=>keyboard++,resize:()=>resized++});
assert.equal(rootStyle['--app-height'],'620px');assert.ok(document.body.classList.contains('mobile-keyboard-open'));
elements.get('#mobile-menu').onclick();assert.ok(document.body.classList.contains('mobile-menu-open'));mobile.closeMenu();assert.ok(!document.body.classList.contains('mobile-menu-open'));
const windows=[{record:{id:'one',name:'中文一'},element:node(),geometry:Object.freeze({x:90,width:700})},{record:{id:'two',name:'中文二'},element:node(),geometry:Object.freeze({x:800,width:900})}];
mobile.render(windows,'two');assert.ok(windows[1].element.classList.contains('mobile-active'));assert.ok(!windows[0].element.classList.contains('mobile-active'));assert.equal(elements.get('#mobile-terminal').value,'two');
elements.get('#mobile-terminal').value='one';elements.get('#mobile-terminal').onchange();assert.equal(selected,'one');
elements.get('#mobile-new').onclick();assert.equal(created,1);
elements.get('#mobile-keys').click({target:{closest:()=>({dataset:{key:'Ctrl+C'}})}});assert.equal(sent,'Ctrl+C');
elements.get('#mobile-keys').click({target:{closest:()=>({dataset:{}})}});assert.equal(keyboard,1);
mobile.render(windows,'unknown');assert.ok(windows[0].element.classList.contains('mobile-active'));
media.matches=false;media.change();assert.ok(!('--app-height' in rootStyle));assert.equal(windows[0].geometry.width,700);assert.ok(resized>=2);
console.log('PASS mobile terminal keys, switching, keyboard viewport and desktop layout preservation');
