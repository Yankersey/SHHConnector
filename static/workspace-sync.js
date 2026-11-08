/* One backend workspace, independently attached desktop and phone views. */
(function(root){
  'use strict';
  class WorkspaceSync {
    constructor(options){this.options=options;this.busy=false;this.attempts=new Map();this.paused=new Set();}
    pause(id){this.paused.add(id);}
    resume(id){this.paused.delete(id);this.attempts.delete(id);}
    wake(){this.attempts.clear();}
    async sync(data){
      if(this.busy)return false;
      this.busy=true;
      try{
        const records=new Map(data.sessions.filter(record=>record.state!=='ended').map(record=>[record.id,record]));
        this.options.apply(data);
        for(const [id,view] of this.options.views()){
          if(!records.has(id)){this.options.remove(view);this.attempts.delete(id);this.paused.delete(id);}
        }
        for(const record of records.values()){
          if(!this.paused.has(record.id)&&(record.layout?.visible||data.shared?.[record.id])&&!this.options.views().has(record.id))await this.options.open(record);
        }
        for(const [id,view] of this.options.views()){
          const live=data.shared?.[id];
          if(!live||this.options.connected(view)||this.paused.has(id))continue;
          if(this.attempts.get(id)===live.connection_id)continue;
          this.attempts.set(id,live.connection_id);
          // This operation only joins an existing PTY. The backend must reject
          // it if that PTY disappears, rather than silently starting new SSH.
          await this.options.observe(view,live);
        }
        this.options.render();
        return true;
      }finally{this.busy=false;}
    }
  }
  root.WorkspaceSync=WorkspaceSync;
})(typeof window==='undefined'?globalThis:window);
