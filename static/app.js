const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const PRIMARY_KEYS=[['product_name','product'],['order_id','order'],['net_settlement','settlement'],['actual_units','units'],['cost','productCost'],['other_cost','otherCost'],['total_cost','totalCost'],['profit','profit'],['status','status']];
const NUMBER_WORDS={zero:0,one:1,two:2,three:3,four:4,five:5,six:6,seven:7,eight:8,nine:9,ten:10};
const SIZE_ORDER={"2XS":10,"XS":20,"S":30,"M":40,"L":50,"XL":60,"2XL":70,"3XL":80,"4XL":90,"5XL":100};
const COLOR_ORDER={cream:10,white:20,black:30,red:40,blue:50,green:60,yellow:70,pink:80,purple:90,brown:100,grey:110,gray:110};
function naturalToken(value){const raw=String(value||'').trim().toUpperCase();if(/^\d+$/.test(raw)){const number=Number(raw);return number<=20?[0,number]:[2,number]}const word=NUMBER_WORDS[raw.toLowerCase()];if(word!==undefined)return [0,word];if(SIZE_ORDER[raw]!==undefined)return [2,SIZE_ORDER[raw]];if(COLOR_ORDER[raw.toLowerCase()]!==undefined)return [1,COLOR_ORDER[raw.toLowerCase()]];return [3,raw]}
function variationSortKey(value){const tokens=String(value||'').split(/[,/|_-]+/).map(x=>x.trim()).filter(Boolean);const ranked=tokens.map(naturalToken);return [...ranked.filter(x=>x[0]===0),...ranked.filter(x=>x[0]===1),...ranked.filter(x=>x[0]===2),...ranked.filter(x=>x[0]===3)].map(x=>`${x[0]}:${String(x[1]).padStart(8,'0')}`).join('|')}
variationSortKey=function(value){const ranked=String(value||'').split(/[,/|_-]+/).map(x=>naturalToken(x.trim())).filter(x=>x[1]!== '');return [...ranked.filter(x=>x[0]!==2),...ranked.filter(x=>x[0]===2)].map(x=>`${x[0]}:${String(x[1]).padStart(8,'0')}`).join('|')}
// One catalog shared with server exports; no layered language overrides.
const LANG=window.MERCHANT_I18N.messages;
const LANGUAGE_STORAGE_KEY='merchant-language-v2';
let currentLang=Object.hasOwn(LANG,localStorage.getItem(LANGUAGE_STORAGE_KEY))?localStorage.getItem(LANGUAGE_STORAGE_KEY):'en';
let latestManagerItems=null,auditOrderId=null;
function t(key,...args){const value=LANG[currentLang]?.[key]??LANG.en[key]??key;return value.replace(/\{(\d+)\}/g,(match,index)=>args[index]??match)}
function systemText(value,lang=currentLang){
  const source=String(value??'');
  if(source.includes('；')&&!window.MERCHANT_I18N.system.some(entry=>entry.zh===source))return source.split('；').map(part=>systemText(part,lang)).join(lang==='zh'?'；':'; ');
  for(const entry of window.MERCHANT_I18N.system){
    const pattern=entry.zh.split(/(\{\d+\})/).map(part=>/^\{\d+\}$/.test(part)?'([\\s\\S]*?)':part.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('');
    const match=source.match(new RegExp('^'+pattern+'$'));
    if(match)return entry[lang].replace(/\{(\d+)\}/g,(_,index)=>match[Number(index)+1]);
  }
  return source.includes('；')?source.split('；').map(part=>systemText(part,lang)).join(lang==='zh'?'；':'; '):source;
}
function draftKey(el,index){
  const row=el.closest('tr'),group=el.closest('.cost-group');
  if(row?.dataset.order)return JSON.stringify(['order',row.dataset.order,el.tagName,el.className]);
  if(group)return JSON.stringify(['cost',group.querySelector('input.product-name')?.value,row?.dataset.sku||'',el.tagName,el.className,el.id]);
  return 'position:'+index;
}
function captureDrafts(root){
  return new Map([...root.querySelectorAll('input,button,details,.manager-group')].map((el,index)=>[draftKey(el,index),{value:el.value,checked:el.checked,disabled:el.disabled,open:el.open,hidden:el.hidden}]));
}
function restoreDrafts(root,drafts){
  [...root.querySelectorAll('input,button,details,.manager-group')].forEach((el,index)=>{const draft=drafts.get(draftKey(el,index));if(!draft)return;for(const key of ['value','checked','disabled','open','hidden'])if(draft[key]!==undefined)el[key]=draft[key]});
}
function applyLanguage(){
  document.documentElement.lang=currentLang==='zh'?'zh-Hans':currentLang;
  document.title=t('appTitle');
  document.querySelectorAll('[data-i18n]').forEach(el=>{const label=t(el.dataset.i18n),textNode=[...el.childNodes].find(node=>node.nodeType===Node.TEXT_NODE&&node.textContent.trim());if(el.children.length&&textNode)textNode.textContent=label;else if(el.children.length)el.insertBefore(document.createTextNode(label),el.firstChild);else el.textContent=label});
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el=>el.placeholder=t(el.dataset.i18nPlaceholder));
  document.querySelectorAll('[data-empty-file]').forEach(el=>{if(!el.dataset.selected)el.textContent=t(el.dataset.emptyFile)});
  $('#language-select').value=currentLang;
  if(latestResult){
    const orders=captureDrafts($('#orders')),costs=captureDrafts($('#missing'));
    const previews=new Map([...document.querySelectorAll('#order-rows tr')].map(row=>[row.dataset.order,[...row.querySelectorAll('td[data-key="total_cost"],td[data-key="profit"]')].map(cell=>cell.textContent)]));
    const scroll=$('.order-table-wrap');const position=scroll?[scroll.scrollLeft,scroll.scrollTop]:[0,0];
    renderResult(latestResult,true);
    restoreDrafts($('#orders'),orders);restoreDrafts($('#missing'),costs);
    document.querySelectorAll('#order-rows tr').forEach(row=>row.querySelectorAll('td[data-key="total_cost"],td[data-key="profit"]').forEach((cell,col)=>{const saved=previews.get(row.dataset.order);if(saved?.[col]!==undefined)(cell.querySelector('.cell-content')||cell).textContent=saved[col]}));
    $('.order-table-wrap').scrollLeft=position[0];$('.order-table-wrap').scrollTop=position[1];
  }
  if(latestManagerItems){const drafts=captureDrafts($('#cost-manager'));$('#cost-manager').innerHTML=costManager(latestManagerItems);restoreDrafts($('#cost-manager'),drafts)}
  if(!$('#cost-manager').hidden)$('#open-cost-manager').textContent=t('collapseCosts');
  if($('#audit-dialog').open)renderAudit(auditOrderId);
}
$('#language-select').addEventListener('change',e=>{
  if(pendingWrites||reportDownloadPending){e.target.value=currentLang;alert(t('busy'));return}
  const previous=currentLang;currentLang=e.target.value;
  for(const key of ['status','flags'])if(valueFilters[key])valueFilters[key]=valueFilters[key].map(label=>{
    const item=latestResult?.orders.find(item=>(key==='status'?systemText(item.status,previous):systemText(item.flags,previous))===label);
    return item?systemText(item[key]):label;
  });
  localStorage.setItem(LANGUAGE_STORAGE_KEY,currentLang);applyLanguage();
});
const ROW_MIN_HEIGHT=34;
let summaryFilter='';
let reportCurrency='MYR',latestResult=null,columns=[],visible=new Set(['product_name','order_id','net_settlement','actual_units','cost','total_cost','profit','status','variation']),valueFilters={},textFilters={},sortKey='',sortDirection=1,columnWidths=JSON.parse(localStorage.getItem('profit-column-widths')||'{}'),rowHeights=JSON.parse(localStorage.getItem('profit-row-heights-v2')||'{}'),resizeState=null,rowResizeState=null;
applyLanguage();
document.addEventListener('change',e=>{if(!e.target.matches('input[type=file]'))return;const name=e.target.parentElement?.querySelector('.file-name');if(name){name.dataset.selected='1';name.textContent=e.target.files?.[0]?.name||t('fileNone')}});
let pendingWrites=0,reportDownloadPending=false,reportOutOfDate=false;
async function post(url,payload,isForm=false){
  if(pendingWrites||reportDownloadPending)throw Error(t('busy'));
  pendingWrites++;
  reportOutOfDate=true;
  try{
    if(!isForm&&latestResult?.revision)payload={...payload,expected_revision:latestResult.revision};
    const r=await fetch(url,{method:'POST',body:isForm?payload:JSON.stringify(payload),headers:{'X-Report-Language':currentLang,...(isForm?{}:{'Content-Type':'application/json'})}});
    const raw=await r.text();let j;
    try{j=raw?JSON.parse(raw):null}catch(_){j=null}
    if(!r.ok)throw Error(j?.error||t('httpFailure',r.status));
    if(!j)throw Error(t('emptyResponse'));
    return j;
  }finally{pendingWrites--}
}
async function downloadCurrentReport(event){
  event.preventDefault();
  if(pendingWrites||reportDownloadPending){alert(t('busyDownload'));return}
  if(reportOutOfDate||!latestResult?.revision){alert(t('unconfirmedDownload'));return}
  // Unsaved order edits must never look like part of the downloaded report.
  if([...document.querySelectorAll('#orders input[type=number]')].some(input=>input.value!==input.defaultValue)){
    alert(t('unsavedDownload'));return;
  }
  reportDownloadPending=true;
  const link=event.currentTarget;
  try{
    const response=await fetch(link.href,{cache:'no-store',headers:{'X-Report-Language':currentLang}});
    if(!response.ok){
      const error=await response.json().catch(()=>({}));
      reportOutOfDate=true;
      throw Error(error.error||t('downloadFailure'));
    }
    const blob=await response.blob(),objectUrl=URL.createObjectURL(blob),anchor=document.createElement('a');
    anchor.href=objectUrl;
    anchor.download=response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1]||(link.id==='download'?'report.xlsx':'report.csv');
    document.body.appendChild(anchor);anchor.click();anchor.remove();
    setTimeout(()=>URL.revokeObjectURL(objectUrl),1000);
  }catch(error){alert(error.message)}
  finally{reportDownloadPending=false}
}
$('#download').addEventListener('click',downloadCurrentReport);
$('#download-csv').addEventListener('click',downloadCurrentReport);
function statusLabel(item){return item.status?systemText(item.status):t(`status_${item.status_code||'confirmed'}`)}
function fieldValue(item,key){if(key.startsWith('source:'))return item.source?.[key.slice(7)]??'';if(key==='profit')return item.profit===null?'':item.profit.toFixed(2);if(['net_settlement','cost','other_cost','total_cost'].includes(key))return Number(item[key]??0).toFixed(2);if(key==='status')return statusLabel(item);if(key==='flags'||key==='loss_label')return systemText(item[key]);return item[key]??''}
function display(value){return esc(value).replace(/\n/g,'<br>')}
function buildColumns(result){const base=PRIMARY_KEYS.map(([key,labelKey])=>({key,label:t(labelKey),required:false}));const extras=[['variation',t('variation')],['details_units',t('units')],['flags',t('reviewNotes')],['transaction_types',t('transactionType')]].map(([key,label])=>({key,label,required:false}));const source=(result.source_columns||[]).map(label=>({key:`source:${label}`,label:label,required:false}));const seen=new Set;columns=[...base,...extras,...source].filter(x=>!seen.has(x.key)&&seen.add(x.key));const saved=JSON.parse(localStorage.getItem('profit-visible-columns')||'[]');if(saved.length)visible=new Set(saved.filter(key=>columns.some(c=>c.key===key)));for(const key of ['product_name','order_id','net_settlement','actual_units','cost','total_cost','profit','status','variation'])visible.add(key);if(![...visible].some(key=>columns.some(c=>c.key===key))&&columns[0])visible.add(columns[0].key)}
function defaultWidth(key){return key==='product_name'?250:key==='order_id'?165:['net_settlement','cost','other_cost','total_cost'].includes(key)?110:key==='actual_units'?75:key==='status'?120:key==='variation'?140:100}
function header(column){const marker=sortKey===column.key?(sortDirection===1?'↑':'↓'):'↕',active=(valueFilters[column.key]?.length||textFilters[column.key])?' active-filter':'';return`<th data-key="${esc(column.key)}" style="position:relative"><span>${esc(column.label)}</span><button type="button" class="sort-toggle" data-key="${esc(column.key)}" title="${t('sortTitle')}">${marker}</button><button type="button" class="filter-toggle${active}" data-key="${esc(column.key)}" title="${t('filterTitle')}">⌄</button><span class="column-resizer" data-key="${esc(column.key)}" title="${t('orderResultsHint')}" style="position:absolute;right:-4px;top:0;width:8px;height:100%;cursor:col-resize;z-index:3"></span></th>`}
function cell(item,column){const key=column.key,value=fieldValue(item,key);if(key==='net_settlement'||key==='cost'||key==='other_cost'){const disabled=item.editable?'':'disabled',klass=key==='net_settlement'?'edit-net':key==='cost'?'edit-cost':'edit-other';return`<td data-key="${esc(key)}"><input class="${klass}" type="number" min="0" step="0.01" value="${esc(value)}" ${disabled}></td>`}if(key==='order_id')return`<td data-key="order_id"><button type="button" class="show-audit" data-order="${esc(item.order_id)}" style="background:none;color:inherit;padding:0;text-align:left">${esc(value)}</button></td>`;if(key==='product_name'){const fullName=String(value||'');const rendered=fullName.split(/\r?\n/).filter(name=>name.trim()).map(name=>`<div class="product-name">${esc(name)}</div>`).join('');return`<td data-key="${esc(key)}" class="product-cell"><div class="product-text" tabindex="0" data-full-name="${esc(fullName)}" aria-label="${esc(fullName)}">${rendered||'—'}</div></td>`}return`<td data-key="${esc(key)}"><div class="cell-content${key==='status'?' status-'+esc(item.status_code):''}">${display(value)||'—'}</div></td>`}
function filteredItems(){if(!latestResult)return[];let items=latestResult.orders.map((item,index)=>({item,index})).filter(({item})=>{if(summaryFilter==='loss'&&!item.is_loss)return false;if(summaryFilter==='needs_review'&&item.status_code!=='needs_review')return false;if(Object.entries(textFilters).some(([key,needle])=>needle&&!String(fieldValue(item,key)).toLowerCase().includes(needle)))return false;if(Object.entries(valueFilters).some(([key,chosen])=>chosen&&!chosen.includes(String(fieldValue(item,key)))))return false;return true});if(sortKey)items.sort((a,b)=>{const av=fieldValue(a.item,sortKey),bv=fieldValue(b.item,sortKey);const an=Number(av),bn=Number(bv);const compared=Number.isFinite(an)&&Number.isFinite(bn)&&String(av).trim()!==''&&String(bv).trim()!==''?an-bn:String(av).localeCompare(String(bv),'zh-Hans');return compared*sortDirection});return items}
  function renderRows(){const shown=columns.filter(c=>visible.has(c.key));const body=$('#order-rows');if(body)body.innerHTML=filteredItems().map(({item,index})=>{const height=Math.max(ROW_MIN_HEIGHT,Number(rowHeights[item.order_id])||ROW_MIN_HEIGHT),expanded=height>ROW_MIN_HEIGHT;return`<tr data-index="${index}" data-order="${esc(item.order_id)}" style="height:${height}px;--row-height:${height}px"${expanded?' class="row-expanded"':''}>${shown.map(c=>cell(item,c)).join('')}<td class="operation-cell">${item.editable?`<button class="save-order">${t('save')}</button>`:'—'}</td></tr>`}).join('')}
