/* Keep mouse reporting local unless a window explicitly opts in. No dependencies. */
(function(root){
  'use strict';
  const mouseModes=new Set([9,1000,1001,1002,1003,1005,1006,1015,1016]);
  class TerminalMouseFilter{
    constructor(remote=false){this.remote=remote;this.sequence='';this.state='text';this.requested=new Map();}
    feed(text){
      let out='';
      for(const char of text){
        if(this.state==='string'){
          out+=char;
          if(char==='\x07')this.state='text';
          else if(char==='\x1b')this.state='stringEscape';
          continue;
        }
        if(this.state==='stringEscape'){out+=char;this.state=char==='\\'?'text':'string';continue;}
        if(this.state==='text'){
          if(char==='\x1b'){this.sequence=char;this.state='escape';}
          else out+=char;
          continue;
        }
        this.sequence+=char;
        if(this.state==='escape'){
          if(char==='[')this.state='csi';
          else{out+=this.sequence;this.sequence='';this.state=']PX^_'.includes(char)?'string':'text';}
          continue;
        }
        const code=char.charCodeAt(0);
        if(code>=0x40&&code<=0x7e){
          const match=this.sequence.match(/^\x1b\[\?([0-9;]+)([hl])$/);
          if(match){
            const values=match[1].split(';').map(Number);
            for(const mode of values)if(mouseModes.has(mode))this.requested.set(mode,match[2]==='h');
            const kept=this.remote?values:values.filter(mode=>!mouseModes.has(mode));
            if(kept.length)out+='\x1b[?'+kept.join(';')+match[2];
          }else out+=this.sequence;
          this.sequence='';this.state='text';
        }else if(this.sequence.length>512){out+=this.sequence;this.sequence='';this.state='text';}
      }
      return out;
    }
    setRemote(enabled){
      this.remote=enabled;
      let out='\x1b[?9;1000;1001;1002;1003;1005;1006;1015;1016l';
      if(enabled)for(const [mode,on] of this.requested)if(on)out+='\x1b[?'+mode+'h';
      return out;
    }
    input(text){
      if(this.remote)return text;
      return text.replace(/\x1b\[<\d+;\d+;\d+[Mm]|\x1b\[M[\s\S]{3}|\x1b\[\d+;\d+;\d+M/g,'');
    }
  }
  root.TerminalMouseFilter=TerminalMouseFilter;
})(typeof window==='undefined'?globalThis:window);
