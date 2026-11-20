import {state} from '../stores/session.js';
import {esc} from '../utils/html.js';
import {badge} from '../components/common/status.js';
import {table} from '../components/common/table.js';

const pct=v=>v===null||v===undefined?'—':(v*100).toFixed(2)+'%';
const pp=v=>v===null||v===undefined?'—':(v>=0?'+':'')+(v*100).toFixed(3)+' pp';
const num=v=>v===null||v===undefined?'—':Number(v).toFixed(6);

function approved(){return state.snapshots.filter(s=>s.state==='done'&&s.approval==='approved'&&s.result&&s.result.value!==null)}
function eligible(s){return approved().filter(o=>o.id!==s.id&&o.result.metric.code===s.result.metric.code&&o.date_from===s.date_from&&o.date_to===s.date_to)}

function picker(){
 let list=approved();let codes=[...new Set(list.map(s=>s.result.metric.code))];
 if(!codes.length)return '<p class="notice">暂无已签核且非 undefined 的快照。先在口径试算完成并签核，再做归因。</p>';
 let left=list.find(s=>s.id===state.left)||list[0];state.left=left.id;
 let rights=eligible(left);
 if(!rights.some(s=>s.id===state.right))state.right=rights[0]?.id||0;
 return `<form id="attribution" class="attr-form">
<label>基线签核<select name="baseline_id">${list.map(s=>`<option value="${s.id}" ${s.id===left.id?'selected':''}>#${s.id} ${esc(s.result.metric.code)} v${s.result.metric.version} · ${s.date_from}—${s.date_to} · ${pct(s.result.value)}</option>`).join('')}</select></label>
<label>当前签核（同指标代码、同日期窗）<select name="current_id">${rights.map(s=>`<option value="${s.id}" ${s.id===state.right?'selected':''}>#${s.id} v${s.result.metric.version} · ${pct(s.result.value)}</option>`).join('')||'<option value="0">没有可比较的同代码签核</option>'}</select></label>
<button class="primary">提交归因（后台计算）</button></form>
<p class="notice">只使用两次签核当时冻结的口径与各班日报，不回读现行口径/事实。贡献按所有替换顺序的边际影响取平均（Shapley），与替换顺序无关，合计等于实际差值；班级多时给出带误差界的估算。任何混搭组合算不出比值时整体失败并指出该组合，不给贡献。</p>`;
}

const scopeLabel=s=>({all_coalitions:'已穷举全部替换组合',structural_cells:'已按行级表达式失败穷举全部组合',mixture_witness:'已穷举全部混搭组合',mixture_witness_partial:'未能穷举全部混搭（分母状态过多）',sampled_coalitions:'抽样访问到的组合'}[s]||s);

function invalidBlock(r){
 return `<div class="notice invalid"><strong>混搭组合无法算出比值，本次不给出贡献。</strong><p>${esc(r.message)}</p>
<p>校验范围：${esc(scopeLabel(r.validity_scope))}；共 ${r.total_coalitions} 组替换组合中 ${r.invalid_coalition_count} 组无法计算${r.invalid_report_truncated?'（下方仅列前若干组）':'（下方列出一组具体组合）'}。</p>
${table(['口径版本','换成当前版的班','原因'],r.invalid_coalitions.map(x=>[x.definition==='current'?'当前':'基线',x.current_classes.join('、')||'无',x.reason]))}</div>`;
}

function basisBlock(r){
 let b=r.basis;let rows=[['基线快照','#'+b.baseline.snapshot_id,'v'+b.baseline.metric_version,b.baseline.date_from+'—'+b.baseline.date_to,b.baseline.facts_rows,pct(b.baseline.value),b.baseline.facts_checksum,b.baseline.approved_by],['当前快照','#'+b.current.snapshot_id,'v'+b.current.metric_version,b.current.date_from+'—'+b.current.date_to,b.current.facts_rows,pct(b.current.value),b.current.facts_checksum,b.current.approved_by]];
 let changed=b.changed_definition_fields;
 let classRows=b.classes.filter(c=>c.changed).map(c=>[c.class_code,c.change.changed_days,c.change.detail_truncated?'是（仅列前20天）':'否',(c.change.details||[]).map(d=>`${d.day}: ${d.change}${d.fields?'（'+d.fields.map(f=>f.field+' '+f.before+'→'+f.after).join('，')+'）':''}`).join('；')]);
 return `<h3>计算依据（只含签核冻结数据）</h3>${table(['签核','快照','口径版本','日期窗','日报行数','比值','日报checksum','签核人'],rows)}
<h4>口径字段变化</h4>${changed.length?table(['字段','基线','当前'],changed.map(c=>[c.field,c.before,c.after])):'<p>口径字段完全一致，差异只来自日报。</p>'}
<p class="muted">其中只有分子、分母及两者的授课方式会改变指标结果，才作为可替换因素；名称和版本号只作依据展示。</p>
<h4>变化的班级日报</h4>${classRows.length?table(['班级','变化天数','是否截断','明细'],classRows):'<p>各班日报完全一致，差异只来自口径。</p>'}
<p class="muted">当前口径：分子 <code>${esc(b.current.metric.numerator)}</code> / 分母 <code>${esc(b.current.metric.denominator)}</code>；方法：${esc(b.method)}</p>`;
}

function resultBlock(r){
 if(r.outcome==='invalid_mix')return invalidBlock(r);
 let rows=r.contributions.filter(c=>c.changed).concat(r.contributions.filter(c=>!c.changed));
 let head='贡献合计 '+pp(r.sum_contributions)+' / 实际差 '+pp(r.delta)+' / 残差 '+num(r.residual)+'；'+(r.exact?'穷举全部 '+r.total_coalitions+' 组替换组合':'固定种子 '+r.seed+' 抽样 '+r.samples+' 次，已访问 '+r.scanned_coalitions+' 组，误差界 95%');
 return `<h3>贡献分解（比值差，单位 pp）</h3><p class="notice">${esc(head)}</p>
${table(['因素','类型','贡献','95%误差界','区间','说明'],rows.map(c=>[c.label,c.kind==='definition'?'口径':'班级日报',pp(c.contribution),c.error_bound?('±'+pp(c.error_bound)):'精确',c.error_bound?pp(c.lower)+' ~ '+pp(c.upper):'—',c.changed?(c.kind==='definition'?'计算口径字段变化':'日报共 '+c.change.changed_days+' 天变化'):'未变化，贡献 0']))}
${r.exact?'<p class="muted">精确模式：因素数不超过上限，已穷举并逐个校验全部替换组合。</p>':'<p class="muted">估算模式：因素较多，用固定种子抽样并以 95% 置信区间给出误差界；端点仍用冻结数据精确重算。</p>'}
${basisBlock(r)}`;
}

function attributionList(){
 if(!state.attributions.length)return '<p>尚无归因。选择两次已签核快照提交。</p>';
 return state.attributions.map(a=>`<article class="rec"><h3>归因 #${a.id} · ${badge(a.state)} ${a.state==='failed'?'<span class="error-inline">'+esc(a.error)+'</span>':''}</h3><p>基线 #${a.baseline_id} → 当前 #${a.current_id}</p>
<button data-attr-view="${a.id}">查看贡献与计算依据</button> ${a.state==='failed'?'<button data-attr-retry="'+a.id+'">重试（不新增归因）</button>':''} ${a.state==='done'?'<button data-attr-export="'+a.id+'">下载归因 JSON</button>':''}</article>`).join('');
}

function detail(){
 let d=state.attrDetail;if(!d)return '';
 if(!d.result)return `<article class="rec"><h3>归因 #${d.id} · ${badge(d.state)}</h3><p class="notice">${esc(d.error||'后台计算中，点击“刷新归因”读取结果。')}</p></article>`;
 let r=d.result;
 return `<article class="rec attr-detail"><h3>归因 #${d.id} 结果 ${badge(r.outcome==='computed'?'已计算':'混搭失败')}</h3>
<p>指标 ${esc(r.metric_code)} · 基线 #${r.baseline.snapshot_id} v${r.baseline.version} ${pct(r.baseline.value)} → 当前 #${r.current.snapshot_id} v${r.current.version} ${pct(r.current.value)} · 差 ${pp(r.delta)}</p>
${resultBlock(r)}</article>`;
}

