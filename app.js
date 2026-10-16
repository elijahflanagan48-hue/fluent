const state={mode:localStorage.getItem('fluent-mode')||'teacher',route:'overview',weekOffset:0};
const students=[
 {name:'Екатерина Волкова',level:'B2 · Business English',balance:7,next:'Сегодня, 10:00',tone:'#c9f37a',initials:'ЕК'},
 {name:'Михаил Иванов',level:'B1 · General English',balance:2,next:'Сегодня, 12:30',tone:'#8db7ff',initials:'МИ'},
 {name:'Алиса Смирнова',level:'C1 · IELTS 7.5',balance:5,next:'Сегодня, 16:00',tone:'#ff9d8e',initials:'АС'},
 {name:'Даниил Петров',level:'A2 · General English',balance:1,next:'Сегодня, 19:00',tone:'#c6b8ff',initials:'ДП'},
 {name:'Мария Соколова',level:'B2 · Travel & Culture',balance:8,next:'Завтра, 11:00',tone:'#ffd36e',initials:'МС'},
 {name:'Лев Кузнецов',level:'B1 · Tech English',balance:4,next:'20 октября, 18:00',tone:'#9ee4d2',initials:'ЛК'}
];
const payments=[
 ['Екатерина Волкова','Сегодня, 09:12','Пакет · 8 уроков','12 000 ₽','Оплачено'],
 ['Мария Соколова','16 октября','Пакет · 4 урока','6 400 ₽','Оплачено'],
 ['Даниил Петров','15 октября','Пакет · 8 уроков','11 200 ₽','Ожидается'],
 ['Алиса Смирнова','11 октября','Пакет · 8 уроков','14 400 ₽','Оплачено'],
 ['Михаил Иванов','3 октября','Пакет · 4 урока','6 000 ₽','Оплачено']
];
const tasks={teacher:[
 ['На проверке',[['Writing','Opinion essay','Алиса Смирнова · сдано 20 мин назад'],['Vocabulary','Urban life','Михаил Иванов · сдано вчера'],['Speaking','Travel story','Мария Соколова · сдано вчера']]],
 ['Назначено',[['Grammar','Conditionals mix','Екатерина · до 19 октября'],['Reading','The future of work','Даниил · до 20 октября']]],
 ['Завершено',[['Speaking','Job interview','Екатерина · 92/100'],['Writing','Formal letter','Алиса · 88/100']]]
],student:[
 ['Сделать',[['Writing','Opinion essay','Срок сегодня · 19:00'],['Vocabulary','Urban life','Срок 20 октября']]],
 ['Отправлено',[['Speaking','Travel story','Отправлено вчера']]],
 ['Проверено',[['Grammar','Conditionals mix','Комментарий преподавателя'],['Reading','The future of work','Результат 9/10']]]
]};
const materials=[
 {type:'Гайд · PDF',title:'How to sound natural',copy:'12 выражений для живой разговорной речи',tone:'#c9f37a',letter:'N'},
 {type:'Видео · 08:42',title:'Present perfect, finally',copy:'Короткое объяснение без сложных терминов',tone:'#8db7ff',letter:'P'},
 {type:'Тренажёр',title:'Words for negotiations',copy:'32 слова · уровень B2–C1',tone:'#ff9d8e',letter:'W'},
 {type:'Подборка',title:'Weekend listening',copy:'Подкасты, TED и интервью на выходные',tone:'#c6b8ff',letter:'L'}
];
const routes={overview:'Обзор',calendar:'Календарь',students:'Ученики',payments:'Оплаты',homework:'Домашние задания',materials:'Материалы',analytics:'Аналитика',messages:'Сообщения'};

