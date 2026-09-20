let activeSection = 'dashboard';
let pollInterval = null;

document.addEventListener('DOMContentLoaded', () => {
  initNavigation();
  checkAuth();
  loadSection(activeSection);
  startPolling();

  // Handle Login form
  document.getElementById('login-form')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const u = document.getElementById('login-username').value;
    const p = document.getElementById('login-password').value;
    const errBox = document.getElementById('login-error');
    try {
      await API.post('/api/auth/login', { username: u, password: p });
      document.getElementById('login-modal').classList.remove('active');
      loadSection(activeSection);
    } catch (err) {
      errBox.innerText = err.message || 'Login failed';
      errBox.style.display = 'block';
    }
  });
});

function initNavigation() {
  document.querySelectorAll('.nav-item').forEach((item) => {
    item.addEventListener('click', () => {
      document.querySelectorAll('.nav-item').forEach((n) => n.classList.remove('active'));
      document.querySelectorAll('.section').forEach((s) => s.classList.remove('active'));

      item.classList.add('active');
      activeSection = item.getAttribute('data-section');
      document.getElementById(`sec-${activeSection}`).classList.add('active');
      loadSection(activeSection);
    });
  });

  document.getElementById('btn-logout')?.addEventListener('click', async () => {
    await API.post('/api/auth/logout', {});
    location.reload();
  });
}

async function checkAuth() {
  try {
    const me = await API.get('/api/auth/me');
    document.getElementById('admin-username-display').innerText = me.username;
  } catch (err) {
    document.getElementById('login-modal').classList.add('active');
  }
}

function startPolling() {
  if (pollInterval) clearInterval(pollInterval);
  pollInterval = setInterval(() => {
    if (activeSection === 'dashboard') loadDashboard();
    else if (activeSection === 'queue') loadQueue();
    else if (activeSection === 'logs') loadLogs();
  }, 3000);
}

function loadSection(name) {
  switch (name) {
    case 'dashboard': loadDashboard(); break;
    case 'queue': loadQueue(); break;
    case 'jobs': loadJobs(); break;
    case 'cache': loadCache(); break;
    case 'users': loadUsers(); break;
    case 'telegram': loadTelegramConfig(); break;
    case 'must-join': loadMustJoin(); break;
    case 'youtube': loadYouTube(); break;
    case 'cookies': loadCookies(); break;
    case 'ai': loadAI(); break;
    case 'settings': loadSettings(); break;
    case 'system': loadSystem(); break;
    case 'logs': loadLogs(); break;
    case 'audit': loadAudit(); break;
  }
}

// 1. DASHBOARD
async function loadDashboard() {
  try {
    const data = await API.get('/api/dashboard/stats');
    document.getElementById('stat-cpu').innerText = `${data.system.cpu_percent}%`;
    document.getElementById('stat-ram').innerText = `${data.system.ram_used_gb} / ${data.system.ram_total_gb} GB (${data.system.ram_percent}%)`;
    document.getElementById('stat-disk').innerText = `${data.system.disk_used_gb} / ${data.system.disk_total_gb} GB`;
    document.getElementById('stat-temp').innerText = `${data.system.temp_used_gb} GB`;

    document.getElementById('stat-active-jobs').innerText = `${data.queue.active_count} / ${data.queue.max_active}`;
    document.getElementById('stat-queue-len').innerText = data.queue.queued_count;
    document.getElementById('stat-cache-entries').innerText = data.cache.entries_count;
    document.getElementById('stat-cache-hitrate').innerText = `${data.cache.hit_rate_pct}%`;
    document.getElementById('stat-completed').innerText = data.jobs.completed;
    document.getElementById('stat-failed').innerText = data.jobs.failed;

    document.getElementById('stat-max-dur').innerText = data.settings.max_duration_formatted;
    document.getElementById('stat-ai-status').innerText = data.settings.ai_enabled ? (data.settings.ai_configured ? 'ON (Ready)' : 'ON (Not Configured)') : 'OFF';
    document.getElementById('stat-cookie-status').innerText = data.settings.cookies_enabled ? 'ON' : 'OFF';
    document.getElementById('stat-mj-status').innerText = data.settings.must_join_enabled ? `ON (${data.settings.required_channels_count} channels)` : 'OFF';

    // Tool versions
    document.getElementById('v-app').innerText = data.tools.application;
    document.getElementById('v-py').innerText = data.tools.python;
    document.getElementById('v-ytdlp').innerText = data.tools.ytdlp;
    document.getElementById('v-ejs').innerText = data.tools.ytdlp_ejs;
    document.getElementById('v-ffmpeg').innerText = data.tools.ffmpeg;
    document.getElementById('v-deno').innerText = data.tools.deno;
  } catch (err) {}
}

