'use strict';
if(window.self!==window.top){document.documentElement.replaceChildren();throw Error('Відкрийте панель в окремій вкладці.');}
const $=id=>document.getElementById(id);
const labels={draft:'Чернетка',review:'На погодженні',scheduled:'Заплановано',queued:'У черзі',published:'Опубліковано',failed:'Помилка',uncertain:'Перевірити канал',archived:'Архів'};
const titles={overview:['УСЕ ПІД КОНТРОЛЕМ','Огляд роботи','Головне про ваш канал в одному місці.'],posts:['ВІД ІДЕЇ ДО ПУБЛІКАЦІЇ','Публікації','Пишіть, плануйте й публікуйте без зайвих кроків.'],templates:['ГОТОВІ ДО НАСТУПНОГО ДОПИСУ','Ваші шаблони','Збережене оформлення, яке завжди під рукою.'],sources:['ІНФОРМАЦІЯ ПОРУЧ','Джерела інформації','Підключені до бота канали та спосіб отримання новин.'],help:['ПРОСТО ПО КРОКАХ','Як користуватися','Почніть з допису — решта вже поруч.']};
titles.tickets=['БУДЬТЕ НА ЗВ’ЯЗКУ','Звернення читачів','Ті самі діалоги, що й у боті. Відповідайте прямо звідси.'];
const ticketLabels={new:'Нове',working:'У роботі',waiting:'Очікує відповіді',closed:'Закрите'};
let apiBase=(window.PANEL_CONFIG?.apiBase||localStorage.getItem('panel_api')||(/^(localhost|127\.0\.0\.1)$/.test(location.hostname)?location.origin:'')).replace(/\/$/,'');
const fragment=new URLSearchParams(location.hash.slice(1));
if(fragment.has('session')){sessionStorage.setItem('panel_session',fragment.get('session'));history.replaceState(null,'',location.pathname+location.search);}
const state={demo:new URLSearchParams(location.search).get('demo')==='1',token:sessionStorage.getItem('panel_session')||'',user:null,view:'overview',status:'all',page:0,overview:null,editor:null,templates:[],busy:false};
const demo={counts:{draft:3,review:2,scheduled:4,published:28},paused:false,target:'Очима Розвідки',sources:[{title:'Новини громади',transport:'reader',mode:'review'},{title:'Офіційні повідомлення',transport:'bot',mode:'instant'}],reader_status:'Підключено',posts:[
  {id:24,body_text:'Нам пишуть:\n\nДякуємо всім, хто допомагає нашій громаді!',status:'review',media:[],buttons:[]},
  {id:23,body_text:'Запрошуємо долучитися до ініціатив громади.',status:'scheduled',media:[],buttons:[]},
  {id:22,body_text:'Оголошення для підписників нашого каналу.',status:'published',media:[],buttons:[]},
  {id:21,body_text:'Дякуємо за вашу підтримку та небайдужість.',status:'draft',media:[],buttons:[]}].map(p=>({...p,revision:0,header:'',footer:'',silent:0,target_title:'Очима Розвідки',created:new Date().toISOString()})),templates:[{id:1,title:'Повідомлення читача',body_text:'Нам пишуть:\n\nТекст повідомлення читача.',header:'',footer:'',media:[],buttons:[],revision:0},{id:2,title:'Оголошення громади',body_text:'Оголошення\n\nДодайте перевірені місце, дату й час події.',header:'',footer:'',media:[],buttons:[],revision:0}]};

