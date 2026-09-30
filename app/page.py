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
</script>
</body>
</html>
"""
