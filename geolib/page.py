#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTML-страница просмотрщика (встраивается сервером)."""

HTML_PAGE = r'''<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>L2 Geodata Viewer</title>
<style>
:root{--bg:#14161a;--panel:#1d2026;--line:#2a2e36;--ink:#e6e8ee;--mut:#9aa3b2;--acc:#5ac8e0}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,'Segoe UI',Roboto,sans-serif;display:flex;height:100vh;overflow:hidden}
#side{width:230px;background:var(--panel);border-right:1px solid var(--line);overflow-y:auto;padding:10px}
#side h1{font-size:15px;color:var(--acc);margin-bottom:8px}
#side .set{font-size:11px;color:var(--mut);margin-bottom:10px;word-break:break-all}
.rgn{padding:4px 8px;border-radius:6px;cursor:pointer;display:flex;justify-content:space-between;font-variant-numeric:tabular-nums}
.rgn:hover{background:#262a32}.rgn.on{background:#2b3a45;color:var(--acc)}
.rgn .sz{color:var(--mut);font-size:11px}.rgn .stub{color:#e0b45a;font-size:11px}
#main{flex:1;display:flex;flex-direction:column;overflow:hidden}
#bar{padding:8px 14px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:center;min-height:46px}
#bar b{color:var(--acc);font-size:15px}
select{background:#262a32;color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:12px;cursor:pointer}
select:focus{outline:none;border-color:var(--acc)}
#layer-box{display:inline-flex}
.lbl{color:var(--mut);font-size:13px}
#wrap{flex:1;display:flex;overflow:hidden}
#cv-box{flex:1;overflow:auto;display:flex;align-items:flex-start;justify-content:center;padding:14px;position:relative}
.mapctl{position:absolute;top:22px;right:22px;display:flex;flex-direction:column;gap:6px;z-index:5}
.mapctl button{width:34px;height:34px;background:rgba(29,32,38,.92);color:var(--ink);border:1px solid var(--line);border-radius:8px;cursor:pointer;font-size:16px;line-height:1;transition:color .15s,border-color .15s}
.mapctl button:hover{border-color:var(--acc);color:var(--acc)}
.mapctl button:focus-visible{outline:2px solid var(--acc);outline-offset:1px}
#zoom{width:34px;text-align:center;font-size:11px;color:var(--mut);font-variant-numeric:tabular-nums}
#help-pop{position:absolute;top:22px;right:64px;background:#0d0f12;border:1px solid var(--line);border-radius:8px;padding:10px 12px;font-size:12px;color:var(--mut);display:none;z-index:6;line-height:1.8;white-space:nowrap}
#help-pop b{color:var(--ink);font-weight:500}
#toast{position:absolute;top:22px;left:50%;transform:translateX(-50%);background:rgba(13,15,18,.95);border:1px solid var(--line);border-radius:8px;padding:8px 14px;font-size:12.5px;color:var(--ink);z-index:6;display:none;max-width:80%;text-align:center;box-shadow:0 4px 16px rgba(0,0,0,.4)}
canvas{image-rendering:pixelated;border:1px solid var(--line);border-radius:4px;cursor:crosshair}
#insp{width:330px;border-left:1px solid var(--line);background:var(--panel);overflow-y:auto;padding:12px}
#insp h2{font-size:13px;color:var(--acc);margin:8px 0 6px}
#insp .kv{display:flex;justify-content:space-between;font-size:12px;padding:2px 0;font-variant-numeric:tabular-nums}
#insp .kv span:first-child{color:var(--mut)}
#cells{display:grid;grid-template-columns:repeat(8,1fr);gap:2px;margin:8px 0}
#cells div{aspect-ratio:1;border-radius:3px;cursor:pointer;border:1px solid transparent;position:relative}
#cells div:hover{border-color:var(--acc)}#cells div.sel{border-color:#fff}
#cells div .ml{position:absolute;right:1px;top:0;font-size:9px;color:rgba(0,0,0,.65);font-weight:700}
.layer{background:#262a32;border-radius:6px;padding:6px 8px;margin:4px 0;font-size:12px;display:flex;justify-content:space-between;align-items:center}
.nswe{display:inline-flex;gap:3px}.nswe i{font-style:normal;width:16px;height:16px;border-radius:3px;display:inline-flex;align-items:center;justify-content:center;font-size:10px;background:#33404a;color:#7fd18a}
.nswe i.x{background:#4a3333;color:#d17f7f;text-decoration:line-through}
#legend{padding:7px 14px;border-top:1px solid var(--line);display:flex;gap:10px;align-items:center;font-size:11px;color:var(--mut);min-height:30px;flex-wrap:wrap}
#grad{width:180px;height:10px;border-radius:5px}
#stats{margin-left:auto;font-variant-numeric:tabular-nums}
.mut{color:var(--mut)}
#tip{position:fixed;pointer-events:none;background:#0d0f12;border:1px solid var(--line);border-radius:6px;padding:6px 9px;font-size:12px;display:none;z-index:9;font-variant-numeric:tabular-nums}
</style></head><body>
<div id="side"><h1>⛰ Geodata Viewer</h1><div class="set" id="setname"></div><div id="list"></div></div>
<div id="main">
  <div id="bar"><b id="title">выбери регион</b>
    <span id="layer-box" style="display:none;align-items:center;gap:6px">
      <label for="slice-sel" class="lbl">Слой:</label>
      <select id="slice-sel" aria-label="Выбор слоя"></select></span></div>
  <div id="wrap">
    <div id="cv-box"><canvas id="cv" width="1024" height="1024" style="display:none"></canvas>
      <div class="mapctl" id="mapctl" style="display:none">
        <button id="z-in" aria-label="Приблизить">+</button>
        <button id="z-out" aria-label="Отдалить">−</button>
        <button id="z-reset" aria-label="Показать весь регион">⌂</button>
        <div id="zoom">×1</div>
        <button id="dl-btn" aria-label="Скачать PNG региона" title="Скачать PNG региона (2048×2048, текущий слой)">⬇</button>
        <button id="help-btn" aria-label="Управление картой">?</button>
      </div>
      <div id="toast"></div>
      <div id="help-pop">
        <b>колесо</b> — зум к курсору<br>
        <b>перетаскивание</b> — панорама<br>
        <b>клик по карте</b> — открыть блок<br>
        <b>клик по ячейке</b> — слои и проходимость<br>
        <span style="color:#e05a5a">━</span> закрытые направления (зум ≥8)<br>
        <span>▪</span> белая точка — мульти-слойный блок<br>
        <b>⬇</b> — скачать PNG региона (текущий слой)
      </div>
    </div>
    <div id="insp"><h2>Инспектор</h2><div id="insp-body" class="mut">Выбери блок на карте.</div></div>
  </div>
  <div id="legend"><span id="leg-name">высота:</span><canvas id="grad" width="180" height="10"></canvas>
    <span id="lo"></span>–<span id="hi"></span>
    <span id="leg-note"></span>
    <span id="stats"></span></div>
</div>
<div id="tip"></div>
<script>
const STOPS=[[0,[26,35,64]],[.125,[35,72,107]],[.25,[46,109,117]],[.375,[61,143,111]],
 [.5,[106,174,106]],[.625,[168,192,122]],[.75,[211,201,154]],[.875,[236,227,200]],[1,[255,255,255]]];
function elev(h,lo,hi){ // палитра нормализована на диапазон высот региона
 const t=hi<=lo?0.5:Math.max(0,Math.min(1,(h-lo)/(hi-lo)));
 for(let i=1;i<STOPS.length;i++){const[t1,c1]=STOPS[i],[t0,c0]=STOPS[i-1];
  if(t<=t1){const f=(t-t0)/(t1-t0);return c0.map((a,j)=>Math.round(a+(c1[j]-a)*f));}}
 return STOPS.at(-1)[1];}
let cur=null,sum=null,selBlock=null;
const $=id=>document.getElementById(id);
fetch('/api/meta').then(r=>r.json()).then(m=>{$('setname').textContent=m.primary;});
fetch('/api/regions').then(r=>r.json()).then(rs=>{
 $('list').innerHTML=rs.map(r=>`<div class="rgn" data-n="${r.name}"><span>${r.name}</span>`+
  `<span class="${r.stub?'stub':'sz'}">${r.stub?'заглушка':(r.size/1048576).toFixed(1)+'M'}</span></div>`).join('');
 document.querySelectorAll('.rgn').forEach(el=>el.onclick=()=>load(el.dataset.n));});
const grad=$('grad').getContext('2d');
function drawLegend(lo,hi){
 for(let x=0;x<180;x++){const[r,g,b]=elev(lo+x/180*(hi-lo),lo,hi);
  grad.fillStyle=`rgb(${r},${g},${b})`;grad.fillRect(x,0,1,10);}
 $('leg-name').textContent='высота поверхности:';
 $('lo').textContent=lo;$('hi').textContent=hi;$('leg-note').textContent='';}
function drawSliceLegend(lo,hi,n){
 drawLegend(lo,hi);
 $('leg-name').textContent=`высота (слой ${n} сверху):`;
 $('leg-note').textContent='тёмные блоки — такого слоя нет';}
drawLegend(-16384,16384);
let SL=-1,sliceGrid=null; // текущий срез региона (-1 = поверхность)
async function load(name){
 document.querySelectorAll('.rgn').forEach(e=>e.classList.toggle('on',e.dataset.n===name));
 $('title').textContent=name;$('stats').textContent='загрузка…';
 sum=await fetch('/api/region/'+name).then(r=>r.json());cur=name;selBlock=null;
 Z=1;OX=0;OY=0;clearNswe();
 buildLayerSelect();
 await setLayer(-1);
 $('insp-body').innerHTML='<span class="mut">Выбери блок на карте.</span>';
 $('mapctl').style.display='';
 $('stats').textContent=`flat ${sum.nf} · complex ${sum.nc} · multi ${sum.nm} · h ∈ [${sum.gmin}, ${sum.gmax}]`;}
function buildLayerSelect(){
 const maxL=sum.lm.reduce((a,b)=>a>b?a:b,0);
 const sel=$('slice-sel');
 sel.innerHTML='<option value="-1">1 — поверхность</option>';
 for(let i=1;i<maxL;i++)
  sel.insertAdjacentHTML('beforeend',`<option value="${i}">${i+1}${i===1?' — под поверхностью':''}</option>`);
 sel.value='-1';sel.disabled=false;
 $('layer-box').style.display=maxL>1?'inline-flex':'none';
 sel.onchange=()=>setLayer(+sel.value);}
async function setLayer(li){
 SL=li;clearNswe();
 if(SL>=0){
  sliceGrid=await fetch(`/api/region/${cur}?layer=${SL}`).then(r=>r.json());
  drawSliceLegend(sum.gmin,sum.gmax,SL+1);
  toast(`<b>Слой ${SL+1} сверху</b>: мосты, этажи, подземелья. Тёмные блоки — на этой глубине слоя нет.`);
 }else{
  sliceGrid=null;
  drawLegend(sum.gsmin,sum.gmax);
 }
 draw();}
let toastTimer=null;
function toast(html,ms){const t=$('toast');t.innerHTML=html;t.style.display='block';
 clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.style.display='none',ms||6000);
 t.onclick=()=>t.style.display='none';}
const cv=$('cv'),ctx=cv.getContext('2d');
const base=document.createElement('canvas');base.width=1024;base.height=1024;
const bctx=base.getContext('2d');
let Z=1,OX=0,OY=0; // зум и смещение вьюпорта (в пикселях base)
function renderBase(){if(!sum)return;
 const img=bctx.createImageData(1024,1024);
 for(let bx=0;bx<256;bx++)for(let by=0;by<256;by++){
  const i=bx*256+by;let col;
  if(SL>=0&&sliceGrid){const h=sliceGrid.hmax[i];
   col=h===null?[26,29,35]:elev(h,sum.gmin,sum.gmax);}
  else{col=elev(sum.hmax[i],sum.gsmin,sum.gmax);if(sum.t[i]===2)col=col.map(v=>Math.max(0,v-18));}
  for(let dy=0;dy<4;dy++)for(let dx=0;dx<4;dx++){
   const p=((by*4+dy)*1024+bx*4+dx)*4;
   img.data[p]=col[0];img.data[p+1]=col[1];img.data[p+2]=col[2];img.data[p+3]=255;}
 }
 bctx.putImageData(img,0,0);}
function clampView(){const vw=1024/Z;OX=Math.max(0,Math.min(1024-vw,OX));OY=Math.max(0,Math.min(1024-vw,OY));}
function draw(){if(!sum)return;cv.style.display='';
 renderBase();blit();}
let nsweCache={},nsweTimer=null,nsweBusy=false;
function clearNswe(){nsweCache={};}
function blit(){clampView();
 ctx.imageSmoothingEnabled=false;
 ctx.clearRect(0,0,1024,1024);
 ctx.drawImage(base,OX,OY,1024/Z,1024/Z,0,0,1024,1024);
 { // метки мульти-слойных блоков: фиксированный размер на любом зуме
  ctx.fillStyle='rgba(255,255,255,.85)';
  const bx0=Math.max(0,Math.floor(OX/4)),by0=Math.max(0,Math.floor(OY/4));
  const bx1=Math.min(255,Math.ceil((OX+1024/Z)/4)),by1=Math.min(255,Math.ceil((OY+1024/Z)/4));
  for(let bx=bx0;bx<=bx1;bx++)for(let by=by0;by<=by1;by++)
   if(sum.t[bx*256+by]===2)ctx.fillRect((bx*4-OX)*Z,(by*4-OY)*Z,2,2);}
 if(Z>=8)drawNswe();
 if(selBlock){ctx.strokeStyle='#fff';ctx.lineWidth=Math.max(1,Z/2);
  ctx.strokeRect((selBlock[0]*4-OX)*Z-.5,(selBlock[1]*4-OY)*Z-.5,4*Z+1,4*Z+1);}
 $('zoom').textContent='×'+Z;}
function drawNswe(){ // красные грани = закрытые направления
 const bx0=Math.max(0,Math.floor(OX/4)),by0=Math.max(0,Math.floor(OY/4));
 const bx1=Math.min(255,Math.ceil((OX+1024/Z)/4)),by1=Math.min(255,Math.ceil((OY+1024/Z)/4));
 const missing=[];
 ctx.strokeStyle='rgba(255,80,80,.9)';ctx.lineWidth=Math.max(1,Z/16);
 for(let bx=bx0;bx<=bx1;bx++)for(let by=by0;by<=by1;by++){
  const key=bx+'_'+by,vals=nsweCache[key];
  if(vals===undefined){missing.push(key);continue;}
  for(let cx=0;cx<8;cx++)for(let cy=0;cy<8;cy++){
   const v=vals[cx*8+cy];
   if(v===null||v===15)continue;
   const x=((bx*4+cx*.5)-OX)*Z,y=((by*4+cy*.5)-OY)*Z,s=.5*Z;
   ctx.beginPath();
   if(!(v&8)){ctx.moveTo(x,y);ctx.lineTo(x+s,y);}         // N закрыт → верх
   if(!(v&4)){ctx.moveTo(x,y+s);ctx.lineTo(x+s,y+s);}     // S → низ
   if(!(v&2)){ctx.moveTo(x,y);ctx.lineTo(x,y+s);}         // W → лево
   if(!(v&1)){ctx.moveTo(x+s,y);ctx.lineTo(x+s,y+s);}     // E → право
   ctx.stroke();}}
 if(missing.length&&!nsweBusy){
  clearTimeout(nsweTimer);
  nsweTimer=setTimeout(async()=>{
   nsweBusy=true;
   try{const r=await fetch(`/api/nswe/${cur}?bx0=${bx0}&by0=${by0}&bx1=${bx1}&by1=${by1}&layer=${SL}`).then(x=>x.json());
    Object.assign(nsweCache,r.b);}finally{nsweBusy=false;}
   blit();},150);}}
function toBlock(e){const r=cv.getBoundingClientRect();
 const px=OX+(e.clientX-r.left)/r.width*1024/Z,py=OY+(e.clientY-r.top)/r.height*1024/Z;
 return [Math.floor(px/4),Math.floor(py/4)];}
function zoomAt(sx,sy,dir){ // dir: +1 приблизить, -1 отдалить
 const px=OX+sx/Z,py=OY+sy/Z;
 Z=dir>0?Math.min(32,Z*2):Math.max(1,Z/2);
 OX=px-sx/Z;OY=py-sy/Z;blit();}
cv.addEventListener('wheel',e=>{if(!sum)return;e.preventDefault();
 const r=cv.getBoundingClientRect();
 zoomAt((e.clientX-r.left)/r.width*1024,(e.clientY-r.top)/r.height*1024,e.deltaY<0?1:-1);},{passive:false});
$('z-in').onclick=()=>{if(sum)zoomAt(512,512,1);};
$('z-out').onclick=()=>{if(sum)zoomAt(512,512,-1);};
$('z-reset').onclick=()=>{if(sum){Z=1;OX=0;OY=0;blit();}};
$('dl-btn').onclick=()=>{if(!cur)return;
 toast('Рендер PNG 2048×2048… файл скачается через несколько секунд.',4000);
 const a=document.createElement('a');
 a.href=`/api/render/${cur}?layer=${SL}`;a.download='';
 document.body.appendChild(a);a.click();a.remove();};
$('help-btn').onclick=()=>{const p=$('help-pop');p.style.display=p.style.display==='none'||!p.style.display?'block':'none';};
document.addEventListener('click',e=>{if(!e.target.closest('#help-btn,#help-pop'))$('help-pop').style.display='none';});
let dragging=false,moved=0,lx=0,ly=0;
cv.onmousedown=e=>{dragging=true;moved=0;lx=e.clientX;ly=e.clientY;};
window.addEventListener('mouseup',()=>dragging=false);
const tip=$('tip');
cv.onmousemove=e=>{if(!sum)return;
 if(dragging){const r=cv.getBoundingClientRect();
  const dx=(e.clientX-lx)/r.width*1024/Z,dy=(e.clientY-ly)/r.height*1024/Z;
  OX-=dx;OY-=dy;moved+=Math.abs(e.clientX-lx)+Math.abs(e.clientY-ly);
  lx=e.clientX;ly=e.clientY;blit();tip.style.display='none';return;}
 const [bx,by]=toBlock(e);
 if(bx<0||by<0||bx>255||by>255){tip.style.display='none';return;}
 const i=bx*256+by,[rx,ry]=cur.split('_').map(Number);
 const wx=(rx-20)*32768+bx*128,wy=(ry-18)*32768+by*128;
 const T=['flat','complex','multi'][sum.t[i]];
 let extra=SL>=0&&sliceGrid?`<br>срез слой ${SL+1}: `+(sliceGrid.hmax[i]===null?'нет':'h='+sliceGrid.hmax[i]):'';
 tip.innerHTML=`блок ${bx},${by} · ${T}<br>мир ≈ ${wx}, ${wy}<br>h ∈ [${sum.hmin[i]}, ${sum.hmax[i]}] · слоёв ≤ ${sum.lm[i]}${extra}`;
 tip.style.display='block';tip.style.left=(e.clientX+14)+'px';tip.style.top=(e.clientY+14)+'px';};
cv.onmouseleave=()=>{tip.style.display='none';dragging=false;};
cv.onclick=async e=>{if(!sum||moved>4)return;
 const [bx,by]=toBlock(e);
 if(bx<0||by<0||bx>255||by>255)return;
 selBlock=[bx,by];blit();
 const d=await fetch(`/api/block/${cur}/${bx}/${by}`).then(r=>r.json());
 showBlock(bx,by,d);};
function showBlock(bx,by,d){
 const[rx,ry]=cur.split('_').map(Number);
 const wx=(rx-20)*32768+bx*128,wy=(ry-18)*32768+by*128;
 // слои каждой ячейки отсортированы сверху вниз
 const cellsSorted=d.cells.map(c=>[...c].sort((a,b)=>b[0]-a[0]));
 const maxL=Math.max(...cellsSorted.map(c=>c.length));
 let html=`<h2>Блок ${bx},${by}</h2>
  <div class="kv"><span>мир. коорд.</span><span>${wx} … ${wx+128}, ${wy} … ${wy+128}</span></div>
  <div class="kv"><span>тип</span><span>${['flat','complex','multilayer'][d.type]}</span></div>
  <h2>Ячейки 8×8</h2>`;
 if(maxL>1){
  html+=`<div id="lsel" style="margin:4px 0"><select id="lsel-sel">
   <option value="-1">поверхность</option>`;
  for(let i=0;i<maxL;i++)html+=`<option value="${i}">слой ${i+1}${i===0?' (верхний)':''}</option>`;
  html+=`</select></div><div class="mut" style="font-size:11px;margin:2px 0 6px">срез: ячейки без такого слоя гаснут</div>`;}
 html+=`<div id="cells"></div><div id="layers" class="mut">Клик по ячейке → слои.</div>`;
 $('insp-body').innerHTML=html;
 function paintCells(li){ // li=-1: верхний слой каждой ячейки; иначе N-й сверху
  let h='';
  for(let cy=0;cy<8;cy++)for(let cx=0;cx<8;cx++){
   const cell=cellsSorted[cx*8+cy];
   const layer=li<0?cell[0]:cell[li];
   let style,body='';
   if(layer===undefined){style='background:#22262d;opacity:.35';}
   else{const[r,g,b]=elev(layer[0],sum.gmin,sum.gmax);style=`background:rgb(${r},${g},${b})`;
    if(li<0&&cell.length>1)body=`<span class="ml">${cell.length}</span>`;}
   h+=`<div data-c="${cx*8+cy}" style="${style}" title="ячейка ${cx},${cy}${layer!==undefined?' · h='+layer[0]:' · слоя нет'}">${body}</div>`;}
  $('cells').innerHTML=h;
  bindCells();}
 if(maxL>1)$('lsel-sel').onchange=e=>paintCells(+e.target.value);
 paintCells(-1);
 function bindCells(){document.querySelectorAll('#cells div').forEach(el=>el.onclick=()=>{
  document.querySelectorAll('#cells div').forEach(x=>x.classList.remove('sel'));
  el.classList.add('sel');
  const ci=+el.dataset.c,cell=cellsSorted[ci];
  const cx=Math.floor(ci/8),cy=ci%8;
  let h=`<h2>Ячейка ${cx},${cy} · мир ${wx+cx*16}, ${wy+cy*16}</h2>`;
  h+=cell.map((l,i)=>layerHtml(l,i,cell.length)).join('');
  if(cell.length>1)h+=`<div class="mut" style="font-size:11px;margin:6px 0">`+
   `Слои — это уровни проходимой поверхности на одной точке карты по вертикали: `+
   `мост над землёй, этажи здания, подземелье под поверхностью. Персонаж стоит `+
   `на слое, ближайшем к его Z; NSWE показывает, в какие стороны с него можно шагнуть.</div>`;
  $('layers').innerHTML=h;});}}
function layerHtml([hh,nswe],idx,total){
 const dir=[['N',8],['S',4],['W',2],['E',1]];
 const tag=total>1?`<span class="mut" style="font-size:10px">слой ${idx+1}${idx===0?' (верхний)':idx===total-1?' (нижний)':''}</span> `:'';
 return `<div class="layer"><span>${tag}h = <b>${hh}</b></span><span class="nswe">`+
  dir.map(([n,b])=>`<i class="${nswe&b?'':'x'}" title="${n}: ${nswe&b?'проход открыт':'заблокировано'}">${n}</i>`).join('')+`</span></div>`;}
</script></body></html>'''
