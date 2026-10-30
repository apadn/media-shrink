/* MediaShrink 前端逻辑（无构建步骤，原生 ES） */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ESC_MAP[c]);

const GROUPS = ['convert', 'already'];
const BODY_ID = { convert: '#convertBody', already: '#alreadyBody' };
const PAGER_ID = { convert: '#convertPager', already: '#alreadyPager' };

const S = {
  presets: [],
  presetId: 'hevc-high',
  preference: 'auto',
  scan: null,
  selected: new Set(),
  overrides: new Map(),
  expanded: new Set(),
  pages: { convert: 0, already: 0 },
  pageSize: 80,
  jobs: [],
  jobsRev: null,
  hw: null,
  browserPath: '',
  cfg: null,
  pendingDelete: [],
  // 转码历史（服务端持久化，用来回看 + 避免重复转码）
  hist: { items: [], total: 0, stats: null, offset: 0, q: '', size: 60 },
  // 全局手动配置（默认预设选「手动配置」时生效）
  manual: {
    codec: 'hevc', mode: 'crf', crf: 21, bitrate: 4000,
    speed: 'medium', max_height: 0, bit_depth: 'auto', audio: 'copy', audio_bitrate: 0,
  },
};

const MANUAL_ID = 'custom';

const SPEED_OPTS = {
  ultrafast: '极速（画质最差）',
  fast: '快',
  medium: '均衡',
  slow: '慢',
  veryslow: '最慢（画质最好）',
};
const SPEED_SHORT = { ultrafast: '极速', fast: '快速', medium: '均衡速度', slow: '慢速', veryslow: '最慢' };
const HEIGHT_OPTS = { 0: '保持原分辨率', 2160: '最高 2160p (4K)', 1440: '最高 1440p', 1080: '最高 1080p', 720: '最高 720p' };
const DEPTH_OPTS = { auto: '跟随源文件', 8: '强制 8bit', 10: '强制 10bit' };
const AUDIO_OPTS = { copy: '直接复制（无损）', aac: '转 AAC', opus: '转 Opus' };
const CODEC_SHORT = { av1: 'AV1', hevc: 'HEVC', h264: 'H.264' };
const CRF_HINT = {
  hevc: 'HEVC：18–22 接近无损，23–25 均衡，26 以上体积优先',
  av1: 'AV1：23–27 接近无损，28–31 均衡，32 以上体积优先（同数值比 HEVC 更省）',
  h264: 'H.264：18–20 高画质，21–23 均衡，24 以上体积优先',
};
const CRF_MAX = { av1: 63, hevc: 51, h264: 51 };

/* ------------------------------- 工具 ------------------------------- */
async function api(url, method = 'GET', body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(url, opt);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = { detail: text }; }
  if (!res.ok) {
    // 令牌过期/被换掉：刷新一次会落到登录页，比静默失败好
    if (res.status === 401) location.reload();
    throw new Error((data && (data.detail || data.error)) || `HTTP ${res.status}`);
  }
  return data;
}

function fmtBytes(n) {
  if (n === null || n === undefined || isNaN(n)) return '-';
  if (n < 1024) return `${n}B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)}${units[i]}`;
}

function fmtDur(sec) {
  if (!sec || sec <= 0) return '-';
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
               : `${m}:${String(s).padStart(2, '0')}`;
}

