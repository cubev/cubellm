"use strict";
const management = (() => {
 const byId = (id) => document.getElementById(id);
 const PENDING = "cube-control-pending-commands";
 const PROVIDER = /^[a-z](?:[a-z0-9_-]{0,62}[a-z0-9])?$/;
 const IDENTIFIER = /^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$/;
 const DECIMAL = /^-?\d{1,16}(\.\d{1,8})?$/;
 const stateLabels = {active:"Aktiv",paused:"Pausiert",cancelled:"Beendet",unknown:"Status offen",not_configured:"Nicht angebunden"};
 const gatewayStates = {current:"Aktuell",stale:"Veraltet",last_attempt_failed:"Letzter Abruf fehlgeschlagen",never_collected:"Noch nie erfasst"};
 const sourceStates = {complete:"vollständig",partial:"teilweise lesbar",unavailable:"nicht lesbar"};
 const findingLabels = {unbound_credential:"Zugang ohne Kontobindung",unbound_route:"Route ohne Kontobindung",invalid_credential:"Ungültige Zugangsbindung",invalid_route:"Ungültige Routenbindung",unattributed_deployment:"Modell ohne gebundenen Zugang",binding_conflict:"Zugang mehreren Konten zugeordnet",rejected_record:"Eintrag nicht lesbar",source_partial:"Quelle teilweise lesbar",source_unavailable:"Quelle nicht lesbar"};
 const fields = [
  {field:"name",label:"Name",kind:"text",max:100,clearable:false},
  {field:"state",label:"Status",kind:"state"},
  {field:"plan",label:"Tarif",kind:"text",max:100,clearable:true},
  {field:"note",label:"Notiz",kind:"text",max:300,clearable:true},
  {field:"budget_usd",label:"Monatsbudget (USD)",kind:"decimal",period:true},
  {field:"recurring_usd",label:"Abopreis / Monat (USD)",kind:"decimal"},
  {field:"balance_usd",label:"Guthaben (USD)",kind:"decimal"},
  {field:"balance_credits",label:"Credits",kind:"decimal"},
 ];
 let enabled = false, csrf = null, actor = "", unknownOnly = false, inventoryTicket = 0, inventoryLoadedAt = null;
 const inFlight = new Set();
 const editors = new Map();
 const awaitingFresh = new Map();
 const enrollments = new Map();
 const referenceLabels = {catalogue:"Kontenkatalog",gateway_credential:"Gateway-Zugang",gateway_route:"Gateway-Route",observation:"Messwert",usage_event:"Nutzungsereignis",gateway_record:"Gateway-Aufruf",media_job:"Medienauftrag",manual_entry:"Manuelle Angabe"};
 const gapLabels = {inventory_not_collected:"Kein Gateway-Inventar erfasst",not_in_catalogue:"Nicht im Kontenkatalog",no_gateway_credential:"Kein Gateway-Zugang",credential_only_in_database:"Zugang nur gespeichert, nicht geladen",credential_only_in_runtime:"Zugang nur geladen, nicht gespeichert",credential_unused:"Zugang ungenutzt",binding_not_in_latest_collection:"Bindung nur aus früherem Scan",invalid_binding:"Ungültige Bindung",no_usage_source:"Keine Nutzungsquelle",no_verified_provider_usage:"Keine bestätigte Anbieternutzung",provider_usage_not_current:"Anbieternutzung nicht aktuell",no_confirmed_credential_binding:"Keine bestätigte Zuordnung zu Inventurfunden"};
 function labelled_codes(prefix, codes, labels) { const p=node("p","explanation",`${prefix}: ${codes.map((code)=>labels[code]||code).join(", ")}`); p.title=codes.join(", "); return p; }
 function node(tag, cls, text) { const e=document.createElement(tag); if(cls)e.className=cls; if(text!==undefined)e.textContent=text; return e; }
 function button(text, cls, handler) { const b=node("button",cls,text); b.type="button"; b.addEventListener("click",handler); return b; }
 function since(value) { const seconds=(Date.now()-Date.parse(value))/1000; if(!Number.isFinite(seconds))return "Zeit unbekannt"; if(seconds<60)return "gerade eben"; if(seconds<3600)return `vor ${Math.floor(seconds/60)} Min`; if(seconds<86400)return `vor ${Math.floor(seconds/3600)} Std`; return `vor ${Math.floor(seconds/86400)} Tagen`; }
 function pending() { try { const value=JSON.parse(sessionStorage.getItem(PENDING)||"{}"); return value&&typeof value==="object"?value:{}; } catch { return {}; } }
 function remember(command) { sessionStorage.setItem(PENDING, JSON.stringify({...pending(), [command.account_id]: command})); }
 function forget(accountId) { const rest={...pending()}; delete rest[accountId]; sessionStorage.setItem(PENDING, JSON.stringify(rest)); }
 function notify(text, error=false) { const box=byId("management-status"); box.textContent=text; box.classList.toggle("error", error); }
 async function call(method, path, body) {
  const response=await fetch(path,{method,credentials:"same-origin",cache:"no-store",headers:{"Content-Type":"application/json",...(csrf?{"X-CSRF-Token":csrf}:{})},body:body===undefined?undefined:JSON.stringify(body)});
  let data={}, valid=true;
  try { const parsed=await response.json(); valid=Boolean(parsed)&&typeof parsed==="object"&&!Array.isArray(parsed); data=valid?parsed:{}; }
  catch { valid=false; }
  return {status:response.status,data,valid};
 }
 async function reloadAll() { if(typeof refresh==="function")await refresh(); else await loadInventory(); }
 function blocked(accountId) {
  if(inFlight.has(accountId)){ notify("Für dieses Konto wird gerade gespeichert.",true); return true; }
  if(awaitingFresh.has(accountId)){ notify("Die Ansicht dieses Kontos wird nach der letzten Änderung noch aktualisiert.",true); return true; }
  if(pending()[accountId]){ notify("Für dieses Konto ist ein Befehl mit unbekanntem Ergebnis offen. Erst denselben Befehl erneut senden oder ihn ausdrücklich verwerfen.",true); renderPending(); return true; }
  return false;
 }
 async function submit(command) { if(blocked(command.account_id))return; remember(command); await deliver(command); }
 async function deliver(command) {
  if(inFlight.has(command.account_id))return;
  inFlight.add(command.account_id); notify("Wird gespeichert …"); renderPending();
  let result;
  try { result=await call("POST",`/api/v1/accounts/${encodeURIComponent(command.account_id)}/commands`,command); }
  catch { result=null; }
  inFlight.delete(command.account_id);
  if(result===null){ notify("Ergebnis unbekannt: Die Verbindung brach ab. Der Befehl kann bereits gespeichert sein; denselben Befehl erneut senden.",true); renderPending(); return; }
  if(result.status===401){ csrf=null; renderSession(); notify("Anmeldung abgelaufen. Nach dem Anmelden denselben Befehl erneut senden.",true); renderPending(); return; }
  const outcome=settled(result);
  if(outcome===null){ notify(`Ergebnis unbekannt (HTTP ${result.status}, Antwort nicht eindeutig). Der Befehl kann bereits gespeichert sein; denselben Befehl erneut senden.`,true); renderPending(); return; }
  forget(command.account_id);
  editors.get(command.account_id)?.form.remove(); editors.delete(command.account_id);
  enrollments.get(command.account_id)?.remove(); enrollments.delete(command.account_id);
  if(command.create_account&&enrollments.get("")&&enrollments.get("").querySelector("input")?.value===command.account_id)enrollments.delete("");
  awaitingFresh.set(command.account_id, Number.isInteger(result.data.version)?result.data.version:0);
  renderPending(); notify(outcome.text,outcome.error);
  await reloadAll();
  if(awaitingFresh.has(command.account_id))notify(`${outcome.text} Die Ansicht ist noch nicht aktualisiert; vor weiteren Änderungen an ${command.account_id} „Aktualisieren“ wählen.`,true);
 }
 function settled(result) {
  const data=result.data, version=data.version;
  if(!result.valid)return null;
  const versioned=Number.isInteger(version)&&version>=0;
  if(result.status===200&&data.status==="applied"&&versioned)return {text:`Gespeichert (Stand ${version}).`,error:false};
  if(result.status===200&&data.status==="duplicate"&&versioned)return {text:`Bereits gespeichert (Stand ${version}). Nichts doppelt angewendet.`,error:false};
  if(result.status===409&&data.status==="conflict"&&versioned)return {text:`Zwischenzeitlich geändert (Stand ${version}). Nichts angewendet; Ansicht neu geladen.`,error:true};
  if(result.status===422&&data.status==="rejected"&&typeof data.message==="string")return {text:`Abgelehnt, nichts geändert: ${data.message}`,error:true};
  if(result.status===422&&typeof data.detail==="string"&&data.status===undefined)return {text:`Abgelehnt, nichts geändert: ${data.detail}`,error:true};
  return null;
 }
 function renderPending() {
  const box=byId("management-pending"); box.replaceChildren();
  for(const command of Object.values(pending())){
   const row=node("div","pending-command");
   row.append(node("p","",`Befehl ${command.command_id} für ${command.account_id}: Ergebnis unbekannt.`));
   const again=button(inFlight.has(command.account_id)?"Wird gesendet …":"Denselben Befehl erneut senden","",()=>deliver(command));
   again.disabled=!csrf||inFlight.has(command.account_id);
   const drop=button("Verwerfen","secondary",()=>{
    if(drop.dataset.confirm!=="1"){ drop.dataset.confirm="1"; drop.textContent="Wirklich verwerfen? Er kann bereits angewendet sein"; return; }
    forget(command.account_id); notify("Befehl verworfen. Sein Ergebnis bleibt unbekannt; vor einer neuen Änderung Ansicht prüfen."); renderPending(); reloadAll();
   });
   drop.disabled=inFlight.has(command.account_id);
   row.append(again,drop); box.append(row);
  }
 }
 function signedOut(message) { csrf=null; actor=""; editors.clear(); enrollments.clear(); notify(message); renderSession(); renderPending(); reloadAll(); }
 function renderSession() {
  const panel=byId("management-session"); panel.replaceChildren(); byId("management").hidden=false;
  if(!enabled){ panel.append(node("p","explanation","Browser-Verwaltung ist auf diesem Dienst nicht eingerichtet. Die Übersicht ist nur lesbar; Änderungen laufen über die lokale Verwaltung.")); return; }
  if(csrf){
   const out=button("Abmelden","secondary",async()=>{
    out.disabled=true; let result;
    try { result=await call("DELETE","/api/v1/session"); } catch { result=null; }
    out.disabled=false;
    if(result&&result.status===200&&result.valid&&result.data.status==="signed_out")signedOut("Abgemeldet.");
    else if(result&&result.status===401)signedOut("Die Sitzung war bereits beendet.");
    else notify("Abmeldung nicht bestätigt; die Sitzung kann noch gültig sein. Erneut abmelden.",true);
   });
   panel.append(node("p","",`Angemeldet als ${actor}. Änderungen werden mit diesem Bedienernamen protokolliert.`),out); return;
  }
  const form=node("form","login-form"); const label=node("label","","Verwaltungstoken");
  const input=node("input"); input.type="password"; input.autocomplete="current-password"; input.required=true; input.id="management-token"; label.append(input);
  const send=node("button","","Anmelden"); send.type="submit"; form.append(label,send);
  form.addEventListener("submit",async(event)=>{ event.preventDefault(); const token=input.value; input.value=""; send.disabled=true;
   let result; try { result=await call("POST","/api/v1/session",{token}); } catch { result=null; }
   send.disabled=false;
   if(result===null){ notify("Anmeldung nicht möglich: Dienst nicht erreichbar.",true); return; }
   if(result.status!==200){ notify(result.data.detail||`Anmeldung abgelehnt (HTTP ${result.status})`,true); return; }
   if(!result.valid||typeof result.data.csrf_token!=="string"||typeof result.data.actor!=="string"){ notify("Anmeldung unklar: Antwort unvollständig. Bitte Seite neu laden.",true); return; }
   csrf=result.data.csrf_token; actor=result.data.actor; notify("Angemeldet."); renderSession(); renderPending(); await reloadAll(); });
  panel.append(form);
 }
 function labelled(text, control) { const label=node("label","",text); label.append(control); return label; }
 function control(spec, account, manual) {
  if(spec.kind==="state"){ const select=node("select"); for(const [value,text] of Object.entries(stateLabels)){ const option=node("option","",text); option.value=value; option.selected=value===account.state; select.append(option);} return select; }
  const input=node("input"); input.type="text";
  if(spec.kind==="text"){ input.maxLength=spec.max; input.value=account[spec.field]||""; }
  else { input.inputMode="decimal"; input.placeholder=manual?manual.value:"z. B. 20.00"; input.value=manual?manual.value:""; }
  return input;
 }
 function decorate(actions, account, data) {
  if(!enabled){ const edit=node("a","manage-link","Lokal bearbeiten"); edit.href="cubellm-control://manage/"+encodeURIComponent(account.id); edit.title="Öffnet die lokale CubeLLM-Kontoverwaltung; speichert keine Änderung"; actions.append(edit); return; }
  if(!csrf){ actions.append(node("span","explanation","Zum Bearbeiten anmelden")); return; }
  const awaited=awaitingFresh.get(account.id);
  if(awaited!==undefined&&(data.manual_version||0)>=awaited)awaitingFresh.delete(account.id);
  if(awaitingFresh.has(account.id)){
   const waiting=button("Wird aktualisiert …","secondary",()=>{}); waiting.disabled=true; waiting.title="Nach der letzten Änderung wird auf die aktualisierte Ansicht gewartet";
   actions.append(waiting); return;
  }
  const draft=editors.get(account.id);
  actions.append(button("Bearbeiten","secondary",()=>{
   if(awaitingFresh.has(account.id)){ notify("Die Ansicht dieses Kontos wird nach der letzten Änderung noch aktualisiert.",true); return; }
   const open=editors.get(account.id);
   if(open){ open.form.hidden=!open.form.hidden; return; }
   const form=editForm(account,data); editors.set(account.id,{form,version:data.manual_version||0}); actions.append(form);
  }));
  if(!draft)return;
  if(draft.version!==(data.manual_version||0)){
   const note=draft.form.querySelector(".draft-note")||node("p","explanation error draft-note");
   note.textContent=`Entwurf bezieht sich auf Stand ${draft.version}; inzwischen Stand ${data.manual_version||0}. Speichern meldet einen Konflikt statt fremde Änderungen zu überschreiben.`;
   draft.form.prepend(note);
  }
  actions.append(draft.form,button("Entwurf verwerfen","secondary",()=>{ editors.delete(account.id); draft.form.remove(); if(typeof renderAccounts==="function")renderAccounts(); }));
 }
 function editForm(account, data) {
  const form=node("form","edit-form");
  const month=new Date().toISOString().slice(0,7);
  const overrideFor=(spec,period)=>(data.manual_fields||[]).find((item)=>item.field===spec.field&&(!spec.period||item.period===period));
  const rows=fields.map((spec)=>{
   const input=control(spec,account,overrideFor(spec,month));
   const action=node("select"); action.setAttribute("aria-label",`${spec.label}: Aktion`);
   const marker=node("span","manual-marker");
   const label=node("label","",spec.label); label.append(marker,input);
   const row=node("div","edit-row"); row.append(label,action);
   let period=null;
   const sync=()=>{
    const manual=overrideFor(spec,period?period.value:month);
    marker.textContent=manual?" · manuell":"";
    if(spec.kind==="decimal"){ input.value=manual?manual.value:""; input.placeholder=manual?manual.value:"z. B. 20.00"; }
    action.replaceChildren();
    for(const [value,text] of [["keep","Unverändert"],["set",spec.clearable?"Setzen (leer löscht)":"Setzen"],...(manual?[["automatic","Automatik wiederherstellen"]]:[])]){ const option=node("option","",text); option.value=value; action.append(option); }
   };
   if(spec.period){ period=node("input"); period.type="month"; period.value=month; period.required=true; period.addEventListener("change",sync); row.append(labelled("Monat",period)); }
   sync();
   return {spec,input,action,period,row};
  });
  const save=node("button","","Speichern"); save.type="submit";
  form.append(...rows.map((row)=>row.row),save);
  form.addEventListener("submit",async(event)=>{ event.preventDefault();
   const changes=[];
   for(const row of rows){
    if(row.action.value==="keep")continue;
    const base={field:row.spec.field,...(row.period?{period:row.period.value}:{})};
    if(row.action.value==="automatic"){ changes.push({...base,action:"automatic"}); continue; }
    const value=row.input.value.trim();
    if(row.spec.kind==="decimal"&&!DECIMAL.test(value)){ notify(`${row.spec.label}: Bitte eine Dezimalzahl mit Punkt angeben.`,true); return; }
    if(!value&&!row.spec.clearable&&row.spec.kind!=="state"){ notify(`${row.spec.label} darf nicht leer sein.`,true); return; }
    changes.push({...base,action:"set",value});
   }
   if(!changes.length){ notify("Keine Änderung gewählt."); return; }
   save.disabled=true;
   await submit({schema_version:1,command_id:"browser-"+crypto.randomUUID(),account_id:account.id,expected_version:data.manual_version||0,changes});
   save.disabled=false; });
  return form;
 }
 function enrollment(accountId) {
  const form=node("form","enroll-form");
  const id=node("input"); id.type="text"; id.maxLength=128; id.value=accountId; id.required=true; id.readOnly=Boolean(accountId);
  const provider=node("input"); provider.type="text"; provider.maxLength=64; provider.required=true; provider.placeholder="z. B. openai oder eigener-anbieter";
  const kind=node("select"); kind.required=true; for(const [value,text] of [["","Bitte wählen"],["api","API-Zugang"],["subscription","Subscription"]]){ const option=node("option","",text); option.value=value; kind.append(option); }
  const name=node("input"); name.type="text"; name.maxLength=100; name.required=true;
  const confirm=node("input"); confirm.type="checkbox"; confirm.required=true;
  const confirmLabel=node("label","confirm","Ich bestätige Anbieter, Kontoart und Zuordnung. Nichts davon wird automatisch abgeleitet."); confirmLabel.prepend(confirm);
  const send=node("button","","Konto aufnehmen"); send.type="submit";
  form.append(labelled("Konto-ID",id),labelled("Anbieter-ID",provider),labelled("Kontoart",kind),labelled("Anzeigename",name),confirmLabel,send);
  form.addEventListener("submit",async(event)=>{ event.preventDefault();
   const values={id:id.value.trim(),provider:provider.value.trim(),kind:kind.value,name:name.value.trim()};
   if(!IDENTIFIER.test(values.id)||!PROVIDER.test(values.provider)||!values.kind||!values.name||!confirm.checked){ notify("Bitte Konto-ID, gültige Anbieter-ID, Kontoart und Namen angeben und bestätigen.",true); return; }
   send.disabled=true;
   await submit({schema_version:1,command_id:"browser-"+crypto.randomUUID(),account_id:values.id,expected_version:0,create_account:values});
   send.disabled=false; });
  return form;
 }
 function binding(item, label) {
  const retained=item.presence==="retained";
  return node("li",retained?"retained":"",`${label} · ${item.gateway_origin} · ${retained?"aus früherem Scan, zuletzt gelesen "+since(item.observed_at):"im letzten Scan gelesen ("+since(item.observed_at)+")"}`);
 }
 function finding(item) {
  const parts=[findingLabels[item.kind]||item.kind,item.gateway_origin,item.source&&item.origin?`${item.source}/${item.origin}`:item.origin,item.name,item.account_id?"Konto "+item.account_id:null,item.detail].filter(Boolean);
  return node("li","",parts.join(" · "));
 }
 const credentialFindingLabels = {unassigned_candidate:"Dienst und Konto unbekannt",provider_hint_without_account:"Anbieterhinweis ohne Katalogkonto",archived_candidate:"Nur in Archiv/Export",unbound_candidate:"Keine bestätigte Kontobindung",quarantined_record:"Unstimmiger Eintrag zurückgehalten",incomplete_scan:"Scan unvollständig"};
 const kindLabels = {api_key_or_token:"API-Schlüssel oder Token",connection_string:"Verbindungszeichenfolge",encrypted_material:"Verschlüsseltes Material",local_application_secret:"Lokales Anwendungsgeheimnis",public_firebase_client_configuration:"Öffentliche Firebase-Clientkonfiguration"};
 const importLabels = {imported:"übernommen",older_or_equal_ignored:"älterer oder gleicher Scan ignoriert",rejected:"abgelehnt",unreadable:"Dateien nicht lesbar"};
 function credentialSection(report) {
  const sources=report.credential_sources||[], candidates=report.credential_candidates||[], findings=report.credential_findings||[];
  if(!sources.length)return [];
  const sourceList=node("div","inventory-gateways");
  for(const source of sources){
   const card=node("article","inventory-gateway credential-source");
   card.append(node("h3","",source.source_id),node("p","",`Letzter Import ${since(source.last_attempt_at)}: ${importLabels[source.last_attempt_status]||source.last_attempt_status}${source.last_attempt_message?" · "+source.last_attempt_message:""}`));
   if(source.scanned_at)card.append(node("p",source.scan_freshness==="stale"?"explanation error":"explanation",`Scan ${since(source.scanned_at)}${source.scan_freshness==="stale"?" (veraltet)":""} · ${source.scan_complete?"Umfang vollständig gelesen":"Umfang unvollständig"} · Gültigkeit ungeprüft · ${source.candidates} Kandidaten, davon ${source.retained_candidates} nur aus früherem Scan`));
   const hosts=node("ul","sources");
   for(const host of source.hosts)hosts.append(node("li",host.complete?"":"warning",`${host.host}: ${host.files_read} Dateien, ${host.roots} Wurzeln (${host.scope_id})${host.presence==="retained"?" · aus früherem Scan":""}${host.complete?"":" · unvollständig"}`));
   card.append(hosts); sourceList.append(card);
  }
  const list=node("div","inventory-accounts");
  for(const item of candidates){
   const card=node("article","inventory-account credential-candidate");
   card.append(node("h3","",`${item.service} · ${item.reference}`));
   card.append(node("p","explanation",`Ungebunden · ${item.provider_hint?"Anbieterhinweis "+item.provider_hint+" (kein Kontonachweis)":"kein Anbieterhinweis"}`));
   card.append(node("p",item.presence==="retained"||item.state==="archived_only"?"explanation error":"explanation",`${item.state==="current"?"Aktuelle Fundstelle":"Nur Archiv/Export"} · ${item.presence==="retained"?"aus früherem Scan, zuletzt gesehen "+since(item.last_observed_at):"im letzten Scan gesehen"}${item.scan_freshness==="stale"?" · Scan veraltet":""}`));
   card.append(node("p","explanation",`Hosts: ${item.hosts.join(", ")} · Projekte: ${item.projects.join(", ")} · Variablen: ${item.variables.join(", ")} · Art: ${item.kinds.map((kind)=>kindLabels[kind]||kind).join(", ")}`));
   list.append(card);
  }
  const notes=node("ul","credential-findings");
  for(const item of findings)notes.append(node("li","",[credentialFindingLabels[item.kind]||item.kind,item.service,item.reference,item.host,item.detail].filter(Boolean).join(" · ")));
  return [node("h3","",`Gefundene Zugangsdaten (${candidates.length}, alle ungebunden)`),node("p","explanation","Nur Metadaten aus der Dienstinventur: keine Schlüsselwerte, keine Dateipfade, keine Prüfung beim Anbieter. Ein Fund belegt weder Gültigkeit noch Kontozugehörigkeit; ein Dienstname ist nur ein Anbieterhinweis."),sourceList,list,node("h3","",`Hinweise zu Zugangsdaten (${findings.length})`),notes];
 }
 async function loadInventory() {
  const box=byId("inventory"); const ticket=++inventoryTicket; let report;
  try { const response=await fetch("/api/v1/inventory",{cache:"no-store"}); if(!response.ok)throw new Error("unavailable"); report=await response.json(); }
  catch {
   if(ticket!==inventoryTicket)return;
   const warning=node("p","explanation error inventory-stale",inventoryLoadedAt?`Inventar konnte nicht aktualisiert werden. Angezeigt: Stand ${since(inventoryLoadedAt)}.`:"Inventar nicht erreichbar.");
   const retry=button("Erneut laden","secondary",loadInventory);
   const old=box.querySelector(".inventory-stale-box"); if(old)old.remove();
   const holder=node("div","inventory-stale-box"); holder.append(warning,retry);
   if(inventoryLoadedAt)box.prepend(holder); else box.replaceChildren(holder);
   return;
  }
  if(ticket!==inventoryTicket)return;
  inventoryLoadedAt=new Date().toISOString();
  const coverage={not_collected:"Gateway: noch kein Inventar erfasst",partial:"Gateway: teilweise erfasst",configured_sources_complete:"Gateway: alle sechs Gatewayquellen vollständig gelesen (keine Aussage über alle Konten)"}[report.coverage];
  const gateways=node("div","inventory-gateways");
  for(const gateway of report.gateways){
   const card=node("article","inventory-gateway");
   card.append(node("h3","",gateway.gateway_origin),node("p","",`${gatewayStates[gateway.state]} · letzter Abruf ${since(gateway.last_attempt_at)} · ${gateway.last_attempt_message}`));
   const list=node("ul","sources");
   for(const source of gateway.sources)list.append(node("li",source.status==="complete"?"":"warning",`${source.name} (${source.origin}): ${sourceStates[source.status]}${source.last_readable_at?" · zuletzt lesbar "+since(source.last_readable_at):" · nie gelesen"}`));
   card.append(list); gateways.append(card);
  }
  const filterToggle=node("label","confirm"); const check=node("input"); check.type="checkbox"; check.checked=unknownOnly;
  check.addEventListener("change",()=>{ unknownOnly=check.checked; loadInventory(); }); filterToggle.append(check," Nur nicht aufgenommene Konten");
  const accounts=node("div","inventory-accounts");
  for(const entry of report.accounts.filter((item)=>!unknownOnly||item.gaps.includes("not_in_catalogue"))){
   const card=node("article","inventory-account");
   card.append(node("h3","",entry.account_id+(entry.provider?" · "+entry.provider:"")),labelled_codes("Quellen",entry.references,referenceLabels));
   if(entry.gaps.length)card.append(labelled_codes("Offen",entry.gaps,gapLabels));
   const list=node("ul","bindings");
   for(const item of entry.credentials)list.append(binding(item,`Zugang ${item.credential.credential_name} (${item.credential.origin}, ${item.credential.binding})`));
   for(const item of entry.routes)list.append(binding(item,`Route ${item.route.route_id||item.route.path||"ohne Kennung"} (${item.route.origin}, ${item.route.binding})`));
   if(list.childElementCount)card.append(list);
   if(csrf&&entry.gaps.includes("not_in_catalogue")){
    const kept=enrollments.get(entry.account_id);
    if(kept)card.append(kept);
    else { const open=button("Konto aufnehmen …","secondary",()=>{ const form=enrollment(entry.account_id); enrollments.set(entry.account_id,form); open.replaceWith(form); }); card.append(open); }
   }
   accounts.append(card);
  }
  if(!accounts.childElementCount)accounts.append(node("p","explanation","Keine passenden Konten."));
  const findings=node("ul","findings"); for(const item of report.findings)findings.append(finding(item));
  if(!findings.childElementCount)findings.append(node("li","","Keine Hinweise."));
  box.replaceChildren(node("p","",`${coverage} · erzeugt ${since(report.generated_at)}`),gateways,node("h3","","Konten und Bindungen"),filterToggle,accounts,node("h3","",`Hinweise (${report.findings.length})`),findings,...credentialSection(report));
  if(csrf){
   if(!enrollments.has("")){ const own=node("details","new-account"); own.append(node("summary","","Weiteres Konto aufnehmen"),enrollment("")); enrollments.set("",own); }
   box.append(enrollments.get(""));
  }
 }
 async function start() {
  try {
   const caps=await (await fetch("/api/v1/capabilities",{cache:"no-store"})).json(); enabled=caps.browser_management==="session_v1";
   if(enabled){ const session=await call("GET","/api/v1/session"); if(session.status===200){ csrf=session.data.csrf_token; actor=session.data.actor; } }
  } catch { renderSession(); notify("Verwaltungsstatus nicht abrufbar.",true); byId("management-pending").replaceChildren(button("Erneut versuchen","secondary",start)); return; }
  renderSession(); renderPending(); await reloadAll();
 }
 return {decorate,start,loadInventory};
})();
document.addEventListener("DOMContentLoaded",()=>management.start());
