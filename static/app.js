const $ = (selector, parent=document) => parent.querySelector(selector);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const E = escapeHTML;
const states = {pending_review:'待主管确认',ready:'待执行',in_progress:'处理中',resolved:'已结案',dismissed:'已驳回'};
const feedbackNames = {satisfied:'满意',neutral:'一般',dissatisfied:'不满意',unknown:'未回访'};
const modes = {rules:'本地规则 + 知识检索',rules_fallback:'模型回退 · 本地规则', 'llm+rules+retrieval':'LLM + 规则 + 知识检索'};
Object.assign(modes, {'jev+retrieval':'Jev 风险判断 + 知识检索', 'jev+llm+retrieval':'Jev 风险判断 + LLM 建议', jev_fallback:'Jev 未完成 · 本地规则线索'});
const needsReview=row=>row.review_status==='pending'&&row.analysis_review_required;
const displayLabels=row=>needsReview(row)&&row.priority==='—'?['待人工判断']:row.labels;
const pageInfo = {
  overview:['主管总览','把客诉跟进到解决','从发现风险到回访复盘，掌握每一个处理节点。'],
  conversations:['会话分析','先读懂问题，再判断风险','查看核心诉求与原文证据，区分客诉风险和客服违规。'],
  tickets:['处置工单','每一个问题，都有人跟进','确认处置方案、协调负责人，并持续跟踪到结案。'],
  insights:['数据复盘','从处理结果中，找到改进方向','跟踪解决率、处理时长、风险分布和用户回访反馈。'],
  knowledge:['企业知识库','让有效的经验，成为下一次的依据','管理服务规范与处理案例，为处置建议提供可追溯的参考。']
};
let data=null, knowledge=[], selected=null, toastTimer, busy=false, editingKnowledge=null;
let page=location.hash.slice(1) || (location.pathname==='/insights'?'insights':'overview');
function toast(message,error=false){
  const node=$('#toast'); node.textContent=message; node.className='show'+(error?' error':'');
  const dialog=document.querySelector('dialog[open]');
  if(dialog){
    let feedback=dialog.querySelector('.dialog-feedback');
    if(!feedback){feedback=document.createElement('p');feedback.className='dialog-feedback warning';feedback.setAttribute('role','status');dialog.querySelector('.dialog-head').after(feedback);}
    feedback.textContent=message;
  }
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>node.className='',6000);
}
async function api(path, options={}){
  const response=await fetch(path,options);
  const result=await response.json();
  if(!response.ok){
    const detail=result.detail;
    throw new Error(typeof detail==='string'?detail:Array.isArray(detail)?detail.map(x=>x.msg).join('；'):'请求失败，请重试');
  }
  return result;
}
const post=(path,payload)=>api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload || {})});
const time=value=>value?new Date(value).toLocaleString('zh-CN',{hour12:false}):'—';
const badge=row=>'<span class="badge '+(row.priority==='—'?'none':E(row.priority))+'">'+E(row.priority)+'</span>';
const statusBadge=row=>row.ticket?'<span class="badge status-'+E(row.ticket.status)+'">'+E(states[row.ticket.status])+'</span>':'<span class="badge">'+(needsReview(row)?'待人工判断':row.review_status==='pending'?'可抽检':'已复核')+'</span>';
const options=(values,current='')=>values.map(value=>'<option value="'+E(value)+'"'+(value===current?' selected':'')+'>'+E(value)+'</option>').join('');
const empty=message=>'<div class="empty">'+E(message)+'</div>';
function card(title,value,note,cls=''){return '<div class="card '+cls+'"><span>'+E(title)+'</span><b>'+E(value ?? '—')+'</b><small>'+E(note)+'</small></div>';}
async function reload(){
  [data,{items:knowledge}]=await Promise.all([api('/api/conversations'),api('/api/knowledge')]);
  $('#loading').hidden=true;
  const oldLabel=$('#label').value;
  $('#label').innerHTML='<option value="">全部分类</option>'+options(data.labels,oldLabel);
  $('#knowledge-form select[name="labels"]').innerHTML=options(data.labels);
  render();
}
function render(){
  if(!data)return;
  if(!pageInfo[page])page='overview';
  const [name,title,description]=pageInfo[page];
  $('#breadcrumb').textContent=name; $('#page-title').textContent=title; $('#page-description').textContent=description;
  document.querySelectorAll('[data-page]').forEach(link=>link.classList.toggle('active',link.dataset.page===page));
  ['overview','insights','knowledge'].forEach(name=>$('#'+name+'-view').hidden=page!==name);
  $('#queue-view').hidden=!['conversations','tickets'].includes(page);
  $('#queue-title').textContent=page==='tickets'?'处置工单':'全部会话';
  $('#nav-count').textContent=data.summary.active;
  $('#unread').textContent=data.notifications.filter(n=>!n.read).length;
  const fallback=data.items.filter(row=>['rules_fallback','jev_fallback'].includes(row.analysis_mode)).length;
  $('#mode').textContent=(data.jev_enabled?'新导入：Jev 风险判断'+(data.llm_enabled?' + LLM 生成建议':' + 本地知识建议'):data.llm_enabled?'已启用 LLM 语义分析 · 每条会话标注实际分析来源':'本地模式 · 规则分析 + 关键词知识检索 · LLM 未启用')+(fallback?' · '+fallback+' 条会话已回退规则':'');
  renderOverview(); renderQueue(); renderInsights(); renderKnowledge();
}
function renderOverview(){
  const s=data.summary;
  $('#cards').innerHTML=card('已分析会话',s.total,'累计导入并保存的会话')+card('待主管确认',s.pending,'确认方案后进入执行','accent')+card('P0 高优先级',s.p0,'已驳回风险不计入','attention')+card('工单解决率',s.resolution_rate===null?'—':s.resolution_rate+'%',s.resolved+' 已解决 / '+s.ticket_total+' 有效工单');
  const urgent=data.items.filter(x=>needsReview(x)||(x.ticket&&!['resolved','dismissed'].includes(x.ticket.status))).sort((a,b)=>Number(needsReview(b))-Number(needsReview(a))).slice(0,4);
  $('#urgent').innerHTML=urgent.map(row=>'<button class="urgent-row" data-open="'+E(row.conversation_id)+'">'+badge(row)+'<div><strong>'+E(row.summary)+'</strong><small>'+E(row.conversation_id)+' · '+E(row.ticket?.owner||'质检主管')+' · '+E(row.ticket?states[row.ticket.status]:'待人工判断')+'</small></div><span>↗</span></button>').join('')||empty('暂无待处理工单');
  const max=Math.max(1,...s.owners.map(o=>o.active));
  $('#owners').innerHTML=s.owners.map(owner=>'<div class="owner-row"><span class="avatar">'+E(owner.owner[0])+'</span><div><small>'+E(owner.owner)+'</small><div class="meter"><i style="width:'+owner.active/max*100+'%"></i></div></div><b>'+owner.active+'</b></div>').join('')||empty('暂无负责人负载');
}
function renderQueue(){
  const search=$('#search').value.trim().toLowerCase(),priority=$('#priority').value,status=$('#status').value,label=$('#label').value;
  const rows=data.items.filter(row=>(page!=='tickets'||row.ticket)&&(!priority||row.priority===priority)&&(!status||row.ticket?.status===status||(status==='pending_review'&&needsReview(row)))&&(!label||row.labels.includes(label))&&(!search||[row.conversation_id,row.customer,row.agent,row.summary].join(' ').toLowerCase().includes(search)));
  $('#count').textContent=rows.length+' 条';
  $('#list').innerHTML=rows.map(row=>'<tr><td><strong>'+E(row.conversation_id)+'</strong><small>'+E(row.customer)+' · '+E(row.agent)+'</small><small>'+E(row.date)+'</small></td><td><p class="row-summary">'+E(row.summary)+'</p>'+displayLabels(row).map(label=>'<span class="tag">'+E(label)+'</span>').join('')+'</td><td>'+badge(row)+'</td><td>'+E(row.ticket?.owner||(needsReview(row)?'质检主管':'—'))+'</td><td>'+statusBadge(row)+(row.ticket&&!['resolved','dismissed'].includes(row.ticket.status)&&new Date(row.ticket.due_at)<new Date()?'<small style="color:var(--red)">已逾期</small>':'')+'</td><td><button data-open="'+E(row.conversation_id)+'">查看详情 →</button></td></tr>').join('')||'<tr><td colspan="6">'+empty('没有符合条件的记录')+'</td></tr>';
}
function bars(rows){
  const max=Math.max(1,...rows.map(row=>row.count));
  return rows.map(row=>'<div class="bar-row"><span>'+E(row.label)+'</span><div class="meter"><i style="width:'+row.count/max*100+'%"></i></div><b>'+row.count+'</b></div>').join('')||empty('暂无统计数据');
}
function renderInsights(){
  const s=data.summary;
  $('#insight-cards').innerHTML=card('工单解决率',s.resolution_rate===null?'—':s.resolution_rate+'%','不包含已驳回工单','accent')+card('平均处理时长',s.avg_handling_hours===null?'—':s.avg_handling_hours+' h','建单至结案，包含等待时间')+card('进行中的工单',s.active,'包含待确认、待执行与处理中')+card('逾期未结案',s.overdue,'P0 2h / P1 24h / P2 72h','attention');
  $('#risk-chart').innerHTML=bars(s.risk_trend.map(row=>({label:row.date,count:row.flagged})));
  $('#label-chart').innerHTML=bars(s.top_labels);
  $('#priority-chart').innerHTML=bars(s.priorities);
  $('#feedback-chart').innerHTML=s.feedback_trend.length?'<table><thead><tr><th>回访日期</th><th>满意</th><th>一般</th><th>不满意</th><th>未回访</th></tr></thead><tbody>'+s.feedback_trend.map(row=>'<tr><td>'+E(row.date)+'</td><td>'+row.satisfied+'</td><td>'+row.neutral+'</td><td>'+row.dissatisfied+'</td><td>'+row.unknown+'</td></tr>').join('')+'</tbody></table>':empty('结案并记录回访后，反馈趋势将显示在这里');
  const actions={'情绪升级':'复盘投诉升级信号与响应时间，完善投诉专员接管流程。','履约发货':'核对高频延迟场景，补充库存和物流异常的告知规范。','售后退款':'核对退款争议证据，统一审核条件与进度解释话术。','越权承诺':'复盘承诺边界，补充需要主管审批的补偿场景。','服务规范缺失':'抽查服务态度与转接流程，补充正向处理案例。'};
  $('#improvements').innerHTML=s.top_labels.slice(0,3).map(row=>'<div class="improvement"><b>'+E(row.label)+' · '+row.count+' 条风险会话</b><p>'+E(actions[row.label])+'</p><a class="text-link" href="#knowledge">完善知识库 →</a></div>').join('')||empty('积累风险会话后，将按高频标签生成规则化改进建议');
}
function renderKnowledge(){
  $('#knowledge-list').innerHTML=knowledge.map(doc=>'<article class="knowledge-card"><span class="badge '+E(doc.status)+'">'+(doc.status==='published'?'已发布':'待审核草稿')+'</span> <span class="tag">'+(doc.kind==='policy'?'服务规范':'处理案例')+'</span><h3>'+E(doc.title)+'</h3><p>'+E(doc.content)+'</p>'+doc.labels.map(label=>'<span class="tag">'+E(label)+'</span>').join('')+'<small>'+E(doc.id)+' · '+E(doc.version)+'</small>'+(doc.status==='draft'?'<button class="ghost" data-edit-knowledge="'+E(doc.id)+'">编辑草稿</button> <button data-publish="'+E(doc.id)+'">审核通过并发布</button>':'')+'</article>').join('')||empty('暂无知识条目');
}
function feedbackSelect(value='unknown'){
  return '<select id="feedback">'+Object.entries(feedbackNames).map(([key,name])=>'<option value="'+key+'"'+(key===value?' selected':'')+'>'+name+'</option>').join('')+'</select>';
}
function jevDetails(row){
  if(!row.jev)return '';
  const names={present:'识别到风险',absent:'未识别到',uncertain:'待人工判断'};
  return '<details class="source"><summary>查看 AI 判断明细</summary><p class="muted">风险概率是模型对该问题回答“是”的概率，尚未用本项目真实数据校准，不代表识别准确率。证据置信度用于判断原文定位是否明确。</p>'+row.jev.decisions.map(d=>'<p><b>'+E(d.label)+'</b> · '+E(names[d.verdict])+'<br>风险概率 '+E(d.probability.toFixed(2))+' · 证据置信度 '+E(d.evidence_confidence.toFixed(2))+' · 原文 '+E(d.evidence_id)+'</p>').join('')+'</details>';
}
function showDetail(id){
  const row=data.items.find(item=>item.conversation_id===id);
  if(!row){toast('会话不存在',true);return;}
  selected=id;
  const ticket=row.ticket,pending=row.review_status==='pending',active=ticket&&!['resolved','dismissed'].includes(ticket.status);
  $('#detail-title').textContent=row.conversation_id+' · '+row.customer;
  const evidence=(row.evidence.map(hit=>'<div class="evidence"><b>'+E(hit.label)+' · '+(hit.scope==='service'?'客服违规线索':'客诉风险信号')+'</b><br>'+E(hit.evidence)+'</div>').join('')||empty(needsReview(row)?'证据不足，需要人工核查':'暂无明确风险证据，可人工抽检'))+jevDetails(row);
  const sourceHTML=row.sources.map(doc=>'<details class="source"><summary>'+E(doc.id)+' · '+E(doc.title)+' · '+E(doc.version)+'</summary><p>'+E(doc.content)+'</p></details>').join('')||'<p class="muted">未检索到匹配知识，请主管补充依据。</p>';
  let controls='';
  if(pending){
    controls='<h3>主管复核</h3><p class="muted">修改标签或等级时，请说明依据并同步调整处置方案。</p><div>'+data.labels.map(label=>'<label class="check-label"><input type="checkbox" name="review-label" value="'+E(label)+'"'+(row.labels.includes(label)?' checked':'')+'>'+E(label)+'</label>').join('')+'</div><label>优先级<select id="review-priority">'+options(['—','P0','P1','P2'],row.priority)+'</select></label><label>确认处置方案<textarea id="plan" rows="7" maxlength="6000">'+E(row.suggestion)+'</textarea></label><label>复核说明<input id="review-note" maxlength="2000" placeholder="修改或驳回时必填"></label><div class="actions"><button data-review="accept">'+(ticket?'确认方案':'确认 / 修改结论')+'</button><button class="danger" data-review="reject">驳回风险</button></div>';
  }else if(ticket){
    controls='<h3>已确认处置方案</h3><div class="conversation">'+E(ticket.approved_plan||'风险结论已驳回')+'</div>'+(row.review_note?'<p class="muted">复核说明：'+E(row.review_note)+'</p>':'');
    if(ticket.status==='ready')controls+='<div class="actions"><button data-ticket="start">登记开始执行</button></div>';
    if(ticket.status==='in_progress')controls+='<label>实际处理结果<textarea id="resolution" rows="4" maxlength="4000" placeholder="填写已完成的实际动作与解决结果，至少 5 字"></textarea></label><label>用户回访反馈'+feedbackSelect()+'</label><div class="actions"><button data-ticket="resolve">登记结果并结案</button></div>';
    if(ticket.status==='resolved')controls+='<h3>处理结果</h3><div class="conversation">'+E(ticket.resolution)+'</div><p class="muted">结案于 '+E(time(ticket.resolved_at))+'</p><label>用户回访反馈'+feedbackSelect(ticket.feedback)+'</label><div class="actions"><button class="ghost" data-ticket="feedback">更新回访</button><button data-ticket="knowledge"'+(row.history.some(h=>h.action==='knowledge_draft')?' disabled':'')+'>沉淀为案例草稿</button></div>';
  }else controls='<p class="muted">此会话已完成人工抽检。</p>';
  $('#detail-content').innerHTML='<div class="detail-grid"><div><div class="summary-box"><b>核心诉求</b><p>'+E(row.summary)+'</p>'+badge(row)+' '+statusBadge(row)+'</div><h3>原始会话</h3><div class="conversation">'+E(row.dialogue)+'</div><h3>风险证据</h3>'+evidence+'<p class="footnote">分析来源：'+E(modes[row.analysis_mode]||row.analysis_mode)+'<br>用户的投诉意向属于客诉风险信号，不直接代表客服违规。</p>'+(row.analysis_warning?'<p class="warning">'+E(row.analysis_warning)+'</p>':'')+'<h3>知识引用 · 分析时版本</h3>'+sourceHTML+'</div><div>'+(ticket?'<div class="summary-box"><b>'+E(ticket.id)+'</b><p>负责人：'+E(ticket.owner)+'<br>处理期限：'+E(time(ticket.due_at))+'</p></div>':'')+(active?'<div class="form-grid"><label>转派负责人<select id="assignee">'+options(data.owners,ticket.owner)+'</select></label><div class="actions"><button class="ghost" data-ticket="assign">确认转派</button></div></div>':'')+controls+'</div></div><h3>处理记录</h3><ul class="history">'+[...row.history].reverse().map(h=>'<li>'+E(h.note)+'<small>'+E(time(h.at))+'</small></li>').join('')+'</ul>';
  if(!$('#detail').open)$('#detail').showModal();
}
async function action(callback,success){
  if(busy)return;
  busy=true;
  const buttons=[...document.querySelectorAll('dialog[open] button')];
  const previouslyDisabled=buttons.filter(button=>button.disabled);
  buttons.forEach(button=>button.disabled=true);
  try{await callback();await reload();if($('#detail').open&&selected)showDetail(selected);toast(success);}
  catch(error){toast(error.message,true);}
  finally{busy=false;buttons.forEach(button=>button.disabled=previouslyDisabled.includes(button));}
}
async function reviewDecision(kind){
  const row=data.items.find(row=>row.conversation_id===selected);
  const labels=[...document.querySelectorAll('[name="review-label"]:checked')].map(input=>input.value);
  const priority=$('#review-priority').value;
  const changed=priority!==row.priority||labels.slice().sort().join('|')!==row.labels.filter(label=>label!=='未命中风险').slice().sort().join('|');
  const payload={status:kind==='reject'?'rejected':changed?'edited':'accepted',note:$('#review-note').value,labels,plan:$('#plan').value};
  if(priority!=='—')payload.priority=priority;
  await action(()=>post('/api/conversations/'+encodeURIComponent(selected)+'/review',payload),'复核结果已保存');
}
async function ticketAction(kind){
  const payload=kind==='assign'?{owner:$('#assignee').value}:kind==='resolve'?{resolution:$('#resolution').value,feedback:$('#feedback').value}:kind==='feedback'?{feedback:$('#feedback').value}:{};
  await action(()=>post('/api/conversations/'+encodeURIComponent(selected)+'/'+kind,payload),kind==='knowledge'?'已生成知识草稿，请在企业知识库审核发布':'工单已更新');
}
function showNotifications(){
  $('#notification-list').innerHTML=data.notifications.map(n=>'<div class="notification '+(n.read?'read':'')+'"><div><b>'+E(n.owner)+' · '+E(n.conversation_id)+'</b><p>'+E(n.message)+'</p><small class="muted">'+E(time(n.created_at))+'</small></div><button class="ghost" data-notification="'+E(n.id)+'">查看工单</button></div>').join('')||empty('暂无站内提醒');
  if(!$('#notification-dialog').open)$('#notification-dialog').showModal();
}
document.addEventListener('click',event=>{
  const target=event.target.closest('button');
  if(!target)return;
  if(target.classList.contains('close'))target.closest('dialog').close();
  if(target.dataset.open)showDetail(target.dataset.open);
  if(target.dataset.review)reviewDecision(target.dataset.review);
  if(target.dataset.ticket)ticketAction(target.dataset.ticket);
  if(target.dataset.editKnowledge){
    const doc=knowledge.find(doc=>doc.id===target.dataset.editKnowledge);
    editingKnowledge=doc.id;
    const form=$('#knowledge-form');
    ['title','kind','version','content'].forEach(key=>form.elements[key].value=doc[key]);
    [...form.elements.labels.options].forEach(option=>option.selected=doc.labels.includes(option.value));
    $('#knowledge-dialog h2').textContent='编辑知识草稿';
    $('#knowledge-dialog').showModal();
  }
  if(target.dataset.publish)action(()=>post('/api/knowledge/'+encodeURIComponent(target.dataset.publish)+'/publish'),'知识已发布，后续新会话可检索此条目');
  if(target.dataset.notification){
    const notification=data.notifications.find(n=>n.id===target.dataset.notification);
    action(async()=>{await post('/api/notifications/'+encodeURIComponent(notification.id)+'/read');$('#notification-dialog').close();showDetail(notification.conversation_id);},'提醒已标为已读');
  }
});
window.addEventListener('hashchange',()=>{page=location.hash.slice(1)||'overview';render();});
$('#search').addEventListener('input',renderQueue);
['priority','status','label'].forEach(id=>$('#'+id).addEventListener('change',renderQueue));
$('#notifications').onclick=()=>data&&showNotifications();
$('#add-knowledge').onclick=()=>{editingKnowledge=null;$('#knowledge-form').reset();$('#knowledge-dialog h2').textContent='新建知识草稿';$('#knowledge-dialog').showModal();};
$('#knowledge-form').onsubmit=event=>{
  event.preventDefault();const form=event.target;
  const payload={title:form.elements.title.value,kind:form.elements.kind.value,version:form.elements.version.value,content:form.elements.content.value,labels:[...form.elements.labels.selectedOptions].map(option=>option.value)};
  action(async()=>{await (editingKnowledge?api('/api/knowledge/'+encodeURIComponent(editingKnowledge),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}):post('/api/knowledge',payload));editingKnowledge=null;form.reset();$('#knowledge-dialog').close();},'知识草稿已保存');
};
$('#file').onchange=async event=>{
  const file=event.target.files[0];if(!file)return;
  event.target.disabled=true;
  const form=new FormData();form.append('file',file);
  $('#mode').textContent='正在导入并分析会话，请稍候…';
  try{await api('/api/upload',{method:'POST',body:form});await reload();location.hash='conversations';toast('导入完成：会话已保存，风险工单已自动分派');}
  catch(error){toast(error.message,true);if(data)render();}
  finally{event.target.value='';event.target.disabled=false;}
};
reload().catch(error=>{$('#loading').textContent='加载失败：'+error.message+'。请刷新重试。';toast(error.message,true);});
