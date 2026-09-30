'use strict';
const $ = s => document.querySelector(s);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {user:null, tables:[], page:'overview', table:null, offset:0, search:'', searchField:'', socket:null, org:null};
const titles = {organizations:['Organizações','Cada organização tem as próprias tabelas, usuários, tokens e storage.'],security:['Segurança da conta','Senha, autenticação em duas etapas e sessões.'],overview:['Visão geral','Acompanhe a plataforma e as organizações.'],tables:['Tabelas e API','Estruture seus dados. A API acompanha cada alteração.'],users:['Usuários','Controle quem pode acessar esta organização.'],admins:['Administradores','Contas globais que administram a plataforma.'],tokens:['Tokens de acesso','Permissões explícitas para cada integração.'],storages:['Storage e arquivos','Conecte os provedores desta organização e organize os arquivos.'],backups:['Backups','Cópias criptografadas, agendadas e com retenção de 1 a 7 cópias.'],audit:['Atividade','Histórico das operações realizadas pela API.'],export:['Exportar dados','Baixe todos os registros da organização em XLSX, CSV ou JSON.'],settings:['Configurações','Personalização, e-mail, aplicativo Google e opções avançadas da plataforma.']};
function pageTitle(){return titles[state.page==='users'&&state.user.admin&&!state.org?'admins':state.page];}
function scoped(path){
 // The administrator's selected organization applies to tables, records and activity, as in the API.
 if(!state.user||!state.user.admin||!state.org||!/^\/(tables|data|audit)(\/|\?|$)/.test(path))return path;
 return path+(path.includes('?')?'&':'?')+'organization_id='+encodeURIComponent(state.org);
}
function savedOrg(){try{return localStorage.getItem('setapi.org');}catch{return null;}}
function saveOrg(id){try{id?localStorage.setItem('setapi.org',id):localStorage.removeItem('setapi.org');}catch{}}
// The administrator's menu follows the switcher: the platform itself, or everything that belongs to one organization.
const PLATFORM_PAGES=['overview','organizations','settings'];
const ORG_PAGES=['tables','tokens','storages','export'];
function canBuild(t){return state.user.admin||(state.user.org_admin&&(!t||!!t.organization_id));}
function inOrg(){return !!(state.user&&(state.user.admin?state.org:state.user.organization_id));}
function orgLabel(){const id=state.user.admin?state.org:state.user.organization_id;return id?((state.organizations||[]).find(o=>o.id===id)||{}).name||'Organização':'Plataforma';}
function fitPage(){if(!state.user.admin)return;if(state.org&&PLATFORM_PAGES.includes(state.page))state.page='tables';if(!state.org&&ORG_PAGES.includes(state.page))state.page='overview';}
function applyNav(){
 const platform=state.user.admin&&!state.org;
 document.querySelectorAll('nav [data-page]').forEach(b=>{const page=b.dataset.page;b.hidden=(b.hasAttribute('data-admin')&&!state.user.admin)||(b.hasAttribute('data-org-admin')&&!state.user.admin&&!state.user.org_admin)||(b.hasAttribute('data-storage')&&!state.user.storage)||(page==='overview'&&!state.user.admin)||(state.user.admin&&state.org&&PLATFORM_PAGES.includes(page))||(platform&&ORG_PAGES.includes(page));});
 $('nav [data-page="users"] span').textContent=platform?'Administradores':'Usuários';
 $('#nav-caption').textContent=platform?'PLATAFORMA':'NESTA ORGANIZAÇÃO';
}
function switcher(){
 const box=$('#org-switcher');box.hidden=false;
 if(!state.user.admin){box.innerHTML='<span>ORGANIZAÇÃO</span><strong>'+esc(orgLabel())+'</strong>';return;}
 box.innerHTML='<label for="org-select">ORGANIZAÇÃO</label><select id="org-select"><option value="">Plataforma</option>'+(state.organizations||[]).filter(o=>o.active).map(o=>`<option value="${esc(o.id)}" ${o.id===state.org?'selected':''}>${esc(o.name)}</option>`).join('')+'</select>';
 $('#org-select').onchange=e=>chooseOrg(e.target.value||null);
}
async function chooseOrg(id){
 state.org=id;saveOrg(id);state.table=null;state.browse=null;state.offset=0;state.search='';state.searchField='';
 fitPage();
 await render().catch(e=>toast(e.message));connect();
}
async function api(path, options={}) {
 const headers = {'X-SETAPI-CSRF':'1',...(options.headers||{})};
 if (options.body && !(options.body instanceof FormData)) {headers['Content-Type']='application/json';options.body=JSON.stringify(options.body);}
 const response = await fetch('/api'+scoped(path),{...options,headers});
 if(response.status===401 && !['/auth/login','/app-auth/login','/auth/mfa/setup','/auth/mfa/enable','/auth/mfa/disable','/auth/password','/auth/reset-password'].includes(path)) {showLogin(); throw Error('Sua sessão expirou. Entre novamente.');}
 if(!response.ok) {const err=await response.json().catch(()=>({detail:'Falha na conexão'}));throw Error(typeof err.detail==='string'?err.detail:JSON.stringify(err.detail));}
 return response.status===204?null:response.json();
}
// Name, login message and color chosen in Settings apply before anyone signs in.
function applyBranding(b){
 state.brand=b;document.title=b.name+' · Console';
 document.querySelectorAll('[data-brand-name]').forEach(e=>e.textContent=b.name);
 document.querySelectorAll('[data-brand-initial]').forEach(e=>e.textContent=b.name.trim().charAt(0).toUpperCase());
 $('#login-message').textContent=b.login_message;$('#login-message').hidden=!b.login_message;
 document.documentElement.style.setProperty('--green',b.color);
}
fetch('/api/platform/branding').then(r=>r.ok?r.json():null).then(b=>b&&applyBranding(b)).catch(()=>{});
function toast(text){$('#toast').textContent=text;$('#toast').hidden=false;setTimeout(()=>$('#toast').hidden=true,5000);}
function empty(title,text){return `<div class="empty"><strong>${esc(title)}</strong>${esc(text)}</div>`;}
function table(headers,rows){return `<div class="table-wrap"><table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;}
function badge(text,ok=true){return `<span class="badge ${ok?'':'muted'}">${esc(text)}</span>`;}
function field(name,label,value='',type='text'){return `<label>${esc(label)}<input name="${esc(name)}" type="${type}" value="${esc(value)}" required></label>`;}
function jsonfield(name,label,value){return `<label>${esc(label)}<textarea name="${esc(name)}" spellcheck="false">${esc(JSON.stringify(value,null,2))}</textarea></label>`;}
function modal(title,fields,submit,label='Salvar'){
 $('#modal-title').textContent=title;$('#modal-fields').innerHTML=fields;bindModalFields($('#modal-fields'));$('#modal-error').textContent='';$('#modal-submit').textContent=label;$('#modal-submit').hidden=!submit;
 $('#modal-form').onsubmit=async e=>{e.preventDefault();const button=$('#modal-submit');button.disabled=true;try{const close=await submit(new FormData(e.target));if(close!==false){$('#modal').close();await render();}}catch(err){$('#modal-error').textContent=err.message;}finally{button.disabled=false;}};
 if(!$('#modal').open)$('#modal').showModal();
}
$('#close-modal').onclick=()=>$('#modal').close();
function showLogin(){state.user=null;if(state.socket)state.socket.close();$('#console').hidden=true;$('#login').hidden=false;}
$('#login-form').onsubmit=async e=>{e.preventDefault();$('#login-error').textContent='';const button=e.target.querySelector('button');button.disabled=true;try{const f=new FormData(e.target);await api('/auth/login',{method:'POST',body:Object.fromEntries(f)});e.target.reset();await boot();}catch(err){$('#login-error').textContent=err.message;}finally{button.disabled=false;}};
$('#logout').onclick=async()=>{try{await api('/auth/logout',{method:'POST'});}finally{showLogin();}};
document.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>{state.page=b.dataset.page;state.table=null;state.offset=0;render().catch(e=>toast(e.message));});
async function boot(){state.user=await api('/auth/me');$('#login').hidden=true;$('#console').hidden=false;$('#account').textContent=state.user.email;state.user.storage=state.user.admin||(state.user.audience==='panel'&&!!state.user.organization_id);state.org=state.user.admin?savedOrg():null;const outcome=new URLSearchParams(location.search).get('google');state.page=state.user.storage&&(location.hash==='#storages'||outcome)?'storages':state.user.admin?'overview':'tables';if(state.page==='storages'&&state.user.admin&&!state.org)state.page='backups';await render();connect();if(outcome){history.replaceState(null,'','/#storages');const messages={connected:'Google Drive conectado. Sua pasta está pronta.',cancelled:'Conexão cancelada. Você pode tentar novamente.',expired:'A autorização expirou. Clique em Conectar Google novamente.',permission:'Autorize o acesso solicitado para conectar o Drive.',folder:'Esta conta não tem acesso à pasta original. Reconecte com a conta correta.',failed:'Não foi possível conectar. Tente novamente.'};toast(messages[outcome]||messages.failed);}}
async function render(){
 if(!state.user)return;
 // Only the latest render may draw: overlapping ones (switching organization, realtime) would duplicate actions.
 const run=state.renders=(state.renders||0)+1;
 state.organizations=(await api('/organizations')).data;
 if(state.org&&!state.organizations.some(o=>o.id===state.org&&o.active)){state.org=null;saveOrg(null);}
 fitPage();
 switcher();applyNav();
 state.tables=(await api('/tables')).data;
 if(run!==state.renders)return;
 if(state.socket && state.subscribed!==JSON.stringify(state.tables.map(t=>t.name)))connect();
 const [title,description]=pageTitle();$('#page-title').textContent=title;$('#page-description').textContent=description;$('#breadcrumb').textContent=orgLabel()+' / '+title;$('#page-actions').innerHTML='';
 document.querySelectorAll('[data-page]').forEach(b=>b.classList.toggle('active',b.dataset.page===state.page));
 const handlers={overview:overview,tables:tablesPage,users:usersPage,tokens:tokensPage,storages:storagesPage,backups:backupsPage,audit:auditPage,security:securityPage,organizations:organizationsPage,settings:settingsPage,export:exportPage};await handlers[state.page]();
}
function action(label,fn){const b=document.createElement('button');b.textContent=label;b.onclick=fn;$('#page-actions').append(b);}
async function overview(){
 if(!state.user.admin){state.page='tables';return tablesPage();}
 const [status,users,backups]=await Promise.all([api('/status'),api('/users'),api('/backups')]);
 const orgs=state.organizations.filter(o=>o.active).length,admins=users.data.filter(u=>!u.tenant_id&&u.role==='admin'&&u.active).length,last=backups.data[0];
 $('#content').innerHTML=`<div class="stats">${[[orgs,'Organizações ativas',state.organizations.length-orgs?(state.organizations.length-orgs)+' desativada(s)':'Todas em operação'],[admins,'Administradores','Contas globais ativas'],[last?new Date(last.created_at).toLocaleDateString():'—','Último backup',last?({completed:'Concluído',failed:'Falhou',queued:'Na fila',running:'Em andamento'}[last.status]||last.status):'Nenhuma cópia ainda'],[status.pending_events,'Eventos pendentes',status.worker_alive?'Worker conectado':'Worker sem sinal']].map(([n,t,s])=>`<div class="stat"><small>${t}</small><strong>${esc(n)}</strong><span>${esc(s)}</span></div>`).join('')}</div><div class="hero"><p class="eyebrow">UMA ORGANIZAÇÃO PARA CADA CLIENTE</p><h2>Tabelas, usuários e storage<br>separados por organização.</h2><p>Crie a organização e escolha-a no seletor da barra lateral. Tudo o que ela usa fica lá dentro: tabelas e API, usuários, tokens e o próprio storage.</p><button id="start-org">＋ Criar organização</button></div><div class="grid-2"><div class="card"><div class="card-head"><h3>Conexões do ambiente</h3>${badge('Em execução')}</div><div class="connection"><span>PostgreSQL <small>${esc(status.postgres)}</small></span>${badge('Conectado')}</div><div class="connection"><span>Redis</span>${badge(status.redis?'Conectado':'Indisponível',status.redis)}</div><div class="connection"><span>Worker de eventos e backups</span>${badge(status.worker_alive?'Online':'Aguardando',status.worker_alive)}</div><p class="help">PostgreSQL e Redis são configurados nas variáveis do serviço. Credenciais não são exibidas no painel.</p></div><div class="card"><h3>Seu ponto de partida</h3><button class="split-row link-row" data-go="settings"><span>01 · Configurar e-mail e aplicativo Google</span><span>→</span></button><button class="split-row link-row" data-go="backups"><span>02 · Conectar o destino dos backups</span><span>→</span></button><button class="split-row link-row" data-go="organizations"><span>03 · Criar as organizações</span><span>→</span></button><p class="help">Documentação interativa disponível em <a href="/docs">/docs ↗</a></p></div></div>`;
 $('#start-org').onclick=newOrganization;
 document.querySelectorAll('[data-go]').forEach(b=>b.onclick=()=>{state.page=b.dataset.go;render().catch(e=>toast(e.message));});
}
function newTable(){const org=inOrg();modal(org?'Criar tabela em '+orgLabel():'Criar tabela',field('name','Nome da tabela')+(org?'<p>A tabela pertence só a '+esc(orgLabel())+'. Outras organizações podem ter uma tabela com o mesmo nome, sem conflito.</p>':'<label class="check"><input name="organization_isolated" type="checkbox" checked>Compartilhar entre organizações, cada uma vendo só os próprios registros</label>')+'<p>Depois de criar, adicione os campos pelos botões da tabela.</p>',async f=>{await api('/tables',{method:'POST',body:{name:f.get('name'),columns:[],organization_isolated:!org&&f.has('organization_isolated')}});state.page='tables';state.table=f.get('name');});}
async function tablesPage(){
 if(canBuild())action('＋ Nova tabela',newTable);
 if(!state.table){$('#content').innerHTML='<div class="card">'+(state.tables.length?table(['TABELA','CAMPOS','ENDPOINT',''],state.tables.map(t=>`<tr><td><strong>${esc(t.name)}</strong></td><td>${t.columns.length}</td><td><code>/api/data/${esc(t.name)}</code></td><td><button data-open="${esc(t.name)}" class="secondary">Abrir →</button>${state.user.admin?`<button class="secondary" data-adopt="${esc(t.name)}">Preparar API</button>`:''}</td></tr>`)):empty('Seu banco começa aqui','Crie uma tabela para publicar sua primeira API.'))+'</div>';document.querySelectorAll('[data-adopt]').forEach(b=>b.onclick=()=>modal('Preparar tabela existente','<p>Adiciona UUID e datas automáticas quando faltarem, além de capturar alterações. Não converte IDs existentes de outro tipo.</p>'+field('confirm','Digite '+b.dataset.adopt),async f=>{await api('/tables/'+b.dataset.adopt+'/adopt',{method:'POST',body:{confirm:f.get('confirm')}});}));document.querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>{state.table=b.dataset.open;state.offset=0;state.search='';state.searchField='';tablesPage().catch(e=>toast(e.message));});return;}
 const selected=state.tables.find(t=>t.name===state.table);if(!selected){state.table=null;return tablesPage();}
 const size=pageSize();
 const fileCols=selected.columns.filter(c=>c.type==='file'),stores=canBuild(selected)||fileCols.length?await orgStores():[];
 const storeLabel=id=>id?((stores.find(x=>x.id===id)||{}).name||'storage removido'):'padrão';
 const fileNames=fileCols.length?Object.fromEntries((await api('/files?limit=1000').catch(()=>({data:[]}))).data.map(f=>[f.id,f.name])):{};
 const cell=(c,v)=>c.type==='file'&&v?`<a class="file-chip" href="/api/files/${esc(v)}/download">📄 ${esc(fileNames[v]||'arquivo')}</a>`:esc(typeof v==='object'?JSON.stringify(v):v);
 const records=await api('/data/'+state.table+'?limit='+size+'&offset='+state.offset+'&search='+encodeURIComponent(state.search||'')+'&search_field='+encodeURIComponent(state.searchField||''));
 // Same navigation above and below the records: first, previous, position, next, last.
 const last=Math.max(0,Math.floor((records.total-1)/size)*size);
 const pager=`<div class="pager"><span>${records.total?(state.offset+1)+'–'+Math.min(state.offset+size,records.total)+' de '+records.total:'0 registros'}</span><button class="secondary" data-go-page="0" ${state.offset===0?'disabled':''} title="Primeira página">«</button><button class="secondary" data-go-page="${Math.max(0,state.offset-size)}" ${state.offset===0?'disabled':''}>← Anterior</button><button class="secondary" data-go-page="${state.offset+size}" ${state.offset+size>=records.total?'disabled':''}>Próxima →</button><button class="secondary" data-go-page="${last}" ${state.offset>=last?'disabled':''} title="Última página">»</button></div>`;
 $('#content').innerHTML=`<div class="toolbar"><button class="secondary" id="back-tables">← Tabelas</button><strong>${esc(state.table)}</strong><code>/api/data/${esc(state.table)}</code><span class="grow"></span><button id="add-record">＋ Registro</button></div><div class="card"><div class="toolbar search-bar"><select id="search-field" aria-label="Campo da pesquisa"><option value="">Todos os campos</option>${selected.columns.map(c=>`<option value="${esc(c.name)}" ${state.searchField===c.name?'selected':''}>${esc(c.name)}</option>`).join('')}</select><input id="record-search" type="search" aria-label="Pesquisar" placeholder="Pesquisar… (acentos e símbolos são ignorados)" value="${esc(state.search||'')}"><button class="secondary" id="apply-search">Pesquisar</button>${state.search?'<button class="quiet" id="clear-search">Limpar</button>':''}<span class="grow"></span><label class="per-page">Por página<input id="page-size" type="number" min="1" max="100" value="${size}"></label></div>${pager}${records.data.length?table([...selected.columns.map(c=>c.name),'AÇÕES'],records.data.map((r,i)=>`<tr>${selected.columns.map(c=>`<td title="${esc(JSON.stringify(r[c.name]))}">${cell(c,r[c.name])}</td>`).join('')}<td><button class="secondary" data-edit="${i}">Editar</button><button class="danger" data-delete="${esc(r.id)}">Excluir</button></td></tr>`)):empty('Nenhum registro encontrado','Adicione um registro ou ajuste o filtro.')}${pager}</div></div><div class="card"><div class="card-head"><h3>Estrutura</h3>${canBuild(selected)?'<button class="secondary" id="add-column">＋ Campo</button>':''}</div>${table(['CAMPO','TIPO','ACEITA NULO',''],selected.columns.map(c=>`<tr><td>${esc(c.name)}</td><td><code>${esc(c.type)}</code>${c.type==='file'?' <small>'+esc(storeLabel(c.storage_id))+'</small>':''}</td><td>${c.nullable?'Sim':'Não'}</td><td>${canBuild(selected)&&!['id','created_at','updated_at'].includes(c.name)?`<button class="secondary" data-rename="${esc(c.name)}">Renomear</button><button class="secondary" data-type="${esc(c.name)}">Tipo</button><button class="danger" data-drop-column="${esc(c.name)}">Excluir</button>`:''}</td></tr>`))}${canBuild(selected)?'<p class="help">Alterações são aplicadas imediatamente. Exclusões de tabelas e campos removem dados permanentemente.</p><button class="danger" id="drop-table">Excluir tabela</button>':''}</div>`;
 $('#back-tables').onclick=()=>{state.table=null;render();};const search=()=>{state.search=$('#record-search').value.trim();state.searchField=$('#search-field').value;state.offset=0;render().catch(e=>toast(e.message));};$('#apply-search').onclick=search;$('#record-search').onkeydown=e=>{if(e.key==='Enter')search();};if($('#clear-search'))$('#clear-search').onclick=()=>{state.search='';state.searchField='';state.offset=0;render().catch(e=>toast(e.message));};
 $('#page-size').onchange=e=>{const n=Math.min(100,Math.max(1,Math.round(Number(e.target.value))||25));try{localStorage.setItem('setapi.pageSize',n);}catch{}state.offset=0;render().catch(err=>toast(err.message));};
 document.querySelectorAll('[data-go-page]').forEach(b=>b.onclick=()=>{state.offset=Number(b.dataset.goPage);render().catch(e=>toast(e.message));});
 function editRecord(record){
  const cols=selected.columns.filter(c=>c.writable!==false&&!['id','created_at','updated_at'].includes(c.name));
  const inputs=cols.map(c=>{const v=record?.[c.name];if(c.name==='organization_id'&&state.user.admin)return '<label>Organização<select name="organization_id"><option value="">Sem organização</option>'+(state.organizations||[]).map(o=>`<option value="${esc(o.id)}" ${v===o.id?'selected':''}>${esc(o.name)}</option>`).join('')+'</select></label>'; if(c.type==='boolean')return '<label>'+esc(c.name)+'<select name="'+esc(c.name)+'"><option value="">Vazio</option><option value="true" '+(v===true?'selected':'')+'>Sim</option><option value="false" '+(v===false?'selected':'')+'>Não</option></select></label>';if(c.type==='jsonb')return jsonfield(c.name,c.name,v??{});if(c.type==='file')return '<fieldset class="file-field"><legend>'+esc(c.name)+' · '+esc(storeLabel(c.storage_id))+'</legend>'+(v?'<p>Atual: <a href="/api/files/'+esc(v)+'/download">'+esc(fileNames[v]||'arquivo')+'</a></p><label class="check"><input type="checkbox" name="clear:'+esc(c.name)+'">Remover o arquivo deste campo</label>':'')+'<label>'+(v?'Trocar por outro arquivo':'Arquivo')+'<input type="file" name="upload:'+esc(c.name)+'" '+(!c.nullable&&!v?'required':'')+'></label></fieldset>';return '<label>'+esc(c.name)+'<input name="'+esc(c.name)+'" value="'+esc(v??'')+'" '+(!c.nullable?'required':'')+'></label>';}).join('');
  modal(record?'Editar registro':'Novo registro',inputs,async f=>{const data={};for(const c of cols){
   if(c.type==='file'){
    // The file goes to the field's own storage (or the default one) first; the record keeps its id.
    const chosen=f.get('upload:'+c.name);
    if(chosen&&chosen.size){const body=new FormData();body.append('file',chosen);const params=new URLSearchParams({storage_id:c.storage_id||'default'});if(state.user.admin&&state.org)params.set('organization_id',state.org);data[c.name]=(await api('/files?'+params,{method:'POST',body})).id;}
    else if(f.has('clear:'+c.name))data[c.name]=null;
    continue;
   }
   const raw=f.get(c.name);data[c.name]=raw===''?null:c.type==='boolean'?raw==='true':c.type==='jsonb'?JSON.parse(raw):raw;}
  if(!Object.keys(data).length)return;await api('/data/'+state.table+(record?'/'+record.id:''),{method:record?'PATCH':'POST',body:data});toast('Registro salvo.');});
 }
 $('#add-record').onclick=()=>editRecord(null);document.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>editRecord(records.data[Number(b.dataset.edit)]));document.querySelectorAll('[data-delete]').forEach(b=>b.onclick=()=>confirmDelete('Excluir registro',b.dataset.delete,()=>api('/data/'+state.table+'/'+b.dataset.delete,{method:'DELETE'})));
 if(canBuild(selected)){action('Permissões da tabela',()=>policyDialog(selected).catch(e=>toast(e.message)));action('Índices',()=>indexesDialog(selected.name).catch(e=>toast(e.message)));$('#add-column').onclick=()=>{modal('Adicionar campo',columnFields({},stores),async f=>{await api('/tables/'+state.table+'/columns',{method:'POST',body:columnValue(f)});});toggleFileStorage();};$('#drop-table').onclick=()=>confirmDelete('Excluir tabela',state.table,async()=>{await api('/tables/'+state.table+'?confirm='+encodeURIComponent(state.table),{method:'DELETE'});state.table=null;});document.querySelectorAll('[data-type]').forEach(b=>b.onclick=()=>{const col=selected.columns.find(c=>c.name===b.dataset.type)||{};modal('Alterar tipo do campo','<label>Tipo<select name="type">'+typeOptions(col.type)+'</select></label>'+storageField(stores,col.storage_id)+'<label class="check"><input type="checkbox" name="nullable" checked>Campo opcional</label>'+field('confirm','Digite '+state.table+'.'+b.dataset.type),async f=>{await api('/tables/'+state.table+'/columns/'+b.dataset.type,{method:'PUT',body:{type:f.get('type'),nullable:f.has('nullable'),confirm:f.get('confirm'),storage_id:f.get('type')==='file'?f.get('storage_id')||null:null}});});toggleFileStorage();});document.querySelectorAll('[data-rename]').forEach(b=>b.onclick=()=>modal('Renomear campo',field('name','Novo nome',b.dataset.rename),async f=>{await api('/tables/'+state.table+'/columns/'+b.dataset.rename,{method:'PATCH',body:{name:f.get('name')}});}));document.querySelectorAll('[data-drop-column]').forEach(b=>b.onclick=()=>{const name=state.table+'.'+b.dataset.dropColumn;confirmDelete('Excluir campo',name,()=>api('/tables/'+state.table+'/columns/'+b.dataset.dropColumn+'?confirm='+encodeURIComponent(name),{method:'DELETE'}));});}
}
function pageSize(){try{const n=Number(localStorage.getItem('setapi.pageSize'));return n>=1&&n<=100?n:25;}catch{return 25;}}
function confirmDelete(title,name,fn){modal(title,`<p>Esta ação remove os dados permanentemente. Para confirmar, digite <strong>${esc(name)}</strong>.</p>`+field('confirm','Confirmação'),async f=>{if(f.get('confirm')!==name)throw Error('A confirmação não corresponde.');await fn();toast('Operação concluída.');},'Excluir permanentemente');}
function adminDialog(){
 modal('Novo administrador',field('email','E-mail','','email')+field('password','Senha inicial (mínimo 12 caracteres)','','password')+'<p class="help">Administradores globais gerenciam organizações, backups e configurações da plataforma, e entram em qualquer organização. Para dar acesso a uma só organização, escolha-a no seletor e crie o usuário lá.</p>',
  async f=>{await api('/users',{method:'POST',body:{email:f.get('email'),password:f.get('password'),role:'admin',scopes:{},audience:'panel',tenant_id:null,org_admin:false}});toast('Administrador criado.');},'Criar administrador');
}
function userDialog(organizations,presetOrgId=null){
 const org=presetOrgId?organizations.find(o=>o.id===presetOrgId):null;
 const roles=org?[['member','Membro · permissões por tabela']]:[['member','Membro · permissões por tabela'],['admin','Administrador global']];
 const header=org
  ?`<div class="locked-org"><span>Organização</span><span>${esc(org.name)}</span></div><input type="hidden" name="tenant_id" value="${esc(org.id)}"><p class="help">Este usuário acessa apenas os dados de ${esc(org.name)}. Administradores globais são criados sem organização.</p>`
  :organizationSelect(organizations)+'<p class="help">Administradores globais devem ficar sem organização. Contas vinculadas acessam apenas sua organização.</p>';
 modal(org?'Novo usuário em '+org.name:'Novo usuário',
  field('email','E-mail','','email')+field('password','Senha inicial (mínimo 12 caracteres)','','password')
  +'<label>Perfil<select name="role">'+roles.map(([v,l])=>`<option value="${v}">${l}</option>`).join('')+'</select></label>'
  +'<label>Tipo de usuário<select name="audience"><option value="panel">Painel</option><option value="app">Aplicativo (usuário final)</option></select></label>'
  +header+(org?'<label class="check"><input type="checkbox" name="org_admin">Administrador da organização · cria e altera as tabelas de '+esc(org.name)+'</label>':'')+permissionsFields()+tokenSection(),
  async f=>{
   const scopes=permissionValues(f),role=f.get('role');
   const created=await api('/users',{method:'POST',body:{email:f.get('email'),password:f.get('password'),role:role,scopes:scopes,audience:f.get('audience')||'panel',tenant_id:f.get('tenant_id')||null,org_admin:f.has('org_admin')}});
   toast('Usuário criado'+(org?' em '+org.name:'')+'.');
   if(!f.has('with_token'))return;
   const token=await api('/tokens',{method:'POST',body:{name:f.get('token_name')||('Token de '+created.email),user_id:created.id,hours:expiryValue(f.get('token_hours')),scopes:f.has('org_admin')?{}:tokenScopes(f.get('token_mode'),scopes,role),admin:f.has('org_admin')}});
   showSecret(token,created.email);return false;
  },'Criar usuário');
}
async function usersPage(){
 const organizations=(await api('/organizations')).data,platform=!state.org;
 action(platform?'＋ Novo administrador':'＋ Novo usuário',()=>platform?adminDialog():userDialog(organizations,state.org));
 // Tokens belong to an organization; at the platform level only access and status are managed.
 const rows=(await api('/users')).data.filter(u=>state.org?u.tenant_id===state.org:!u.tenant_id);$('#content').innerHTML='<div class="card">'+table(['E-MAIL','ORGANIZAÇÃO','PERFIL','STATUS',''],rows.map(r=>`<tr><td>${esc(r.email)}</td><td>${esc(organizations.find(o=>o.id===r.tenant_id)?.name||'Conta global')}</td><td>${esc(r.org_admin?'Administrador da organização':r.role==='admin'?'Administrador global':'Membro')}</td><td>${badge(r.active?'Ativo':'Inativo',r.active)}</td><td class="row-actions">${r.active&&!platform?`<button class="secondary" data-token="${esc(r.id)}">＋ Token</button>`:''}<button class="secondary" data-edit-user="${esc(r.id)}">Editar</button>${r.id!==state.user.id?(!platform&&r.audience==='panel'&&r.role!=='admin'?`<button class="secondary" data-level="${esc(r.id)}">${r.org_admin?'↓ Tornar membro':'↑ Promover a administrador'}</button>`:'')+(platform&&r.role==='admin'?'':`<button class="secondary" data-access="${esc(r.id)}">Permissões</button>`)+`<button class="secondary" data-toggle="${esc(r.id)}" data-active="${!r.active}">${r.active?'Desativar':'Ativar'}</button><button class="danger" data-delete-user="${esc(r.id)}">Excluir</button>`:''}</td></tr>`))+'</div><p class="help">'+(platform?'Contas globais, sem organização. Os usuários de cada organização aparecem ao selecioná-la no seletor da barra lateral.':'Usuários de '+esc(orgLabel())+'. Membros recebem acesso apenas às tabelas configuradas. Configure também a política da tabela para limitar registros e campos. Os tokens precisam permitir a operação.')+'</p>';
 document.querySelectorAll('[data-token]').forEach(b=>b.onclick=()=>tokenDialog(rows.find(x=>x.id===b.dataset.token),rows));
 document.querySelectorAll('[data-access]').forEach(b=>b.onclick=()=>{const u=rows.find(x=>x.id===b.dataset.access);modal('Permissões do usuário',organizationSelect(organizations,u.tenant_id)+(u.audience==='panel'&&u.role!=='admin'?`<label class="check"><input type="checkbox" name="org_admin" ${u.org_admin?'checked':''}>Administrador da organização · cria e altera as tabelas dela</label>`:'')+permissionsFields(u.scopes),async f=>{await api('/users/'+u.id+'/access',{method:'PUT',body:{scopes:permissionValues(f),tenant_id:f.get('tenant_id')||null,org_admin:f.has('org_admin')}});});});
 document.querySelectorAll('[data-toggle]').forEach(b=>b.onclick=async()=>{try{await api('/users/'+b.dataset.toggle,{method:'PATCH',body:{active:b.dataset.active==='true'}});await render();}catch(e){toast(e.message);}});
 const who=id=>rows.find(x=>x.id===id);
 document.querySelectorAll('[data-edit-user]').forEach(b=>b.onclick=()=>{const u=who(b.dataset.editUser),own=u.id===state.user.id;
  modal('Editar usuário','<label>E-mail<input name="email" type="email" value="'+esc(u.email)+'" required></label>'+(own?'<p class="help">Para trocar a sua senha, use Segurança da conta.</p>':'<label>Nova senha (deixe vazio para manter)<input name="password" type="password" minlength="12" autocomplete="new-password"></label><p class="help">Uma nova senha encerra as sessões e revoga os tokens deste usuário.</p>'),
   async f=>{const body={};if(f.get('email')!==u.email)body.email=f.get('email');if(f.get('password'))body.password=f.get('password');if(!Object.keys(body).length)return;await api('/users/'+u.id,{method:'PATCH',body});toast('Usuário atualizado.');});});
 document.querySelectorAll('[data-level]').forEach(b=>b.onclick=()=>{const u=who(b.dataset.level),up=!u.org_admin;
  modal(up?'Promover a administrador':'Tornar membro','<p>'+(up?esc(u.email)+' passa a criar e alterar as tabelas de '+esc(orgLabel())+' e a gerenciar o storage dela.':esc(u.email)+' volta a ter só as permissões por tabela.')+(up?' Os tokens atuais deste usuário continuam valendo e passam a ser administrativos.':' Os tokens atuais deste usuário são revogados.')+'</p>',
   async()=>{await api('/users/'+u.id+'/access',{method:'PUT',body:{scopes:u.scopes||{},tenant_id:u.tenant_id,org_admin:up}});toast(up?'Usuário promovido a administrador da organização.':'Usuário agora é membro.');},up?'Promover':'Tornar membro');});
 document.querySelectorAll('[data-delete-user]').forEach(b=>b.onclick=()=>{const u=who(b.dataset.deleteUser);confirmDelete('Excluir usuário',u.email,()=>api('/users/'+u.id,{method:'DELETE'}));});
}
async function tokensPage(){
 const owners=(await api('/users')).data.filter(u=>u.active&&(state.org?u.tenant_id===state.org:!u.tenant_id));
 action('＋ Criar token',()=>owners.length?tokenDialog(null,owners):toast('Crie primeiro um usuário nesta organização. O token age em nome dele.'));
 const rows=(await api('/tokens')).data.filter(t=>state.org?t.organization_id===state.org:!t.organization_id);$('#content').innerHTML='<div class="card">'+(rows.length?table(['NOME','USUÁRIO','PREFIXO','ACESSO','VALIDADE',''],rows.map(r=>`<tr><td>${esc(r.name)}</td><td>${esc((owners.find(u=>u.id===r.user_id)||{}).email||'—')}</td><td><code>${esc(r.prefix)}…</code></td><td>${r.admin?badge('Administrativo'):scopeSummary(r.scopes)}</td><td>${esc(expiryLabel(r.expires_at))}</td><td class="row-actions">${r.revoked_at?badge('Revogado',false):`<button class="danger" data-revoke="${esc(r.id)}">Revogar</button>`}</td></tr>`)):empty('Nenhum token de integração','Crie um token para conectar sua aplicação.'))+'</div><p class="help">Você também cria um token direto na página Usuários, junto com o usuário ou pelo botão Token da linha.</p>'+
  '<div class="card"><h3>Conectar uma IA por MCP</h3><p>Qualquer cliente MCP usa a API inteira: uma ferramenta para cada rota, com as mesmas permissões do token.</p>'+
  '<div class="split-row"><span>URL do servidor MCP</span><code>'+esc(location.origin+'/mcp')+'</code></div><div class="split-row"><span>Cabeçalho</span><code>Authorization: Bearer set_…</code></div>'+
  '<pre>'+esc(JSON.stringify({mcpServers:{setapi:{type:'http',url:location.origin+'/mcp',headers:{Authorization:'Bearer SEU_TOKEN'}}}},null,2))+'</pre>'+
  '<p class="help">Para criar e alterar tabelas e campos pelo MCP, use o token administrativo de um administrador da organização. O token só enxerga o que a organização pode acessar.</p></div>';
 document.querySelectorAll('[data-revoke]').forEach(b=>b.onclick=async()=>{try{await api('/tokens/'+b.dataset.revoke,{method:'DELETE'});await render();toast('Token revogado.');}catch(e){toast(e.message);}});
}
async function storageDialog(){
 const google=await api('/integrations/google/config');
 modal('Conectar storage',field('name','Nome da conexão','Google Drive')+
  '<label>Provedor<select name="provider" id="provider"><option value="drive">Google Drive</option><option value="r2-token">Cloudflare R2 · só com o token</option><option value="s3">S3 compatível</option></select></label><div id="provider-fields"></div>',
  async f=>{
   const provider=f.get('provider');
   if(provider==='drive'){
    const data=await api('/integrations/google/connect',{method:'POST',body:{name:f.get('name'),organization_id:state.user.admin?state.org:null}});
    window.location.assign(data.url);return false;
   }
   if(provider==='r2-token'){
    const made=await api('/integrations/cloudflare/connect',{method:'POST',body:{name:f.get('name'),api_token:f.get('api_token'),account_id:f.get('account_id')||'',bucket:f.get('bucket')||'',organization_id:state.user.admin?state.org:null}});
    toast('Cloudflare R2 conectado. Bucket '+made.bucket+' pronto.');return;
   }
   const config=Object.fromEntries(['bucket','region','endpoint_url','access_key_id','secret_access_key'].map(k=>[k,f.get(k)||'']));
   await api('/storages',{method:'POST',body:{name:f.get('name'),provider,config,organization_id:state.user.admin?state.org:null}});
  },'Conectar Google');
 const update=()=>{
  const provider=$('#provider').value;
  $('#modal-submit').hidden=provider==='drive'&&!google.configured;
  $('#modal-submit').textContent=provider==='drive'?'Conectar Google':provider==='r2-token'?'Conectar R2':'Salvar conexão';
  const name=$('#modal-fields [name=name]');if(['Google Drive','Cloudflare R2','S3'].includes(name.value))name.value={drive:'Google Drive',s3:'S3'}[provider]||'Cloudflare R2';
  if(provider==='drive'){
   $('#provider-fields').innerHTML='<div class="connection"><span><strong>Google Drive</strong><small>Entre com sua conta Google e autorize o SETAPI.</small></span></div><p>'+(inOrg()?'Uma pasta exclusiva será criada no Drive escolhido para os arquivos de '+esc(orgLabel())+'.':'Uma pasta exclusiva será criada para os backups da plataforma.')+'</p>'+(google.configured?'':'<p class="help">Conexão com Google indisponível: '+(state.user.admin?'informe o Client ID e o Client Secret em Configurações → Aplicativo Google.':'peça ao administrador da plataforma para configurar o aplicativo Google.')+'</p>');
  }else if(provider==='r2-token'){
   $('#provider-fields').innerHTML='<div class="steps"><div class="step"><b>1</b><span>No painel da Cloudflare, abra R2 → Gerenciar tokens de API → Criar token de API da conta.</span></div><div class="step"><b>2</b><span>Escolha a permissão <strong>Administrador de leitura e gravação</strong> (Workers R2 Storage Write) e crie.</span></div><div class="step"><b>3</b><span>Cole abaixo o valor do token. O SETAPI cria o bucket e as chaves S3 sozinho.</span></div></div>'+
    field('api_token','Token de API da Cloudflare','','password')+
    '<details><summary>Opções</summary><label>Bucket (vazio: criar um novo)<input name="bucket" placeholder="setapi-…" spellcheck="false"></label><label>Account ID (só se o token acessar mais de uma conta)<input name="account_id" spellcheck="false"></label></details>'+
    '<p class="help">O token não é guardado: o SETAPI guarda só as chaves S3 derivadas dele, criptografadas. Revogar o token na Cloudflare corta o acesso.</p>';
  }else{
   $('#provider-fields').innerHTML=field('bucket','Bucket')+field('region','Região',provider==='r2'?'auto':'us-east-1')+
    '<label>Endpoint HTTPS'+(provider==='s3'?' (opcional para AWS)':'')+'<input name="endpoint_url" type="url" placeholder="'+(provider==='r2'?'https://ACCOUNT_ID.r2.cloudflarestorage.com':'https://s3.exemplo.com')+'" '+(provider==='r2'?'required':'')+'></label>'+field('access_key_id','Access Key ID')+field('secret_access_key','Secret Access Key','','password')+'<p class="help">As credenciais são criptografadas no banco.</p>';
  }
 };
 $('#provider').onchange=update;update();
}
function storageActions(){
 document.querySelectorAll('[data-remove-storage]').forEach(b=>b.onclick=()=>confirmDelete('Remover conexão',b.dataset.removeStorage,()=>api('/storages/'+b.dataset.removeStorage,{method:'DELETE'})));
 document.querySelectorAll('[data-reconnect]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{const data=await api('/integrations/google/connect',{method:'POST',body:{storage_id:b.dataset.reconnect}});window.location.assign(data.url);}catch(e){toast(e.message);b.disabled=false;}});
 document.querySelectorAll('[data-test]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{await api('/storages/'+b.dataset.test+'/test',{method:'POST'});toast('Conexão validada.');}catch(e){toast(e.message);}finally{b.disabled=false;}});
}
// File browser: folders are virtual, so renaming and moving are instant for every provider.
async function storagesPage(){
 action('＋ Conectar storage',()=>storageDialog().catch(e=>toast(e.message)));
 const own=s=>state.user.admin?(state.org?s.organization_id===state.org:!s.organization_id):true;
 const stores=(await api('/storages')).data.filter(own);
 const nav=state.browse=state.browse&&stores.some(s=>s.id===state.browse.storage)?state.browse:{storage:(stores.find(s=>s.is_default)||stores[0])?.id||null,folder:null};
 const providers='<div class="card"><h3>Provedores conectados</h3>'+(stores.length?table(['NOME','PROVEDOR','PADRÃO',''],stores.map(s=>`<tr><td><strong>${esc(s.name)}</strong></td><td>${badge(s.provider.toUpperCase())}</td><td>${s.is_default?'<span class="default-star">★ Padrão</span>':`<button class="secondary" data-default-storage="${esc(s.id)}">Tornar padrão</button>`}</td><td class="row-actions"><button class="secondary" data-browse="${esc(s.id)}">Abrir arquivos</button>${s.provider==='drive'?`<button class="secondary" data-reconnect="${esc(s.id)}">Reconectar com Google</button>`:''}<button class="secondary" data-test="${esc(s.id)}">Testar conexão</button><button class="danger" data-remove-storage="${esc(s.id)}">Remover</button></td></tr>`)):empty('Conecte seu primeiro storage','Google Drive, Cloudflare R2 ou S3.'))+'<p class="help">O storage padrão recebe os arquivos quando a API não informa storage_id (ou informa storage_id=default) e os campos de arquivo sem storage próprio.</p></div>';
 if(!nav.storage){$('#content').innerHTML=providers;storageActions();return;}
 const [folders,files]=await Promise.all([api('/folders?storage_id='+nav.storage),api('/files?storage_id='+nav.storage+(nav.folder?'&folder_id='+nav.folder:'&root=true')+'&limit=1000')]).then(r=>r.map(x=>x.data));
 const byId=Object.fromEntries(folders.map(f=>[f.id,f]));
 if(nav.folder&&!byId[nav.folder])nav.folder=null;
 const path=id=>{const out=[];for(let f=byId[id];f;f=byId[f.parent_id])out.unshift(f);return out;};
 const label=id=>id?path(id).map(f=>f.name).join(' / '):'Início';
 const here=folders.filter(f=>(f.parent_id||null)===nav.folder);
 const crumbs='<nav class="crumbs" aria-label="Pasta atual"><button class="link" data-open-folder="">Início</button>'+path(nav.folder).map(f=>`<span>/</span><button class="link" data-open-folder="${esc(f.id)}">${esc(f.name)}</button>`).join('')+'</nav>';
 const size=n=>n>1048576?(n/1048576).toFixed(1)+' MB':(n/1024).toFixed(1)+' KB';
 const rows=here.map(f=>`<tr><td><button class="link folder-link" data-open-folder="${esc(f.id)}">📁 ${esc(f.name)}</button></td><td>Pasta</td><td>${esc(new Date(f.created_at).toLocaleDateString())}</td><td class="row-actions"><button class="secondary" data-rename-folder="${esc(f.id)}">Renomear</button><button class="secondary" data-move-folder="${esc(f.id)}">Mover</button><button class="danger" data-delete-folder="${esc(f.id)}">Excluir</button></td></tr>`)
  .concat(files.map(f=>`<tr><td>📄 ${esc(f.name)}</td><td>${size(f.size)}</td><td>${esc(new Date(f.created_at).toLocaleDateString())}</td><td class="row-actions"><a class="button secondary" href="/api/files/${esc(f.id)}/download">Baixar ↓</a><button class="secondary" data-rename-file="${esc(f.id)}">Renomear</button><button class="secondary" data-move-file="${esc(f.id)}">Mover</button><button class="danger" data-remove-file="${esc(f.id)}">Excluir</button></td></tr>`));
 const browser=`<div class="card"><div class="card-head"><h3>Arquivos · ${esc((stores.find(x=>x.id===nav.storage)||{}).name||"")}</h3><div class="toolbar browser-tools"><select id="browse-storage" aria-label="Storage">${stores.map(s=>`<option value="${esc(s.id)}" ${s.id===nav.storage?'selected':''}>${esc(s.name)}${s.is_default?' ★':''}</option>`).join('')}</select><button class="secondary" id="new-folder">＋ Pasta</button><button id="upload-here">↑ Enviar arquivo</button></div></div>${crumbs}${rows.length?table(['NOME','TAMANHO','CRIADO EM',''],rows):empty('Pasta vazia','Crie uma pasta ou envie um arquivo para cá.')}<p class="help">Pastas são organizadas pelo SETAPI: renomear e mover não copiam nada no provedor. Os arquivos continuam privados.</p></div>`;
 $('#content').innerHTML=browser+providers;
 storageActions();
 const go=()=>render().catch(e=>toast(e.message));
 $('#browse-storage').onchange=e=>{state.browse={storage:e.target.value,folder:null};go();};
 document.querySelectorAll('[data-browse]').forEach(b=>b.onclick=()=>{state.browse={storage:b.dataset.browse,folder:null};go().then(()=>window.scrollTo({top:0,behavior:'smooth'}));});
 document.querySelectorAll('[data-default-storage]').forEach(b=>b.onclick=async()=>{try{await api('/storages/'+b.dataset.defaultStorage+'/default',{method:'POST'});toast('Storage padrão atualizado.');go();}catch(e){toast(e.message);}});
 document.querySelectorAll('[data-open-folder]').forEach(b=>b.onclick=()=>{nav.folder=b.dataset.openFolder||null;go();});
 $('#new-folder').onclick=()=>modal('Nova pasta em '+label(nav.folder),field('name','Nome da pasta'),async f=>{await api('/folders',{method:'POST',body:{storage_id:nav.storage,name:f.get('name'),parent_id:nav.folder}});toast('Pasta criada.');},'Criar pasta');
 $('#upload-here').onclick=()=>modal('Enviar para '+label(nav.folder),'<label>Arquivo<input type="file" name="file" required></label>',async f=>{await api('/files/'+nav.storage+(nav.folder?'?folder_id='+nav.folder:''),{method:'POST',body:f});toast('Upload concluído.');},'Enviar');
 // Destinations for a move: the top level and every folder, except the moved folder and what is inside it.
 const inside=(id,root)=>path(id).some(f=>f.id===root);
 const destinations=(skip)=>'<label>Mover para<select name="target"><option value="">Início</option>'+folders.filter(f=>!skip||!inside(f.id,skip)).map(f=>[label(f.id),f.id]).sort().map(([l,id])=>`<option value="${esc(id)}">${esc(l)}</option>`).join('')+'</select></label>';
 const file=id=>files.find(x=>x.id===id);
 document.querySelectorAll('[data-rename-folder]').forEach(b=>b.onclick=()=>{const f=byId[b.dataset.renameFolder];modal('Renomear pasta',field('name','Novo nome',f.name),async d=>{await api('/folders/'+f.id,{method:'PATCH',body:{name:d.get('name')}});toast('Pasta renomeada.');});});
 document.querySelectorAll('[data-move-folder]').forEach(b=>b.onclick=()=>{const f=byId[b.dataset.moveFolder];modal('Mover pasta '+f.name,destinations(f.id),async d=>{await api('/folders/'+f.id,{method:'PATCH',body:{parent_id:d.get('target')||null}});toast('Pasta movida.');},'Mover');});
 document.querySelectorAll('[data-delete-folder]').forEach(b=>b.onclick=()=>{const f=byId[b.dataset.deleteFolder];
  modal('Excluir pasta '+f.name,'<p>Se a pasta tiver conteúdo, todas as subpastas e arquivos dentro dela também serão apagados, inclusive no provedor.</p>'+field('confirm','Para confirmar, digite '+f.name),async d=>{if(d.get('confirm')!==f.name)throw Error('A confirmação não corresponde.');await api('/folders/'+f.id+'?recursive=true',{method:'DELETE'});toast('Pasta excluída.');},'Excluir permanentemente');});
 document.querySelectorAll('[data-rename-file]').forEach(b=>b.onclick=()=>{const f=file(b.dataset.renameFile);modal('Renomear arquivo',field('name','Novo nome',f.name),async d=>{await api('/files/'+f.id,{method:'PATCH',body:{name:d.get('name')}});toast('Arquivo renomeado.');});});
 document.querySelectorAll('[data-move-file]').forEach(b=>b.onclick=()=>{const f=file(b.dataset.moveFile);modal('Mover '+f.name,destinations(null),async d=>{await api('/files/'+f.id,{method:'PATCH',body:{folder_id:d.get('target')||null}});toast('Arquivo movido.');},'Mover');});
 document.querySelectorAll('[data-remove-file]').forEach(b=>b.onclick=()=>{const f=file(b.dataset.removeFile);confirmDelete('Excluir arquivo',f.name,()=>api('/files/'+f.id,{method:'DELETE'}));});
}
const BACKUP_STATUS={completed:['Concluído',true],failed:['Falhou',false],queued:['Na fila',false],running:['Em andamento',false],expired:['Expirado',false]};
function backupBadge(r){const [label,ok]=BACKUP_STATUS[r.status]||[r.status,false];return badge(label,ok);}
const EXPORT_FORMATS=[['xlsx','Planilha Excel (XLSX)','Uma aba para cada tabela. Abre no Excel, LibreOffice e Google Planilhas.'],['csv','CSV','Um arquivo .csv por tabela, num .zip. Bom para importar em outros sistemas.'],['json','JSON','Todas as tabelas num único arquivo, com os tipos preservados. Bom para programadores.']];
async function exportPage(){
 const own=state.tables.filter(t=>t.organization_id);
 $('#content').innerHTML='<div class="card"><h3>O que exportar</h3><label>Tabelas<select id="export-table"><option value="">Todas as tabelas de '+esc(orgLabel())+'</option>'+own.map(t=>`<option value="${esc(t.name)}">${esc(t.name)}</option>`).join('')+'</select></label><p class="help">A exportação traz todos os registros e todos os campos, sem limite de página. Tabelas compartilhadas da plataforma não entram.</p></div>'+
  '<div class="export-grid">'+EXPORT_FORMATS.map(([id,title,text])=>`<div class="card export-card"><span class="export-tag">${id.toUpperCase()}</span><h3>${esc(title)}</h3><p>${esc(text)}</p><button data-export="${id}">↓ Baixar ${id.toUpperCase()}</button></div>`).join('')+'</div>';
 document.querySelectorAll('[data-export]').forEach(b=>b.onclick=async()=>{
  const params=new URLSearchParams({format:b.dataset.export,table:$('#export-table').value});
  if(state.user.admin&&state.org)params.set('organization_id',state.org);
  b.disabled=true;const label=b.textContent;b.textContent='Preparando…';
  try{
   const response=await fetch('/api/export?'+params,{headers:{'X-SETAPI-CSRF':'1'}});
   if(!response.ok){const err=await response.json().catch(()=>({detail:'Falha ao exportar'}));throw Error(typeof err.detail==='string'?err.detail:'Falha ao exportar');}
   const name=(/filename="?([^";]+)"?/.exec(response.headers.get('content-disposition')||'')||[])[1]||('dados.'+b.dataset.export);
   const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');
   link.href=url;link.download=name;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);
   toast('Exportação pronta: '+name);
  }catch(e){toast(e.message);}finally{b.disabled=false;b.textContent=label;}
 });
}
function storeChooser(stores){return '<label>Destino<select name="storage_id">'+stores.map(s=>`<option value="${esc(s.id)}">${esc(s.name)} · ${esc(s.provider.toUpperCase())}</option>`).join('')+'</select></label>';}
// Backups of the platform (the whole database) or of one organization (only its tables, into its own storage).
function backupNow(orgId,stores){
 if(!stores.length)return toast(orgId?'Esta organização ainda não tem storage. Conecte um em Storage e arquivos, dentro dela.':'Conecte primeiro um destino em Destinos dos backups.');
 modal('Fazer backup agora',storeChooser(stores)+'<p>'+(orgId?'Cópia criptografada só das tabelas e configurações desta organização.':'Cópia criptografada do banco inteiro e das configurações. Os arquivos externos não fazem parte desta cópia.')+'</p>',
  async f=>{await api('/backups',{method:'POST',body:{storage_id:f.get('storage_id'),organization_id:orgId}});toast('Backup adicionado à fila.');},'Iniciar backup');
}
function scheduleDialog(orgId,stores){
 if(!stores.length)return backupNow(orgId,stores);
 modal('Agendar backups',storeChooser(stores)+field('every_hours','Intervalo em horas',24,'number')+'<label>Cópias a manter<select name="retention">'+[1,2,3,4,5,6,7].map(n=>`<option value="${n}" ${n===7?'selected':''}>${n} ${n===1?'cópia':'cópias'}</option>`).join('')+'</select></label><p class="help">Depois de cada backup concluído, as cópias além desse número são excluídas do provedor.</p>',
  async f=>{await api('/backup-schedules',{method:'POST',body:{storage_id:f.get('storage_id'),every_hours:Number(f.get('every_hours')),retention:Number(f.get('retention')),organization_id:orgId}});toast('Agendamento criado.');});
}
async function backupsPage(){
 const org=state.user.admin?state.org:state.user.organization_id,scope=org?'?organization_id='+encodeURIComponent(org):'';
 const restores=org?(await api('/restores'+scope)).data:[];
 const [allStores,rows,schedules]=await Promise.all([api('/storages'),api('/backups'+scope),api('/backup-schedules'+scope)]).then(r=>r.map(x=>x.data));
 const mine=s=>org?s.organization_id===org:!s.organization_id,stores=allStores.filter(mine),storeName=id=>(allStores.find(x=>x.id===id)||{}).name||'—';
 const hours=h=>h%24?h+' horas':h===24?'Diário':(h/24)+' dias';
 action('＋ Backup agora',()=>backupNow(org,stores));
 const destinations=`<div class="card"><div class="card-head"><h3>Destinos dos backups</h3><button class="secondary" id="add-destination">＋ Conectar destino</button></div>${stores.length?table(['NOME','PROVEDOR',''],stores.map(s=>`<tr><td>${esc(s.name)}</td><td>${badge(s.provider.toUpperCase())}</td><td class="row-actions">${s.provider==='drive'?`<button class="secondary" data-reconnect="${esc(s.id)}">Reconectar com Google</button>`:''}<button class="secondary" data-test="${esc(s.id)}">Testar conexão</button><button class="danger" data-remove-storage="${esc(s.id)}">Remover</button></td></tr>`)):empty('Nenhum destino conectado','Conecte Google Drive, Cloudflare R2 ou S3 para guardar as cópias.')}<p class="help">${org?'Os destinos são as conexões de storage de '+esc(orgLabel())+'. A cópia leva só as tabelas dela.':'Destinos da plataforma recebem o backup do banco inteiro. Cada organização guarda os próprios backups no storage dela.'}</p></div>`;
 const scheduleCard=`<div class="card"><div class="card-head"><h3>Agendamentos</h3><button class="secondary" id="schedule">＋ Agendar</button></div>${schedules.length?table(['DESTINO','FREQUÊNCIA','RETENÇÃO','PRÓXIMA EXECUÇÃO',''],schedules.map(s=>`<tr><td>${esc(storeName(s.storage_id))}</td><td>${esc(hours(s.every_hours))}</td><td>${s.retention} ${s.retention===1?'cópia':'cópias'}</td><td>${esc(new Date(s.next_run).toLocaleString())}</td><td class="row-actions"><button class="danger" data-unschedule="${esc(s.id)}">Remover</button></td></tr>`)):empty('Sem agendamentos','Escolha a frequência e quantas cópias manter, de 1 a 7.')}</div>`;
 const history=`<div class="card"><div class="card-head"><h3>Histórico de backups</h3><button class="secondary" id="refresh-backups">Atualizar</button></div>${rows.length?table(['DATA','STATUS','DESTINO','TAMANHO','OBSERVAÇÃO',''],rows.slice(0,50).map(r=>`<tr><td>${esc(new Date(r.created_at).toLocaleString())}</td><td>${backupBadge(r)}</td><td>${esc(storeName(r.storage_id))}</td><td>${r.size?(r.size/1048576).toFixed(2)+' MB':'—'}</td><td title="${esc(r.object_key||r.error)}">${esc(r.label||r.error||(r.status==='completed'?'Ponto de restauração':'Aguardando processamento'))}</td><td class="row-actions">${org&&r.status==='completed'?`<button class="secondary" data-restore="${esc(r.id)}">↺ Restaurar</button>`:''}</td></tr>`)):empty('Ainda não há backups','Faça uma cópia agora ou crie um agendamento.')}</div>`;
 let orgsCard='';
 if(!org){
  // Global view: every organization's schedule and latest copy, managed from here.
  const [allSchedules,allRows]=await Promise.all([api('/backup-schedules?all=true'),api('/backups?all=true')]).then(r=>r.map(x=>x.data));
  const orgs=state.organizations.filter(o=>o.active);
  orgsCard=`<div class="card"><div class="card-head"><h3>Backups das organizações</h3></div>${orgs.length?table(['ORGANIZAÇÃO','AGENDAMENTO','ÚLTIMO BACKUP',''],orgs.map(o=>{const sc=allSchedules.filter(x=>x.organization_id===o.id),lastRow=allRows.find(x=>x.organization_id===o.id);
   return `<tr><td><strong>${esc(o.name)}</strong></td><td>${sc.length?sc.map(x=>esc(hours(x.every_hours))+' · '+x.retention+(x.retention===1?' cópia':' cópias')).join('<br>'):'<span class="muted-text">Sem agendamento</span>'}</td><td>${lastRow?esc(new Date(lastRow.created_at).toLocaleString())+' '+backupBadge(lastRow):'—'}</td><td class="row-actions"><button class="secondary" data-org-backup="${esc(o.id)}">Backup agora</button><button class="secondary" data-org-schedule="${esc(o.id)}">＋ Agendar</button>${sc.map(x=>`<button class="danger" data-unschedule="${esc(x.id)}">Remover ${esc(hours(x.every_hours).toLowerCase())}</button>`).join('')}<button class="secondary" data-org-backups="${esc(o.id)}">Abrir →</button></td></tr>`;})):empty('Nenhuma organização','Crie uma organização para agendar os backups dela.')}<p class="help">Cada organização guarda os backups no próprio storage. Aqui você agenda, dispara e acompanha todos; em Abrir → vê o histórico completo dela.</p></div>`;
 }
 const restore=org?'<div class="card"><h3>O que vai na cópia</h3><p>As tabelas de '+esc(orgLabel())+' com os registros, as permissões das tabelas, as conexões de storage e a lista de usuários (sem as senhas). Cada backup concluído é um ponto de restauração: use ↺ Restaurar no histórico para voltar a ele. Para abrir os dados em planilha, use Exportar dados.</p></div>'
  :'<div class="card"><h3>Restauração segura</h3><p>Baixe o arquivo .setapi no provedor e use o comando de restauração em um banco vazio. A chave SETAPI_ENCRYPTION_KEY original é necessária para abrir a cópia. Guarde-a fora do servidor.</p><code>python -m app.restore backup.setapi</code><p class="help">O procedimento completo está no README. O painel não substitui o banco em uso.</p></div>';
 const RESTORE_STATUS={completed:['Concluída',true],failed:['Falhou',false],queued:['Na fila',false],running:['Em andamento',false]};
 const restoresCard=org&&restores.length?`<div class="card"><div class="card-head"><h3>Restaurações</h3></div>${table(['PEDIDA EM','PONTO RESTAURADO','STATUS','RESULTADO'],restores.map(x=>{const [l,ok]=RESTORE_STATUS[x.status]||[x.status,false];const point=rows.find(r=>r.id===x.backup_id);return `<tr><td>${esc(new Date(x.created_at).toLocaleString())}</td><td>${point?esc(new Date(point.created_at).toLocaleString()):'—'}</td><td>${badge(l,ok)}</td><td>${esc(x.error||(x.status==='completed'?'Estado anterior salvo como "Antes da restauração".':'Aguardando o worker'))}</td></tr>`;}))}</div>`:'';
 $('#content').innerHTML=destinations+scheduleCard+orgsCard+restoresCard+history+restore;
 document.querySelectorAll('[data-restore]').forEach(b=>b.onclick=()=>{const point=rows.find(r=>r.id===b.dataset.restore);
  modal('Restaurar ponto de '+new Date(point.created_at).toLocaleString(),'<div class="steps"><div class="step"><b>1</b><span>O estado atual de '+esc(orgLabel())+' é salvo antes, como backup "Antes da restauração". Dá para voltar atrás.</span></div><div class="step"><b>2</b><span>Todas as tabelas da organização voltam a ser exatamente as deste ponto: registros, campos e permissões. Tabelas criadas depois dele são removidas.</span></div><div class="step"><b>3</b><span>Tudo acontece numa única transação: se algo falhar, nada muda.</span></div></div>'+field('confirm','Para confirmar, digite RESTAURAR'),
   async f=>{if(f.get('confirm').trim().toUpperCase()!=='RESTAURAR')throw Error('Digite RESTAURAR para confirmar.');await api('/backups/'+point.id+'/restore',{method:'POST'});toast('Restauração na fila. Acompanhe em Restaurações.');},'Restaurar');});
 $('#add-destination').onclick=()=>storageDialog().catch(e=>toast(e.message));storageActions();
 $('#refresh-backups').onclick=()=>render();$('#schedule').onclick=()=>scheduleDialog(org,stores);
 const orgStores=id=>allStores.filter(s=>s.organization_id===id);
 document.querySelectorAll('[data-org-backup]').forEach(b=>b.onclick=()=>backupNow(b.dataset.orgBackup,orgStores(b.dataset.orgBackup)));
 document.querySelectorAll('[data-org-schedule]').forEach(b=>b.onclick=()=>scheduleDialog(b.dataset.orgSchedule,orgStores(b.dataset.orgSchedule)));
 document.querySelectorAll('[data-org-backups]').forEach(b=>b.onclick=()=>{state.page='backups';chooseOrg(b.dataset.orgBackups);});
 document.querySelectorAll('[data-unschedule]').forEach(b=>b.onclick=async()=>{try{await api('/backup-schedules/'+b.dataset.unschedule,{method:'DELETE'});await render();toast('Agendamento removido.');}catch(e){toast(e.message);}});
}
async function settingsPage(){
 const [cfg,provs,google,status,tokens,plat]=await Promise.all([api('/mail/config'),api('/mail/providers'),api('/integrations/google/config'),api('/status'),api('/tokens'),api('/platform/settings')]);
 const label=id=>(provs.data.find(p=>p.id===id)||{}).label||id;
 const row=(k,v)=>`<div class="split-row"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`;
 const mail='<div class="card"><div class="card-head"><h3>E-mail da plataforma</h3>'+(cfg.configured?badge(label(cfg.provider)):badge('Não configurado',false))+'</div>'+
  (cfg.configured?row('Enviado por',cfg.sender)+row('API key',cfg.key_hint)+row('Administradores que recebem',cfg.recipients.length?cfg.recipients.join(', '):'—'):'<p>Conecte AgentMail, OpenMail ou AGMail. Basta colar a API key.</p>')+
  '<div class="toolbar card-actions"><button id="mail-edit">'+(cfg.configured?'Alterar e-mail':'＋ Configurar e-mail')+'</button>'+(cfg.configured?'<button class="secondary" id="mail-test">Enviar teste para mim</button><button class="danger" id="mail-remove">Remover</button>':'')+'</div>'+
  '<p class="help">Recuperação de senha e autenticação de todos os usuários do painel. Os administradores recebem os avisos gerais (backup que falhou, organização que conectou o Google Drive), e os usuários de cada organização recebem os avisos dela.</p></div>';
 const googleCard='<div class="card"><div class="card-head"><h3>Aplicativo Google</h3>'+(google.configured?badge(google.source==='panel'?'Configurado':'Pelas variáveis do servidor'):badge('Não configurado',false))+'</div>'+
  (google.configured?row('Client ID',google.client_id):'<p>Com o aplicativo Google configurado, os backups da plataforma e cada organização conectam o próprio Google Drive com um clique.</p>')+
  '<div class="split-row"><span>URI de redirecionamento</span><code>'+esc(google.redirect_uri)+'</code></div>'+
  '<div class="toolbar card-actions"><button id="google-edit">'+(google.source==='panel'?'Alterar credenciais':'＋ Configurar Google')+'</button>'+(google.source==='panel'?'<button class="danger" id="google-remove">Remover</button>':'')+'</div>'+
  '<p class="help">'+(google.source==='environment'?'Hoje o Client ID vem de SETAPI_GOOGLE_CLIENT_ID e SETAPI_GOOGLE_CLIENT_SECRET. O que for salvo aqui passa a valer no lugar das variáveis. ':'')+'O Client Secret é criptografado no banco e nunca volta ao navegador. As conexões já feitas continuam funcionando mesmo se as credenciais mudarem.</p></div>';
 const hoursLabel=h=>h%24?h+' horas':(h/24)+(h===24?' dia':' dias');
 const look='<div class="card"><div class="card-head"><h3>Personalização</h3><span class="swatch" id="brand-swatch"></span></div>'+row('Nome da plataforma',plat.name)+row('Mensagem do login',plat.login_message||'—')+row('Cor principal',plat.color)+
  '<div class="toolbar card-actions"><button id="look-edit">Personalizar</button></div><p class="help">O nome aparece na tela de login, na barra lateral, no assunto dos e-mails e no aplicativo autenticador.</p></div>';
 const advanced='<div class="card"><h3>Avançado</h3>'+row('Duração do login',hoursLabel(plat.session_hours))+row('Tamanho máximo de arquivo',plat.max_upload_mb+' MB')+row('Validade padrão de novos tokens',(EXPIRY_CHOICES.find(([v])=>v===String(plat.token_hours))||[,plat.token_hours+' horas'])[1])+
  '<div class="toolbar card-actions"><button id="advanced-edit">Alterar</button></div><p class="help">A nova duração do login vale para as próximas entradas; quem já está conectado continua até a sessão atual expirar.</p></div>';
 const system='<div class="card"><h3>Sistema</h3>'+row('Versão',document.querySelector('.version').textContent.replace('SETAPI / ',''))+row('PostgreSQL',status.postgres)+row('Leitura da API',status.read_engine==='postgrest'?'PostgREST':'Nativa')+row('Redis',status.redis?'Conectado':'Indisponível')+row('Worker de eventos e backups',status.worker_alive?'Online':'Sem sinal')+row('Endereço público',location.origin)+row('Origens liberadas (CORS)',status.cors_origins.length?status.cors_origins.join(', '):'Somente o próprio endereço')+'<p class="help">Banco, Redis, chave de criptografia, endereço público e origens CORS são definidos nas variáveis do serviço.</p></div>';
 // Tokens now belong to organizations; old platform tokens stay listed here until revoked.
 const legacy=tokens.data.filter(t=>!t.organization_id&&!t.revoked_at);
 const legacyCard=legacy.length?'<div class="card"><h3>Tokens da plataforma</h3><p>Tokens criados antes de os tokens passarem a pertencer às organizações. Continuam válidos até expirar ou serem revogados. Os novos são criados dentro de cada organização.</p>'+table(['NOME','PREFIXO','ACESSO','VALIDADE',''],legacy.map(r=>`<tr><td>${esc(r.name)}</td><td><code>${esc(r.prefix)}…</code></td><td>${r.admin?badge('Administrativo'):scopeSummary(r.scopes)}</td><td>${esc(expiryLabel(r.expires_at))}</td><td class="row-actions"><button class="danger" data-revoke="${esc(r.id)}">Revogar</button></td></tr>`))+'</div>':'';
 $('#content').innerHTML='<div class="settings-grid">'+look+advanced+'</div><div class="settings-grid">'+mail+googleCard+'</div>'+system+legacyCard;
 $('#brand-swatch').style.background=plat.color;
 $('#look-edit').onclick=()=>platformDialog(plat,'look');$('#advanced-edit').onclick=()=>platformDialog(plat,'advanced');
 $('#mail-edit').onclick=()=>mailDialog(cfg,provs.data);
 $('#google-edit').onclick=()=>googleDialog(google);
 if(google.source==='panel')$('#google-remove').onclick=()=>modal('Remover credenciais do Google','<p>Novas conexões com o Google Drive passam a usar as variáveis do servidor, se existirem; sem elas, ficam indisponíveis. As conexões existentes continuam funcionando.</p>',async()=>{await api('/integrations/google/config',{method:'DELETE'});toast('Credenciais do Google removidas.');},'Remover');
 document.querySelectorAll('[data-revoke]').forEach(b=>b.onclick=async()=>{try{await api('/tokens/'+b.dataset.revoke,{method:'DELETE'});await render();toast('Token revogado.');}catch(e){toast(e.message);}});
 if(!cfg.configured)return;
 $('#mail-test').onclick=async e=>{e.target.disabled=true;try{const r=await api('/mail/test',{method:'POST'});toast('Teste enviado para '+r.to+'.');}catch(err){toast(err.message);}finally{e.target.disabled=false;}};
 $('#mail-remove').onclick=()=>modal('Remover e-mail','<p>O SETAPI deixa de enviar notificações e recuperação de senha para administradores e organizações.</p>',async()=>{await api('/mail/config',{method:'DELETE'});toast('Provedor de e-mail removido.');},'Remover');
}
function platformDialog(plat,part){
 const keep=Object.entries(plat).map(([k,v])=>`<input type="hidden" name="${esc(k)}" value="${esc(v)}">`).join('');
 const fields=part==='look'
  ?field('name','Nome da plataforma',plat.name)+'<label>Mensagem do login<input name="login_message" maxlength="160" value="'+esc(plat.login_message)+'"></label><label>Cor principal<input name="color" type="color" value="'+esc(plat.color)+'"></label><p class="help">Use uma cor escura o bastante para o texto branco dos botões continuar legível.</p>'
  :field('session_hours','Duração do login, em horas (1 a 720)',plat.session_hours,'number')+field('max_upload_mb','Tamanho máximo de arquivo, em MB (1 a 2048)',plat.max_upload_mb,'number')+expirySelect('token_hours','Validade padrão de novos tokens',String(plat.token_hours)).replace('<option value="never">Nunca expira</option>','');
 modal(part==='look'?'Personalização':'Configurações avançadas',keep+fields,async f=>{
  // The visible fields come after the hidden copies, so getAll()'s last value is the edited one.
  const v=k=>{const all=f.getAll(k);return all[all.length-1];};
  const saved=await api('/platform/settings',{method:'PUT',body:{name:v('name'),login_message:v('login_message'),color:v('color'),session_hours:Number(v('session_hours')),max_upload_mb:Number(v('max_upload_mb')),token_hours:Number(v('token_hours'))}});
  state.platform=saved;applyBranding(saved);toast('Configurações salvas.');
 },'Salvar');
}
function googleDialog(google){
 const current=google.source==='panel';
 modal('Aplicativo Google','<div class="steps"><div class="step"><b>1</b><span>No Google Cloud, crie um projeto e ative a API Google Drive.</span></div><div class="step"><b>2</b><span>Configure a tela de consentimento OAuth.</span></div><div class="step"><b>3</b><span>Crie credenciais OAuth do tipo Aplicativo da Web com a URI de redirecionamento abaixo.</span></div></div><div class="secret">'+esc(google.redirect_uri)+'</div>'+
  '<label>Arquivo JSON baixado do Google Cloud<input type="file" id="google-json" accept=".json,application/json"></label><p class="help" id="google-json-status">Opcional: o arquivo preenche o Client ID e o Client Secret. Ele é lido só neste navegador.</p>'+
  '<label>Client ID<input name="client_id" value="'+esc(current?google.client_id:'')+'" placeholder="000000-xxxx.apps.googleusercontent.com" autocomplete="off" spellcheck="false" required></label>'+
  '<label>Client Secret<input name="client_secret" type="password" autocomplete="off" spellcheck="false" placeholder="'+(current?'Deixe vazio para manter o atual':'Cole o Client Secret')+'" '+(current?'':'required')+'></label>',
  async f=>{await api('/integrations/google/config',{method:'PUT',body:{client_id:f.get('client_id').trim(),client_secret:f.get('client_secret').trim()}});toast('Aplicativo Google configurado.');},'Salvar');
 $('#google-json').onchange=async e=>{
  const status=$('#google-json-status'),file=e.target.files[0];if(!file)return;
  try{
   // The Google Cloud download wraps the client in "web" (or "installed" for desktop apps).
   const data=JSON.parse(await file.text()),app=data.web||data.installed||data;
   if(!app.client_id||!app.client_secret)throw Error('O arquivo não tem client_id e client_secret.');
   $('#modal-fields [name=client_id]').value=app.client_id;$('#modal-fields [name=client_secret]').value=app.client_secret;
   const uris=app.redirect_uris||[];
   status.textContent=uris.includes(google.redirect_uri)?'Arquivo lido. A URI de redirecionamento confere. Clique em Salvar.':'Arquivo lido, mas ele não inclui a URI '+google.redirect_uri+'. Adicione-a no Google Cloud antes de conectar um Drive.';
   status.classList.toggle('warn',!uris.includes(google.redirect_uri));
  }catch(err){status.textContent=err instanceof SyntaxError?'Este arquivo não é um JSON válido.':err.message;status.classList.add('warn');}
 };
}
function mailDialog(cfg,provs){
 modal('Configurar e-mail','<label>Provedor<select name="provider" id="mail-provider">'+provs.map(p=>`<option value="${esc(p.id)}" ${p.id===cfg.provider?'selected':''}>${esc(p.label)}</option>`).join('')+'</select></label>'+
  '<label>API key<input name="api_key" id="mail-key" type="password" autocomplete="off" spellcheck="false"></label><div id="mail-inboxes"></div><p class="help">A chave é criptografada no banco e nunca volta ao navegador.</p>',
  async f=>{const r=await api('/mail/config',{method:'PUT',body:{provider:f.get('provider'),api_key:f.get('api_key')||'',inbox_id:f.get('inbox_id')||''}});toast('E-mail configurado: '+r.sender+'.');},'Salvar');
 const provider=$('#mail-provider'),key=$('#mail-key'),box=$('#mail-inboxes'),submit=$('#modal-submit');let timer,seq=0;
 const saved=()=>cfg.configured&&provider.value===cfg.provider;
 async function load(){
  const current=++seq;submit.hidden=true;
  if(!key.value.trim()&&!saved()){box.innerHTML='';return;}
  box.innerHTML='<p class="help">Buscando as inboxes desta chave…</p>';
  try{
   const list=(await api('/mail/discover',{method:'POST',body:{provider:provider.value,api_key:key.value.trim()}})).data;
   if(current!==seq)return;
   if(!list.length){box.innerHTML='<p class="help">Essa chave ainda não tem nenhuma inbox. Crie uma no provedor e cole a chave de novo.</p>';return;}
   box.innerHTML='<label>Inbox que envia<select name="inbox_id">'+list.map(i=>`<option value="${esc(i.id)}" ${i.id===cfg.inbox_id?'selected':''}>${esc((i.name?i.name+' — ':'')+i.email)}</option>`).join('')+'</select></label>';
   submit.hidden=false;
  }catch(e){if(current===seq)box.innerHTML='<p class="help" style="color:#ad443b">'+esc(e.message)+'</p>';}
 }
 const hint=()=>{key.placeholder=saved()?'Deixe vazio para manter a chave atual ('+cfg.key_hint+')':'Cole a API key do provedor';};
 provider.onchange=()=>{hint();load();};key.oninput=()=>{clearTimeout(timer);timer=setTimeout(load,500);};
 hint();load();
}
async function auditPage(){const rows=(await api('/audit')).data;$('#content').innerHTML='<div class="card">'+(rows.length?table(['DATA','OPERAÇÃO','RECURSO','DETALHES'],rows.map(r=>`<tr><td>${esc(new Date(r.created_at).toLocaleString())}</td><td>${badge(r.action)}</td><td>${esc(r.resource)}</td><td>${esc(JSON.stringify(r.details))}</td></tr>`)):empty('Nenhuma atividade','As próximas operações aparecerão aqui.'))+'</div>';}
let reconnectTimer, refreshTimer;
function connect(){clearTimeout(reconnectTimer);if(state.socket){state.socket.onclose=null;state.socket.close();}if(!state.user)return;state.subscribed=JSON.stringify(state.tables.map(t=>t.name));const ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');state.socket=ws;ws.onopen=()=>ws.send(JSON.stringify({tables:state.tables.map(t=>t.name),...(state.user.admin&&state.org?{organization_id:state.org}:{})}));ws.onmessage=e=>{const msg=JSON.parse(e.data);if(msg.type==='ready'){$('#live-status').textContent='● Tempo real conectado';if(state.page==='tables')render().catch(()=>{});}if(msg.type==='change'){clearTimeout(refreshTimer);refreshTimer=setTimeout(()=>{if(state.page==='tables')render().catch(e=>toast(e.message));},200);}};ws.onclose=()=>{$('#live-status').textContent='○ Reconectando';if(state.user)reconnectTimer=setTimeout(connect,3000);};}
boot().catch(()=>showLogin());

const PERM_MODES={none:[],read:['read'],write:['create','update','delete'],full:['read','create','update','delete']};
const EXPIRY_CHOICES=[['168','7 dias'],['720','30 dias'],['1440','60 dias'],['2160','90 dias'],['4320','180 dias'],['8760','1 ano'],['never','Nunca expira']];
function expirySelect(name,label='Validade do token',selected='720'){return `<label>${esc(label)}<select name="${esc(name)}">`+EXPIRY_CHOICES.map(([v,l])=>`<option value="${v}" ${v===selected?'selected':''}>${l}</option>`).join('')+'</select></label>';}
function defaultExpiry(){return String((state.brand&&state.brand.token_hours)||720);}
function expiryValue(raw){return raw==='never'?null:Number(raw)||720;}
function expiryLabel(value){const d=new Date(value);return d.getUTCFullYear()>=9000?'Nunca expira':d.toLocaleDateString();}
function levelSelect(selected='standard',reach='controle total do ambiente'){return '<label>Nível de acesso<select name="level">'+[['standard','Acesso padrão · permissões por tabela'],['admin','Acesso administrador · '+reach]].map(([v,l])=>`<option value="${v}" ${v===selected?'selected':''}>${l}</option>`).join('')+'</select></label>';}
function accessModeSelect(selected='full'){return '<div class="access-mode"><label>Acesso aos dados<select name="access_mode">'+permissionOptions(selected,['read','write','full','custom'])+'</select></label></div>';}
const PERM_LABELS={none:'Sem acesso',read:'Somente leitura',write:'Somente escrita',full:'Leitura e escrita',custom:'Personalizado',same:'Mesmas permissões do usuário'};
function permissionMode(actions){const list=actions||[];for(const mode of ['none','read','write','full'])if(PERM_MODES[mode].length===list.length&&PERM_MODES[mode].every(a=>list.includes(a)))return mode;return 'custom';}
function permissionOptions(selected,keys){return keys.map(k=>`<option value="${k}" ${k===selected?'selected':''}>${PERM_LABELS[k]}</option>`).join('');}
function permissionsFields(value={},title='Permissões por tabela'){
 if(!state.tables.length)return '<div class="perm-block"><p class="help">Crie uma tabela antes de atribuir permissões.</p></div>';
 const names=['Ler','Criar','Editar','Excluir'];
 return `<div class="perm-block"><div class="perms"><div class="perms-head"><span>${esc(title)}</span><select class="perm-all" aria-label="Aplicar o mesmo acesso a todas as tabelas"><option value="">Aplicar a todas…</option>${permissionOptions('',['none','read','write','full'])}</select></div>`+state.tables.map(t=>{
  const actions=value[t.name]||[],mode=permissionMode(actions);
  return `<div class="perm-row"><span class="perm-name">${esc(t.name)}</span><select class="perm-mode" aria-label="Acesso de ${esc(t.name)}">${permissionOptions(mode,['none','read','write','full','custom'])}</select><div class="perm-custom">${['read','create','update','delete'].map((a,i)=>`<label class="check"><input type="checkbox" data-action="${a}" name="perm:${esc(t.name)}:${a}" ${actions.includes(a)?'checked':''}>${names[i]}</label>`).join('')}</div></div>`;
 }).join('')+'</div><p class="help">Somente leitura consulta registros. Somente escrita cria, edita e exclui sem consultar. Leitura e escrita reúne as duas.</p></div>';
}
function bindModalFields(root){
 root.querySelectorAll('.perm-row').forEach(row=>{
  const select=row.querySelector('.perm-mode'),custom=row.querySelector('.perm-custom'),boxes=[...custom.querySelectorAll('input')];
  const apply=()=>{custom.hidden=select.value!=='custom';if(select.value!=='custom')boxes.forEach(b=>b.checked=PERM_MODES[select.value].includes(b.dataset.action));};
  select.onchange=apply;apply();
 });
 const all=root.querySelector('.perm-all');
 if(all)all.onchange=()=>{if(!all.value)return;root.querySelectorAll('.perm-row .perm-mode').forEach(s=>{s.value=all.value;s.onchange();});all.value='';};
 const toggle=root.querySelector('[name="with_token"]'),extra=root.querySelector('.token-extra');
 if(toggle&&extra){const show=()=>extra.hidden=!toggle.checked;toggle.onchange=show;show();}
 const level=root.querySelector('[name="level"]'),modeWrap=root.querySelector('.access-mode'),mode=root.querySelector('[name="access_mode"]'),block=root.querySelector('.perm-block');
 if(level||mode){
  const sync=()=>{const full=level&&level.value==='admin';if(modeWrap)modeWrap.hidden=full;if(block)block.hidden=full||!mode||mode.value!=='custom';};
  if(level)level.onchange=sync;if(mode)mode.onchange=sync;sync();
 }
}
function tokenSection(){
 return `<fieldset class="token-block"><legend>Token de acesso</legend><label class="check"><input type="checkbox" name="with_token">Criar um token de API junto com este usuário</label><div class="token-extra" hidden><label>Nome do token<input name="token_name" placeholder="Aplicativo do cliente"></label>${expirySelect('token_hours','Validade do token',defaultExpiry())}<label>Acesso do token<select name="token_mode">${permissionOptions('same',['same','read','write','full'])}</select></label><p class="help">O token nunca ultrapassa as permissões do usuário acima. O segredo aparece uma única vez.</p></div></fieldset>`;
}
function tokenScopes(mode,userScopes,role){
 if(mode==='same')return userScopes;
 const wanted=PERM_MODES[mode]||[],result={};
 const names=role==='admin'?state.tables.map(t=>t.name):Object.keys(userScopes);
 for(const name of names){const base=role==='admin'?PERM_MODES.full:(userScopes[name]||[]);const actions=wanted.filter(a=>base.includes(a));if(actions.length)result[name]=actions;}
 return result;
}
function scopeSummary(scopes){
 const keys=Object.keys(scopes||{});
 if(!keys.length)return badge('Sem tabelas',false);
 return keys.slice(0,3).map(t=>badge(t+' · '+PERM_LABELS[permissionMode(scopes[t])])).join(' ')+(keys.length>3?` <small>+${keys.length-3}</small>`:'');
}
function showSecret(token,who){
 modal('Token criado',`<p>Copie agora${who?' o token de '+esc(who):''}. O segredo não será exibido novamente.</p><div class="secret">${esc(token.token)}</div><p class="help">Envie no cabeçalho <code>Authorization: Bearer TOKEN</code>. Para revogar, use a página Tokens de acesso.</p>`,async()=>{},'Concluído');
}
function tokenDialog(owner,owners){
 const organizations=state.organizations||[];
 const ownerName=o=>`${o.email} · ${(organizations.find(x=>x.id===o.tenant_id)||{}).name||'Sem organização'}`;
 const ownerField=owner
  ?`<input type="hidden" name="user_id" value="${esc(owner.id)}"><p class="help">Token vinculado a <strong>${esc(ownerName(owner))}</strong>. Ele herda o isolamento da organização e não pode ampliar as permissões do usuário.</p>`
  :'<label>Usuário responsável<select name="user_id" required>'+(state.org?'':'<option value="">Minha conta global</option>')+owners.filter(u=>u.id!==state.user.id).map(u=>`<option value="${esc(u.id)}">${esc(ownerName(u))}</option>`).join('')+'</select></label><p class="help">O token pertence a '+esc(orgLabel())+' e age com as permissões do usuário escolhido.</p>';
 const global=!owner||(owner.role==='admin'&&!owner.tenant_id)||owner.org_admin;
 const role=owner?owner.role:'admin',ownerScopes=owner?owner.scopes:{};
 modal('Criar token de acesso',field('name','Nome da integração')+ownerField+expirySelect('hours','Validade do token',defaultExpiry())+(global?levelSelect(owner&&owner.org_admin?'admin':'standard',owner&&owner.org_admin?'tabelas e registros da organização':undefined):'')+accessModeSelect()+permissionsFields(ownerScopes,'Permissões do token')+'<p class="help">Use permissões mínimas nas aplicações. Nunca coloque um token administrativo no frontend público.</p>',async f=>{
  const level=f.get('level')||'standard',mode=f.get('access_mode')||'full';
  const scopes=level==='admin'?{}:mode==='custom'?permissionValues(f):tokenScopes(mode,ownerScopes,role);
  const created=await api('/tokens',{method:'POST',body:{name:f.get('name'),user_id:f.get('user_id')||null,hours:expiryValue(f.get('hours')),scopes:scopes,admin:level==='admin'}});
  showSecret(created,owner?owner.email:null);return false;
 },'Criar token');
}
function permissionValues(f){const result={};for(const [k] of f.entries()){if(!k.startsWith('perm:'))continue;const [,t,a]=k.split(':');(result[t]??=[]).push(a);}return result;}
const FIELD_TYPES=['text','integer','decimal','boolean','datetime','date','uuid','json','file'];
function typeOptions(selected){return FIELD_TYPES.map(t=>`<option value="${t}" ${selected===t?'selected':''}>${t==='file'?'file (arquivo)':t}</option>`).join('');}
// Storage a file field sends its files to: the default one, or a specific connection.
function storageField(stores,selected){return '<label class="file-storage">Storage dos arquivos deste campo<select name="storage_id"><option value="">Padrão da organização</option>'+stores.map(s=>`<option value="${esc(s.id)}" ${selected===s.id?'selected':''}>${esc(s.name)} · ${esc(s.provider.toUpperCase())}${s.is_default?' (padrão)':''}</option>`).join('')+'</select></label>';}
function toggleFileStorage(){const type=$('#modal-fields [name=type]'),box=$('#modal-fields .file-storage');if(!type||!box)return;const sync=()=>box.hidden=type.value!=='file';type.addEventListener('change',sync);sync();}
async function orgStores(){try{return (await api('/storages')).data.filter(s=>state.user.admin?(state.org?s.organization_id===state.org:!s.organization_id):true);}catch{return [];}}
function columnFields(value={},stores=[]){
 return field('name','Nome do campo',value.name||'')+'<label>Tipo<select name="type">'+typeOptions(value.type)+'</select></label>'+storageField(stores,value.storage_id)+'<label class="check"><input type="checkbox" name="nullable" checked>Opcional (aceita vazio)</label><label class="check"><input type="checkbox" name="unique">Valor único</label><label>Relacionamento (campo UUID)<select name="references"><option value="">Sem relacionamento</option>'+state.tables.map(t=>`<option value="${esc(t.name)}">${esc(t.name)}</option>`).join('')+'</select></label>';
}
function columnValue(f){return {name:f.get('name'),type:f.get('type'),nullable:f.has('nullable'),unique:f.has('unique'),references:f.get('type')==='uuid'?f.get('references')||null:null,storage_id:f.get('type')==='file'?f.get('storage_id')||null:null};}
async function policyDialog(selected){
 const p=await api('/tables/'+selected.name+'/policy');
 const owner=(name,label)=>'<label>'+label+'<select name="'+name+'"><option value="">Sem restrição por este critério</option>'+selected.columns.filter(c=>c.type==='uuid'&&!['id'].includes(c.name)).map(c=>`<option value="${esc(c.name)}" ${p[name]===c.name?'selected':''}>${esc(c.name)}</option>`).join('')+'</select></label>';
 modal('Acesso aos dados', '<p>Estas regras se aplicam aos acessos sem privilégio administrativo. Sem política, membros não acessam a tabela.</p>'+owner('owner_column','Campo do proprietário (UUID do usuário)')+owner('tenant_column','Campo da organização (UUID do tenant)')+selected.columns.map(c=>`<fieldset><legend>${esc(c.name)}</legend><label class="check"><input type="checkbox" name="read:${esc(c.name)}" ${p.read_fields.includes(c.name)||c.name==='id'?'checked':''}>Permitir leitura</label>${!['id','created_at','updated_at'].includes(c.name)?`<label class="check"><input type="checkbox" name="write:${esc(c.name)}" ${p.write_fields.includes(c.name)?'checked':''}>Permitir escrita</label>`:''}</fieldset>`).join(''),async f=>{await api('/tables/'+selected.name+'/policy',{method:'PUT',body:{owner_column:f.get('owner_column')||null,tenant_column:f.get('tenant_column')||null,read_fields:[...f.keys()].filter(k=>k.startsWith('read:')).map(k=>k.slice(5)),write_fields:[...f.keys()].filter(k=>k.startsWith('write:')).map(k=>k.slice(6))}});toast('Política aplicada. Proprietário e organização são preenchidos pelo servidor.');});
}
async function securityPage(){
 const me=await api('/auth/me'),sessions=(await api('/auth/sessions')).data;
 $('#content').innerHTML='<div class="card"><h3>Proteja sua conta</h3><button id="change-password">Trocar senha</button> <button class="secondary" id="mfa">'+(me.mfa_enabled?'Desativar autenticação em duas etapas':'Ativar autenticação em duas etapas')+'</button><p class="help">A troca de senha encerra todas as sessões e revoga os tokens da conta.</p></div><div class="card"><h3>Sessões ativas</h3>'+table(['INÍCIO','EXPIRAÇÃO',''],sessions.map(s=>`<tr><td>${esc(new Date(s.created_at).toLocaleString())}</td><td>${esc(new Date(s.expires_at).toLocaleString())}</td><td><button class="danger" data-session="${esc(s.id)}">Encerrar</button></td></tr>`))+'</div>';
 const otp='<label>Código do autenticador ou de recuperação<input name="otp" autocomplete="one-time-code"></label>';
 $('#change-password').onclick=()=>modal('Trocar senha',field('current_password','Senha atual','','password')+field('password','Nova senha (mínimo 12 caracteres)','','password')+(me.mfa_enabled?otp:''),async f=>{await api('/auth/password',{method:'POST',body:Object.fromEntries(f)});showLogin();toast('Senha alterada. Entre novamente.');});
 $('#mfa').onclick=()=>modal(me.mfa_enabled?'Desativar duas etapas':'Ativar duas etapas',field('password','Senha atual','','password')+(me.mfa_enabled?otp:''),async f=>{
  if(me.mfa_enabled){await api('/auth/mfa/disable',{method:'POST',body:Object.fromEntries(f)});return;}
  const data=await api('/auth/mfa/setup',{method:'POST',body:Object.fromEntries(f)});
  modal('Vincule seu autenticador','<p>No aplicativo autenticador, adicione uma chave de configuração do tipo TOTP. Guarde os códigos de recuperação que aparecerão a seguir.</p><div class="secret">'+esc(data.secret)+'</div>'+field('password','Confirme sua senha','','password')+otp,async next=>{const done=await api('/auth/mfa/enable',{method:'POST',body:Object.fromEntries(next)});modal('Guarde seus códigos de recuperação','<p>Cada código funciona uma vez. Guarde-os em local seguro. As outras sessões e tokens foram revogados.</p><div class="secret">'+done.recovery_codes.map(esc).join('<br>')+'</div>',null);return false;});return false;
 });
 document.querySelectorAll('[data-session]').forEach(b=>b.onclick=async()=>{await api('/auth/sessions/'+b.dataset.session,{method:'DELETE'});await render();});
}
function accountLink(){
 const params=new URLSearchParams(location.hash.slice(1));const action=params.get('action'),token=params.get('token');
 if(!token||!['verify','reset'].includes(action))return;
 history.replaceState(null,'','/');
 if(action==='verify'){modal('Confirmar e-mail','<p>Confirme seu cadastro para entrar no aplicativo.</p>',async()=>{await api('/app-auth/verify',{method:'POST',body:{token}});toast('E-mail confirmado. Entre no seu aplicativo.');},'Confirmar');return;}
 modal('Redefinir senha',field('password','Nova senha (mínimo 12 caracteres)','','password')+'<label>Código do autenticador ou recuperação (se ativado)<input name="otp"></label>',async f=>{await api('/auth/reset-password',{method:'POST',body:{token,password:f.get('password'),otp:f.get('otp')}});toast('Senha redefinida. Entre novamente.');});
}

accountLink();
$('#forgot-password').onclick=()=>modal('Recuperar senha',field('email','E-mail','','email'),async f=>{const r=await api('/auth/forgot-password',{method:'POST',body:{email:f.get('email')}});toast(r.message);});
async function indexesDialog(tableName){
 const data=(await api('/tables/'+tableName+'/indexes')).data;
 modal('Índices da tabela','<p>Índices ajudam consultas frequentes. A criação em tabelas grandes pode exceder o limite de tempo; planeje a manutenção.</p>'+data.map(x=>'<p><code>'+esc(x.indexdef)+'</code></p>').join('')+'<label>Campos (na ordem, separados por vírgula)<input name="columns" required></label><label class="check"><input type="checkbox" name="unique">Valores únicos</label>',async f=>{await api('/tables/'+tableName+'/indexes',{method:'POST',body:{columns:f.get('columns').split(',').map(x=>x.trim()),unique:f.has('unique')}});toast('Índice criado.');},'Criar índice');
}

function organizationSelect(organizations,current=null){return '<label>Organização<select name="tenant_id"><option value="">Sem organização (conta global/pessoal)</option>'+organizations.filter(o=>o.active||o.id===current).map(o=>`<option value="${esc(o.id)}" ${o.id===current?'selected':''}>${esc(o.name)}${o.active?'':' (inativa)'}</option>`).join('')+'</select></label>';}
function newOrganization(){
 modal('Criar organização',
  '<div class="steps"><div class="step"><b>1</b><span>Dê um nome à organização.</span></div><div class="step"><b>2</b><span>Em seguida abrimos o cadastro do primeiro usuário já dentro dela.</span></div></div>'+field('name','Nome da organização'),
  async f=>{
   const created=await api('/organizations',{method:'POST',body:{name:f.get('name')}});
   state.organizations=(await api('/organizations')).data;switcher();
   toast('Organização criada. Agora crie o primeiro usuário dela.');
   userDialog(state.organizations,created.id);return false;
  },'Criar organização');
}
// Moving an organization between servers: a package protected by a passphrase, not by this server's key.
function exportOrganization(o){
 modal('Exportar '+o.name,'<div class="steps"><div class="step"><b>1</b><span>O pacote leva as tabelas e registros, as permissões, os usuários (com as senhas atuais), as conexões de storage, as pastas e a lista de arquivos.</span></div><div class="step"><b>2</b><span>Os arquivos continuam no Drive, R2 ou S3: o servidor novo usa as mesmas conexões.</span></div><div class="step"><b>3</b><span>No servidor novo, use Importar organização com este arquivo e a mesma senha.</span></div></div>'+
  '<label>Senha do pacote (mínimo 12 caracteres)<input name="passphrase" type="password" minlength="12" autocomplete="new-password" required></label><label>Repita a senha<input name="again" type="password" autocomplete="new-password" required></label><p class="help">O SETAPI não guarda essa senha. Sem ela, o pacote não abre.</p>',
  async f=>{
   if(f.get('passphrase')!==f.get('again'))throw Error('As senhas não conferem.');
   const body=new FormData();body.append('passphrase',f.get('passphrase'));
   const response=await fetch('/api/organizations/'+o.id+'/package',{method:'POST',body,headers:{'X-SETAPI-CSRF':'1'}});
   if(!response.ok){const err=await response.json().catch(()=>({}));throw Error(typeof err.detail==='string'?err.detail:'Falha ao exportar');}
   const name=(/filename="?([^";]+)"?/.exec(response.headers.get('content-disposition')||'')||[])[1]||'organizacao.setapi-org';
   const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=url;link.download=name;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);
   toast('Pacote exportado: '+name);
  },'Exportar pacote');
}
function importOrganization(){
 modal('Importar organização','<p>Traga uma organização exportada de outro servidor SETAPI. Tudo mantém os mesmos ids; se algo dela já existir aqui, nada é importado.</p>'+
  '<label>Pacote (.setapi-org)<input type="file" name="file" accept=".setapi-org" required></label><label>Senha do pacote<input name="passphrase" type="password" autocomplete="off" required></label><p class="help">Importe só pacotes que você mesmo exportou: o pacote cria tabelas neste banco de dados.</p>',
  async f=>{const done=await api('/organizations/import',{method:'POST',body:f});toast(done.name+' importada: '+done.tables+' tabela(s), '+done.users+' usuário(s), '+done.files+' arquivo(s).');},'Importar');
}
async function organizationsPage(){
 action('↑ Importar organização',importOrganization);action('＋ Nova organização',newOrganization);
 const rows=(await api('/organizations')).data;
 $('#content').innerHTML='<div class="card"><p>A administração global gerencia as organizações. Contas vinculadas só acessam dados da própria organização e nunca recebem privilégio administrativo global.</p>'+(rows.length?table(['ORGANIZAÇÃO','USUÁRIOS','STATUS',''],rows.map(o=>`<tr><td>${esc(o.name)}</td><td>${o.users}</td><td>${badge(o.active?'Ativa':'Inativa',o.active)}</td><td class="row-actions">${o.active?`<button data-org-open="${esc(o.id)}">Abrir →</button><button class="secondary" data-org-user="${esc(o.id)}">＋ Usuário</button>`:''}<button class="secondary" data-org-name="${esc(o.id)}">Renomear</button><button class="secondary" data-org-package="${esc(o.id)}">↓ Exportar</button><button class="${o.active?'danger':'secondary'}" data-org-state="${esc(o.id)}">${o.active?'Desativar':'Ativar'}</button></td></tr>`)):empty('Nenhuma organização','Crie uma organização para cada empresa, projeto ou cliente.'))+'</div><div class="card"><h3>Como trabalhar com organizações</h3><div class="steps"><div class="step step-done"><b>1</b><span>Crie a organização com <strong>＋ Nova organização</strong>.</span></div><div class="step"><b>2</b><span>Clique em <strong>Abrir →</strong>, ou escolha a organização no seletor da barra lateral.</span></div><div class="step"><b>3</b><span>Dentro dela, crie tabelas, usuários, tokens e storage: tudo pertence só a ela. Outra organização pode ter uma tabela com o mesmo nome.</span></div><div class="step"><b>4</b><span>Ajuste em <strong>Permissões da tabela</strong> os campos que os usuários da organização podem ler e alterar.</span></div></div><p class="help">Na API, envie <code>organization_id</code> para trabalhar dentro de uma organização; usuários e tokens de uma organização já trabalham nela. Desativar a organização revoga os acessos e preserva seus dados. Para voltar às configurações gerais, escolha Plataforma no seletor.</p></div>';
 document.querySelectorAll('[data-org-package]').forEach(b=>b.onclick=()=>exportOrganization(rows.find(x=>x.id===b.dataset.orgPackage)));
 document.querySelectorAll('[data-org-user]').forEach(b=>b.onclick=()=>userDialog(rows,b.dataset.orgUser));document.querySelectorAll('[data-org-open]').forEach(b=>b.onclick=()=>chooseOrg(b.dataset.orgOpen));
 document.querySelectorAll('[data-org-name]').forEach(b=>b.onclick=()=>{const o=rows.find(x=>x.id===b.dataset.orgName);modal('Renomear organização',field('name','Nome',o.name),async f=>{await api('/organizations/'+o.id,{method:'PATCH',body:{name:f.get('name')}});});});
 document.querySelectorAll('[data-org-state]').forEach(b=>b.onclick=()=>{const o=rows.find(x=>x.id===b.dataset.orgState);modal(o.active?'Desativar organização':'Ativar organização','<p>'+ (o.active?'Todos os acessos serão revogados. Os registros serão preservados.':'Os usuários poderão entrar novamente. Tokens antigos continuam revogados.')+'</p>'+field('confirm','Digite '+o.name),async f=>{if(f.get('confirm')!==o.name)throw Error('Confirmação incorreta');await api('/organizations/'+o.id,{method:'PATCH',body:{active:!o.active}});});});
}