/** 历史记录的时间：近的说「几小时前」，远的直接给日期。 */
function fmtWhen(ts) {
  const t = Number(ts) || 0;
  if (!t) return '—';
  const sec = Date.now() / 1000 - t;
  if (sec >= 0) {
    if (sec < 60) return '刚刚';
    if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
    if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`;
    if (sec < 86400 * 7) return `${Math.floor(sec / 86400)} 天前`;
  }
  const d = new Date(t * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function toast(msg, isErr = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast' + (isErr ? ' err' : '');
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, isErr ? 5200 : 2600);
}

const codecClass = (c) => (c === 'av1' ? 'av1' : c === 'hevc' || c === 'h265' ? 'hevc' : c === 'h264' ? 'h264' : '');

const CH_LAYOUT = { 1: '单声道', 2: '立体声', 6: '5.1', 8: '7.1' };

function trackLabel(a) {
  const ch = CH_LAYOUT[a.channels] || `${a.channels}ch`;
  const br = a.bitrate ? ` · ${Math.round(a.bitrate / 1000)}k` : '';
  return `${ch}${br}`;
}

/* ------------------------------- 初始化 ------------------------------- */
// 顶栏版本号：直接取后端 /api/config 的 version，前端不再各写一份
function showVersion(v) {
  const el = $('#appVersion');
  if (!el) return;
  const ver = String(v ?? '').trim();
  if (!ver) return;
  el.textContent = `v${ver}`;
  el.title = `当前运行的镜像版本 v${ver}（${location.host}）`;
  el.hidden = false;
  document.title = `MediaShrink v${ver} · 媒体压缩`;
}

/* ------------------------------ 面板折叠 ------------------------------ */
// 扫描结果显示 / 任务队列 / 转码历史 都可以折叠：列表一长就不用一路往下滚。
// 折叠状态记在 localStorage，刷新后保持原样。
const COLLAPSE_KEY = 'ms.collapsed';

function collapsedSet() {
  try {
    const raw = JSON.parse(localStorage.getItem(COLLAPSE_KEY) || '[]');
    return new Set(Array.isArray(raw) ? raw : []);
  } catch {
    return new Set();
  }
}

function setPanelCollapsed(panel, collapsed, persist = true) {
  if (!panel) return;
  panel.classList.toggle('collapsed', !!collapsed);
  const btn = panel.querySelector('[data-panel-btn]');
  if (btn) btn.setAttribute('aria-expanded', String(!collapsed));
  if (!persist) return;
  const set = collapsedSet();
  if (collapsed) set.add(panel.id);
  else set.delete(panel.id);
  localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...set]));
}

function restorePanelState() {
  const set = collapsedSet();
  document.querySelectorAll('.panel').forEach((p) => {
    if (set.has(p.id)) setPanelCollapsed(p, true, false);
  });
}

function bindPanelToggles() {
  document.querySelectorAll('.panel [data-panel-btn]').forEach((btn) => {
    const panel = btn.closest('.panel');
    btn.addEventListener('click', () => {
      setPanelCollapsed(panel, !panel.classList.contains('collapsed'));
    });
  });
}

async function init() {
  restorePanelState();
  bindEvents();
  try {
    const [cfg, ps] = await Promise.all([api('/api/config'), api('/api/presets')]);
    S.cfg = cfg;
    showVersion(cfg.version);
    S.presets = ps.presets || [];
    S.presetId = cfg.default_preset || 'hevc-high';
    $('#pathInput').value = (cfg.media_roots && cfg.media_roots[0]) || '/media';
    $('#pathInput').placeholder = (cfg.media_roots || []).join(' 或 ');
    // 排除文件夹：默认用后端下发的 EXCLUDE_DIRS，改过就记住
    const savedExclude = localStorage.getItem('ms.exclude');
    $('#excludeInput').value = savedExclude !== null
      ? savedExclude
      : (cfg.exclude_dirs || []).join(', ');
    fillPresetSelect($('#presetSel'));
    fillPresetSelect($('#bulkPreset'));
    const saved = localStorage.getItem('ms.preset');
    if (saved && S.presets.some((p) => p.id === saved)) {
      S.presetId = saved;
    }
    $('#presetSel').value = S.presetId;

    // 手动配置的默认值由后端下发，避免两边各写一份
    if (cfg.manual_defaults) S.manual = { ...S.manual, ...cfg.manual_defaults };
    try {
      const sm = JSON.parse(localStorage.getItem('ms.manual') || 'null');
      if (sm && typeof sm === 'object') S.manual = { ...S.manual, ...sm };
    } catch (_) { /* 忽略坏数据 */ }
    renderGlobalManual();
  } catch (e) {
    toast('初始化失败：' + e.message, true);
  }
  loadHw();
  pollJobs().then(schedulePoll);
  loadHistory();

  // 支持 /?scan=/media/movies 直接触发扫描，方便收藏成书签
  const q = new URLSearchParams(location.search);
  const qp = q.get('preset');
  if (qp && S.presets.some((p) => p.id === qp)) {
    S.presetId = qp;
    $('#presetSel').value = qp;
    $('#bulkPreset').value = qp;
    renderGlobalManual();
  }
  const pre = q.get('scan');
  if (pre) {
    $('#pathInput').value = pre;
    setTimeout(doScan, 400);
  }
}

function fillPresetSelect(sel) {
  sel.innerHTML = S.presets
    .map((p) => {
      const badge = p.badge && !p.label.includes(p.badge) ? ' · ' + p.badge : '';
      return `<option value="${esc(p.id)}">${esc(p.label)}${esc(badge)}</option>`;
    })
    .join('');
  sel.value = S.presetId;
}

/* ------------------------------- 硬件 ------------------------------- */
async function loadHw() {
  try {
    const hw = await api('/api/hw');
    S.hw = hw;
    renderHw(hw);
    if (hw.probing) setTimeout(loadHw, 2500);
  } catch (e) {
    $('#hwBadges').innerHTML = `<span class="badge warn">硬件探测失败</span>`;
  }
}

function renderHw(hw) {
  const box = $('#hwBadges');
  if (hw.probing) {
    box.innerHTML = `<span class="badge muted">正在实测编码器…</span>`;
    return;
  }
  const encs = hw.encoders || [];
  const avail = encs.filter((e) => e.available);
  const parts = [];

  const dev = hw.devices || {};
  if (dev.nvidia && dev.nvidia.available) {
    const name = (dev.nvidia.gpus || [])[0] || 'NVIDIA';
    parts.push(`<span class="badge ok">NVENC · ${esc(name.split(',')[0])}</span>`);
  }
  if (dev.dri_mounted) {
    const drv = dev.vaapi_driver || '';
    const isIntel = /iHD|Intel/i.test(drv);
    const isAmd = /mesa|radeon|amd/i.test(drv);
    const label = isIntel ? 'Intel 核显' : isAmd ? 'AMD 显卡' : 'VAAPI 设备';
    // 有驱动 ≠ 能编码：只按 /dev/dri 这一路的实测结果给颜色，别让用户误以为核显在干活
    const driEnc = encs.filter((e) => e.available && (e.kind === 'intel' || e.kind === 'vaapi' || e.kind === 'amd'));
    const cls = driEnc.length ? 'ok' : 'warn';
    const tail = driEnc.length
      ? ` · ${driEnc.length} 个硬编可用`
      : (drv ? ' · 硬件编码不可用' : ' · 驱动未就绪');
    parts.push(`<span class="badge ${cls}" title="${esc(drv || '/dev/dri 已挂载，但 vainfo 没报出驱动')}">${label}${tail}</span>`);
  } else {
    parts.push(`<span class="badge warn">未挂载 /dev/dri（无硬编码）</span>`);
  }

  ['av1', 'hevc', 'h264'].forEach((c) => {
    const best = avail.find((e) => e.codec === c);
    if (best) {
      const hwFlag = best.kind !== 'cpu';
      parts.push(`<span class="badge ${hwFlag ? 'ok' : 'muted'}">${c.toUpperCase()}: ${esc(best.name)}</span>`);
    } else {
      parts.push(`<span class="badge warn">${c.toUpperCase()}: 不可用</span>`);
    }
  });

  parts.push(`<button class="link hw-more" data-hw-detail="1">详情</button>`);
  box.innerHTML = parts.join('');
  if (hw.ffmpeg) box.title = hw.ffmpeg;
}

/* 硬件编码器明细：点「详情」看每个候选编码器的实测结果与报错 */
const HW_RESULT = {
  cpu: ['软件编码', 'muted'],
  intel: ['QSV', 'ok'],
  nvidia: ['NVENC', 'ok'],
  amd: ['AMF', 'ok'],
  vaapi: ['VAAPI', 'ok'],
};

function openHwDetail() {
  const hw = S.hw || {};
  const dev = hw.devices || {};
  const encs = hw.encoders || [];
  const rows = encs.map((e) => {
    const ok = e.available;
    const kindLabel = (HW_RESULT[e.kind] || [e.kind])[0];
    const msg = e.message || '';
    const hint = e.hint || '';
    const cmd = e.cmd || '';
    return `<tr class="${ok ? 'ok' : 'fail'}">
      <td><b>${esc(e.name)}</b><div class="small muted">${kindLabel}</div></td>
      <td>${esc(e.codec.toUpperCase())}</td>
      <td>${ok ? '<span class="badge ok">可用</span>' : '<span class="badge warn">不可用</span>'}</td>
      <td class="hw-msg">${esc(msg)}${hint ? `<div class="hw-hint">${esc(hint)}</div>` : ''}${cmd ? `<div class="hw-cmd">$ ffmpeg ${esc(cmd)}</div>` : ''}</td>
    </tr>`;
  }).join('');

  const dri = (dev.vaapi_dri || []).join('  ');
  const nodes = dev.dri_nodes || [];
  const nodeTxt = nodes.length
    ? nodes.map((n) => `${esc(n.path)}<span class="muted"> · ${esc(n.mode)}</span>`).join('&nbsp;&nbsp;')
    : (dri ? esc(dri) : '');
  const profiles = dev.vaapi_profiles || [];
  const profEnc = profiles.filter((p) => /Main|Main10|Professional/i.test(p));
  const driEnc = encs.filter((e) => e.available && (e.kind === 'intel' || e.kind === 'vaapi' || e.kind === 'amd'));
  const mfx = dev.mfx || {};
  const qsvOk = encs.some((e) => e.kind === 'intel' && e.available);
  const vaapiOk = encs.some((e) => e.kind === 'vaapi' && e.available);

  // 按当前状态给一条最可能命中的处理建议
  let advice = '';
  if (!dev.dri_mounted) {
    advice = `宿主机有核显，但容器里看不到 <code>/dev/dri</code>。compose 里要有
      <code>devices: ["/dev/dri:/dev/dri"]</code>；Unraid 则要在模板里加一个 Device
      （名称 <code>/dev/dri</code>、值 <code>/dev/dri</code>），或在 Extra Parameters 里加
      <code>--device=/dev/dri</code>，然后重启容器。宿主机上先确认 <code>ls /dev/dri</code> 能看到
      <code>renderD128</code>。`;
  } else if (!dev.vaapi_driver) {
    advice = `设备进来了，但 <code>vainfo</code> 没跑起来${dev.vaapi_error ? `：<code>${esc(dev.vaapi_error)}</code>` : ''}。
      常见原因是渲染节点权限不足（容器内 <code>ls -l /dev/dri</code> 看 renderD128 的属组），
      加 <code>--group-add video</code> / <code>--group-add render</code> 后重试。`;
  } else if (!driEnc.length) {
    advice = `驱动正常，但每个编码器实测都失败${mfx.legacy ? '' : '；而且镜像里没有旧版 MFX 运行时（libmfx1），8~10 代核显的 QSV 必然起不来——升级到 1.0.5 以上镜像'}。
      具体原因看下表「结果 / 报错」列，点下面的「完整自检报告」可以把全部输出一次拿到。`;
  } else if (!qsvOk && vaapiOk) {
    advice = `核显已经在编码，走的是 <code>VAAPI</code>（${driEnc.filter((e) => e.kind === 'vaapi').map((e) => `<code>${esc(e.name)}</code>`).join('、')}）。
      QSV 这一路没通，原因见下表；两者用的是同一块媒体引擎，速度与画质基本没差别，
      编码器保持「自动」就会一直用 VAAPI。`;
  } else {
    advice = `硬件编码已生效：${driEnc.map((e) => `<code>${esc(e.name)}</code>`).join('、')}。任务里编码器选「自动」就会优先走它们。`;
  }

  $('#hwBody').innerHTML = `
    <div class="hw-facts">
      <div><span class="muted">/dev/dri</span> ${nodeTxt || '<span class="bad">未发现</span>'}</div>
      <div><span class="muted">VAAPI 驱动</span> ${dev.vaapi_driver ? esc(dev.vaapi_driver) : '<span class="bad">未就绪</span>'}</div>
      <div><span class="muted">编码 profile</span> ${profiles.length ? `${profiles.length} 个${profEnc.length ? '：' + esc(profEnc.slice(0, 6).join(', ')) : ''}` : '无'}</div>
      <div><span class="muted">MFX 运行时</span> ${mfx.legacy ? '旧版 MSDK ✓' : '<span class="bad">旧版 MSDK 缺失</span>'}
        · ${mfx.vpl ? 'oneVPL GPU ✓' : '<span class="muted">oneVPL GPU 缺失</span>'}</div>
      <div><span class="muted">ffmpeg</span> ${esc((hw.ffmpeg || '').slice(0, 90))}</div>
    </div>
    <div class="table-wrap">
      <table class="hw-table">
        <thead><tr><th>编码器</th><th>编码</th><th>实测</th><th>结果 / 报错</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <p class="small hw-tip"><b>怎么修</b>：${advice}</p>
    <p class="small muted hw-tip">「实测」是真的编 0.3 秒测试片：能跑通才会出现在徽章里。任何一条都能在容器里手动复现（<code>docker exec -it media-shrink sh</code>），照表格里给的命令贴进去跑即可。</p>`;
  $('#hwReportBox').hidden = true;
  $('#hwReportBox').innerHTML = '';
  $('#hwModal').hidden = false;
}

/* 完整自检报告：把 hw-doctor 的输出原样贴出来，方便截图求助 */
async function showHwReport() {
  const box = $('#hwReportBox');
  box.hidden = false;
  box.innerHTML = '<p class="small muted">正在跑自检，约 5~10 秒…</p>';
  let text = '';
  try {
    const r = await api('/api/hw/report');
    text = (r && r.text) || '（无输出）';
  } catch (e) {
    box.innerHTML = `<p class="small bad">自检失败：${esc(String(e))}</p>`;
    return;
  }
  box.innerHTML = `
    <div class="hw-report-head">
      <b>完整自检报告</b>
      <span class="grow"></span>
      <button class="ghost mini-btn" id="hwCopy">复制</button>
    </div>
    <pre class="hw-report">${esc(text)}</pre>`;
  const btn = $('#hwCopy');
  if (btn) {
    btn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(text);
        toast('报告已复制');
      } catch {
        toast('复制失败，请手动选中文本复制');
      }
    });
  }
}

/* ------------------------------- 目录浏览 ------------------------------- */
async function openBrowser(path) {
  const target = path || $('#pathInput').value.trim();
  try {
    const r = await api('/api/browse?path=' + encodeURIComponent(target));
    S.browserPath = r.path;
    $('#browserPath').textContent = r.path;
    const crumbs = [];
    const parts = r.path.split('/').filter(Boolean);
    let acc = '';
    crumbs.push(`<button data-nav="/">/</button>`);
    parts.forEach((p) => {
      acc += '/' + p;
      crumbs.push(`<button data-nav="${esc(acc)}">${esc(p)}</button>`);
    });
    $('#browserCrumbs').innerHTML = crumbs.join('<span class="muted">/</span>');
    const rows = [];
    if (r.parent) rows.push(`<div class="browser-row" data-nav="${esc(r.parent)}"><span class="ico">↑</span>上级目录</div>`);
    r.dirs.forEach((d) => {
      rows.push(`<div class="browser-row" data-nav="${esc(d.path)}"><span class="ico">▸</span>${esc(d.name)}</div>`);
    });
    r.files.forEach((f) => {
      rows.push(`<div class="browser-row"><span class="ico">▪</span>${esc(f.name)}<span class="sz">${fmtBytes(f.size)}</span></div>`);
    });
    if (!r.dirs.length && !r.files.length) rows.push(`<p class="empty" style="padding:20px">此目录下没有子目录或视频文件</p>`);
    $('#browserList').innerHTML = rows.join('');
    $('#browserModal').hidden = false;
  } catch (e) {
    toast(e.message, true);
  }
}

/* ------------------------------- 扫描 ------------------------------- */
// 排除文件夹输入框 → 数组。"@eaDir, 花絮, */sample" -> ["@eaDir","花絮","*/sample"]
function excludeList() {
  const raw = ($('#excludeInput').value || '');
  const seen = new Set();
  return raw.split(',')
    .map((s) => s.trim().replace(/^\/+|\/+$/g, ''))
    .filter((s) => {
      if (!s) return false;
      const k = s.toLowerCase();
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    });
}

async function doScan() {
  const path = $('#pathInput').value.trim();
  if (!path) return toast('请先填写媒体目录', true);
  const btn = $('#scanBtn');
  btn.disabled = true;
  btn.textContent = '扫描中…';
  try {
    const r = await api('/api/scan', 'POST', {
      path,
      recursive: $('#recursive').checked,
      preset_id: S.presetId,
      preference: S.preference,
      manual: S.manual,
      min_size_mb: parseFloat($('#minSize').value) || 0,
      exclude: excludeList(),
      ignore_history: $('#ignoreHistory').checked,
    });
    S.scan = r;
    S.overrides.clear();
    S.expanded.clear();
    S.pages = { convert: 0, already: 0 };
    // 只默认勾选「需要转码」组里建议压缩的；已是 AV1/HEVC 的默认不选
    S.selected = new Set(
      itemsOf('convert').filter((i) => i.suggestion === 'yes').map((i) => i.path)
    );
    renderTable();
    $('#resultPanel').hidden = false;
    // 扫完就是要看结果，之前折叠着的话这次自动展开（不写回记忆，下次仍按用户习惯）
    setPanelCollapsed($('#resultPanel'), false, false);
    const st = r.stats || {};
    if (!r.items.length) {
      toast('没有找到视频文件');
    } else {
      const hist = st.history_count ? `，${st.history_count} 个已转过` : '';
      toast(`扫描完成：需转码 ${st.convert_count || 0} 个，已是 AV1/HEVC ${st.already_count || 0} 个${hist}；默认勾选 ${S.selected.size} 个`);
    }
    if (r.errors && r.errors.length) toast(`${r.errors.length} 个文件无法读取，已跳过`, true);
  } catch (e) {
    toast('扫描失败：' + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '扫描';
  }
}

/* ------------------------------- 分组 ------------------------------- */
function itemsOf(group) {
  const all = (S.scan && S.scan.items) || [];
  return group === 'convert'
    ? all.filter((i) => i.codec_family === 'other')
    : all.filter((i) => i.codec_family !== 'other');
}

/* ------------------------------- 覆盖配置 ------------------------------- */
function overrideOf(path) {
  if (!S.overrides.has(path)) {
    S.overrides.set(path, {
      preset_id: null,        // null = 跟随全局预设
      preset_locked: false,   // 单行手动改过就锁定
      subtitle_mode: 'keep',  // keep = 软字幕轨 | none = 不要字幕
      subtitles: null,        // null = 全选外挂字幕
      audio_mode: '',
      audio_tracks: null,     // null = 全部音轨；Set = 指定集合
      custom_crf: null,
      manual: null,           // null = 跟随全局手动配置
    });
  }
  return S.overrides.get(path);
}

/** 这一行最终用的预设 id。 */
function presetOf(path) {
  return overrideOf(path).preset_id || S.presetId;
}

/** 这一行最终的手动配置（单行改过就用单行的，否则用全局的）。 */
function manualOf(path) {
  const ov = overrideOf(path);
  return ov.manual || S.manual;
}

function itemOf(path) {
  return (S.scan?.items || []).find((i) => i.path === path);
}

function chosenSubs(item) {
  const ov = overrideOf(item.path);
  const all = item.subtitles || [];
  if (ov.subtitles === null) return all.map((s) => s.path);
  return all.filter((s) => ov.subtitles.has(s.path)).map((s) => s.path);
}

function chosenTracks(item) {
  const ov = overrideOf(item.path);
  const all = (item.audio || []).map((a) => a.index);
  if (ov.audio_tracks === null) return all;
  return all.filter((i) => ov.audio_tracks.has(i));
}

/* ------------------------------- 手动配置 ------------------------------- */
function optList(map, value) {
  return Object.entries(map).map(([v, label]) =>
    `<option value="${esc(v)}"${String(v) === String(value) ? ' selected' : ''}>${esc(label)}</option>`
  ).join('');
}

function manualLabel(m) {
  const rate = m.mode === 'bitrate' ? `${m.bitrate}kbps` : `CRF ${m.crf}`;
  return `手动 · ${CODEC_SHORT[m.codec] || m.codec} ${rate}`;
}

function manualSummary(m) {
  const parts = [CODEC_SHORT[m.codec] || m.codec];
  parts.push(m.mode === 'bitrate' ? `${m.bitrate} kbps` : `CRF ${m.crf}`);
  parts.push(SPEED_SHORT[m.speed] || m.speed);
  parts.push(m.max_height ? `≤${m.max_height}p` : '原分辨率');
  parts.push(m.bit_depth === 'auto' ? '位深跟随源' : `${m.bit_depth}bit`);
  parts.push({ copy: '音频复制', aac: '音频 AAC', opus: '音频 Opus' }[m.audio] || m.audio);
  return parts.join(' · ');
}

function manualEditorHtml(m, owner, compact = false) {
  const crfMax = CRF_MAX[m.codec] || 51;
  const isBr = m.mode === 'bitrate';
  return `
  <div class="manual-box${compact ? ' compact' : ''}" data-owner="${esc(owner)}">
    <div class="mp-grid">
      <label class="mp-field"><span>编码格式</span>
        <select data-mp="codec">${optList(CODEC_SHORT, m.codec)}</select>
      </label>
      <label class="mp-field"><span>码率控制</span>
        <select data-mp="mode">${optList({ crf: '恒定质量（CRF）', bitrate: '目标码率' }, m.mode)}</select>
      </label>
      <label class="mp-field mp-crf"${isBr ? ' hidden' : ''}><span>CRF 质量</span>
        <div class="mp-range">
          <input type="range" min="0" max="${crfMax}" step="1" data-mp="crf-range" value="${m.crf}">
          <input type="number" class="num" min="0" max="${crfMax}" step="1" data-mp="crf" value="${m.crf}">
        </div>
      </label>
      <label class="mp-field mp-br"${isBr ? '' : ' hidden'}><span>视频码率</span>
        <div class="mp-range">
          <input type="number" class="num" min="200" max="200000" step="100" data-mp="bitrate" value="${m.bitrate}">
          <span class="unit">kbps</span>
        </div>
      </label>
      <label class="mp-field"><span>编码速度</span>
        <select data-mp="speed">${optList(SPEED_OPTS, m.speed)}</select>
      </label>
      <label class="mp-field"><span>分辨率上限</span>
        <select data-mp="max_height">${optList(HEIGHT_OPTS, m.max_height)}</select>
      </label>
      <label class="mp-field"><span>位深</span>
        <select data-mp="bit_depth">${optList(DEPTH_OPTS, m.bit_depth)}</select>
      </label>
      <label class="mp-field"><span>音频</span>
        <select data-mp="audio">${optList(AUDIO_OPTS, m.audio)}</select>
      </label>
    </div>
    <div class="mp-foot">
      <span class="mp-summary">${esc(manualSummary(m))}</span>
      <span class="mp-hint">${esc(isBr ? '码率模式下体积可控，画质随画面复杂度浮动' : (CRF_HINT[m.codec] || ''))}</span>
    </div>
  </div>`;
}

function renderGlobalManual() {
  const panel = $('#manualPanel');
  if (!panel) return;
  const on = S.presetId === MANUAL_ID;
  panel.hidden = !on;
  if (!on) return;
  $('#manualBody').innerHTML = manualEditorHtml(S.manual, 'global');
}

function boxOwnerCfg(owner) {
  if (owner === 'global') return S.manual;
  const ov = overrideOf(owner);
  if (!ov.manual) ov.manual = { ...S.manual };
  return ov.manual;
}

function commitManual(owner) {
  if (owner === 'global') {
    localStorage.setItem('ms.manual', JSON.stringify(S.manual));
    renderGlobalManual();
    const paths = (S.scan?.items || [])
      .filter((i) => presetOf(i.path) === MANUAL_ID)
      .map((i) => i.path);
    reestimate(paths);
    return;
  }
  reestimate([owner]);
}

/* ------------------------------- 表格渲染 ------------------------------- */
function renderTable() {
  GROUPS.forEach(renderGroup);
  updateStats();
}

function renderGroup(group) {
  const items = itemsOf(group);
  const body = $(BODY_ID[group]);
  if (!body) return;
  const totalPages = Math.max(1, Math.ceil(items.length / S.pageSize));
  if (S.pages[group] >= totalPages) S.pages[group] = totalPages - 1;
  const page = S.pages[group];
  const slice = items.slice(page * S.pageSize, (page + 1) * S.pageSize);

  // 空分组不显示表头，只留一行提示
  const wrap = body.closest('.table-wrap');
  const groupEl = body.closest('.group');
  let hint = groupEl.querySelector('.group-empty');
  if (!items.length) {
    if (!hint) {
      hint = document.createElement('p');
      hint.className = 'empty group-empty';
      groupEl.insertBefore(hint, wrap);
    }
    hint.textContent = group === 'convert'
      ? '没有需要转码的文件（全部已是 AV1 / HEVC）'
      : '没有已经是 AV1 / HEVC 的文件';
    hint.hidden = false;
    wrap.hidden = true;
  } else {
    if (hint) hint.hidden = true;
    wrap.hidden = false;
  }

  body.innerHTML = slice.length ? slice.map(rowHtml).join('') : '';

  const pager = $(PAGER_ID[group]);
  if (pager) {
    if (items.length <= S.pageSize) {
      pager.innerHTML = '';
      pager.hidden = true;
    } else {
      pager.hidden = false;
      pager.innerHTML = `
        <button class="ghost mini-btn" data-page="prev" data-group="${group}" ${page === 0 ? 'disabled' : ''}>上一页</button>
        <span class="muted small">第 ${page + 1} / ${totalPages} 页 · 共 ${items.length} 个</span>
        <button class="ghost mini-btn" data-page="next" data-group="${group}" ${page >= totalPages - 1 ? 'disabled' : ''}>下一页</button>`;
    }
  }

  const badge = $(group === 'convert' ? '#convertBadge' : '#alreadyBadge');
  if (badge) badge.textContent = String(items.length);

  const allChk = $(group === 'convert' ? '#checkAllConvert' : '#checkAllAlready');
  if (allChk) {
    const selCount = items.filter((i) => S.selected.has(i.path)).length;
    allChk.checked = items.length > 0 && selCount === items.length;
    allChk.indeterminate = selCount > 0 && selCount < items.length;
  }
}

function rowHtml(it) {
  const ov = overrideOf(it.path);
  const sel = S.selected.has(it.path);
  const presetId = presetOf(it.path);
  const subs = it.subtitles || [];
  const emb = it.embedded_subtitles || [];
  const chosen = chosenSubs(it).length;
  const tracks = it.audio || [];
  const chosenT = chosenTracks(it).length;

  const subCell = [];
  if (emb.length) subCell.push(`<span class="tag sub">内嵌 ${emb.length}</span>`);
  if (subs.length) subCell.push(`<span class="tag sub">外挂 ${chosen}/${subs.length}</span>`);
  if (!emb.length && !subs.length) subCell.push(`<span class="muted">—</span>`);

  let audioCell;
  if (!tracks.length) {
    audioCell = `<span class="muted">—</span>`;
  } else {
    const muted = chosenT === 0 ? ' style="opacity:.45"' : '';
    audioCell = `<span class="tag audio"${muted}>音轨 ${chosenT}/${tracks.length}</span>`;
  }

  // 预估用「省了多少」而不是「原体积的百分之几」——
  // 后者在 106% 这种数字上会让人以为是估算没生效
  const ratio = it.estimate_ratio || 0;
  let estSub = '—';
  let estCls = '';
  if (ratio > 0) {
    const pct = Math.round(Math.abs(1 - ratio) * 100);
    if (ratio >= 1.03) {
      estSub = `体积增加 ${pct}%`;
      estCls = ' up';
    } else if (ratio >= 0.95) {
      // ±3% 以内属于估算误差，说成"增加"只会让人以为估算坏了
      estSub = '体积基本持平';
      estCls = ' flat';
    } else {
      estSub = `预计省 ${pct}%`;
      estCls = ' down';
    }
  }
  const estHtml = it.history
    ? `<span class="est-badge hist-badge">已转过</span>
      <span class="est-sub">${esc(fmtWhen(it.history.at))}${it.history.size_after ? ` · ${fmtBytes(it.history.size_after)}` : ''}</span>`
    : `<span class="est-badge">${fmtBytes(it.estimate_bytes)}</span>
      <span class="est-sub${estCls}">${estSub}</span>`;

  const presetOpts = S.presets
    .map((p) => `<option value="${esc(p.id)}"${p.id === presetId ? ' selected' : ''}>${esc(p.label)}</option>`)
    .join('');

  return `
  <tr data-path="${esc(it.path)}" title="${esc(it.reason || '')}" class="${sel ? 'selected' : ''} suggest-${esc(it.suggestion)}">
    <td><input type="checkbox" class="rowchk" ${sel ? 'checked' : ''}></td>
    <td>
      <div class="fname">
        <span class="n"><button class="expand-btn" data-open="${S.expanded.has(it.path) ? '1' : '0'}" title="展开详细设置"></button> ${esc(it.filename)}${it.history ? ' <span class="tag hist" title="转码历史里已有这个文件，默认不勾选">已转过</span>' : ''}</span>
        <span class="p">${esc(it.dir)}</span>
      </div>
    </td>
    <td class="mono">${fmtBytes(it.size)}</td>
    <td class="mono">${it.width}×${it.height}<br><span class="muted small">${(it.fps || 0).toFixed(2)}fps</span></td>
    <td>
      <span class="tag ${codecClass(it.codec)}">${esc(it.codec)}</span>${it.hdr ? '<span class="tag hdr">HDR</span>' : ''}
      <br><span class="muted small">${it.bitrate ? Math.round(it.bitrate / 1000) + ' kbps' : '—'} · ${it.bit_depth}bit</span>
    </td>
    <td>${audioCell}<br><span class="muted small">${tracks.length ? esc(tracks.map((a) => a.language || a.codec).join('/')) : ''}</span></td>
    <td>${subCell.join('<br>')}</td>
    <td>${estHtml}</td>
    <td><select class="rowPreset mini">${presetOpts}</select></td>
  </tr>
  <tr class="row-detail" data-detail="${esc(it.path)}" ${S.expanded.has(it.path) ? '' : 'hidden'}>
    <td colspan="9">${detailHtml(it)}</td>
  </tr>`;
}

function detailHtml(it) {
  const ov = overrideOf(it.path);
  const subs = it.subtitles || [];
  const emb = it.embedded_subtitles || [];
  const chosen = chosenSubs(it);
  const tracks = it.audio || [];
  const chosenT = chosenTracks(it);
  const isManual = presetOf(it.path) === MANUAL_ID;
  const presetLabel = isManual
    ? manualLabel(manualOf(it.path))
    : (S.presets.find((p) => p.id === presetOf(it.path)) || {}).label || '-';

  const subRows = subs.map((s) => `
    <label class="subopt">
      <input type="checkbox" class="subchk" data-sub="${esc(s.path)}" ${chosen.includes(s.path) ? 'checked' : ''}>
      <span>${esc(s.filename)}</span>
      <span class="muted small">${esc(s.ext.toUpperCase())} · ${esc(s.label)}</span>
    </label>`).join('');

  const embRows = emb.length
    ? emb.map((s) => `<div class="subopt muted small">内嵌轨 #${s.index} · ${esc(s.codec)} ${s.language ? '· ' + esc(s.language) : ''} ${s.title ? '· ' + esc(s.title) : ''}</div>`).join('')
    : `<div class="subopt muted small">无内嵌字幕</div>`;

  const trackRows = tracks.length
    ? tracks.map((a) => `
      <label class="subopt track">
        <input type="checkbox" class="trackchk" data-track="${a.index}" ${chosenT.includes(a.index) ? 'checked' : ''}>
        <span class="tname">#${a.index} ${esc(a.codec)}${a.default ? ' · 默认' : ''}</span>
        <span class="muted small">${esc(trackLabel(a))}${a.language ? ' · ' + esc(a.language) : ''}${a.title ? ' · ' + esc(a.title) : ''}</span>
      </label>`).join('')
    : `<div class="subopt muted small">此文件没有音轨</div>`;

  return `<div class="detail-inner${isManual ? ' wide' : ''}">
    <div class="detail-block">
      <h4>音轨</h4>
      ${trackRows}
      <div class="quick-row">
        <button class="link" data-track-act="all">保留全部</button>
        <button class="link" data-track-act="default">仅默认轨</button>
        <button class="link" data-track-act="best">仅最高规格</button>
        <button class="link" data-track-act="none">全部去掉</button>
      </div>
      <select class="rowAudio mini" style="width:100%;margin-top:8px">
        <option value="" ${!ov.audio_mode ? 'selected' : ''}>音频编码：跟随预设</option>
        <option value="copy" ${ov.audio_mode === 'copy' ? 'selected' : ''}>直接复制（画质无损）</option>
        <option value="aac" ${ov.audio_mode === 'aac' ? 'selected' : ''}>转 AAC（兼容性好）</option>
        <option value="opus" ${ov.audio_mode === 'opus' ? 'selected' : ''}>转 Opus（体积最小）</option>
      </select>
      <div class="detail-note">默认复制原始音轨，音质零损失。只保留需要的语言轨可省下不少空间。</div>
    </div>

    <div class="detail-block">
      <h4>字幕</h4>
      ${embRows}
      ${subs.length ? subRows : '<div class="subopt muted small">同目录未发现外挂字幕</div>'}
      <div class="quick-row" style="margin-top:10px">
        <label class="subopt"><input type="radio" name="sm-${esc(it.id)}" class="submode" value="keep" ${ov.subtitle_mode === 'keep' ? 'checked' : ''}> 作为软字幕轨</label>
        <label class="subopt"><input type="radio" name="sm-${esc(it.id)}" class="submode" value="none" ${ov.subtitle_mode === 'none' ? 'checked' : ''}> 不要字幕</label>
      </div>
      <div class="detail-note">
        只做软字幕轨，不烧录画面 —— 烧录需要重新编码画面，会二次损失画质。软字幕可在播放器里随时开关。
      </div>
    </div>

    <div class="detail-block${isManual ? ' wide' : ''}">
      <h4>编码参数</h4>
      <div class="detail-note">
        预设：<b>${esc(presetLabel)}</b><br>
        编码器：<b>${esc(it.encoder || '待定')}</b>${it.encoder_kind && it.encoder_kind !== 'cpu' ? '（硬件）' : '（软件）'}<br>
        时长：${fmtDur(it.duration)}　章节：${it.chapters || 0}<br>
        预估：${fmtBytes(it.estimate_bytes)}　${esc(it.reason || '')}
      </div>
      ${isManual ? `
        <div class="mp-owner">这一行使用手动配置${ov.manual ? '' : '（跟随全局）'}
          ${ov.manual ? '<button class="link" data-mp-act="inherit">恢复跟随全局</button>' : ''}
        </div>
        ${manualEditorHtml(manualOf(it.path), it.path, true)}`
      : `<div class="detail-note">想逐项调参？把这一行的预设换成「手动配置」。</div>`}
    </div>
  </div>`;
}

function updateStats() {
  const all = (S.scan && S.scan.items) || [];
  const sel = all.filter((i) => S.selected.has(i.path));
  const before = sel.reduce((s, i) => s + (i.size || 0), 0);
  const after = sel.reduce((s, i) => s + (i.estimate_bytes || 0), 0);
  const save = before - after;
  const skipped = (S.scan && S.scan.stats && S.scan.stats.excluded_dirs) || 0;
  const hist = all.filter((i) => i.history).length;
  $('#statsText').textContent = all.length
    ? `· 共 ${all.length} 个 · 需转码 ${itemsOf('convert').length} · 已是 AV1/HEVC ${itemsOf('already').length}${hist ? ` · 已转过 ${hist}` : ''} · 已选 ${sel.length} · ${fmtBytes(before)} → ${fmtBytes(after)}（省 ${fmtBytes(save)}）${skipped ? ` · 已跳过 ${skipped} 个文件夹` : ''}`
    : '';
  $('#startBtn').disabled = sel.length === 0;
  $('#startBtn').textContent = sel.length ? `开始压缩（${sel.length}）` : '开始压缩';
}

/* ------------------------------- 预估重算 ------------------------------- */
async function reestimate(paths) {
  if (!paths.length) return;
  // 每个文件可能带各自的手动配置，所以逐条提交而不是按预设分组
  const items = paths.map((p) => {
    const ov = overrideOf(p);
    return {
      path: p,
      preset_id: presetOf(p),
      manual: manualOf(p),
      custom_crf: ov.custom_crf,
      tracks: ov.audio_tracks === null ? null : Array.from(ov.audio_tracks),
    };
  });
  try {
    const r = await api('/api/estimate', 'POST', {
      items, preference: S.preference, ignore_history: $('#ignoreHistory').checked,
    });
    Object.entries(r.items || {}).forEach(([path, v]) => {
      const it = itemOf(path);
      if (it) Object.assign(it, v);
    });
  } catch (e) { /* 失败时保留上一次的预估，不打断操作 */ }
  renderTable();
}

/* ------------------------------- 转码历史 ------------------------------- */
async function loadHistory(offset) {
  const h = S.hist;
  if (offset !== undefined) h.offset = Math.max(0, offset);
  try {
    const q = h.q ? `&q=${encodeURIComponent(h.q)}` : '';
    const r = await api(`/api/history?limit=${h.size}&offset=${h.offset}${q}`);
    h.items = r.items || [];
    h.total = r.total || 0;
    h.stats = r.stats || null;
    renderHistory();
  } catch (e) {
    /* 历史读失败不该挡住主流程 */
  }
}

function renderHistory() {
  const h = S.hist;
  const list = $('#histList');
  if (!list) return;
  const st = h.stats || {};
  $('#histStats').textContent = h.total
    ? `· ${h.total} 条 · 累计省 ${fmtBytes(st.bytes_saved || 0)}（${fmtBytes(st.bytes_before || 0)} → ${fmtBytes(st.bytes_after || 0)}）`
    : '';

  if (!h.items.length) {
    list.innerHTML = `<p class="empty">${h.q ? '没有匹配的记录' : '暂无历史记录，压缩成功后会记在这里'}</p>`;
    $('#histPager').hidden = true;
    return;
  }

  list.innerHTML = h.items.map((r) => {
    const before = r.size_before || 0;
    const after = r.size_after || 0;
    const saved = before > 0 && after > 0 ? 1 - after / before : 0;
    const delta = before && after
      ? `${fmtBytes(before)} → ${fmtBytes(after)}`
      : '—';
    const saveTxt = saved > 0.0005 ? `省 ${(saved * 100).toFixed(1)}%` : (saved < -0.0005 ? `增加 ${(-saved * 100).toFixed(1)}%` : '基本持平');
    const meta = [delta, saveTxt];
    if (r.duration) meta.push(`片长 ${fmtDur(r.duration)}`);
    if (r.encoder) meta.push(r.encoder + (r.encoder_kind === 'cpu' ? ' CPU' : ''));
    meta.push(fmtWhen(r.finished_at));

    const bits = [];
    if (r.preset_label) bits.push(`预设 ${r.preset_label}`);
    const act = { keep: '源文件保留', rename: '源文件已改名 .old', delete: '源文件已删除' }[r.source_action] || '源文件未处理';
    bits.push(act);
    return `<div class="job">
      <div class="job-top">
        <span class="job-name" title="${esc(r.src)}">${esc(String(r.src || '').split('/').pop())}</span>
        <span class="job-meta">${esc(meta.join(' · '))}</span>
        <span class="job-status done">完成</span>
      </div>
      <div class="job-msg hist-out" title="${esc(r.out)}"><span class="hist-label">输出</span>${esc(r.out || '')}</div>
      <div class="job-srcnote">${esc(bits.join(' · '))}</div>
    </div>`;
  }).join('');

  const pager = $('#histPager');
  const pages = Math.ceil(h.total / h.size);
  if (pages <= 1) {
    pager.hidden = true;
    pager.innerHTML = '';
  } else {
    const page = Math.floor(h.offset / h.size) + 1;
    pager.hidden = false;
    pager.innerHTML = `
      <button class="ghost mini-btn" data-histpage="prev" ${h.offset === 0 ? 'disabled' : ''}>上一页</button>
      <span class="muted small">第 ${page} / ${pages} 页 · 共 ${h.total} 条</span>
      <button class="ghost mini-btn" data-histpage="next" ${h.offset + h.size >= h.total ? 'disabled' : ''}>下一页</button>`;
  }
}

/* ------------------------------- 任务 ------------------------------- */
async function pollJobs() {
  try {
    const r = await api('/api/jobs' + (S.jobsRev === null ? '' : `?rev=${S.jobsRev}`));
    // 服务端说「没有变化」：连重绘都省了
    if (r && r.changed === false) return;
    if (typeof r.rev === 'number') S.jobsRev = r.rev;
    S.jobs = r.jobs || [];
    renderJobs();
    // 有新任务完成就刷新历史面板（历史是服务端写的，前端只在完成时拉一次）
    const done = S.jobs.filter((j) => j.status === 'done').length;
    if (done > (S.doneSeen || 0)) {
      S.doneSeen = done;
      loadHistory(0);
    } else {
      S.doneSeen = done;
    }
  } catch (_) { /* 静默 */ }
}

/* 轮询节奏跟着状态走：有任务在跑就 1.2 秒一次，空闲时 5 秒一次。
   压缩一个大文件要几小时，空闲时每秒拉一次纯属白烧流量和 CPU。 */
let pollTimer = null;
function schedulePoll() {
  clearTimeout(pollTimer);
  const busy = S.jobs.some((j) => j.status === 'running' || j.status === 'queued');
  pollTimer = setTimeout(async () => {
    await pollJobs();
    schedulePoll();
  }, busy ? 1200 : 5000);
}

function renderJobs() {
  const list = $('#jobList');
  if (!S.jobs.length) { list.innerHTML = '<p class="empty">暂无任务</p>'; $('#jobStats').textContent = ''; hideBulkPause(); return; }

  const running = S.jobs.filter((j) => j.status === 'running').length;
  const done = S.jobs.filter((j) => j.status === 'done').length;
  $('#jobStats').textContent = `· ${S.jobs.length} 个 · 进行中 ${running} · 完成 ${done}`;

  // 批量暂停 / 继续：只要有排队或运行中的任务就显示「暂停全部」；有暂停的就显示「继续全部」
  const pausable = S.jobs.filter((j) => j.status === 'queued' || (j.status === 'running' && !j.paused)).length;
  const resumable = S.jobs.filter((j) => j.status === 'paused' || (j.status === 'running' && j.paused)).length;
  const bp = $('#srcBulkPause');
  const br = $('#srcBulkResume');
  if (bp) { bp.hidden = pausable === 0; bp.textContent = `暂停全部（${pausable}）`; }
  if (br) { br.hidden = resumable === 0; br.textContent = `继续全部（${resumable}）`; }

  const pending = S.jobs.filter((j) => j.status === 'done' && !j.source_action);
  const bulkRename = $('#srcBulkRename');
  const bulkDelete = $('#srcBulkDelete');
  if (bulkRename) bulkRename.hidden = pending.length === 0;
  if (bulkDelete) bulkDelete.hidden = pending.length === 0 || !allowDelete();

  // 源文件已腾出原名的任务，可以批量把输出改回源文件名
  const bulkRenameOut = $('#srcBulkRenameOut');
  const renOutCount = S.jobs.filter(canRenameOutput).length;
  if (bulkRenameOut) {
    bulkRenameOut.hidden = renOutCount === 0;
    bulkRenameOut.textContent = `输出改回源文件名（${renOutCount}）`;
  }

  list.innerHTML = S.jobs.slice().reverse().map((j) => {
    const pct = j.status === 'done' ? 100 : (j.progress || 0);
    const cls = j.status === 'failed' ? 'failed' : j.status === 'done' ? 'done' : '';
    const meta = [];
    if (j.status === 'running') {
      if (j.paused) {
        meta.push('已暂停');
      } else {
        meta.push(`${pct.toFixed(1)}%`);
        if (j.speed) meta.push(`${j.speed.toFixed(2)}x`);
        if (j.eta > 0) meta.push(`剩余 ${fmtDur(j.eta)}`);
        if (j.encoder) meta.push(j.encoder + (j.encoder_kind === 'cpu' ? ' CPU' : ''));
      }
    } else if (j.status === 'done') {
      meta.push(`${fmtBytes(j.size_before)} → ${fmtBytes(j.size_after)}`);
    } else if (j.duration) {
      meta.push(fmtDur(j.duration));
    }
    // 暂停 / 继续按钮（排队中可暂停、暂停中可继续、运行中未暂停可暂停）
    const pauseBtn = (j.status === 'queued' || (j.status === 'running' && !j.paused))
      ? `<button class="ghost mini-btn" data-pause="${esc(j.id)}">暂停</button>` : '';
    const resumeBtn = (j.status === 'paused' || (j.status === 'running' && j.paused))
      ? `<button class="ghost mini-btn" data-resume="${esc(j.id)}">继续</button>` : '';
    return `<div class="job">
      <div class="job-top">
        <span class="job-name" title="${esc(j.src)}">${esc(j.src.split('/').pop())}</span>
        <span class="job-meta">${esc(meta.join(' · '))}</span>
        <span class="job-status ${esc(j.status)}">${statusLabel(j)}</span>
        ${pauseBtn}${resumeBtn}
        ${j.status === 'running' || j.status === 'queued'
          ? `<button class="ghost mini-btn" data-cancel="${esc(j.id)}">取消</button>` : ''}
      </div>
      <div class="bar ${cls}"><i style="width:${pct}%"></i></div>
      <div class="job-msg ${j.status === 'failed' ? 'err' : ''}">${esc(j.message || '')}</div>
      ${j.source_note
        ? `<div class="job-srcnote">${esc(j.source_note)}</div>${renameOutHtml(j)}`
        : (j.status === 'done' ? sourceActionsHtml(j) : '')}
    </div>`;
  }).join('');
}

function hideBulkPause() {
  const bp = $('#srcBulkPause');
  const br = $('#srcBulkResume');
  if (bp) bp.hidden = true;
  if (br) br.hidden = true;
}

function oldSuffix() { return S.cfg?.source_old_suffix || '.old'; }
function allowDelete() { return S.cfg?.allow_source_delete !== false; }

function sourceActionsHtml(j) {
  const del = allowDelete()
    ? `<button class="ghost mini-btn danger-ghost" data-src="delete" data-job="${esc(j.id)}">删除…</button>`
    : `<button class="ghost mini-btn" disabled title="服务端已禁用删除（ALLOW_SOURCE_DELETE=0）">删除…</button>`;
  return `<div class="job-src">
      <span class="job-src-label">压缩完成，源文件：</span>
      <button class="ghost mini-btn" data-src="keep" data-job="${esc(j.id)}">保留</button>
      <button class="ghost mini-btn" data-src="rename" data-job="${esc(j.id)}">改名加 ${esc(oldSuffix())}</button>
      ${del}
    </div>`;
}

function pendingSourceJobs() {
  return S.jobs.filter((j) => j.status === 'done' && !j.source_action);
}

/** 源文件腾出原名后（已删除或已归档 .old），才允许把输出改回源文件名。 */
function canRenameOutput(j) {
  return j.status === 'done'
    && !!j.out
    && (j.source_action === 'delete' || j.source_action === 'rename')
    && !j.renamed_to;
}

function renameOutHtml(j) {
  if (j.renamed_to) return `<div class="job-srcnote">${esc(j.rename_note || '')}</div>`;
  if (!canRenameOutput(j)) return '';
  const outName = (j.out || '').split('/').pop();
  const srcName = (j.src || '').split('/').pop().replace(/\.[^.]+$/, '');
  const outExt = (outName.match(/\.[^.]+$/) || [''])[0];
  return `<div class="job-src">
      <span class="job-src-label">输出文件还带着后缀：</span>
      <button class="ghost mini-btn" data-renout="${esc(j.id)}">改名为 ${esc(srcName + outExt)}</button>
    </div>`;
}

function renameOutputs(ids) {
  if (!ids.length) return toast('没有可改名的输出文件', true);
  return api('/api/jobs/rename-output', 'POST', { job_ids: ids })
    .then((r) => {
      const bad = (r.results || []).filter((x) => !x.ok);
      if (bad.length) toast(bad[0].message, true);
      else toast(`已把 ${r.ok_count} 个输出文件改回源文件名`);
      pollJobs();
    })
    .catch((err) => toast(err.message, true));
}

function pauseJobs(ids) {
  if (!ids.length) return toast('没有可暂停的任务', true);
  return api('/api/jobs/pause', 'POST', { job_ids: ids })
    .then((r) => {
      const bad = (r.results || []).filter((x) => !x.ok);
      if (bad.length) toast(bad[0].message, true);
      else toast(`已暂停 ${r.ok_count} 个任务`);
      pollJobs();
    })
    .catch((err) => toast(err.message, true));
}

function resumeJobs(ids) {
  if (!ids.length) return toast('没有可继续的任务', true);
  return api('/api/jobs/resume', 'POST', { job_ids: ids })
    .then((r) => {
      const bad = (r.results || []).filter((x) => !x.ok);
      if (bad.length) toast(bad[0].message, true);
      else toast(`已继续 ${r.ok_count} 个任务`);
      pollJobs();
    })
    .catch((err) => toast(err.message, true));
}

async function applySource(ids, action, confirm = false) {
  if (!ids.length) return;
  try {
    const r = await api('/api/jobs/source', 'POST', { job_ids: ids, action, confirm });
    const bad = (r.results || []).filter((x) => !x.ok);
    if (bad.length) {
      toast(bad[0].message, true);
    } else {
      const msg = action === 'rename' ? `已改名归档 ${r.ok_count} 个源文件`
        : action === 'delete' ? `已删除 ${r.ok_count} 个源文件`
          : `已标记保留 ${r.ok_count} 个源文件`;
      toast(msg);
    }
    if (action === 'rename' || action === 'delete') {
      // 只摘掉真正处理成功的，失败的留在列表里
      const gone = (r.results || []).filter((x) => x.ok).map((x) => x.path);
      forgetPaths(gone);
    }
    pollJobs();
  } catch (err) { toast(err.message, true); }
}

/** 源文件被改名或删除后，把它从扫描结果里摘掉，避免出现点不动的行。 */
function forgetPaths(paths) {
  if (!S.scan || !paths.length) return;
  const gone = new Set(paths);
  const before = {};
  GROUPS.forEach((g) => { before[g] = itemsOf(g).length; });
  S.scan.items = S.scan.items.filter((i) => !gone.has(i.path));
  GROUPS.forEach((g) => { if (itemsOf(g).length !== before[g]) S.pages[g] = 0; });
  gone.forEach((p) => { S.selected.delete(p); S.overrides.delete(p); S.expanded.delete(p); });
  renderTable();
}

function openDeleteModal(ids) {
  const jobs = S.jobs.filter((j) => ids.includes(j.id));
  if (!jobs.length) return;
  S.pendingDelete = ids;
  const list = jobs.slice(0, 6).map((j) => `<li>${esc(j.src)}</li>`).join('');
  const more = jobs.length > 6 ? `<li class="muted">…还有 ${jobs.length - 6} 个</li>` : '';
  $('#delBody').innerHTML = `
    <p>即将<b>永久删除</b> ${jobs.length} 个源文件，无法恢复（容器内没有回收站）。</p>
    <ul class="del-list">${list}${more}</ul>
    <div class="warnbox">
      更稳妥的做法是先「改名加 ${esc(oldSuffix())}」——文件只是多个后缀，随时能改回来，
      播放确认压缩结果没问题后再删。
    </div>
    <label class="chk del-ack"><input type="checkbox" id="delAck"> 我确认这些源文件可以永久删除</label>`;
  $('#delConfirm').disabled = true;
  $('#delModal').hidden = false;
  $('#delAck').addEventListener('change', (e) => { $('#delConfirm').disabled = !e.target.checked; });
}

function statusLabel(j) {
  const s = typeof j === 'string' ? j : j.status;
  if (typeof j === 'object' && j.status === 'running' && j.paused) return '已暂停';
  if (s === 'paused') return '已暂停';
  return { queued: '排队中', running: '转码中', done: '完成', failed: '失败', canceled: '已取消' }[s] || s;
}

/* ------------------------------- 提交 ------------------------------- */
function containerHint() {
  const c = (S.cfg?.output_container || 'mkv').toLowerCase();
  if (c === 'auto') return '跟随源文件容器（mp4→mp4，其余→mkv）';
  if (c === 'mp4') return 'MP4（遇 PGS/DVD 图形字幕时该文件自动改用 MKV）';
  if (c === 'webm') return 'WebM（仅限 AV1/VP9，其余文件自动改用 MKV）';
  return 'MKV（默认，兼容性最好：不限视频/音频编码，图形与特效字幕都能装）';
}

function openConfirm() {
  const items = (S.scan?.items || []).filter((i) => S.selected.has(i.path));
  if (!items.length) return toast('还没有勾选任何文件', true);

  const before = items.reduce((s, i) => s + i.size, 0);
  const after = items.reduce((s, i) => s + (i.estimate_bytes || 0), 0);
  let subCount = 0, trackCount = 0, trackTrimmed = 0, noSubCount = 0;
  const manualFiles = [];
  items.forEach((it) => {
    const ov = overrideOf(it.path);
    const totalTracks = (it.audio || []).length;
    const keepTracks = chosenTracks(it).length;
    trackCount += keepTracks;
    if (keepTracks !== totalTracks) trackTrimmed += 1;
    if (presetOf(it.path) === MANUAL_ID) manualFiles.push(it);
    if (ov.subtitle_mode === 'none') { noSubCount += 1; return; }
    subCount += (it.embedded_subtitles || []).length + chosenSubs(it).length;
  });

  const encoders = {};
  const encKind = {};
  items.forEach((it) => {
    const key = it.encoder || '自动';
    encoders[key] = (encoders[key] || 0) + 1;
    if (it.encoder_kind) encKind[key] = it.encoder_kind;
  });
  const encLine = Object.entries(encoders).map(([k, v]) => {
    const hwEnc = encKind[k] && encKind[k] !== 'cpu';
    return `${esc(k)} ×${v}${hwEnc ? '' : '<span class="muted">（软件）</span>'}`;
  }).join('、');

  // 硬件挂上了却只能软编 —— 这是最容易被忽略、代价最大的一种情况，明确提示
  const dev = (S.hw && S.hw.devices) || {};
  const hasHwDev = !!dev.dri_mounted || !!(dev.nvidia && dev.nvidia.available);
  const hwUsable = !!((S.hw && S.hw.hw_available) || []).length;
  const swCount = items.filter((it) => it.encoder && it.encoder_kind === 'cpu').length;
  const swWarn = (swCount && hasHwDev && !hwUsable)
    ? `<div class="warnbox">检测到显卡已挂载，但所有硬件编码器实测都没通过，本次 <b>${swCount}</b> 个文件会用 CPU 编码（慢 5~20 倍）。点顶部硬件徽章的「详情」能逐条看到报错与复现命令。</div>`
    : '';

  // 历史里已经压过的还勾着：说清楚，但允许（换参数重压是正当需求）
  const histItems = items.filter((it) => it.history);
  const histWarn = histItems.length
    ? `<div class="warnbox">选中的文件里有 <b>${histItems.length}</b> 个在转码历史中已经压过（源文件指纹没变）：
       ${esc(histItems.slice(0, 3).map((i) => i.filename).join('、'))}${histItems.length > 3 ? ' 等' : ''}。
       想换参数重压可以继续；只是想补齐的话，取消勾选能省下这些时间。</div>`
    : '';

  // 手动配置的汇总：参数相同的合并成一条
  const manualLines = [];
  if (manualFiles.length) {
    const seen = new Map();
    manualFiles.forEach((it) => {
      const text = manualSummary(manualOf(it.path));
      seen.set(text, (seen.get(text) || 0) + 1);
    });
    seen.forEach((n, text) => manualLines.push(`${esc(text)}${n > 1 ? ` ×${n}` : ''}`));
  }

  $('#confirmBody').innerHTML = `
    <dl>
      <dt>文件数量</dt><dd>${items.length} 个</dd>
      <dt>体积变化</dt><dd>${fmtBytes(before)} → 预估 ${fmtBytes(after)}（省 ${fmtBytes(before - after)}）</dd>
      <dt>编码器</dt><dd>${encLine}</dd>
      ${manualLines.length ? `<dt>手动参数</dt><dd>${manualLines.join('<br>')}</dd>` : ''}
      <dt>音轨</dt><dd>保留 ${trackCount} 条${trackTrimmed ? `，其中 ${trackTrimmed} 个文件裁掉了部分音轨` : '（全部保留）'}</dd>
      <dt>字幕</dt><dd>${subCount} 条软字幕轨${noSubCount ? `，${noSubCount} 个文件不保留字幕` : ''}（不烧录画面）</dd>
      <dt>输出格式</dt><dd>${esc(containerHint())}</dd>
      <dt>源文件</dt><dd>保留（不会自动删除，输出为 "_compressed" 后缀）</dd>
    </dl>
    ${swWarn}
    ${histWarn}
    <div class="warnbox">
      预估体积基于经验公式，实际结果会随画面复杂度浮动。画质优先时可把预设换成高画质档，
      或改用「手动配置」把 CRF 调低。
    </div>`;
  $('#confirmModal').hidden = false;
}

async function startJobs() {
  const items = (S.scan?.items || []).filter((i) => S.selected.has(i.path));
  const payload = {
    preset_id: S.presetId,
    preference: S.preference,
    manual: S.manual,
    items: items.map((it) => {
      const ov = overrideOf(it.path);
      const subs = ov.subtitle_mode === 'none' ? [] : chosenSubs(it);
      return {
        path: it.path,
        preset_id: presetOf(it.path),
        manual: manualOf(it.path),
        subtitles: subs,
        subtitle_mode: ov.subtitle_mode,
        audio_mode: ov.audio_mode || null,
        audio_tracks: ov.audio_tracks === null ? null : Array.from(ov.audio_tracks),
        custom_crf: ov.custom_crf,
      };
    }),
  };
  $('#confirmModal').hidden = true;
  try {
    const r = await api('/api/jobs', 'POST', payload);
    toast(`已加入队列：${r.jobs.length} 个任务`);
    pollJobs();
    // 刚提交完肯定要看进度，队列若折叠着就展开
    setPanelCollapsed(document.getElementById('jobPanel'), false, false);
    document.getElementById('jobPanel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (e) {
    toast('提交失败：' + e.message, true);
  }
}

/* ------------------------------- 事件 ------------------------------- */
function bindBody(el) {
  if (!el) return;

  el.addEventListener('click', (e) => {
    const row = e.target.closest('tr[data-path]');
    if (!row) return;
    const path = row.dataset.path;

    if (e.target.classList.contains('rowchk')) {
      if (e.target.checked) S.selected.add(path); else S.selected.delete(path);
      row.classList.toggle('selected', e.target.checked);
      updateStats();
      renderGroup(row.closest('.group').dataset.group);
      return;
    }
    if (e.target.classList.contains('expand-btn')) {
      const detail = row.nextElementSibling;
      if (!detail || !detail.classList.contains('row-detail')) return;
      const open = detail.hidden;
      detail.hidden = !open;
      e.target.dataset.open = open ? '1' : '0';
      if (open) S.expanded.add(path); else S.expanded.delete(path);
      return;
    }
    const subchk = e.target.closest('.subchk');
    if (subchk) {
      const ov = overrideOf(path);
      const it = itemOf(path);
      const all = (it.subtitles || []).map((s) => s.path);
      if (ov.subtitles === null) ov.subtitles = new Set(all);
      if (subchk.checked) ov.subtitles.add(subchk.dataset.sub);
      else ov.subtitles.delete(subchk.dataset.sub);
      renderGroup(row.closest('.group').dataset.group);
      return;
    }
    const tchk = e.target.closest('.trackchk');
    if (tchk) {
      const ov = overrideOf(path);
      const it = itemOf(path);
      const all = (it.audio || []).map((a) => a.index);
      if (ov.audio_tracks === null) ov.audio_tracks = new Set(all);
      const idx = parseInt(tchk.dataset.track, 10);
      if (tchk.checked) ov.audio_tracks.add(idx);
      else ov.audio_tracks.delete(idx);
      reestimate([path]);
      return;
    }
    const tAct = e.target.closest('[data-track-act]');
    if (tAct) {
      applyTrackAction(itemOf(path), tAct.dataset.trackAct);
      reestimate([path]);
      return;
    }
    const mode = e.target.closest('.submode');
    if (mode) {
      const ov = overrideOf(path);
      ov.subtitle_mode = mode.value;
      renderGroup(row.closest('.group').dataset.group);
      return;
    }
  });

  el.addEventListener('change', (e) => {
    const row = e.target.closest('tr[data-path]');
    if (!row) return;
    const path = row.dataset.path;
    if (e.target.classList.contains('rowPreset')) {
      const ov = overrideOf(path);
      ov.preset_id = e.target.value;
      ov.preset_locked = true;
      if (e.target.value === MANUAL_ID && !ov.manual) ov.manual = { ...S.manual };
      reestimate([path]);
    }
    if (e.target.classList.contains('rowAudio')) {
      overrideOf(path).audio_mode = e.target.value;
    }
  });
}

function applyTrackAction(it, kind) {
  if (!it) return;
  const ov = overrideOf(it.path);
  const tracks = it.audio || [];
  if (kind === 'all') { ov.audio_tracks = null; return; }
  if (kind === 'none') { ov.audio_tracks = new Set(); return; }
  if (kind === 'default') {
    const d = tracks.filter((t) => t.default).map((t) => t.index);
    ov.audio_tracks = new Set(d.length ? d : (tracks[0] ? [tracks[0].index] : []));
    return;
  }
  if (kind === 'best') {
    const best = tracks.slice().sort(
      (a, b) => ((b.channels || 0) - (a.channels || 0)) || ((b.bitrate || 0) - (a.bitrate || 0))
    )[0];
    ov.audio_tracks = new Set(best ? [best.index] : []);
  }
}

function bindEvents() {
  bindPanelToggles();
  $('#scanBtn').addEventListener('click', doScan);
  $('#pathInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') doScan(); });
  $('#excludeInput').addEventListener('change', () => {
    localStorage.setItem('ms.exclude', excludeList().join(', '));
  });
  $('#excludeInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') doScan(); });
  $('#browseBtn').addEventListener('click', () => openBrowser());
  $('#refreshHw').addEventListener('click', async () => {
    await api('/api/hw/refresh', 'POST');
    toast('正在重新实测编码器…');
    setTimeout(loadHw, 800);
  });
  $('#hwReprobe').addEventListener('click', async () => {
    await api('/api/hw/refresh', 'POST');
    toast('正在重新实测编码器…');
    setTimeout(loadHw, 800);
    setTimeout(() => { if (S.hw && !S.hw.probing) openHwDetail(); }, 2500);
  });
  $('#hwReport').addEventListener('click', showHwReport);

  $('#prefSel').addEventListener('change', (e) => {
    S.preference = e.target.value;
    if (S.scan) { reestimate((S.scan.items || []).map((i) => i.path)); }
  });
  $('#presetSel').addEventListener('change', (e) => {
    S.presetId = e.target.value;
    localStorage.setItem('ms.preset', S.presetId);
    renderGlobalManual();
    if (S.scan) {
      // 跟随全局的项刷新，单行手动改过的保持不动
      S.overrides.forEach((ov) => { if (!ov.preset_locked) ov.preset_id = null; });
      reestimate((S.scan.items || []).map((i) => i.path));
    }
  });

  // 手动配置：滑杆拖动时实时同步数字框（不触发重算）
  document.addEventListener('input', (e) => {
    const box = e.target.closest('.manual-box');
    if (!box || e.target.dataset.mp !== 'crf-range') return;
    const num = box.querySelector('[data-mp="crf"]');
    if (num) num.value = e.target.value;
  });

  // 手动配置：提交时写回配置并重算（滑杆松手、输入框失焦、下拉切换）
  document.addEventListener('change', (e) => {
    const box = e.target.closest('.manual-box');
    if (!box) return;
    const field = e.target.dataset.mp;
    if (!field) return;
    const owner = box.dataset.owner;
    const cfg = boxOwnerCfg(owner);

    if (field === 'crf-range' || field === 'crf') {
      const v = parseInt(e.target.value, 10);
      const max = CRF_MAX[cfg.codec] || 51;
      cfg.crf = isNaN(v) ? cfg.crf : Math.max(0, Math.min(max, v));
    } else if (field === 'max_height' || field === 'bitrate' || field === 'audio_bitrate') {
      const v = parseInt(e.target.value, 10);
      cfg[field] = isNaN(v) ? 0 : v;
    } else {
      cfg[field] = e.target.value;
    }
    // 切换编码格式后 CRF 上限可能变小
    if (cfg.crf > (CRF_MAX[cfg.codec] || 51)) cfg.crf = CRF_MAX[cfg.codec] || 51;
    commitManual(owner);
  });

  bindBody($('#convertBody'));
  bindBody($('#alreadyBody'));

  GROUPS.forEach((g) => {
    const chk = $(g === 'convert' ? '#checkAllConvert' : '#checkAllAlready');
    if (chk) {
      chk.addEventListener('change', (e) => {
        itemsOf(g).forEach((i) => {
          if (e.target.checked) S.selected.add(i.path); else S.selected.delete(i.path);
        });
        renderTable();
      });
    }
  });

  document.addEventListener('click', (e) => {
    const hwMore = e.target.closest('[data-hw-detail]');
    if (hwMore) { openHwDetail(); return; }
    const mpAct = e.target.closest('[data-mp-act]');
    if (mpAct) {
      if (mpAct.dataset.mpAct === 'reset') {
        S.manual = { ...S.manual, ...(S.cfg?.manual_defaults || {}) };
        localStorage.setItem('ms.manual', JSON.stringify(S.manual));
        renderGlobalManual();
        reestimate((S.scan?.items || [])
          .filter((i) => presetOf(i.path) === MANUAL_ID).map((i) => i.path));
        toast('已恢复手动配置默认值');
      } else if (mpAct.dataset.mpAct === 'inherit') {
        const detail = mpAct.closest('tr.row-detail');
        const path = detail?.dataset.detail;
        if (path) {
          overrideOf(path).manual = null;
          reestimate([path]);
        }
      }
      return;
    }
    const selBtn = e.target.closest('[data-sel]');
    if (selBtn) {
      const g = selBtn.dataset.group;
      const act = selBtn.dataset.sel;
      if (act === 'none') itemsOf(g).forEach((i) => S.selected.delete(i.path));
      if (act === 'recommend') {
        itemsOf(g).forEach((i) => { if (i.suggestion === 'yes') S.selected.add(i.path); });
      }
      renderTable();
      return;
    }
    const pg = e.target.closest('[data-page]');
    if (pg) {
      const g = pg.dataset.group;
      S.pages[g] = pg.dataset.page === 'prev'
        ? Math.max(0, S.pages[g] - 1)
        : S.pages[g] + 1;
      renderGroup(g);
      return;
    }
    const nav = e.target.closest('[data-nav]');
    if (nav) { openBrowser(nav.dataset.nav); return; }
    const close = e.target.closest('[data-close]');
    if (close) { $('#' + close.dataset.close).hidden = true; return; }
    if (e.target.classList.contains('modal')) e.target.hidden = true;
  });

  $('#applyPreset').addEventListener('click', async () => {
    const pid = $('#bulkPreset').value;
    const paths = (S.scan?.items || []).filter((i) => S.selected.has(i.path)).map((i) => i.path);
    if (!paths.length) return toast('先勾选要应用的文件', true);
    paths.forEach((p) => { const ov = overrideOf(p); ov.preset_id = pid; ov.preset_locked = true; });
    toast(`已把预设应用到 ${paths.length} 个文件`);
    reestimate(paths);
  });

  $('#startBtn').addEventListener('click', openConfirm);
  $('#confirmStart').addEventListener('click', startJobs);
  $('#clearJobs').addEventListener('click', async () => {
    const pending = pendingSourceJobs().length;
    if (pending && !confirm(`还有 ${pending} 个任务的源文件没处理，清理记录后需要自己去文件系统处理。继续？`)) return;
    const r = await api('/api/jobs/clear', 'POST');
    toast(`已清理 ${r.removed} 条记录`);
    pollJobs();
  });

  $('#srcBulkRename').addEventListener('click', () => {
    const ids = pendingSourceJobs().map((j) => j.id);
    if (!ids.length) return toast('没有待处理的源文件', true);
    applySource(ids, 'rename');
  });
  $('#srcBulkPause').addEventListener('click', () => {
    const ids = S.jobs.filter((j) => j.status === 'queued' || (j.status === 'running' && !j.paused)).map((j) => j.id);
    if (!ids.length) return toast('没有可暂停的任务', true);
    pauseJobs(ids);
  });
  $('#srcBulkResume').addEventListener('click', () => {
    const ids = S.jobs.filter((j) => j.status === 'paused' || (j.status === 'running' && j.paused)).map((j) => j.id);
    if (!ids.length) return toast('没有可继续的任务', true);
    resumeJobs(ids);
  });
  $('#srcBulkDelete').addEventListener('click', () => {
    const ids = pendingSourceJobs().map((j) => j.id);
    if (!ids.length) return toast('没有待处理的源文件', true);
    openDeleteModal(ids);
  });
  $('#srcBulkRenameOut').addEventListener('click', () => {
    const ids = S.jobs.filter(canRenameOutput).map((j) => j.id);
    if (!ids.length) return toast('没有可改名的输出文件', true);
    renameOutputs(ids);
  });
  $('#delConfirm').addEventListener('click', async () => {
    if ($('#delConfirm').disabled) return;
    $('#delModal').hidden = true;
    await applySource(S.pendingDelete.slice(), 'delete', true);
    S.pendingDelete = [];
  });

  // ---------------- 转码历史 ----------------
  $('#histRefresh').addEventListener('click', () => { loadHistory(0); toast('历史已刷新'); });
  let histTimer = null;
  $('#histQ').addEventListener('input', (e) => {
    S.hist.q = e.target.value.trim();
    clearTimeout(histTimer);
    histTimer = setTimeout(() => loadHistory(0), 250);
  });
  $('#histClear').addEventListener('click', async () => {
    if (!S.hist.total) return toast('历史已经是空的');
    if (!confirm('清空转码历史后，已经压过的文件不会再被标成「已转过」，可能被重复压缩。确定清空？')) return;
    try {
      const r = await api('/api/history/clear', 'POST');
      toast(`已清空 ${r.removed} 条历史`);
      S.doneSeen = 0;
      loadHistory(0);
      // 扫描结果里的「已转过」标记也要跟着消失
      if (S.scan) {
        S.scan.items.forEach((i) => { i.history = null; });
        reestimate((S.scan.items || []).map((i) => i.path));
      }
    } catch (err) { toast(err.message, true); }
  });
  $('#histPager').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-histpage]');
    if (!btn || btn.disabled) return;
    const step = S.hist.size;
    loadHistory(btn.dataset.histpage === 'prev' ? S.hist.offset - step : S.hist.offset + step);
  });

  $('#jobList').addEventListener('click', async (e) => {
    const renOut = e.target.closest('[data-renout]');
    if (renOut && !renOut.disabled) {
      renameOutputs([renOut.dataset.renout]);
      return;
    }
    const srcBtn = e.target.closest('[data-src]');
    if (srcBtn && !srcBtn.disabled) {
      const id = srcBtn.dataset.job;
      const action = srcBtn.dataset.src;
      if (action === 'delete') openDeleteModal([id]);
      else applySource([id], action);
      return;
    }
    const pauseBtn = e.target.closest('[data-pause]');
    if (pauseBtn && !pauseBtn.disabled) {
      pauseJobs([pauseBtn.dataset.pause]);
      return;
    }
    const resumeBtn = e.target.closest('[data-resume]');
    if (resumeBtn && !resumeBtn.disabled) {
      resumeJobs([resumeBtn.dataset.resume]);
      return;
    }
    const btn = e.target.closest('[data-cancel]');
    if (!btn) return;
    try {
      await api(`/api/jobs/${btn.dataset.cancel}/cancel`, 'POST');
      toast('已发送取消请求');
      pollJobs();
    } catch (err) { toast(err.message, true); }
  });

  $('#browserPick').addEventListener('click', () => {
    $('#pathInput').value = S.browserPath;
    $('#browserModal').hidden = true;
  });
}

document.addEventListener('DOMContentLoaded', init);