// 2. QUEUE
async function loadQueue() {
  try {
    const data = await API.get('/api/queue');
    const pauseBtn = document.getElementById('btn-queue-pause');
    if (data.is_paused) {
      pauseBtn.innerText = '▶ Resume Queue';
      pauseBtn.className = 'btn btn-success';
    } else {
      pauseBtn.innerText = '⏸ Pause Queue';
      pauseBtn.className = 'btn btn-warning';
    }
    document.getElementById('queue-max-active-input').value = data.max_active_jobs;

    // Active Jobs Table
    const activeTbody = document.getElementById('tbody-active-jobs');
    if (data.active_jobs.length === 0) {
      activeTbody.innerHTML = '<tr><td colspan="6" style="text-align:center;">No active jobs currently running</td></tr>';
    } else {
      activeTbody.innerHTML = data.active_jobs.map(j => `
        <tr>
          <td><code>${j.id.slice(0, 8)}</code></td>
          <td><b>${escapeHtml(j.title)}</b></td>
          <td>${j.operation} ${j.codec ? `· ${j.codec}` : ''} ${j.resolution || ''}</td>
          <td><span class="badge badge-info">${j.stage}</span></td>
          <td>
            ${j.progress}% ${j.speed ? `(${j.speed})` : ''} ${j.eta ? `ETA: ${j.eta}` : ''}
            <div class="progress-bar-bg"><div class="progress-bar-fill" style="width: ${j.progress}%"></div></div>
          </td>
          <td><button class="btn btn-danger" onclick="cancelJob('${j.id}')">Cancel</button></td>
        </tr>
      `).join('');
    }

    // Queued Jobs Table
    const queuedTbody = document.getElementById('tbody-queued-jobs');
    if (data.queued_jobs.length === 0) {
      queuedTbody.innerHTML = '<tr><td colspan="6" style="text-align:center;">Queue is empty</td></tr>';
    } else {
      queuedTbody.innerHTML = data.queued_jobs.map(j => `
        <tr>
          <td><b>#${j.position}</b></td>
          <td><code>${j.id.slice(0, 8)}</code></td>
          <td><b>${escapeHtml(j.title)}</b></td>
          <td>${j.operation} ${j.codec ? `· ${j.codec}` : ''} ${j.resolution || ''}</td>
          <td>${formatDate(j.queued_at)}</td>
          <td><button class="btn btn-danger" onclick="cancelJob('${j.id}')">Cancel</button></td>
        </tr>
      `).join('');
    }
  } catch (err) {}
}

async function toggleQueuePause() {
  const data = await API.get('/api/queue');
  if (data.is_paused) {
    await API.post('/api/queue/resume', {});
  } else {
    await API.post('/api/queue/pause', {});
  }
  loadQueue();
}

async function updateConcurrency() {
  const val = parseInt(document.getElementById('queue-max-active-input').value, 10);
  try {
    const res = await API.post('/api/queue/concurrency', { max_active_jobs: val });
    if (res.warning) alert(`⚠️ ${res.warning}`);
    loadQueue();
  } catch (err) {
    alert(err.message);
  }
}

async function cancelJob(id) {
  if (confirm(`Cancel job ${id}?`)) {
    await API.post(`/api/queue/cancel/${id}`, {});
    loadQueue();
  }
}

// 3. JOBS
async function loadJobs() {
  const search = document.getElementById('jobs-search')?.value || '';
  const status = document.getElementById('jobs-filter-status')?.value || '';
  try {
    const data = await API.get(`/api/jobs?limit=50&search=${encodeURIComponent(search)}&status=${encodeURIComponent(status)}`);
    const tbody = document.getElementById('tbody-jobs');
    if (data.items.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;">No jobs found</td></tr>';
    } else {
      tbody.innerHTML = data.items.map(j => `
        <tr>
          <td><code>${j.id.slice(0, 8)}</code></td>
          <td><b>${escapeHtml(j.title)}</b></td>
          <td>${j.operation} ${j.codec ? `· ${j.codec}` : ''} ${j.resolution || ''}</td>
          <td><span class="badge ${getBadgeClass(j.status)}">${j.status}</span></td>
          <td>${formatDate(j.created_at)}</td>
          <td>${formatDate(j.completed_at)}</td>
          <td>
            ${j.status === 'FAILED' ? `<button class="btn btn-secondary" onclick="retryJob('${j.id}')">Retry</button>` : ''}
            ${['QUEUED', 'PREPARING', 'DOWNLOADING', 'PROCESSING', 'UPLOADING'].includes(j.status) ? `<button class="btn btn-danger" onclick="cancelJob('${j.id}')">Cancel</button>` : ''}
          </td>
        </tr>
      `).join('');
    }
  } catch (err) {}
}

