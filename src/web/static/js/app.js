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
