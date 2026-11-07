/* Ordered terminal replay and parser-barrier snapshots for a shared PTY. */
(function(root){
  'use strict';
  class TerminalSharing {
    constructor(term,serializer,options){this.term=term;this.serializer=serializer;this.options=options;this.decoder=new TextDecoder();this.seq=0;this.replaying=true;this.role=null;}
    output(buffer){
      if(buffer.byteLength<9)throw new Error('画面数据不完整，请重新连接');
      const head=new DataView(buffer),snapshot=head.getUint8(0)===1,seq=head.getUint32(1)*4294967296+head.getUint32(5),raw=new Uint8Array(buffer,9);
      if(!snapshot)this.seq=seq;
      const text=this.options.filter().feed(this.decoder.decode(raw,{stream:true}));
      this.term.write(text,()=>this.options.send({type:'ack',bytes:raw.byteLength}));
    }
    message(data){
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
        this.seq=data.seq;this.term.write('',()=>{this.term.resize(data.cols,data.rows);this.options.geometry(data.cols,data.rows);});return true;
      }
      if(data.type==='snapshot_request'){
        if(data.seq!==this.seq)return true;
        this.term.write('',()=>{
          if(this.term.cols!==data.cols||this.term.rows!==data.rows)return;
          for(const scrollback of [1000,0]){
            const reply={type:'snapshot',nonce:data.nonce,seq:data.seq,cols:data.cols,rows:data.rows,text:this.serializer.serialize({scrollback,excludeAltBuffer:true})};
            if(new TextEncoder().encode(JSON.stringify(reply)).length<900000){this.options.send(reply);break;}
          }
        });return true;
      }
      return false;
    }
  }
  root.TerminalSharing=TerminalSharing;
})(typeof window==='undefined'?globalThis:window);
