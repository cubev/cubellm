"use strict";
const $ = (id) => document.getElementById(id);
const labels = {active:"Aktiv",paused:"Pausiert",cancelled:"Beendet",unknown:"Status offen",not_configured:"Nicht angebunden"};
const sources = {provider_api:"Anbieterbericht",local_collector:"Subscription-Collector",manual:"Manuell erfasst",import:"Import"};
const names = {openai:"OpenAI",anthropic:"Anthropic",openrouter:"OpenRouter",xai:"xAI",heygen:"HeyGen",suno:"Suno"};
let accounts = [], filter = "all";
function element(tag, cls, text) { const e=document.createElement(tag); if(cls)e.className=cls; if(text!==undefined)e.textContent=text; return e; }
function age(value) { const seconds=(Date.now()-Date.parse(value))/1000; if(!Number.isFinite(seconds))return "Zeit unbekannt"; if(seconds< -60)return "Uhrzeit prüfen"; if(seconds<60)return "gerade eben"; if(seconds<3600)return `vor ${Math.floor(seconds/60)} Min`; if(seconds<86400)return `vor ${Math.floor(seconds/3600)} Std`; return `vor ${Math.floor(seconds/86400)} Tagen`; }
function number(value, unit) { if(unit==="USD"||unit==="EUR")return new Intl.NumberFormat("de-DE",{style:"currency",currency:unit}).format(Number(value));return new Intl.NumberFormat("de-DE",{maximumFractionDigits:2}).format(Number(value))+" "+unit; }
function metric(card,label,value,small=false) { const row=element("div","metric");row.append(element("span","",label),element("strong",small?"small":"",value));card.append(row); }
function isFresh(reading) {
 const delta=(Date.now()-Date.parse(reading.observed_at))/1000;
 if(!Number.isFinite(delta)||delta< -60||delta>reading.fresh_for_seconds)return false;
 if(reading.expires_at&&Date.parse(reading.expires_at)<=Date.now())return false;
 if(reading.metric==="limit"&&reading.resets_at&&Date.parse(reading.resets_at)<=Date.now())return false;
 if(["spend","budget"].includes(reading.metric)&&reading.period!==new Date().toISOString().slice(0,7))return false;
 return true;
}
function renderAccounts() {
 const visible=accounts.filter(a=>filter==="all"||a.data.account.kind===filter);$("accounts").replaceChildren();$("count").textContent=`${visible.length} Konten · ${new Set(visible.map(a=>a.data.account.provider)).size} Anbieter`;
 for(const item of visible) {
  const data=item.data, account=data.account, card=element("article","card"), head=element("div","card-head"), title=element("div");
  title.append(element("div","provider",`${names[account.provider]} · ${account.kind==="api"?"API":"Subscription"}`),element("h3","",account.name));
  head.append(title,element("span","badge "+(account.state==="active"?"active":""),labels[account.state]));card.append(head);
  const current=item.readings.filter(v=>v.current&&isFresh(v.reading)), limits=current.filter(v=>v.reading.metric==="limit");
  for(const v of limits) {const r=v.reading;metric(card,r.window,`${Math.round(Number(r.value)*100)} % genutzt`);const track=element("div","track"),fill=element("div","fill");fill.style.width=`${Math.min(100,Math.max(0,Number(r.value)*100))}%`;track.append(fill);card.append(track);}
  const spend=data.selected_spend&&isFresh(data.selected_spend)?data.selected_spend:null;
  if(spend)metric(card,spend.status==="manual"?"Monatskosten (manuell)":spend.status==="estimated"?"Monatskosten (geschätzt)":"Gemeldete Monatskosten",number(spend.value,spend.unit));
  for(const v of current.filter(v=>v.reading.metric==="budget")) {
   const r=v.reading;
   metric(card,"Selbst gesetztes Monatsbudget",number(r.value,r.unit));
   if(spend&&spend.unit===r.unit&&spend.period===r.period) {
    const remaining=Number(r.value)-Number(spend.value);
    metric(card,remaining<0?"Budget überschritten um":"Verbleibendes Monatsbudget",number(Math.abs(remaining),r.unit));
   }
  }
  for(const v of current.filter(v=>["balance","recurring_cost","usage"].includes(v.reading.metric))) {const r=v.reading;metric(card,`${r.metric==="balance"?"Guthaben":r.metric==="recurring_cost"?"Abokosten":"Nutzung"}${r.window?" · "+r.window:""}`,number(r.value,r.unit));}
  if(!current.length&&!spend)card.append(element("p","empty","Noch keine verifizierten aktuellen Werte"));
  for(const [currency,value] of Object.entries(data.proxy_amounts)) {metric(card,"Beobachteter Proxy-Verbrauch",number(value,currency),true);card.append(element("p","explanation","Separat erfasst. Wird nicht zum Anbieterbericht addiert."));}
  for(const [unit,value] of Object.entries(data.open_reservations))metric(card,"Für laufende Aufträge reserviert",number(value,unit),true);
  const errors=item.readings.filter(v=>v.reading.status==="error"||v.reading.status==="not_configured");
  if(errors.length)card.append(element("p","explanation error",errors[0].reading.message||"Datenquelle nicht verfügbar"));
  if(account.note)card.append(element("p","explanation",account.note));
  const latest=[...item.readings].sort((a,b)=>Date.parse(b.reading.observed_at)-Date.parse(a.reading.observed_at))[0];
  const foot=element("div","source");foot.append(element("span","",latest?sources[latest.reading.source]:"Keine Datenquelle verbunden"),element("span","",latest?`${isFresh(latest.reading)?"":"Veraltet · "}${age(latest.reading.observed_at)}`:""));card.append(foot);$("accounts").append(card);
 }
}
function renderFleet(fleet) {
 $("fleet").replaceChildren();if(!fleet){$("fleet").append(element("div","empty","Noch kein zentraler Systembericht"));return;}
 $("fleet-age").textContent=age(fleet.checked_at);
 for(const host of fleet.hosts){const stale=Date.now()-Date.parse(host.checked_at)>900000;const card=element("article","host"),title=element("h3","",host.id.toUpperCase());title.append(element("span","badge "+(host.status==="ok"&&!stale?"active":"warning"),stale?"Veraltet":host.status==="ok"?"Erreichbar":host.status==="snapshot_only"?"Snapshot":"Nicht erreichbar"));card.append(title,element("p","host-detail",host.hostname||host.address),element("p","",`${host.repositories.length} Projekte · ${host.services.length} geladene Dienste`),element("p","",age(host.checked_at)));
  if(host.message)card.append(element("p","",host.message));if(host.repositories.length){const details=element("details"),summary=element("summary","","Projekte anzeigen");details.append(summary);for(const repo of host.repositories){const row=element("div","repo");row.append(element("b","",repo.name),element("div","",`${repo.branch||"Branch unbekannt"} · ${repo.commit||"Commit unbekannt"}${repo.tracked_changes?" · "+repo.tracked_changes+" Änderungen":""}`));details.append(row);}card.append(details);}$("fleet").append(card);
 }
}
async function refresh() { $("refresh").disabled=true;try{const [s,f]=await Promise.all([fetch("/api/v1/summary",{cache:"no-store"}),fetch("/api/v1/fleet",{cache:"no-store"})]);if(!s.ok||!f.ok)throw new Error("unavailable");const data=await s.json();accounts=data.accounts;$("period").textContent=new Date(data.period+"-01T12:00:00Z").toLocaleDateString("de-DE",{month:"long",year:"numeric"});$("connection").textContent="Aktualisiert "+new Date(data.generated_at).toLocaleTimeString("de-DE",{hour:"2-digit",minute:"2-digit"});const stale=Date.now()-Date.parse(data.generated_at)>900000||(data.transport_status&&data.transport_status!=="ok");$("notice").hidden=!stale;if(stale)$("notice").textContent="Verbindung zur Zentrale unterbrochen. Die Übersicht zeigt den letzten erfolgreichen Stand; veraltete Messwerte werden ausgeblendet.";renderAccounts();renderFleet(await f.json());}catch{$("connection").textContent="Verbindung unterbrochen";$("notice").hidden=false;$("notice").textContent="Die zentrale Übersicht ist gerade nicht erreichbar. Bereits angezeigte Werte stammen vom letzten erfolgreichen Abruf.";renderAccounts();}finally{$("refresh").disabled=false;}}
$("refresh").addEventListener("click",refresh);document.querySelectorAll("[data-filter]").forEach(button=>button.addEventListener("click",()=>{filter=button.dataset.filter;document.querySelectorAll("[data-filter]").forEach(b=>b.classList.toggle("selected",b===button));renderAccounts();}));refresh();setInterval(refresh,60000);
