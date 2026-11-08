/* Ordered terminal replay and parser-barrier snapshots for a shared PTY. */
(function(root){
  'use strict';
  class TerminalSharing {
    constructor(term,serializer,options){this.term=term;this.serializer=serializer;this.options=options;this.decoder=new TextDecoder();this.seq=0;this.replaying=true;this.role=null;this.resizing=null;}
    output(buffer){
      if(this.resizing){this.resizing.then(()=>this.output(buffer));return;}
      if(buffer.byteLength<9)throw new Error('画面数据不完整，请重新连接');
      const head=new DataView(buffer),snapshot=head.getUint8(0)===1,seq=head.getUint32(1)*4294967296+head.getUint32(5),raw=new Uint8Array(buffer,9);
      if(!snapshot)this.seq=seq;
      const text=this.options.filter().feed(this.decoder.decode(raw,{stream:true}));
      this.term.write(text,()=>this.options.send({type:'ack',bytes:raw.byteLength}));
    }
    snapshot(scrollback){
      const text=this.serializer.serialize({scrollback,excludeAltBuffer:true});
      const region=/\x1b\[(\d+);\d+r$/.exec(text);
      const origin=this.term.modes.originMode;
      if(!region&&!origin)return text;
      // Restoring margins or origin mode homes the cursor. Restore the
      // position after the addon has restored those settings.
      const {cursorX,cursorY}=this.term.buffer.active;
      const row=cursorY+1-(origin&&region?Number(region[1])-1:0);
      return text+'\x1b['+Math.max(1,row)+';'+Math.min(this.term.cols,cursorX+1)+'H';
    }
    message(data){
      if(this.resizing&&['replay_start','replay_end','sharing','geometry','snapshot_request'].includes(data.type)){
        this.resizing.then(()=>this.message(data));return true;
      }
      if(data.type==='replay_start'){
        this.replaying=true;this.seq=0;this.decoder=new TextDecoder();this.options.reset();this.term.reset();this.term.resize(data.cols,data.rows);this.options.geometry(data.cols,data.rows);return true;
      }
      if(data.type==='replay_end'){
        this.seq=data.seq;this.term.write('',()=>{this.replaying=false;if(this.role)this.options.role(this.role,false);});return true;
      }
      if(data.type==='sharing'){
        this.role=data;this.options.role(data,this.replaying);return true;
      }
      if(data.type==='geometry'){
        // xterm 6 flushes its write queue in resize(). Leave the parser
        // callback before resizing and hold frames for the new grid until then.
        this.seq=data.seq;
        this.resizing=new Promise(resolve=>this.term.write('',resolve)).then(()=>{
          this.resizing=null;
          this.term.resize(data.cols,data.rows);
          this.options.geometry(data.cols,data.rows);
        });
        return true;
      }
      if(data.type==='snapshot_request'){
        if(data.seq!==this.seq)return true;
        const mouseModes=this.options.filter().snapshotModes();
        this.term.write('',()=>{
          if(this.term.cols!==data.cols||this.term.rows!==data.rows)return;
          for(const scrollback of [1000,0]){
            const reply={type:'snapshot',nonce:data.nonce,seq:data.seq,cols:data.cols,rows:data.rows,text:this.snapshot(scrollback)+mouseModes};
            if(new TextEncoder().encode(JSON.stringify(reply)).length<900000){this.options.send(reply);break;}
          }
        });return true;
      }
      return false;
    }
  }
  root.TerminalSharing=TerminalSharing;
})(typeof window==='undefined'?globalThis:window);