async function retryJob(id) {
  await API.post(`/api/queue/retry/${id}`, {});
  loadJobs();
}

// 4. CACHE
async function loadCache() {
  const search = document.getElementById('cache-search')?.value || '';
  try {
    const data = await API.get(`/api/cache?limit=50&search=${encodeURIComponent(search)}`);
    const tbody = document.getElementById('tbody-cache');
    if (data.items.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;">No cached items found</td></tr>';
    } else {
      tbody.innerHTML = data.items.map(c => `
        <tr>
          <td><code>${c.source_id}</code></td>
          <td><b>${escapeHtml(c.title)}</b></td>
          <td>${c.operation} ${c.codec || ''} ${c.resolution || c.subtitle_lang || ''}</td>
          <td>${c.hit_count} hits</td>
          <td>${c.file_size ? `${(c.file_size / (1024*1024)).toFixed(1)} MB` : 'N/A'}</td>
          <td><span class="badge ${c.is_valid ? 'badge-success' : 'badge-danger'}">${c.is_valid ? 'VALID' : 'INVALID'}</span></td>
          <td>
            ${c.is_valid ? `<button class="btn btn-warning" onclick="invalidateCache(${c.id})">Invalidate</button>` : ''}
            <button class="btn btn-danger" onclick="deleteCache(${c.id})">Delete</button>
          </td>
        </tr>
      `).join('');
    }
  } catch (err) {}
}

async function invalidateCache(id) {
  await API.post(`/api/cache/${id}/invalidate`, {});
  loadCache();
}

async function deleteCache(id) {
  if (confirm('Delete this cache record from database?')) {
    await API.delete(`/api/cache/${id}`);
    loadCache();
  }
}

// 5. USERS
async function loadUsers() {
  const search = document.getElementById('users-search')?.value || '';
  try {
    const data = await API.get(`/api/users?limit=50&search=${encodeURIComponent(search)}`);
    const tbody = document.getElementById('tbody-users');
    if (data.items.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;">No users found</td></tr>';
    } else {
      tbody.innerHTML = data.items.map(u => `
        <tr>
          <td><code>${u.id}</code></td>
          <td><b>@${escapeHtml(u.username || 'N/A')}</b> (${escapeHtml(u.first_name || '')})</td>
          <td><span class="badge ${u.role === 'ADMIN' ? 'badge-info' : 'badge-secondary'}">${u.role}</span></td>
          <td><span class="badge ${u.status === 'ACTIVE' ? 'badge-success' : 'badge-danger'}">${u.status}</span></td>
          <td>${u.total_jobs} (✅ ${u.successful_jobs} | ❌ ${u.failed_jobs})</td>
          <td>${formatDate(u.last_seen_at)}</td>
          <td>
            ${u.status === 'ACTIVE' ? `<button class="btn btn-danger" onclick="setUserStatus(${u.id}, 'BANNED')">Ban</button>` : `<button class="btn btn-success" onclick="setUserStatus(${u.id}, 'ACTIVE')">Unban</button>`}
          </td>
        </tr>
      `).join('');
    }
  } catch (err) {}
}

async function setUserStatus(id, st) {
  await API.post(`/api/users/${id}/status`, { status: st });
  loadUsers();
}