export function render(){
 return `${detail()}<section class="panel"><h2>业务解释先行，负责人签核冻结</h2><p class="notice">签核锁定该口径版本和快照意见。后续事实修订不改旧快照；需要新口径或新试算。零分母undefined不能签核。</p>${state.snapshots.filter(s=>s.state==='done').map(s=>`<article class="rec" data-history="${s.id}"><h3>#${s.id} ${esc(s.result.metric.name)} · v${s.result.metric.version} ${badge(s.approval)}</h3><p>${s.date_from} — ${s.date_to} / ${s.result.rows} 条事实 / ${s.result.value===null?'undefined':(s.result.value*100).toFixed(2)+'%'}</p>${s.approval==='approved'?`<p>${esc(s.note)}</p><p>签核人 ${esc(s.approved_by)}</p>`:`<form data-note="${s.id}"><label>业务解释与限制<textarea name="note" required minlength="8" rows="2">${esc(s.note)}</textarea></label><button>保存业务解释</button></form>${state.user.role==='admin'?`<button data-approve="${s.id}">负责人签核冻结</button>`:''}`} <button data-export="${s.id}">下载快照 JSON</button></article>`).join('')||'<p>尚无成功试算。先在口径试算提交并完成任务。</p>'}</section><section class="panel"><h2>两次签核归因</h2><p>选同一指标代码、同一日期窗的两次签核，分别替换口径和各班日报来重算指标。</p>${picker()}<p><button id="attr-refresh">刷新归因</button></p><h3>归因记录</h3>${attributionList()}</section>`;
}
