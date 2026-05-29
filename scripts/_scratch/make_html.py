import json
p=json.load(open("/tmp/payload.json"))
DATA=json.dumps(p,ensure_ascii=False,separators=(",",":"))

html = r'''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>投资组合时间线 · Z20695967</title>
<style>
:root{--bg:#0b0f1a;--panel:#141b2d;--panel2:#1b2438;--line:#26304a;--txt:#e6ecf5;--mut:#8a97b0;
--green:#34d399;--red:#f87171;--blue:#60a5fa;--orange:#fbbf24;--purple:#a78bfa;}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"PingFang SC","Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--txt);}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:16px;flex-wrap:wrap;background:linear-gradient(180deg,#10172a,#0b0f1a);}
header h1{font-size:18px;margin:0;font-weight:650}
header .sub{color:var(--mut);font-size:13px}
.kpis{display:flex;gap:12px;flex-wrap:wrap;padding:16px 24px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 16px;min-width:150px;flex:1}
.kpi .l{color:var(--mut);font-size:12px;margin-bottom:4px}
.kpi .v{font-size:20px;font-weight:700}
.wrap{display:flex;gap:16px;padding:0 24px 24px;align-items:flex-start}
.left{width:300px;flex:none;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;position:sticky;top:12px}
.controls{padding:10px;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:8px}
.controls input,.controls select{background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:7px 9px;font-size:13px;width:100%}
.tabs{display:flex;gap:6px}
.tabs button{flex:1;background:var(--panel2);border:1px solid var(--line);color:var(--mut);border-radius:8px;padding:6px;font-size:12px;cursor:pointer}
.tabs button.on{background:var(--blue);color:#06122b;border-color:var(--blue);font-weight:600}
.list{max-height:72vh;overflow:auto}
.row{display:flex;justify-content:space-between;align-items:center;padding:9px 12px;border-bottom:1px solid #1c2336;cursor:pointer;gap:8px}
.row:hover{background:var(--panel2)}
.row.sel{background:#1f2a44;border-left:3px solid var(--blue)}
.row .sym{font-weight:650;font-size:14px}
.row .meta{font-size:11px;color:var(--mut)}
.row .pnl{font-size:13px;font-weight:600;text-align:right;white-space:nowrap}
.right{flex:1;min-width:0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}
.dh{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:6px}
.dh .t{font-size:22px;font-weight:700}
.dh .nm{color:var(--mut);font-size:13px}
.badges{display:flex;gap:18px;flex-wrap:wrap;margin:10px 0 4px}
.badge .l{color:var(--mut);font-size:11px}
.badge .v{font-size:16px;font-weight:650}
.pos{color:var(--green)}.neg{color:var(--red)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--mut);margin:6px 0 2px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;vertical-align:middle}
.chartbox{position:relative;width:100%;overflow:hidden}
svg{width:100%;height:auto;display:block}
.tt{position:fixed;pointer-events:none;background:#0a1326ee;border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:12px;z-index:9;display:none;min-width:150px}
.tt b{font-size:12px}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
th,td{padding:7px 8px;text-align:right;border-bottom:1px solid #1c2336;white-space:nowrap}
th{color:var(--mut);font-weight:600;position:sticky;top:0;background:var(--panel)}
td.l,th.l{text-align:left}
.tag{font-size:10px;padding:2px 6px;border-radius:6px;font-weight:600}
.tag.b{background:#0d3b2e;color:var(--green)}
.tag.s{background:#3b1414;color:var(--red)}
.tag.o{background:#2a2f45;color:var(--mut)}
.note{color:var(--mut);font-size:12px;line-height:1.6}
.scroll{max-height:420px;overflow:auto}
details{margin-top:4px}
summary{cursor:pointer;color:var(--blue);font-size:13px}
.legacychip{font-size:10px;background:#3a2d10;color:var(--orange);padding:2px 7px;border-radius:6px;margin-left:6px}
</style>
</head>
<body>
<header>
 <h1>📈 投资组合时间线 · Z20695967</h1>
 <span class="sub" id="rangelbl"></span>
</header>
<div class="kpis" id="kpis"></div>
<div class="wrap">
 <div class="left">
   <div class="controls">
     <input id="search" placeholder="🔍 搜索代码…"/>
     <select id="sort">
       <option value="value">按市值排序</option>
       <option value="unreal">按未实现盈亏</option>
       <option value="realized">按已实现盈亏</option>
       <option value="numTrades">按交易次数</option>
       <option value="sym">按代码字母</option>
     </select>
     <div class="tabs">
       <button data-f="held" class="on">持有中</button>
       <button data-f="exited">已清仓</button>
       <button data-f="all">全部</button>
     </div>
   </div>
   <div class="list" id="list"></div>
 </div>
 <div class="right" id="right"></div>
</div>
<div class="tt" id="tt"></div>
<script>
const DATA = __DATA__;
const fmt=(n,d=2)=>n==null?'—':(n<0?'-$':'$')+Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const fmtN=(n,d=0)=>n==null?'—':n.toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:Math.max(d,2)});
const pct=n=>(n>=0?'+':'')+n.toFixed(2)+'%';
const cls=n=>n>0?'pos':(n<0?'neg':'');
const S=DATA.summary;
document.getElementById('rangelbl').textContent=`数据窗口 ${S.dateRange[0]} → ${S.dateRange[1]} · 价格来自 Yahoo Finance · 共 ${S.numStocks} 只标的（持有 ${S.numHeld}）`;

const kpis=[
 ['当前持仓市值',fmt(S.marketValue),''],
 ['未实现盈亏 (券商实际)',fmt(S.unrealized),cls(S.unrealized)],
 ['已实现盈亏 (窗口内·含估算)',fmt(S.realized),cls(S.realized)],
 ['期权净现金流',fmt(S.optNet),cls(S.optNet)],
 ['窗口内净买入',fmt(S.netInvested),''],
 ['现金转入',fmt(S.deposits),''],
];
document.getElementById('kpis').innerHTML=kpis.map(k=>`<div class="kpi"><div class="l">${k[0]}</div><div class="v ${k[2]}">${k[1]}</div></div>`).join('');

let filter='held', sortKey='value', q='', sel=null;
const stocks=DATA.stocks;

function filtered(){
 let a=stocks.filter(s=>filter==='all'||(filter==='held'?s.held:!s.held));
 if(q)a=a.filter(s=>s.sym.toLowerCase().includes(q)||(s.name||'').toLowerCase().includes(q));
 const k=sortKey;
 a.sort((x,y)=> k==='sym'? x.sym.localeCompare(y.sym) : (y[k]||0)-(x[k]||0));
 return a;
}
function renderList(){
 const a=filtered();
 document.getElementById('list').innerHTML=a.map(s=>{
  const main = s.held? s.unreal : s.realized;
  const lbl = s.held? '未实现' : '已实现';
  return `<div class="row ${sel===s.sym?'sel':''}" data-s="${s.sym}">
    <div><div class="sym">${s.sym}${s.hasLegacy?'<span class="legacychip">含旧仓</span>':''}</div>
    <div class="meta">${s.held? fmtN(s.shares)+' 股 @ '+fmt(s.avg):'已清仓 · '+s.numTrades+' 笔'}</div></div>
    <div class="pnl ${cls(main)}">${fmt(main)}<div class="meta">${lbl}</div></div>
  </div>`;}).join('') || '<div style="padding:16px;color:var(--mut)">无匹配</div>';
 document.querySelectorAll('.row').forEach(r=>r.onclick=()=>{sel=r.dataset.s;renderList();renderDetail();});
 if(!sel&&a.length){sel=a[0].sym;renderDetail();}
}

function chart(s){
 const W=900,H=420,mL=58,mR=120,mT=18,mB=42;
 const prices=s.prices; // [[date,close]]
 const txns=s.txns;
 // x domain
 const dates=prices.map(p=>+new Date(p[0]));
 let xmin=Math.min(...dates, +new Date('2026-03-28'));
 let xmax=+new Date('2026-05-28');
 // y domain
 let ys=[...prices.map(p=>p[1])];
 txns.forEach(t=>{if(t.price)ys.push(t.price); if(t.avg)ys.push(t.avg);});
 if(s.curPrice)ys.push(s.curPrice);
 ys=ys.filter(v=>v>0);
 let ymin=Math.min(...ys),ymax=Math.max(...ys);
 const pad=(ymax-ymin)*0.08||1; ymin-=pad;ymax+=pad;
 const xs=d=> mL+((+new Date(d)-xmin)/(xmax-xmin))*(W-mL-mR);
 const yc=v=> mT+(1-(v-ymin)/(ymax-ymin))*(H-mT-mB);
 let el='';
 // grid + y labels
 for(let i=0;i<=4;i++){const v=ymin+(ymax-ymin)*i/4;const y=yc(v);
   el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="#1c2336"/>`;
   el+=`<text x="${mL-8}" y="${y+4}" fill="#8a97b0" font-size="11" text-anchor="end">$${v.toFixed(0)}</text>`;}
 // x labels (monthly-ish, 5 ticks)
 for(let i=0;i<=5;i++){const t=xmin+(xmax-xmin)*i/5;const x=xs(new Date(t));
   const dt=new Date(t);const lab=(dt.getMonth()+1)+'/'+dt.getDate();
   el+=`<line x1="${x}" y1="${mT}" x2="${x}" y2="${H-mB}" stroke="#161d2e"/>`;
   el+=`<text x="${x}" y="${H-mB+18}" fill="#8a97b0" font-size="11" text-anchor="middle">${lab}</text>`;}
 // price line
 if(prices.length){
   const pts=prices.map(p=>`${xs(p[0]).toFixed(1)},${yc(p[1]).toFixed(1)}`).join(' ');
   el+=`<polyline points="${pts}" fill="none" stroke="#5b6b8c" stroke-width="1.6" stroke-opacity="0.9"/>`;
 }
 // avg cost step line
 const av=txns.filter(t=>t.avg>0&&t.pos>0.0001);
 if(av.length){
   let d2='';let prevx=null,prevy=null;
   av.forEach((t,i)=>{const x=xs(t.date==='≤2026-03-30'?'2026-03-30':t.date);const y=yc(t.avg);
     if(i===0){d2=`M ${x} ${y}`;}else{d2+=` L ${prevx} ${y} L ${x} ${y}`;}prevx=x;prevy=y;});
   d2+=` L ${xs('2026-05-28')} ${prevy}`;
   el+=`<path d="${d2}" fill="none" stroke="#fbbf24" stroke-width="1.8" stroke-dasharray="5 3" stroke-opacity="0.85"/>`;
 }
 // current price line
 if(s.curPrice&&s.held){const y=yc(s.curPrice);
   el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="#60a5fa" stroke-width="1.3" stroke-dasharray="2 3"/>`;
   el+=`<text x="${W-mR+6}" y="${y+4}" fill="#60a5fa" font-size="11">现价 $${s.curPrice}</text>`;}
 // markers
 const amts=txns.filter(t=>t.side!=='OPEN').map(t=>Math.abs(t.amount));
 const amax=Math.max(...amts,1);
 txns.forEach((t,idx)=>{
   const dd=t.date==='≤2026-03-30'?'2026-03-30':t.date;
   const x=xs(dd),y=yc(t.price);
   const r=t.side==='OPEN'?5:Math.max(4,Math.min(15,4+11*Math.sqrt(Math.abs(t.amount)/amax)));
   const col=t.side==='BUY'?'#34d399':(t.side==='SELL'?'#f87171':'#8a97b0');
   el+=`<circle cx="${x}" cy="${y}" r="${r}" fill="${col}" fill-opacity="0.55" stroke="${col}" stroke-width="1.4"
     data-i="${idx}" data-sym="${s.sym}" class="mk" style="cursor:pointer"/>`;
 });
 return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${el}</svg>`;
}

function renderDetail(){
 const s=stocks.find(x=>x.sym===sel); if(!s){document.getElementById('right').innerHTML='';return;}
 const badges=[
  ['当前持股',s.held?fmtN(s.shares)+' 股':'已清仓'],
  ['平均成本',s.held?fmt(s.avg):'—'],
  ['现价',fmt(s.curPrice)],
  ['市值',s.held?fmt(s.value):'—'],
  ['未实现盈亏',s.held?`<span class="${cls(s.unreal)}">${fmt(s.unreal)} (${pct(s.unrealPct)})</span>`:'—'],
  ['已实现盈亏(窗口)',`<span class="${cls(s.realized)}">${fmt(s.realized)}</span>`],
 ];
 const rows=s.txns.map(t=>{
   const tag=t.side==='BUY'?'<span class="tag b">买入</span>':(t.side==='SELL'?'<span class="tag s">卖出</span>':'<span class="tag o">期初底仓</span>');
   return `<tr>
    <td class="l">${t.date}</td><td class="l">${tag}</td>
    <td>${fmtN(t.qty,0)}</td><td>${fmt(t.price)}</td>
    <td class="${t.amount<0?'':'pos'}">${fmt(t.amount)}</td>
    <td>${fmtN(t.pos,0)}</td><td>${t.avg?fmt(t.avg):'—'}</td>
    <td class="${cls(t.realized)}">${t.realized==null?'—':fmt(t.realized)}</td></tr>`;}).join('');
 document.getElementById('right').innerHTML=`
 <div class="card">
   <div class="dh"><span class="t">${s.sym}</span><span class="nm">${s.name}</span>
     ${s.hasLegacy?'<span class="legacychip">含 3/30 前旧底仓 · 成本按当日市价估算</span>':''}</div>
   <div class="badges">${badges.map(b=>`<div class="badge"><div class="l">${b[0]}</div><div class="v">${b[1]}</div></div>`).join('')}</div>
   <div class="legend">
     <span><i style="background:#5b6b8c"></i>市场价格(Yahoo)</span>
     <span><i style="background:#fbbf24"></i>持仓平均成本</span>
     <span><i style="background:#60a5fa"></i>当前价</span>
     <span><i style="background:#34d399"></i>买入(大小=金额)</span>
     <span><i style="background:#f87171"></i>卖出</span>
   </div>
   <div class="chartbox">${chart(s)}</div>
 </div>
 <div class="card">
   <div style="font-weight:650;margin-bottom:6px">交易明细（${s.numTrades} 笔）</div>
   <div class="scroll"><table>
     <thead><tr><th class="l">日期</th><th class="l">动作</th><th>数量</th><th>成交价</th><th>金额</th><th>持仓后</th><th>均价后</th><th>已实现</th></tr></thead>
     <tbody>${rows}</tbody></table></div>
 </div>`;
 bindMarkers();
}

function bindMarkers(){
 const tt=document.getElementById('tt');
 document.querySelectorAll('.mk').forEach(m=>{
   m.onmousemove=e=>{const s=stocks.find(x=>x.sym===m.dataset.sym);const t=s.txns[+m.dataset.i];
     tt.style.display='block';tt.style.left=(e.clientX+14)+'px';tt.style.top=(e.clientY+14)+'px';
     const side=t.side==='BUY'?'买入':(t.side==='SELL'?'卖出':'期初底仓');
     tt.innerHTML=`<b>${m.dataset.sym} · ${side}</b><br>${t.date}<br>
       数量 ${fmtN(t.qty,0)} @ ${fmt(t.price)}<br>金额 ${fmt(t.amount)}
       ${t.realized!=null?'<br>已实现 <span class="'+cls(t.realized)+'">'+fmt(t.realized)+'</span>':''}`;};
   m.onmouseleave=()=>tt.style.display='none';
 });
}

// options section
function renderOptions(){
 if(!DATA.options.length)return'';
 const rows=DATA.options.map(o=>{
   const tr=o.txns.map(t=>`<tr><td class="l">${t.date}</td><td class="l">${t.side==='BUY'?'<span class="tag b">买入</span>':'<span class="tag s">卖出</span>'}</td><td>${t.qty}</td><td>${fmt(t.price)}</td><td class="${t.amount<0?'':'pos'}">${fmt(t.amount)}</td></tr>`).join('');
   return `<details><summary>${o.sym} · 净现金流 <span class="${cls(o.net)}">${fmt(o.net)}</span></summary>
     <table><thead><tr><th class="l">日期</th><th class="l">动作</th><th>合约</th><th>权利金</th><th>金额</th></tr></thead><tbody>${tr}</tbody></table></details>`;}).join('');
 return `<div class="card"><div style="font-weight:650;margin-bottom:6px">期权交易（净现金流合计 <span class="${cls(S.optNet)}">${fmt(S.optNet)}</span>）</div>${rows}</div>`;
}

document.getElementById('search').oninput=e=>{q=e.target.value.toLowerCase().trim();renderList();};
document.getElementById('sort').onchange=e=>{sortKey=e.target.value;renderList();};
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('on'));
 b.classList.add('on');filter=b.dataset.f;sel=null;renderList();});

renderList();
// append options + note after first detail
const obs=()=>{const r=document.getElementById('right');
 if(r&&!document.getElementById('optsec')){const d=document.createElement('div');d.id='optsec';d.innerHTML=renderOptions()+
   `<div class="card note"><b>计算口径说明</b><br>
   • 价格曲线为 Yahoo Finance 实际日收盘价；买卖点的 Y 轴为你的真实成交价。<br>
   • <b>未实现盈亏</b>采用券商 5/28 实际数据（精确）。<br>
   • <b>已实现盈亏</b>用平均成本法在数据窗口内计算；标「含旧仓」的股票在 3/30 之前已有底仓，其成本按 3/30 当日市场价估算，故为近似值。<br>
   • 数据窗口 ${S.dateRange[0]} → ${S.dateRange[1]}（约 2 个月），并非完整开户记录。</div>`;
   r.appendChild(d);} };
const mo=new MutationObserver(obs);mo.observe(document.getElementById('right'),{childList:true});
setTimeout(obs,300);
</script>
</body></html>'''

html=html.replace("__DATA__",DATA)
open("/Users/linji/Downloads/portfolio_dashboard.html","w").write(html)
print("written:", len(html),"bytes")