// 6. MUST JOIN
async function loadMustJoin() {
  try {
    const data = await API.get('/api/must-join');
    const toggleBtn = document.getElementById('btn-mj-toggle');
    if (data.enabled) {
      toggleBtn.innerText = 'Must Join: ENABLED';
      toggleBtn.className = 'btn btn-success';
    } else {
      toggleBtn.innerText = 'Must Join: DISABLED';
      toggleBtn.className = 'btn btn-secondary';
    }

    const tbody = document.getElementById('tbody-must-join');
    if (data.channels.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;">No required channels configured</td></tr>';
    } else {
      tbody.innerHTML = data.channels.map(ch => `
        <tr>
          <td><code>${ch.chat_id}</code></td>
          <td><b>${escapeHtml(ch.title)}</b></td>
          <td>${ch.username ? `@${ch.username}` : (ch.invite_url ? `<a href="${ch.invite_url}" target="_blank">Invite</a>` : 'N/A')}</td>
          <td><span class="badge ${ch.bot_status === 'administrator' ? 'badge-success' : 'badge-danger'}">${ch.bot_status}</span></td>
          <td><span class="badge ${ch.enabled ? 'badge-success' : 'badge-secondary'}">${ch.enabled ? 'YES' : 'NO'}</span></td>
          <td>${formatDate(ch.last_bot_check)}</td>
          <td>
            <button class="btn btn-secondary" onclick="testChannel(${ch.id})">Test</button>
            <button class="btn btn-danger" onclick="deleteChannel(${ch.id})">Delete</button>
          </td>
        </tr>
      `).join('');
    }

    // Load custom Must-Join template message
    try {
      const msgData = await API.get('/api/must-join/message');
      const area = document.getElementById('mj-custom-msg-area');
      if (area && msgData.message) {
        area.value = msgData.message;
      }
    } catch (e) {}
  } catch (err) {}
}

async function toggleMustJoin() {
  const data = await API.get('/api/must-join');
  await API.post('/api/must-join/toggle', { enabled: !data.enabled });
  loadMustJoin();
}

async function addChannelModalSubmit(e) {
  e.preventDefault();
  const cid = parseInt(document.getElementById('mj-chat-id').value, 10);
  const title = document.getElementById('mj-title').value;
  const username = document.getElementById('mj-username').value;
  const invite = document.getElementById('mj-invite').value;

  try {
    await API.post('/api/must-join/channels', {
      chat_id: cid,
      title: title,
      username: username || null,
      invite_url: invite || null,
      enabled: true,
    });
    document.getElementById('modal-add-channel').classList.remove('active');
    loadMustJoin();
  } catch (err) {
    alert(err.message);
  }
}

async function testChannel(id) {
  const res = await API.post(`/api/must-join/channels/${id}/test`, {});
  alert(res.is_admin ? '✅ Bot is an administrator in this channel!' : `❌ ${res.detail}`);
  loadMustJoin();
}

async function deleteChannel(id) {
  if (confirm('Remove this required channel?')) {
    await API.delete(`/api/must-join/channels/${id}`);
    loadMustJoin();
  }
}

// 7. YOUTUBE SETTINGS
async function loadYouTube() {
  const data = await API.get('/api/settings');
  document.getElementById('yt-max-height').value = data.default_max_height;
  document.getElementById('yt-playlists-toggle').checked = data.playlists_enabled;
  document.getElementById('yt-h264-toggle').checked = data.h264_enabled;
  document.getElementById('yt-h265-toggle').checked = data.h265_enabled;
  document.getElementById('yt-mp3-toggle').checked = data.mp3_enabled;
  document.getElementById('yt-subs-toggle').checked = data.subtitles_enabled;
}

// 8. COOKIES
async function loadCookies() {
  try {
    const data = await API.get('/api/cookies');
    const toggleBtn = document.getElementById('btn-cookie-toggle');
    if (data.enabled) {
      toggleBtn.innerText = 'YouTube Cookies: ON';
      toggleBtn.className = 'btn btn-success';
    } else {
      toggleBtn.innerText = 'YouTube Cookies: OFF';
      toggleBtn.className = 'btn btn-secondary';
    }

    document.getElementById('cookie-configured-val').innerText = data.configured ? 'Configured ✅' : 'Not Configured ❌';
    document.getElementById('cookie-count-val').innerText = data.cookie_count;
    document.getElementById('cookie-updated-val').innerText = formatDate(data.last_updated);
  } catch (err) {}
}

async function toggleCookies() {
  const data = await API.get('/api/cookies');
  await API.post('/api/cookies/toggle', { enabled: !data.enabled });
  loadCookies();
}

async function pasteCookiesSubmit(e) {
  e.preventDefault();
  const text = document.getElementById('cookie-paste-area').value;
  try {
    const res = await API.post('/api/cookies/paste', { content: text });
    alert(`✅ Successfully saved and validated ${res.cookie_count} cookies!`);
    document.getElementById('cookie-paste-area').value = '';
    loadCookies();
  } catch (err) {
    alert(`❌ ${err.message}`);
  }
}

