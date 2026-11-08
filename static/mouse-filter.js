/* Keep mouse reporting local unless a window explicitly opts in. No dependencies. */
(function(root){
  'use strict';
  const mouseModes=new Set([9,1000,1001,1002,1003,1005,1006,1015,1016]);
  const trackingModes=new Set([9,1000,1002,1003]);
  const encodingModes=new Set([1005,1006,1015,1016]);
  const alternateModes=new Set([47,1047,1049]);
  class TerminalMouseFilter{
    constructor(remote=false,{keepScrollback=false}={}){this.remote=remote;this.keepScrollback=keepScrollback;this.sequence='';this.state='text';this.tracking=0;this.encoding=0;}
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
          else{if(char==='c')this.tracking=this.encoding=0;out+=this.sequence;this.sequence='';this.state=']PX^_'.includes(char)?'string':'text';}
          continue;
        }
        const code=char.charCodeAt(0);
        if(code>=0x40&&code<=0x7e){
          const match=this.sequence.match(/^\x1b\[\?([0-9;]+)([hl])$/);
          if(match){
            const values=match[1].split(';').map(Number);
            const enabled=match[2]==='h';
            for(const mode of values){
              if(trackingModes.has(mode))this.tracking=enabled?mode:0;
              if(encodingModes.has(mode))this.encoding=enabled?mode:0;
            }
            // screen owns the remote application's alternate buffer. Its outer
            // display must stay in xterm's normal buffer so local scrolling works.
            const kept=values.filter(mode=>(this.remote||!mouseModes.has(mode))&&!(this.keepScrollback&&alternateModes.has(mode)));
            if(kept.length)out+='\x1b[?'+kept.join(';')+match[2];
          }else out+=this.sequence;
          this.sequence='';this.state='text';
        }else if(this.sequence.length>512){out+=this.sequence;this.sequence='';this.state='text';}
      }
      return out;
    }
    setRemote(enabled){
      this.remote=enabled;
      return '\x1b[?9;1000;1001;1002;1003;1005;1006;1015;1016l'+(enabled?this.snapshotModes():'');
    }
    snapshotModes(){
      const modes=[this.tracking,this.encoding].filter(Boolean);
      return modes.length?'\x1b[?'+modes.join(';')+'h':'';
    }
    input(text){
      if(this.remote)return text;
      return text.replace(/\x1b\[<\d+;\d+;\d+[Mm]|\x1b\[M[\s\S]{3}|\x1b\[\d+;\d+;\d+M/g,'');
    }
  }
  root.TerminalMouseFilter=TerminalMouseFilter;
})(typeof window==='undefined'?globalThis:window);