function renderOrders(){const shown=columns.filter(c=>visible.has(c.key));const menu=columns.map(c=>`<label><input type="checkbox" class="column-choice" data-key="${esc(c.key)}" ${visible.has(c.key)?'checked':''}>${esc(c.label)}</label>`).join(''),widths=shown.map(c=>`<col data-key="${esc(c.key)}" style="width:${columnWidths[c.key]||defaultWidth(c.key)}px">`).join('');$('#orders').innerHTML=`<div class="order-heading"><div><h3>${t('orderResults')}</h3><p class="sub">${t('orderResultsHint')}</p></div><div class="column-options"><button type="button" id="column-options">${t('displayColumns')}</button><div id="column-menu" hidden>${menu}</div></div></div><div class="order-table-wrap"><table style="table-layout:fixed"><colgroup>${widths}<col data-key="operation" style="width:85px"></colgroup><thead><tr>${shown.map(header).join('')}<th class="operation-cell"><span class="operation-label">${t('operation')}</span></th></tr></thead><tbody id="order-rows"></tbody></table></div>`;renderRows()}
function applyFilters(){renderRows()}
function filterMenu(key){const values=[...new Set(latestResult.orders.map(item=>String(fieldValue(item,key))))].sort((a,b)=>a.localeCompare(b,'en',{numeric:true})),previousSearch=textFilters[key]||'',canSearchDirect=['product_name','order_id'].includes(key),placeholder=canSearchDirect?t('filterSearchProduct'):t('filterSearchOption');const selected=new Set(valueFilters[key]||values);return`<div id="filter-menu" class="filter-menu" data-key="${esc(key)}" data-previous-search="${esc(previousSearch)}"><input id="filter-search" value="" placeholder="${placeholder}"><label><input id="filter-all" type="checkbox" ${selected.size===values.length?'checked':''}>${t('selectAll')}</label><div id="filter-values">${values.map(value=>`<label data-value="${esc(value)}"><input class="filter-choice" type="checkbox" value="${esc(value)}" ${selected.has(value)?'checked':''}>${display(value)||'—'}</label>`).join('')}</div><div class="filter-actions"><button type="button" id="filter-clear">${t('clearFilter')}</button><button type="button" id="filter-cancel">${t('cancel')}</button><button type="button" id="filter-apply">${t('apply')}</button></div></div>`}
function openFilter(button){$('#filter-menu')?.remove();$('#orders').insertAdjacentHTML('beforeend',filterMenu(button.dataset.key));const menu=$('#filter-menu'),rect=button.getBoundingClientRect();menu.style.top=`${rect.bottom+window.scrollY+4}px`;menu.style.left=`${Math.max(8,rect.left+window.scrollX-180)}px`;$('#filter-search').focus()}
function renderResult(d,preserveView=false){latestResult=d;if(!preserveView)reportOutOfDate=false;$('#download').href='/api/download/latest?revision='+encodeURIComponent(d.revision||'');$('#download-csv').href='/api/download/csv?revision='+encodeURIComponent(d.revision||'');reportCurrency=d.summary.currency||'MYR';if(!preserveView){valueFilters={};textFilters={};sortKey='';sortDirection=1;buildColumns(d)}else{const selected=visible;buildColumns(d);visible=selected}const currency=d.summary.currency||'';$('#summary').innerHTML=`<div class="cards"><b>${t('summarySettlement')}<br>${currency} ${d.summary.settlement_total.toFixed(2)}</b><b>${t('summaryCost')}<br>${currency} ${d.summary.confirmed_cost.toFixed(2)}</b><b>${t('summaryProfit')}<br>${d.summary.confirmed_orders===0?'—':currency+' '+d.summary.confirmed_profit.toFixed(2)}<small>${d.summary.confirmed_orders===0?t('noConfirmedProfit'):d.summary.needs_review?t('partialProfit'):''}</small></b><b class="summary-filter" role="button" tabindex="0" data-summary-filter="" aria-pressed="${summaryFilter===''}">${t('summaryOrders')}<br>${d.summary.orders}</b><b class="summary-filter" role="button" tabindex="0" data-summary-filter="needs_review" aria-pressed="${summaryFilter==='needs_review'}" data-status-code="needs_review" title="${t('summaryNeedsReview')}">${t('summaryNeedsReview')}<br>${d.summary.needs_review}</b><b class="summary-filter" role="button" tabindex="0" data-summary-filter="loss" aria-pressed="${summaryFilter==='loss'}">${t('lossOrders')}<br>${d.summary.loss_orders??0}<small>${t('confirmedLoss')}: ${currency} ${Number(d.summary.confirmed_loss_total??0).toFixed(2)}</small><small>${t('pendingNegative')}: ${currency} ${Number(d.summary.pending_negative_total??0).toFixed(2)}<br>${t('pendingNegativeHint')}</small></b></div>`;$('#download').hidden=false;$('#download-csv').hidden=false;$('#missing').innerHTML=groupedCosts(d.cost_catalog||d.missing_costs);renderOrders()}
function groupedCosts(items){if(!items.length)return'';const groups={};[...items].sort((a,b)=>variationSortKey(a.variation).localeCompare(variationSortKey(b.variation),'en',{numeric:true})).forEach(x=>(groups[x.product_name]??=[]).push(x));return`<h3>${t('missingHeading')}</h3><p>${t('missingHintSimple')}<br>${t('missingHintSelect')}</p>${Object.entries(groups).map(([name,rows])=>{const defaultAmount=rows.find(x=>x.amount!==''&&x.amount!==null&&x.amount!==undefined)?.amount??'';return`<details class="cost-group" open><summary>${esc(name)}</summary><div class="default-cost"><input class="product-name" type="hidden" value="${esc(name)}"><label>${t('simpleMode')}<input class="product-amount" type="number" min="0" step="0.01" value="${esc(defaultAmount)}" placeholder="8.40"></label><button class="save-product-cost">${t('syncAll')}</button><button type="button" class="confirm-product-cost" disabled>${t('apply')}</button></div><div class="selected-cost"><label>${t('selectedMode')}<input class="selected-amount" type="number" min="0" step="0.01" placeholder="8.90"></label><button class="save-selected-cost">${t('syncSelected')}</button><button type="button" class="confirm-selected-cost" disabled>${t('apply')}</button></div><table><tr><th>${t('select')}</th><th>${t('variation')}</th><th>${t('soldThisPeriod')}</th><th>${t('specialCost')}</th><th></th></tr>${rows.map(x=>`<tr data-sku="${esc(x.sku_id)}"><td><input class="variation-pick" type="checkbox"></td><td>${esc(x.variation)}</td><td>${x.quantity}</td><td><input class="variation-amount" type="number" min="0" step="0.01" value="${esc(x.amount??'')}" placeholder="8.90"></td><td><button class="save-variation-cost">${t('saveSpecialCost')}</button></td></tr>`).join('')}</table></details>`}).join('')}`}
$('#analyse').onsubmit=async e=>{e.preventDefault();const summary=$('#summary');summary.textContent=t('loadingAnalysis');try{const result=await post('/api/analyse',new FormData(e.target),true);summaryFilter='';renderResult(result)}catch(err){summary.textContent=err.message}};
$('#cost-table').onsubmit=async e=>{e.preventDefault();const output=$('#cost-table-result');output.textContent=t('saving');try{const result=await post('/api/cost-table',new FormData(e.target),true);output.textContent=t('costTableDone',result.applied,result.stored_for_later,result.skipped,result.conflicts);if(result.result)renderResult(result.result)}catch(err){output.textContent=err.message}};
function renderAudit(orderId){const item=latestResult.orders.find(x=>x.order_id===orderId);if(!item)return;const entries=item.settlement_entries||[];$('#audit-content').textContent=[item.order_id,...entries.map(x=>t('auditLine',x.sheet||'CSV',x.row,x.description,Number(x.amount).toFixed(2))),t('auditOriginal')+': '+entries.reduce((sum,x)=>sum+Number(x.amount),0).toFixed(2),t('auditReport')+': '+Number(item.net_settlement).toFixed(2)].join('\n')}
$('#orders').onclick=async e=>{const auditButton=e.target.closest('.show-audit');if(auditButton){auditOrderId=auditButton.dataset.order;renderAudit(auditOrderId);$('#audit-dialog').showModal();return}if(e.target.id==='column-options'){const menu=$('#column-menu');menu.hidden=!menu.hidden;return}if(e.target.classList.contains('sort-toggle')){const key=e.target.dataset.key;if(sortKey!==key){sortKey=key;sortDirection=1}else if(sortDirection===1){sortDirection=-1}else{sortKey='';sortDirection=1}renderOrders();return}if(e.target.classList.contains('filter-toggle')){openFilter(e.target);return}if(e.target.id==='filter-clear'){const key=$('#filter-menu').dataset.key;delete valueFilters[key];delete textFilters[key];$('#filter-menu').remove();renderRows();return}if(e.target.id==='filter-cancel'){const menu=$('#filter-menu'),key=menu.dataset.key,previous=menu.dataset.previousSearch;if(previous)textFilters[key]=previous;else delete textFilters[key];menu.remove();renderRows();return}if(e.target.id==='filter-apply'){const menu=$('#filter-menu'),key=menu.dataset.key,search=menu.querySelector('#filter-search').value.trim().toLowerCase(),choices=[...menu.querySelectorAll('.filter-choice')],selected=choices.filter(x=>x.checked).map(x=>x.value),all=choices.length>0&&selected.length===choices.length;if(search){delete valueFilters[key];textFilters[key]=search}else if(all){delete valueFilters[key];delete textFilters[key]}else{valueFilters[key]=selected;delete textFilters[key]}menu.remove();renderRows();return}if(!e.target.classList.contains('save-order'))return;const row=e.target.closest('tr');e.target.disabled=true;e.target.textContent=t('saving');try{const original=latestResult.orders.find(item=>item.order_id===row.dataset.order),payload={order_id:row.dataset.order};for(const [key,selector] of [['net_settlement','.edit-net'],['cost','.edit-cost'],['other_cost','.edit-other']]){const input=row.querySelector(selector);if(input&&(input.value===''||Number(input.value)!==Number(original[key]??0)))payload[key]=input.value}renderResult(await post('/api/order-override',payload))}catch(err){alert(err.message);e.target.disabled=false;e.target.textContent=t('save')}};
$('#orders').oninput=e=>{if(e.target.matches('.edit-cost,.edit-net,.edit-other')){const row=e.target.closest('tr'),net=Number(row.querySelector('.edit-net')?.value),cost=Number(row.querySelector('.edit-cost')?.value),other=Number(row.querySelector('.edit-other')?.value||0),totalCell=row.querySelector('td[data-key="total_cost"]'),profitCell=row.querySelector('td[data-key="profit"]'),total=cost+other;if(totalCell&&Number.isFinite(total))(totalCell.querySelector('.cell-content')||totalCell).textContent=total.toFixed(2);if(profitCell&&Number.isFinite(net)&&Number.isFinite(total))(profitCell.querySelector('.cell-content')||profitCell).textContent=(net-total).toFixed(2);return}if(e.target.id!=='filter-search')return;const needle=e.target.value.trim().toLowerCase(),key=$('#filter-menu').dataset.key,labels=[...document.querySelectorAll('#filter-values label')];labels.forEach(label=>{const match=!needle||label.textContent.toLowerCase().includes(needle);label.hidden=!match;const choice=label.querySelector('.filter-choice');if(choice&&needle)choice.checked=match});const menu=$('#filter-menu'),choices=[...menu.querySelectorAll('.filter-choice')];menu.querySelector('#filter-all').checked=choices.length>0&&choices.every(x=>x.checked);if(['product_name','order_id'].includes(key)){if(needle)textFilters[key]=needle;else delete textFilters[key];renderRows()}};
$('#orders').onchange=e=>{if(e.target.id==='filter-all'){document.querySelectorAll('.filter-choice').forEach(x=>x.checked=e.target.checked);return}if(e.target.classList.contains('filter-choice')){const menu=e.target.closest('#filter-menu');if(menu){const choices=[...menu.querySelectorAll('.filter-choice')];menu.querySelector('#filter-all').checked=choices.length>0&&choices.every(x=>x.checked)}return}if(!e.target.classList.contains('column-choice'))return;const key=e.target.dataset.key;if(e.target.checked)visible.add(key);else visible.delete(key);if(!visible.size){visible.add(key);e.target.checked=true;return}localStorage.setItem('profit-visible-columns',JSON.stringify([...visible]));renderOrders();$('#column-menu').hidden=false};
$('#summary').onclick=e=>{const card=e.target.closest('.summary-filter');if(!card||!latestResult)return;summaryFilter=card.dataset.summaryFilter||'';valueFilters={};textFilters={};document.querySelectorAll('#summary .summary-filter').forEach(item=>item.setAttribute('aria-pressed',String((item.dataset.summaryFilter||'')===summaryFilter)));renderOrders();$('#orders').scrollIntoView({behavior:'smooth',block:'start'})};
$('#summary').addEventListener('keydown',e=>{const card=e.target.closest('.summary-filter');if(card&&(e.key==='Enter'||e.key===' ')){e.preventDefault();card.click()}});
function setRowHeight(row,height){height=Math.max(ROW_MIN_HEIGHT,height);row.style.height=`${height}px`;row.style.setProperty('--row-height',`${height}px`);row.querySelectorAll('td').forEach(cell=>{cell.style.height=`${height}px`});row.classList.toggle('row-expanded',height>ROW_MIN_HEIGHT)}
function finishResize(){if(resizeState){localStorage.setItem('profit-column-widths',JSON.stringify(columnWidths));resizeState=null}if(rowResizeState){const height=Math.max(ROW_MIN_HEIGHT,Math.round(rowResizeState.row.getBoundingClientRect().height));rowHeights[rowResizeState.order]=height;localStorage.setItem('profit-row-heights-v2',JSON.stringify(rowHeights));rowResizeState=null;document.body.classList.remove('row-resizing')}}
document.addEventListener('pointerdown',e=>{const menu=$('#filter-menu');if(menu&&!menu.contains(e.target)&&!e.target.classList.contains('filter-toggle')){const key=menu.dataset.key;delete textFilters[key];delete valueFilters[key];menu.remove();renderRows()}if(e.target.classList.contains('column-resizer')){const th=e.target.closest('th');resizeState={key:e.target.dataset.key,startX:e.clientX,startWidth:th.getBoundingClientRect().width};e.target.setPointerCapture?.(e.pointerId);e.preventDefault();return}const row=e.target.closest('#order-rows tr');if(row){const rect=row.getBoundingClientRect();if(e.clientY>=rect.bottom-12){rowResizeState={order:row.dataset.order,startY:e.clientY,startHeight:rect.height,row};try{e.target.setPointerCapture?.(e.pointerId)}catch(_){/* pointer capture is optional */}document.body.classList.add('row-resizing');e.preventDefault()}}});
document.addEventListener('pointermove',e=>{if(resizeState){const width=Math.max(70,Math.round(resizeState.startWidth+e.clientX-resizeState.startX));columnWidths[resizeState.key]=width;document.querySelectorAll('col[data-key]').forEach(col=>{if(col.dataset.key===resizeState.key)col.style.width=`${width}px`})}if(rowResizeState){const height=Math.max(ROW_MIN_HEIGHT,Math.round(rowResizeState.startHeight+e.clientY-rowResizeState.startY));setRowHeight(rowResizeState.row,height)}});
window.addEventListener('pointerup',finishResize);window.addEventListener('pointercancel',finishResize);
// One delegated tooltip, outside the table so it cannot affect row height.
const productTooltip=document.createElement('div');
productTooltip.id='product-name-tooltip';
productTooltip.className='product-name-tooltip';
productTooltip.setAttribute('role','tooltip');
productTooltip.hidden=true;
document.body.appendChild(productTooltip);
let productTooltipTarget=null;
function hideProductTooltip(){
  productTooltipTarget?.removeAttribute('aria-describedby');
  productTooltipTarget=null;
  productTooltip.hidden=true;
}
function showProductTooltip(target){
  if(!target||rowResizeState||resizeState)return;
  hideProductTooltip();
  productTooltipTarget=target;
  productTooltip.textContent=target.dataset.fullName||'—';
  target.setAttribute('aria-describedby',productTooltip.id);
  productTooltip.hidden=false;
  productTooltip.style.left='8px';
  productTooltip.style.top='8px';
  const rect=target.getBoundingClientRect(),tip=productTooltip.getBoundingClientRect();
  productTooltip.style.left=`${Math.max(8,Math.min(rect.left,window.innerWidth-tip.width-8))}px`;
  const top=rect.top-tip.height-8;
  productTooltip.style.top=`${Math.max(8,top>=8?top:Math.min(rect.bottom+8,window.innerHeight-tip.height-8))}px`;
}
$('#orders').addEventListener('pointerover',e=>{
  const target=e.target.closest('.product-text');
  if(target&&!target.contains(e.relatedTarget))showProductTooltip(target);
});
$('#orders').addEventListener('pointerout',e=>{
  if(productTooltipTarget&&!productTooltipTarget.contains(e.relatedTarget))hideProductTooltip();
});
$('#orders').addEventListener('focusin',e=>showProductTooltip(e.target.closest('.product-text')));
$('#orders').addEventListener('focusout',hideProductTooltip);
document.addEventListener('pointerdown',hideProductTooltip,true);
document.addEventListener('scroll',hideProductTooltip,true);
document.addEventListener('keydown',e=>{if(e.key==='Escape')hideProductTooltip()});
window.addEventListener('blur',hideProductTooltip);
window.addEventListener('resize',hideProductTooltip);
new MutationObserver(hideProductTooltip).observe($('#orders'),{childList:true,subtree:true});
$('#cost').onsubmit=async e=>{e.preventDefault();try{const payload=Object.fromEntries(new FormData(e.target));payload.currency=payload.currency||reportCurrency;const result=await post('/api/cost',payload);if(result.result)renderResult(result.result);alert(t('costSaved'))}catch(err){alert(err.message)}};
// One delegated cost handler only. Applying a product cost updates every
// visible variation immediately; the server keeps the product default atomic.
$('#missing').onclick=async e=>{const button=e.target.closest('button');if(!button)return;const group=button.closest('.cost-group'),effective_from=$('#global-effective-date').value;if(button.classList.contains('save-product-cost')){const amount=group.querySelector('.product-amount').value;group.querySelectorAll('.variation-amount').forEach(input=>{input.value=amount});group.querySelector('.confirm-product-cost').disabled=false;return}if(button.classList.contains('save-selected-cost')){const rows=[...group.querySelectorAll('.variation-pick:checked')].map(x=>x.closest('tr'));if(!rows.length){alert(t('missingHintSelect'));return}const amount=group.querySelector('.selected-amount').value;rows.forEach(row=>{row.querySelector('.variation-amount').value=amount});group.querySelector('.confirm-selected-cost').disabled=false;return}let response;if(button.classList.contains('confirm-product-cost')){const amount=group.querySelector('.product-amount').value;button.disabled=true;try{response=await post('/api/product-cost',{product_name:group.querySelector('.product-name').value,amount,effective_from,currency:reportCurrency});button.textContent=t('saved')}catch(err){alert(err.message);button.disabled=false}}else if(button.classList.contains('confirm-selected-cost')){const rows=[...group.querySelectorAll('.variation-pick:checked')].map(x=>x.closest('tr'));const amount=group.querySelector('.selected-amount').value;button.disabled=true;try{response=await post('/api/cost-batch',{sku_ids:rows.map(row=>row.dataset.sku),amount,effective_from,currency:reportCurrency,note:'selected'});button.textContent=t('saved')}catch(err){alert(err.message);button.disabled=false}}else if(button.classList.contains('save-variation-cost')){const row=button.closest('tr'),amount=row.querySelector('.variation-amount').value;try{response=await post('/api/cost',{sku_id:row.dataset.sku,amount,effective_from,currency:reportCurrency,note:'variation override'});button.textContent=t('saved')}catch(err){alert(err.message)}}else return;if(response?.result)renderResult(response.result)};
$('#retime-costs').onclick=async()=>{const effective_from=$('#global-effective-date').value;const question=t('retimeConfirm').replace('{date}',effective_from);if(!confirm(question))return;try{const r=await post('/api/retime-costs',{effective_from});if(r.result)renderResult(r.result);alert(t('retimeDone').replace('{count}',r.count))}catch(err){alert(err.message)}};
function costManager(items){latestManagerItems=items;const groups={};items.forEach(item=>(groups[item.cost_product_name]??=[]).push(item));return`<input id="cost-manager-search" placeholder="${t('filterSearchOption')}">${Object.entries(groups).map(([raw,rows])=>{const title=rows[0].product_name,defaultAmount=rows[0].default_amount;return`<section class="cost-group manager-group"><h4>${esc(title)}</h4><div class="default-cost"><input class="product-name" type="hidden" value="${esc(raw)}"><label>${t('simpleMode')}<input class="product-amount" type="number" min="0" step="0.01" value="${esc(defaultAmount)}"></label><button class="save-product-cost">${t('updateAll')}</button></div><table><tr><th>${t('variation')}</th><th>SKU ID</th><th>${t('specialCost')}</th><th></th></tr>${rows.map(x=>`<tr data-sku="${esc(x.sku_id)}"><td>${esc(x.variation)}</td><td>${esc(x.sku_id)}</td><td><input class="variation-amount" type="number" min="0" step="0.01" value="${esc(x.sku_amount)}"></td><td><button class="save-variation-cost">${t('save')}</button></td></tr>`).join('')}</table></section>`}).join('')}`}
$('#open-cost-manager').onclick=async()=>{const box=$('#cost-manager'),button=$('#open-cost-manager');if(!box.hidden){box.hidden=true;button.textContent=t('openSavedCosts');return}box.hidden=false;button.textContent=t('collapseCosts');box.textContent=t('loadingCosts');try{box.innerHTML=costManager((await fetch('/api/cost-catalog').then(r=>r.json())).items)}catch(err){box.textContent=err.message}};
$('#cost-manager').oninput=e=>{if(e.target.id!=='cost-manager-search')return;const needle=e.target.value.toLowerCase();document.querySelectorAll('#cost-manager .manager-group').forEach(group=>group.hidden=!group.textContent.toLowerCase().includes(needle))};
$('#cost-manager').onclick=async e=>{try{const group=e.target.closest('.manager-group'),effective_from=$('#global-effective-date').value;let response;if(e.target.classList.contains('save-product-cost')){const amount=group.querySelector('.product-amount').value;response=await post('/api/product-cost',{product_name:group.querySelector('.product-name').value,amount,effective_from,currency:reportCurrency});e.target.textContent=t('saved')}else if(e.target.classList.contains('save-variation-cost')){const row=e.target.closest('tr'),amount=row.querySelector('.variation-amount').value;response=await post('/api/cost',{sku_id:row.dataset.sku,amount,effective_from,currency:reportCurrency,note:'variation override'});e.target.textContent=t('saved')}else return;if(response?.result)renderResult(response.result);alert(t('costSaved'))}catch(err){alert(err.message)}};
$('#name-replace').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');if(button){button.disabled=true;button.textContent=t('saving')}try{const d=await post('/api/name-replace',Object.fromEntries(new FormData(e.target)));if(d.result)renderResult(d.result);alert(t('replaceDone'))}catch(err){alert(err.message)}finally{if(button){button.disabled=false;button.textContent=t('replaceButton')}}};