async function testCookiesSubmit() {
  const url = prompt('Enter a YouTube URL to test cookie extraction:', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ');
  if (!url) return;
  try {
    const res = await API.post('/api/cookies/test', { url });
    alert(res.message);
  } catch (err) {
    alert(`❌ ${err.message}`);
  }
}

async function deleteCookiesFile() {
  if (confirm('Delete saved cookies file?')) {
    await API.delete('/api/cookies');
    loadCookies();
  }
}

// 9. AI TRANSLATION
async function loadAI() {
  try {
    const data = await API.get('/api/ai');
    document.getElementById('ai-enabled-toggle').checked = data.enabled;
    document.getElementById('ai-provider').value = data.provider;
    document.getElementById('ai-base-url').value = data.base_url;
    document.getElementById('ai-model').value = data.model;
    document.getElementById('ai-api-key-display').innerText = data.api_key_masked || 'None';
  } catch (err) {}
}

async function saveAISettings(e) {
  e.preventDefault();
  const enabled = document.getElementById('ai-enabled-toggle').checked;
  const provider = document.getElementById('ai-provider').value;
  const baseUrl = document.getElementById('ai-base-url').value;
  const model = document.getElementById('ai-model').value;
  const key = document.getElementById('ai-api-key-input').value;

  try {
    await API.post('/api/ai', {
      enabled,
      provider,
      base_url: baseUrl,
      model,
      api_key: key || null,
    });
    alert('AI Settings saved successfully!');
    document.getElementById('ai-api-key-input').value = '';
    loadAI();
  } catch (err) {
    alert(err.message);
  }
}

async function testAI() {
  try {
    const res = await API.post('/api/ai/test', {});
    alert(res.message);
  } catch (err) {
    alert(err.message);
  }
}

// 10. SETTINGS
async function loadSettings() {
  const data = await API.get('/api/settings');
  document.getElementById('set-max-dur').value = data.max_video_duration_seconds;
  document.getElementById('set-allow-unknown').checked = data.allow_unknown_duration;
  document.getElementById('set-cache-bypass').checked = data.cache_hit_bypasses_duration_limit;
  document.getElementById('set-max-per-user').value = data.max_concurrent_per_user;
  document.getElementById('set-max-queued').value = data.max_queued_per_user;
  document.getElementById('set-max-temp').value = data.max_temp_storage_gb;
}

async function saveGlobalSettings(e) {
  e.preventDefault();
  const payload = {
    max_video_duration_seconds: parseInt(document.getElementById('set-max-dur').value, 10),
    allow_unknown_duration: document.getElementById('set-allow-unknown').checked,
    cache_hit_bypasses_duration_limit: document.getElementById('set-cache-bypass').checked,
    max_concurrent_per_user: parseInt(document.getElementById('set-max-per-user').value, 10),
    max_queued_per_user: parseInt(document.getElementById('set-max-queued').value, 10),
    max_temp_storage_gb: parseInt(document.getElementById('set-max-temp').value, 10),
    default_max_height: parseInt(document.getElementById('yt-max-height')?.value || '1080', 10),
    playlists_enabled: document.getElementById('yt-playlists-toggle')?.checked || false,
    h264_enabled: document.getElementById('yt-h264-toggle')?.checked || true,
    h265_enabled: document.getElementById('yt-h265-toggle')?.checked || true,
    mp3_enabled: document.getElementById('yt-mp3-toggle')?.checked || true,
    subtitles_enabled: document.getElementById('yt-subs-toggle')?.checked || true,
  };

  try {
    await API.post('/api/settings', payload);
    alert('Settings saved and applied dynamically!');
    loadSettings();
  } catch (err) {
    alert(err.message);
  }
}

// 11. SYSTEM
async function loadSystem() {
  try {
    const ready = await API.get('/ready');
    document.getElementById('sys-ready-db').innerText = ready.database ? 'CONNECTED ✅' : 'ERROR ❌';
    document.getElementById('sys-ready-redis').innerText = ready.redis ? 'CONNECTED ✅' : 'ERROR ❌';
  } catch (err) {}
}

// 12. LOGS
async function loadLogs() {
  try {
    const data = await API.get('/api/logs?limit=100');
    const logBox = document.getElementById('log-display');
    if (data.logs.length === 0) {
      logBox.innerText = 'No logs available.';
    } else {
      logBox.innerText = data.logs.map(l => `[${l.timestamp}] [${l.level}] [${l.service}] ${l.message}`).join('\n');
    }
  } catch (err) {}
}

// 13. AUDIT
async function loadAudit() {
  try {
    const data = await API.get('/api/audit?limit=50');
    const tbody = document.getElementById('tbody-audit');
    if (data.items.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;">No audit logs</td></tr>';
    } else {
      tbody.innerHTML = data.items.map(a => `
        <tr>
          <td><code>${a.id}</code></td>
          <td><span class="badge badge-info">${a.action}</span></td>
          <td><b>${escapeHtml(a.admin_username)}</b></td>
          <td>${escapeHtml(a.details || '')}</td>
          <td>${formatDate(a.created_at)}</td>
        </tr>
      `).join('');
    }
  } catch (err) {}
}

// Helpers
function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function formatDate(isoStr) {
  if (!isoStr) return 'N/A';
  return new Date(isoStr).toLocaleString();
}

function getBadgeClass(status) {
  switch (status) {
    case 'COMPLETED': return 'badge-success';
    case 'FAILED': return 'badge-danger';
    case 'CANCELLED': return 'badge-secondary';
    case 'QUEUED': return 'badge-warning';
    default: return 'badge-info';
  }
}

// =============================================================================
// TELEGRAM ARCHITECTURE & SETTINGS
// =============================================================================
let currentTelegramConfig = {};
let targetMigrationMode = 'local';

async function loadTelegramConfig() {
  try {
    const data = await API.get('/api/telegram/config');
    currentTelegramConfig = data;

    // Set radio mode
    const radios = document.getElementsByName('tg-mode-radio');
    for (const r of radios) {
      if (r.value === data.mode) r.checked = true;
    }
    onTelegramModeChange();

    document.getElementById('tg-derived-endpoint').value = data.derived_endpoint || 'http://telegram-bot-api:8081';
    document.getElementById('tg-token-masked-display').innerText = data.bot_token_masked || 'Not Set';
    document.getElementById('tg-api-id').value = data.api_id || '';
    const idDisplay = document.getElementById('tg-id-display');
    if (idDisplay) idDisplay.innerText = data.api_id ? String(data.api_id) : 'Not Set';
    document.getElementById('tg-hash-masked-display').innerText = data.api_hash_masked || 'Not Set';
    document.getElementById('tg-cache-channel').value = data.cache_channel_id || '';
    document.getElementById('stat-tg-version').innerText = `v${data.config_version || 1}`;

    // Load dynamic welcome message
    await loadWelcomeMessage();

    // Update badges
    const badgeBot = document.getElementById('badge-tg-bot');
    if (data.is_configured) {
      badgeBot.className = 'badge badge-success';
      badgeBot.innerText = '🟢 Configured';
    } else {
      badgeBot.className = 'badge badge-danger';
      badgeBot.innerText = '🔴 Not Configured';
    }

    const badgeMode = document.getElementById('badge-tg-mode');
    badgeMode.innerText = data.mode === 'local' ? 'Local Bot API' : 'Cloud Bot API';
    badgeMode.className = data.mode === 'local' ? 'badge badge-info' : 'badge badge-warning';

    // Test Local API connectivity in background
    if (data.mode === 'local') {
      try {
        await API.post('/api/telegram/test-local-api', {});
        const badgeLocal = document.getElementById('badge-tg-local');
        badgeLocal.className = 'badge badge-success';
        badgeLocal.innerText = '🟢 Healthy';
      } catch (e) {
        const badgeLocal = document.getElementById('badge-tg-local');
        badgeLocal.className = 'badge badge-danger';
        badgeLocal.innerText = '🔴 Unreachable';
      }
    } else {
      const badgeLocal = document.getElementById('badge-tg-local');
      badgeLocal.className = 'badge badge-secondary';
      badgeLocal.innerText = '⚪ Standby / Disabled';
    }

    // Load disk telemetry stats from /api/system
    try {
      const sysData = await API.get('/api/system');
      if (sysData.stats) {
        document.getElementById('stat-transfer-usage').innerText = `${Math.round(sysData.stats.worker_transfer_gb * 1024)} MB`;
        document.getElementById('stat-worker-temp').innerText = `${Math.round(sysData.stats.worker_temp_gb * 1024)} MB`;
        document.getElementById('stat-botapi-data').innerText = `${Math.round(sysData.stats.bot_api_data_gb * 1024)} MB`;
        document.getElementById('stat-host-free').innerText = `${sysData.stats.disk_free_gb} GB`;
      }
    } catch (e) {}
  } catch (err) {
    console.error('Failed to load telegram config:', err);
  }
}

function onTelegramModeChange() {
  const selectedMode = document.querySelector('input[name="tg-mode-radio"]:checked')?.value || 'local';
  const localGroup = document.getElementById('tg-local-credentials-group');
  const endpointInput = document.getElementById('tg-derived-endpoint');

  if (selectedMode === 'local') {
    if (localGroup) localGroup.style.display = 'block';
    if (endpointInput) endpointInput.value = 'http://telegram-bot-api:8081';
  } else {
    if (localGroup) localGroup.style.display = 'none';
    if (endpointInput) endpointInput.value = 'https://api.telegram.org';
  }
}

function toggleInputMask(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.type = el.type === 'password' ? 'text' : 'password';
}

function showTelegramAlert(msg, isSuccess = true) {
  const el = document.getElementById('telegram-alert');
  if (!el) return;
  el.style.display = 'block';
  el.style.background = isSuccess ? 'rgba(40,167,69,0.15)' : 'rgba(220,53,69,0.15)';
  el.style.border = isSuccess ? '1px solid #28a745' : '1px solid #dc3545';
  el.style.color = isSuccess ? '#28a745' : '#dc3545';
  el.innerText = msg;
}

async function saveTelegramConfig() {
  const saveBtn = document.getElementById('btn-save-telegram');
  saveBtn.disabled = true;
  saveBtn.innerText = 'Validating & Saving...';

  const mode = document.querySelector('input[name="tg-mode-radio"]:checked')?.value || 'local';
  const token = document.getElementById('tg-bot-token').value.trim();
  const apiId = document.getElementById('tg-api-id').value.trim();
  const apiHash = document.getElementById('tg-api-hash').value.trim();
  const cacheChannel = document.getElementById('tg-cache-channel').value.trim();

  const payload = {
    mode: mode,
    bot_token: token || undefined,
    api_id: apiId ? parseInt(apiId, 10) : undefined,
    api_hash: apiHash || undefined,
    cache_channel_id: cacheChannel || undefined,
  };

  try {
    const res = await API.post('/api/telegram/save', payload);
    showTelegramAlert(`✅ ${res.message}`, true);
    document.getElementById('tg-bot-token').value = '';
    document.getElementById('tg-api-hash').value = '';
    await loadTelegramConfig();
  } catch (err) {
    if (err.message && err.message.includes('Conflict')) {
      showTelegramAlert('⚠️ Conflict: Another Telegram configuration update is already in progress. Please retry in a few seconds.', false);
    } else {
      showTelegramAlert(`❌ Error: ${err.message}`, false);
    }
  } finally {
    saveBtn.disabled = false;
    saveBtn.innerText = 'Save Telegram Configuration';
  }
}

async function testBotToken() {
  const mode = document.querySelector('input[name="tg-mode-radio"]:checked')?.value || 'local';
  const token = document.getElementById('tg-bot-token').value.trim();
  try {
    const res = await API.post('/api/telegram/test-token', { bot_token: token || undefined, mode: mode });
    alert(`✅ Bot Token Valid!\nBot: @${res.username} (${res.first_name})\nID: ${res.bot_id}\nEndpoint: ${res.endpoint}`);
  } catch (err) {
    alert(`❌ Bot Token Test Failed:\n${err.message}`);
  }
}

async function testLocalBotAPI() {
  try {
    const res = await API.post('/api/telegram/test-local-api', {});
    alert(`✅ Local Bot API Server is reachable at ${res.host}:${res.port}!`);
  } catch (err) {
    alert(`❌ Local Bot API Test Failed:\n${err.message}`);
  }
}

async function testCacheChannel() {
  const mode = document.querySelector('input[name="tg-mode-radio"]:checked')?.value || 'local';
  const token = document.getElementById('tg-bot-token').value.trim();
  const channelId = document.getElementById('tg-cache-channel').value.trim();
  if (!channelId) {
    alert('Please enter a Cache Channel ID to test.');
    return;
  }
  try {
    const res = await API.post('/api/telegram/test-channel', {
      channel_id: channelId,
      bot_token: token || undefined,
      mode: mode,
    });
    alert(`✅ Cache Channel Access Verified!\nChannel ID: ${res.channel_id}`);
  } catch (err) {
    alert(`❌ Cache Channel Test Failed:\n${err.message}`);
  }
}

function openMigrationModal(targetMode) {
  targetMigrationMode = targetMode;
  const modal = document.getElementById('modal-migration');
  const warn = document.getElementById('migration-warning');
  const desc = document.getElementById('migration-desc');

  if (targetMode === 'local') {
    warn.innerText = '⚠️ Caution: Migrating from Cloud to Local Bot API calls logOut() on Telegram Cloud. Returning to Cloud API is blocked by Telegram for 10 minutes following that operation.';
    desc.innerText = 'This will log out the cloud session, verify Local Bot API server connectivity, and transition all polling and file uploads to the internal Local Bot API server (up to 2000 MB).';
  } else {
    warn.innerText = '⚠️ Note: Cloud Bot API limits uploads to 50 MB. Local Bot API will enter standby mode.';
    desc.innerText = 'This will transition the bot session to Telegram Cloud API (https://api.telegram.org). Any media file exceeding 50 MB will be rejected.';
  }
  modal.classList.add('active');
}

async function executeMigration() {
  const btn = document.getElementById('btn-confirm-migration');
  btn.disabled = true;
  btn.innerText = 'Migrating...';
  try {
    const res = await API.post('/api/telegram/migrate', { target_mode: targetMigrationMode });
    alert(`✅ Migration Complete!\n${res.message}`);
    document.getElementById('modal-migration').classList.remove('active');
    await loadTelegramConfig();
  } catch (err) {
    alert(`❌ Migration Failed:\n${err.message}`);
  } finally {
    btn.disabled = false;
    btn.innerText = 'Confirm & Migrate';
  }
}

async function saveMustJoinMessage() {
  const msg = document.getElementById('mj-custom-msg-area').value;
  try {
    await API.post('/api/must-join/message', { message: msg });
    alert('✅ Custom Must-Join message saved successfully!');
  } catch (err) {
    alert(`❌ Failed to save: ${err.message}`);
  }
}

async function resetMustJoinMessage() {
  if (confirm('Reset Must-Join template message to system default?')) {
    try {
      const res = await API.post('/api/must-join/message/reset', {});
      document.getElementById('mj-custom-msg-area').value = res.message;
      alert('✅ Reset to default template!');
    } catch (err) {
      alert(`❌ Failed to reset: ${err.message}`);
    }
  }
}

// =============================================================================
// TELEGRAM /start WELCOME MESSAGE
// =============================================================================

async function loadWelcomeMessage() {
  try {
    const res = await API.get('/api/telegram/welcome-message');
    const area = document.getElementById('tg-welcome-msg-area');
    if (area) {
      area.value = res.message || '';
    }
  } catch (err) {
    console.error('Failed to load welcome message:', err);
  }
}

function showWelcomeAlert(msg, isSuccess = true) {
  const el = document.getElementById('tg-welcome-alert');
  if (!el) return;
  el.style.display = 'block';
  el.style.background = isSuccess ? 'rgba(40,167,69,0.15)' : 'rgba(220,53,69,0.15)';
  el.style.border = isSuccess ? '1px solid #28a745' : '1px solid #dc3545';
  el.style.color = isSuccess ? '#28a745' : '#dc3545';
  el.innerText = msg;
}

async function saveWelcomeMessage() {
  const btn = document.getElementById('btn-save-welcome');
  if (btn) {
    btn.disabled = true;
    btn.innerText = 'Validating & Saving...';
  }
  const area = document.getElementById('tg-welcome-msg-area');
  const msg = area ? area.value : '';

  try {
    const res = await API.post('/api/telegram/welcome-message', { message: msg });
    if (area) {
      area.value = res.message;
    }
    showWelcomeAlert('✅ Welcome message saved successfully!', true);
  } catch (err) {
    showWelcomeAlert(`❌ Failed to save welcome message: ${err.message}`, false);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerText = 'Save Welcome Message';
    }
  }
}

async function resetWelcomeMessage() {
  if (confirm('Reset /start welcome message to system default?')) {
    const btn = document.getElementById('btn-reset-welcome');
    if (btn) {
      btn.disabled = true;
      btn.innerText = 'Resetting...';
    }
    try {
      const res = await API.post('/api/telegram/welcome-message/reset', {});
      const area = document.getElementById('tg-welcome-msg-area');
      if (area) {
        area.value = res.message || '';
      }
      showWelcomeAlert('✅ Welcome message reset to default template!', true);
    } catch (err) {
      showWelcomeAlert(`❌ Failed to reset welcome message: ${err.message}`, false);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerText = 'Reset to Default';
      }
    }
  }
}

