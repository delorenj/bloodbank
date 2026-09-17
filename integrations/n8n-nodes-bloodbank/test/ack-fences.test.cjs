const test=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const path=require('node:path');
for(const name of ['ticket-delegation','ticket-grooming'])test(`${name} prepare and cached recall honor managed/shadow deployment fences`,()=>{
 const workflows=JSON.parse(fs.readFileSync(path.join(__dirname,`../../n8n-workflows/${name}.v1.json`)));
 const nodes=workflows.flatMap(w=>w.nodes);const board='a8a12be1-b3ab-44f4-ab24-abe8829aeb72';let store={};
 function invoke(label,input,fenced){const code=nodes.find(n=>n.name===label).parameters.jsCode;return new Function('$env','$input','$getWorkflowStaticData','$',code)({KREBS_FENCED_BOARDS:JSON.stringify(fenced)}, {all:()=>input},()=>store,()=>({all:()=>[{json:{data:{ticket_id:'ticket'}}}]}));}
 const item={json:{invoked:true,boardId:board,correlationid:'cid'}};
 assert.equal(invoke('Ack — Prepare',[item],[]).length,1);assert(store.ack.cid);
 assert.equal(invoke('Ack — Recall',[{json:{correlationid:'cid'}}],[board]).length,0);assert(!store.ack.cid);
 assert.equal(invoke('Ack — Prepare',[item],[board]).length,0);assert(!store.ack.cid);
});
