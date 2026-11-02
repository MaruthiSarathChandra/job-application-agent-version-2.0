// Dependency-free DOM harness: executes the real dashboard script and interactions.
// This checks behavior, not browser rendering or layout.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const html=fs.readFileSync(path.join(__dirname,'../agent/web/index.html'),'utf8');
class Element {
 constructor(tag='div'){this.tagName=tag;this.children=[];this.style={};this.value='';this.textContent='';this.hidden=false;this.disabled=false;this.attributes={};}
 append(...nodes){this.children.push(...nodes)}
 replaceChildren(...nodes){this.children=nodes}
 setAttribute(k,v){this.attributes[k]=v}
 addEventListener(k,v){this['on'+k]=v}
 showModal(){this.open=true} close(){this.open=false} focus(){}
}
const nodes=new Map([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],new Element()]));
for(const [id,value] of Object.entries({'region':'US','posted-window':'24','match-threshold':'70','board-provider':'all','scan-interval':'15','prompt':'Engineer'}))nodes.get(id).value=value;
const job={job_id:'job1',id:'job1',title:'Engineer',company:'Example',source:'lever:example',location:'US',url:'https://example.com/job',description:'Build Java APIs',qualification:{status:'matched',reasons:[]}};
const payload={job,job_url:job.url,resume:{artifact_id:'artifact1'},submission:{id:'receipt1',submitted_at:'2026-11-02T00:00:00Z',confirmation_url:'https://example.com/confirmation'}};
let state={build_version:'fixture',jobs:[job],latest_jobs:[job],queue:[{sequence:1,job_id:'job1',status:'resume_ready',snapshot:JSON.stringify(job)}],runs:[],activity:[],sources:[],drafts:[],applications:[{sequence:1,job_id:'job1',resume_artifact_id:'artifact1',status:'ready_for_application',payload:JSON.stringify(payload)}],applied_history:[],source_scans:[],scanner:{enabled:true,interval_minutes:15},worker:{online:false},searched_count:55,board_options:[{id:'lever',name:'Lever',available:true},{id:'linkedin',name:'LinkedIn',available:false,reason:'Not implemented'}]};
let requests=[];
const context={document:{getElementById:id=>{assert(nodes.has(id),'Missing DOM element: '+id);return nodes.get(id)},createElement:tag=>new Element(tag)},localStorage:{getItem:()=>null,setItem(){}},window:{},URL,console,setInterval(){},fetch:async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>url==='/api/state'?state:{promoted:1}}}};
vm.createContext(context);vm.runInContext(html.split('<script>')[1].split('</script>')[0],context);
(async()=>{
 await vm.runInContext('refresh()',context);
 assert.equal(nodes.get('connection').textContent,'Connected');
 assert(nodes.get('board-provider').children.find(x=>x.value==='linkedin').disabled);
 nodes.get('queuetab').onclick();assert.equal(nodes.get('rows').children.length,0,'Completed resume must leave active queue');
 state.drafts=[{queue_order:1,result:JSON.stringify({sections:[],missing_requirements:[],note:'Completed draft'})}];
 await vm.runInContext('refresh()',context);
 nodes.get('drafttab').onclick();
 assert.equal(nodes.get('rows').children.length,0,'Completed DOCX must not remain in unfinished GPT draft view');
 nodes.get('applicationtab').onclick();assert.equal(nodes.get('rows').children.length,1);
 assert.equal(nodes.get('stage-agent3').textContent,'0');
 state.pipeline_counts={ready_for_application:1,confirmed_submissions:0};
 state.agent3_tasks=[{application_order:1,status:'awaiting_browser_adapter',last_error:null}];
 await vm.runInContext('refresh()',context);
 assert.equal(nodes.get('stage-agent3').textContent,1);
 assert.equal(nodes.get('rows').children.length,1);
 nodes.get('appliedtab').onclick();assert.equal(nodes.get('rows').children.length,0,'Generated resume is not applied');
 state={...state,applications:[{...state.applications[0],status:'submitted'}],applied_history:[{job_id:'job1',artifact_id:'artifact1',payload:JSON.stringify(payload)}]};
 await vm.runInContext('refresh()',context);assert.equal(nodes.get('rows').children.length,1);
 nodes.get('board-provider').value='lever';nodes.get('required-years').value='2';
 await nodes.get('search').onsubmit({preventDefault(){}});
 const sent=JSON.parse(requests.find(x=>x.url==='/api/runs').options.body);
 assert.equal(sent.board_provider,'lever');assert.equal(sent.max_required_years,2);
 assert.equal(sent.min_match_percent,70);assert.equal(sent.batch_size,55);
 assert.deepEqual(sent.country_codes,['US']);assert.equal(sent.time_basis,'published');
 console.log('Dashboard JS interaction checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