function init(){
 renderStudents();renderPayments();renderMaterials();renderCalendar();renderHeatmap();renderConversations();setMode(state.mode);bind();
 const opts={weekday:'long',day:'numeric',month:'long'};document.getElementById('todayLabel').textContent=new Intl.DateTimeFormat('ru-RU',opts).format(new Date());
}
function bind(){
 document.addEventListener('click',e=>{
  const route=e.target.closest('[data-route]');if(route){e.preventDefault();navigate(route.dataset.route)}
  const mode=e.target.closest('[data-mode]');if(mode)setMode(mode.dataset.mode);
  const modal=e.target.closest('[data-modal]');if(modal)openModal(modal.dataset.modal);
  const toast=e.target.closest('[data-toast]');if(toast)showToast(toast.dataset.toast);
 });
 document.getElementById('mobileMenu').onclick=()=>document.getElementById('sidebar').classList.toggle('open');
 document.getElementById('quickAdd').onclick=()=>openModal('lesson');
 document.getElementById('modalClose').onclick=closeModal;
 document.getElementById('modalBackdrop').onclick=e=>{if(e.target.id==='modalBackdrop')closeModal()};
 document.getElementById('searchButton').onclick=openPalette;
 document.getElementById('commandPalette').onclick=e=>{if(e.target.id==='commandPalette')closePalette()};
 document.getElementById('commandInput').oninput=e=>renderCommands(e.target.value);
 document.getElementById('prevWeek').onclick=()=>{state.weekOffset--;renderCalendar()};
 document.getElementById('nextWeek').onclick=()=>{state.weekOffset++;renderCalendar()};
 document.getElementById('chatForm').onsubmit=e=>{e.preventDefault();const input=document.getElementById('chatInput');if(input.value.trim()){const b=document.createElement('div');b.className='bubble mine';b.textContent=input.value;document.querySelector('.chat-body').appendChild(b);input.value='';showToast('Сообщение отправлено')}};
 document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();openPalette()}if(e.key==='Escape'){closeModal();closePalette()}});
}
function navigate(route){
 if(state.mode==='student'&&['students','payments','analytics'].includes(route))route='overview';state.route=route;
 document.querySelectorAll('.page').forEach(x=>x.classList.toggle('active',x.id===`page-${route}`));
 document.querySelectorAll('.nav-item').forEach(x=>x.classList.toggle('active',x.dataset.route===route));
 document.getElementById('sidebar').classList.remove('open');window.scrollTo({top:0,behavior:'smooth'});
}
function setMode(mode){
 state.mode=mode;localStorage.setItem('fluent-mode',mode);document.body.classList.toggle('student-mode',mode==='student');
 document.querySelectorAll('[data-mode]').forEach(x=>x.classList.toggle('active',x.dataset.mode===mode));
 const student=mode==='student';document.getElementById('profileName').textContent=student?'Екатерина Волкова':'Анна Морозова';document.getElementById('profileRole').textContent=student?'Ученик · B2':'Преподаватель';
 document.querySelector('.profile-card .avatar').textContent=student?'ЕК':'AM';document.getElementById('welcomeTitle').innerHTML=student?'Hello,<br><em>Катя.</em>':'Доброе утро,<br><em>Анна.</em>';
 document.getElementById('welcomeCopy').textContent=student?'Следующий урок уже сегодня. До него осталось проверить новые слова и отправить эссе.':'Спокойный день: четыре урока, один новый платёж и два задания ждут проверки.';
 document.getElementById('homeworkWidgetTitle').textContent=student?'Нужно сделать':'Ждут проверки';document.getElementById('homeworkCount').textContent=student?'02':'03';renderHomework();renderCalendar();if(student&&['students','payments','analytics'].includes(state.route))navigate('overview');
}
function renderStudents(){document.getElementById('studentGrid').innerHTML=students.map(s=>`<article class="student-card" style="--tone:${s.tone}"><div class="student-top"><div class="avatar">${s.initials}</div><div><h3>${s.name}</h3><small>${s.level}</small></div><div><small>Следующий урок</small><strong>${s.next.split(',')[0]}</strong></div></div><p>${s.next}</p><div class="balance-row"><div><strong>${s.balance}</strong> <span>занятий</span></div><button class="more-button">•••</button></div></article>`).join('')}
function renderPayments(){document.getElementById('paymentsTable').innerHTML=payments.map((p,i)=>`<div class="data-row"><strong>${p[0]}</strong><small>${p[1]}</small><span>${p[2]}</span><strong class="amount">${p[3]}</strong><span class="payment-status ${p[4]==='Ожидается'?'pending':''}">${p[4]}</span></div>`).join('')}
function renderHomework(){document.getElementById('homeworkBoard').innerHTML=tasks[state.mode].map(col=>`<section class="kanban-col"><div class="kanban-title">${col[0]} <span>${col[1].length}</span></div>${col[1].map(t=>`<article class="task-card"><span class="task-type">${t[0]}</span><h3>${t[1]}</h3><p>${t[2]}</p><div class="task-meta"><span>${state.mode==='teacher'?'Открыть работу':'Открыть задание'}</span><b>→</b></div></article>`).join('')}</section>`).join('')}
function renderMaterials(){document.getElementById('materialGrid').innerHTML=materials.map(m=>`<article class="material-card" style="--tone:${m.tone}" data-letter="${m.letter}"><small>${m.type}</small><h3>${m.title}</h3><p>${m.copy}</p></article>`).join('')}
function renderCalendar(){
 const base=new Date(2026,9,13+state.weekOffset*7),days=['Пн','Вт','Ср','Чт','Пт','Сб','Вс'];let html='<div class="cal-corner"></div>';
 days.forEach((d,i)=>{const dt=new Date(base);dt.setDate(base.getDate()+i);html+=`<div class="cal-day-head ${dt.getDate()===17?'today-col':''}"><span>${d}</span><strong>${dt.getDate()}</strong></div>`});
 const events={'9-1':['Михаил','blue'],'10-4':['Екатерина','lime'],'12-0':['Мария','coral'],'12-4':['Михаил','blue'],'16-2':['Алиса','coral'],'18-3':['Лев','lime'],'19-4':['Даниил','blue']};
 [9,10,11,12,13,14,15,16,17,18,19,20].forEach(h=>{html+=`<div class="cal-time">${h}:00</div>`;for(let d=0;d<7;d++){const ev=events[`${h}-${d}`];html+=`<div class="cal-cell">${ev?`<div class="cal-event ${ev[1]==='blue'?'':ev[1]}"><strong>${h}:00</strong>${state.mode==='student'?'Урок английского':ev[0]}</div>`:''}</div>`}});document.getElementById('calendarGrid').innerHTML=html;
 const end=new Date(base);end.setDate(base.getDate()+6);const monthName=end.toLocaleDateString('ru-RU',{day:'numeric',month:'long'}).replace(/^\d+\s+/, '');document.getElementById('calendarMonth').textContent=`${base.getDate()}–${end.getDate()} ${monthName} 2026`;
}
function renderHeatmap(){document.getElementById('heatmap').innerHTML=Array.from({length:36},(_,i)=>`<i style="--o:${.16+((i*7)%10)/12}"></i>`).join('')}
function renderConversations(){document.getElementById('conversationList').innerHTML=students.slice(0,5).map((s,i)=>`<div class="conversation ${i===0?'active':''}"><div class="avatar small" style="background:${s.tone}">${s.initials}</div><div><strong>${s.name}</strong><small>${i===0?'Спасибо! 🙌':'Последнее сообщение · вчера'}</small></div></div>`).join('')}
const modalConfig={lesson:{eyebrow:'Calendar',title:'Новый урок',fields:[['Ученик','select','Екатерина Волкова|Михаил Иванов|Алиса Смирнова|Даниил Петров'],['Дата','date',''],['Время','time',''],['Тема урока','text','Например, Speaking practice']]},student:{eyebrow:'People',title:'Новый ученик',fields:[['Имя и фамилия','text',''],['Email','email',''],['Уровень','select','A1|A2|B1|B2|C1|C2'],['Цель','text','Свободно говорить, экзамен…']]},payment:{eyebrow:'Payments',title:'Внести оплату',fields:[['Ученик','select','Екатерина Волкова|Михаил Иванов|Алиса Смирнова'],['Сумма','number',''],['Количество занятий','number',''],['Комментарий','text','']]},homework:{eyebrow:'Practice',title:'Новое задание',fields:[['Ученик','select','Екатерина Волкова|Михаил Иванов|Алиса Смирнова'],['Срок','date',''],['Название','text',''],['Инструкция','textarea','Что нужно сделать…']]}};
function openModal(type){const c=modalConfig[type];document.getElementById('modalEyebrow').textContent=c.eyebrow;document.getElementById('modalTitle').textContent=c.title;document.getElementById('modalForm').innerHTML=c.fields.map((f,i)=>`<label class="${f[1]==='textarea'?'full':''}">${f[0]}${f[1]==='select'?`<select>${f[2].split('|').map(x=>`<option>${x}</option>`).join('')}</select>`:f[1]==='textarea'?`<textarea placeholder="${f[2]}"></textarea>`:`<input type="${f[1]}" placeholder="${f[2]}" ${i===0?'required':''}>`}</label>`).join('')+`<button class="primary-button">Сохранить</button>`;document.getElementById('modalForm').onsubmit=e=>{e.preventDefault();closeModal();showToast('Готово — изменения сохранены')};document.getElementById('modalBackdrop').hidden=false}
function closeModal(){document.getElementById('modalBackdrop').hidden=true}
function openPalette(){document.getElementById('commandPalette').hidden=false;const i=document.getElementById('commandInput');i.value='';renderCommands('');setTimeout(()=>i.focus(),30)}function closePalette(){document.getElementById('commandPalette').hidden=true}
function renderCommands(q){const all=[...Object.entries(routes).map(([id,label])=>({id,label,type:'Раздел'})),...students.map(s=>({id:'students',label:s.name,type:'Ученик'}))].filter(x=>x.label.toLowerCase().includes(q.toLowerCase())).slice(0,8);document.getElementById('commandResults').innerHTML=all.map((x,i)=>`<button class="command-result ${i===0?'selected':''}" data-command="${x.id}"><span>${x.label}</span><small>${x.type}</small></button>`).join('');document.querySelectorAll('[data-command]').forEach(b=>b.onclick=()=>{navigate(b.dataset.command);closePalette()})}
function showToast(text){const t=document.getElementById('toast');t.textContent=text;t.classList.add('show');clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>t.classList.remove('show'),2600)}
init();
