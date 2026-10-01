"""The single audit page (plain HTML/JS, no build step).

All rendered data comes from the real API: POST /api/audits renders the fresh
verdict, GET /api/audits/<id> re-opens a frozen one.
"""

from __future__ import annotations

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>星载启动参数槽 · 扇区恢复审查</title>
<style>
  :root {
    --bg:#0b1020; --panel:#131a2e; --panel2:#1a2340; --ink:#e8ecf8;
    --muted:#93a0c4; --line:#283356; --accent:#5b8cff; --good:#2fbf71;
    --bad:#ff5d6c; --warn:#f5a623; --bad-bg:#2a1620; --good-bg:#12251c;
  }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.6 "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
         background:var(--bg); color:var(--ink); }
  header { padding:18px 28px; border-bottom:1px solid var(--line);
           background:linear-gradient(180deg,#101732,#0b1020); }
  header h1 { margin:0; font-size:18px; letter-spacing:1px; }
  header p { margin:4px 0 0; color:var(--muted); font-size:12px; }
  main { max-width:1180px; margin:0 auto; padding:24px 28px 60px; }
  .grid { display:grid; grid-template-columns:380px 1fr; gap:20px; }
  @media (max-width:960px){ .grid{ grid-template-columns:1fr; } }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
          padding:18px 20px; }
  .card h2 { margin:0 0 12px; font-size:14px; color:#c4d0f5; letter-spacing:.5px; }
  label { display:block; font-size:12px; color:var(--muted); margin:10px 0 4px; }
  input[type=text], textarea { width:100%; background:#0d1326; color:var(--ink);
    border:1px solid var(--line); border-radius:6px; padding:8px 10px;
    font:12px/1.5 ui-monospace, Menlo, Consolas, monospace; }
  textarea { min-height:200px; resize:vertical; word-break:break-all; }
  button { background:var(--accent); color:#fff; border:0; border-radius:6px;
    padding:9px 18px; font-size:13px; cursor:pointer; margin-right:8px; }
  button.ghost { background:transparent; border:1px solid var(--line); color:var(--muted); }
  button:disabled { opacity:.5; cursor:wait; }
  .hint { font-size:11px; color:var(--muted); margin-top:6px; }
  .verdict { border-width:2px; }
  .verdict.has-violation { border-color:var(--bad); }
  .verdict.clean { border-color:var(--good); }
  .big { font-size:22px; font-weight:700; margin:6px 0 2px; }
  .big.good { color:var(--good); } .big.bad { color:var(--bad); }
  .big.warn { color:var(--warn); }
  .sub { color:var(--muted); font-size:12px; }
  .frozen-badge { display:inline-block; margin-left:10px; font-size:11px;
    background:#243056; color:#bcd0ff; border:1px solid #3a4d86; padding:1px 8px;
    border-radius:10px; vertical-align:middle; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line);
           vertical-align:top; }
  th { color:var(--muted); font-weight:600; font-size:11px; text-transform:uppercase; }
  .tag { display:inline-block; min-width:52px; text-align:center; padding:1px 7px;
         border-radius:4px; font-size:11px; }
  .tag.adopt { background:var(--good-bg); color:var(--good); border:1px solid #23503c; }
  .tag.drop  { background:var(--bad-bg); color:var(--bad); border:1px solid #5c2a35; }
  .tag.tx    { background:#1d2748; color:#9db4ee; border:1px solid #334577; }
  .viol { background:var(--bad-bg); border:1px solid #5c2a35; color:#ffb3bb;
          padding:10px 14px; border-radius:8px; margin-bottom:14px; }
  .viol b { color:var(--bad); }
  .boot { background:var(--panel2); border:1px solid var(--line); border-radius:8px;
          padding:12px 14px; margin-top:12px; }
  .boot .name { font-size:16px; font-weight:700; }
  code { color:#bcd0ff; }
  .err-inline { color:var(--bad); font-size:12px; margin-top:8px; min-height:16px; }
  .slots { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
           gap:10px; margin-top:10px; }
  .slot { background:var(--panel2); border:1px solid var(--line); border-radius:8px;
          padding:10px 12px; }
  .slot.boot-pick { border-color:var(--good); box-shadow:0 0 0 1px var(--good); }
  .slot h3 { margin:0 0 4px; font-size:13px; }
  .slot .digest { font-size:11px; color:var(--muted); font-family:ui-monospace,monospace; }
  .muted{color:var(--muted);}
</style>
</head>
<body>
<header>
  <h1>星载控制器 · 启动参数槽扇区镜像恢复审查</h1>
  <p>逐字节解析固定字段与 CRC32；仅当 准备 → 目标槽完整页 → 完成 三段齐备、
     事务标识与载荷摘要相符且物理写入顺序正确时才切换。损坏记录不会被忽略或跳过。</p>
</header>
<main>
<div class="grid">
  <section class="card">
    <h2>提交扇区镜像</h2>
    <label for="audit">稳定审计标识</label>
    <input id="audit" type="text" placeholder="例如 OBC-AUDIT-20260929-01"
           value="OBC-AUDIT-0001">
    <label for="active">初始活动槽（物理槽名）</label>
    <input id="active" type="text" placeholder="例如 SLOT_A" value="SLOT_A">
    <label for="sectors">至多 32 个 Base64 定长扇区（每行一个，按物理写入顺序）</label>
    <textarea id="sectors" placeholder="U0NUU..."></textarea>
    <div class="hint">每条扇区原始 64 字节，Base64 后恰为 88 字符。提交后结论按审计标识冻结。</div>
    <div style="margin-top:14px;">
      <button id="submit">提交恢复裁决</button>
      <button id="load" class="ghost">按标识查看冻结结论</button>
    </div>
    <div id="formerr" class="err-inline"></div>
  </section>

  <section class="card verdict" id="verdict-card">
    <h2>恢复结论 <span id="frozen-badge" class="frozen-badge" style="display:none">已冻结</span></h2>
    <div id="empty" class="muted">尚未加载任何结论。左侧提交镜像，或输入已冻结标识后点击「按标识查看冻结结论」。</div>
    <div id="result" style="display:none">
      <div id="violation"></div>
      <div class="big" id="headline"></div>
      <div class="sub" id="headline-sub"></div>
      <div class="boot" id="boot"></div>
      <h2 style="margin-top:18px">各物理槽最终状态（无效写入未覆盖旧有效代次）</h2>
      <div class="slots" id="slots"></div>
      <h2 style="margin-top:20px">逐条记录裁决依据（按物理写入顺序）</h2>
      <table>
        <thead><tr><th>#</th><th>seq</th><th>类型</th><th>采纳/舍弃</th><th>事务</th><th>裁决依据</th></tr></thead>
        <tbody id="decisions"></tbody>
      </table>
    </div>
  </section>
</div>

<section class="card" style="margin-top:20px;">
  <h2>提交重放纠正（不改动任何扇区字节，仅重排写入顺序）</h2>
  <div class="hint" style="margin:0 0 10px;">
    适用于已冻结来源审计：镜像内每个扇区均通过既有字节级校验，但物理写入顺序使
    <b>目标事务</b>未被采纳。服务在全部扇区排列中按既有恢复语义逐前缀裁决，给出
    <b>相邻换位次数最少</b>、再按最终原始下标序列稳定裁决的可重放编排；任何前缀都
    不会把未完成的目标页当作可启动。至多 12 个扇区。
  </div>
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px 14px;">
    <div>
      <label for="corr-id">稳定纠正标识</label>
      <input id="corr-id" type="text" placeholder="例如 REPLAY-20261001-01" value="REPLAY-0001">
    </div>
    <div>
      <label for="corr-src">冻结来源审计标识</label>
      <input id="corr-src" type="text" placeholder="例如 OBC-AUDIT-0001">
    </div>
    <div>
      <label for="corr-active">初始活动槽</label>
      <input id="corr-active" type="text" value="SLOT_A">
    </div>
    <div>
      <label for="corr-tx">目标事务标识（整数）</label>
      <input id="corr-tx" type="text" inputmode="numeric" placeholder="例如 200">
    </div>
  </div>
  <label for="corr-sectors">来源镜像扇区（每行一个 88 字符 Base64，至多 12 个；顺序与冻结来源一致）</label>
  <textarea id="corr-sectors" style="min-height:120px;" placeholder="U0NUU..."></textarea>
  <div style="margin-top:12px;">
    <button id="corr-submit">提交重放纠正</button>
    <button id="corr-load" class="ghost">按纠正标识重开冻结结果</button>
    <button id="corr-copy" class="ghost">复用上方来源镜像</button>
  </div>
  <div id="corr-err" class="err-inline"></div>

  <div id="corr-empty" class="muted" style="margin-top:12px;">尚未计算纠正编排。</div>
  <div id="corr-result" style="display:none;margin-top:14px;">
    <div class="big good" id="corr-headline"></div>
    <div class="sub" id="corr-sub"></div>
    <div class="sub" id="corr-orig" style="margin-top:6px;"></div>

    <div class="boot" style="margin-top:12px;">
      <div><b>规范物理下标序列</b>（最终顺序，数字为来源镜像中的冻结物理下标）：</div>
      <div id="corr-seq" style="font-size:18px;letter-spacing:2px;margin:6px 0;"></div>
      <div class="sub">相邻换位次数：<b id="corr-swaps"></b> ·
        来源扇区数 <span id="corr-n"></span> · 来源 SHA-256
        <code id="corr-hash"></code></div>
      <details style="margin-top:6px;">
        <summary class="muted" style="cursor:pointer;">具体相邻换位步骤（位置对，0 起，按执行顺序）</summary>
        <code id="corr-steps" style="word-break:break-all;"></code>
      </details>
    </div>

    <div class="boot" style="margin-top:12px;">
      <b>目标事务三段证据</b>
      <table style="margin-top:6px;">
        <thead><tr><th>环节</th><th>来源物理下标</th><th>内容</th></tr></thead>
        <tbody id="corr-evidence"></tbody>
      </table>
    </div>

    <h2 style="margin-top:16px;">各前缀启动结论（逐前缀按既有恢复语义裁决）</h2>
    <table>
      <thead><tr><th>前缀长度</th><th>该前缀物理下标序列</th><th>启动槽</th><th>代次</th>
        <th>目标完成已写入</th><th>目标页可启动</th><th>首个违约</th><th>裁决依据</th></tr></thead>
      <tbody id="corr-prefixes"></tbody>
    </table>
  </div>
</section>
</main>
<script>
const MAX = 32;
const TYPE_LABEL = {slot_page:"槽页", prepare:"准备", complete:"完成",
                    corrupt:"损坏", unreadable:"不可读"};
const VIOL_LABEL = {
  bad_base64:"非法 Base64", bad_length:"长度/数量错误", bad_magic:"魔数错误",
  bad_version:"版本错误", bad_type:"非法类型", bad_reserved:"保留字节异常",
  bad_header_crc:"头校验错误", bad_sector_crc:"整扇区 CRC32 错误",
  bad_payload_crc:"载荷 CRC32 错误", bad_slot_name:"槽名非法",
  bad_padding:"填充区异常", duplicate_tx_inconsistent:"重复事务内容不一致",
  complete_without_prepare:"完成记录无有效前置（悬空/缺页）",
  prepare_after_complete:"已完成事务的迟到准备",
  page_before_prepare:"槽页缺少匹配准备",
  page_tx_mismatch:"槽页摘要与准备不符",
  complete_tx_mismatch:"完成与准备字段不符"
};

function esc(s){return String(s).replace(/[&<>"']/g, c=>(
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

function setHeadline(kind, text, sub){
  const h = document.getElementById('headline');
  h.className = 'big ' + kind;
  h.textContent = text;
  document.getElementById('headline-sub').textContent = sub || '';
}

function render(data){
  document.getElementById('empty').style.display = 'none';
  document.getElementById('result').style.display = 'block';
  const card = document.getElementById('verdict-card');
  card.className = 'card verdict ' + (data.first_violation ? 'has-violation' : 'clean');
  document.getElementById('frozen-badge').style.display = data.frozen ? 'inline-block' : 'none';

  const v = data.first_violation;
  const vbox = document.getElementById('violation');
  if(v){
    vbox.innerHTML = `<div class="viol"><b>首个违约定位</b>：物理扇区 #${v.index}
      （${v.sector_type ? esc(TYPE_LABEL[v.sector_type] || v.sector_type) : '输入'}，
      seq=${v.seq ?? '-'}）<br>代码 <code>${esc(v.code)}</code> ·
      ${esc(v.message)}</div>`;
    const dropped = data.decisions.filter(d=>!d.adopted && d.violation).length;
    setHeadline('bad', '存在违约 —— 重启不得采用写到一半的较新槽',
      `共 ${data.total_sectors} 条输入；${dropped} 条记录因首个违约链被舍弃。`);
  } else {
    vbox.innerHTML = '';
    setHeadline('good', '未发现违约',
      `共 ${data.total_sectors} 条记录，均按物理顺序通过固定字段与 CRC32 校验。`);
  }

  // boot pick
  const b = data.boot;
  const boot = document.getElementById('boot');
  if(b){
    boot.innerHTML = `<span class="name">实际将启动：槽 ${esc(b.slot)} · 代次 ${b.generation}</span>
      <div class="sub">${esc(b.reason)}</div>
      <div class="sub">载荷摘要 CRC32：<code>${esc(b.digest)}</code>
        · 初始活动槽 ${esc(data.active_slot)} 不参与越代裁决</div>
      <details><summary class="muted" style="cursor:pointer">载荷 HEX（32B）</summary>
        <code style="word-break:break-all">${esc(b.payload_hex)}</code></details>`;
  } else {
    boot.innerHTML = `<span class="name">无任何有效完整事务</span>
      <div class="sub">${esc(data.boot_reason)}</div>`;
  }

  // slots
  const slots = document.getElementById('slots');
  slots.innerHTML = '';
  Object.values(data.slots).forEach(s=>{
    const pick = b && s.slot===b.slot;
    const el = document.createElement('div');
    el.className = 'slot' + (pick ? ' boot-pick' : '');
    el.innerHTML = `<h3>${esc(s.slot)} ${pick?'<span class="tag adopt">启动</span>':''}</h3>
      <div>代次：<b>${s.generation}</b></div>
      <div class="digest">digest ${esc(s.digest)}<br>tx ${s.transaction_id}
        · 页 #${s.page_index} / 完成 #${s.complete_index}</div>
      <details><summary class="muted" style="cursor:pointer">载荷</summary>
        <code style="word-break:break-all">${esc(s.payload_hex)}</code></details>`;
    slots.appendChild(el);
  });

  // decisions
  const tb = document.getElementById('decisions');
  tb.innerHTML = '';
  data.decisions.forEach(d=>{
    const tr = document.createElement('tr');
    const txCell = d.transaction_id==null ? '-' : String(d.transaction_id);
    tr.innerHTML = `<td>#${d.index}</td><td>${d.seq<0?'-':d.seq}</td>
      <td>${esc(TYPE_LABEL[d.kind]||d.kind)}</td>
      <td><span class="tag ${d.adopted?'adopt':'drop'}">${d.adopted?'采纳':'舍弃'}</span></td>
      <td><span class="tag tx">tx ${esc(txCell)}</span></td>
      <td>${esc(d.basis)}</td>`;
    tb.appendChild(tr);
  });
}

function sectorsFromTextarea(){
  const lines = document.getElementById('sectors').value.split('\n')
    .map(s=>s.trim()).filter(Boolean);
  if(!lines.length){ document.getElementById('formerr').textContent='请粘贴至少一个扇区'; return null; }
  if(lines.length > MAX){ document.getElementById('formerr').textContent=`至多 ${MAX} 个扇区`; return null; }
  for(const [i,l] of lines.entries()){
    if(l.length!==88){ document.getElementById('formerr').textContent=
      `第 ${i+1} 行不是 88 字符定长 Base64（实际 ${l.length}）`; return null; }
  }
  document.getElementById('formerr').textContent='';
  return lines;
}

async function submit(){
  const sectors = sectorsFromTextarea();
  if(!sectors) return;
  const audit = document.getElementById('audit').value.trim();
  const active = document.getElementById('active').value.trim();
  const btn = document.getElementById('submit');
  btn.disabled = true;
  try{
    const resp = await fetch('/api/audits', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({audit_id:audit, active_slot:active, sectors})});
    const data = await resp.json();
    if(resp.status===409){
      render(data.frozen);
      document.getElementById('formerr').textContent = data.message;
    } else if(!resp.ok){
      document.getElementById('formerr').textContent = data.message || '提交失败';
    } else {
      render(data);
    }
  } catch(e){
    document.getElementById('formerr').textContent = '网络错误：'+e;
  } finally {
    btn.disabled = false;
  }
}

async function loadFrozen(){
  const audit = document.getElementById('audit').value.trim();
  if(!audit){ document.getElementById('formerr').textContent='请输入审计标识'; return; }
  const btn = document.getElementById('load');
  btn.disabled = true;
  try{
    const resp = await fetch('/api/audits/'+encodeURIComponent(audit));
    const data = await resp.json();
    if(!resp.ok){ document.getElementById('formerr').textContent=data.message; }
    else { document.getElementById('formerr').textContent=''; render(data); }
  } catch(e){
    document.getElementById('formerr').textContent = '网络错误：'+e;
  } finally { btn.disabled = false; }
}

document.getElementById('submit').addEventListener('click', submit);
document.getElementById('load').addEventListener('click', loadFrozen);

// ---- replay correction --------------------------------------------------
const CORR_MAX = 12;
function corrReadSectors(){
  const lines = document.getElementById('corr-sectors').value.split('\n')
    .map(s=>s.trim()).filter(Boolean);
  const err = document.getElementById('corr-err');
  if(!lines.length){ err.textContent='请粘贴至少一个来源扇区'; return null; }
  if(lines.length > CORR_MAX){ err.textContent=`至多 ${CORR_MAX} 个扇区（实际 ${lines.length}）`; return null; }
  for(const [i,l] of lines.entries()){
    if(l.length!==88){ err.textContent=`第 ${i+1} 行不是 88 字符定长 Base64（实际 ${l.length}）`; return null; }
  }
  err.textContent='';
  return lines;
}

function renderCorrection(d){
  document.getElementById('corr-empty').style.display='none';
  document.getElementById('corr-result').style.display='block';
  document.getElementById('corr-headline').textContent =
    `可重放最小编排：${d.swap_count} 次相邻换位即让事务 ${d.target_transaction} 生效`;
  document.getElementById('corr-sub').textContent =
    `来源审计 ${d.source_audit_id} · 目标启动槽 ${d.target.slot} 代次 ${d.target.generation} · 初始活动槽 ${d.active_slot} 不参与越代裁决`;
  const sv = d.source_verdict || {};
  const origBoot = sv.boot_slot==null
    ? '无可启动新代次' : `槽 ${sv.boot_slot} 代次 ${sv.boot_generation}`;
  const violTxt = sv.first_violation
    ? `，首个违约 ${esc(sv.first_violation.code)}（扇区 #${sv.first_violation.index}）` : '';
  document.getElementById('corr-orig').innerHTML =
    `来源镜像按<b>原始物理顺序</b>裁决：${origBoot}${violTxt}；`
    + `目标事务原本${sv.target_adopted_originally ? '已被采纳' : `<b>未被采纳</b>`}。`
    + ` 来源扇区（${d.total_sectors} 个）与该结论已一并冻结。`;
  document.getElementById('corr-seq').textContent =
    d.physical_sequence.map(i=>String(i).padStart(2,'0')).join('  →  ');
  document.getElementById('corr-swaps').textContent = d.swap_count;
  document.getElementById('corr-n').textContent = d.total_sectors;
  document.getElementById('corr-hash').textContent = d.source_hash.slice(0,16) + '…';
  document.getElementById('corr-steps').textContent =
    d.swap_steps.length ? d.swap_steps.map(s=>`(${s[0]}↔${s[1]})`).join(' ') : '（无需换位，来源顺序已正确）';

  const t = d.target;
  const rows = [
    ['准备 prepare', t.prepare_index, `事务 ${t.transaction_id} · 槽 ${t.slot} · 代次 ${t.generation}`],
    ['目标槽完整页 slot page', t.page_index, `载荷摘要 CRC32 ${t.digest}`],
    ['完成 complete', t.complete_index, `三段齐备且顺序正确后切换槽 ${t.slot}`],
  ];
  document.getElementById('corr-evidence').innerHTML = rows.map(r=>
    `<tr><td><b>${r[0]}</b></td><td>物理下标 #${r[1]}</td><td>${esc(r[2])}</td></tr>`).join('')
    + `<tr><td>载荷 HEX（32B）</td><td colspan="2"><code style="word-break:break-all">${esc(t.payload_hex)}</code></td></tr>`;

  const tb = document.getElementById('corr-prefixes');
  tb.innerHTML = '';
  d.prefixes.forEach(p=>{
    const tr = document.createElement('tr');
    const boot = p.boot_slot==null
      ? '<span class="muted">无可启动新代次</span>'
      : `槽 <b>${esc(p.boot_slot)}</b>`;
    const bootableTag = p.target_bootable
      ? (p.target_complete_included
          ? '<span class="tag adopt">是（完成已写入）</span>'
          : '<span class="tag drop">是（未完成！）</span>')
      : '<span class="tag tx">否</span>';
    tr.innerHTML = `<td>${p.length}</td>
      <td><code>[${p.physical_sequence.join(', ')}]</code></td>
      <td>${boot}</td><td>${p.boot_generation==null?'-':p.boot_generation}</td>
      <td>${p.target_complete_included?'是':'否'}</td>
      <td>${bootableTag}</td>
      <td>${p.first_violation?esc(p.first_violation):'—'}</td>
      <td>${esc(p.reason)}</td>`;
    tb.appendChild(tr);
  });
}

async function submitCorrection(){
  const sectors = corrReadSectors();
  if(!sectors) return;
  const txRaw = document.getElementById('corr-tx').value.trim();
  const tx = Number(txRaw);
  const err = document.getElementById('corr-err');
  if(!/^\d+$/.test(txRaw) || !Number.isInteger(tx)){ err.textContent='目标事务标识须为非负整数'; return; }
  const btn = document.getElementById('corr-submit');
  btn.disabled = true;
  try{
    const resp = await fetch('/api/corrections', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        correction_id: document.getElementById('corr-id').value.trim(),
        source_audit_id: document.getElementById('corr-src').value.trim(),
        active_slot: document.getElementById('corr-active').value.trim(),
        target_transaction: tx, sectors})});
    const data = await resp.json();
    if(!resp.ok){ err.textContent = data.message || '提交失败'; return; }
    err.textContent = resp.status===200 ? '该纠正已冻结，返回既有冻结编排。' : '';
    renderCorrection(data);
  }catch(e){ err.textContent='网络错误：'+e; }
  finally{ btn.disabled=false; }
}

async function loadCorrection(){
  const id = document.getElementById('corr-id').value.trim();
  const err = document.getElementById('corr-err');
  if(!id){ err.textContent='请输入纠正标识'; return; }
  const btn = document.getElementById('corr-load');
  btn.disabled = true;
  try{
    const resp = await fetch('/api/corrections/'+encodeURIComponent(id));
    const data = await resp.json();
    if(!resp.ok){ err.textContent=data.message; return; }
    err.textContent='';
    document.getElementById('corr-src').value = data.source_audit_id;
    document.getElementById('corr-active').value = data.active_slot;
    document.getElementById('corr-tx').value = data.target_transaction;
    if(Array.isArray(data.source_sectors)){
      document.getElementById('corr-sectors').value = data.source_sectors.join('\n');
    }
    renderCorrection(data);
  }catch(e){ err.textContent='网络错误：'+e; }
  finally{ btn.disabled=false; }
}

document.getElementById('corr-submit').addEventListener('click', submitCorrection);
document.getElementById('corr-load').addEventListener('click', loadCorrection);
document.getElementById('corr-copy').addEventListener('click', ()=>{
  document.getElementById('corr-sectors').value =
    document.getElementById('sectors').value;
  document.getElementById('corr-src').value =
    document.getElementById('audit').value.trim();
  document.getElementById('corr-active').value =
    document.getElementById('active').value.trim();
  document.getElementById('corr-err').textContent='已复用上方镜像与参数';
});
</script>
</body>
</html>
"""
