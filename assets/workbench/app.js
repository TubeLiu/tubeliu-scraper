/* Native, read-only client. All displayed progress comes from the workbench API. */
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const STATUS = {pending:"待处理",running:"处理中",done:"已完成",completed:"已完成",blocked:"待确认",error:"异常",failed:"失败",cancelled:"已取消",unknown:"尚未记录"};
  const PHASE = {init:"初始化",hash:"测量 ROM 哈希",deployment_plan:"准备备份与部署计划",deployment_preflight:"部署前核对",rollback:"回滚",awaiting_media_visual_qa:"等待媒体与目视验收",awaiting_scoped_completion:"等待封存本次范围",history_import:"历史验收导入",snapshot:"原始快照",prepare:"准备修复",normalize:"修正引用",media_qa:"媒体验证",awaiting_visual_qa:"等待目视验收",inventory:"资源盘点",discover:"发现资源",discovery:"发现资源",audit:"资料检查",scan:"资源扫描",backup:"备份",identify:"游戏识别",identification:"游戏识别",match:"匹配游戏",scrape:"刮削资料",scraping:"刮削资料",metadata:"补齐资料",translate:"中文化",translation:"中文化",media:"整理媒体",download:"下载媒体",videos:"视频处理",video:"视频处理",repair:"修复资料",merge:"合并资料",validate:"验证资源",verification:"验证资源",verify:"验证资源",deploy:"写入设备",sync:"同步设备",transfer:"同步设备",reload:"重新加载",report:"生成报告",complete:"完成",completed:"完成",cleanup:"清理暂存"};
  const MEDIA = [
    {key:"covers",label:"封面",short:"封",legacy:"image",icon:'<rect x="3" y="2" width="10" height="12" rx="1"/><path d="M5 10l2-3 2 2 2-3"/>'},
    {key:"screenshots",label:"截图",short:"截",legacy:"thumbnail",icon:'<rect x="2" y="3" width="12" height="10" rx="1"/><path d="M3 11l4-4 3 3 3-2"/><circle cx="11" cy="6" r=".6"/>'},
    {key:"titlescreens",label:"标题画面",short:"题",legacy:"titlescreen",icon:'<rect x="2" y="3" width="12" height="10" rx="1"/><path d="M5 6h6M8 6v4"/>'},
    {key:"marquees",label:"标识",short:"标",legacy:"marquee",icon:'<path d="M2 11V4l3 5 3-5v7M10 4h4M10 7h3M10 11h4"/>'},
    {key:"miximages",label:"组合图",short:"组",legacy:"fanart",icon:'<rect x="2" y="3" width="8" height="8" rx="1"/><rect x="6" y="6" width="8" height="7" rx="1"/>'},
    {key:"videos",label:"视频",short:"视",legacy:"video",icon:'<rect x="2" y="3" width="12" height="10" rx="1"/><path d="M6 5.5l4 2.5-4 2.5z"/>'}
  ];
  const VIDEO = {gameplay_video:"实机视频",screenshot_preview:"截图预览",original_title_art_preview:"原始标题图预览",gameplay:"实机视频",scraped:"来源视频",original:"原始视频",screenshot:"截图预览",screenshots:"截图预览",slideshow:"截图预览",preview:"预览视频",generated:"生成预览",title:"标题预览",titlescreen:"标题预览",unknown:"类型未知"};
  const FIELD = {name:"游戏名称",desc:"游戏简介",description:"游戏简介",developer:"开发商",publisher:"发行商",genre:"类型",players:"玩家人数",releasedate:"发行日期",rating:"评分",path:"ES-DE 游戏路径",rom_path:"ROM 原始路径",original_path:"原始路径",relative_path:"相对路径",size:"文件大小",rom_size:"ROM 文件大小",rom_size_bytes:"ROM 文件大小",sha256:"SHA-256",md5:"MD5",crc32:"CRC32",filename:"文件名",file:"文件",system:"平台",favorite:"收藏",hidden:"隐藏",playcount:"游玩次数",playtime:"游玩时间",lastplayed:"最近游玩",altemulator:"指定模拟器",sortname:"排序名称",controller:"控制器",controllers:"控制器",broken:"标记损坏",kidgame:"儿童游戏",nogamecount:"不计入游戏数量",nomultiscrape:"不参与重复刮削",completed:"已通关",gametime:"游戏时长",language:"语言",languages:"语言",region:"地区",regions:"地区",date_precision:"日期精度",source:"来源",source_urls:"来源链接",gamelist:"游戏列表",gamelist_path:"游戏列表文件",metadata:"游戏资料",history:"游玩记录",extra:"其他字段",rom:"ROM 文件信息",scan:"扫描信息",native_fields:"原生字段",original_fields:"原始字段",attributes:"XML 属性",text:"内容",tag:"字段",value:"内容",children:"子字段",true:"是",false:"否"};
  const OPTIONAL_MEDIA = {backcovers:"封底",backcover:"封底",physicalmedia:"实体介质","3dboxes":"立体盒图","3dboxfronts":"立体盒图","3dboxbacks":"立体盒背面",fanart:"背景美术",manuals:"游戏手册",manual:"游戏手册",boxfront:"盒面",boxback:"盒背",spines:"书脊",bezels:"边框"};
  let detailRequest=0, detailView=null, lightboxReturn=null, lightboxItems=[], lightboxIndex=0;
  const terminal = new Set(["done","completed","cancelled","error","failed"]);
  const app = {runId:"",runs:[],state:null,jobs:[],page:1,limit:50,total:0,events:[],lastEvent:0,stream:null,transport:"connecting",contactAt:0,generation:0,jobRequest:0,eventRequest:0,selected:null,selectedFresh:false,detailFingerprint:"",phaseFingerprint:"",jobsStale:null,authFailed:false,loadedOnce:false,refreshTimer:null,searchTimer:null,returnFocus:null};
  let token = "";
  try {
    const params = new URLSearchParams(location.search);
    token = params.get("token") || sessionStorage.getItem("esde-workbench-token") || "";
    if (params.has("token")) {
      if (token) sessionStorage.setItem("esde-workbench-token", token);
      params.delete("token");
      history.replaceState(null,"",location.pathname + (params.size ? "?" + params.toString() : "") + location.hash);
    }
  } catch (_) {
    // In a restricted browser session, keep the token in memory for this page only.
    token = new URLSearchParams(location.search).get("token") || "";
    const clean = new URL(location.href); clean.searchParams.delete("token");
    try { history.replaceState(null,"",clean.pathname + clean.search + clean.hash); } catch (_) {}
  }
  function node(tag, className, text) { const e=document.createElement(tag); if(className)e.className=className; if(text!==undefined)e.textContent=String(text); return e; }
  function count(value) { const n=Number(value); return Number.isFinite(n)&&n>=0?n:0; }
  function num(value) { return count(value).toLocaleString("zh-CN"); }
  function list(value) { return Array.isArray(value)?value:value&&typeof value==="object"?Object.entries(value).map(([key,val])=>({key,value:val})):[]; }
  function valueText(value) { if(value===null||value===undefined)return ""; return typeof value==="object"?JSON.stringify(value):String(value); }
  function stamp(value, short=false) {
    const d=new Date(value); if(!value||!Number.isFinite(d.getTime()))return "—";
    return d.toLocaleString("zh-CN", short?{hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}:{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
  }
  function titleDate(e,value,short=false) { e.textContent=stamp(value,short); if(value)e.title=String(value); if(e.tagName==="TIME"&&value)e.dateTime=String(value); }
  function age(value) { const n=Date.parse(value); return Number.isFinite(n)?Math.max(0,Date.now()-n):null; }
  function statusClass(value) { return Object.prototype.hasOwnProperty.call(STATUS,value)?value:"pending"; }
  function statusName(value) { return STATUS[value]||value||"尚未记录"; }
  function apiURL(path,params={}) { const u=new URL(path,location.origin); for(const [k,v] of Object.entries(params))if(v!==undefined&&v!==null&&v!=="")u.searchParams.set(k,String(v)); if(token)u.searchParams.set("token",token); return u.href; }
  async function get(path,params={}) {
    const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),10000);
    try {
      const response=await fetch(apiURL(path,params),{cache:"no-store",credentials:"same-origin",signal:controller.signal,referrerPolicy:"no-referrer"});
      if(response.status===401||response.status===403){app.authFailed=true;setTransport("unauthorized");closeStream();const e=new Error("访问凭据失效，请使用工作台启动时提供的访问链接重新打开。");e.auth=true;throw e;}
      if(!response.ok){const e=new Error(response.status===404?"任务或工作台接口不存在。":"无法读取数据（HTTP "+response.status+"）。");e.status=response.status;throw e;}
      const data=await response.json(); app.contactAt=Date.now(); return data;
    } catch(e) {
      if(!e.auth&&!e.status){setTransport(navigator.onLine?"offline":"network-offline");throw new Error("工作台连接中断，正在等待恢复。以下数据可能已经过期。");}
      throw e;
    } finally {clearTimeout(timeout);}
  }
  function runPath(suffix="") { return "/api/runs/"+encodeURIComponent(app.runId)+suffix; }
  function showNotice(message,error=false) { $("notice").hidden=!message; $("notice").textContent=message; $("notice").classList.toggle("error",error); }
  function setTransport(mode) {
    app.transport=mode;
    const labels={live:"实时连接",connecting:"正在连接",reconnecting:"实时通道重连中",offline:"工作台离线", "network-offline":"网络离线",unauthorized:"访问已失效",idle:"工作台已连接"};
    $("transport").className="connection-pill "+(["live","idle"].includes(mode)?"is-live":["offline","network-offline","unauthorized"].includes(mode)?"is-offline":"is-waiting");
    $("transport").lastElementChild.textContent=labels[mode]||"正在连接";
    renderFreshness();
  }
  function effectiveStatus(run) {
    if(run.status==="running") {
      if(["offline","network-offline","unauthorized","reconnecting"].includes(app.transport))return {label:"状态待确认",css:"stale"};
      if(age(run.updated_at)!==null&&age(run.updated_at)>300000)return {label:"等待新进度",css:"stale"};
    }
    return {label:statusName(run.status),css:statusClass(run.status)};
  }
  function renderFreshness() {
    const run=app.state&&app.state.run;
    if(app.authFailed){showNotice("访问凭据失效，请使用工作台启动时提供的访问链接重新打开。",true);}
    else if(["offline","network-offline"].includes(app.transport)){showNotice("工作台连接已中断，正在等待恢复。下方保留的是最后一次读取的数据；当前任务状态待确认。",true);}
    else if(app.transport==="reconnecting"){showNotice("实时通道已断开，正在重新连接。下方显示最后已知数据，工作台会继续尝试读取任务状态。");}
    else if(run&&run.status==="running"&&age(run.updated_at)>300000){showNotice("任务超过 5 分钟没有新进度。下方为最后已知数据，请结合设备连接状态与任务日志确认进展。");}
    else if(run&&run.connection&&run.connection.mode==="historical_receipt"){showNotice("这是原任务的历史验收记录。本次未连接设备重新扫描，当前设备上的资源可能已有变化。");}
    else {showNotice("");}
    if(run){const s=effectiveStatus(run); $("run-status").textContent=s.label; $("run-status").className="status-badge status-"+s.css; $("metric-running-note").textContent=s.css==="stale"?"最后记录，当前状态待确认":"正在处理的游戏"; $("run-content").classList.toggle("run-content-stale",s.css==="stale");renderPhases(app.state.phases||[],run);if(app.jobsStale!==(s.css==="stale"))renderJobs();}
    if(!$("detail-drawer").hidden&&app.selected)renderDetail();
    $("events-status").textContent=app.transport==="live"?"随任务实时更新":app.transport==="idle"?"定时读取":"显示最后已知记录";
  }
  function empty(title,message) { $("run-content").hidden=true; $("empty-state").hidden=false; $("empty-title").textContent=title; $("empty-message").textContent=message; }
  function renderRunPicker() {
    const select=$("run-select"), frag=document.createDocumentFragment();
    if(!app.runs.length){const option=node("option","","暂无任务");option.value="";frag.append(option);}
    for(const item of app.runs){const run=item.run||item;const option=node("option","",(run.title||run.id)+" · "+statusName(run.status));option.value=run.id;frag.append(option);}
    select.replaceChildren(frag); select.value=app.runId; select.disabled=!app.runs.length;
  }
  async function loadRuns(initial=false) {
    try {
      const data=await get("/api/runs"); app.runs=Array.isArray(data.items)?data.items:[];
      if(!app.runs.length){closeStream();app.runId="";app.state=null;renderRunPicker();setTransport("idle");empty("还没有任务","启动资源整理任务后，真实进度会出现在这里。工作台只显示已记录的数据。");return;}
      const ids=app.runs.map(x=>(x.run||x).id);
      if(!app.runId||!ids.includes(app.runId)) {
        let stored="";try{stored=sessionStorage.getItem("esde-workbench-run")||"";}catch(_){}
        const requested=new URLSearchParams(location.search).get("run")||"";
        const active=app.runs.find(x=>(x.run||x).status==="running");
        const selected=ids.includes(requested)?requested:ids.includes(stored)?stored:active?(active.run||active).id:ids[0];
        await selectRun(selected);
      }
      renderRunPicker();
      if(app.transport==="offline"||app.transport==="network-offline"){setTransport(app.stream&&app.stream.readyState===1?"live":"reconnecting");}
    } catch(e) { if(initial&&!app.state){empty(e.auth?"访问凭据失效":"暂时无法连接",e.message);} }
  }
  function closeStream(){if(app.stream){app.stream.close();app.stream=null;}if(app.refreshTimer){clearTimeout(app.refreshTimer);app.refreshTimer=null;}}
  async function selectRun(id) {
    closeStream(); closeDetail(); ++app.generation; ++app.jobRequest; ++app.eventRequest;
    app.runId=id;app.state=null;app.jobs=[];app.events=[];app.lastEvent=0;app.page=1;app.total=0;app.loadedOnce=false;app.authFailed=false;app.phaseFingerprint="";app.jobsStale=null;
    try{sessionStorage.setItem("esde-workbench-run",id);}catch(_){}
    $("system-filter").replaceChildren(Object.assign(node("option","","全部平台"),{value:""}));
    $("search").value="";$("status-filter").value="";$("run-content").hidden=true;$("empty-state").hidden=false;
    $("empty-title").textContent="正在读取任务";$("empty-message").textContent="读取已保存的进度、游戏记录与设备状态…";setTransport("connecting");renderRunPicker();
    const generation=app.generation;
    try {
      const state=await get(runPath()); if(generation!==app.generation)return;
      applyState(state);await Promise.all([loadJobs(true),loadEvents(true)]);if(generation!==app.generation)return;
      connectStream(generation); app.loadedOnce=true;
    } catch(e) { if(generation===app.generation){empty(e.auth?"访问凭据失效":"任务读取失败",e.message); if(e.status===404)loadRuns();} }
  }
  function applyState(data) {
    if(!data||!data.run||data.run.id!==app.runId)return;
    app.state=data; $("empty-state").hidden=true;$("run-content").hidden=false;
    const run=data.run,summary=data.summary||{};
    $("run-title").textContent=run.title||run.id;$("run-id").textContent=run.id;$("run-message").textContent=run.message||"等待任务记录下一步进展。";
    titleDate($("created-at"),run.created_at);titleDate($("updated-at"),run.updated_at);
    renderDevice(run.connection||{});
    $("metric-total").textContent=num(summary.total);$("metric-done").textContent=num(summary.done);$("metric-running").textContent=num(summary.running);
    $("metric-blocked").textContent=num(summary.blocked);$("metric-error").textContent=num(summary.error);$("metric-missing").textContent=num(summary.missing_media);
    $("metric-unknown").textContent="未知资料 "+num(summary.unknown)+" 款";$("metric-systems").textContent=Object.keys(summary.systems||{}).length+" 平台 · "+num(summary.pending)+" 待处理";
    $("metric-done-note").textContent=summary.total?Math.round(count(summary.done)/count(summary.total)*100)+"% 的游戏已完成":"已完成的游戏";
    const prev=$("system-filter").value,fragment=document.createDocumentFragment(),option=node("option","","全部平台");option.value="";fragment.append(option);
    for(const [system,value] of Object.entries(summary.systems||{}).sort((a,b)=>a[0].localeCompare(b[0]))){const o=node("option","",(system||"未分类")+" · "+num(typeof value==="object"?value.total:value));o.value=system;fragment.append(o);}
    $("system-filter").replaceChildren(fragment);$("system-filter").value=prev;
    renderPhases(data.phases||[],run);renderVideos(summary.video_kinds||{});renderFreshness();
  }
  function renderDevice(connection) {
    if(connection.mode==="historical_receipt"){
      $("device-status").textContent="历史验收";$("device-dot").className="dot neutral";
      $("device-name").textContent="本次未连接设备复核";$("device-message").textContent="显示原任务的已验证记录。";return;
    }
    const status=String(connection.status||"unknown").toLowerCase();
    const good=["device","connected","online","authorized","ready"].includes(status),bad=["offline","disconnected","error","missing","not_found"].includes(status),warning=["unauthorized","waiting","connecting","reconnecting"].includes(status);
    const label={device:"已连接",connected:"已连接",online:"已连接",authorized:"已授权",ready:"已就绪",offline:"设备离线",disconnected:"设备已断开",error:"连接异常",missing:"未找到设备",not_found:"未找到设备",unauthorized:"等待 USB 调试授权",waiting:"等待连接",connecting:"正在连接",reconnecting:"正在重连",unknown:"尚未记录",local:"本地资源"};
    $("device-status").textContent=label[status]||statusName(status);$("device-dot").className="dot "+(good?"good":bad?"bad":warning?"warning":"neutral");
    const model=connection.model||connection.name||connection.device||connection.serial||connection.type;
    $("device-name").textContent=typeof model==="object"?valueText(model):model||"设备信息等待更新";
    $("device-message").textContent=connection.message||connection.detail|| (connection.updated_at?"最近检查 "+stamp(connection.updated_at):"连接状态来自任务记录。");
  }
  function renderPhases(phases,run) {
    const stale=effectiveStatus(run).css==="stale",fingerprint=JSON.stringify([phases,run.phase,run.status,stale]);if(app.phaseFingerprint===fingerprint)return;app.phaseFingerprint=fingerprint;const fragment=document.createDocumentFragment();$("phase-empty").hidden=phases.length>0;$("current-phase").textContent=run.phase?(stale?"最后阶段 · ":"当前 · ")+(PHASE[run.phase]||run.phase):"";
    phases.forEach((phase,index)=>{
      const current=phase.phase===run.phase&&!terminal.has(run.status)&&!stale,entry=node("li","phase"+(current?" is-current":"")),top=node("div","phase-top"),name=node("div","phase-name");
      name.append(node("span","phase-index",String(index+1).padStart(2,"0")),document.createTextNode(PHASE[phase.phase]||phase.phase));const status=node("span","phase-status",stale&&phase.status==="running"?"状态待确认":statusName(phase.status));if(stale&&phase.status==="running")status.title="最后记录：处理中";top.append(name,status);entry.append(top);
      if(phase.message)entry.append(node("p","phase-description",phase.message));
      if(phase.total!==null&&phase.total!==undefined&&phase.completed!==null&&phase.completed!==undefined){const total=count(phase.total),done=count(phase.completed),percent=total?Math.min(100,Math.round(done/total*100)):0;const bar=node("div","phase-progress"),fill=node("div");fill.style.width=percent+"%";bar.append(fill);bar.setAttribute("role","progressbar");bar.setAttribute("aria-label",PHASE[phase.phase]||phase.phase);bar.setAttribute("aria-valuemin","0");bar.setAttribute("aria-valuemax",String(total));bar.setAttribute("aria-valuenow",String(done));entry.append(bar,node("p","phase-count",num(done)+" / "+num(total)+(total?" · "+percent+"%":"")));}
      else entry.append(node("p","phase-count","尚未提供数量进度"));fragment.append(entry);
    });$("phases").replaceChildren(fragment);
  }
  function renderVideos(kinds) {
    const text=Object.entries(kinds).filter(([,n])=>count(n)>0).map(([kind,n])=>(VIDEO[kind]||kind||"类型未记录")+" "+num(n)).join(" · ");
    $("video-note").textContent=text?"视频 · "+text:"视频类型等待记录";
  }
  function mediaState(job,media) {
    const all=job.media||{},missing=list(job.missing).map(x=>typeof x==="string"?x:x.key||x.type||""),value=all[media.key]!==undefined?all[media.key]:all[media.legacy];
    const absent=missing.includes(media.key)||missing.includes(media.legacy);
    let state=absent?"missing":"unrecorded";
    if(!absent&&value!==undefined&&value!==null&&value!==false&&value!==""){
      if(Array.isArray(value)){state=value.some(v=>v&&(v.present||v.exists||v.path||previewOf(v)?.available))?"present":value.length&&value.every(v=>v&&(v.present===false||v.exists===false))?"missing":"unrecorded";}
      else
      if(typeof value==="object")state=value.present===false||value.exists===false||["missing","error","failed"].includes(value.status)?"missing":value.present===true||value.exists===true||value.path||value.file||value.url||["done","completed","present","available"].includes(value.status)?"present":"unrecorded";
      else state="present";
    }
    return {state,value,path:typeof value==="string"?value:value&&typeof value==="object"?value.path||value.file||value.url||"":""};
  }
  function mediaIcon(media,state) {const wrap=node("span","media-cell"+(state!=="present"?" "+state:""));wrap.title=media.label+"："+(state==="present"?"已记录":state==="missing"?"缺失":"未记录");wrap.setAttribute("aria-label",wrap.title);const svg=document.createElementNS("http://www.w3.org/2000/svg","svg");svg.setAttribute("viewBox","0 0 16 16");svg.setAttribute("aria-hidden","true");svg.innerHTML=media.icon;wrap.append(svg);return wrap;}
  async function loadJobs(showLoading=false) {
    if(!app.runId||app.authFailed)return;const generation=app.generation,request=++app.jobRequest;
    if(showLoading)$("jobs-loading").hidden=false;
    try {
      const data=await get(runPath("/jobs"),{search:$("search").value.trim(),system:$("system-filter").value,status:$("status-filter").value,page:app.page,limit:app.limit});
      if(generation!==app.generation||request!==app.jobRequest)return;
      app.jobs=Array.isArray(data.items)?data.items:[];app.total=count(data.total);app.page=Math.max(1,count(data.page)||1);$("jobs-error").hidden=true;
      if(app.total&&app.page>Math.ceil(app.total/app.limit)){app.page=Math.ceil(app.total/app.limit);return loadJobs(showLoading);}
      renderJobs();
      if(app.selected){refreshSelected(generation);}
    } catch(e) {if(generation===app.generation&&request===app.jobRequest){$("jobs-error").hidden=false;$("jobs-error").textContent=e.message;}}
    finally {if(generation===app.generation&&request===app.jobRequest)$("jobs-loading").hidden=true;}
  }
  function renderJobs() {
    const stale=!!(app.state&&effectiveStatus(app.state.run).css==="stale");app.jobsStale=stale;const fragment=document.createDocumentFragment();
    app.jobs.forEach(job=>{
      const row=node("tr","game-row"+(app.selected&&app.selected.id===job.id?" is-selected":""));row.tabIndex=0;row.setAttribute("aria-label","查看 "+(job.name||job.file||job.id)+" 的资源详情");
      const title=node("td"),name=node("div","game-name",job.name||job.file||job.id);name.title=job.name||job.file||job.id;const file=node("div","game-file",job.file||job.id);file.title=job.file||job.id;title.append(name,file);
      const system=node("td");system.append(node("span","system-tag",job.system||"未分类"));const status=node("td"),badge=node("span","status-badge status-"+(stale&&job.status==="running"?"stale":statusClass(job.status)),stale&&job.status==="running"?"状态待确认":statusName(job.status));if(stale&&job.status==="running")badge.title="最后记录：处理中";status.append(badge);
      const media=node("td"),icons=node("div","media-icons");MEDIA.forEach(m=>icons.append(mediaIcon(m,mediaState(job,m).state)));media.append(icons);
      const metadata=node("td"),meta=node("div","data-meta"),sourceCount=list(job.sources).length,unknown=list(job.unknown).length,issues=list(job.issues).length;
      meta.append(node("span","meta-label",sourceCount?sourceCount+" 个来源":"来源未记录"));if(unknown)meta.append(node("span","meta-label warn",unknown+" 项未知"));if(issues)meta.append(node("span","meta-label warn",issues+" 项问题"));metadata.append(meta);
      const time=node("td"),t=node("time","timestamp");titleDate(t,job.updated_at,true);time.append(t);row.append(title,system,status,media,metadata,time);
      row.addEventListener("click",()=>openDetail(job,row));row.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();openDetail(job,row);}});fragment.append(row);
    });$("jobs").replaceChildren(fragment);$("jobs-empty").hidden=app.jobs.length>0;
    const filtered=$("search").value.trim()||$("system-filter").value||$("status-filter").value;
    $("jobs-empty").textContent=filtered?"没有符合筛选条件的游戏。试试更短的名称或清除筛选。":"还没有游戏记录。完成资源盘点后会逐项显示。";
    const pages=Math.max(1,Math.ceil(app.total/app.limit)),start=app.total?(app.page-1)*app.limit+1:0,end=Math.min(app.total,app.page*app.limit);
    $("page-description").textContent="第 "+num(start)+"–"+num(end)+" 条 · 共 "+num(app.total)+" 款";$("page-number").textContent=app.page+" / "+pages;$("prev-page").disabled=app.page<=1;$("next-page").disabled=app.page>=pages;
    $("games-caption").textContent=stale?"最后已知游戏记录 · 当前处理状态待确认。":filtered?"筛选结果 · 点选游戏查看资源详情。":"点选游戏，查看媒体、来源与未知资料。";
  }
  async function refreshSelected(generation) {
    const id=app.selected&&app.selected.id;if(!id)return;const request=++detailRequest;
    try {
      const data=await get(runPath("/jobs/"+encodeURIComponent(id)));
      if(generation!==app.generation||request!==detailRequest||!app.selected||app.selected.id!==id)return;
      if(!data.job||data.job.id!==id)throw new Error("游戏详情与所选条目不一致，等待重新读取。");
      app.selected=data.job;app.selectedFresh=true;if(detailView){detailView.loading=false;detailView.error="";}renderDetail();
    } catch(e) {
      if(generation!==app.generation||request!==detailRequest||!app.selected||app.selected.id!==id)return;
      app.selectedFresh=false;if(detailView){detailView.loading=false;detailView.error=e.message;}renderDetail();
    }
  }
  function safeLink(value) {try{const url=new URL(value);return ["https:","http:"].includes(url.protocol)?url.href:null;}catch(_){return null;}}
  function sourceList(items) {
    const ul=node("ul","detail-list");items.forEach(item=>{
      const li=node("li"),isObject=item&&typeof item==="object";const raw=isObject?item.url||item.href||"":String(item);const sourceType={historical_source:"历史记录来源",screenscraper:"ScreenScraper",manual:"人工核实"};const label=isObject?item.name||item.label||item.provider||sourceType[item.type]||item.type||fieldLabel(item.key)||"来源":raw;
      const safe=safeLink(raw);if(safe){const a=node("a","",label);a.href=safe;a.target="_blank";a.rel="noopener noreferrer";a.referrerPolicy="no-referrer";li.append(a);if(label!==raw)li.append(node("span","source-url",raw));}else{li.append(node("span","",isObject?label+(item.value!==undefined?" · "+valueText(item.value):raw?" · "+raw:""):raw));}
      if(isObject&&item.note)li.append(node("p","source-url",item.note));ul.append(li);
    });return ul;
  }
  function stopPlayers(root) {if(!root)return;root.querySelectorAll("video").forEach(video=>{video.pause();video.removeAttribute("src");video.querySelectorAll("source").forEach(s=>s.removeAttribute("src"));video.load();});}
  function openDetail(job,origin) {closeLightbox(false);stopPlayers($("detail-content"));++detailRequest;app.selected=job;app.selectedFresh=false;app.detailFingerprint="";detailView=null;app.returnFocus=origin;$("drawer-backdrop").hidden=false;$("detail-drawer").hidden=false;$("detail-drawer").scrollTop=0;document.body.style.overflow="hidden";renderDetail();renderJobs();$("close-detail").focus();refreshSelected(app.generation);}
  function closeDetail() {const wasOpen=!$("detail-drawer").hidden;closeLightbox(false);stopPlayers($("detail-content"));++detailRequest;$("drawer-backdrop").hidden=true;$("detail-drawer").hidden=true;document.body.style.overflow="";app.selected=null;detailView=null;$("detail-content").replaceChildren();if(wasOpen){renderJobs();if(app.returnFocus&&app.returnFocus.isConnected)app.returnFocus.focus();}app.returnFocus=null;}
  function detailSection(title) {const section=node("section","detail-section");section.append(node("h3","",title));return section;}
  function fieldLabel(key) {return FIELD[key]||MEDIA.find(m=>m.key===key||m.legacy===key)?.label||OPTIONAL_MEDIA[key]||key||"资料";}
  function fieldValue(key,value) {
    if(value===null||value===undefined||value==="")return "尚未记录";
    if(["favorite","hidden","kidgame","broken","nogamecount","nomultiscrape","completed"].includes(key))return [true,"true",1,"1","yes"].includes(value)?"是":[false,"false",0,"0","no"].includes(value)?"否":valueText(value);
    if(key==="date_precision")return {day:"精确到日",month:"精确到月",year:"精确到年",unknown:"精度未确认"}[value]||valueText(value);
    if(["releasedate","lastplayed"].includes(key)&&/^\d{8}(T\d{6})?$/.test(String(value))){const text=String(value),date=text.slice(0,4)+"-"+text.slice(4,6)+"-"+text.slice(6,8);if(text.endsWith("T000000")&&key==="releasedate"||text.length===8)return date;return date+" "+text.slice(9,11)+":"+text.slice(11,13)+":"+text.slice(13,15);}
    if(key==="rating"&&Number.isFinite(Number(value))&&Number(value)>=0&&Number(value)<=1)return (Number(value)*10).toFixed(1)+" / 10（原值 "+value+"）";
    if(key==="playtime"&&Number.isFinite(Number(value))&&Number(value)>=0){const seconds=Math.round(Number(value)),hours=Math.floor(seconds/3600),minutes=Math.floor(seconds%3600/60);return (hours?hours+" 小时 ":"")+(minutes?minutes+" 分 ":"")+(seconds%60||!seconds?seconds%60+" 秒":"")+"（"+seconds+" 秒）";}
    if(["size","rom_size","rom_size_bytes"].includes(key)&&Number.isFinite(Number(value))&&Number(value)>=0){const size=Number(value);return size>=1024*1024?(size/1024/1024).toFixed(2)+" MiB（"+num(size)+" 字节）":size>=1024?(size/1024).toFixed(1)+" KiB（"+num(size)+" 字节）":num(size)+" 字节";}
    return typeof value==="boolean"?value?"是":"否":valueText(value);
  }
  function fieldGrid(fields,emptyText="尚未记录扫描资料。") {
    const dl=node("dl","detail-fields");let entries=0;
    for(const [key,value] of Object.entries(fields||{})){if(value===undefined)continue;const item=node("div","detail-field"),label=node("dt","",fieldLabel(key)),content=node("dd");label.title=key;
      if(value&&typeof value==="object"&&!Array.isArray(value))content.append(fieldGrid(value));
      else if(Array.isArray(value)&&value.some(v=>v&&typeof v==="object")){const children=node("div","field-array");value.forEach((v,i)=>{const child=node("div","field-array-item");child.append(node("span","field-array-number",String(i+1).padStart(2,"0")),v&&typeof v==="object"?fieldGrid(v):node("span","",fieldValue(key,v)));children.append(child);});content.append(children);}
      else content.textContent=Array.isArray(value)?value.map(v=>fieldValue(key,v)).join("、"):fieldValue(key,value);
      if(["path","original_path","relative_path","rom_path","file","sha256","md5","crc32"].includes(key))content.classList.add("mono");item.append(label,content);dl.append(item);entries++;
    }return entries?dl:node("p","detail-empty",emptyText);
  }
  function previewOf(value) {if(!value||typeof value!=="object")return null;const p=value.preview||value.extras?.preview;return p&&typeof p==="object"?p:null;}
  function previewURL(preview,params={}) {return preview&&preview.available===true&&typeof preview.asset_id==="string"&&preview.asset_id?apiURL(runPath("/media/"+encodeURIComponent(preview.asset_id)),params):null;}
  async function mediaErrorReason(preview) {
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),12000);
    try {const response=await fetch(previewURL(preview),{method:"GET",headers:{Range:"bytes=0-0"},credentials:"same-origin",cache:"no-store",signal:controller.signal,referrerPolicy:"no-referrer"});if(!response.ok){if(String(response.headers.get("Content-Type")||"").includes("application/json")){const body=await response.json();return body.error||body.message||"媒体文件暂时无法读取（HTTP "+response.status+"）。";}return "媒体文件暂时无法读取（HTTP "+response.status+"）。";}if(response.body)await response.body.cancel();return "文件已可读取，可点击重试；如仍无法播放，请检查浏览器是否支持媒体编码。";}catch(_){return "媒体预览连接中断，请确认电脑工作台与设备连接后重试。";}finally{clearTimeout(timeout);}
  }
  function mediaCandidates(value) {
    if(Array.isArray(value))return value.flatMap(item=>mediaCandidates(item));
    const base=value&&typeof value==="object"?value:{path:typeof value==="string"?value:"",present:!!value},candidates=[base],extras=base.extras||{};
    for(const item of [...(Array.isArray(base.candidates)?base.candidates:[]),...(Array.isArray(extras.candidates)?extras.candidates:[])])if(item&&typeof item==="object")candidates.push(item.asset_id?{preview:item,path:item.path||item.original_path||"",status:item.status}:item);
    const seen=new Set();return candidates.filter(candidate=>{const preview=previewOf(candidate),key=preview?.asset_id||candidate.path||candidate.file||JSON.stringify(candidate);if(seen.has(key))return false;seen.add(key);return true;});
  }
  function mediaLabel(kind) {return MEDIA.find(m=>m.key===kind)?.label||OPTIONAL_MEDIA[kind]||"其他媒体 · "+kind;}
  function nativeMedia(job) {const result={};for(const m of MEDIA)result[m.key]=job.media?.[m.key]??job.media?.[m.legacy]??null;for(const [key,value] of Object.entries(job.media||{})){if(MEDIA.some(m=>m.legacy===key&&job.media[m.key]===undefined))continue;result[key]=value;}return result;}
  function imageEntries() {const images=[];if(!app.selected)return images;for(const [kind,value] of Object.entries(nativeMedia(app.selected)))mediaCandidates(value).forEach((candidate,index)=>{const preview=previewOf(candidate);if(previewURL(preview)&&String(preview.mime||"").startsWith("image/"))images.push({kind,index,preview,label:mediaLabel(kind)+(mediaCandidates(value).length>1?" · "+(index+1):""),path:candidate.path||candidate.file||value?.path||""});});return images;}
  function openLightbox(preview,label,origin) {lightboxItems=imageEntries();lightboxIndex=Math.max(0,lightboxItems.findIndex(item=>item.preview.asset_id===preview.asset_id));if(!lightboxItems.length)lightboxItems=[{preview,label,path:""}];lightboxReturn=origin;$("media-lightbox").hidden=false;renderLightbox();$("close-lightbox").focus();}
  function renderLightbox() {const item=lightboxItems[lightboxIndex];if(!item)return;const image=$("lightbox-image");image.src=previewURL(item.preview);image.alt=item.label;image.referrerPolicy="no-referrer";$("lightbox-title").textContent=item.label;$("lightbox-count").textContent=(lightboxIndex+1)+" / "+lightboxItems.length;$("lightbox-path").textContent=item.path||"当前工作台中的原始媒体";$("lightbox-info").textContent=item.preview.size?fieldValue("size",item.preview.size):"原图";$("prev-image").disabled=lightboxItems.length<2;$("next-image").disabled=lightboxItems.length<2;}
  function closeLightbox(restore=true) {if(!$("media-lightbox"))return;const opened=!$("media-lightbox").hidden;$("media-lightbox").hidden=true;$("lightbox-image").removeAttribute("src");lightboxItems=[];if(opened&&restore&&lightboxReturn?.isConnected)lightboxReturn.focus({preventScroll:true});lightboxReturn=null;}
  function switchImage(delta){if(lightboxItems.length){lightboxIndex=(lightboxIndex+delta+lightboxItems.length)%lightboxItems.length;renderLightbox();}}
  function buildDetailView(job) {
    const content=$("detail-content"),view={id:job.id,loading:true,error:"",metadataFingerprint:"",notesFingerprint:"",cards:new Map(),coverAsset:""};const tags=node("div","detail-tags");view.platform=node("span","system-tag");view.badge=node("span","status-badge");tags.append(view.platform,view.badge);
    view.notice=node("p","detail-stale");view.notice.setAttribute("role","status");view.hero=node("div","detail-hero");view.cover=node("div","detail-cover");const summary=node("div","detail-summary");view.title=node("h2","detail-title");view.title.id="detail-title";view.subtitle=node("p","detail-meta");view.facts=node("dl","detail-facts");summary.append(tags,view.title,view.subtitle,view.facts);view.hero.append(view.cover,summary);
    view.file=node("p","detail-file");view.updated=node("p","detail-meta");view.metadata=node("div");view.media=detailSection("扫描到的媒体");view.media.append(node("p","section-note","图片可点开查看原图，视频可直接播放。状态与预览能力分别来自扫描记录和电脑上的实际文件。"));view.grid=node("div","detail-media");view.media.append(view.grid);view.notes=node("div");content.replaceChildren(view.notice,view.hero,view.file,view.updated,view.metadata,view.media,view.notes);detailView=view;return view;
  }
  function updateHero(view,job,details) {
    view.platform.textContent=job.system||"未分类";view.title.textContent=details.name||job.name||job.file||job.id;view.subtitle.textContent="编号 · "+job.id;view.file.textContent=job.file||"文件路径未记录";view.updated.textContent="扫描记录更新 · "+stamp(job.updated_at);
    const fields={developer:details.developer,publisher:details.publisher,genre:details.genre,releasedate:details.releasedate,players:details.players,rating:details.rating},fingerprint=JSON.stringify(fields);
    if(view.factsFingerprint!==fingerprint){view.factsFingerprint=fingerprint;const fragment=document.createDocumentFragment();for(const [key,value] of Object.entries(fields)){const item=node("div");item.append(node("dt","",fieldLabel(key)),node("dd",value===undefined||value===""?"is-unknown":"",fieldValue(key,value)));fragment.append(item);}view.facts.replaceChildren(fragment);}
    const coverValue=nativeMedia(job).covers,candidate=mediaCandidates(coverValue).find(c=>previewURL(previewOf(c))&&String(previewOf(c).mime||"").startsWith("image/")),preview=previewOf(candidate),asset=preview?.asset_id||"";
    if(view.coverAsset!==asset||!view.cover.childElementCount){view.coverAsset=asset;if(asset){const button=node("button","cover-preview"),image=node("img");button.type="button";button.setAttribute("aria-label","查看封面原图");image.src=previewURL(preview);image.alt=(job.name||"游戏")+"封面";image.decoding="async";image.referrerPolicy="no-referrer";button.append(image,node("span","","查看封面原图"));button.addEventListener("click",()=>openLightbox(preview,"封面",button));image.addEventListener("error",()=>{view.cover.replaceChildren(node("p","preview-unavailable","封面文件暂不可读取"));});view.cover.replaceChildren(button);}else view.cover.replaceChildren(node("p","preview-unavailable",coverValue?"封面已记录\n电脑尚无可预览文件":"封面尚未记录"));}
  }
  function renderMetadata(view,job,details) {
    const fingerprint=JSON.stringify([details,view.loading]);if(view.metadataFingerprint===fingerprint)return;view.metadataFingerprint=fingerprint;const fragment=document.createDocumentFragment(),description=details.desc??details.description;
    const info=detailSection("游戏简介");if(description){const text=node("p","detail-description",description);if(String(description).length>360){text.classList.add("is-collapsed");const toggle=node("button","description-toggle","展开完整简介");toggle.type="button";toggle.setAttribute("aria-expanded","false");toggle.addEventListener("click",()=>{const expanded=toggle.getAttribute("aria-expanded")!=="true";toggle.setAttribute("aria-expanded",String(expanded));toggle.textContent=expanded?"收起简介":"展开完整简介";text.classList.toggle("is-collapsed",!expanded);});info.append(text,toggle);}else info.append(text);}else info.append(node("p","detail-empty",view.loading?"正在读取扫描到的完整简介…":"扫描记录中没有游戏简介。"));fragment.append(info);
    const paths=detailSection("ROM 与扫描信息"),pathFields={file:job.file,...(details.path!==undefined?{path:details.path}:{})};for(const key of ["original_path","original_paths","relative_path","rom_path","size","rom_size","rom_size_bytes","sha256","md5","crc32","gamelist_path","date_precision"])if(details[key]!==undefined)pathFields[key]=details[key];if(details.file&&typeof details.file==="object")pathFields.rom=details.file;if(details.rom)pathFields.rom=details.rom;if(details.scan)pathFields.scan=details.scan;paths.append(fieldGrid(pathFields));fragment.append(paths);
    const history=detailSection("游玩记录与设置"),historyFields={...details.protected,...details.history};for(const key of ["favorite","hidden","playcount","playtime","lastplayed","altemulator","sortname","controller","controllers","broken","kidgame","nogamecount","nomultiscrape","completed"])if(details[key]!==undefined)historyFields[key]=details[key];history.append(fieldGrid(historyFields,"扫描记录中没有收藏、游玩记录或模拟器设置。"));fragment.append(history);
    const handled=new Set(["name","desc","description","developer","publisher","genre","players","releasedate","rating","path","original_path","original_paths","relative_path","rom_path","size","rom_size","rom_size_bytes","sha256","md5","crc32","gamelist_path","date_precision","rom","scan","history","file","protected","metadata",...Object.keys(historyFields)]),additional={};for(const [key,value] of Object.entries(details))if(!handled.has(key))additional[key]=value;
    if(Object.keys(additional).length){const extras=detailSection("完整扫描字段"),disclosure=node("details","detail-disclosure");disclosure.append(node("summary","","查看其他资料与原生字段"),fieldGrid(additional));extras.append(disclosure);fragment.append(extras);}if(!Object.keys(details).length&&!view.loading)fragment.append(node("p","detail-note","这条历史记录尚未包含完整游戏资料。工作台会展示实际导入或扫描的内容，不补造未记录的字段。"));
    const expanded=[...view.metadata.querySelectorAll("details[open]")].map(d=>d.querySelector("summary")?.textContent),descExpanded=view.metadata.querySelector(".description-toggle")?.getAttribute("aria-expanded")==="true";view.metadata.replaceChildren(fragment);view.metadata.querySelectorAll("details").forEach(d=>{if(expanded.includes(d.querySelector("summary")?.textContent))d.open=true;});if(descExpanded)view.metadata.querySelector(".description-toggle")?.click();
  }
  function mediaStatus(value,preview) {
    const v=Array.isArray(value)?value[0]||{}:value&&typeof value==="object"?value:value===null||value===undefined?{}:{present:!!value},extras=v.extras||{},status=v.status||extras.status;
    if(v.present===false||v.exists===false||["missing","error","failed"].includes(status))return {label:"文件缺失",state:"missing"};
    if(extras.verified===true||v.verified===true||["verified","validated","accepted"].includes(status))return {label:"已验收",state:"verified"};
    if(["historical_receipt","historical_verified_receipt"].includes(status)||v.evidence==="historical_receipt"||v.verification==="historical_receipt")return {label:"历史验收已记录",state:"recorded"};
    if(v.present||v.exists||v.path||v.file||["present","available","done","completed"].includes(status)||preview?.available)return {label:"已扫描",state:"scanned"};
    return {label:"尚未记录",state:"unrecorded"};
  }
  function buildMediaCard(kind) {
    const card=node("article","detail-media-item"),top=node("div","detail-media-heading"),title=node("h4","",mediaLabel(kind)),badge=node("span","media-status"),select=node("select","candidate-select"),preview=node("div","media-preview"),message=node("p","media-preview-message"),facts=node("div","media-file-facts");select.setAttribute("aria-label",mediaLabel(kind)+"候选媒体");top.append(title,badge);card.append(top,select,preview,message,facts);const state={card,badge,select,preview,message,facts,index:0,asset:"",fingerprint:"",candidates:[],kind};select.addEventListener("change",()=>{state.index=Number(select.value);updateMediaCard(state,state.value,true);});return state;
  }
  function showMediaError(card,preview,label) {const selectedId=app.selected?.id,asset=preview?.asset_id;card.message.textContent=label+"，正在检查实际原因…";const retry=node("button","button media-retry","重试预览");retry.type="button";retry.addEventListener("click",()=>updateMediaCard(card,card.value,true));if(!card.preview.querySelector("video"))card.preview.replaceChildren(node("span","preview-unavailable",label),retry);else{card.message.append(document.createTextNode(" "),retry);}mediaErrorReason(preview).then(reason=>{if(app.selected?.id===selectedId&&card.asset===asset&&card.card.isConnected){card.message.textContent=reason;if(card.preview.querySelector("video"))card.message.append(document.createTextNode(" "),retry);}});}
  function updateMediaCard(card,value,force=false) {
    const fingerprint=JSON.stringify([value,app.selected?.video_kind]);card.value=value;if(!force&&card.fingerprint===fingerprint)return;card.fingerprint=fingerprint;card.candidates=mediaCandidates(value);if(card.index>=card.candidates.length)card.index=0;if(!card.asset&&!force){const availableIndex=card.candidates.findIndex(c=>previewURL(previewOf(c)));if(availableIndex>=0)card.index=availableIndex;}
    const candidate=card.candidates[card.index]||{},base=value&&typeof value==="object"?value:{},preview=previewOf(candidate),url=previewURL(preview,force?{retry:Date.now()}:{}),mime=String(preview?.mime||""),asset=preview?.asset_id||"",status=mediaStatus(candidate.present===false?candidate:value,preview);card.card.classList.toggle("media-video-card",mime.startsWith("video/")||card.kind==="videos");card.badge.textContent=status.label;card.badge.className="media-status media-status-"+status.state;card.select.hidden=card.candidates.length<2;const options=document.createDocumentFragment();card.candidates.forEach((item,index)=>{const option=node("option","",String(index+1)+" · "+(item.name||item.label||item.path||item.file||"候选媒体"));option.value=String(index);options.append(option);});card.select.replaceChildren(options);card.select.value=String(card.index);
    const previewStatus=preview?.status||preview?.state,message=preview?.reason||preview?.message||candidate.extras?.preview_reason||base.extras?.preview_reason;
    card.message.textContent=url?preview.needs_device&&!preview.cached?"设备媒体按需读取，成功后保存在电脑预览缓存中":mime.startsWith("video/")?"点击播放查看实际视频内容":"点击图片查看原图":previewStatus==="pending"?"正在准备电脑预览…":status.state==="missing"?"扫描记录显示文件缺失":status.state==="unrecorded"?"没有扫描到这一类媒体":"已记录媒体，电脑当前没有可预览文件";if(message)card.message.textContent+=" · "+valueText(message);
    if(card.asset!==asset||force||!card.preview.childElementCount){stopPlayers(card.preview);card.asset=asset;card.preview.replaceChildren();if(url&&mime.startsWith("image/")){const button=node("button","media-image-button"),img=node("img");button.type="button";button.setAttribute("aria-label","查看"+mediaLabel(card.kind)+"原图");img.src=url;img.alt=mediaLabel(card.kind)+" · "+(app.selected?.name||"游戏");img.loading="lazy";img.decoding="async";img.referrerPolicy="no-referrer";button.append(img);button.addEventListener("click",()=>openLightbox(preview,mediaLabel(card.kind),button));img.addEventListener("error",()=>showMediaError(card,preview,"图片暂不可读取"));card.preview.append(button);}else if(url&&mime.startsWith("video/")){const video=node("video");video.controls=true;video.preload="metadata";video.playsInline=true;video.src=url;video.setAttribute("aria-label",mediaLabel(card.kind));video.addEventListener("error",()=>showMediaError(card,preview,"视频暂时无法播放"));card.preview.append(video);}else if(url){const a=node("a","button","打开原始媒体");a.href=url;a.target="_blank";a.rel="noopener noreferrer";a.referrerPolicy="no-referrer";card.preview.append(a);}else card.preview.append(node("span","preview-unavailable",status.state==="missing"?"文件缺失":"暂无电脑预览"));}
    const fields={};const path=candidate.path||candidate.file||base.path||base.file||typeof value==="string"&&value;if(path)fields.path=path;const size=preview?.size??candidate.size??base.size;if(size!==undefined)fields.size=size;if(card.kind==="videos")fields["视频内容类型"]=VIDEO[candidate.video_kind||base.video_kind||app.selected?.video_kind]||"类型未确认";const origin=preview?.origin;if(origin)fields["电脑预览来源"]={local_cache:"本地媒体缓存",local_file:"本地扫描文件",adb_cache:"设备媒体缓存"}[origin]||origin;if(candidate.source||base.source)fields.source=candidate.source||base.source;card.facts.replaceChildren(fieldGrid(fields,""));
  }
  function renderMedia(view,job) {const media=nativeMedia(job),keys=Object.keys(media);for(const [kind,card] of view.cards){if(!keys.includes(kind)){stopPlayers(card.card);card.card.remove();view.cards.delete(kind);}}for(const [kind,value] of Object.entries(media)){let card=view.cards.get(kind);if(!card){card=buildMediaCard(kind);view.cards.set(kind,card);view.grid.append(card.card);}updateMediaCard(card,value);}}
  function issueList(items,fallback) {const ul=node("ul","detail-list");items.forEach(item=>{const li=node("li");if(item&&typeof item==="object"){const key=item.field||item.name||item.key||item.code||item.kind;li.append(node("span","value-label",fieldLabel(key)||fallback),document.createTextNode(valueText(item.message||item.reason||item.value||"未知 / 待核实")));}else{const key=String(item);li.append(node("span","value-label",fieldLabel(key)),document.createTextNode(FIELD[key]?"尚未核实":""));}ul.append(li);});return ul;}
  function renderNotes(view,job) {
    const fingerprint=JSON.stringify([job.sources,job.issues,job.unknown,job.missing,job.video_kind]);if(view.notesFingerprint===fingerprint)return;view.notesFingerprint=fingerprint;const fragment=document.createDocumentFragment();
    if(["screenshot_preview","original_title_art_preview","screenshot","screenshots","slideshow","preview","generated","title","titlescreen"].includes(job.video_kind))fragment.append(node("p","detail-note","视频内容为"+(VIDEO[job.video_kind]||"预览")+"，请按实际类型查看；截图或标题画面预览不代表实机游玩录像。"));
    const sources=detailSection("资料来源"),sourceItems=list(job.sources);sources.append(sourceItems.length?sourceList(sourceItems):node("p","detail-empty","来源尚未记录。"));fragment.append(sources);
    const issues=detailSection("资料问题"),issueItems=list(job.issues);issues.append(issueItems.length?issueList(issueItems,"资料问题"):node("p","detail-empty","此游戏没有记录资料审计问题。"));fragment.append(issues);
    const unknown=detailSection("未知与待确认资料"),unknownItems=list(job.unknown);unknown.append(unknownItems.length?issueList(unknownItems,"待确认"):node("p","detail-empty","此游戏没有记录未知字段。"));fragment.append(unknown);
    const missingItems=list(job.missing);if(missingItems.length){const section=detailSection("缺失记录");section.append(node("p","detail-note",missingItems.map(x=>fieldLabel(typeof x==="string"?x:x.key||x.type||valueText(x))).join("、")));fragment.append(section);}view.notes.replaceChildren(fragment);
  }
  function renderDetail() {
    const job=app.selected;if(!job)return;const view=detailView&&detailView.id===job.id?detailView:buildDetailView(job),raw=job.details&&typeof job.details==="object"?job.details:{},details={...(raw.metadata&&typeof raw.metadata==="object"?raw.metadata:{}),...raw},stale=!app.selectedFresh||["offline","network-offline","reconnecting","unauthorized"].includes(app.transport)||!!(app.state&&effectiveStatus(app.state.run).css==="stale"),scrollTop=$("detail-drawer").scrollTop;
    view.badge.textContent=stale&&job.status==="running"?"状态待确认":statusName(job.status);view.badge.className="status-badge status-"+(stale&&job.status==="running"?"stale":statusClass(job.status));view.notice.hidden=!view.loading&&!view.error&&!stale;view.notice.classList.toggle("is-loading",view.loading);view.notice.textContent=view.loading?"正在读取这个条目的完整扫描资料与媒体…":view.error||"此处为最后一次读取的记录，当前资源状态待确认。";
    updateHero(view,job,details);renderMetadata(view,job,details);renderMedia(view,job);renderNotes(view,job);$("detail-drawer").scrollTop=scrollTop;
  }
  async function loadEvents(initial=false) {
    if(!app.runId||app.authFailed)return;const generation=app.generation,request=++app.eventRequest;
    try{const data=await get(runPath("/events"),initial?{tail:1,limit:100}:{after:app.lastEvent,limit:500});if(generation!==app.generation||request!==app.eventRequest)return;
      const items=Array.isArray(data.items)?data.items:[];const known=new Set(app.events.map(x=>x.id));for(const item of items)if(!known.has(item.id))app.events.push(item);app.events.sort((a,b)=>count(a.id)-count(b.id));app.events=app.events.slice(-100);app.lastEvent=Math.max(app.lastEvent,count(data.last_id));renderEvents();
      if(!initial&&items.length===500)scheduleRefresh();
    }catch(_){if(generation===app.generation)$("events-status").textContent="事件读取失败，等待连接恢复";}
  }
  function renderEvents() {
    const fragment=document.createDocumentFragment();[...app.events].reverse().forEach(event=>{
      const error=["error","failed","failure"].includes(event.kind)||(event.payload&&["error","failed"].includes(event.payload.status)),li=node("li","event-item"+(error?" is-error":"")),time=node("time");titleDate(time,event.created_at,true);
      const kinds={progress:"进度",job:"游戏",initialized:"初始化",device:"设备",connection:"连接",error:"异常",warning:"提醒",backup:"备份",validation:"验证",report:"报告",retry:"重试",complete:"完成",completed:"完成",info:"记录"};const kind=node("span","event-kind",kinds[event.kind]||event.kind||"记录");kind.title=event.kind||"记录";li.append(time,kind,node("p","event-message",event.message||"（无说明）"));
      if(event.payload&&Object.keys(event.payload).length){const details=node("details"),summary=node("summary","","查看记录数据"),pre=node("pre","",JSON.stringify(event.payload,null,2));details.append(summary,pre);li.append(details);}fragment.append(li);
    });$("events").replaceChildren(fragment);$("events-empty").hidden=app.events.length>0;
  }
  function connectStream(generation) {
    if(app.authFailed||generation!==app.generation)return;closeStream();setTransport("connecting");
    const stream=new EventSource(apiURL(runPath("/stream")));app.stream=stream;
    stream.onopen=()=>{if(generation===app.generation){app.contactAt=Date.now();setTransport("live");}};
    stream.addEventListener("snapshot",event=>{if(generation!==app.generation)return;try{const data=JSON.parse(event.data);app.contactAt=Date.now();setTransport("live");applyState(data);}catch(_){showNotice("收到无法识别的进度数据，正在重新读取。");scheduleRefresh();}});
    stream.addEventListener("update",event=>{if(generation!==app.generation)return;app.contactAt=Date.now();setTransport("live");scheduleRefresh();});
    stream.onerror=()=>{if(generation!==app.generation||app.authFailed)return;setTransport(navigator.onLine?"reconnecting":"network-offline");scheduleRefresh(1000);};
  }
  function scheduleRefresh(delay=600) {if(app.refreshTimer)return;app.refreshTimer=setTimeout(()=>{app.refreshTimer=null;refreshCurrent();},delay);}
  async function refreshCurrent(manual=false) {
    if(!app.runId||app.authFailed){if(!app.authFailed)await loadRuns(true);return;}
    const generation=app.generation;if(manual)$("refresh").disabled=true;
    try{const state=await get(runPath());if(generation!==app.generation)return;applyState(state);if(app.stream&&app.stream.readyState===1)setTransport("live");else if(!app.stream||app.stream.readyState===2)connectStream(generation);else setTransport("reconnecting");await Promise.all([loadJobs(manual),loadEvents()]);}
    catch(e){if(e.status===404)loadRuns();}
    finally{if(manual)$("refresh").disabled=false;}
  }
  function filterChanged(){app.page=1;loadJobs(true);}
  $("run-select").addEventListener("change",()=>selectRun($("run-select").value));$("refresh").addEventListener("click",()=>refreshCurrent(true));$("empty-refresh").addEventListener("click",()=>{if(app.runId)refreshCurrent(true);else loadRuns(true);});
  $("filter-form").addEventListener("submit",e=>{e.preventDefault();clearTimeout(app.searchTimer);filterChanged();});$("search").addEventListener("input",()=>{clearTimeout(app.searchTimer);app.searchTimer=setTimeout(filterChanged,300);});
  ["system-filter","status-filter"].forEach(id=>$(id).addEventListener("change",filterChanged));$("clear-filters").addEventListener("click",()=>{$("search").value="";$("system-filter").value="";$("status-filter").value="";filterChanged();});
  $("page-size").addEventListener("change",()=>{app.limit=Number($("page-size").value);filterChanged();});$("prev-page").addEventListener("click",()=>{if(app.page>1){app.page--;loadJobs(true);}});$("next-page").addEventListener("click",()=>{if(app.page<Math.ceil(app.total/app.limit)){app.page++;loadJobs(true);}});
  document.querySelectorAll("[data-filter]").forEach(button=>button.addEventListener("click",()=>{$("status-filter").value=button.dataset.filter;filterChanged();$("games-title").scrollIntoView({behavior:matchMedia("(prefers-reduced-motion: reduce)").matches?"auto":"smooth",block:"start"});}));
  $("refresh-detail").addEventListener("click",async()=>{const button=$("refresh-detail"),id=app.selected?.id;if(!id)return;button.disabled=true;if(detailView){detailView.loading=true;detailView.error="";}renderDetail();try{await refreshSelected(app.generation);}finally{button.disabled=false;}});
  $("close-detail").addEventListener("click",closeDetail);$("drawer-backdrop").addEventListener("click",closeDetail);$("close-lightbox").addEventListener("click",()=>closeLightbox());$("prev-image").addEventListener("click",()=>switchImage(-1));$("next-image").addEventListener("click",()=>switchImage(1));$("media-lightbox").addEventListener("click",e=>{if(e.target===$("media-lightbox"))closeLightbox();});$("lightbox-image").addEventListener("load",()=>{const img=$("lightbox-image");$("lightbox-info").textContent=img.naturalWidth+" × "+img.naturalHeight+(lightboxItems[lightboxIndex]?.preview?.size?" · "+fieldValue("size",lightboxItems[lightboxIndex].preview.size):"");});$("lightbox-image").addEventListener("error",()=>{$("lightbox-info").textContent="原图暂时无法读取，请刷新游戏详情重试。";});
  document.addEventListener("keydown",e=>{if($("detail-drawer").hidden)return;const lightboxOpen=!$("media-lightbox").hidden;if(e.key==="Escape"){e.preventDefault();if(lightboxOpen)closeLightbox();else closeDetail();}else if(lightboxOpen&&["ArrowLeft","ArrowRight"].includes(e.key)){e.preventDefault();switchImage(e.key==="ArrowLeft"?-1:1);}else if(e.key==="Tab"){const dialog=lightboxOpen?$("media-lightbox"):$("detail-drawer"),focusables=[...dialog.querySelectorAll('button,a[href],select,summary,video[controls],[tabindex="0"]')].filter(el=>!el.disabled&&!el.hidden&&el.getClientRects().length),first=focusables[0],last=focusables[focusables.length-1];if(first&&e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(last&&!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}});
  window.addEventListener("offline",()=>setTransport("network-offline"));window.addEventListener("online",()=>{if(!app.authFailed){setTransport("reconnecting");refreshCurrent();}});document.addEventListener("visibilitychange",()=>{if(!document.hidden&&!app.authFailed)refreshCurrent();});window.addEventListener("beforeunload",()=>{closeStream();stopPlayers($("detail-content"));});
  setInterval(()=>{renderFreshness();},5000);setInterval(()=>{if(!document.hidden&&!app.authFailed)refreshCurrent();},15000);setInterval(()=>{if(!document.hidden&&!app.authFailed)loadRuns();},30000);
  loadRuns(true);
})();