function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;}
function button(text,cls,fn){const n=el('button',cls,text);n.type='button';n.addEventListener('click',()=>safe(fn));return n;}
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').hidden=true,4500);}
async function safe(fn){try{await fn();}catch(e){toast(e.message||'Не вдалося виконати дію.');}}
async function request(path,method='GET',data){
  if(!apiBase)throw Error('Спершу вкажіть адресу сервера бота.');
  const headers={Authorization:'Bearer '+state.token};
  if(data!==undefined)headers['Content-Type']='application/json';
  let response;
  try{response=await fetch(apiBase+'/api/'+path,{method,headers,body:data===undefined?undefined:JSON.stringify(data),cache:'no-store',redirect:'error',credentials:'omit'});}catch{throw Error('Немає зв’язку із сервером. Перевірте адресу та роботу серверного ПК.');}
  const result=await response.json();
  if(response.status===401){sessionStorage.removeItem('panel_session');state.token='';showLogin();}
  if(!response.ok)throw Error(result.error||'Не вдалося виконати дію.');
  return result;
}
function showLogin(){state.user=null;$('workspace').hidden=true;$('login').hidden=false;$('server-url').value=apiBase;}
function dateText(value){if(!value)return '—';const d=new Date(value.includes('T')?value:value.replace(' ','T')+'Z');return Number.isNaN(d.valueOf())?'—':new Intl.DateTimeFormat('uk-UA',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',timeZone:'Europe/Kyiv'}).format(d);}
function isAdmin(){return ['owner','admin'].includes(state.user?.role);}
function badge(status){return el('span','badge '+status,labels[status]||status);}
function empty(title,description,action){const n=el('div','empty');n.append(el('h3','',title),el('p','small',description));if(action)n.append(button(action[0],'primary',action[1]));return n;}
function showWorkspace(){
  $('login').hidden=true;$('workspace').hidden=false;$('demo-banner').hidden=!state.demo;
  $('user-name').textContent=state.user.name;$('avatar').textContent=state.user.name.slice(0,1).toUpperCase();
  $('user-role').textContent=({owner:'Власник',admin:'Адміністратор',moderator:'Модератор'})[state.user.role];
  document.querySelectorAll('.admin-only').forEach(n=>n.hidden=!isAdmin());
  $('publish-post').textContent=isAdmin()?'Опублікувати зараз':'На погодження';
}
async function navigate(view){
  clearInterval(state.ticketTimer);
  state.view=view;const info=titles[view];$('page-eyebrow').textContent=info[0];$('page-title').textContent=info[1];$('page-description').textContent=info[2];$('crumb').textContent=info[1];
  document.querySelectorAll('[data-nav]').forEach(b=>b.classList.toggle('active',b.dataset.nav===view));
  $('view').replaceChildren(el('p','muted','Завантажуємо…'));
  try{
    if(view==='overview')await renderOverview();
    else if(view==='posts')await renderPosts();
    else if(view==='templates')await renderTemplates();
    else if(view==='sources')await renderSources();
    else if(view==='tickets')await renderTickets();
    else renderHelp();
    $('connection-label').textContent=state.demo?'Демонстраційний режим':'Панель підключена';$('connection-label').className='';
  }catch(e){$('view').replaceChildren(empty('Немає зв’язку',e.message,['Повторити',()=>navigate(view)]));$('connection-label').textContent='З’єднання відсутнє';$('connection-label').className='offline';}
}
async function overview(){state.overview=state.demo?demo:await request('overview');return state.overview;}
function postTable(items){
  const wrap=el('div','table-scroll'),table=el('table'),head=el('thead'),hr=el('tr');
  ['Повідомлення','Статус','Дата · Київ',''].forEach(t=>hr.append(el('th','',t)));head.append(hr);table.append(head);const body=el('tbody');
  for(const post of items){const tr=el('tr'),title=el('td');title.append(el('span','post-title',post.body_text?.replace(/\n/g,' ')||'Медіа без тексту'),el('small','',`№${post.id} · ${post.media.length} вкладень`));
    const s=el('td');s.append(badge(post.status));const action=el('td');action.append(button('Відкрити ↗','',()=>openPost(post)));tr.append(title,s,el('td','muted',dateText(post.due||post.created)),action);body.append(tr);}
  table.append(body);wrap.append(table);return wrap;
}
async function getPosts(status='all',page=0){if(state.demo){const items=demo.posts.filter(p=>status==='all'||p.status===status);return {items,total:items.length,page:0};}return request('posts?status='+encodeURIComponent(status)+'&page='+page);}
async function renderOverview(){
  const data=await overview(),view=el('div'),stats=el('div','stats-grid');
  for(const [key,name,note,icon] of [['published','Опубліковано','Дописи у вашому каналі','↗'],['scheduled','Заплановано','Очікують свого часу','◷'],['review','На погодженні','Потребують уваги команди','◉'],['draft','Чернетки','Ідеї для наступних дописів','▤']]){
    const box=el('div','stat'),title=el('div','stat-title',name);title.append(el('span','',icon));box.append(title,el('div','stat-value',String(data.counts[key]||0)),el('div','stat-note',note));stats.append(box);
  }view.append(stats);
  const grid=el('div','overview-grid'),channel=el('section','box box-pad'),heading=el('div','section-title');heading.append(el('h3','','Ваш канал'),el('span','badge',data.paused?'Публікації на паузі':'Готовий до роботи'));channel.append(heading);
  const row=el('div','channel-row'),details=el('div');details.append(el('h2','',data.target),el('p','muted','Єдиний канал для всіх нових публікацій'));row.append(el('span','channel-symbol','◉'),details);channel.append(row);
  const status=el('div','status-row');status.append(el('span','muted',data.paused?'Надсилання призупинено':'Черга публікацій активна'));
  if(isAdmin())status.append(button(data.paused?'▶ Відновити':'Ⅱ Пауза','secondary',async()=>{if(state.demo)demo.paused=!demo.paused;else await request('pause','POST',{paused:!data.paused});await navigate('overview');toast(data.paused?'Публікації відновлено':'Публікації призупинено');}));channel.append(status);
  const quick=el('section','box box-pad');quick.append(el('h3','section-title','Швидкий старт'));
  for(const [icon,title,desc,fn] of [['＋','Створити публікацію','Текст, фото або відео',()=>openEditor()],['▧','Використати шаблон','Почніть зі збереженого оформлення',()=>navigate('templates')]]){
    const b=button('','shortcut',fn),d=el('div');d.append(el('b','',title),el('small','',desc));b.append(el('span','',icon),d,el('span','arrow','↗'));quick.append(b);
  }grid.append(channel,quick);view.append(grid);
  const recent=el('section','box'),rh=el('div','table-heading');rh.append(el('h3','','Останні публікації'),button('Усі дописи ↗','quiet',()=>{state.status='all';state.page=0;return navigate('posts');}));recent.append(rh);
  const posts=await getPosts();recent.append(posts.items.length?postTable(posts.items.slice(0,5)):empty('Ще немає дописів','Створіть першу публікацію для свого каналу.'));view.append(recent);$('view').replaceChildren(view);
}
async function renderPosts(){
  const view=el('div'),filters=el('div','filters');
  for(const [key,label] of [['all','Усі'],['draft','Чернетки'],['review','На погодженні'],['scheduled','Заплановані'],['published','Опубліковані'],['failed','Помилки'],['uncertain','Перевірити канал'],['archived','Архів']])filters.append(button(label,key===state.status?'active':'',()=>{state.status=key;state.page=0;return navigate('posts');}));
  view.append(filters);const result=await getPosts(state.status,state.page),box=el('div','box');box.append(result.items.length?postTable(result.items):empty('У цьому розділі поки порожньо','Ваші дописи з’являться тут після створення.', ['Створити допис',()=>openEditor()]));view.append(box);
  const controls=el('div','page-controls');controls.append(el('span','',`Загалом: ${result.total} · Сторінка ${state.page+1}`));const nav=el('div');if(state.page)nav.append(button('← Попередні','quiet',()=>{state.page--;return navigate('posts');}));if((state.page+1)*20<result.total)nav.append(button('Наступні →','quiet',()=>{state.page++;return navigate('posts');}));controls.append(nav);view.append(controls);$('view').replaceChildren(view);
}
async function renderTemplates(){
  const result=state.demo?{items:demo.templates}:await request('templates');state.templates=result.items;
  if(!result.items.length){$('view').replaceChildren(el('div','box'));$('view').firstChild.append(empty('Збережіть свій перший шаблон','Відкрийте допис і натисніть «Зберегти як шаблон».',['Створити допис',()=>openEditor()]));return;}
  const grid=el('div','templates-grid');for(const item of result.items){const card=el('article','template-card'),actions=el('div','template-actions');card.append(el('span','template-icon','▧'),el('h3','',item.title),el('p','',item.body_text||'Медіа без тексту'));
    actions.append(button('Використати','primary',async()=>{if(state.demo)return openEditor({...item,id:null,status:'draft'},false);const post=await request(`templates/${item.id}/use`,'POST',{revision:item.revision});await openEditor(post,false);}),button('Редагувати','secondary',()=>openEditor(item,true)),button('Видалити','quiet',()=>deleteTemplate(item)));card.append(actions);grid.append(card);}$('view').replaceChildren(grid);
}
async function renderSources(){
  const data=await overview(),box=el('div','box');for(const source of data.sources){const row=el('div','source-item'),d=el('div');d.append(el('h3','',source.title),el('p','muted small',source.transport==='reader'?'Читання через акаунт':'Читання через бота'));row.append(el('span','channel-symbol','◌'),d,el('span','badge',source.mode==='instant'?'Одразу':'На погодження'));box.append(row);}
  if(!data.sources.length)box.append(empty('Джерела ще не додані','Підключіть канал у розділі «Канали» вашого бота.'));
  const view=el('div');view.append(box,el('p','muted small','Налаштування джерел, правил загроз і підключення акаунта залишаються в Telegram-боті.'));$('view').replaceChildren(view);
}
function renderHelp(){const box=el('div','box box-pad help-grid');for(const [i,title,text] of [
  [1,'Створіть повідомлення','Натисніть «Створити допис». Додайте текст, фото або відео. Канал підставиться автоматично.'],
  [2,'Перевірте та надішліть','Натисніть «Опублікувати зараз» або виберіть дату й час за Києвом. Модератор передає допис на погодження.'],
  [3,'Збережіть оформлення','«Зберегти як шаблон» збереже текст, медіа, кнопки й підписи. Використання створює окрему чернетку.'],
  [4,'Відповідайте читачам','Відкрийте «Звернення», виберіть діалог і надішліть текст, фото або відео. Історія спільна з ботом та оновлюється кожні 5 секунд. Замовлення поки керуються в Telegram.']]){const row=el('div','help-step'),d=el('div');d.append(el('h3','',title),el('p','muted',text));row.append(el('span','step-number',String(i)),d);box.append(row);}$('view').replaceChildren(box);}

const demoTickets=[{id:8,name:'Читач каналу',username:null,user_id:10,status:'new',unread:1,preview:'Вітаю! Як запропонувати новину?',updated:new Date().toISOString()},{id:7,name:'Підписник',username:null,user_id:11,status:'waiting',unread:0,preview:'Дякуємо за звернення!',updated:new Date().toISOString()}];
const demoMessages=[{id:1,direction:'in',body:'Вітаю! Як запропонувати новину?',content_type:'text',created:new Date().toISOString()},{id:2,direction:'out',body:'Вітаємо! Скористайтеся кнопкою «Запропонувати новину» в нашому боті.',content_type:'text',created:new Date().toISOString()}];
async function renderTickets(){
  state.ticketPage=state.ticketPage||0;state.ticketQuery=state.ticketQuery||'';state.ticketFilter=state.ticketFilter||'all';
  const wrap=el('div','tickets-layout'),list=el('div','box tickets-sidebar'),toolbar=el('div','tickets-tools'),search=el('input');search.type='search';search.placeholder='Ім’я, username або номер…';search.value=state.ticketQuery;search.setAttribute('aria-label','Пошук звернення');
  search.addEventListener('input',()=>{clearTimeout(state.searchTimer);state.searchTimer=setTimeout(()=>{state.ticketQuery=search.value;state.ticketPage=0;safe(refreshTickets);},350);});
  const filter=el('select');filter.setAttribute('aria-label','Стан звернення');for(const [key,label] of Object.entries({all:'Усі звернення',...ticketLabels})){const option=el('option','',label);option.value=key;filter.append(option);}filter.value=state.ticketFilter;filter.addEventListener('change',()=>{state.ticketFilter=filter.value;state.ticketPage=0;safe(refreshTickets);});
  toolbar.append(search,filter);list.append(toolbar,el('div','ticket-list'));const conversation=el('div','box conversation');conversation.id='conversation';conversation.append(empty('Виберіть звернення','Історія й відповіді з’являться тут.'));wrap.append(list,conversation);$('view').replaceChildren(wrap);
  await refreshTickets();if(state.ticketId)await openTicket(state.ticketId);
  state.ticketTimer=setInterval(()=>{if(state.view!=='tickets'||document.hidden||state.ticketPolling)return;state.ticketPolling=true;safe(async()=>{await refreshTickets();if(state.ticketId)await pollConversation();}).finally(()=>state.ticketPolling=false);},5000);
}
async function refreshTickets(){
  const data=state.demo?{items:demoTickets,total:2,unread:1}:await request(`tickets?status=${encodeURIComponent(state.ticketFilter)}&q=${encodeURIComponent(state.ticketQuery)}&page=${state.ticketPage}`);
  if(state.view!=='tickets')return;const list=document.querySelector('.ticket-list');if(!list)return;list.replaceChildren();
  const count=el('div','ticket-count',`Звернень: ${data.total} · Непрочитаних: ${data.unread}`);list.append(count);
  for(const item of data.items){const row=button('','ticket-row'+(state.ticketId===item.id?' selected':''),()=>openTicket(item.id)),top=el('div','ticket-row-top');top.append(el('b','',item.name||'Користувач'),el('small','',dateText(item.updated)));row.append(top,el('p','',item.preview||'Вкладення'),el('small','',`№${item.id} · ${ticketLabels[item.status]}`));if(item.unread)row.append(el('span','unread-count',String(item.unread)));list.append(row);}
  if(!data.items.length)list.append(empty('Немає звернень','Спробуйте інший фільтр або пошук.'));
  const nav=el('div','ticket-pagination');if(state.ticketPage)nav.append(button('←','quiet',()=>{state.ticketPage--;return refreshTickets();}));if((state.ticketPage+1)*30<data.total)nav.append(button('→','quiet',()=>{state.ticketPage++;return refreshTickets();}));list.append(nav);
}
async function openTicket(id){
  if(state.replySending||state.replyUploading)return;state.ticketId=id;
  const data=state.demo?{ticket:demoTickets.find(t=>t.id===id)||demoTickets[0],messages:demoMessages,blocked:false,older:false}:await request('tickets/'+id);
  if(state.view!=='tickets'||state.ticketId!==id)return;
  state.currentTicket=data.ticket;state.ticketBlocked=data.blocked;state.ticketMessages=data.messages;state.replyMedia=[];state.replyOperation=null;
  const view=$('conversation');view.replaceChildren();const heading=el('div','conversation-heading'),who=el('div');who.append(el('h3','',data.ticket.name||'Користувач'),el('p','small muted',`Звернення №${id} · ${data.ticket.username?'@'+data.ticket.username:'Без username'}`));
  const controls=el('div','conversation-controls');const working=button('У роботу','secondary',()=>setTicketStatus('working'));working.id='ticket-working';const toggle=button('Закрити','secondary',()=>setTicketStatus(state.currentTicket.status==='closed'?'new':'closed'));toggle.id='ticket-toggle';controls.append(working,toggle);heading.append(who,controls);view.append(heading);
  const messages=el('div','chat-messages');messages.id='chat-messages';if(data.older)messages.append(button('Завантажити попередні','quiet load-older',loadOlderMessages));appendMessages(messages,data.messages);view.append(messages);
  const form=el('form','reply-form');form.id='reply-form';const notice=el('p','small muted');notice.id='reply-notice';const textarea=el('textarea');textarea.id='reply-body';textarea.rows=3;textarea.maxLength=4000;textarea.placeholder='Напишіть відповідь користувачу…';textarea.setAttribute('aria-label','Відповідь користувачу');textarea.addEventListener('input',()=>state.replyOperation=null);
  const actions=el('div','reply-actions'),uploadLabel=el('label','reply-upload','＋ Фото / відео'),uploadInput=el('input');uploadInput.type='file';uploadInput.accept='image/jpeg,image/png,video/mp4';uploadInput.hidden=true;uploadInput.addEventListener('change',()=>safe(()=>uploadReply(uploadInput)));uploadLabel.append(uploadInput);const send=el('button','primary','Надіслати відповідь');send.type='submit';send.id='send-reply';const attachment=el('span','small muted');attachment.id='reply-attachment';actions.append(uploadLabel,attachment,send);form.append(notice,textarea,actions);form.addEventListener('submit',event=>{event.preventDefault();safe(sendReply);});view.append(form);updateReplyAvailability();messages.scrollTop=messages.scrollHeight;
  await markTicketRead(data.messages);await refreshTickets();
}
function appendMessages(container,messages,prepend=false){
  const fragment=document.createDocumentFragment();for(const message of messages){const bubble=el('article','chat-bubble '+(message.direction==='out'?'outgoing':'incoming'));bubble.dataset.messageId=String(message.id);bubble.append(el('span','chat-sender',message.direction==='out'?'Команда':'Користувач'));if(message.body)bubble.append(el('div','chat-text',message.body));
    if(message.attachment){bubble.append(button(message.has_media?'↓ Завантажити вкладення':'↗ Отримати вкладення в Telegram','attachment-button',()=>getAttachment(message)));}
    bubble.append(el('time','chat-time',dateText(message.created)));fragment.append(bubble);}
  if(prepend)container.prepend(fragment);else container.append(fragment);
}
async function markTicketRead(messages){if(state.demo||!messages.length)return;await request(`tickets/${state.ticketId}/read`,'POST',{ids:messages.map(m=>m.id)});}
async function pollConversation(){
  const id=state.ticketId;if(!id||state.demo)return;const after=state.ticketMessages.at(-1)?.id||0;const data=await request(`tickets/${id}?after=${after}`);if(state.view!=='tickets'||state.ticketId!==id)return;state.currentTicket=data.ticket;state.ticketBlocked=data.blocked;updateReplyAvailability();
  const messages=$('chat-messages');if(!messages)return;const nearBottom=messages.scrollHeight-messages.scrollTop-messages.clientHeight<120;
  const known=new Set(state.ticketMessages.map(m=>m.id));const fresh=data.messages.filter(m=>!known.has(m.id));if(fresh.length){appendMessages(messages,fresh);state.ticketMessages.push(...fresh);if(nearBottom)messages.scrollTop=messages.scrollHeight;await markTicketRead(fresh);}
}
async function loadOlderMessages(){if(state.demo)return;const id=state.ticketId,messages=$('chat-messages'),first=state.ticketMessages[0]?.id;if(!first)return;const data=await request(`tickets/${id}?before=${first}`);if(state.view!=='tickets'||state.ticketId!==id)return;const oldHeight=messages.scrollHeight;const load=messages.querySelector('.load-older');if(load)load.remove();appendMessages(messages,data.messages,true);state.ticketMessages.unshift(...data.messages);if(data.older)messages.prepend(button('Завантажити попередні','quiet load-older',loadOlderMessages));messages.scrollTop=messages.scrollHeight-oldHeight;await markTicketRead(data.messages);}
function updateReplyAvailability(){const closed=state.currentTicket.status==='closed',disabled=closed||state.ticketBlocked;const body=$('reply-body'),send=$('send-reply');if(!body||!send)return;body.disabled=disabled;send.disabled=disabled||state.replySending||state.replyUploading;if($('ticket-toggle'))$('ticket-toggle').textContent=closed?'Відкрити':'Закрити';if($('ticket-working'))$('ticket-working').disabled=closed;$('reply-notice').textContent=state.ticketBlocked?'Користувача заблоковано. Відповіді недоступні.':closed?'Звернення закрите. Відкрийте його, щоб відповісти.':ticketLabels[state.currentTicket.status]+' · Повідомлення надходять автоматично';}
async function setTicketStatus(status){const id=state.ticketId;if(state.demo){toast('Демонстрація: стан не змінювався');return;}await request(`tickets/${id}/status`,'POST',{status,expected:state.currentTicket.status});await openTicket(id);}
async function uploadReply(input){if(!input.files.length)return;state.replyUploading=true;updateReplyAvailability();try{if(state.demo){toast('Демонстрація: файл не завантажувався');return;}const form=new FormData();form.append('file',input.files[0]);const response=await fetch(apiBase+'/api/upload',{method:'POST',headers:{Authorization:'Bearer '+state.token},body:form,credentials:'omit',redirect:'error'});const data=await response.json();if(!response.ok)throw Error(data.error);state.replyMedia=[data];state.replyOperation=null;$('reply-attachment').textContent='Вкладення додано';}finally{state.replyUploading=false;updateReplyAvailability();}}
async function sendReply(){if(state.replySending||state.replyUploading)return;const body=$('reply-body').value;if(!body.trim()&&!state.replyMedia.length)return;if(state.demo){toast('Демонстрація: користувачу нічого не надсилалося');return;}state.replySending=true;updateReplyAvailability();try{state.replyOperation=state.replyOperation||crypto.randomUUID();await request(`tickets/${state.ticketId}/reply`,'POST',{body,media:state.replyMedia,operation:state.replyOperation});$('reply-body').value='';state.replyMedia=[];state.replyOperation=null;$('reply-attachment').textContent='';await pollConversation();await refreshTickets();toast('Відповідь доставлено');}finally{state.replySending=false;updateReplyAvailability();}}
async function getAttachment(message){if(state.demo){toast('Демонстрація: вкладень немає');return;}if(!message.has_media){await request(`tickets/${state.ticketId}/messages/${message.id}/forward`,'POST',{});toast('Вкладення надіслано вам у Telegram-бота');return;}const response=await fetch(`${apiBase}/api/tickets/${state.ticketId}/messages/${message.id}/attachment`,{headers:{Authorization:'Bearer '+state.token},credentials:'omit',redirect:'error'});if(!response.ok){const data=await response.json();throw Error(data.error);}const blob=await response.blob(),url=URL.createObjectURL(blob),a=el('a');a.href=url;const header=response.headers.get('Content-Disposition')||'';a.download=header.match(/filename="([^"]+)"/)?.[1]||'attachment';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);}

function preview(){const text=[$('post-header').value,$('body-text').value,$('post-footer').value].filter(Boolean).join('\n\n');$('preview-text').textContent=text||'Тут з’явиться ваш допис.';$('character-count').textContent=$('body-text').value.length+' символів';}
function mediaList(){const list=$('media-list');list.replaceChildren();state.editor.media.forEach((m,i)=>{const row=el('div','media-item');row.append(el('span','',`${m.kind==='photo'?'▧ Фото':'▷ Відео'} ${i+1}`));if(!state.editor.readonly)row.append(button('×','',()=>{state.editor.media.splice(i,1);mediaList();}));list.append(row);});}
async function openPost(post){if(!state.demo)post=await request('posts/'+post.id);return openEditor(post,false);}
async function openEditor(post=null,template=false){
  if(!state.overview)await overview();
  state.editor={post,template,media:structuredClone(post?.media||[]),readonly:!!post&&!template&&!['draft','review','failed'].includes(post.status)};
  const read=state.editor.readonly;$('editor-heading').textContent=template?'Редагувати шаблон':post?`Допис №${post.id||'новий'}`:'Новий допис';$('editor-type').textContent=template?'БІБЛІОТЕКА ШАБЛОНІВ':'РЕДАКТОР ПУБЛІКАЦІЇ';
  $('template-title-label').hidden=!template;$('template-title').value=post?.title||'';$('body-text').value=post?.body_text||'';$('post-header').value=post?.header||'';$('post-footer').value=post?.footer||'';$('post-silent').checked=!!post?.silent;
  $('post-buttons').value=(post?.buttons||[]).map(b=>b.text+' | '+b.url).join('\n');$('preview-channel').textContent=state.overview.target;$('schedule-fields').hidden=true;$('post-date').value='';$('editor-error').textContent=post?.error||'';
  $('save-draft').textContent=template?'Зберегти шаблон':'Зберегти чернетку';$('save-draft').hidden=read;$('publish-post').hidden=template||read;$('schedule-post').hidden=template||read||!isAdmin();$('save-template').hidden=template;
  $('media-upload').disabled=read;for(const id of ['body-text','post-header','post-footer','post-buttons','post-silent'])$(id).disabled=read;
  mediaList();preview();$('editor').showModal();
}
function editorData(){return {body_text:$('body-text').value,header:$('post-header').value,footer:$('post-footer').value,buttons_text:$('post-buttons').value||'-',silent:$('post-silent').checked,media:state.editor.media};}
function busy(value){state.busy=value;document.querySelectorAll('#editor button').forEach(b=>b.disabled=value);$('media-upload').disabled=value||state.editor?.readonly;}
async function saveEditor(){
  if(state.editor.template){const data={...editorData(),title:$('template-title').value,revision:state.editor.post.revision};if(state.demo){toast('Приклад: шаблон збережено');return null;}await request('templates/'+state.editor.post.id,'PUT',data);return null;}
  const post=state.editor.post;
  if(state.demo){const item={...editorData(),id:post?.id||99,revision:0,status:'draft',target_title:demo.target,buttons:[],created:new Date().toISOString()};return item;}
  const saved=await request(post?.id?'posts/'+post.id:'posts',post?.id?'PUT':'POST',{...editorData(),...(post?.id?{revision:post.revision}:{})});state.editor.post=saved;return saved;
}
async function editorAction(action){if(state.busy)return;busy(true);$('editor-error').textContent='';try{
  if(state.editor.readonly)throw Error('Цей допис доступний лише для перегляду.');
  if(action==='schedule'&&$('schedule-fields').hidden){$('schedule-fields').hidden=false;$('post-date').focus();return;}
  let when;if(action==='schedule'){const value=$('post-date').value;if(!value)throw Error('Виберіть дату й час за Києвом.');const [d,t]=value.split('T');const [y,m,day]=d.split('-');when=`${day}.${m}.${y} ${t}`;}
  const saved=await saveEditor();
  if(action!=='save'&&saved){if(!state.demo)await request(`posts/${saved.id}/${action}`,'POST',{revision:saved.revision,...(when?{when}:{})});}
  $('editor').close();toast(state.demo?'Демонстрація: дію показано без зміни бота':action==='publish'?'Допис у черзі на публікацію':action==='submit'?'Надіслано на погодження':action==='schedule'?'Публікацію заплановано':'Зміни збережено');await navigate(state.view);
}catch(e){$('editor-error').textContent=e.message;}finally{busy(false);}}
async function saveAsTemplate(){if(state.busy)return;const title=prompt('Назва шаблону (до 60 символів):');if(!title)return;busy(true);try{let post=state.editor.post;if(!state.editor.readonly)post=await saveEditor();if(!state.demo)await request(`posts/${post.id}/template`,'POST',{title,revision:post.revision});toast(state.demo?'Демонстрація: шаблон збережено':'Шаблон збережено');}catch(e){$('editor-error').textContent=e.message;}finally{busy(false);}}
async function deleteTemplate(item){$('confirm-title').textContent='Видалити шаблон?';$('confirm-message').textContent=`«${item.title}». Створені з нього дописи залишаться.`;$('confirm-dialog').returnValue='';$('confirm-dialog').showModal();const result=await new Promise(resolve=>$('confirm-dialog').addEventListener('close',()=>resolve($('confirm-dialog').returnValue),{once:true}));if(result!=='ok')return;if(state.demo){toast('Демонстрація: шаблон не видалявся');return;}await request('templates/'+item.id,'DELETE',{revision:item.revision});toast('Шаблон видалено');await navigate('templates');}
async function upload(){if(state.busy)return;const files=[...$('media-upload').files];if(!files.length)return;busy(true);try{for(const file of files){if(state.editor.media.length>=10)throw Error('Можна додати до 10 вкладень.');if(state.demo){state.editor.media.push({kind:file.type.startsWith('image/')?'photo':'video',file_id:'demo'});continue;}const form=new FormData();form.append('file',file);const response=await fetch(apiBase+'/api/upload',{method:'POST',headers:{Authorization:'Bearer '+state.token},body:form,credentials:'omit',redirect:'error'});const result=await response.json();if(!response.ok)throw Error(result.error);state.editor.media.push(result);}mediaList();}catch(e){$('editor-error').textContent=e.message;mediaList();}finally{$('media-upload').value='';busy(false);}}

$('login-button').addEventListener('click',()=>{try{const url=new URL($('server-url').value.trim());if(url.username||url.password||url.search||url.hash||url.pathname!=='/'||!(url.protocol==='https:'||url.protocol==='http:'&&['127.0.0.1','localhost'].includes(url.hostname)))throw Error();apiBase=url.origin;localStorage.setItem('panel_api',apiBase);sessionStorage.removeItem('panel_session');location.assign(apiBase+'/auth/start');}catch{$('login-error').textContent='Вкажіть HTTPS-адресу сервера або http://127.0.0.1:8787 на серверному ПК.';}});
$('demo-button').addEventListener('click',()=>{state.demo=true;state.user={name:'Олександр',role:'owner'};showWorkspace();safe(()=>navigate('overview'));});
$('logout').addEventListener('click',()=>safe(async()=>{if(!state.demo)await request('logout','POST',{});state.token='';sessionStorage.removeItem('panel_session');state.demo=false;state.overview=null;showLogin();}));
document.querySelectorAll('[data-nav]').forEach(b=>b.addEventListener('click',()=>safe(()=>navigate(b.dataset.nav))));
$('new-post').addEventListener('click',()=>safe(()=>openEditor()));$('close-editor').addEventListener('click',()=>{if(!state.busy)$('editor').close();});
$('editor').addEventListener('cancel',event=>{if(state.busy)event.preventDefault();});
$('editor-form').addEventListener('submit',event=>{event.preventDefault();editorAction('save');});
$('publish-post').addEventListener('click',()=>editorAction(isAdmin()?'publish':'submit'));$('schedule-post').addEventListener('click',()=>editorAction('schedule'));$('save-template').addEventListener('click',saveAsTemplate);
for(const id of ['body-text','post-header','post-footer'])$(id).addEventListener('input',preview);
$('media-upload').addEventListener('change',upload);
(async()=>{if(state.demo){state.user={name:'Олександр',role:'owner'};showWorkspace();await navigate('overview');}else if(state.token){try{state.user=await request('session');showWorkspace();await navigate('overview');}catch(e){showLogin();$('login-error').textContent=e.message;}}else showLogin();})();
